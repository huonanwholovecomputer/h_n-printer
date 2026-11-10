# -*- coding: utf-8 -*-
"""顾客要撤回一单，不能只能找客服（🔴15 / 🟡9 / 批次 D）

问题：`cancel_order` 只允许 `queued/printing/waiting/downloading/scheduled`。而父订单聚合
成 `accepted` / `offline_unknown` / `timeout` 之后，顾客点「取消」得到
「任务状态为 accepted，无法取消」——纸可能根本没出来，人却只能找客服手工改库。

修法：把 `accepted` / `offline_unknown` / `timeout` 纳入可取消范围。口径不变：
  · 取消只是把**还没终结**的子任务置 canceled，并通知设备清队列；
  · `timeout` 是终态但没出纸，一并翻成 canceled（否则顾客点完取消，列表里仍显示「超时」）；
  · `sent`（真打出来了）/ `failed` / `rejected` / `abandoned` 一概不碰；
  · 设备是最终裁决者 —— 迟到的 print_success 仍能把 canceled 覆盖成 sent。

用法: python other/tests/verify_cancel_terminal_orders.py   （exit 0 = 全部通过）
"""
import io
import os
import shutil
import sys
import tempfile
import traceback
from datetime import datetime, timedelta

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SRC = os.path.join(REPO, "mobile_apps", "printer-backend")

ok_all = True


def check(name, cond, detail=""):
    global ok_all
    if not cond:
        ok_all = False
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


tmp = tempfile.mkdtemp(prefix="hn_cancel_term_")
dst = os.path.join(tmp, "printer-backend")
shutil.copytree(SRC, dst, ignore=shutil.ignore_patterns(
    "__pycache__", "orders.db", "users.db", "uploads", "logs", "venv", "*.bak-*"))
sys.path.insert(0, dst)
os.chdir(dst)

try:
    import app as backend
    print(f"imported app.py from {dst}")
except Exception:
    traceback.print_exc()
    sys.exit("无法导入 app.py")

backend.init_db()

OPENID = "openid-cancel-test"
TOKEN = "Bearer " + backend.token_serializer.dumps(OPENID)
client = backend.app.test_client()
NOW = datetime.now()


def ts(minutes_ago=0):
    return (NOW - timedelta(minutes=minutes_ago)).replace(microsecond=0).strftime("%Y-%m-%d %H:%M:%S")


def table_cols(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def insert(table, **values):
    conn = backend.get_db()
    cols = table_cols(conn, table)
    data = {k: v for k, v in values.items() if k in cols}
    cur = conn.execute(
        f"INSERT INTO {table} ({', '.join(data)}) VALUES ({', '.join('?' for _ in data)})",
        tuple(data.values()))
    conn.commit()
    rid = cur.lastrowid
    conn.close()
    return rid


def make(parent_status, of_status, openid=OPENID):
    oid = insert("orders", file="a.docx", copies=1, status=parent_status,
                 created_at=ts(30), openid=openid, page_count=2, total_price=0.4,
                 schedule_mode="now", scheduled_at="", schedule_frozen=0)
    of = insert("order_files", order_id=oid, file_name="a.docx", copies=1, page_count=2,
                status=of_status, created_at=ts(30), duplex="on")
    return oid, of


def read(oid, of_id):
    conn = backend.get_db()
    o = conn.execute("SELECT status FROM orders WHERE id = ?", (oid,)).fetchone()
    f = conn.execute("SELECT status, COALESCE(locked_at,'') AS lk FROM order_files WHERE id = ?",
                     (of_id,)).fetchone()
    conn.close()
    return o[0] if o else "", f


def cancel(oid):
    return client.post("/api/cancel_order", headers={"Authorization": TOKEN},
                       json={"order_id": oid})


# ══════════════════════════════════════════════════════════════════════════
print("\n=== 1. accepted（打印机已确认）也该能取消 ===")
O1, F1 = make("accepted", "accepted")
r1 = cancel(O1)
check("1-a 接口返回 200", r1.status_code == 200, r1.status_code)
check("1-b success=true", bool(r1.get_json().get("success")), r1.get_json())
_p1, _f1 = read(O1, F1)
check("1-c 父订单 canceled", _p1 == "canceled", _p1)
check("1-d 子任务 canceled", _f1["status"] == "canceled", _f1["status"])

print("\n=== 2. offline_unknown（断线未知）也能取消 ===")
O2, F2 = make("offline_unknown", "offline_unknown")
r2 = cancel(O2)
check("2-a 200 + success", r2.status_code == 200 and bool(r2.get_json().get("success")),
      f'{r2.status_code}/{r2.get_json()}')
_p2, _f2 = read(O2, F2)
check("2-b 子任务 canceled", _f2["status"] == "canceled", _f2["status"])

print("\n=== 3. timeout（到了没接单、没出纸）也能取消，并被翻成 canceled ===")
O3, F3 = make("timeout", "timeout")
r3 = cancel(O3)
check("3-a 200 + success", r3.status_code == 200 and bool(r3.get_json().get("success")),
      f'{r3.status_code}/{r3.get_json()}')
_p3, _f3 = read(O3, F3)
check("3-b 父订单 canceled（顾客点了取消就该显示已取消）", _p3 == "canceled", _p3)
check("3-c timeout 子任务翻成 canceled", _f3["status"] == "canceled", _f3["status"])

print("\n=== 4. sent（真打出来了）不许取消 ===")
O4, F4 = make("sent", "sent")
r4 = cancel(O4)
check("4-a 400 拒绝", r4.status_code == 400, r4.status_code)
_p4, _f4 = read(O4, F4)
check("4-b 状态原样（sent 不动）", _p4 == "sent" and _f4["status"] == "sent",
      f'{_p4}/{_f4["status"]}')

print("\n=== 5. 混单：一个 sent + 一个 timeout ⇒ 取消只动 timeout ===")
O5 = insert("orders", file="a.docx", copies=1, status="timeout", created_at=ts(30),
            openid=OPENID, page_count=2, total_price=0.4, schedule_mode="now",
            scheduled_at="", schedule_frozen=0)
F5a = insert("order_files", order_id=O5, file_name="a.docx", copies=1, page_count=2,
             status="sent", created_at=ts(30), duplex="on")
F5b = insert("order_files", order_id=O5, file_name="b.docx", copies=1, page_count=2,
             status="timeout", created_at=ts(30), duplex="on")
r5 = cancel(O5)
check("5-a 200", r5.status_code == 200, r5.status_code)
_p5, _f5a = read(O5, F5a)
_, _f5b = read(O5, F5b)
check("5-b 已打印的那份仍是 sent（事实不能抹）", _f5a["status"] == "sent", _f5a["status"])
check("5-c 超时的那份翻成 canceled", _f5b["status"] == "canceled", _f5b["status"])

print("\n=== 6. 别人的订单不许取消（零回归）===")
O6, F6 = make("accepted", "accepted", openid="someone-else")
r6 = cancel(O6)
check("6-a 403 拒绝", r6.status_code == 403, r6.status_code)
_p6, _f6 = read(O6, F6)
check("6-b 状态原样", _f6["status"] == "accepted", _f6["status"])

print("\n" + "=" * 60)
if ok_all:
    print("取消终态订单验收: 全部通过")
    sys.exit(0)
print("取消终态订单验收: 存在失败 ❌")
sys.exit(1)
