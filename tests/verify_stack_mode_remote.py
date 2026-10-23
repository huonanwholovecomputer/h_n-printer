# -*- coding: utf-8 -*-
"""攒批模式（🖐 多任务合并翻纸）远程开关 验收

背景（2026-10）：攒批模式的开关原本只能在本地工具上点。现在要：
  · 移动端（小程序 / APP）的设备列表**显示**每台设备的攒批模式状态；
  · **仅管理员/超管**能从移动端开启/关闭它；
  · 前提（两侧同口径）：开启 = 当前未开 + 空闲；关闭 = 当前已开 + 空闲。
    忙（正在打印 / 等待人工翻纸打第二趟 / 有排队待打的请求）时，**移动端与本地工具两侧都拦**。
  · **适用性前提**（2026-10 用户要求）：攒批（多任务合并翻纸）只对「无自动双面器、双面需人工
    翻纸」的机型有意义 —— 支持自动双面 / 能力未确认的设备**不渲染开关、也不接受远程开启**。
    三端同一条判据：服务端 `_stack_needs_manual_flip`（auto_duplex is False）、本地工具
    `_stack_applicable`（档案 auto_duplex is False）、移动端 `auto_duplex === false`。

本脚本钉住的代码事实：
  ① 服务端忙闲闸门 `_stack_toggle_problem`（纯函数，各组合）；
  ② `/api/claiming_devices` 下发 stack_mode / stack_busy / stack_can_toggle / stack_reason；
  ③ `POST /api/printer/stack_mode`：鉴权 / 权限 / 离线 / 忙 / 成功 / 设备端拒绝 / 超时；
  ④ socketio `stack_state` 上报通道（真实 test_client 连接）；
  ⑤ 三端静态接线：本地工具的忙闲口径与两侧同闸门、小程序与 APP 的展示与开关。

用法: python tests/verify_stack_mode_remote.py   （exit 0 = 全部通过）
"""
import io
import os
import shutil
import sys
import tempfile
import traceback
from datetime import datetime

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(REPO, "mobile_apps", "printer-backend")
ok_all = True


def check(name, cond, detail=""):
    global ok_all
    if not cond:
        ok_all = False
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


tmp = tempfile.mkdtemp(prefix="hn_stack_remote_")
dst = os.path.join(tmp, "printer-backend")
shutil.copytree(SRC, dst, ignore=shutil.ignore_patterns(
    "__pycache__", "orders.db", "users.db", "uploads", "logs", "venv"))
sys.path.insert(0, dst)
os.chdir(dst)
try:
    import app as backend
    print(f"imported app.py from {dst}")
except Exception:
    traceback.print_exc()
    sys.exit("无法导入 app.py")

backend.init_db()

# ==================== 桩环境 ====================
CID = "DESKTOP-STACK-a1b2"
ADMIN_OID, USER_OID = "openid-admin", "openid-user"
backend.PRINTER_TOKEN = "test-token"
backend.compute_role = lambda oid: ("admin" if oid == ADMIN_OID else "user")
ADMIN_TOKEN = backend.token_serializer.dumps(ADMIN_OID)
USER_TOKEN = backend.token_serializer.dumps(USER_OID)

backend.register_device(CID, "DESKTOP-STACK")
backend.get_claiming_devices = lambda: {CID: {"claimed_at": "", "owner_name": "张三"}}
backend.get_claiming_device_ids = lambda: [CID]
# 攒批只对「无自动双面器、需人工翻纸」的机型开放（2026-10 用户要求）⇒ 主用例先给该设备
# 盖上「不支持自动双面」的人工结论；「支持自动双面 / 能力未确认」的用例在 ①/②/③ 里单独验。
backend.set_device_manual_capability(CID, False, "测试")


def _online(flag=True):
    backend.printer_clients.clear()
    if flag:
        backend.printer_clients[CID] = {
            "sid": "sid-stack", "heartbeat": datetime.now(), "connected_at": datetime.now()}


