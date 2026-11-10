# -*- coding: utf-8 -*-
"""预约单 start_print 落空时的「讲原因」验收（客户端侧）

现场（2026-11-10，HN20261110-0005 / 订单 id 1252）：35 分钟里每 30 秒一条
「☁ 预约单 #1252 收到开始打印指令」，日志里没有任何结果 —— 现场判断成「开始打印指令失效」，
实际是**指令到了、本机没状态机可驱动**：预约到点遇到手动双面机型时本机是故意挂起的
（`_hold_for_manual_duplex()` → 不回报失败 → `_cleanup_scheduled_order(...)` 结束状态机），
之后后端兜底重发的每一条都落进 `if not st: return` 静默丢弃。

本脚本钉住客户端这半边的修复：
  · 落空必须讲清原因（分四种：已无任务 / 已打完 / 已转人工打印 / 不在预约队列）；
  · 同一单 10 分钟内只讲一次（后端仍在重发，别造新刷屏）；
  · **行为不许变**：收到兜底指令绝不自动开打（双面单没人翻纸，自动开打就是打废纸）。

用法: python other/tests/verify_scheduled_start_print_ignored.py   （exit 0 = 全部通过）
"""
import io
import os
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LOCAL_TOOL = os.path.join(REPO, "local_print_tool")

_SANDBOX = tempfile.mkdtemp(prefix="hn_start_print_ign_")
os.environ["APPDATA"] = _SANDBOX
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, LOCAL_TOOL)
os.chdir(LOCAL_TOOL)

from PySide6.QtWidgets import QApplication  # noqa: E402

_app = QApplication.instance() or QApplication([])

import gui      # noqa: E402
import paths    # noqa: E402

ok_all = True


def check(name, cond, detail=""):
    global ok_all
    if not cond:
        ok_all = False
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


class Sink:
    def __init__(self):
        self.lines = []
        self.started = 0

    def log(self, msg):
        self.lines.append(str(msg))

    def text(self):
        return "\n".join(self.lines)


def new_win():
    w = gui.MainWindow(config_path=paths.config_path())
    w._cloud_client = None
    w._cloud_connected = True
    sink = Sink()
    w._log = sink.log
    w._hold_for_manual_duplex = lambda jobs, tab_key, reason: False

    def _start(*a, **kw):
        sink.started += 1
        return True

    w._start_print_worker = _start
    w._scheduled_orders = {}
    w._start_print_ignored_ts = {}
    return w, sink


def put_order_tab(win, key, order_id, sent=False, manual_pending=False):
    tab = gui.TabSettings()
    tab.jobs = [gui.PrintJob(file_path=os.path.join(_SANDBOX, f"{order_id}.pdf"),
                             order_id=order_id, task_id=order_id,
                             page_count=2, sent=sent)]
    tab.manual_duplex_pending = manual_pending
    win._config.tabs[key] = tab
    return tab


# ══════════════════════════════════════════════════════════════════════════
print("=== 1. 四种「为什么没打」都要讲清楚 ===")
win, sink = new_win()
win._config.tabs = {}
win._scheduled_orders = {}          # 状态机已结束（现场情形）
win._on_cloud_start_print(901, 0, [901])
check("无任务：说明本机已无这单的任务", "已无这单的任务" in sink.text(), sink.text())
check("措辞点出可能已人工打印/取消/清理",
      ("人工打印" in sink.text() or "已取消" in sink.text() or "已清理" in sink.text()))

win, sink = new_win()
win._config.tabs = {}
win._scheduled_orders = {}
put_order_tab(win, "1", 902, sent=True)
win._on_cloud_start_print(902, 0, [902])
check("已打完：说明本机这单已经打完并回报成功", "已经打完并回报成功" in sink.text(), sink.text())

