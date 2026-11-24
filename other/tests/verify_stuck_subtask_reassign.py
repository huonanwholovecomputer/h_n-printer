"""verify_stuck_subtask_reassign.py — 改派 CLI 的行为验收（2026-11-24 P1-3 · 只改派）

被测对象：`mobile_apps/printer-backend/ops/stuck_subtasks.py`
（`process_pending_orders` 的 `[WAIT]` 只说明了"卡住了"，这个 CLI 是唯一的出口。）

**只允许改派，不允许作废**：`UPDATE orders SET target_client = ?`，不碰
`order_files.status` / `orders.status` / 任何金额列。作废要动 `MANUAL_FIX_STATUSES`
与结算口径（硬规矩 6、7），口径未定之前不开。

做法：每次场景都新建一个临时 SQLite（列名照生产 `PRAGMA table_info` 抄），
然后把 CLI 当**子进程真跑**，断言输出与库内状态。断言一律用 ASCII 标记
（stdout 属 `log_vars`，带中文会顶高 `verify_guard_quality` 的 A1 基线）。

场景：
  A --list 只列 queued 的，跳过"全终态"的订单，并对混单打标记
  B --reassign-of 指向非 queued 文件 ⇒ 拒绝（exit 2），库不动
  C 不带 --yes ⇒ 只打印 SQL，库不动
  D 带 --yes ⇒ 只改 orders.target_client，order_files.status / orders.status 一个都不动
  E --reassign-from ⇒ 只改"目标为旧设备且有排队文件"的单，跳过全终态单与别的设备
  F --to 为空 ⇒ 拒绝（exit 2）
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
CLI = ROOT / "mobile_apps" / "printer-backend" / "ops" / "stuck_subtasks.py"

fails: list[str] = []
checks = 0


def check(name: str, cond: bool, detail: str = "") -> bool:
    global checks
    checks += 1
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}" + (f"  ← {detail}" if detail else ""))
        fails.append(name)
    return bool(cond)


OLD = "R7000-AHP10-168dbdc3d1"
NEW = "RN-AHPN-NdbdcNdN"
OTHER = "DESKTOP-EJGEB1V-aaaaaaaa"


def make_db() -> str:
    """建一个最小可用的库：列名照生产 orders / order_files 抄。"""
    d = tempfile.mkdtemp(prefix="hn_reassign_")
    p = os.path.join(d, "orders.db")
    conn = sqlite3.connect(p)
    conn.executescript("""
        CREATE TABLE orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_number TEXT, target_client TEXT,
            status TEXT, created_at TEXT
        );
        CREATE TABLE order_files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL, file_name TEXT,
            status TEXT, created_at TEXT
        );
    """)
    t = "2026-11-23 10:00:00"
    # order 1：纯排队单
    conn.execute("INSERT INTO orders(id,order_number,target_client,status,created_at) "
                 "VALUES (1,'HN-1',?,'sent',?)", (OLD, t))
    conn.execute("INSERT INTO order_files(id,order_id,file_name,status,created_at) "
                 "VALUES (101,1,'a.pdf','queued',?)", (t,))
    # order 2：混单（一个 queued + 一个 sent）
    conn.execute("INSERT INTO orders(id,order_number,target_client,status,created_at) "
                 "VALUES (2,'HN-2',?,'sent',?)", (OLD, t))
    conn.execute("INSERT INTO order_files(id,order_id,file_name,status,created_at) "
                 "VALUES (201,2,'b.pdf','queued',?)", (t,))
    conn.execute("INSERT INTO order_files(id,order_id,file_name,status,created_at) "
                 "VALUES (202,2,'c.pdf','sent',?)", (t,))
    # order 3：全终态（没有可派的东西）
    conn.execute("INSERT INTO orders(id,order_number,target_client,status,created_at) "
                 "VALUES (3,'HN-3',?,'sent',?)", (OLD, t))
    conn.execute("INSERT INTO order_files(id,order_id,file_name,status,created_at) "
                 "VALUES (301,3,'d.pdf','sent',?)", (t,))
    # order 4：目标已经是另一台设备
    conn.execute("INSERT INTO orders(id,order_number,target_client,status,created_at) "
                 "VALUES (4,'HN-4',?,'waiting',?)", (OTHER, t))
    conn.execute("INSERT INTO order_files(id,order_id,file_name,status,created_at) "
                 "VALUES (401,4,'e.pdf','queued',?)", (t,))
    conn.commit()
    conn.close()
    return p


def run(db: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(CLI), "--db", db, *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)


def snapshot(db: str) -> dict:
    conn = sqlite3.connect(db)
    out = {
        "targets": {int(r[0]): r[1] for r in conn.execute(
            "SELECT id, target_client FROM orders ORDER BY id")},
        "order_status": {int(r[0]): r[1] for r in conn.execute(
            "SELECT id, status FROM orders ORDER BY id")},
        "file_status": {int(r[0]): r[1] for r in conn.execute(
            "SELECT id, status FROM order_files ORDER BY id")},
    }
    conn.close()
    return out


def main() -> int:
    print("=" * 78)
    print("verify_stuck_subtask_reassign.py — 改派 CLI（只改派）")
    print("=" * 78)

    check("0.0 CLI 文件存在", CLI.is_file(), str(CLI))
    if not CLI.is_file():
        return 1

    # ── A. --list ────────────────────────────────────────────────
    print("\n=== A. --list：只列 queued，混单要标出来 ===")
    db = make_db()
    base = snapshot(db)
    r = run(db, "--list")
    check("A1 exit 0", r.returncode == 0, f"rc={r.returncode} err={r.stderr[:200]}")
    out = r.stdout
    check("A2 列出纯排队单 HN-1", "HN-1" in out)
    check("A3 列出混单 HN-2", "HN-2" in out)
    check("A4 列出别的设备的排队单 HN-4", "HN-4" in out)
    check("A5 不列全终态的 HN-3", "HN-3" not in out, "terminal-only order has nothing to dispatch")
    check("A6 混单有 ASCII 标记 [MIXED]", "[MIXED]" in out, "mixed order must be greppable")
    check("A7 --list 不改库", snapshot(db) == base)

    # ── B. 指向非 queued 文件 ⇒ 拒绝 ────────────────────────────
    print("\n=== B. --reassign-of 指向已 sent 的文件 ⇒ 拒绝 ===")
    db = make_db()
    base = snapshot(db)
    r = run(db, "--reassign-of", "202", "--to", NEW, "--yes")
    check("B1 exit 2", r.returncode == 2, f"rc={r.returncode}")
    check("B2 库没被动", snapshot(db) == base)

    # ── C. 不带 --yes ⇒ dry-run ────────────────────────────────
    print("\n=== C. 不带 --yes：只打印 SQL，不写库 ===")
    db = make_db()
    base = snapshot(db)
    r = run(db, "--reassign-of", "101", "--to", NEW)
    check("C1 exit 0", r.returncode == 0, f"rc={r.returncode} err={r.stderr[:200]}")
    check("C2 明确说了 dry-run", "dry-run" in r.stdout.lower(), f"out={r.stdout[-160:]}")
    check("C3 打印了要执行的 SQL", "UPDATE orders SET target_client" in r.stdout)
    check("C4 库没被动", snapshot(db) == base)

    # ── D. 带 --yes ⇒ 只改 target_client ───────────────────────
    print("\n=== D. 带 --yes：只改 orders.target_client ===")
    db = make_db()
    base = snapshot(db)
    r = run(db, "--reassign-of", "101", "--to", NEW, "--yes")
    check("D1 exit 0", r.returncode == 0, f"rc={r.returncode} err={r.stderr[:200]}")
    after = snapshot(db)
    check("D2 order#1 的目标设备变成新设备", after["targets"].get(1) == NEW,
          f"实得 {after['targets'].get(1)!r}")
    check("D3 order_files.status 一个都没动", after["file_status"] == base["file_status"])
    check("D4 orders.status 一个都没动", after["order_status"] == base["order_status"])
    check("D5 别的订单目标没动",
          all(after["targets"][k] == base["targets"][k] for k in (2, 3, 4)),
          f"实得 {after['targets']}")

    # ── E. --reassign-from 批量 ────────────────────────────────
    print("\n=== E. --reassign-from：只搬「旧设备 + 有排队文件」的单 ===")
    db = make_db()
    base = snapshot(db)
    r = run(db, "--reassign-from", OLD, "--to", NEW, "--yes")
    check("E1 exit 0", r.returncode == 0, f"rc={r.returncode} err={r.stderr[:200]}")
    after = snapshot(db)
    check("E2 order#1（纯排队）被搬走", after["targets"].get(1) == NEW)
    check("E3 order#2（混单）被搬走", after["targets"].get(2) == NEW)
    check("E4 order#3（全终态）不动", after["targets"].get(3) == OLD,
          f"实得 {after['targets'].get(3)!r}")
    check("E5 order#4（别的设备）不动", after["targets"].get(4) == OTHER)
    check("E6 所有 order_files.status 未动", after["file_status"] == base["file_status"])
    check("E7 orders.status 未动", after["order_status"] == base["order_status"])

    # ── F. --to 为空 ⇒ 拒绝 ────────────────────────────────────
    print("\n=== F. --to 为空 ⇒ 拒绝 ===")
    db = make_db()
    base = snapshot(db)
    r = run(db, "--reassign-of", "101", "--to", "  ", "--yes")
    check("F1 exit 2", r.returncode == 2, f"rc={r.returncode}")
    check("F2 库没被动", snapshot(db) == base)

    # ── G. 空库（生产当前状态：queued = 0）────────────────────
    print("\n=== G. 没有排队子任务时：正常退 0，不报错 ===")
    d = tempfile.mkdtemp(prefix="hn_reassign_empty_")
    ep = os.path.join(d, "orders.db")
    conn = sqlite3.connect(ep)
    conn.executescript("""
        CREATE TABLE orders (id INTEGER PRIMARY KEY AUTOINCREMENT, order_number TEXT,
                             target_client TEXT, status TEXT, created_at TEXT);
        CREATE TABLE order_files (id INTEGER PRIMARY KEY AUTOINCREMENT, order_id INTEGER NOT NULL,
                                  file_name TEXT, status TEXT, created_at TEXT);
    """)
    conn.commit()
    conn.close()
    r = run(ep, "--list")
    check("G1 exit 0", r.returncode == 0, f"rc={r.returncode} err={r.stderr[:200]}")
    check("G2 没有 traceback", "Traceback" not in r.stderr, f"err={r.stderr[:200]}")

    print("\n" + "=" * 78)
    if fails:
        print(f"FAILED  {len(fails)}/{checks}: {fails}")
        return 1
    print(f"ALL PASS  {checks}/{checks}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
