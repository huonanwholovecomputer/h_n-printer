# -*- coding: utf-8 -*-
"""云端新订单落地时的前台切换口径（2026-10 用户要求）

规则（`gui.MainWindow._cloud_new_tab_should_focus`）：
收到云端新增订单、要建/定位标签页时，若**当前标签页为空**或**已锁定（frozen）**，
就把前台切到那个标签页；当前标签页有未打印任务且未锁定时保持原行为（不抢前台）。

现场动机：打完一单标签页锁着（或刚开工具是空白页），无障碍订单在后台悄悄建了
标签页，操作员在界面上看不到它 —— 得手动点 ＋ 才能找到。

覆盖：
  ① 后台落地（switch_tab=False：无障碍 / 预约）× {空 / 已锁定 / 有任务未锁定}
  ② 同一订单追加到已有标签页时同样按此判据切
  ③ 人工确认路径（switch_tab=True）永远切（零回归）
  ④ 当前标签页 key 失效 → 视为无可保护的前台
  ⑤ 前台**真的换了**（标签号 + 表格内容），不只是变量
  ⑥ 无障碍真实入口 `_process_auto_print_order` 端到端
  ⑦ 静态：三个后台调用点都汇入同一个漏斗（不会漏改）
"""
import os
import re
import shutil
import sys
import tempfile

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL_TOOL = os.path.join(REPO, "local_print_tool")

_SANDBOX = tempfile.mkdtemp(prefix="hn_tabfocus_")
os.environ["APPDATA"] = _SANDBOX
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, LOCAL_TOOL)
os.chdir(LOCAL_TOOL)

from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

_app = QApplication.instance() or QApplication([])
QMessageBox.information = staticmethod(lambda *a, **kw: QMessageBox.Ok)
QMessageBox.warning = staticmethod(lambda *a, **kw: QMessageBox.Ok)
QMessageBox.question = staticmethod(lambda *a, **kw: QMessageBox.Yes)

import gui    # noqa: E402
import paths  # noqa: E402

ok_all = True


def check(name, cond, detail=""):
    global ok_all
    if not cond:
        ok_all = False
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


class FakeTask:
    """`_add_cloud_task_to_new_tab` 只会读这些字段（local_path 指向不存在的文件：
    page_count 保持 0、不触发转换线程，前台切换逻辑与文件内容无关）。"""

    def __init__(self, task_id, order_id, file_name=None):
        self.task_id = task_id
        self.order_id = order_id
        self.status = "ready"
        self.auto_print = True
        self.schedule_mode = "now"
        self.file_name = file_name or f"f{task_id}.pdf"
        self.local_path = os.path.join(_SANDBOX, self.file_name)
        self.source_md5 = ""
        self.image_orientation = "auto"
        self.copies = 1
        self.duplex = "on"
        self.page_range = ""
        self.order_number = f"HN20261017-{order_id}"
        self.delivery_enabled = False
        self.delivery_location = ""
        self.urgency = "低"
        self.cover_page = False
        self.cover_page_price = 0.1
        self.remark = ""
        self.is_admin_print = False
        self.owner_name = ""
        self.bound_owner_name = ""


class CloudStub:
    def __init__(self):
        self.accepted = []
        self.abandoned = []

    def accept_order_to_server(self, order_id):
        self.accepted.append(order_id)
        return "ok"

    def accept_task(self, task_id):
        pass

    def abandon_order_to_server(self, order_id):
        self.abandoned.append(order_id)


def new_win():
    w = gui.MainWindow(config_path=paths.config_path())
    w._cloud_client = None
    w._start_print_worker = lambda jobs, **kw: True
    w._manual_flip_required_for = lambda jobs: False
    w._auto_print_queue = {}
    w._auto_print_timers = {}
    w._enqueued_cloud_tasks = set()
    w._auto_print_retry = []
    w._config.tabs = {"1": gui.TabSettings()}
    w._current_tab = "1"
    w._config.active_tab = "1"
    w._rebuild_table()
    w._refresh_tab_display()
    return w


def put_job(w, key, task_id, order_id, sent=False):
    w._config.tabs[key].jobs.append(gui.PrintJob(
        file_path="x.pdf", task_id=task_id, order_id=order_id,
        page_count=1, sent=sent))
    w._rebuild_table()


