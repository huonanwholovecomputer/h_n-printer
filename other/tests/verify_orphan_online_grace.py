# -*- coding: utf-8 -*-
"""孤儿 printing 任务回收：设备仍在线时不许回退重推（重复出纸）验收

🔴-A5（2026-11-10 健壮性体检）：
  后端 `recover_orphaned_printing_tasks()` 每 2 分钟扫一次 `printing` 且 `locked_at` 超过
  5 分钟的子任务，直接回退 queued 让别的打印机重试。判据里**只有时间，没有设备在线状态**。
  而现场一次手动双面 / 攒批整叠第 2 趟动辄十分钟（第 1 趟打完要等人把纸叠放回纸盘）：
  只要保活断了几拍（网络抖动、切网、客户端卡在 COM 转换），这个函数就会把「纸已经出到
  出纸槽、第 2 趟还没开始」的任务回退 queued → 另一台设备/下一次接单又打一遍 →
  **同一份文件出两次纸**，订单状态来回翻转（sent→queued→printing→…）。

修法（本脚本钉住）：
  · 设备仍在线（`operator_client` 在 `get_active_clients()` 里）**且** `locked_at` 还在
    `_ORPHAN_ONLINE_GRACE_MIN`（30 分钟）硬上限内 → 本轮不回退（`[ORPHAN] … 设备仍在线`
    一行日志），交给下一轮再看；
  · 设备离线 / `operator_client` 为空的历史数据 → 照旧 5 分钟回退（真孤儿）；
  · 在线但超过硬上限（真卡死，纸不可能还在出）→ 回退 queued；
  · 文件已被清理（path 不存在）→ 立刻 failed（没有重出纸风险，早失败早通知）。

用法: python other/tests/verify_orphan_online_grace.py   （exit 0 = 全部通过）
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


tmp = tempfile.mkdtemp(prefix="hn_orphan_grace_")
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

ONLINE = "R7000-AHP10-online"
OFFLINE = "DESKTOP-offline"

backend.printer_clients[ONLINE] = {"sid": "sid-online", "heartbeat": datetime.now()}
backend.printer_clients[OFFLINE] = {
    "sid": "sid-offline", "heartbeat": datetime.now() - timedelta(hours=2)}

NOW = datetime.now().replace(microsecond=0)
TS = "%Y-%m-%d %H:%M:%S"
T_6MIN = (NOW - timedelta(minutes=6)).strftime(TS)
T_31MIN = (NOW - timedelta(minutes=31)).strftime(TS)


def table_cols(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def insert(table, **values):
    """按数据库真实列裁剪后插入（init_db 的迁移会按版本加减列，不写死列名）。"""
    conn = backend.get_db()
    allowed = table_cols(conn, table)
    data = {k: v for k, v in values.items() if k in allowed}
    cols = ", ".join(data)
    qs = ", ".join("?" for _ in data)
    cur = conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({qs})", tuple(data.values()))
    conn.commit()
    rid = cur.lastrowid
    conn.close()
    return rid


_FILE_SEQ = [0]


def make_file(name, exists=True):
    """造一条 files 记录（可选真的在磁盘上放一个文件）。files.id 是 TEXT 主键，
    所以这里返回的是字符串 id 本身 —— insert() 返回的 lastrowid 对 TEXT 主键表无意义。"""
    _FILE_SEQ[0] += 1
    fid = f"file-{_FILE_SEQ[0]}"
    path = os.path.join(tmp, name)
    if exists:
        with open(path, "wb") as f:
            f.write(b"%PDF-1.4\n% orphan test\n")
    insert("files", id=fid, original_name=name, saved_name=name, path=path, size=21,
           created_at=NOW.strftime(TS))
    return fid


def make_order(name="job.docx"):
    return insert("orders", file=name, copies=1, status="printing",
                  created_at=NOW.strftime(TS), page_count=2, total_price=0.4,
                  schedule_mode="now")


def make_of(order_id, file_id, operator_client, locked_at, status="printing"):
    return insert("order_files", order_id=order_id, file_id=file_id,
                  file_name="job.docx", copies=1, page_count=2, status=status,
                  created_at=NOW.strftime(TS), duplex="on",
                  operator_client=operator_client, locked_at=locked_at)


def of_row(of_id):
    conn = backend.get_db()
    row = conn.execute("SELECT status, COALESCE(operator_client,'') AS oc, "
                       "COALESCE(locked_at,'') AS lk, COALESCE(reject_reason,'') AS rr "
                       "FROM order_files WHERE id=?", (of_id,)).fetchone()
    conn.close()
    return row


# ══════════════════════════════════════════════════════════════════════════
print("\n=== 1. 设备在线 + 锁定 6 分钟 → 不回退（正在打，回退就重复出纸）===")
f1 = make_file("a.pdf")
o1 = make_order()
of1 = make_of(o1, f1, ONLINE, T_6MIN)
backend.recover_orphaned_printing_tasks()
r1 = of_row(of1)
check("①-a 在线设备的在飞任务仍是 printing（没被回退 queued）",
      r1["status"] == "printing", f"status={r1['status']}")
check("①-b 归属设备没被清空（还是那台在打的机器）",
      r1["oc"] == ONLINE, f"operator_client={r1['oc']!r}")
check("①-c 锁定时间没被清（下一轮还能按它判硬上限）",
      r1["lk"] == T_6MIN, f"locked_at={r1['lk']!r}")

# ══════════════════════════════════════════════════════════════════════════
print("\n=== 2. 设备离线 + 锁定 6 分钟 → 照旧回退 queued（真孤儿）===")
f2 = make_file("b.pdf")
o2 = make_order()
of2 = make_of(o2, f2, OFFLINE, T_6MIN)
backend.recover_orphaned_printing_tasks()
r2 = of_row(of2)
check("②-a 离线设备的超时任务回退 queued",
      r2["status"] == "queued", f"status={r2['status']}")
check("②-b 回退时清空归属与锁定（别的设备可以接手）",
      r2["oc"] == "" and r2["lk"] == "", f"oc={r2['oc']!r} lk={r2['lk']!r}")

# ══════════════════════════════════════════════════════════════════════════
print("\n=== 3. 设备在线但锁定超过 30 分钟硬上限 → 判卡死并回退 ===")
f3 = make_file("c.pdf")
o3 = make_order()
of3 = make_of(o3, f3, ONLINE, T_31MIN)
backend.recover_orphaned_printing_tasks()
r3 = of_row(of3)
check("③-a 在线但超硬上限 → 回退 queued（在线不等于永远不动它）",
      r3["status"] == "queued", f"status={r3['status']}")
check("③-b 硬上限常量为 30 分钟（改小会让长作业被误伤，改大则永远不动）",
      int(getattr(backend, "_ORPHAN_ONLINE_GRACE_MIN", 0)) == 30,
      f"{getattr(backend, '_ORPHAN_ONLINE_GRACE_MIN', None)}")

# ══════════════════════════════════════════════════════════════════════════
print("\n=== 4. 历史数据 operator_client 为空 → 按孤儿回退（零回归）===")
f4 = make_file("d.pdf")
o4 = make_order()
of4 = make_of(o4, f4, "", T_6MIN)
backend.recover_orphaned_printing_tasks()
r4 = of_row(of4)
check("④-a 无归属的旧任务照旧回退 queued",
      r4["status"] == "queued", f"status={r4['status']}")

# ══════════════════════════════════════════════════════════════════════════
print("\n=== 5. 文件已被清理 → 立刻 failed（在线也不等，没有重出纸风险）===")
f5 = make_file("e.pdf", exists=False)
o5 = make_order()
of5 = make_of(o5, f5, ONLINE, T_6MIN)
backend.recover_orphaned_printing_tasks()
r5 = of_row(of5)
check("⑤-a 文件不存在 → failed（不是回退 queued）",
      r5["status"] == "failed", f"status={r5['status']}")
check("⑤-b 失败原因说明文件已被清理",
      "清理" in r5["rr"] or "无法打印" in r5["rr"], f"{r5['rr']!r}")

# ══════════════════════════════════════════════════════════════════════════
print("\n=== 6. 锁定时间 4 分钟（没到 5 分钟窗口）→ 一律不动（零回归）===")
f6 = make_file("f.pdf")
o6 = make_order()
of6 = make_of(o6, f6, OFFLINE, (NOW - timedelta(minutes=4)).strftime(TS))
backend.recover_orphaned_printing_tasks()
r6 = of_row(of6)
check("⑥-a 未到 5 分钟窗口 → 保持 printing",
      r6["status"] == "printing", f"status={r6['status']}")

# ══════════════════════════════════════════════════════════════════════════
print("\n=== 7. 静态接线（防止判据被悄悄删掉）===")
src = open(os.path.join(dst, "app.py"), encoding="utf-8", errors="replace").read()
_body = src.split("def recover_orphaned_printing_tasks", 1)[1].split("\ndef ", 1)[0]
check("⑦-a 函数里取了在线设备集合", "get_active_clients()" in _body)
check("⑦-b 在线 + 硬上限内 → 进 kept_ids（不回退）",
      "kept_ids.append" in _body and "_ORPHAN_ONLINE_GRACE_MIN" in _body)
check("⑦-c SELECT 里带了 operator_client 与 locked_at（判据必需）",
      "of.operator_client" in _body and "of.locked_at" in _body)
check("⑦-d 有「设备仍在线、本轮不回退」的日志",
      "[ORPHAN]" in _body and "仍在线" in _body)
check("⑦-e 保留原有 5 分钟窗口与 queued/failed 两条处置",
      "timedelta(minutes=5)" in _body and "'queued'" in _body and "'failed'" in _body)

# ══════════════════════════════════════════════════════════════════════════
print("\n" + "=" * 60)
if ok_all:
    print("全部通过 ✅")
    sys.exit(0)
print("存在失败项 ❌")
sys.exit(1)
