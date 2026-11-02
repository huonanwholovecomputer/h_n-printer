# -*- coding: utf-8 -*-
"""验收脚本 · 攒批「离线期望」上线校验（2026-11 用户需求 2 · 设备端）

场景：用户在外面把手机上的「🖐 攒批模式」点了开/关，但打印电脑当时离线。
后端把这条期望记下来（`stack_pending`），设备一上线补发一条
`set_stack_mode`，载荷里带 `"reason": "offline_intent"`。

设备端必须做到（逐条对应本脚本判据）：

  ① **先比对**：`self._config.duplex_stack_mode` 已经等于期望值 ⇒ 什么都不切，
     只回报 `stack_state`（带 `intent_applied: true` + 原 `request_id`），云端据此清期望。
  ② **不一致才切**：走既有切换路径 —— 也就是**保留既有"忙"闸门**
     （正在打印 / 纸叠没打完第 2 趟 / 有排队）和机型适用性闸门，
     拒绝时回报 `intent_applied: false` + note（云端保留期望下次再试）。
  ③ **上线对账**：连接建立后若 `printer_state` / register 回包里带 `stack_pending`
     （或 `stack_intent`），消费一次；同一条期望不许被心跳反复执行。
  ④ 日志前缀 `[stack_intent]`，现场一眼能看懂发生了什么。

不联网、不碰真打印机（offscreen + 沙箱 APPDATA）。

    python tests/verify_stack_intent_local.py     # exit 0 = 全绿
"""
import io
import json
import os
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL_TOOL = os.path.join(REPO, "local_print_tool")

_SANDBOX = tempfile.mkdtemp(prefix="hn_stack_intent_")
os.environ["APPDATA"] = _SANDBOX                  # 用户数据目录 → 沙箱（不碰真实配置）
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, LOCAL_TOOL)
os.chdir(LOCAL_TOOL)

from PySide6.QtWidgets import QApplication  # noqa: E402

_app = QApplication.instance() or QApplication([])

import cloud_client  # noqa: E402
import gui  # noqa: E402
import paths  # noqa: E402
from printer_config import PrinterProfile  # noqa: E402

ok_all = True


def check(name, cond, detail=""):
    global ok_all
    if not cond:
        ok_all = False
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


MANUAL = PrinterProfile(auto_duplex=False, capability_source="manual", manual_duplex=True,
                        first="odd", reverse_second=True, second_rotate_180=True,
                        stack_capacity=90, stack_warn_after_sec=1800)


class ReportStub:
    """只记录 report_stack_state 调用（新签名 5 参）。"""

    def __init__(self):
        self.calls = []
        self.last_printer_state = None

    def is_connected(self):
        return True

    def report_stack_state(self, mode, busy, note="", request_id="", intent_applied=None):
        self.calls.append({"mode": bool(mode), "busy": bool(busy), "note": note,
                           "request_id": request_id, "intent_applied": intent_applied})
        return True


class OldSignatureStub:
    """旧客户端/老 stub：只有 3 参签名 → GUI 必须能回退，不能抛。"""

    def __init__(self):
        self.calls = []

    def report_stack_state(self, mode, busy, note=""):
        self.calls.append((bool(mode), bool(busy), note))
        return True


class FakeWorker:
    def __init__(self, running=True):
        self._running = running

    def isRunning(self):
        return self._running


print("=" * 60)
print("① parse_stack_command：认得出 offline_intent，普通点击不受影响")
c1 = cloud_client.parse_stack_command({"enable": True, "by": "张三",
                                       "reason": "offline_intent", "request_id": "abc123"})
check("offline_intent → intent=True 且字段齐全",
      c1["intent"] is True and c1["enable"] is True and c1["by"] == "张三"
      and c1["request_id"] == "abc123", c1)
