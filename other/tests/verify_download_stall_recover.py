# -*- coding: utf-8 -*-
"""`downloading` 子树不能只按「设备在线」判活（🔴14 / 批次 D）

问题：`recover_stale_downloading()` 只看 `operator_client not in active`。而 push 时写过
`locked_at`，**全文件没人读它** —— 于是「设备连着网、文件永远传不完」会把子任务挂在
`downloading` 到天荒地老：顾客的预约单既不打印也不失败，零日志、无入口。
反过来，只按在线判也无法处理「设备在线但下载早就断了」这种最常见的情况。

修法：加时间维度 —— `locked_at` 超过 `DOWNLOAD_STALL_TIMEOUT`（30 分钟）没刷新 ⇒ 回退
`scheduled`（重下是幂等的，不重复出纸），并清 `operator_client` / `locked_at`，
让 process_scheduled_orders 重新下发。在线且 `locked_at` 新鲜（哪怕文件很大）一律不碰。

用法: python other/tests/verify_download_stall_recover.py   （exit 0 = 全部通过）
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
APP_PY = os.path.join(SRC, "app.py")

ok_all = True


def check(name, cond, detail=""):
    global ok_all
    if not cond:
        ok_all = False
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


tmp = tempfile.mkdtemp(prefix="hn_dl_stall_")
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
ONLINE = "dev-online-1"
OFFLINE = "dev-gone-9"


def ts(minutes_ago):
    return (NOW - timedelta(minutes=minutes_ago)).replace(microsecond=0).strftime("%Y-%m-%d %H:%M:%S")


# 只把 ONLINE 报成在线心跳
backend.printer_clients.clear()
backend.printer_clients[ONLINE] = {"sid": "sid-online", "heartbeat": NOW}


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


def make(order_client, locked_min_ago):
    oid = insert("orders", file="a.docx", copies=1, status="scheduled", created_at=ts(120),
                 page_count=2, total_price=0.4, schedule_mode="at", scheduled_at=ts(30),
                 schedule_frozen=0, schedule_frozen_at="")
    of = insert("order_files", order_id=oid, file_name="a.docx", copies=1, page_count=2,
                status="downloading", created_at=ts(120), duplex="on",
                operator_client=order_client,
                locked_at=ts(locked_min_ago) if locked_min_ago is not None else "")
    return oid, of


def read(of_id):
    conn = backend.get_db()
    r = conn.execute("SELECT status, COALESCE(operator_client,'') AS oc, "
                     "COALESCE(locked_at,'') AS lk FROM order_files WHERE id = ?",
                     (of_id,)).fetchone()
    conn.close()
    return r


# ══════════════════════════════════════════════════════════════════════════
print("\n=== 0. 常量存在（30 分钟，可调）===")
_src = open(APP_PY, encoding="utf-8", errors="replace").read()
check("0-a DOWNLOAD_STALL_TIMEOUT 已定义", "DOWNLOAD_STALL_TIMEOUT = " in _src)

print("\n=== 1. 在线但 locked_at 是 2 小时前 ⇒ 回退 scheduled 重下 ===")
O1, F1 = make(ONLINE, 120)
backend.recover_stale_downloading()
_r1 = read(F1)
check("1-a 回退成 scheduled", _r1["status"] == "scheduled", _r1["status"])
check("1-b 清掉 operator_client", _r1["oc"] == "", _r1["oc"])
check("1-c 清掉 locked_at（重新计时）", _r1["lk"] == "", _r1["lk"])

print("\n=== 2. 在线且 locked_at 新鲜（大文件正常下载中）⇒ 不许动 ===")
O2, F2 = make(ONLINE, 5)
backend.recover_stale_downloading()
_r2 = read(F2)
check("2-a 仍 downloading", _r2["status"] == "downloading", _r2["status"])
check("2-b operator_client 保留", _r2["oc"] == ONLINE, _r2["oc"])

print("\n=== 3. 设备已离线 ⇒ 照旧回退（零回归）===")
O3, F3 = make(OFFLINE, 2)
backend.recover_stale_downloading()
_r3 = read(F3)
check("3-a 回退成 scheduled", _r3["status"] == "scheduled", _r3["status"])
check("3-b operator_client 清空", _r3["oc"] == "", _r3["oc"])

print("\n=== 4. 没有 locked_at（旧数据 / 还没真正开始拉）⇒ 不按时间误杀 ===")
O4, F4 = make(ONLINE, None)
backend.recover_stale_downloading()
_r4 = read(F4)
check("4-a 仍 downloading（在线且无计时基准）", _r4["status"] == "downloading", _r4["status"])

print("\n=== 5. 状态守卫：SELECT 与 UPDATE 之间被取消的子任务不许被翻回 scheduled ===")
O5, F5 = make(ONLINE, 120)
_conn = backend.get_db()
_conn.execute("UPDATE order_files SET status = 'canceled' WHERE id = ?", (F5,))
_conn.commit()
_conn.close()
backend.recover_stale_downloading()
_r5 = read(F5)
check("5-a 已取消的仍 canceled（竞态守卫）", _r5["status"] == "canceled", _r5["status"])

print("\n=== 6. 静态：判定同时看在线与 locked_at，且回退时清 locked_at ===")
check("6-a SELECT 读了 locked_at", "operator_client, locked_at FROM order_files WHERE status = 'downloading'" in _src)
check("6-b 回退时清 operator_client 与 locked_at",
      "status = 'scheduled', operator_client = '', locked_at = ''" in _src)
check("6-c 有「在线但没进度」的独立日志",
      "分钟没有任何进度" in _src)

print("\n" + "=" * 60)
if ok_all:
    print("下载卡死回收验收: 全部通过")
    sys.exit(0)
print("下载卡死回收验收: 存在失败 ❌")
sys.exit(1)