print("=== ① 当前标签页为空（无障碍/预约的 switch_tab=False）→ 切到新标签页 ===")
w1 = new_win()
key = w1._add_cloud_tasks_to_new_tab([FakeTask(101, 501)], switch_tab=False)
check("空标签页：新标签页照样建", key == "2" and "2" in w1._config.tabs, f"key={key}")
check("空标签页：前台已切到新标签页", w1._current_tab == "2" and w1._config.active_tab == "2",
      f"current={w1._current_tab} active={w1._config.active_tab}")
check("空标签页：前台真的换了（标签号 + 表格显示新订单的文件）",
      w1._tab_label.text() == "2" and w1._table.rowCount() == 1
      and w1._table.item(0, gui.MainWindow.COL_FILE) is not None
      and "f101.pdf" in w1._table.item(0, gui.MainWindow.COL_FILE).text(),
      f"label={w1._tab_label.text()} rows={w1._table.rowCount()}")

print("\n=== ② 当前标签页有未打印任务且未锁定 → 不抢前台（旧行为不变）===")
w2 = new_win()
put_job(w2, "1", 11, 501)
key = w2._add_cloud_tasks_to_new_tab([FakeTask(102, 502)], switch_tab=False)
check("有任务未锁定：新标签页照样建（不丢单）", key == "2" and "2" in w2._config.tabs, f"key={key}")
check("有任务未锁定：前台不抢，仍在 1", w2._current_tab == "1" and w2._config.active_tab == "1",
      f"current={w2._current_tab}")
check("有任务未锁定：表格仍显示 1 的内容（未被换页）",
      w2._tab_label.text() == "1" and w2._table.rowCount() == 1)

print("\n=== ③ 当前标签页已锁定（frozen，打印已开始/订单已固定）→ 切到新标签页 ===")
w3 = new_win()
put_job(w3, "1", 12, 513, sent=True)
w3._config.tabs["1"].frozen = True
w3._refresh_tab_display()
key = w3._add_cloud_tasks_to_new_tab([FakeTask(103, 503)], switch_tab=False)
check("已锁定：前台切到新标签页", key == "2" and w3._current_tab == "2" and w3._config.active_tab == "2",
      f"key={key} current={w3._current_tab}")
check("已锁定：新标签页可编辑（没有把 frozen 带过去）",
      not w3._config.tabs["2"].frozen and w3._btn_start.isEnabled() is True)

print("\n=== ④ 同一订单追加（文件分散到达）→ 按同判据切到该订单已有标签页 ===")
w5 = new_win()
w5._config.tabs["1"].jobs.append(gui.PrintJob(file_path="a.pdf", task_id=21,
                                              order_id=601, page_count=1))
w5._config.tabs["2"] = gui.TabSettings()
w5._config.tabs["2"].frozen = True
w5._current_tab = "2"
w5._config.active_tab = "2"
key = w5._add_cloud_tasks_to_new_tab([FakeTask(104, 601)], switch_tab=False)
check("同订单追加：定位到已有订单标签页 1 并切过去",
      key == "1" and w5._current_tab == "1", f"key={key} current={w5._current_tab}")
check("同订单追加：没有另起新标签页", sorted(w5._config.tabs.keys()) == ["1", "2"],
      f"tabs={sorted(w5._config.tabs.keys())}")

w5b = new_win()
w5b._config.tabs["1"].jobs.append(gui.PrintJob(file_path="a.pdf", task_id=22,
                                               order_id=602, page_count=1))
w5b._config.tabs["2"] = gui.TabSettings()
put_job(w5b, "2", 23, 603)          # 当前页有任务、未锁定
w5b._current_tab = "2"
w5b._config.active_tab = "2"
key = w5b._add_cloud_tasks_to_new_tab([FakeTask(105, 602)], switch_tab=False)
check("同订单追加 + 当前页有任务未锁定 → 不抢前台（仍在 2）",
      key == "1" and w5b._current_tab == "2", f"key={key} current={w5b._current_tab}")

print("\n=== ⑤ 人工确认路径（switch_tab=True）永远切 —— 零回归 ===")
w4 = new_win()
put_job(w4, "1", 13, 514)
key = w4._add_cloud_tasks_to_new_tab([FakeTask(106, 504)], switch_tab=True)
check("人工确认：当前页有任务未锁定也照样切到新标签页",
      key == "2" and w4._current_tab == "2", f"key={key} current={w4._current_tab}")

print("\n=== ⑥ 当前标签页 key 失效（已被删除/重编号）→ 视为无可保护的前台 ===")
w6 = new_win()
w6._current_tab = "9"
w6._config.active_tab = "9"
key = w6._add_cloud_tasks_to_new_tab([FakeTask(107, 505)], switch_tab=False)
check("失效 key：切到新标签页", key == "2" and w6._current_tab == "2",
      f"key={key} current={w6._current_tab}")