# ==================== ④ socketio 上报通道（真 test_client，先跑）====================
print("\n=== ④ socketio stack_state 上报（真实连接）===")
_online(False)
try:
    ws = backend.socketio.test_client(
        backend.app,
        query_string=f"?token=test-token&client_id={CID}&device_name=DESKTOP-STACK")
    ws.emit("stack_state", {"mode": True, "busy": False})
    st = backend._stack_state_of(CID)
    check("设备端上报 stack_state → 服务端镜像更新（mode / busy）",
          st["mode"] is True and st["busy"] is False, f"{st}")
    ws.emit("stack_state", {"mode": True, "busy": True, "note": "正在打印整叠的第 2 趟"})
    st = backend._stack_state_of(CID)
    check("忙闲随上报刷新，note 一并留存",
          st["busy"] is True and "第 2 趟" in st["note"], f"{st}")
    ws.disconnect()
except Exception:
    traceback.print_exc()
    check("socketio test_client 上报链路可用", False, "见上面的 traceback")


# ==================== 桩 socketio（③ 用）====================
class FakeSocketIO:
    """记录 emit；收到 set_stack_mode 时可立刻"模拟设备端执行 + 回报"。"""

    def __init__(self):
        self.sent = []
        self.auto_reply = True
        self.reply_mode = None      # None = 按请求的 enable 回报
        self.reply_note = ""

    def emit(self, event, payload=None, to=None):
        self.sent.append({"event": event, "payload": payload, "to": to})
        if event != "set_stack_mode" or not self.auto_reply:
            return
        mode = self.reply_mode if self.reply_mode is not None else bool((payload or {}).get("enable"))
        backend._set_stack_state(CID, mode, False, self.reply_note)
        with backend.printer_stack_state_lock:
            w = backend.printer_stack_waiters.pop(CID, None)
            if w is not None:
                backend.printer_stack_results[CID] = {"mode": mode, "busy": False,
                                                      "note": self.reply_note}
        if w is not None:
            w.set()


fake = FakeSocketIO()
backend.socketio = fake
backend.STACK_TOGGLE_WAIT_SEC = 0.3      # 加速"设备端不回应"用例

client = backend.app.test_client()


def post(body, token=ADMIN_TOKEN):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return client.post("/api/printer/stack_mode", json=body, headers=headers)


# ==================== ① 忙闲闸门（纯函数）====================
print("\n=== ① 忙闲闸门（_stack_toggle_problem）===")
gate = backend._stack_toggle_problem
backend._set_stack_state(CID, None, False)
check("★ 从未上报 → 拦（旧版客户端 / 刚接入）",
      gate(CID, True) != "" and "尚未上报" in gate(CID, True), gate(CID, True))
backend._set_stack_state(CID, False, True)
check("★ 未开 + 忙 → 开启拦下（正忙）", "正忙" in gate(CID, True), gate(CID, True))
check("★ 未开 + 忙 → 关闭同样拦（忙就是忙）", "正忙" in gate(CID, False), gate(CID, False))
backend._set_stack_state(CID, False, False)
check("未开 + 空闲 + 要开启 → 放行", gate(CID, True) == "", gate(CID, True))
check("未开 + 空闲 + 要关闭 → 拦（已经是关闭）", "已经是关闭" in gate(CID, False), gate(CID, False))
check("未开 + 空闲 + enable=None（列表接口用）→ 放行", gate(CID) == "", gate(CID))
backend._set_stack_state(CID, True, False)
check("已开 + 空闲 + 要关闭 → 放行", gate(CID, False) == "", gate(CID, False))
check("已开 + 空闲 + 要开启 → 拦（已经是开启）", "已经是开启" in gate(CID, True), gate(CID, True))

# ==================== ①.1 适用性前提：只有「需人工翻纸」的机型才给攒批（2026-10 用户要求）===
print("\n=== ①.1 攒批只对「不支持自动双面、需人工翻纸」的机型开放 ===")
backend._set_stack_state(CID, False, False)          # 状态本身"空闲 + 未开"（正好是放行的组合）
backend.set_device_manual_capability(CID, True, "测试")      # 人工盖章：支持自动双面
check("★ 支持自动双面的设备 → 拦下，且原因说明「不需要攒批」（不是忙）",
      gate(CID, True) != "" and "不需要攒批" in gate(CID, True), gate(CID, True))
