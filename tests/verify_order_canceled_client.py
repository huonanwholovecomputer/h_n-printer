# -*- coding: utf-8 -*-
"""验收 · 本地工具 order_canceled 客户端处理器（顾客取消 ⇄ 人工订正，v7.0 / 2026-10-18）

为什么单独一条持久守卫：后端 F1 把「管理员在结算页人工订正为终态」**复用**到了
`order_canceled` 这条既有链路上（好处：后端热部署即刻对存量客户端生效），于是同一个事件
承载两件事：

    {"order_id":12,"task_ids":[3,4],"reason":"manual_fix","status":"abandoned"}   人工订正
    {"order_id":15,"task_ids":[7]}                                                顾客主动取消

客户端把信号从 2 参扩成 3 参（第 3 参 = 订正后的状态，普通取消为 ""），
`gui.MainWindow._on_cloud_order_canceled(self, order_id, task_ids, fix_status="")`
用默认值兼容既有两参连接。这条链路一旦被改坏（信号 arity 变了 / reason-status 解析失效），
PySide6 在 `emit()` 时抛 TypeError，而**现象是「已取消的订单照样打」**——正是本批 🔴 要堵
的那件事；此前只有一轮性探针，没有入库守卫。

做法（不起网络、不起 GUI）：假 `socketio` 模块 + 真 `CloudClient`，连一次把 handler 注册上，
再从 `client._sio.handlers["order_canceled"]` 拿**真 handler** 喂 payload，只看**行为**：
emit 的实参、以及「人工订正」与「普通取消」是否走了**不同**的消息分支（用两条 payload 的
消息文本是否相同判定，不写死任何中文文案）。

用法: PYTHONPATH= PYTHONUTF8=1 <python> tests/verify_order_canceled_client.py   （exit 0 = 全绿）
"""
import io
import os
import re
import sys
import tempfile
import types

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL_TOOL = os.path.join(REPO, "local_print_tool")

# 沙箱：不碰真实 %APPDATA%\HN打印工具（CloudClient 的缓存/索引路径都由 paths 推导）
_SANDBOX = tempfile.mkdtemp(prefix="hn_oc_client_")
os.environ["APPDATA"] = _SANDBOX
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, LOCAL_TOOL)
os.chdir(LOCAL_TOOL)

bad = 0


def chk(name, cond, detail=""):
    global bad
    if not cond:
        bad += 1
    print("[%s] %s%s" % ("PASS" if cond else "FAIL", name,
                         ("  -- " + str(detail)) if (not cond and detail != "") else ""))


# ── 假 socketio：与 tests/verify_cloud_proxy_bypass.py 同一手法（不起真网络） ──
class _FakeSioClient:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.handlers = {}
        self.connect_calls = []
        self.connected = False

    def on(self, event):
        def deco(fn):
            self.handlers[event] = fn
            return fn
        return deco

    def connect(self, url, **kwargs):
        self.connect_calls.append((url, kwargs))
        self.connected = True
        fn = self.handlers.get("connect")
        if fn:
            fn()

    def disconnect(self):
        if self.connected:
            self.connected = False
            fn = self.handlers.get("disconnect")
            if fn:
                fn()

    def emit(self, *a, **k):
        pass


_fake_module = types.ModuleType("socketio")
_fake_module.Client = _FakeSioClient
sys.modules["socketio"] = _fake_module

import cloud_client as cc                     # noqa: E402

CC_SRC = open(os.path.join(LOCAL_TOOL, "cloud_client.py"), encoding="utf-8").read()
GUI_SRC = open(os.path.join(LOCAL_TOOL, "gui.py"), encoding="utf-8").read()

print("=== 1. 信号契约（3 参）与 gui 槽默认值（既有两参调用不破） ===")
_cancel_sig = re.search(r"order_canceled = Signal\(([^)]*)\)", CC_SRC)
chk("★ cloud_client: order_canceled = Signal(int, list, str)"
    "（改前 Signal(int, list)：订正状态无处可传）",
    _cancel_sig is not None
    and [s.strip() for s in _cancel_sig.group(1).split(",")] == ["int", "list", "str"],
    _cancel_sig.group(1) if _cancel_sig else None)
chk("★ gui: _on_cloud_order_canceled(self, order_id: int, task_ids: list, fix_status: str = \"\")"
    "—— 第 3 参有默认值，既有两参调用/两参连接不破",
    re.search(r"def _on_cloud_order_canceled\(self, order_id: int, task_ids: list,"
              r" fix_status: str = \"\"\):", GUI_SRC) is not None)

print("\n=== 2. 真 handler（真 CloudClient + 假 socketio 连接） ===")
client = cc.CloudClient(api_url="", ws_url="", token="", client_id="cancel-guard-device")
client._stop_event.set()          # 不让成功路径阻塞在等待断开的循环里

_conn_err = None
try:
    client._connect_and_wait()
except Exception as e:            # noqa: BLE001
    _conn_err = repr(e)
chk("建连成功（假 socketio）", _conn_err is None, _conn_err)

