#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""验收：攒批「离线期望」+ 设备上线校验（task-2）。

背景：原来 POST /api/printer/stack_mode 遇到设备离线直接拒绝；
2026-11 改成「先记下期望（落盘，重启不丢），设备上线第一次上报 stack_state 时比对：
一致就不下发、清掉期望；不一致就下发 set_stack_mode（reason=offline_intent）；
设备忙/拒绝则保留期望，下次上线再试」。

覆盖（逐条对应任务契约）：
  ① 离线 POST → success:true / pending:true，期望落盘 uploads/stack_intent.json，
     设备列表（/api/claiming_devices、/api/printer/devices）出现 stack_pending:true；
  ② 「重启」后（清内存 → load_stack_intent）期望仍在；
  ③ 上线且实际状态已一致 → **不下发** set_stack_mode，期望清掉；
  ④ 上线且不一致 → 下发 set_stack_mode，payload 带 reason=offline_intent / by / by_role /
     request_id；设备回报新状态后清掉期望；
  ⑤ 设备忙而"拒绝" → 期望保留；同一会话不重复轰炸（只下发一次），下次上线再试；
  ⑥ 非管理员被拒（本端点沿用旧契约：HTTP 200 + success:false，不是 403）；
  ⑦ 旧闸门仍在：不需要攒批的机型离线也不记期望；在线忙时仍然拦下且不落期望；
     在线切换成功后清掉离线期望；
  ⑧ 静态接线：app.py 里确实有 stack_pending / offline_intent / [stack_intent] 日志。

用法（Windows / GBK 控制台必须带 -X utf8）：
  PYTHONUTF8=1 PYTHONPATH= python -X utf8 tests/verify_stack_offline_intent.py
  PYTHONUTF8=1 PYTHONPATH= python -X utf8 tests/verify_stack_offline_intent.py --mutate pending
      ↑ 在临时副本里改坏一处自证脚本能红（仓库源码不动）。