c2 = cloud_client.parse_stack_command({"enable": False, "by": "李四", "request_id": "r2"})
check("普通点击（无 reason）→ intent=False", c2["intent"] is False and c2["enable"] is False, c2)
c3 = cloud_client.parse_stack_command({"enable": True, "reason": "offline_intent"})
check("缺 by / request_id 也不炸", c3["intent"] is True and c3["by"] == "" and c3["request_id"] == "")
c4 = cloud_client.parse_stack_command(None)
check("None/坏载荷安全降级", c4["intent"] is False and c4["enable"] is False, c4)

print()
print("② report_stack_state：常规心跳不带新字段；答复期望才带 request_id/intent_applied")
sent = []


class _EmitProbe:
    """只挂 report_stack_state 一个方法（复用真实现，不联网）。"""

    report_stack_state = cloud_client.CloudClient.report_stack_state

    def _sio_emit(self, ev, data):
        sent.append((ev, data))
        return True


def _fake_client():
    return _EmitProbe()


cc = _fake_client()
sent.clear()
cc.report_stack_state(True, False, "常规")
ev, data = sent[-1]
check("常规心跳：只有 mode/busy/note", ev == "stack_state" and set(data) == {"mode", "busy", "note"}, data)
check("常规心跳：不带 intent_applied（老后端无感）", "intent_applied" not in data)

sent.clear()
cc.report_stack_state(False, True, "打印机正忙（正在打印），未执行",
                      request_id="r-9", intent_applied=False)
ev, data = sent[-1]
check("答复期望：回带 request_id + intent_applied=False",
      data.get("request_id") == "r-9" and data.get("intent_applied") is False, data)
check("备注原样带回（移动端要看到原因）", "打印机正忙" in data.get("note", ""), data.get("note"))

sent.clear()
cc.report_stack_state(True, False, "x" * 300, request_id="y" * 200, intent_applied=True)
ev, data = sent[-1]
check("note 截 120 / request_id 截 64（别把事件撑爆）",
      len(data["note"]) == 120 and len(data["request_id"]) == 64,
      f"note={len(data['note'])}, rid={len(data['request_id'])}")

print()
print("③ 静态接线：offline_intent 走独立信号，GUI 两个槽都连着")
cc_src = open(os.path.join(LOCAL_TOOL, "cloud_client.py"), encoding="utf-8").read()
gui_src = open(os.path.join(LOCAL_TOOL, "gui.py"), encoding="utf-8").read()
check("cloud_client：有 stack_intent_requested 信号",
      "stack_intent_requested = Signal(bool, str, str)" in cc_src)
check("cloud_client：旧 stack_mode_requested 签名原样保留（既有判据不许被改）",
      "stack_mode_requested = Signal(bool, str)" in cc_src)
check("cloud_client：handler 用 parse_stack_command 分流",
      "cmd = parse_stack_command(data)" in cc_src
      and 'self.stack_intent_requested.emit(enable, by, cmd["request_id"])' in cc_src)
check("gui：新信号连到独立槽（绑定方法，才能在主线程执行）",
      "self._cloud_client.stack_intent_requested.connect(self._on_remote_stack_intent)" in gui_src)
check("gui：连接建立后做一次上线对账", "self._reconcile_stack_intent()" in gui_src)
check("gui：接单状态推送里也消费 stack_pending", "self._reconcile_stack_intent(state)" in gui_src)
check("既有用法适用性闸门仍恰好 2 处（没被我加出第三份）",
      gui_src.count("if want and not self._stack_applicable():") == 2,
      gui_src.count("if want and not self._stack_applicable():"))

print()
print("④ GUI：期望值与本地一致 → 只回报，不切换")
win = gui.MainWindow(config_path=paths.config_path())
logs = []
win._log = lambda msg, *a, **k: logs.append(str(msg))


def _prep():
    """每段用例前把「打印机名 + 档案」钉回去：`_save_config()` 会 `_sync_ui_to_config()`，
    把 printer_name 从真实下拉框读回来（测试环境里是系统默认打印机）⇒ 适用性判定会跑偏。"""
    win._config.printer_name = "Fake Print"
    win._config.printer_profiles["Fake Print"] = MANUAL
    win._config.printer_profiles["Auto Duplex"] = PrinterProfile(
        auto_duplex=True, capability_source="manual", manual_duplex=False)
    win._worker = None
    win._stack_flush_worker = None
    try:
        win._stack.entries.clear()
    except Exception:      # noqa: BLE001
        pass