print("\n=== ⑦ 无障碍真实入口 `_process_auto_print_order` 端到端 ===")
w7 = new_win()
stub = CloudStub()
w7._cloud_client = stub
w7._processed_cloud_tasks = set()
w7._mark_processed_task = lambda tid: w7._processed_cloud_tasks.add(tid)
w7._auto_print_queue = {701: [FakeTask(201, 701)]}
w7._process_auto_print_order(701)
check("无障碍订单：入口即把前台切到新标签页", w7._current_tab == "2" and w7._config.active_tab == "2",
      f"current={w7._current_tab}")
check("无障碍订单：表格显示该订单文件、且已 accept",
      w7._table.rowCount() == 1 and stub.accepted == [701],
      f"rows={w7._table.rowCount()} accepted={stub.accepted}")

w7b = new_win()
stub7b = CloudStub()
w7b._cloud_client = stub7b
w7b._processed_cloud_tasks = set()
w7b._mark_processed_task = lambda tid: w7b._processed_cloud_tasks.add(tid)
put_job(w7b, "1", 31, 712)          # 操作员正在准备的一单（未锁定）
w7b._auto_print_queue = {702: [FakeTask(202, 702)]}
w7b._process_auto_print_order(702)
check("无障碍订单 + 操作员正在编辑 → 不抢前台（仍在 1）",
      w7b._current_tab == "1" and stub7b.accepted == [702],
      f"current={w7b._current_tab} accepted={stub7b.accepted}")

print("\n=== ⑧ 切页前结算旧标签页的「撤回清空」窗口（`_focus_cloud_tab`）===")
w8 = new_win()
put_job(w8, "1", 41, 801)
stub8 = CloudStub()
w8._cloud_client = stub8
w8._on_clear_list()                     # 清空 tab1 → 空标签页 + 5 秒撤回窗口
check("前置：tab1 已清空、撤回窗口已开",
      w8._config.tabs["1"].jobs == [] and w8._clear_undo_timer.isActive()
      and "撤回清空" in w8._btn_clear.text(),
      f"jobs={len(w8._config.tabs['1'].jobs)} active={w8._clear_undo_timer.isActive()}")
key = w8._add_cloud_tasks_to_new_tab([FakeTask(108, 508)], switch_tab=False)
check("撤回窗口内的空标签页：照样切到新订单标签页", key == "2" and w8._current_tab == "2",
      f"key={key} current={w8._current_tab}")
check("撤回窗口已结算：备份不留在旧 key 上、定时器已停",
      w8._cleared_jobs_backup == {} and not w8._clear_undo_timer.isActive(),
      f"backup={list(w8._cleared_jobs_backup)} active={w8._clear_undo_timer.isActive()}")
check("清空已确认 → 被清空的云端任务已通知后端放弃（不静默丢）",
      stub8.abandoned == [801], f"abandoned={stub8.abandoned}")
check("清空按钮文案已恢复", "撤回清空" not in w8._btn_clear.text(), w8._btn_clear.text())

print("\n=== ⑨ 静态：三个后台调用点都汇入同一漏斗（判据不会漏改）===")
src = open(os.path.join(LOCAL_TOOL, "gui.py"), encoding="utf-8").read()
check("判据唯一实现在 `_cloud_new_tab_should_focus`",
      src.count("def _cloud_new_tab_should_focus") == 1
      and "def _cloud_new_tab_should_focus" in src)
check("漏斗内按 frozen / jobs 为空判定",
      re.search(r"if getattr\(tab, \"frozen\", False\):\n\s+return True\n\s+return len"
                r"\(getattr\(tab, \"jobs\", None\) or \[\]\) == 0", src) is not None)
check("`switch_tab=False` 时经判据提升",
      "if not switch_tab and self._cloud_new_tab_should_focus():" in src)
_bg_calls = re.findall(r"_add_cloud_tasks_to_new_tab\(ready_tasks, switch_tab=False\)", src)
check("三处后台调用点仍是 switch_tab=False（无障碍 + 预约两处）",
      len(_bg_calls) == 3, f"count={len(_bg_calls)}")

for w in (w1, w2, w3, w4, w5, w5b, w6, w7, w7b, w8):
    try:
        w.close()
    except Exception:
        pass

print("\n" + ("全部通过 ✅" if ok_all else "存在失败项 ❌"))
shutil.rmtree(_SANDBOX, ignore_errors=True)
sys.exit(0 if ok_all else 1)