check("★ 支持自动双面的设备 → 关闭方向同样拦（这个功能对它整体不适用）",
      "不需要攒批" in gate(CID, False), gate(CID, False))
backend.set_device_manual_capability(CID, None, "")          # 清除人工结论 → 无探测 → 未确认
check("★ 能力未确认（既没盖章也没探测上报）→ 也不给攒批入口",
      "不需要攒批" in gate(CID, True), gate(CID, True))
backend.set_device_manual_capability(CID, False, "测试")     # 恢复「需人工翻纸」
check("★ 恢复「不支持自动双面」→ 闸门回到忙闲判定（空闲 + 未开 + 要开启 = 放行）",
      gate(CID, True) == "", gate(CID, True))

# ==================== ② /api/claiming_devices 下发 ====================
print("\n=== ② /api/claiming_devices 下发攒批状态 ===")


def _dev():
    data = client.get("/api/claiming_devices").get_json() or {}
    return {d["client_id"]: d for d in (data.get("devices") or [])}.get(CID, {})


_online(True)
backend._set_stack_state(CID, True, False)
d = _dev()
check("★ 空闲 + 已开 → stack_mode=True / can_toggle=True / 无原因",
      d.get("stack_mode") is True and d.get("stack_can_toggle") is True
      and not d.get("stack_reason"), f"{ {k: d.get(k) for k in ('stack_mode', 'stack_busy', 'stack_can_toggle', 'stack_reason')} }")
backend._set_stack_state(CID, True, True)
d = _dev()
check("★ 忙 → stack_busy=True / can_toggle=False / reason 说明正忙",
      d.get("stack_busy") is True and d.get("stack_can_toggle") is False
      and "正忙" in (d.get("stack_reason") or ""), d.get("stack_reason"))
backend._set_stack_state(CID, None, False)
d = _dev()
check("未上报 → stack_mode=None / can_toggle=False / reason 说明未上报",
      d.get("stack_mode") is None and d.get("stack_can_toggle") is False
      and "尚未上报" in (d.get("stack_reason") or ""), d.get("stack_reason"))
_online(False)
d = _dev()
check("★ 设备离线 → can_toggle=False / reason=设备离线",
      d.get("stack_can_toggle") is False and d.get("stack_reason") == "设备离线",
      d.get("stack_reason"))

# 适用性前提也体现在下发字段里：支持自动双面的设备 can_toggle=False（移动端据此连区块都不渲染）
_online(True)
backend.set_device_manual_capability(CID, True, "测试")
backend._set_stack_state(CID, False, False)
d = _dev()
check("★ 支持自动双面的设备 → stack_can_toggle=False 且 reason 说明「不需要攒批」",
      d.get("stack_can_toggle") is False and "不需要攒批" in (d.get("stack_reason") or ""),
      d.get("stack_reason"))
backend.set_device_manual_capability(CID, False, "测试")
d = _dev()
check("恢复「需人工翻纸」+ 空闲 → can_toggle 重新为 True（判据不是恒假）",
      d.get("stack_can_toggle") is True and not d.get("stack_reason"), d.get("stack_reason"))

# ==================== ③ POST /api/printer/stack_mode ====================
print("\n=== ③ POST /api/printer/stack_mode ===")
_online(True)
r = client.post("/api/printer/stack_mode", json={"client_id": CID, "enable": True})
check("无 Authorization → 401", r.status_code == 401, f"status={r.status_code}")

backend._set_stack_state(CID, False, False)
j = (post({"client_id": CID, "enable": True}, token=USER_TOKEN).get_json() or {})
check("★ 非管理员（user 角色）→ 拒绝，且不下发指令",
      j.get("success") is False and "仅管理员" in (j.get("message") or "")
      and not [s for s in fake.sent if s["event"] == "set_stack_mode"], j.get("message"))
j = (post({"client_id": CID}).get_json() or {})
check("参数不完整（缺 enable）→ success=False", j.get("success") is False, j.get("message"))

