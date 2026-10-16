# -*- coding: utf-8 -*-
"""批次 B2 / B3 验收：预约单与标签页生命周期脱钩（打错订单 / 永久冻死）

🔴-B2 事故形状
    预约单状态机把归属记在 `st["tab_key"]`（数字串）。标签页管理窗口的三个删除入口
    （删除单个 / 清空全部 / 删除已完成）只做了 `del self._config.tabs[key]` +
    `_renumber_tabs()`，**没有**联动清理 `_scheduled_orders`。被删标签页的 key 不在
    重编号的 key_map 里 ⇒ 预约单保留旧 key，而重编号后该数字 key 已被**另一个**标签页
    接管 ⇒ 到点会把那个订单的文件打印出来，并把归属/计费挂到别人的标签页快照上
    （白费纸 + 账目串单）。
    （对照：`_auto_print_retry` 有 `_remap_auto_print_retry` + 按 order_id 反查兜底，
    `_scheduled_orders` 原本没有等价物。）

🔴-B3 事故形状
    后端只在 file_ready 那一瞬清 `schedule_frozen`（且要求"同一刻全部就绪"）。最后一次
    file_ready 早于冻结置位时两侧**永久冻结**：本地 `_check_scheduled_timeouts` 的
    `if not st["pending"]: continue` 每秒跳过它，`_evaluate_scheduled_order` 的冻结分支
    又只在断网时排自恢复 ⇒ 订单文件已齐，却永不打印、也不上报，客户端毫无自救路径。

验收方式：APPDATA 沙箱（不碰真实配置/缓存）里构造真实 MainWindow，直接驱动这四个
删除入口与预约单状态机。
"""
import io
import os
import shutil
import sys
import tempfile

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

if os.environ.get("HN_DUMP_STACK"):
    import faulthandler
    faulthandler.enable()
    faulthandler.dump_traceback_later(60, exit=True)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL_TOOL = os.path.join(REPO, "local_print_tool")

_SANDBOX = tempfile.mkdtemp(prefix="hn_sched_tab_")
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


class FakeTask:
    """`_fire_scheduled_print` 只用到 task_id / order_id / file_name。"""

    def __init__(self, task_id, order_id, file_name="f.pdf"):
        self.task_id = task_id
        self.order_id = order_id
        self.file_name = file_name


def new_win():
    w = gui.MainWindow(config_path=paths.config_path())
    w._cloud_client = None            # 无云端客户端：不触发任何网络请求
    w._cloud_connected = True         # 默认按"在线"（B3 的缺陷恰恰只在在线时暴露）
    # 打印链一律打桩：真实 _start_print_worker 会做成员校验（弹窗阻塞）与引擎探测（COM），
    # 与本脚本要验的"预约单状态机"无关。
    w._hold_for_manual_duplex = lambda jobs, tab_key, reason: False
    w._start_print_worker = lambda jobs, **kw: True
    return w


def put_order_tab(win, key, order_id, sent=False):
    """造一个挂了该订单文件的标签页。"""
    tab = gui.TabSettings()
    tab.jobs = [gui.PrintJob(file_path=os.path.join(_SANDBOX, f"{order_id}.pdf"),
                             order_id=order_id, task_id=order_id,
                             page_count=1, sent=sent)]
    win._config.tabs[key] = tab
    return tab


def make_st(tab_key, ready=None, frozen=False):
    """预约单状态机条目（字段与 `_register_scheduled_task` 的 setdefault 一致）。"""
    return {"target_ts": 0, "ready": dict(ready or {}), "pending": {},
            "timer": None, "print_timer": None, "frozen": frozen,
            "delayed_sent": False, "printed": False, "printed_ts": 0,
            "tab_key": tab_key, "retry_count": 0}


# ══════════════════════════════════════════════════════════════════════════
print("=== 1. 🔴-B2 删除标签页必须清理指向它的预约单（不得残留旧 key）===")
win = new_win()
win._config.tabs = {}
put_order_tab(win, "1", 101)
put_order_tab(win, "2", 202)
win._current_tab = "1"
win._config.active_tab = "1"
win._scheduled_orders = {101: make_st("1"), 202: make_st("2")}

removed = win._remove_tabs(["1"], reason="删除标签页")
check("返回实际删除的 key 列表", removed == ["1"], f"{removed}")
check("被删标签页的预约单已从状态机移除", 101 not in win._scheduled_orders,
      f"残留 {sorted(win._scheduled_orders)}")
check("另一标签页的预约单不受影响", 202 in win._scheduled_orders)
check("其 tab_key 已随重编号更新（'2' → '1'）",
      win._scheduled_orders[202]["tab_key"] == "1",
      f"实为 {win._scheduled_orders[202]['tab_key']!r}")
