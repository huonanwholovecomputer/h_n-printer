#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""验收 · 超时状态 timeout / 迟到成功救人 / 人工订正留痕·原因往返（2026-11 task-1 C1+C2）

背景（现场）：「添加到本地」十来分钟没有真正打印，订单却显示「失败」，且一句话原因都没有。
真实路径在 check_printing_timeout：客户端 accept（写 accepted + locked_at）之后，用户没点
开始打印 → 该 job 不在客户端的保活集合里 → locked_at 不再被 print_keepalive 刷新 →
3 分钟（PRINT_FEEDBACK_TIMEOUT）后被判 failed。断线未知超 30 分钟也是同样一刀切 failed。

契约（本脚本逐条验）：
  ① "timeout" 是**终态**但不是 NO_LONGER_PRINTABLE —— 设备迟到的 print_success 仍能救回 sent；
     MANUAL_FIX_STATUSES 保持不变（人工订正仍只允许 sent/failed/abandoned/canceled/rejected）；
  ② aggregate_order_status：timeout 压过 sent（部分超时+部分完成 ≠ 整单完成），取消仍最优先；
  ③ check_printing_timeout 三条路径：accepted 超 3 分钟 / offline_unknown 超 30 分钟 → timeout
     + 明确 reject_reason；printing 未 accept 不判失败（只清 pushed_tasks 计时）；
  ④ 迟到 print_success 把 timeout 救回 sent：清 reject_reason、清 manual_status_by/at、
     父订单聚合回到 sent；**别的设备**回报救不回（归属 fail-closed）；
  ⑤ /api/admin/order_status 的 by 字段往返（≤32 字符、去空白、非字符串不炸、缺省 ''），
     订单详情与收益明细都返回订单级 status_reason + manual_status_by/at。

没有 config.py 也能跑（FLASK_ENV=development），全程在临时副本里建库，不碰生产库。
用法（本机只有 Python312）：
  PYTHONPATH= PYTHONIOENCODING=utf-8 \
    C:/Users/Administrator/AppData/Local/Programs/Python/Python312/python.exe \
    tests/verify_timeout_status.py