handler = None
try:
    handler = client._sio.handlers.get("order_canceled")
except Exception:                 # noqa: BLE001
    pass
chk("★ 真连接上注册了 order_canceled handler（从 _sio.handlers 取到真函数）",
    callable(handler), type(handler).__name__)

got = []
messages = []


def _on_canceled(oid, tids, fix_status):
    got.append((oid, tids, fix_status))


_connect_err = None
try:
    client.order_canceled.connect(_on_canceled)
except Exception as e:            # noqa: BLE001
    _connect_err = repr(e)
chk("★ 三参槽能连到 order_canceled（信号 arity 真是 3；改前 Signal(int,list) 连三参槽当场 "
    "TypeError —— 真实 link 里就是 emit 时炸）",
    _connect_err is None, _connect_err)
client.status_message.connect(messages.append)


def _feed(payload):
    """喂一个 payload，返回 (emit 实参, 最后一条状态消息, 异常)。任何异常都不许抛穿 —— 
    反过来要红在下面的 chk 上（反向验证时改前版会在 emit 里 TypeError）。"""
    got.clear()
    messages.clear()
    err = None
    if not callable(handler):
        return None, None, "no handler"
    try:
        handler(payload)
    except Exception as e:        # noqa: BLE001
        err = repr(e)
    return (got[0] if got else None), (messages[-1] if messages else None), err


def _norm(msg):
    """抹掉具体 order_id 再比较消息分支（否则两条 payload 的 id 不同会让文本必然不同）。"""
    return re.sub(r"#\d+", "#<id>", str(msg or ""))


print("\n=== 3. 行为：人工订正 / 顾客取消 / reason 有但 status 缺失 ===")
args1, msg1, err1 = _feed({"order_id": 12, "task_ids": [3, 4],
                           "reason": "manual_fix", "status": "abandoned"})
chk("★ 人工订正 payload → emit 实参 == (12, [3, 4], \"abandoned\")（第 3 参真拿到状态）",
    err1 is None and args1 == (12, [3, 4], "abandoned"), (args1, err1))
chk("  status 原样透传，不做字符串化/不擅自翻成中文",
    args1 is not None and args1[2] == "abandoned", args1)

args2, msg2, err2 = _feed({"order_id": 15, "task_ids": [7]})
chk("★ 顾客取消（老 payload，无 reason/status）→ emit 实参 == (15, [7], \"\")",
    err2 is None and args2 == (15, [7], ""), (args2, err2))

args3, msg3, err3 = _feed({"order_id": 16, "task_ids": [8], "reason": "manual_fix"})
chk("★ reason==manual_fix 但 status 缺失 → 按旧语义 (16, [8], \"\")，不伪造订正文案",
    err3 is None and args3 == (16, [8], ""), (args3, err3))

chk("★ 订正分支与取消分支的**消息文本不同**（判分支用行为，不写死中文文案）",
    msg1 is not None and msg2 is not None and _norm(msg1) != _norm(msg2),
    (_norm(msg1), _norm(msg2)))
chk("★ reason 有但 status 缺失时与「顾客取消」走**同一条**消息分支"
    "（第 3 参为 \"\" 就不该说成人工订正）",
    msg2 is not None and msg3 is not None and _norm(msg3) == _norm(msg2),
    (_norm(msg3), _norm(msg2)))

print("\n=== 4. 畸变载荷：不炸、仍 emit、第 3 参为 \"\" ===")
MALFORMED = [
    ("None", None, (0, [], "")),
    ("非 dict（list）", [1, 2], (0, [], "")),
    # task_ids 照旧透传（handler 还要用它清 _pending_tasks），只有 order_id 兜底成 0
    ("缺 order_id", {"task_ids": [9]}, (0, [9], "")),
    ("status 非字符串（123）", {"order_id": 17, "task_ids": [10],
                              "reason": "manual_fix", "status": 123}, (17, [10], "")),
]
for _label, _pl, _want in MALFORMED:
    _args, _msg, _err = _feed(_pl)
    chk("③ %s：不炸（无异常）且仍 emit 三参" % _label,
        _err is None and _args is not None, (_args, _err))
    chk("③ %s：第 3 参为 \"\"（非字符串/缺失的 status 不伪造订正文案）" % _label,
        _args == _want, (_args, _want))

print("\n=== 5. 回归：handler 仍在收到事件后清掉 _pending_tasks ===")
client._pending_tasks[3] = object()
client._pending_tasks[4] = object()
_feed({"order_id": 12, "task_ids": [3, 4], "reason": "manual_fix", "status": "abandoned"})
chk("handler 把 payload 里的 task_ids 从 _pending_tasks 移除（撤单/订正后不再重试）",
    3 not in client._pending_tasks and 4 not in client._pending_tasks,
    list(client._pending_tasks.keys()))

print("\n" + "=" * 56)
if bad:
    print("order_canceled 客户端处理器验收：存在失败 ❌ (%d)" % bad)
else:
    print("order_canceled 客户端处理器验收：全部通过 ✅")
sys.exit(1 if bad else 0)
