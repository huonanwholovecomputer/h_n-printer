# -*- coding: utf-8 -*-
"""2026-11-02 现场事故回归：预约单「未到点不进标签页」+ 后台转换不得改写前台标签页

事故 1（HN20261102-0005 打了三份、0006/0007 一页未打）
    `_on_convert_finished` 尾部原有一句**无守卫**的 `self._set_current_jobs(jobs)`，而
    `jobs = self._tab_jobs(tab_key)` 是**目标标签页**的列表对象 ⇒ 后台标签页转换完成时，
    前台标签页的 jobs 被整表替换成那一页的列表（两个标签页从此共享同一个 list）。
    08:18:35/38 两次 tab3 的转换把订单 #956 的两个文件分别灌进 tab6/tab7；随后在前台
    点「开始打印」，打出来的是 #956（连打三份），#1943/#1944 的 job 对象被孤立、从未打印。
    修法：只在目标页 == 前台页时才 `_set_current_jobs`；`_set_current_jobs` 另加别名防御。

事故 2（#951 重启后消失 / 未到点的单子摆在标签页里）
    旧实现「文件全部就绪即建标签页」⇒ 未到点的预约单出现在工作区，可被误点提前打印；
    关工具时又按「标签页里未打印的云端 job」上报 abandoned（用户 2026-11-02 明确否掉）。
    现在：未到点只下载到本地 + 打印队列显示，到点由 `_fire_scheduled_print` 现建页并锁定；
    状态机落盘，重启后自己恢复。

验收方式：APPDATA 沙箱（不碰真实配置/缓存）+ offscreen 真实 MainWindow，直接驱动这几个
方法；不打桩被验的方法本身，只桩打印链与网络。
"""
import os
import sys
import json
import shutil
import tempfile

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LOCAL_TOOL = os.path.join(REPO, "local_print_tool")

_SANDBOX = tempfile.mkdtemp(prefix="hn_sched_notab_")
os.environ["APPDATA"] = _SANDBOX                  # 用户数据目录 → 沙箱
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, LOCAL_TOOL)
os.chdir(LOCAL_TOOL)

from PySide6.QtWidgets import QApplication  # noqa: E402

_app = QApplication.instance() or QApplication([])

import gui    # noqa: E402
import paths  # noqa: E402

ok_all = True


def check(name, cond, detail=""):
    global ok_all
    if not cond:
        ok_all = False
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


def make_pdf(path, pages=1):
    """造一个真 PDF：`_on_convert_finished` 会调 `_detect_blank_tail` 真去读它。"""
    import fitz
    doc = fitz.open()
    for _ in range(pages):
        doc.new_page()
    doc.save(path)
    doc.close()
    return path


class FakeTask:
    """预约状态机只用到 task_id / order_id / file_name / status / local_path。"""

    def __init__(self, task_id, order_id, file_name="f.pdf", local_path=""):
        self.task_id = task_id
        self.order_id = order_id
        self.file_name = file_name
        self.local_path = local_path
        self.status = "ready"


def new_win(cloud=True):
    w = gui.MainWindow(config_path=paths.config_path())
    if not cloud:
        w._cloud_client = None
    w._cloud_connected = True
    # 打印链打桩：真实 _start_print_worker 会做成员校验（弹窗阻塞）与引擎探测（COM）。
    w._hold_for_manual_duplex = lambda jobs, tab_key, reason: False
    w._refresh_engine_availability = lambda *a, **kw: None
    return w


def put_tab(win, key, jobs):
    tab = gui.TabSettings()
    tab.jobs = list(jobs)
    win._config.tabs[key] = tab
    return tab


# ══════════════════════════════════════════════════════════════════════════
print("=== 1. 后台标签页转换完成，绝不能改写前台标签页（事故 1 根因）===")
win = new_win(cloud=False)
win._config.tabs = {}
_pdf3 = make_pdf(os.path.join(_SANDBOX, "t3.pdf"), 1)
_src3 = os.path.join(_SANDBOX, "订单0005.docx")
open(_src3, "wb").write(b"x")
_src6 = os.path.join(_SANDBOX, "订单0006.doc")
open(_src6, "wb").write(b"x")
_job3 = gui.PrintJob(file_path=_src3, order_id=956, task_id=1941, page_count=0)
_job3.source_md5 = "d41d8cd98f00b204e9800998ecf8427e"   # 走 "保存 source_md5" 那条分支
_job6 = gui.PrintJob(file_path=_src6, order_id=957, task_id=1943, page_count=0)
put_tab(win, "3", [_job3])
put_tab(win, "6", [_job6])
_list6_before = win._config.tabs["6"].jobs
win._current_tab = "6"
win._config.active_tab = "6"

win._on_convert_finished(0, _src3, _pdf3, 1, "portrait", tab_key="3")

check("后台页(tab3)自己的 job 已回写 cached_pdf", _job3.cached_pdf == _pdf3,
      f"{_job3.cached_pdf!r}")