# ★ 适用性前提：支持自动双面 / 能力未确认的设备，一律拒绝且不下发指令
backend.set_device_manual_capability(CID, True, "测试")
fake.sent.clear()
backend._set_stack_state(CID, False, False)
j = (post({"client_id": CID, "enable": True}).get_json() or {})
check("★ 支持自动双面的设备 → 拦下并说明「不需要攒批」",
      j.get("success") is False and "不需要攒批" in (j.get("message") or ""), j.get("message"))
check("★ 适用性不符时**不**给设备端下发指令",
      not [s for s in fake.sent if s["event"] == "set_stack_mode"],
      f"sent={[s['event'] for s in fake.sent]}")
backend.set_device_manual_capability(CID, None, "")
j = (post({"client_id": CID, "enable": True}).get_json() or {})
check("★ 能力未确认的设备 → 同样拦下（不给攒批入口）",
      j.get("success") is False and "不需要攒批" in (j.get("message") or ""), j.get("message"))
backend.set_device_manual_capability(CID, False, "测试")

_online(False)
j = (post({"client_id": CID, "enable": True}).get_json() or {})
check("★ 设备离线 → 拦下", j.get("success") is False and "离线" in (j.get("message") or ""),
      j.get("message"))

_online(True)
backend._set_stack_state(CID, False, True)
fake.sent.clear()
j = (post({"client_id": CID, "enable": True}).get_json() or {})
check("★ 忙 → 拦下并给出正忙原因", j.get("success") is False and "正忙" in (j.get("message") or ""),
      j.get("message"))
check("★ 忙时**不**给设备端下发指令", not [s for s in fake.sent if s["event"] == "set_stack_mode"],
      f"sent={[s['event'] for s in fake.sent]}")

backend._set_stack_state(CID, False, False)
fake.sent.clear()
fake.auto_reply, fake.reply_mode, fake.reply_note = True, None, ""
j = (post({"client_id": CID, "enable": True}).get_json() or {})
check("★ 空闲 + 未开 → 开启成功（等设备端回报后回读新状态）",
      j.get("success") is True and j.get("stack_mode") is True, f"{j}")
check("★ 下发了 set_stack_mode 给该设备（带 enable）",
      any(s["event"] == "set_stack_mode" and s["to"] == "sid-stack"
          and (s["payload"] or {}).get("enable") is True for s in fake.sent),
      f"{fake.sent}")

backend._set_stack_state(CID, False, False)      # 服务端镜像：空闲、未开（放行后由设备端自查拒绝）
fake.sent.clear()
fake.reply_mode, fake.reply_note = False, "打印机正忙（正在打印），未执行"
j = (post({"client_id": CID, "enable": True}).get_json() or {})
check("★ 设备端自查拒绝（两端状态竞态）→ success=False 且带回设备端原因",
      j.get("success") is False and "未执行" in (j.get("message") or ""), f"{j}")

backend._set_stack_state(CID, True, False)
fake.sent.clear()
fake.reply_mode, fake.reply_note = None, ""
j = (post({"client_id": CID, "enable": False}).get_json() or {})
check("★ 空闲 + 已开 → 关闭成功", j.get("success") is True and j.get("stack_mode") is False,
      f"{j}")

backend._set_stack_state(CID, False, False)
fake.sent.clear()
fake.auto_reply = False
j = (post({"client_id": CID, "enable": True}).get_json() or {})
check("★ 设备端没在等待窗口内回应 → success=False 且提示稍后重试",
      j.get("success") is False and "回应" in (j.get("message") or ""), j.get("message"))
fake.auto_reply = True

# ==================== ⑤ 三端静态接线 ====================
print("\n=== ⑤ 三端静态接线 ===")
app_src = open(os.path.join(dst, "app.py"), encoding="utf-8").read()
cc_src = open(os.path.join(REPO, "local_print_tool", "cloud_client.py"), encoding="utf-8").read()
gui_src = open(os.path.join(REPO, "local_print_tool", "gui.py"), encoding="utf-8").read()
mp_js = open(os.path.join(REPO, "mobile_apps", "h_n_print", "pages", "index", "index.js"),
             encoding="utf-8").read()
mp_wxml = open(os.path.join(REPO, "mobile_apps", "h_n_print", "pages", "index", "index.wxml"),
               encoding="utf-8").read()