_prep()
stub = ReportStub()
win._cloud_client = stub

win._config.duplex_stack_mode = True            # 本地已经是"开"
win._last_stack_sig = None
logs.clear()
stub.calls.clear()
win._on_remote_stack_intent(True, "张三", "req-A")
last = stub.calls[-1] if stub.calls else {}
check("一致：没有多余的切换回报，只发一条",
      len(stub.calls) == 1, stub.calls)
check("一致：intent_applied=True + request_id 原样带回",
      last.get("intent_applied") is True and last.get("request_id") == "req-A", last)
check("一致：note 说清「已与云端一致」", "已与云端一致" in (last.get("note") or ""), last.get("note"))
check("一致：日志带 [stack_intent] 前缀且说明无需切换",
      any("[stack_intent]" in m and "本地配置一致" in m for m in logs), logs)

# 为什么"比对"必须排在忙闸门**之前**：期望值与本地一致时，设备再忙也没有动作要做。
# 若把它挪到忙闸门之后，这里会错误回报 intent_applied=False，云端白等一轮（甚至反复重发）。
_prep()
win._config.duplex_stack_mode = True
win._worker = FakeWorker(True)                  # 正在打印
win._last_stack_sig = None
logs.clear()
stub.calls.clear()
win._on_remote_stack_intent(True, "张三", "req-A2")
last = stub.calls[-1] if stub.calls else {}
check("★ 一致且设备忙：仍报 intent_applied=True（比对优先于忙闸门）",
      last.get("intent_applied") is True, last)
check("★ 一致且设备忙：note 是「已与云端一致」而不是「打印机正忙」",
      "已与云端一致" in (last.get("note") or "")
      and "打印机正忙" not in (last.get("note") or ""), last.get("note"))
check("★ 一致且设备忙：日志不出现「设备忙」", not any("设备忙" in m for m in logs), logs)
win._worker = None

print()
print("⑤ GUI：期望值与本地不一致 → 走既有切换路径（含忙闸门）")
_prep()
win._config.duplex_stack_mode = False
win._last_stack_sig = None
logs.clear()
stub.calls.clear()
win._on_remote_stack_intent(True, "张三", "req-B")
last = stub.calls[-1] if stub.calls else {}
check("不一致且空闲：真的落盘开启了", win._config.duplex_stack_mode is True)
check("不一致且空闲：回报 intent_applied=True", last.get("intent_applied") is True, last)
check("不一致且空闲：日志说明已切换",
      any("[stack_intent]" in m and "开启" in m for m in logs), logs)

# 忙闸门：正在打印 → 必须拒绝，且配置不变
_prep()
win._config.duplex_stack_mode = False
win._worker = FakeWorker(True)
logs.clear()
stub.calls.clear()
win._on_remote_stack_intent(True, "张三", "req-C")
last = stub.calls[-1] if stub.calls else {}
check("★ 忙（正在打印）：拒绝，配置保持关闭",
      win._config.duplex_stack_mode is False, win._config.duplex_stack_mode)
check("★ 忙：回报 intent_applied=False（云端保留期望下次再试）",
      last.get("intent_applied") is False, last)
check("★ 忙：note 说明打印机正忙", "打印机正忙" in (last.get("note") or ""), last.get("note"))
check("★ 忙：日志说清「暂不切换，云端会保留期望」",
      any("[stack_intent]" in m and "设备忙" in m and "下次再试" in m for m in logs), logs)
win._worker = None

# 机型不适用（换成自动双面机型）→ 开启必须被拦（与本地开关同一道闸门）
_prep()
win._config.printer_name = "Auto Duplex"
win._config.duplex_stack_mode = False
logs.clear()
stub.calls.clear()
win._on_remote_stack_intent(True, "李四", "req-D")
last = stub.calls[-1] if stub.calls else {}
check("★ 机型不适用（自动双面）：拒绝开启",
      win._config.duplex_stack_mode is False and last.get("intent_applied") is False, last)
