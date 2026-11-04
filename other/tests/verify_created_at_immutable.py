#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""验收 · created_at 不可被重复上报改写（2026-11 task-1 C3）

口径（硬规矩）：
  **created_at = 订单首次生成（首次与服务器交互）时刻，此后不可覆盖。**
  · /api/next_order_number 预留 → 服务器 now；
  · 本地工具离线首同步（/api/local_orders 的 INSERT 分支）→ 允许 payload 里的 created_at；
  · 已存在的行（预留转正式 / 重复上报 / 重打）→ 一律不写 created_at。

为什么重要：等待时长 = print_started_at − created_at、按日期统计、结算区间都挂在它上面。
旧实现把它放在 UPDATE 的 SET 列表里，本地工具每次上报（同一张单可能报两三次：预留、
打印完成、断线重连补报）都会把「下单时间」改写成本次 payload —— 于是同一天的单子
按日期筛选会跳日期，等待时长会变，甚至出现负等待。
（对照：finished_at 允许设备上报覆盖，因为它描述"最后一次真的出纸"的物理事实；
 created_at 是账期归属的锚点，不能被改写。）

用法（本机只有 Python312）：
  PYTHONPATH= PYTHONIOENCODING=utf-8 \
    C:/Users/Administrator/AppData/Local/Programs/Python/Python312/python.exe \
    tests/verify_created_at_immutable.py