"""
import io
import json
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

MUTATIONS = {
    "pending": ('"pending": True,', '"pending": False,'),
    "dispatch": ('"reason": "offline_intent",', '"reason": "manual",'),
    "persist": ('json.dump(_stack_intent, f, ensure_ascii=False, indent=2)',
                'json.dump({}, f, ensure_ascii=False, indent=2)'),
    "consistent": ('if st["mode"] is want:',
                   'if False:'),
}
MUTATE = ""
if "--mutate" in sys.argv:
    i = sys.argv.index("--mutate")
    MUTATE = sys.argv[i + 1] if i + 1 < len(sys.argv) else ""
    if MUTATE and MUTATE not in MUTATIONS:
        sys.exit(f"未知变异：{MUTATE}（可选 {sorted(MUTATIONS)}）")

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append(bool(cond))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


def section(title):
    print(f"\n=== {title} ===")


tmp = tempfile.mkdtemp(prefix="hn_stack_intent_")
dst = os.path.join(tmp, "printer-backend")
shutil.copytree(SRC, dst, ignore=shutil.ignore_patterns(
    "__pycache__", "orders.db", "users.db", "uploads", "logs", "venv",
    "config.py", "stack_intent.json", "recent_orders_config.json",
    "device_pin_order.json", "printer_claim.json", "devices.json"))

if MUTATE:
    app_path = os.path.join(dst, "app.py")
    old, new = MUTATIONS[MUTATE]
    with open(app_path, "r", encoding="utf-8") as f:
        src = f.read()
    if old not in src:
        sys.exit(f"[mutate] 锚点消失，请更新 MUTATIONS['{MUTATE}'] 的锚点字符串")
    with open(app_path, "w", encoding="utf-8") as f:
        f.write(src.replace(old, new, 1))
    print(f"[mutate] 已把 app.py 改坏一处（{MUTATE}），期望本脚本转红")

sys.path.insert(0, dst)
os.chdir(dst)
os.environ["FLASK_ENV"] = "development"
try:
    import app as backend
    print(f"imported app.py from {dst}")
except Exception:
    traceback.print_exc()
    sys.exit("无法导入 app.py")

backend.init_db()

APP_SRC = open(os.path.join(dst, "app.py"), "r", encoding="utf-8").read()

# ==================== 桩环境 ====================
CID = "DESKTOP-OFFLINE-9f2c"       # 需人工翻纸的机型（攒批对它有意义）
CID2 = "DESKTOP-AUTODUP-7a1b"      # 自动双面机型（不该有攒批开关）
ADMIN_OID = "openid-admin"
USER_OID = "openid-user"
ROLES = {ADMIN_OID: "admin", USER_OID: "user"}
backend.PRINTER_TOKEN = "test-token"
backend.compute_role = lambda oid: ROLES.get(oid, "guest")
ADMIN_TOKEN = backend.token_serializer.dumps(ADMIN_OID)
USER_TOKEN = backend.token_serializer.dumps(USER_OID)

backend.register_device(CID, "DESKTOP-OFFLINE")
backend.register_device(CID2, "DESKTOP-AUTODUP")
backend.set_device_manual_capability(CID, False, "测试")   # 不支持自动双面 → 需要攒批
backend.set_device_manual_capability(CID2, True, "测试")   # 支持自动双面
backend.get_claiming_devices = lambda: {CID: {"claimed_at": "", "owner_name": "张三"}}
backend.get_claiming_device_ids = lambda: [CID]

client = backend.app.test_client()


def post(body, token=ADMIN_TOKEN):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return client.post("/api/printer/stack_mode", json=body, headers=headers)


def go_offline():
    backend.printer_clients.clear()
    backend._sid_to_client.clear()


def intent_path():
    return os.path.join(os.getcwd(), backend.STACK_INTENT_FILE)


def read_intent_file():
    try:
        with open(intent_path(), "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def set_intent(enable, by="管理员", by_role="admin"):
    return backend._set_stack_intent(CID, enable, by=by, by_role=by_role)


def clear_all_intents():
    with backend._stack_intent_lock:
        backend._stack_intent.clear()
        backend._stack_intent_dispatched.clear()
    try:
        os.remove(intent_path())
    except OSError:
        pass


def connect(name="DESKTOP-OFFLINE", cid=CID):
    return backend.socketio.test_client(
        backend.app,
        query_string=f"?token=test-token&client_id={cid}&device_name={name}")


def recv_sets(ws):
    """取出该 socket 客户端收到的所有 set_stack_mode 事件 payload。"""
    out = []
    for e in ws.get_received():
        if e.get("name") == "set_stack_mode":
            args = e.get("args") or []
            out.append(args[0] if args else {})
    return out


# ==================== ① 离线 → 记录期望并落盘 ====================
section("① 离线 POST → pending 期望（落盘 + 设备列表可见）")
go_offline()
backend._set_stack_state(CID, None, False)
clear_all_intents()

r = post({"client_id": CID, "enable": True})
j = r.get_json() or {}
check("离线 POST → HTTP 200 / success:true（不再拒绝）",
      r.status_code == 200 and j.get("success") is True, f"status={r.status_code} body={j}")
check("返回 pending:true + 明确文案",
      j.get("pending") is True and "已记录" in (j.get("message") or ""), f"body={j}")
check("返回里带 stack_mode / stack_busy（前端两个字段照旧读）",
      "stack_mode" in j and "stack_busy" in j, f"body={j}")
check("离线时**不**进同步等待下发（不建 waiter，设备根本收不到）",
      backend.printer_stack_waiters == {}, f"waiters={backend.printer_stack_waiters}")

f = read_intent_file()
ent = f.get(CID) or {}
check("期望已落盘 stack_intent.json（uploads/ 下）", bool(ent), f"file={f}")
check("落盘内容 enable:true / applied:false", ent.get("enable") is True
      and ent.get("applied") is False, f"ent={ent}")
check("落盘带操作者 by=管理员 / by_role=admin",
      ent.get("by") == "管理员" and ent.get("by_role") == "admin", f"ent={ent}")
check("落盘带 ts（YYYY-MM-DD HH:MM:SS）",
      len(ent.get("ts") or "") == 19 and (ent.get("ts") or "")[4] == "-", f"ts={ent.get('ts')}")
check("内存副本 pending=True", backend._stack_intent_pending(CID) is True)

r = client.get("/api/claiming_devices")
cd = {d["client_id"]: d for d in (r.get_json() or {}).get("devices") or []}
check("/api/claiming_devices 下发 stack_pending:true",
      (cd.get(CID) or {}).get("stack_pending") is True, f"dev={cd.get(CID)}")
r = client.get("/api/printer/devices", headers={"X-Printer-Token": "test-token"})
pd_ = {d["client_id"]: d for d in (r.get_json() or {}).get("devices") or []}
check("/api/printer/devices 下发 stack_pending:true",
      (pd_.get(CID) or {}).get("stack_pending") is True, f"dev={pd_.get(CID)}")

# 覆盖语义：再点一次「关闭」应覆盖成 enable:false，而不是留下两条
r = post({"client_id": CID, "enable": False})
j = r.get_json() or {}
check("离线再点关闭 → 覆盖同一条期望（enable:false）",
      j.get("success") is True and (read_intent_file().get(CID) or {}).get("enable") is False,
      f"body={j} file={read_intent_file()}")
set_intent(True)   # 回到开启，供 ③/④ 用

# ==================== ② 重启不丢 ====================
section("② 重启（清内存 → 重新 load）后期望仍在")
with backend._stack_intent_lock:
    backend._stack_intent.clear()
    backend._stack_intent_dispatched[CID] = True
check("清内存后 pending=False（模拟重启瞬间）", backend._stack_intent_pending(CID) is False)
loaded = backend.load_stack_intent()
check("重新 load 后读回期望（重启不丢）",
      backend._stack_intent_pending(CID) is True
      and (loaded.get(CID) or {}).get("enable") is True, f"loaded={loaded}")
check("load 会清掉「本次已下发」标记（新会话允许重试）",
      CID not in backend._stack_intent_dispatched,
      f"dispatched={backend._stack_intent_dispatched}")

# ==================== ③ 上线且一致 → 不下发、清期望 ====================
section("③ 上线且状态已一致 → 不下发、清掉期望")
go_offline()
backend._set_stack_state(CID, True, False)   # 设备实际就开着
set_intent(True)                             # 期望也是开 → 一致
ws = connect()
check("设备已连上（上线）", ws.is_connected())
ws.emit("stack_state", {"mode": True, "busy": False})
sent = recv_sets(ws)
check("一致 → **不**下发 set_stack_mode", sent == [], f"sent={sent}")
check("一致 → 期望已清掉", backend._stack_intent_pending(CID) is False)
check("一致 → 落盘文件里也没了", CID not in read_intent_file(), f"file={read_intent_file()}")
ws.disconnect()
go_offline()

# ==================== ④ 上线不一致 → 下发 offline_intent ====================
section("④ 上线不一致 → 下发 set_stack_mode(reason=offline_intent)")
backend._set_stack_state(CID, False, False)  # 设备实际关着
go_offline()
r = post({"client_id": CID, "enable": True})   # 离线点「开启」
check("准备：离线期望已记下（pending）",
      (r.get_json() or {}).get("pending") is True and backend._stack_intent_pending(CID))
ws = connect()
ws.emit("stack_state", {"mode": False, "busy": False})
sent = recv_sets(ws)
check("不一致 → 恰好下发 1 条 set_stack_mode", len(sent) == 1, f"sent={sent}")
p = sent[0] if sent else {}
check("payload.enable = 期望值 True", p.get("enable") is True, f"payload={p}")
check("payload.reason = offline_intent", p.get("reason") == "offline_intent", f"payload={p}")
check("payload.by / by_role = 当初操作者信息",
      p.get("by") == "管理员" and p.get("by_role") == "admin", f"payload={p}")
check("payload.request_id 非空（8 位）",
      isinstance(p.get("request_id"), str) and len(p["request_id"]) == 8, f"payload={p}")
check("设备还没接受 → 期望仍保留", backend._stack_intent_pending(CID) is True)
# 设备真的切过去了，回报新状态 → 期望落地清掉
ws.emit("stack_state", {"mode": True, "busy": False})
sent2 = recv_sets(ws)
check("设备接受后 → 不再重复下发", sent2 == [], f"sent={sent2}")
check("设备接受后 → 期望清掉", backend._stack_intent_pending(CID) is False)
ws.disconnect()
go_offline()

# ==================== ⑤ 设备忙拒绝 → 保留、下次上线再试 ====================
section("⑤ 设备忙拒绝 → 期望保留（且同会话不轰炸）")
backend._set_stack_state(CID, False, False)
go_offline()
post({"client_id": CID, "enable": True})
ws = connect()
ws.emit("stack_state", {"mode": False, "busy": True, "note": "打印机正忙（正在打印）"})
sent = recv_sets(ws)
check("上线首次上报不一致 → 先下发一次", len(sent) == 1, f"sent={sent}")
ws.emit("stack_state", {"mode": False, "busy": True, "note": "打印机正忙（正在打印）"})
sent2 = recv_sets(ws)
check("设备忙拒绝后再上报 → 不再重复下发（不轰炸设备）", sent2 == [], f"sent={sent2}")
check("忙拒绝 → 期望保留（下次上线再试）", backend._stack_intent_pending(CID) is True)
check("忙拒绝 → 落盘也还在", (read_intent_file().get(CID) or {}).get("enable") is True,
      f"file={read_intent_file()}")
ws.disconnect()
go_offline()

# 下次上线再试：新会话清掉 dispatched，空闲后应再次下发
backend._set_stack_state(CID, False, False)
ws = connect()
ws.emit("stack_state", {"mode": False, "busy": False})
sent3 = recv_sets(ws)
check("下次上线（设备空闲）→ 重新下发一次", len(sent3) == 1, f"sent={sent3}")
ws.disconnect()
go_offline()

# ==================== ⑥ 权限 ====================
section("⑥ 权限（沿用旧契约：200 + success:false）")
go_offline()
clear_all_intents()
r = post({"client_id": CID, "enable": True}, token=USER_TOKEN)
j = r.get_json() or {}
check("普通用户 → 被拒（success:false / 仅管理员）",
      r.status_code == 200 and j.get("success") is False
      and "仅管理员" in (j.get("message") or ""), f"status={r.status_code} body={j}")
check("普通用户被拒后没有落期望", backend._stack_intent_pending(CID) is False
      and CID not in read_intent_file())
r = post({"client_id": CID, "enable": True}, token="")
check("未登录 → 401", r.status_code == 401, f"status={r.status_code}")

# ==================== ⑦ 旧闸门仍在 ====================
section("⑦ 旧闸门：不需要攒批 / 在线忙 / 在线成功清期望")
go_offline()
r = post({"client_id": CID2, "enable": True})
j = r.get_json() or {}
check("自动双面机型离线也不记期望（不需要攒批）",
      j.get("success") is False and "不需要攒批" in (j.get("message") or "")
      and backend._stack_intent_pending(CID2) is False, f"body={j}")

# 在线 + 忙：走原 _stack_toggle_problem，拦下且不落期望
clear_all_intents()
backend.printer_clients[CID] = {"sid": "sid-x", "heartbeat": datetime.now(),
                                "connected_at": datetime.now()}
backend._set_stack_state(CID, False, True)
r = post({"client_id": CID, "enable": True})
j = r.get_json() or {}
check("在线忙 → 仍然拦下并给正忙原因", j.get("success") is False
      and "正忙" in (j.get("message") or ""), f"body={j}")
check("在线忙被拦 → 不产生期望", backend._stack_intent_pending(CID) is False)


# 在线成功 → 清掉残留的离线期望
class FakeSocketIO:
    """记录 emit；收到 set_stack_mode 立刻模拟设备执行 + 回报（在线路径是同步等待）。"""

    def __init__(self):
        self.sent = []
        self.reply_mode = None

    def emit(self, event, payload=None, to=None):
        self.sent.append({"event": event, "payload": payload, "to": to})
        if event != "set_stack_mode":
            return
        mode = self.reply_mode if self.reply_mode is not None else bool((payload or {}).get("enable"))
        backend._set_stack_state(CID, mode, False, "")
        with backend.printer_stack_state_lock:
            w = backend.printer_stack_waiters.pop(CID, None)
            if w is not None:
                backend.printer_stack_results[CID] = {"mode": mode, "busy": False, "note": ""}
        if w is not None:
            w.set()


fake = FakeSocketIO()
real_sio = backend.socketio
backend.socketio = fake
backend.STACK_TOGGLE_WAIT_SEC = 0.3
set_intent(True)                      # 留一条"离线期望"
backend.printer_clients[CID] = {"sid": "sid-x", "heartbeat": datetime.now(),
                                "connected_at": datetime.now()}
backend._set_stack_state(CID, False, False)
r = post({"client_id": CID, "enable": True})
j = r.get_json() or {}
check("在线空闲 + 未开 → 切换成功（原逻辑不变）",
      j.get("success") is True and j.get("stack_mode") is True, f"body={j}")
check("在线成功后清掉离线期望", backend._stack_intent_pending(CID) is False)
backend.socketio = real_sio
backend.STACK_TOGGLE_WAIT_SEC = 3.0

# ==================== ⑧ 静态接线 ====================
section("⑧ 静态接线（日志标签 / 字段名 / 契约字符串）")
check("app.py 里有 [stack_intent] 日志标签", APP_SRC.count("[stack_intent]") >= 4,
      f"count={APP_SRC.count('[stack_intent]')}")
check("app.py 里下发 payload 带 reason=offline_intent",
      '"reason": "offline_intent"' in APP_SRC)
check("两个设备列表接口都下发 stack_pending",
      APP_SRC.count('"stack_pending"') >= 3, f"count={APP_SRC.count(chr(34) + 'stack_pending' + chr(34))}")
check("printer_state 也下发 stack_pending",
      "stack_pending" in APP_SRC.split("def _printer_state_payload")[-1].split("def ")[0])

# ==================== 汇总 ====================
print()
if MUTATE:
    print(f"[mutate={MUTATE}] 失败 {RESULTS.count(False)} / 共 {len(RESULTS)} 条")
    print("（自证模式：只要出现 FAIL 就说明能红；本次退出码恒为 0，不污染套件）")
    sys.exit(0)
if all(RESULTS):
    print(f"全部通过：{len(RESULTS)} 条检查")
    sys.exit(0)
print(f"存在失败：{RESULTS.count(False)} / {len(RESULTS)} 条")
sys.exit(1)