check("更新后的 tab_key 仍指向该订单自己的标签页（未串号）",
      win._scheduled_tab_still_owns(202, "1") is True)

# ══════════════════════════════════════════════════════════════════════════
print("\n=== 2. 缺陷真实性的直接证据：残留旧 key 会撞到另一个订单的标签页 ===")
# 手工把「被删标签页」的预约单放回去（等价于改前代码 / 未来漏掉清理的新路径）
win._scheduled_orders[101] = make_st("1", ready={101: FakeTask(101, 101)})
check("残留 key '1' 现在指向的是订单 202 的标签页 ⇒ 按它取 jobs 就会打错单",
      win._scheduled_tab_still_owns(101, "1") is False)
check("（若按 key 直接取 jobs，取到的确实是 202 的文件）",
      [getattr(j, "order_id", 0) for j in win._tab_jobs("1")] == [202],
      f"{[getattr(j, 'order_id', 0) for j in win._tab_jobs('1')]}")

# ══════════════════════════════════════════════════════════════════════════
print("\n=== 3. 🔴-B2 兜底：消费前校验 tab_key，撞号时按 ready 重建标签页 ===")
rebuild_calls = []
win._add_cloud_tasks_to_new_tab = lambda tasks, switch_tab=True: (
    rebuild_calls.append([t.task_id for t in tasks]) or "9")
win._fire_scheduled_print(101)
check("不复用坏 key，而是重建标签页（tab_key 变为 '9'）",
      win._scheduled_orders[101]["tab_key"] == "9",
      f"实为 {win._scheduled_orders[101]['tab_key']!r}")
check("重建用的是该订单自己的 ready 文件（task_id=101）",
      rebuild_calls == [[101]], f"{rebuild_calls}")

# 对照：tab_key 正常时不应触发重建
rebuild_calls.clear()
win._config.tabs["5"] = gui.TabSettings()
win._config.tabs["5"].jobs = [gui.PrintJob(file_path="x.pdf", order_id=505, task_id=505)]
win._scheduled_orders[505] = make_st("5", ready={505: FakeTask(505, 505)})
win._fire_scheduled_print(505)
check("tab_key 正常时不重建（误判会白建标签页）", rebuild_calls == [], f"{rebuild_calls}")
check("tab_key 保持 '5'", win._scheduled_orders[505]["tab_key"] == "5")

# ══════════════════════════════════════════════════════════════════════════
print("\n=== 4. 🔴-B3 在线冻结 + 文件已齐 ⇒ 必须排本地自恢复定时器 ===")
win2 = new_win()
win2._config.tabs = {}
put_order_tab(win2, "1", 303)
win2._current_tab = "1"
win2._config.active_tab = "1"
win2._cloud_connected = True
win2._scheduled_orders = {303: make_st("1", ready={303: FakeTask(303, 303)}, frozen=True)}
win2._evaluate_scheduled_order(303)
st2 = win2._scheduled_orders[303]
check("在线冻结也排了自恢复定时器（改前：直接 return ⇒ print_timer 为 None ⇒ 永久冻死）",
      st2["print_timer"] is not None)
check("冻结标志暂不解除（仍优先等后端解冻）", st2["frozen"] is True)

# ══════════════════════════════════════════════════════════════════════════
print("\n=== 5. 自恢复到点后确实解冻并打印（文件已全部就绪）===")
_orig_heal = gui.SCHEDULED_SELF_HEAL_S
gui.SCHEDULED_SELF_HEAL_S = 0        # 免等 30 秒
try:
    win2._scheduled_orders = {303: make_st("1", ready={303: FakeTask(303, 303)},
                                           frozen=True)}
    started = []
    win2._hold_for_manual_duplex = lambda jobs, tab_key, reason: False
    win2._start_print_worker = lambda jobs, **kw: (
        started.append([getattr(j, "order_id", 0) for j in jobs]), True)[1]
    win2._evaluate_scheduled_order(303)
    timer = win2._scheduled_orders[303]["print_timer"]
    check("已排入定时器", timer is not None)
    if timer is not None:
        timer.timeout.emit()          # 手动触发（等价于到点）
    check("确实启动了打印", len(started) == 1, f"{started}")
    check("打印的是该订单自己的文件（order_id=303）", started and started[0] == [303],
          f"{started}")
    check("冻结标志已解除", win2._scheduled_orders[303]["frozen"] is False)
    check("已置 printed（不会重复触发）", win2._scheduled_orders[303]["printed"] is True)
finally:
    gui.SCHEDULED_SELF_HEAL_S = _orig_heal