check("★ 前台页(tab6)的 jobs **列表对象**没有被换掉",
      win._config.tabs["6"].jobs is _list6_before,
      "被 _set_current_jobs 别名替换 = 事故 1 复现")
check("★ 前台页(tab6)里仍是自己的订单 957 文件，没被灌进 #956",
      [j.order_id for j in win._config.tabs["6"].jobs] == [957],
      f"{[j.order_id for j in win._config.tabs['6'].jobs]}")
check("两个标签页不再共享同一个 list",
      win._config.tabs["3"].jobs is not win._config.tabs["6"].jobs)

# 直接验别名防御：把 tab3 的列表对象塞给当前页
win._set_current_jobs(win._config.tabs["3"].jobs)
check("★ _set_current_jobs 对「别的标签页的 list 对象」做复制隔离（不共享）",
      win._config.tabs["6"].jobs is not win._config.tabs["3"].jobs,
      "两页共享同一 list ⇒ 一页改动会串到另一页")
check("隔离后内容仍按传入值落地",
      [j.order_id for j in win._config.tabs["6"].jobs] == [956],
      f"{[j.order_id for j in win._config.tabs['6'].jobs]}")

# ══════════════════════════════════════════════════════════════════════════
print("\n=== 2. 未到点的预约单：只下载到本地 + 队列，不进标签页 ===")
win2 = new_win(cloud=False)
win2._config.tabs = {"1": gui.TabSettings()}
win2._current_tab = "1"
_oid = 4242
_t1 = FakeTask(9001, _oid, "a.pdf")
_t2 = FakeTask(9002, _oid, "b.pdf")
win2._scheduled_orders = {
    _oid: {"target_ts": int(__import__("time").time()) + 3600, "ready": {9001: _t1, 9002: _t2},
           "pending": {}, "timer": None, "print_timer": None, "frozen": False,
           "delayed_sent": False, "printed": False, "printed_ts": 0,
           "tab_key": None, "retry_count": 0, "queued": False},
}
_calls = {"add_tab": 0}


def _no_add(tasks, switch_tab=True):
    _calls["add_tab"] += 1
    return "99"


win2._add_cloud_tasks_to_new_tab = _no_add
win2._evaluate_scheduled_order(_oid)
_st = win2._scheduled_orders[_oid]
check("★ 就绪后**没有**建标签页", _calls["add_tab"] == 0, f"建页次数 {_calls['add_tab']}")
check("★ 状态机标记 queued（已下载、未到点）", _st.get("queued") is True, str(_st.get("queued")))
check("tab_key 仍为空（到点才建页）", _st.get("tab_key") is None, str(_st.get("tab_key")))
check("工作区里没有凭空多出标签页", list(win2._config.tabs) == ["1"], f"{list(win2._config.tabs)}")
check("到点打印定时器已排上", _st.get("print_timer") is not None)
check("打印列表里能看到这一单（等待中·预约）",
      any(r.get("state", "").startswith("等待中（预约）")
          for r in win2._local_queue_rows()),
      f"{[r.get('state') for r in win2._local_queue_rows()]}")

# ══════════════════════════════════════════════════════════════════════════
print("\n=== 3. 到点：现建标签页 + 立刻锁定 + 开打 ===")
win3 = new_win(cloud=False)
win3._config.tabs = {}
win3._current_tab = "1"
win3._config.active_tab = "1"
_oid3 = 4243
_f3 = make_pdf(os.path.join(_SANDBOX, "sched3.pdf"), 2)
_started = {"n": 0}


def _stub_add(tasks, switch_tab=True):
    tab = gui.TabSettings()
    tab.jobs = [gui.PrintJob(file_path=_f3, order_id=_oid3, task_id=9003, page_count=2)]
    win3._config.tabs["9"] = tab
    return "9"


def _stub_start(jobs, **kw):
    _started["n"] += 1
    _started["tab_key"] = kw.get("tab_key")
    return True


win3._add_cloud_tasks_to_new_tab = _stub_add
win3._start_print_worker = _stub_start
win3._scheduled_orders = {
    _oid3: {"target_ts": 1, "ready": {9003: FakeTask(9003, _oid3, "sched3.pdf")},
            "pending": {}, "timer": None, "print_timer": None, "frozen": False,
            "delayed_sent": False, "printed": False, "printed_ts": 0,
            "tab_key": None, "retry_count": 0, "queued": True},
}
win3._fire_scheduled_print(_oid3)
check("★ 到点真的建了标签页并开打", _started["n"] == 1, f"开打次数 {_started['n']}")
check("★ 新建的标签页立刻锁定（frozen=True）",
      bool(win3._config.tabs.get("9") and win3._config.tabs["9"].frozen),
      f"frozen={getattr(win3._config.tabs.get('9'), 'frozen', None)}")
check("打印挂在正确的标签页上", _started.get("tab_key") == "9", str(_started.get("tab_key")))
check("状态机置 printed（不再重复打）", win3._scheduled_orders[_oid3]["printed"] is True)

