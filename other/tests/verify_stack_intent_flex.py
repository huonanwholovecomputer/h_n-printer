#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""验收 · 攒批期望（stack_intent）可反复改 / 可重试下发 / stack_desired 字段（2026-11 task-1 C4）

背景：攒批远程开关只对「无自动双面器、需人工翻纸」的机型有意义。设备离线时点开关，
后端先把**期望**落盘（重启不丢），等设备上线第一次上报 stack_state 时比对：
  一致 → 清期望；不一致 → 下发 set_stack_mode(reason=offline_intent)。

2026-11 的两处行为变更（本脚本逐条验）：
  ① 旧实现是「每次上线最多自动重试一次」的**一次性**限制（_stack_intent_dispatched
     只存 {client_id: enable}）—— 设备忙/掉线一次就再也不试，期望永远停在 pending；
     现在每次下发都记 {enable, at, count}，冷却（STACK_INTENT_RETRY_COOLDOWN_SEC）过后
     仍不一致就再发，直到设备接受或人改主意；
  ② 后写覆盖先写：离线连点三次（开/关/开），只留最后一条，且新指令立刻生效、
     不受冷却限制；在线改过之后旧期望不会残留。

另验：两个设备列表接口 + printer_state 都下发 stack_desired（true/false/null）与
stack_pending（bool），前端不用再靠 stack_pending 猜"期望是开还是关"。

用法（本机只有 Python312）：
  PYTHONPATH= PYTHONIOENCODING=utf-8 \
    C:/Users/Administrator/AppData/Local/Programs/Python/Python312/python.exe \
    tests/verify_stack_intent_flex.py