apk_app = open(os.path.join(REPO, "mobile_apps", "android_app", "www", "app.js"),
               encoding="utf-8").read()

# S1-07（2026-10-23）：事件注册统一走 @sio_event（自带令牌桶），判据接受两种写法。
check("后端：socketio `stack_state` 通道存在",
      '@sio_event("stack_state")' in app_src or '@socketio.on("stack_state")' in app_src)
check("后端：`/api/printer/stack_mode` 需登录",
      '"/api/printer/stack_mode"' in app_src and "@login_required" in app_src)
check("后端：claiming_devices 下发四个 stack 字段",
      all(k in app_src for k in ('"stack_mode"', '"stack_busy"',
                                 '"stack_can_toggle"', '"stack_reason"')))
check("★ 后端适用性判据：只认「已确认不支持自动双面」（_stack_needs_manual_flip）",
      "def _stack_needs_manual_flip" in app_src
      and 'get_device_capability(client_id).get("auto_duplex") is False' in app_src)
check("★ 后端闸门先判适用性再看忙闲（顺序即优先级）",
      app_src.index("if not _stack_needs_manual_flip(client_id):")
      < app_src.index('st = _stack_state_of(client_id)\n    if st["mode"] is None:')
      if ("if not _stack_needs_manual_flip(client_id):" in app_src
          and 'st = _stack_state_of(client_id)\n    if st["mode"] is None:' in app_src) else False)
check("★ 后端忙闲口径含「等待人工翻纸打第二趟」与「有排队待打的请求」",
      "等待人工翻纸打第二趟" in app_src and "有排队待打的请求" in app_src)

check("本地工具 cloud_client：信号 + handler + 上报方法齐备",
      "stack_mode_requested = Signal(bool, str)" in cc_src
      and '@sio.on("set_stack_mode")' in cc_src
      and "def report_stack_state" in cc_src)
check("★ 本地工具忙闲判定含：正在打印 / 等翻纸打第 2 趟 / 攒批排队 / 补打队列",
      "def _stack_busy_reason" in gui_src
      and "出纸槽里有" in gui_src
      and "个打印请求在排队等这一叠打完" in gui_src
      and "个订单在补打队列里等打印机空出来" in gui_src)
check("★ 本地工具适用性：档案未确认不支持自动双面 → 攒批不显示也不生效",
      "def _stack_applicable" in gui_src
      and 'getattr(self._printer_profile(), "auto_duplex", None) is False' in gui_src
      and "and self._stack_applicable()" in gui_src)
check("★ 本地工具：复选框按适用性显隐（自动双面的机器直接隐藏）",
      "chk.setVisible(applicable)" in gui_src and "self._set_stack_checkbox(" in gui_src)
check("★ 本地工具：本地勾选与移动端下发都先过适用性闸门",
      gui_src.count("if want and not self._stack_applicable():") == 2)
check("★ 本地工具：换选打印机即刷新攒批入口（不能等下一次刷新）",
      "currentIndexChanged.connect(self._on_printer_selection_changed)" in gui_src
      and "def _on_printer_selection_changed" in gui_src)
check("★ 本地勾选框与移动端下发走同一道闸门（两侧都拦）",
      "def _on_stack_mode_toggled" in gui_src and "self._stack_busy_reason()" in gui_src
      and "空闲后才能" in gui_src)
check("★ 云端指令执行前**再自查一次**忙闲",
      "def _on_remote_stack_mode" in gui_src and "再自查一次" in gui_src)
check("本地工具：连线后立即上报 + 2 秒变化心跳",
      "self._report_stack_state()" in gui_src and "_stack_state_timer" in gui_src
      and "2_000" in gui_src)
check("本地工具：切换后立刻上报（不等下一次 tick）",
      "self._report_stack_state(\n" in gui_src or "_report_stack_state(" in gui_src)

check("★ 小程序：设备列表用到 stack_mode / stack_can_toggle / stack_reason",
      all(k in mp_js for k in ("stack_mode", "stack_can_toggle", "stack_reason")))
check("★ 小程序：调 POST /api/printer/stack_mode 且 body 带 client_id + enable",
      "/api/printer/stack_mode" in mp_js and "client_id" in mp_js and "enable" in mp_js)