exit 0 = 全绿。
"""
import io
import os
import re
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


TMP = tempfile.mkdtemp(prefix="hn_created_at_")
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

CID = "DESKTOP-CREATED-7a7a"
backend.PRINTER_TOKEN = "test-token"
backend.register_device(CID, "DESKTOP-CREATED")
backend.get_claiming_devices = lambda: {CID: {"claimed_at": "", "owner_name": "张三"}}
backend.get_claiming_device_ids = lambda: [CID]
client = backend.app.test_client()

NOW_FMT = "%Y-%m-%d %H:%M:%S"


def db_created_at(order_number=None, order_id=None):
    conn = backend.get_db()
    try:
        if order_id is not None:
            row = conn.execute("SELECT id, order_number, created_at, status, total_price, file "
                               "FROM orders WHERE id = ?", (order_id,)).fetchone()
        else:
            row = conn.execute("SELECT id, order_number, created_at, status, total_price, file "
                               "FROM orders WHERE order_number = ? ORDER BY id DESC LIMIT 1",
                               (order_number,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def nearest_now(ts, tolerance_sec=180):
    try:
        t = datetime.strptime(str(ts), NOW_FMT)
    except (TypeError, ValueError):
        return False
    return abs((datetime.now() - t).total_seconds()) <= tolerance_sec


def local_report(order_number, created_at=None, total_price=1.0, status="sent",
                 file_name="a.pdf", page_count=2, remark=""):
    body = {
        "order_number": order_number,
        "token": "test-token",
        "client_id": CID,
        "total_price": total_price,
        "status": status,
        "remark": remark,
        "owner_name": "张三",
        "is_admin_print": False,
        "files": [{"file_name": file_name, "copies": 1, "page_count": page_count,
                   "cost": total_price, "duplex": "on", "page_range": ""}],
    }
    if created_at is not None:
        body["created_at"] = created_at
    return client.post("/api/local_orders", json=body)


# ==================== ① 预留取号：created_at = 服务器 now ====================
section("① /api/next_order_number 预留单：created_at = 服务器 now（不接受 payload 覆盖）")
r = client.get(f"/api/next_order_number?token=test-token&request_id=req-c3-1"
               f"&created_at=2020-01-01%2000:00:00")
j = r.get_json() or {}
check("取号 200", r.status_code == 200 and j.get("success") is True, f"{r.status_code} {j}")
on1 = j.get("order_number", "")
row1 = db_created_at(on1)
check("占位单已建（status=reserved）", bool(row1) and row1["status"] == "reserved", str(row1))
check("created_at 是服务器 now，不是 payload 里的 2020-01-01",
      nearest_now(row1["created_at"]) and not str(row1["created_at"]).startswith("2020"),
      str(row1["created_at"]))
check("created_at 格式 YYYY-MM-DD HH:MM:SS（19 位）",
      re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", str(row1["created_at"])) is not None,
      str(row1["created_at"]))
reserved_created = row1["created_at"]
cid1 = row1["id"]

# 同一 request_id 再取一次 → 复用同一行，created_at 不许变
r = client.get("/api/next_order_number?token=test-token&request_id=req-c3-1")
j2 = r.get_json() or {}
check("同一 request_id 复用同一订单号（幂等）",
      j2.get("order_number") == on1 and j2.get("reused") is True, str(j2))
row1b = db_created_at(order_id=cid1)
check("复用路径不动 created_at", row1b["created_at"] == reserved_created,
      f"{reserved_created} → {row1b['created_at']}")

def db_files(order_id):
    conn = backend.get_db()
    try:
        return [dict(r) for r in conn.execute(
            "SELECT id, file_name, status, created_at FROM order_files WHERE order_id = ? "
            "ORDER BY id", (order_id,)).fetchall()]
    finally:
        conn.close()


# ==================== ② 预留单 → 正式单：payload 的 created_at 被忽略 ====================
section("② 预留单转正式单（reserved → sent）：payload created_at 被忽略")
r = local_report(on1, created_at="2026-01-05 09:00:00", total_price=2.4,
                 file_name="first.pdf", page_count=4, status="reserved")
j = r.get_json() or {}
check("上报 200 且 success", r.status_code == 200 and j.get("success") is True, f"{r.status_code} {j}")
row1c = db_created_at(order_id=cid1)
check("预留上报后 status=reserved（内容确实被更新了）", row1c["status"] == "reserved", str(row1c))
check("total_price 被更新为 2.4（只丢 created_at，不是整个 UPDATE 失效）",
      abs(float(row1c["total_price"]) - 2.4) < 1e-6, str(row1c["total_price"]))
check("created_at 仍是取号时刻（没被 payload 的 2026-01-05 覆盖）",
      row1c["created_at"] == reserved_created
      and not str(row1c["created_at"]).startswith("2026-01-05"),
      f"{reserved_created} → {row1c['created_at']}")

# 再报一次（reserved 允许覆盖：模拟断线重连/改份数），带另一个 created_at
r = local_report(on1, created_at="2026-09-09 09:09:09", total_price=3.6,
                 file_name="second.pdf", page_count=6, status="reserved")
row1d = db_created_at(order_id=cid1)
check("二次上报（重连补报）created_at 仍不变", row1d["created_at"] == reserved_created,
      f"{reserved_created} → {row1d['created_at']}")
files1 = db_files(cid1)
check("二次上报内容仍能更新（子任务换成 second.pdf、价格 3.6）",
      len(files1) == 1 and files1[0]["file_name"] == "second.pdf"
      and abs(float(row1d["total_price"]) - 3.6) < 1e-6,
      f"{files1} price={row1d['total_price']}")

# 打印完成上报（reserved → sent）
r = local_report(on1, created_at="2026-09-09 10:10:10", total_price=3.6,
                 file_name="second.pdf", page_count=6, status="sent")
row1e = db_created_at(order_id=cid1)
check("完成上报 → status=sent", row1e["status"] == "sent", str(row1e["status"]))
check("完成上报也不动 created_at", row1e["created_at"] == reserved_created,
      f"{reserved_created} → {row1e['created_at']}")

# 已完成的单再上报 → 整单幂等跳过（内容也不再改）
r = local_report(on1, created_at="2026-12-12 12:12:12", total_price=99.0,
                 file_name="after-sent.pdf", page_count=6, status="sent")
j = r.get_json() or {}
row1f = db_created_at(order_id=cid1)
check("已 sent 的单再上报 → 幂等跳过（不建新单、不改内容、不改时间）",
      (j.get("message") or "").startswith("订单已存在")
      and row1f["created_at"] == reserved_created
      and abs(float(row1f["total_price"]) - 3.6) < 1e-6,
      f"{j.get('message')} price={row1f['total_price']}")

# ==================== ③ 离线首同步（新单）：payload created_at 允许生效 ====================
section("③ 离线首同步（INSERT 分支）：payload created_at 允许生效")
ON2 = "HN20260101-TEST1"
r = local_report(ON2, created_at="2026-01-05 09:00:00", total_price=0.8,
                 file_name="offline.pdf", page_count=2, status="reserved")
j = r.get_json() or {}
check("离线首同步 200", r.status_code == 200 and j.get("success") is True, f"{r.status_code} {j}")
row2 = db_created_at(ON2)
check("新插入的订单允许用 payload 的 created_at（离线单的真实下单时间）",
      row2 is not None and row2["created_at"] == "2026-01-05 09:00:00", str(row2))

r = local_report(ON2, created_at="2026-11-03 12:00:00", total_price=0.8,
                 file_name="offline2.pdf", page_count=2, remark="补报", status="reserved")
row2b = db_created_at(ON2)
check("同一离线单再上报 → created_at 不变（INSERT 之后就不许动）",
      row2b["created_at"] == "2026-01-05 09:00:00", str(row2b["created_at"]))
files2 = db_files(row2b["id"])
check("同一离线单再上报 → 子任务被替换（offline2.pdf）",
      len(files2) == 1 and files2[0]["file_name"] == "offline2.pdf", str(files2))

# ==================== ④ 不带 created_at 的上报也不许动 ====================
section("④ 老客户端不带 created_at：既不覆盖也不清空")
ON3 = "HN20260101-TEST2"
local_report(ON3, created_at="2026-02-02 08:08:08", total_price=0.5,
             file_name="old.pdf", status="reserved")
before = db_created_at(ON3)["created_at"]
local_report(ON3, created_at=None, total_price=0.5, file_name="old2.pdf", status="reserved")
after = db_created_at(ON3)["created_at"]
check("老客户端（无 created_at 字段）重复上报 → created_at 不变",
      before == after == "2026-02-02 08:08:08", f"{before} → {after}")

# ==================== ⑤ 静态：全仓没有 UPDATE orders ... created_at ====================
section("⑤ 静态：UPDATE orders 里不允许出现 created_at")
bad = []
for m in re.finditer(r"UPDATE\s+orders\b", APP_SRC, re.I):
    window = APP_SRC[m.start():m.start() + 400]
    window = window.split("WHERE")[0] if "WHERE" in window else window
    if "created_at" in window:
        line = APP_SRC[:m.start()].count("\n") + 1
        bad.append(line)
check("没有任何 UPDATE orders 在 SET 里写 created_at", bad == [],
      f"可疑行号={bad}" if bad else "")
check("local_orders 的 UPDATE 语句明确只更新内容字段（file/total_price/status/source/remark）",
      "UPDATE orders SET file = ?, total_price = ?, status = ?,\n"
      "                                     source = 'local', remark = ?" in APP_SRC
      or bool(re.search(r"UPDATE orders SET file = \?, total_price = \?, status = \?,\s*\n\s*source = 'local', remark = \?", APP_SRC)))
check("函数开头保留了 created_at 口径注释（含「此后不可覆盖」）",
      "此后不可覆盖" in APP_SRC or "不可覆盖" in APP_SRC)
check("注释里写清了与 finished_at 的口径取舍",
      "finished_at" in APP_SRC and "物理事实" in APP_SRC)
inserts_with_ts = [m.start() for m in re.finditer(r"INSERT\s+INTO\s+orders\b", APP_SRC, re.I)
                   if "created_at" in APP_SRC[m.start():m.start() + 400]]
check("INSERT 时仍写 created_at（取号预留 / 云端建单 / 本地首同步）",
      len(inserts_with_ts) >= 2, f"n={len(inserts_with_ts)}")

# ==================== 汇总 ====================
ok = all(RESULTS)
print("\n" + "=" * 68)
print(f"created_at 不可覆盖验收：{sum(RESULTS)}/{len(RESULTS)} 条通过" + (" ✅" if ok else " ❌"))
sys.exit(0 if ok else 1)
