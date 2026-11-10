# -*- coding: utf-8 -*-
"""预约单「到点后长期没人打」必须超时取消、释放文件（🔴12 / 批次 A-4）

问题：`expire_stale_queued_orders()` 只扫 `queued`，而 `cleanup_expired_files()` 把
`scheduled` / `waiting` 也算「活跃订单」⇒ 预约单到点后目标设备一直不出现时，这一单
既不打、也不清理、也不会超时：文件永久钉在服务器上（现场 HN20261110-0005 就是这类）。

修法：新增 `expire_stale_scheduled_orders()`，每分钟级别的定时任务里每 10 分钟扫一次：
  · 只动 `scheduled` / `waiting`（还没开始打的）；
  · 计时基准是 `orders.scheduled_at`（**不是** created_at —— 提前几天下一周后的预约是正常用法），
    为空才退回子任务 created_at；
  · 标 `timeout` 而不是 `failed`（结算页显示「超时」），写清楚 reject_reason；
  · 父订单状态按 `refresh_order_status()` 聚合；
  · 已取消/已放弃/已打回的订单不碰。

用法: python other/tests/verify_scheduled_expire_stale.py   （exit 0 = 全部通过）
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


tmp = tempfile.mkdtemp(prefix="hn_expire_sched_")
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

NOW = datetime.now()


def ts(delta_hours):
    return (NOW + timedelta(hours=delta_hours)).replace(microsecond=0).strftime("%Y-%m-%d %H:%M:%S")


def table_cols(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def insert(table, **values):
    conn = backend.get_db()
    allowed = table_cols(conn, table)
    data = {k: v for k, v in values.items() if k in allowed}
    cur = conn.execute(
        f"INSERT INTO {table} ({', '.join(data)}) VALUES ({', '.join('?' for _ in data)})",
        tuple(data.values()))
    conn.commit()
    rid = cur.lastrowid
    conn.close()
    return rid


def make_order(sched_offset_h=-30, of_status="scheduled", mode="at",
               parent_status="", of_created_h=None, scheduled_at=None,
               target=""):
    _sched = ts(sched_offset_h) if scheduled_at is None else scheduled_at
    oid = insert("orders", file="a.docx", copies=1, status=parent_status or of_status,
                 created_at=ts(-40), page_count=2, total_price=0.4,
                 schedule_mode=mode, scheduled_at=_sched, schedule_frozen=0,
                 target_client=target)
    of = insert("order_files", order_id=oid, file_name="a.docx", copies=1,
                page_count=2, status=of_status,
                created_at=ts(of_created_h) if of_created_h is not None else ts(-40),
                duplex="on")
    return oid, of


def read(of_id):
    conn = backend.get_db()
    row = conn.execute("SELECT o.status AS ost, of.status AS ofst, "
                       "COALESCE(of.reject_reason,'') AS rr "
                       "FROM order_files of JOIN orders o ON of.order_id=o.id "
                       "WHERE of.id=?", (of_id,)).fetchone()
    conn.close()
    return row["ost"], row["ofst"], row["rr"]


# ══════════════════════════════════════════════════════════════════════════
print("\n=== 1. 到点 30 小时没打 ⇒ 标 timeout + 写清原因 ===")
O1, F1 = make_order(sched_offset_h=-30, of_status="scheduled")
backend.expire_stale_scheduled_orders()
_ost, _ofst, _rr = read(F1)
check("1-a 子任务变成 timeout", _ofst == "timeout", _ofst)
check("1-b 原因写明「预约到点后…仍未打印」", "预约到点后" in _rr, _rr)
check("1-c 父订单聚合为 timeout", _ost == "timeout", _ost)

print("\n=== 2. waiting 一样要清（现场 HN20261110-0005 的形态）===")
O2, F2 = make_order(sched_offset_h=-30, of_status="waiting")
backend.expire_stale_scheduled_orders()
_ost2, _ofst2, _rr2 = read(F2)
check("2-a waiting → timeout", _ofst2 == "timeout", _ofst2)
check("2-b 原因写明", "预约到点后" in _rr2, _rr2)
check("2-c 父订单 timeout", _ost2 == "timeout", _ost2)

print("\n=== 3. 还没到点的不许动（提前下单是正常用法）===")
O3, F3 = make_order(sched_offset_h=+48, of_status="scheduled")
backend.expire_stale_scheduled_orders()
_ost3, _ofst3, _rr3 = read(F3)
check("3-a 48 小时后的预约仍是 scheduled", _ofst3 == "scheduled", _ofst3)
check("3-b 父订单没被改成 timeout", _ost3 != "timeout", _ost3)

print("\n=== 4. 订单很老但 scheduled_at 在未来 ⇒ 仍不动（计时基准必须是到点时间）===")
O4, F4 = make_order(sched_offset_h=+24, of_status="scheduled", of_created_h=-200)
backend.expire_stale_scheduled_orders()
_ost4, _ofst4, _rr4 = read(F4)
check("4-a 仍是 scheduled（没按 created_at 判）", _ofst4 == "scheduled", _ofst4)

print("\n=== 5. scheduled_at 为空 ⇒ 退回子任务 created_at ===")
O5, F5 = make_order(of_status="scheduled", of_created_h=-40, scheduled_at="")
backend.expire_stale_scheduled_orders()
_ost5, _ofst5, _rr5 = read(F5)
check("5-a 空 scheduled_at + 40 小时前创建 ⇒ timeout", _ofst5 == "timeout", _ofst5)

print("\n=== 6. 已取消 / 已放弃 / 已打回的订单不碰 ===")
_keep = []
for _st in ("canceled", "abandoned", "rejected"):
    _o, _f = make_order(sched_offset_h=-30, of_status="scheduled", parent_status=_st)
    _keep.append((_st, _f))
backend.expire_stale_scheduled_orders()
for _st, _f in _keep:
    _ost6, _ofst6, _ = read(_f)
    check(f"6-{_st} 子任务仍 scheduled（父单终态，不越权改）", _ofst6 == "scheduled", _ofst6)

print("\n=== 7. schedule_mode='now' 的单不归这个函数管 ===")
O7, F7 = make_order(sched_offset_h=-30, of_status="scheduled", mode="now")
backend.expire_stale_scheduled_orders()
_ost7, _ofst7, _ = read(F7)
check("7-a now 模式不动", _ofst7 == "scheduled", _ofst7)

print("\n=== 8. queued 仍归 expire_stale_queued_orders 管（本函数不越界）===")
O8, F8 = make_order(sched_offset_h=-30, of_status="queued")
backend.expire_stale_scheduled_orders()
_ost8, _ofst8, _ = read(F8)
check("8-a 本函数不碰 queued", _ofst8 == "queued", _ofst8)

print("\n=== 9. 一单里还有已打完的：父订单按聚合显示 timeout ===")
O9 = insert("orders", file="b.docx", copies=1, status="waiting",
            created_at=ts(-40), page_count=2, total_price=0.4,
            schedule_mode="at", scheduled_at=ts(-30), schedule_frozen=0)
F9a = insert("order_files", order_id=O9, file_name="b1.docx", copies=1, page_count=2,
             status="sent", created_at=ts(-40), duplex="on")
F9b = insert("order_files", order_id=O9, file_name="b2.docx", copies=1, page_count=2,
             status="waiting", created_at=ts(-40), duplex="on")
backend.expire_stale_scheduled_orders()
_ost9, _ofst9, _ = read(F9b)
check("9-a 卡住的那份变 timeout", _ofst9 == "timeout", _ofst9)
check("9-b 已打完的那份不动", read(F9a)[1] == "sent", read(F9a)[1])
check("9-c 父订单显示 timeout（不能给顾客看「已完成」）", _ost9 == "timeout", _ost9)

print("\n=== 10. 静态接线：定时任务已注册 ===")
_src = open(os.path.join(dst, "app.py"), encoding="utf-8").read()
check("10-a 函数存在", "def expire_stale_scheduled_orders" in _src)
_sched = _src.split('if __name__ == "__main__":', 1)[1]
check("10-b scheduler 里注册了它",
      "expire_stale_scheduled_orders" in _sched and "expire_stale_scheduled" in _sched)
check("10-c 用 scheduled_at 而不是 created_at 当基准",
      "COALESCE(NULLIF(o.scheduled_at, ''), of.created_at)" in _src)
check("10-d 标的是 timeout 不是 failed",
      "TIMEOUT_STATUS" in _src.split("def expire_stale_scheduled_orders")[1].split("def ")[0])

print("\n" + ("全部通过 ✅" if ok_all else "存在失败 ❌"))
sys.exit(0 if ok_all else 1)