exit 0 = 全绿。
"""
import io
import os
import shutil
import sys
import tempfile
import traceback
from datetime import datetime, timedelta

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(REPO, "mobile_apps", "printer-backend")

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append(bool(cond))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


def section(title):
    print(f"\n=== {title} ===")


TMP = tempfile.mkdtemp(prefix="hn_timeout_status_")
DST = os.path.join(TMP, "printer-backend")
shutil.copytree(SRC, DST, ignore=shutil.ignore_patterns(
    "__pycache__", "config.py", "config.py.example", "orders.db", "users.db",
    "uploads", "logs", "*.db-wal", "*.db-shm", "venv"))

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

# ==================== 桩环境 ====================
CID = "DESKTOP-TIMEOUT-1a2b"
CID_OTHER = "DESKTOP-TIMEOUT-9z9z"
OWNER_OID = "openid-timeout-user"
ROLES = {"openid-admin": "admin", OWNER_OID: "user"}
backend.PRINTER_TOKEN = "test-token"
backend.SUPER_ADMIN_OPENID = "openid-super"
backend.compute_role = lambda oid: ROLES.get(oid, "guest")
OWNER_TOKEN = backend.token_serializer.dumps(OWNER_OID)

backend.register_device(CID, "DESKTOP-TIMEOUT")
backend.register_device(CID_OTHER, "DESKTOP-TIMEOUT-OTHER")
backend.set_device_manual_capability(CID, True, "测试")
backend.set_device_manual_capability(CID_OTHER, True, "测试")
backend.get_claiming_devices = lambda: {
    CID: {"claimed_at": "", "owner_name": "张三"},
    CID_OTHER: {"claimed_at": "", "owner_name": "李四"},
}
backend.get_claiming_device_ids = lambda: [CID, CID_OTHER]

client = backend.app.test_client()
PRINTER_HDR = {"X-Printer-Token": "test-token"}
APP_SRC = open(os.path.join(DST, "app.py"), "r", encoding="utf-8").read()


def ts_minutes_ago(minutes):
    return (datetime.now() - timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S")


NUM = [0]


def mk_order(status="accepted", files=None, openid=OWNER_OID, created_at=None,
             number=None, operator=CID):
    """files = [(status, locked_at, reject_reason), ...]；返回 (order_id, [subtask_id...])"""
    NUM[0] += 1
    number = number or f"HNTIMEOUT-{NUM[0]:04d}"
    created_at = created_at or "2026-11-03 10:00:00"
    conn = backend.get_db()
    cur = conn.execute(
        "INSERT INTO orders (file, copies, status, created_at, openid, duplex, page_count,"
        " total_price, order_number, source, owner_name, is_admin_print)"
        " VALUES ('f.pdf', 1, ?, ?, ?, 'on', 2, 0.6, ?, 'wechat', '', 0)",
        (status, created_at, openid, number))
    oid = cur.lastrowid
    tids = []
    for st, locked, reason in (files or [(status, "", "")]):
        cur = conn.execute(
            "INSERT INTO order_files (order_id, file_id, file_name, copies, page_count,"
            " total_price, status, duplex, created_at, page_range, locked_at,"
            " reject_reason, operator_client, last_operator_client)"
            " VALUES (?, '', 'f.pdf', 1, 2, 0.6, ?, 'on', ?, '', ?, ?, ?, ?)",
            (oid, st, created_at, locked, reason, operator, operator))
        tids.append(cur.lastrowid)
    conn.commit()
    conn.close()
    return oid, tids


def read_order(oid):
    conn = backend.get_db()
    o = conn.execute("SELECT status, manual_status_by, manual_status_at FROM orders"
                     " WHERE id = ?", (oid,)).fetchone()
    fs = conn.execute("SELECT id, status, locked_at, reject_reason FROM order_files"
                      " WHERE order_id = ? ORDER BY id", (oid,)).fetchall()
    conn.close()
    return (dict(o) if o else {}, [dict(f) for f in fs])


# ==================== ① 常量与集合契约 ====================
section("① timeout 常量 / 集合归属（终态但可被设备救回）")
check("TIMEOUT_STATUS == 'timeout'", backend.TIMEOUT_STATUS == "timeout",
      repr(backend.TIMEOUT_STATUS))
check("_STATUS_PRIORITY 有 timeout，且与 failed 同级",
      backend._STATUS_PRIORITY.get("timeout") == backend._STATUS_PRIORITY.get("failed") == 9,
      repr(backend._STATUS_PRIORITY.get("timeout")))
check("timeout **不**在 NO_LONGER_PRINTABLE_STATUSES（设备迟到成功仍可救回）",
      "timeout" not in backend.NO_LONGER_PRINTABLE_STATUSES,
      repr(backend.NO_LONGER_PRINTABLE_STATUSES))
check("MANUAL_FIX_STATUSES 未被放宽（人工订正清单前后端同一份，不含 timeout）",
      tuple(backend.MANUAL_FIX_STATUSES) == ("sent", "failed", "abandoned", "canceled",
                                             "rejected"),
      repr(backend.MANUAL_FIX_STATUSES))
check("排队淘汰口径已改 timeout（不再冒充 failed）",
      "status = ?" in APP_SRC and "TIMEOUT_STATUS" in APP_SRC)

# ==================== ② 聚合 ====================
section("② aggregate_order_status 的 timeout 语义")
oid, _ = mk_order("sent", [("sent", "", ""), ("sent", "", "")])
conn = backend.get_db()
check("全 sent → sent", backend.aggregate_order_status(conn, oid) == "sent")
conn.close()

oid, _ = mk_order("timeout", [("timeout", "", "x"), ("sent", "", "")])
conn = backend.get_db()
check("超时 + 完成 → timeout（部分超时 ≠ 整单完成）",
      backend.aggregate_order_status(conn, oid) == "timeout",
      str(backend.aggregate_order_status(conn, oid)))
conn.close()

oid, _ = mk_order("canceled", [("canceled", "", ""), ("timeout", "", "x")])
conn = backend.get_db()
check("取消仍最优先（canceled + timeout → canceled）",
      backend.aggregate_order_status(conn, oid) == "canceled")
conn.close()

oid, _ = mk_order("failed", [("timeout", "", "x"), ("failed", "", "y")])
conn = backend.get_db()
check("timeout + failed → failed（两者同级，失败更明确）",
      backend.aggregate_order_status(conn, oid) == "failed",
      str(backend.aggregate_order_status(conn, oid)))
conn.close()

oid, _ = mk_order("queued", [("timeout", "", "x"), ("queued", "", "")])
conn = backend.get_db()
check("timeout + queued → queued（还有文件在排队，整单没结束）",
      backend.aggregate_order_status(conn, oid) == "queued",
      str(backend.aggregate_order_status(conn, oid)))
conn.close()

# ==================== ③ check_printing_timeout 三条路径 ====================
section("③ check_printing_timeout：超时一律 timeout + 明确原因")
oid_a, tids_a = mk_order("accepted", [("accepted", ts_minutes_ago(4), "")])
oid_f, tids_f = mk_order("accepted", [("accepted", ts_minutes_ago(0.5), "")])
oid_o, tids_o = mk_order("offline_unknown", [("offline_unknown", ts_minutes_ago(31), "")])
oid_p, tids_p = mk_order("printing", [("printing", ts_minutes_ago(11), "")])
tid_p = tids_p[0]
with backend.pushed_tasks_lock:
    backend.pushed_tasks[tid_p] = {"pushed_at": datetime.now() - timedelta(minutes=11),
                                   "client_id": CID}

backend.check_printing_timeout()

o_a, f_a = read_order(oid_a)
check("A. accepted 超 3 分钟 → 子任务 timeout", f_a[0]["status"] == "timeout",
      str(f_a[0]))
check("A. 原因写明「3 分钟内未开始打印/未回报结果」",
      f_a[0]["reject_reason"] == "打印机已确认，但 3 分钟内未开始打印/未回报结果，已超时",
      repr(f_a[0]["reject_reason"]))
check("A. 父订单聚合 status 也落成 timeout", o_a.get("status") == "timeout",
      str(o_a.get("status")))
check("A. locked_at 已清空（不再卡在反馈窗口）", (f_a[0]["locked_at"] or "") == "",
      repr(f_a[0]["locked_at"]))

o_f, f_f = read_order(oid_f)
check("B. accepted 仅 30 秒 → 仍在反馈窗口，保持 accepted 且无原因",
      f_f[0]["status"] == "accepted" and (f_f[0]["reject_reason"] or "") == "",
      str(f_f[0]))

o_o, f_o = read_order(oid_o)
check("C. offline_unknown 超 30 分钟 → timeout",
      f_o[0]["status"] == "timeout", str(f_o[0]))
check("C. 原因写明「断线、结果未知、已超时」",
      f_o[0]["reject_reason"] == "打印机断线，打印结果未知，已超时",
      repr(f_o[0]["reject_reason"]))

o_p, f_p = read_order(oid_p)
check("D. printing（未 accept）超 10 分钟 → **不判失败**，仍 printing",
      f_p[0]["status"] == "printing", str(f_p[0]))
with backend.pushed_tasks_lock:
    still = tid_p in backend.pushed_tasks
check("D. 只清掉 pushed_tasks 计时条目（不再累积）", still is False, str(still))

# ==================== ④ 迟到成功救人 ====================
section("④ 设备迟到 print_success：timeout → sent（设备是最终裁决者）")


def connect(cid, name):
    return backend.socketio.test_client(
        backend.app,
        query_string=f"?token=test-token&client_id={cid}&device_name={name}")


oid, tids = mk_order("timeout", [("timeout", "", "打印机断线，打印结果未知，已超时")])
tid = tids[0]
conn = backend.get_db()
conn.execute("UPDATE orders SET manual_status_by = '霍楠', manual_status_at ="
             " '2026-11-03 10:30:00' WHERE id = ?", (oid,))
conn.commit()
conn.close()

ws = connect(CID, "DESKTOP-TIMEOUT")
check("设备连接成功（socketio test client）", ws.is_connected())
ws.emit("print_success", {"task_id": tid, "copies": 1, "duplex": "on",
                          "page_range": "", "page_count": 2})
o, f = read_order(oid)
check("E. 子任务 timeout → sent（迟到的成功被接受）", f[0]["status"] == "sent", str(f[0]))
check("E. 救回时清掉 reject_reason（旧文案会误导用户）",
      (f[0]["reject_reason"] or "") == "", repr(f[0]["reject_reason"]))
check("E. 救回时清掉人工订正留痕（manual_status_by/at 置空）",
      (o.get("manual_status_by") or "") == "" and (o.get("manual_status_at") or "") == "",
      f"{o.get('manual_status_by')!r}/{o.get('manual_status_at')!r}")
check("E. 父订单聚合回到 sent", o.get("status") == "sent", str(o.get("status")))
ws.disconnect()

# 归属校验：别的设备回报救不回
oid2, tids2 = mk_order("timeout", [("timeout", "", "打印机已确认，但 3 分钟内未开始打印/未回报结果，已超时")])
ws2 = connect(CID_OTHER, "DESKTOP-TIMEOUT-OTHER")
ws2.emit("print_success", {"task_id": tids2[0], "copies": 1, "duplex": "on",
                           "page_range": "", "page_count": 2})
o2, f2 = read_order(oid2)
check("F. 非归属设备回报 → 仍是 timeout（fail-closed，不能替别人救单）",
      f2[0]["status"] == "timeout", str(f2[0]))
ws2.disconnect()

# ==================== ⑤ 人工订正 by 往返 + 原因回传 ====================
section("⑤ /api/admin/order_status 的 by 往返 + 订单级 status_reason")


def fix_order(oid, status="failed", by=None, extra=None):
    body = {"order_id": oid, "status": status}
    if by is not None:
        body["by"] = by
    body.update(extra or {})
    return client.post("/api/admin/order_status", headers=PRINTER_HDR, json=body)


oid3, tids3 = mk_order("sent", [("sent", "", "")], number="HNTIMEOUT-FIX3")
r = fix_order(oid3, "failed", by="  霍楠  ")
j = r.get_json() or {}
o3, f3 = read_order(oid3)
check("G. 订正成功且 by 去空白后落库",
      r.status_code == 200 and o3.get("manual_status_by") == "霍楠",
      f"HTTP {r.status_code} body={j} db={o3.get('manual_status_by')!r}")
check("G. manual_status_at 是服务器当前时间（YYYY-MM-DD HH:MM:SS）",
      len(o3.get("manual_status_at") or "") == 19
      and (o3.get("manual_status_at") or "")[4] == "-", repr(o3.get("manual_status_at")))
check("G. 回执里带 manual_status_by / manual_status_at",
      j.get("manual_status_by") == "霍楠" and bool(j.get("manual_status_at")), str(j))
check("G. 子任务也订正为 failed", f3[0]["status"] == "failed", str(f3[0]))

oid4, _ = mk_order("sent", [("sent", "", "")], number="HNTIMEOUT-FIX4")
r = fix_order(oid4, "failed", by="X" * 40)
o4, _ = read_order(oid4)
check("H. by 超长被截到 32 字符", len(o4.get("manual_status_by") or "") == 32,
      f"len={len(o4.get('manual_status_by') or '')}")

oid5, _ = mk_order("sent", [("sent", "", "")], number="HNTIMEOUT-FIX5")
r = fix_order(oid5, "failed", by=12345)
o5, _ = read_order(oid5)
check("I. by 非字符串 → 不 500，记 ''（不编造操作者）",
      r.status_code == 200 and (o5.get("manual_status_by") or "") == "",
      f"HTTP {r.status_code} db={o5.get('manual_status_by')!r}")

oid6, _ = mk_order("sent", [("sent", "", "")], number="HNTIMEOUT-FIX6")
r = fix_order(oid6, "failed")
o6, _ = read_order(oid6)
check("J. 不带 by（老前端）→ 仍 200，manual_status_by=''",
      r.status_code == 200 and (o6.get("manual_status_by") or "") == "",
      f"HTTP {r.status_code} db={o6.get('manual_status_by')!r}")

# 订单详情：订单级 status_reason + manual_status_*
r = client.get(f"/api/order/{oid3}", headers={"Authorization": f"Bearer {OWNER_TOKEN}"})
det = r.get_json() or {}
d = det.get("order") if isinstance(det.get("order"), dict) else det
check("K. /api/order/<id> 200", r.status_code == 200, f"HTTP {r.status_code} {det}")
check("K. 详情返回 manual_status_by/at",
      d.get("manual_status_by") == "霍楠" and bool(d.get("manual_status_at")),
      f"{d.get('manual_status_by')!r}/{d.get('manual_status_at')!r}")
check("K. 详情返回订单级 status_reason 字段（无原因时为 ''，不是 undefined）",
      "status_reason" in d, list(d.keys())[:12])

# 有原因的单：详情 + 收益明细都要把这个原因透出
oid7, _ = mk_order("timeout", [("timeout", "", "打印机断线，打印结果未知，已超时")],
                   created_at="2026-11-03 09:00:00", number="HNTIMEOUT-REASON7")
r = client.get(f"/api/order/{oid7}", headers={"Authorization": f"Bearer {OWNER_TOKEN}"})
det = r.get_json() or {}
d7 = det.get("order") if isinstance(det.get("order"), dict) else det
check("L. 详情 status_reason = 子任务第一条非空 reject_reason",
      d7.get("status_reason") == "打印机断线，打印结果未知，已超时",
      repr(d7.get("status_reason")))

r = client.get("/api/admin/statistics/revenue?token=test-token"
               "&start_date=2026-11-01&end_date=2026-11-30")
rev = r.get_json() or {}
rows = ((rev.get("data") or {}).get("orders")
        or rev.get("orders") or [])
hit = next((x for x in rows if x.get("order_id") == oid7 or x.get("id") == oid7), None)
check("M. 收益明细包含该单", hit is not None, f"HTTP {r.status_code} n={len(rows)}")
check("M. 收益明细返回 status_reason（超时原因在结算页可见）",
      bool(hit) and (hit.get("status_reason") == "打印机断线，打印结果未知，已超时"),
      repr((hit or {}).get("status_reason")))
check("M. 收益明细返回 manual_status_by/at 两个字段（可为空串）",
      bool(hit) and "manual_status_by" in hit and "manual_status_at" in hit,
      str(sorted((hit or {}).keys())[:16]))

# ==================== 汇总 ====================
ok = all(RESULTS)
print("\n" + "=" * 68)
print(f"超时/人工订正验收：{sum(RESULTS)}/{len(RESULTS)} 条通过" + (" ✅" if ok else " ❌"))
sys.exit(0 if ok else 1)