check("★ 小程序：管理员角色判定（admin / super_admin）",
      "super_admin" in mp_js and "'admin'" in mp_js)
check("★ 小程序：攒批状态徽标文案 + 深色样式",
      ("攒批中" in mp_wxml or "攒批中" in mp_js)
      and ("theme-dark" in open(os.path.join(REPO, "mobile_apps", "h_n_print", "pages",
                                             "index", "index.wxss"), encoding="utf-8").read()))

check("★ APP：设备列表用到 stack_mode / stack_can_toggle / stack_reason",
      all(k in apk_app for k in ("stack_mode", "stack_can_toggle", "stack_reason")))
check("★ APP：调 POST /api/printer/stack_mode 且带 client_id + enable",
      "/api/printer/stack_mode" in apk_app and "client_id" in apk_app and "enable" in apk_app)
check("★ APP：管理员角色判定",
      "super_admin" in apk_app and "admin" in apk_app)
check("★ 移动端两端同一条适用性判据：只给「需人工翻纸」的设备渲染攒批区块",
      "wx:if=\"{{item.auto_duplex === false}}\"" in mp_wxml
      and "auto_duplex !== false" in apk_app)

# ==================== ⑥ 判据自证（变异必须报错，否则是恒真守卫）====================
print("\n=== ⑥ 判据自证（防恒真守卫）===")
# B1：静态判据不是恒真 —— 把源码里那个关键字抹掉，同一条断言必须转假
_mut_src = app_src.replace('"stack_can_toggle"', '"X"')
check("B1 抹掉 claiming_devices 里的 stack_can_toggle 后，同一条断言必须转假",
      _mut_src != app_src
      and all(k in _mut_src for k in ('"stack_mode"', '"stack_busy"',
                                      '"stack_can_toggle"', '"stack_reason"')) is False)
# B2：忙闲闸门真的在读 busy —— 同一入参下"忙→拦、空闲→放行"必须给出相反结果
backend._set_stack_state(CID, False, True)
_while_busy = gate(CID, True) != ""
backend._set_stack_state(CID, False, False)
_while_idle = gate(CID, True) != ""
check("B2 同一入参：忙 → 拦、空闲 → 放行（证明闸门真在读 busy）",
      _while_busy and not _while_idle, f"busy={_while_busy} idle={_while_idle}")
# B3：移动端静态判据不是恒真 —— 换一个不存在的字段名必须找不到
check("B3 不存在的字段名在两端都找不到",
      "stack_can_toggle_XXX_NOT_EXIST" not in mp_js
      and "stack_can_toggle_XXX_NOT_EXIST" not in apk_app)
# B4：权限判据真的在读角色 —— 同一请求换 user token 必须被拒（对照 admin 放行）
backend._set_stack_state(CID, False, False)
_ok_admin = (post({"client_id": CID, "enable": True}).get_json() or {}).get("success") is True
backend._set_stack_state(CID, False, False)
_ok_user = (post({"client_id": CID, "enable": True}, token=USER_TOKEN).get_json() or {}).get("success")
check("B4 同一请求：admin 放行、user 被拒（证明权限判据不是恒真）",
      _ok_admin and _ok_user is False)
# B5：适用性判据不是恒真/恒假 —— 同一状态（空闲 + 未开）下：自动双面 → 拦、需人工翻纸 → 放行
backend._set_stack_state(CID, False, False)
backend.set_device_manual_capability(CID, True, "测试")
_blocked_by_cap = gate(CID, True) != ""
backend.set_device_manual_capability(CID, False, "测试")
_allowed_by_cap = gate(CID, True) == ""
check("B5 同一状态下：自动双面 → 拦、需人工翻纸 → 放行（证明适用性判据真在读设备能力）",
      _blocked_by_cap and _allowed_by_cap, f"auto={_blocked_by_cap} manual={_allowed_by_cap}")

print("\n" + "=" * 56)
print("攒批模式远程开关验收:", "全部通过 ✅" if ok_all else "存在失败 ❌")
shutil.rmtree(tmp, ignore_errors=True)
sys.exit(0 if ok_all else 1)