check("★ 机型不适用：note 带「未执行」原因", "未执行" in (last.get("note") or ""), last.get("note"))
win._config.printer_name = "Fake Print"

print()
print("⑥ 上线对账：printer_state 带 stack_pending → 消费一次，心跳不重复执行")
check("布尔形态 stack_pending=True → (True, '', '')",
      win._stack_pending_from_state({"stack_pending": True}) == (True, "", ""))
check("布尔形态 stack_pending=False → 没有待落地期望",
      win._stack_pending_from_state({"stack_pending": False}) is None)
check("字典形态 {enable,by,request_id} 解得出",
      win._stack_pending_from_state(
          {"stack_pending": {"enable": False, "by": "王五", "request_id": "q1"}})
      == (False, "王五", "q1"))
check("兼容 stack_intent 字段名 + mode 键",
      win._stack_pending_from_state({"stack_intent": {"mode": True}}) == (True, "", ""))
check("没有该字段 → None", win._stack_pending_from_state({"owner_name": "x"}) is None)
check("非 dict 安全 → None", win._stack_pending_from_state(None) is None)

_prep()
win._config.duplex_stack_mode = False
win._stack_intent_sig = None
logs.clear()
stub.calls.clear()
win._reconcile_stack_intent({"stack_pending": True, "owner_name": "张三"})
last = stub.calls[-1] if stub.calls else {}
check("对账：本地关 → 期望开 ⇒ 真的开了", win._config.duplex_stack_mode is True)
check("对账：回报 intent_applied=True", last.get("intent_applied") is True, last)
check("对账：日志写清「上线对账 … 需要切换」",
      any("[stack_intent]" in m and "上线对账" in m and "需要切换" in m for m in logs), logs)

n_calls, n_logs = len(stub.calls), len(logs)
win._reconcile_stack_intent({"stack_pending": True, "owner_name": "张三"})
win._reconcile_stack_intent({"stack_pending": True, "owner_name": "张三"})
check("★ 同一条期望：心跳推两遍也只处理一次（不重复刷日志/回报）",
      len(stub.calls) == n_calls and len(logs) == n_logs,
      f"calls={len(stub.calls)}/{n_calls}, logs={len(logs)}/{n_logs}")

# 断开连接 → 允许重连后再试一次
win._on_cloud_connection_changed(False)
check("断开：清掉「已处理」记号（重连可再试）",
      getattr(win, "_stack_intent_sig", None) is None, win._stack_intent_sig)

# 缓存兜底：连接建立时没带 state → 用缓存的接单状态
_prep()
win._config.duplex_stack_mode = False
win._cloud_printer_state_cache = {"stack_pending": {"enable": True, "by": "赵六",
                                                   "request_id": "q2"}}
logs.clear()
stub.calls.clear()
win._reconcile_stack_intent()
last = stub.calls[-1] if stub.calls else {}
check("对账：无参调用读缓存（连接建立时的路径）",
      win._config.duplex_stack_mode is True and last.get("intent_applied") is True, last)

print()
print("⑦ 兼容性：只有一个 3 参 report_stack_state 的旧客户端不能把 GUI 抛穿")
win._cloud_client = OldSignatureStub()
try:
    win._report_stack_state("旧客户端", request_id="r1", intent_applied=True)
    old_ok = True
except Exception as e:      # noqa: BLE001
    old_ok = False
    print(f"   异常: {e}")
check("旧签名回退：不抛异常且确实收到 3 参调用",
      old_ok and len(win._cloud_client.calls) == 1, win._cloud_client.calls)

print()
print("=" * 60)
print("攒批离线期望（设备端）验收：" + ("全部通过 ✅" if ok_all else "存在失败 ❌"))
sys.exit(0 if ok_all else 1)