# ══════════════════════════════════════════════════════════════════════════
print("\n=== 6. 每秒检查不得吞掉「冻结 + 文件已齐」 ===")
win3 = new_win()
win3._config.tabs = {}
put_order_tab(win3, "1", 404)
win3._current_tab = "1"
win3._config.active_tab = "1"
win3._scheduled_orders = {404: make_st("1", ready={404: FakeTask(404, 404)}, frozen=True)}
win3._check_scheduled_timeouts()
check("补排了自恢复定时器（改前：`if not pending: continue` ⇒ 每秒跳过、永不处理）",
      win3._scheduled_orders[404]["print_timer"] is not None)

# ══════════════════════════════════════════════════════════════════════════
print("\n=== 7. 删除入口必须走统一清理（三处闭包 + 清空全部）===")
src = open(os.path.join(LOCAL_TOOL, "gui.py"), encoding="utf-8").read()
check("源码中已无『逐个 del 标签页后仅 _renumber_tabs』的旧写法",
      "self._config.tabs = {\"1\": self._make_new_tab()}\n            self._current_tab = \"1\"" not in src)
check("_clear_all_tabs 存在且被「清空全部」调用", "self._clear_all_tabs(" in src)
check("_remove_tabs 存在", "def _remove_tabs(" in src)
for caller, marker in (("删除单个标签页", 'self._remove_tabs([key], reason="删除标签页")'),
                       ("删除空标签页", 'self._remove_tabs(removed, reason="删除空标签页")'),
                       ("删除已完成订单", 'self._remove_tabs(frozen_keys, reason="删除已完成订单")')):
    check(f"{caller} 路径已改为走 _remove_tabs", marker in src, marker)

# ══════════════════════════════════════════════════════════════════════════
print("\n=== 8. 🟡-D5(12) 云端撤单必须清掉「本机还会打它」的四处状态 ===")
# 缺陷：撤单只弹一句"建议删除对应标签页"，标签页任务/补打队列/纸叠排队都还留着 ⇒
# 手动双面挂起的单子等人工点「开始打印」、补打队列与纸叠打完自动放行，都会把**已取消订单**
# 打出来（白费纸、顾客那边是"已取消"，收不到钱；预约单那一路当时已单独修过）。
win4 = new_win()
win4._config.tabs = {}
put_order_tab(win4, "1", 301)                      # 被取消的订单独占一页（挂起等人工点开始打印）
put_order_tab(win4, "2", 302)                      # 无关订单，必须原样保留
_tab3 = put_order_tab(win4, "3", 303)              # 同一页里混着两单
_tab3.jobs.append(gui.PrintJob(file_path=os.path.join(_SANDBOX, "3011.pdf"),
                               order_id=301, task_id=3011, page_count=1))
_tab3.manual_duplex_pending = True                 # 该页被标 ⏸（挂起等人翻纸）
win4._current_tab = "1"
win4._config.active_tab = "1"
win4._auto_print_retry = [{"order_id": 301, "tab_key": "1", "task_ids": [301]},
                          {"order_id": 302, "tab_key": "2", "task_ids": [302]}]
win4._stack_wait = [{"jobs": [gui.PrintJob(file_path=os.path.join(_SANDBOX, "301.pdf"),
                                           order_id=301, task_id=301, page_count=1),
                              gui.PrintJob(file_path=os.path.join(_SANDBOX, "302.pdf"),
                                           order_id=302, task_id=302, page_count=1)],
                     "tab_key": "1", "reason": "出纸槽独占"}]
win4._scheduled_orders = {301: make_st("1"), 302: make_st("2")}


def _purge(win, order_id, task_ids):
    """调用待验方法；改前版本没有它 → 返回 None（让断言变红，而不是 AttributeError 崩在中间）。"""
    fn = getattr(win, "_purge_canceled_print_state", None)
    return fn(order_id, task_ids) if fn else None


purged = _purge(win4, 301, [301])
_left = sorted(j.order_id for tab in win4._config.tabs.values() for j in tab.jobs)
check("★ 被取消订单的任务全部消失（挂起单不会再被「开始打印」打出来）",
      301 not in _left, str(_left))
check("★ 其它订单的任务原样保留（按订单粒度剔除，不整页丢）",
      _left == [302, 303] and len(win4._config.tabs) == 2, f"{_left} / {sorted(win4._config.tabs)}")
check("★ 补打队列里不再有被取消订单（本方法或 _remove_tabs 清掉都算）",
      [it["order_id"] for it in win4._auto_print_retry] == [302],
      str([it["order_id"] for it in win4._auto_print_retry]))
check("★ 纸叠排队里剔除了被取消订单的文件",
      [j.order_id for req in win4._stack_wait for j in req["jobs"]] == [302],
      str([[j.order_id for j in req["jobs"]] for req in win4._stack_wait]))