win, sink = new_win()
win._config.tabs = {}
win._scheduled_orders = {}
put_order_tab(win, "1", 903, sent=False, manual_pending=True)
win._on_cloud_start_print(903, 0, [903])
_txt = sink.text()
check("已转人工（手动双面机型）：说明含双面、不支持自动双面", "不支持自动双面" in _txt, _txt)
check("已转人工：说明本机挂起等人工点「开始打印」并翻纸",
      "开始打印" in _txt and "翻纸" in _txt, _txt)

win, sink = new_win()
win._config.tabs = {}
win._scheduled_orders = {}
put_order_tab(win, "1", 904, sent=False, manual_pending=False)
win._on_cloud_start_print(904, 0, [904])
check("其余：说明不在预约队列、需要人工点「开始打印」",
      "不在预约队列里" in sink.text() and "需要人工点" in sink.text(), sink.text())

print("\n=== 2. 同一单 10 分钟只讲一次（后端还在每 30 秒重发）===")
win, sink = new_win()
win._config.tabs = {}
win._scheduled_orders = {}
for _ in range(6):
    win._on_cloud_start_print(905, 0, [905])
check("连来 6 次兜底指令，只讲 1 次", len(sink.lines) == 1, f"{len(sink.lines)} 行")
check("节流按 order_id 记账", 905 in win._start_print_ignored_ts,
      f"{sorted(win._start_print_ignored_ts)}")
# 把时间戳往前拨 11 分钟 → 允许再讲一次
win._start_print_ignored_ts[905] -= 660
win._on_cloud_start_print(905, 0, [905])
check("过了 10 分钟可以再讲一次（长期挂着不被永久噤声）", len(sink.lines) == 2,
      f"{len(sink.lines)} 行")

print("\n=== 3. 铁律：绝不因为收到兜底指令就自动开打 ===")
win, sink = new_win()
win._config.tabs = {}
win._scheduled_orders = {}
put_order_tab(win, "1", 906, sent=False, manual_pending=True)
win._on_cloud_start_print(906, 0, [906])
check("没有启动任何打印", sink.started == 0, f"启动 {sink.started} 次")
check("状态机没被凭空创建", 906 not in win._scheduled_orders,
      f"{sorted(win._scheduled_orders)}")

print("\n=== 4. 零回归：状态机仍在时照旧驱动（幂等）===")
win, sink = new_win()
win._config.tabs = {}
win._config.tabs["1"] = gui.TabSettings()
win._config.tabs["1"].jobs = []
win._scheduled_orders = {}
win._start_print_ignored_ts = {}
_called = {"eval": 0}
st = {"target_ts": 0, "ready": {}, "pending": {}, "timer": None, "print_timer": None,
      "frozen": True, "delayed_sent": False, "printed": False, "printed_ts": 0,
      "tab_key": "1", "retry_count": 0}
win._scheduled_orders[907] = st
win._save_scheduled_state = lambda: None
win._restart_scheduled_debounce = lambda oid: _called.__setitem__("eval", _called["eval"] + 1)
win._evaluate_scheduled_order = lambda oid: _called.__setitem__("eval", _called["eval"] + 1)
win._on_cloud_start_print(907, 1794290000, [907])
check("状态机存在时仍走正常流程（不写「落空」日志）", sink.text() == "", sink.text())
check("冻结被解除、目标时间被后端覆盖",
      st["frozen"] is False and st["target_ts"] == 1794290000,
      f"frozen={st['frozen']} ts={st['target_ts']}")
check("仍会驱动后续评估", _called["eval"] >= 1, f"{_called['eval']}")
check("落空节流没被误写", 907 not in win._start_print_ignored_ts,
      f"{sorted(win._start_print_ignored_ts)}")

print("\n=== 5. 无 order_id 的脏指令直接忽略（不崩、不记）===")
win, sink = new_win()
win._on_cloud_start_print(0, 0, [])
check("order_id=0 不产生日志也不崩", sink.lines == [], f"{sink.lines}")

print()
if ok_all:
    print("全部通过 ✅")
    sys.exit(0)
print("存在失败 ❌")
sys.exit(1)