# ══════════════════════════════════════════════════════════════════════════
print("\n=== 4. 落盘/恢复：重启后预约单不丢、且不会退化成「立即打印」 ===")
win4 = new_win(cloud=False)
_oid4 = 4244
_f4 = make_pdf(os.path.join(_SANDBOX, "sched4.pdf"), 1)
_ct = gui.CloudTask({
    "task_id": 9004, "order_id": _oid4, "order_number": "HN20261102-0005",
    "file_name": "sched4.pdf", "copies": 1, "duplex": "on",
    "auto_print": True, "schedule_mode": "appointment",
    "scheduled_at": "2026-11-02 08:22:00",
    "scheduled_ts": 1793600000, "remark": "备注要留住", "source": "wechat",
})
_ct.local_path = _f4
_ct.status = "ready"
win4._scheduled_orders = {
    _oid4: {"target_ts": 1793600000, "ready": {9004: _ct}, "pending": {},
            "timer": None, "print_timer": None, "frozen": False, "delayed_sent": False,
            "printed": False, "printed_ts": 0, "tab_key": None, "retry_count": 0,
            "queued": True},
}
win4._save_scheduled_state()
_state_path = win4._scheduled_state_path()
check("状态文件已落盘", os.path.isfile(_state_path), _state_path)
_raw = json.load(open(_state_path, encoding="utf-8")) if os.path.isfile(_state_path) else {}
check("落盘含 schedule_mode / auto_print（to_dict 会丢，必须自己补）",
      _raw.get(str(_oid4), {}).get("tasks", [{}])[0].get("schedule_mode") == "appointment"
      and _raw.get(str(_oid4), {}).get("tasks", [{}])[0].get("auto_print") is True,
      str(_raw.get(str(_oid4), {}).get("tasks", [{}])[0])[:120])

win5 = new_win(cloud=False)
win5._cloud_client = object()          # 有客户端才允许恢复（否则建不了页）
win5._scheduled_orders = {}
win5._load_scheduled_state()
check("★ 重启后预约单被恢复", _oid4 in win5._scheduled_orders,
      f"{sorted(win5._scheduled_orders)}")
_rt = (win5._scheduled_orders.get(_oid4, {}).get("ready") or {}).get(9004)
check("★ 恢复后仍是预约单（schedule_mode/auto_print 不丢）",
      _rt is not None and getattr(_rt, "schedule_mode", "") == "appointment"
      and bool(getattr(_rt, "auto_print", False)),
      f"schedule_mode={getattr(_rt, 'schedule_mode', None)!r}")
check("恢复后 local_path / 备注保留",
      _rt is not None and _rt.local_path == _f4 and getattr(_rt, "remark", "") == "备注要留住",
      f"{getattr(_rt, 'local_path', None)!r}")
check("恢复后仍是「已下载未到点」（不建标签页）",
      win5._scheduled_orders[_oid4].get("queued") is True
      and win5._scheduled_orders[_oid4].get("tab_key") is None)

# ══════════════════════════════════════════════════════════════════════════
print("\n=== 5. 关工具不算放弃：预约单不进标签页 ⇒ 退出时不 abandon ===")
from PySide6.QtWidgets import QMessageBox as _QMB  # noqa: E402

_QMB.question = staticmethod(lambda *a, **kw: _QMB.Yes)
_QMB.information = staticmethod(lambda *a, **kw: _QMB.Ok)
_QMB.warning = staticmethod(lambda *a, **kw: _QMB.Ok)

win6 = new_win(cloud=False)
_abandoned = []
win6._cloud_client = type("_C", (), {
    "abandon_order_to_server": lambda self, oid: _abandoned.append(oid),
    "abandon_reserved_order": lambda self, *a, **kw: _abandoned.append(a),
    "stop": lambda self: None,
    "is_connected": lambda self: False,
})()
win6._config.tabs = {"1": gui.TabSettings()}
win6._current_tab = "1"
win6._scheduled_orders = {
    777: {"target_ts": 1793600000, "ready": {9100: FakeTask(9100, 777, "x.pdf")},
          "pending": {}, "timer": None, "print_timer": None, "frozen": False,
          "delayed_sent": False, "printed": False, "printed_ts": 0,
          "tab_key": None, "retry_count": 0, "queued": True},
}
try:
    win6.close()
except Exception as e:      # closeEvent 里任何与云端/托盘相关的异常都不影响判据
    print(f"  (close 抛了 {type(e).__name__}: {e}，按判据继续)")
check("★ 退出时没有对预约单调用 abandon（关工具不算放弃）",
      _abandoned == [], f"{_abandoned}")
check("退出时预约单状态仍留在盘上（下次启动继续）",
      os.path.isfile(win6._scheduled_state_path())
      and "777" in json.load(open(win6._scheduled_state_path(), encoding="utf-8")),
      win6._scheduled_state_path())

for w in (win, win2, win3, win4, win5):
    try:
        w.close()
    except Exception:
        pass

print("\n" + ("全部通过" if ok_all else "存在失败项"))
shutil.rmtree(_SANDBOX, ignore_errors=True)
sys.exit(0 if ok_all else 1)