check("预约单状态机里的该订单被清掉、别的预约单不受影响",
      301 not in win4._scheduled_orders and 302 in win4._scheduled_orders,
      str(sorted(win4._scheduled_orders)))
check("清理计数如实返回（jobs/tabs/wait 由本方法清；retry/scheduled 已由 _remove_tabs 顺带清）",
      purged is not None and purged["jobs"] == 2 and purged["tabs"] == 1
      and purged["wait"] == 1, str(purged))
check("混排页里仍有未打印任务（303）⇒ ⏸ 保留（剩下那单仍要人工点开始打印）",
      any(getattr(t, "manual_duplex_pending", False) for t in win4._config.tabs.values()))

# 8b：被取消订单**没有**独占标签页时（_remove_tabs 不介入），本方法必须自己清队列与预约
win4b = new_win()
win4b._config.tabs = {}
put_order_tab(win4b, "1", 501)
win4b._config.tabs["1"].jobs.append(
    gui.PrintJob(file_path=os.path.join(_SANDBOX, "502.pdf"),
                 order_id=502, task_id=502, page_count=1))
win4b._current_tab = "1"
win4b._config.active_tab = "1"
win4b._auto_print_retry = [{"order_id": 501, "tab_key": "1", "task_ids": [501]},
                           {"order_id": 502, "tab_key": "1", "task_ids": [502]}]
win4b._scheduled_orders = {501: make_st("1"), 502: make_st("1")}
_p4b = _purge(win4b, 501, [501])
check("★ 标签页未被清空时，补打队列里只丢被取消订单那条",
      [it["order_id"] for it in win4b._auto_print_retry] == [502],
      str([it["order_id"] for it in win4b._auto_print_retry]))
check("★ 同一页里别的订单的补打队列项不受影响",
      win4b._auto_print_retry[0]["tab_key"] == "1")
check("★ 预约单状态机里只清被取消那单，计数为 1",
      _p4b is not None and _p4b["scheduled"] == 1 and 501 not in win4b._scheduled_orders
      and 502 in win4b._scheduled_orders, f"{_p4b} / {sorted(win4b._scheduled_orders)}")

# 8c：已打出去（sent）的任务不删 —— 删不删都不会再出纸，留着才是对账依据
win5 = new_win()
win5._config.tabs = {}
put_order_tab(win5, "1", 401, sent=True)
win5._current_tab = "1"
win5._config.active_tab = "1"
_p5 = _purge(win5, 401, [401])
check("已 sent 的任务保留（不删已打出的记录）",
      _p5 is not None and _p5["jobs"] == 0 and len(win5._config.tabs["1"].jobs) == 1, str(_p5))

# 接线判据（语义，不 grep 文案）：_on_cloud_order_canceled 必须真的调用清理
_gui_src = open(os.path.join(LOCAL_TOOL, "gui.py"), encoding="utf-8").read()
_body = _gui_src.split("def _on_cloud_order_canceled(self, order_id: int, task_ids: list):", 1)[1] \
    .split("\n    def _on_cloud_order_accepted", 1)[0]
_body_nc = "\n".join(ln[:ln.find("#")] if ln.find("#") >= 0 else ln for ln in _body.split("\n"))
check("★ _on_cloud_order_canceled 内真的调用了 _purge_canceled_print_state",
      "_purge_canceled_print_state(" in _body_nc)
check("撤单不再用「建议删除对应标签页」把责任推给用户",
      "建议删除对应标签页" not in _body_nc)
check("D 对拍：抽掉清理调用后该判据必红",
      "_purge_canceled_print_state(" not in _body_nc.replace(
          "self._purge_canceled_print_state(", "ZZ_NO_PURGE("))

# 收尾前打桩：closeEvent 在"标签页里仍有未打印文件"时会弹确认框 —— offscreen 平台没有
# 交互对象，`QMessageBox.question` 会**永久阻塞**（测试挂死）。统一回答 Yes。
from PySide6.QtWidgets import QMessageBox as _QMB  # noqa: E402

_QMB.question = staticmethod(lambda *a, **kw: _QMB.Yes)
_QMB.information = staticmethod(lambda *a, **kw: _QMB.Ok)
_QMB.warning = staticmethod(lambda *a, **kw: _QMB.Ok)

for w in (win, win2, win3, win4, win5):
    # closeEvent 还会调 _refresh_engine_availability → converter.get_available_engines()
    # 做 COM 引擎探测（本机抛 0x800706be），与本脚本无关，一并桩掉。
    w._refresh_engine_availability = lambda *a, **kw: None
    try:
        w.close()
    except Exception:
        pass

print("\n" + ("全部通过" if ok_all else "存在失败项"))
shutil.rmtree(_SANDBOX, ignore_errors=True)
sys.exit(0 if ok_all else 1)