exit 0 = 全绿。
"""
import io
import json
import os
import shutil
import sys
import tempfile
import traceback
from datetime import datetime, timedelta

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SRC = os.path.join(REPO, "mobile_apps", "printer-backend")

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append(bool(cond))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


def section(title):
    print(f"\n=== {title} ===")


TMP = tempfile.mkdtemp(prefix="hn_stack_flex_")
DST = os.path.join(TMP, "printer-backend")
shutil.copytree(SRC, DST, ignore=shutil.ignore_patterns(
    "__pycache__", "orders.db", "users.db", "uploads", "logs", "venv",
    "config.py", "stack_intent.json", "recent_orders_config.json",
    "device_pin_order.json", "printer_claim.json", "devices.json"))

sys.path.insert(0, DST)
os.chdir(DST)
os.environ["FLASK_ENV"] = "development"
try:
    import app as backend
    print(f"imported app.py from {DST}")
except Exception:
    traceback.print_exc()
    sys.exit("无法导入 app.py")

backend.init_db()
APP_SRC = open(os.path.join(DST, "app.py"), "r", encoding="utf-8").read()

# ==================== 桩环境 ====================
CID = "DESKTOP-FLEX-5c5c"
ADMIN_OID = "openid-admin"
ROLES = {ADMIN_OID: "admin"}
backend.PRINTER_TOKEN = "test-token"
backend.compute_role = lambda oid: ROLES.get(oid, "guest")
ADMIN_TOKEN = backend.token_serializer.dumps(ADMIN_OID)

backend.register_device(CID, "DESKTOP-FLEX")
backend.set_device_manual_capability(CID, False, "测试")   # 不支持自动双面 → 攒批有意义
backend.get_claiming_devices = lambda: {CID: {"claimed_at": "", "owner_name": "张三"}}
backend.get_claiming_device_ids = lambda: [CID]

client = backend.app.test_client()


def post(enable):
    return client.post("/api/printer/stack_mode", json={"client_id": CID, "enable": enable},
                       headers={"Authorization": f"Bearer {ADMIN_TOKEN}"})


def go_offline():
    backend.printer_clients.clear()
    backend._sid_to_client.clear()


def intent_file():
    try:
        with open(os.path.join(os.getcwd(), backend.STACK_INTENT_FILE), "r",
                  encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def connect():
    return backend.socketio.test_client(
        backend.app, query_string=f"?token=test-token&client_id={CID}&device_name=DESKTOP-FLEX")


def recv_sets(ws):
    out = []
    for e in ws.get_received():
        if e.get("name") == "set_stack_mode":
            args = e.get("args") or []
            out.append(args[0] if args else {})
    return out


def age_dispatch(seconds=60):
    """把「上次下发」时间往前拨，模拟冷却已过（避免测试里真等 20 秒）。"""
    with backend._stack_intent_lock:
        rec = backend._stack_intent_dispatched.get(CID)
        if isinstance(rec, dict):
            rec["at"] = datetime.now() - timedelta(seconds=seconds)


def clear_all():
    with backend._stack_intent_lock:
        backend._stack_intent.clear()
        backend._stack_intent_dispatched.clear()
    try:
        os.remove(os.path.join(os.getcwd(), backend.STACK_INTENT_FILE))
    except OSError:
        pass


# ==================== ① stack_desired 字段 ====================
section("① stack_desired / stack_pending 三个出口（true / false / null）")
go_offline()
backend._set_stack_state(CID, None, False)
clear_all()

r = client.get("/api/printer/devices", headers={"X-Printer-Token": "test-token"})
dev = {d["client_id"]: d for d in (r.get_json() or {}).get("devices") or []}.get(CID) or {}
check("无期望 → stack_desired=None（不是缺字段）",
      "stack_desired" in dev and dev["stack_desired"] is None, str(dev.get("stack_desired")))
check("无期望 → stack_pending=False", dev.get("stack_pending") is False,
      str(dev.get("stack_pending")))

backend._set_stack_intent(CID, True, by="管理员", by_role="admin")
r = client.get("/api/printer/devices", headers={"X-Printer-Token": "test-token"})
dev = {d["client_id"]: d for d in (r.get_json() or {}).get("devices") or []}.get(CID) or {}
check("期望开启 → stack_desired=True", dev.get("stack_desired") is True,
      str(dev.get("stack_desired")))
check("期望开启 → stack_pending=True", dev.get("stack_pending") is True)

r = client.get("/api/claiming_devices")
cdev = {d["client_id"]: d for d in (r.get_json() or {}).get("devices") or []}.get(CID) or {}
check("/api/claiming_devices 也带 stack_desired=True", cdev.get("stack_desired") is True,
      str(cdev.get("stack_desired")))

ps = backend._printer_state_payload(CID)
check("printer_state 载荷也带 stack_desired=True", ps.get("stack_desired") is True, str(ps))

backend._set_stack_intent(CID, False, by="管理员", by_role="admin")
r = client.get("/api/printer/devices", headers={"X-Printer-Token": "test-token"})
dev = {d["client_id"]: d for d in (r.get_json() or {}).get("devices") or []}.get(CID) or {}
check("期望关闭 → stack_desired=False（不是 None，也不丢）",
      dev.get("stack_desired") is False, str(dev.get("stack_desired")))

# ==================== ② 离线反复改：后写覆盖先写 ====================
section("② 离线连点（开→关→开）只留最后一条")
go_offline()
backend._set_stack_state(CID, False, False)
clear_all()
for want in (True, False, True):
    r = post(want)
    j = r.get_json() or {}
    if not (r.status_code == 200 and j.get("success") is True and j.get("pending") is True):
        check(f"离线 POST enable={want} 返回 success+pending", False, f"{r.status_code} {j}")
        break
f = intent_file()
check("落盘只有一条（同 client_id 后写覆盖）",
      list(f.keys()) == [CID], str(list(f.keys())))
check("最终期望 = 最后一次点击（True）", (f.get(CID) or {}).get("enable") is True,
      str((f.get(CID) or {}).get("enable")))
check("内存副本与落盘一致（stack_desired=True）",
      backend._stack_desired_of(CID) is True, str(backend._stack_desired_of(CID)))

# ==================== ③ 可重试下发（不再是一次性） ====================
section("③ 冷却过后仍不一致 → 再发（旧实现只发一次）")
backend._set_stack_state(CID, None, False)
clear_all()
backend._set_stack_intent(CID, True, by="管理员", by_role="admin")
ws = connect()
check("设备已连接", ws.is_connected())
backend._set_stack_state(CID, False, False)

ws.emit("stack_state", {"mode": False, "busy": False})
sent1 = recv_sets(ws)
check("首次上报不一致 → 下发 1 条", len(sent1) == 1, f"sent={sent1}")
rec = backend._stack_intent_dispatched.get(CID)
check("dispatched 记录 {enable, at, count}（不再是裸 bool）",
      isinstance(rec, dict) and rec.get("enable") is True and isinstance(rec.get("at"),
                                                                       datetime)
      and rec.get("count") == 1, str(rec))

ws.emit("stack_state", {"mode": False, "busy": False})
sent_same = recv_sets(ws)
check("冷却内重复上报 → 不重发（防抖）", sent_same == [], f"sent={sent_same}")

age_dispatch(60)   # 模拟过了冷却
ws.emit("stack_state", {"mode": False, "busy": False})
sent2 = recv_sets(ws)
check("冷却过后仍不一致 → **再发**一次（可重试，不是一次性）",
      len(sent2) == 1, f"sent={sent2}")
rec2 = backend._stack_intent_dispatched.get(CID)
check("count 累加到 2（便于排查反复下发）",
      isinstance(rec2, dict) and rec2.get("count") == 2, str(rec2))

# 人改主意「改回设备现状」→ 不该再发一条，期望直接清掉
backend._set_stack_intent(CID, False, by="管理员", by_role="admin")
ws.emit("stack_state", {"mode": False, "busy": False})
sent_same2 = recv_sets(ws)
check("期望改回设备现状 → 不发指令、直接清期望（不制造无谓切换）",
      sent_same2 == [] and backend._stack_intent_pending(CID) is False,
      f"sent={sent_same2} pending={backend._stack_intent_pending(CID)}")

# 后写覆盖先写 → 新期望与「上次下发过的」不同时立刻发，不等冷却
# （用 mode=None 制造「实际未知」：此时 desired 与 actual 无法判定一致）
backend._set_stack_state(CID, None, False)
backend._set_stack_intent(CID, False, by="管理员", by_role="admin")
ws.emit("stack_state", {"mode": None, "busy": False})
sent_unknown = recv_sets(ws)
check("实际未知 + 有期望 → 下发（宁可按期望推一次）",
      len(sent_unknown) == 1 and sent_unknown[0].get("enable") is False,
      f"sent={sent_unknown}")
backend._set_stack_intent(CID, True, by="管理员", by_role="admin")   # 冷却期内改主意
ws.emit("stack_state", {"mode": None, "busy": False})
sent3 = recv_sets(ws)
check("期望改成相反值 → 立刻下发（人的新指令不受冷却限制）",
      len(sent3) == 1 and sent3[0].get("enable") is True, f"sent={sent3}")

# 设备真的切过去了
backend._set_stack_state(CID, True, False)
ws.emit("stack_state", {"mode": True, "busy": False})
recv_sets(ws)
check("设备接受（mode == 期望 True）→ 期望清掉",
      backend._stack_intent_pending(CID) is False, str(backend._stack_intent_pending(CID)))
check("设备接受 → stack_desired 回到 None",
      backend._stack_desired_of(CID) is None, str(backend._stack_desired_of(CID)))
check("设备接受 → dispatched 也清掉（新会话可重新下发）",
      CID not in backend._stack_intent_dispatched,
      str(list(backend._stack_intent_dispatched.keys())))
ws.disconnect()
go_offline()

# ==================== ④ 设备忙：期望保留、不轰炸 ====================
section("④ 设备忙：期望保留 + 冷却内不轰炸 + 冷却后再试")
backend._set_stack_state(CID, None, False)
clear_all()
backend._set_stack_intent(CID, True, by="管理员", by_role="admin")
ws = connect()
backend._set_stack_state(CID, False, True)   # 忙
ws.emit("stack_state", {"mode": False, "busy": True, "note": "打印机正忙"})
sent1 = recv_sets(ws)
check("忙时首次仍下发一次（由设备自己裁决/拒绝）", len(sent1) == 1, f"sent={sent1}")
ws.emit("stack_state", {"mode": False, "busy": True, "note": "打印机正忙"})
sent2 = recv_sets(ws)
check("忙时重复上报 → 冷却内不重发（不轰炸设备）", sent2 == [], f"sent={sent2}")
check("忙 → 期望**保留**（绝不在忙的时候清掉）",
      backend._stack_intent_pending(CID) is True, str(backend._stack_intent_pending(CID)))
age_dispatch(60)
ws.emit("stack_state", {"mode": False, "busy": True, "note": "打印机正忙"})
sent3 = recv_sets(ws)
check("冷却过后仍忙 → 再试一次（期望没丢，设备一空闲就能落地）",
      len(sent3) == 1, f"sent={sent3}")
check("忙 → 落盘期望仍在", (intent_file().get(CID) or {}).get("enable") is True,
      str(intent_file()))
# 设备空闲后真正切过去
backend._set_stack_state(CID, True, False)
ws.emit("stack_state", {"mode": True, "busy": False})
recv_sets(ws)
check("设备空闲后接受 → 期望清掉", backend._stack_intent_pending(CID) is False)
ws.disconnect()
go_offline()

# ==================== ⑤ 静态接线（防止回退成一次性） ====================
section("⑤ 静态接线")
check("冷却常量存在且为正数",
      isinstance(backend.STACK_INTENT_RETRY_COOLDOWN_SEC, (int, float))
      and backend.STACK_INTENT_RETRY_COOLDOWN_SEC > 0,
      str(backend.STACK_INTENT_RETRY_COOLDOWN_SEC))
check("app.py 里 dispatched 记的是 {enable, at, count} 而不是裸 bool",
      "_stack_intent_dispatched[client_id] = {" in APP_SRC
      and '"enable": want,' in APP_SRC
      and '"at": datetime.now(),' in APP_SRC
      and '"count": int(rec.get("count") or 0) + 1' in APP_SRC)
check("下发的 set_stack_mode 仍带 reason=offline_intent（前端/设备按它认来源）",
      '"reason": "offline_intent"' in APP_SRC)
check("三个出口都带 stack_desired",
      APP_SRC.count('"stack_desired"') == 3,
      f"count={APP_SRC.count(chr(34) + 'stack_desired' + chr(34))}")

# ==================== 汇总 ====================
ok = all(RESULTS)
print("\n" + "=" * 68)
print(f"攒批期望灵活度验收：{sum(RESULTS)}/{len(RESULTS)} 条通过" + (" ✅" if ok else " ❌"))
sys.exit(0 if ok else 1)
