"""db_migration_drill.py — S3-02 数据层迁移演练（非破坏性，源库只读）

用途
----
在**不碰生产库**的前提下回答三个问题：

1. 把线上 SQLite 在线备份出来之后，数据是否完整（integrity_check / foreign_key_check）；
2. 用**当前代码的 init_db()** 在同一份数据上重放迁移，会不会丢行/写坏库（幂等性演练）；
3. WAL 模式下，一个写事务持锁时另一个连接还能不能正常读（"写锁不再阻塞读"的实证），
   以及并发写是否被正确串行化（全部成功、不报 database is locked）。

整个演练在 `--workdir` 临时目录里进行：
`source` 只用 `mode=ro` 打开、用 SQLite backup API 拷出（WAL 里未 checkpoint 的页也会进备份），
演练只改副本、不改源库。跑完把 JSON 报告写到 `--report`，供留档/对比。

用法
----
    # 本机（开发库）
    python ops/db_migration_drill.py \
        --source orders.db --users users.db \
        --workdir /tmp/hn_drill --report /tmp/hn_drill/report.json

    # 服务器（生产，推荐在低峰跑；源库只读）
    /home/printer-backend/venv/bin/python ops/db_migration_drill.py \
        --source /home/printer-backend/orders.db \
        --users /home/printer-backend/users.db \
        --workdir /tmp/hn_drill_$(date +%s) --report /tmp/hn_drill_$(date +%s).json

退出码：0 = 全部检查通过；1 = 有检查失败（报告里写明哪一项）；2 = 参数/IO 错误。

阈值
----
`READ_BLOCK_MS_LIMIT = 250`：WAL 下写事务持锁时，普通 SELECT 应在毫秒级完成；超过 250ms
说明读被写锁挡住了（或库不在 WAL 上），判失败。阈值可用 `--read-limit-ms` 调整。

外键口径（2026-10-24 生产演练后补）
----------------------------------
线上 `orders.file_id → files.id` 有**已知良性**违例：文件到期被清理（S1-03/S2-05），订单行
保留 file_id 作历史凭据；SQLite 默认不强制外键，生产实测 228 条全部属于这一类。默认放行该类
（计入 warnings），其它组合仍判失败；`--strict-fk` 可恢复"任何违例即失败"。用户库无豁免。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time

READ_BLOCK_MS_LIMIT = 250.0
CONCURRENT_WRITERS = 8
SOURCE_IGNORE = shutil.ignore_patterns(
    "__pycache__", "config.py", "config.py.example", "*.db", "*.db-wal", "*.db-shm",
    "uploads", "logs", "static", "*.bak*", ".git", "node_modules")


# ────────────────────────── 小工具 ──────────────────────────

def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _count_tables(conn: sqlite3.Connection) -> dict[str, int]:
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
        "ORDER BY name")]
    counts: dict[str, int] = {}
    for t in tables:
        try:
            counts[t] = int(conn.execute('SELECT COUNT(*) FROM "%s"' % t.replace('"', '""')).fetchone()[0])
        except sqlite3.Error:
            counts[t] = -1
    return counts


# 已知"良性"外键违例（**不是损坏**，是保留期清理留下的历史引用）：
#   orders.file_id → files.id
# files 行到期后由 cleanup_expired_files 清掉（S1-03 保留期 / S2-05 引用计数），但订单行
# 保留 file_id 作为历史凭据（结算/追溯/重印要用）；SQLite 默认不强制外键
# （PRAGMA foreign_keys 默认 OFF），所以线上一直存在这类行。
# 2026-10-24 生产演练实测 228 条，全部是这一种（`orders.file_id` 非空、files 行已过期）。
# 处置：默认计为 benign（写进 warnings）；**不**去改历史订单数据，也**不**把整类违例一律放过 ——
# 其它 (table -> parent) 组合仍算 unexpected（例如某次迁移写坏的外键）并判失败。
# 需要"任何违例即失败"的严格口径时加 `--strict-fk`。
BENIGN_FK_RULES = {("orders", "files")}


def _fk_violations(conn: sqlite3.Connection, benign_rules=BENIGN_FK_RULES) -> dict:
    """把 PRAGMA foreign_key_check 的结果按 (table -> parent) 分类。"""
    try:
        rows = conn.execute("PRAGMA foreign_key_check").fetchall()
    except sqlite3.Error as e:  # noqa: BLE001
        return {"total": -1, "benign": 0, "unexpected": 0, "detail": {},
                "samples": [], "error": "%s: %s" % (type(e).__name__, e)}
    detail: dict[str, int] = {}
    samples: list[dict] = []
    benign = unexpected = 0
    for table, rowid, parent, _fkid in rows:
        key = "%s->%s" % (table, parent)
        detail[key] = detail.get(key, 0) + 1
        if (str(table), str(parent)) in benign_rules:
            benign += 1
        else:
            unexpected += 1
            if len(samples) < 5:
                samples.append({"table": table, "rowid": rowid, "parent": parent})
    return {"total": len(rows), "benign": benign, "unexpected": unexpected,
            "detail": detail, "samples": samples, "error": ""}


def _inspect_db(path: str, benign_rules=BENIGN_FK_RULES) -> dict:
    conn = sqlite3.connect("file:%s?mode=ro" % path.replace("\\", "/"), uri=True, timeout=10)
    try:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        fk = _fk_violations(conn, benign_rules)
        journal = conn.execute("PRAGMA journal_mode").fetchone()[0]
        counts = _count_tables(conn)
        return {
            "integrity_check": integrity,
            # 兼容旧字段：违例总数（含 benign）
            "foreign_key_violations": fk["total"],
            "foreign_key_detail": fk,
            "journal_mode": journal,
            "tables": counts,
            "rows": sum(v for v in counts.values() if v > 0),
        }
    finally:
        conn.close()


def _online_backup(src: str, dst_dir: str) -> str:
    """用 SQLite backup API 把源库（只读打开）拷到 dst_dir；返回副本路径。"""
    if not os.path.isfile(src):
        raise FileNotFoundError("源库不存在: %s" % src)
    dst = os.path.join(dst_dir, os.path.basename(src))
    src_conn = sqlite3.connect("file:%s?mode=ro" % src.replace("\\", "/"), uri=True, timeout=30)
    try:
        dst_conn = sqlite3.connect(dst)
        try:
            src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
    finally:
        src_conn.close()
    return dst


# ────────────────────────── 迁移演练 ──────────────────────────

def _rehearse_migrations(backend_dir: str, workdir: str, originals: dict[str, str]) -> dict:
    """把后端源码复制到 workdir/app，放入备份库，跑 `app.init_db()`（与线上同一份迁移代码）。

    originals: {文件名: 备份路径}，例如 {"orders.db": ..., "users.db": ...}
    """
    app_dir = os.path.join(workdir, "app")
    shutil.copytree(backend_dir, app_dir, ignore=SOURCE_IGNORE)
    os.makedirs(os.path.join(app_dir, "uploads"), exist_ok=True)
    os.makedirs(os.path.join(app_dir, "logs"), exist_ok=True)
    for name, src in originals.items():
        shutil.copy2(src, os.path.join(app_dir, name))

    code = (
        "import os, sys\n"
        "os.chdir(%r)\n"
        "sys.path.insert(0, %r)\n"
        "import app as backend\n"
        "backend.init_db()\n"
        "print('INIT_DB_OK')\n" % (app_dir, app_dir)
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = ""
    env["FLASK_ENV"] = "development"      # 允许内置默认 SECRET_KEY（演练不碰真实凭据）
    env["PYTHONUTF8"] = "1"
    # 演练副本不搬 uploads：让 app 跳过"磁盘幽灵清理"与 MD5 索引补全（否则 files 行会被
    # 当成物理文件已消失而删掉，行数比对失去意义）。正常启动/生产不会设置该变量。
    env["HN_DRILL_MODE"] = "1"
    proc = subprocess.run([sys.executable, "-c", code], cwd=app_dir, env=env,
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=180)
    return {
        "ok": proc.returncode == 0 and "INIT_DB_OK" in (proc.stdout or ""),
        "returncode": proc.returncode,
        "stdout_tail": (proc.stdout or "").strip().splitlines()[-5:],
        "stderr_tail": (proc.stderr or "").strip().splitlines()[-5:],
        "app_dir": app_dir,
    }


# ────────────────────────── 读写并发实证 ──────────────────────────

def _measure_read_during_write(db_path: str, read_limit_ms: float) -> dict:
    """WAL 下：写事务持锁（未提交）时，另一个连接的 SELECT 是否还能在阈值内完成。

    做法：开一个写连接 BEGIN IMMEDIATE + UPDATE（不提交），开另一个读连接 SELECT；
    记录耗时。随后提交，校验读连接能看到新值（快照语义）。
    没有可更新的行时创建一个演练专用表 `_drill_probe`（只存在于演练副本）。
    """
    writer = sqlite3.connect(db_path, timeout=10, isolation_level=None)
    reader = sqlite3.connect(db_path, timeout=10, isolation_level=None)
    result = {"read_ms": None, "read_ok": False, "limit_ms": read_limit_ms,
              "journal_mode": "", "error": ""}
    try:
        result["journal_mode"] = writer.execute("PRAGMA journal_mode").fetchone()[0]
        reader.execute("PRAGMA busy_timeout = %d" % int(read_limit_ms))

        # 找一个可更新的目标（优先真实表，避免污染业务数据：用 rollback 收尾）
        target, rowid = "", None
        for table in ("orders", "order_files", "files", "users"):
            try:
                row = writer.execute('SELECT rowid FROM "%s" LIMIT 1' % table).fetchone()
            except sqlite3.Error:
                row = None
            if row:
                target, rowid = table, row[0]
                break
        if not target:
            writer.execute("CREATE TABLE IF NOT EXISTS _drill_probe (id INTEGER PRIMARY KEY, v TEXT)")
            writer.execute("INSERT INTO _drill_probe (v) VALUES ('a')")
            target, rowid = "_drill_probe", writer.execute("SELECT last_insert_rowid()").fetchone()[0]

        writer.execute("BEGIN IMMEDIATE")
        try:
            # 持锁期间做一次写（不提交）
            writer.execute('UPDATE "%s" SET rowid = rowid WHERE rowid = ?' % target, (rowid,))
            t0 = time.perf_counter()
            reader.execute('SELECT COUNT(*) FROM "%s"' % target).fetchone()
            elapsed = (time.perf_counter() - t0) * 1000.0
            result["read_ms"] = round(elapsed, 2)
            result["read_ok"] = elapsed <= read_limit_ms
        finally:
            writer.execute("ROLLBACK")
    except Exception as e:  # noqa: BLE001
        result["error"] = "%s: %s" % (type(e).__name__, e)
        result["read_ok"] = False
    finally:
        try:
            writer.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            reader.close()
        except Exception:  # noqa: BLE001
            pass
    return result


def _measure_concurrent_writes(db_path: str, writers: int = CONCURRENT_WRITERS) -> dict:
    """N 个线程各写一次：WAL 下写是串行的，但**全部必须成功**（busy_timeout 30s 兜住）。"""
    conn = sqlite3.connect(db_path, timeout=10, isolation_level=None)
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS _drill_probe (id INTEGER PRIMARY KEY, v TEXT)")
        conn.execute("INSERT INTO _drill_probe (v) VALUES ('w')")
        rid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    finally:
        conn.close()

    ok = [0]
    errors: list[str] = []
    lock = threading.Lock()

    def _worker(i: int) -> None:
        c = sqlite3.connect(db_path, timeout=30, isolation_level=None)
        try:
            c.execute("PRAGMA busy_timeout = 30000")
            c.execute("BEGIN IMMEDIATE")
            c.execute("UPDATE _drill_probe SET v = ? WHERE id = ?", ("w%d" % i, rid))
            c.execute("COMMIT")
            with lock:
                ok[0] += 1
        except Exception as e:  # noqa: BLE001
            with lock:
                errors.append("%s: %s" % (type(e).__name__, e))
        finally:
            c.close()

    t0 = time.perf_counter()
    threads = [threading.Thread(target=_worker, args=(i,), daemon=True) for i in range(writers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    elapsed = (time.perf_counter() - t0) * 1000.0
    return {"requested": writers, "succeeded": ok[0], "elapsed_ms": round(elapsed, 2),
            "ok": ok[0] == writers and not errors, "errors": errors[:3]}


# ────────────────────────── 主流程 ──────────────────────────

def run_drill(args) -> dict:
    workdir = os.path.abspath(args.workdir)
    os.makedirs(workdir, exist_ok=True)
    report: dict = {
        "ok": False,
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "source": {}, "backup": {}, "integrity_before": {}, "integrity_after": {},
        "migration": {}, "read_during_write": {}, "concurrent_writes": {},
        "read_limit_ms": args.read_limit_ms, "warnings": [],
    }

    # 1) 源库指纹（演练前后必须一致）
    src_hash_before = _sha256(args.source)
    report["source"] = {
        "path": os.path.abspath(args.source),
        "sha256_before": src_hash_before,
        "size": os.path.getsize(args.source),
    }

    # 2) 在线备份 + 备份完整性
    backup_dir = os.path.join(workdir, "backup")
    os.makedirs(backup_dir, exist_ok=True)
    b_orders = _online_backup(args.source, backup_dir)
    backup_users = None
    if args.users:
        if not os.path.isfile(args.users):
            raise FileNotFoundError("用户库不存在: %s" % args.users)
        backup_users = _online_backup(args.users, backup_dir)
    report["backup"] = {"orders": b_orders, "users": backup_users,
                        "orders_sha256": _sha256(b_orders)}

    before = _inspect_db(b_orders, () if args.strict_fk else BENIGN_FK_RULES)
    report["integrity_before"] = before
    fk_before = before.get("foreign_key_detail") or {}
    if fk_before.get("benign"):
        report["warnings"].append(
            "外键违例 %d 条属于已知良性类（orders.file_id → files.id：文件到期已清理、"
            "订单保留历史 file_id），不计失败；明细 %s"
            % (fk_before.get("benign"), fk_before.get("detail")))
    checks = {
        "integrity_before_ok": before["integrity_check"] == "ok",
        # 只把**非良性**违例判失败；--strict-fk 时 benign 规则被清空 ⇒ 等价于"任何违例即失败"
        "foreign_keys_before_ok": fk_before.get("unexpected", 1) == 0,
        "orders_copy_has_data": before["rows"] > 0 or bool(args.allow_empty),
    }
    if before["rows"] == 0:
        report["warnings"].append("源库没有任何数据行：演练只验证 schema/迁移，不代表业务数据规模")

    # 3) 迁移演练（当前代码 init_db 在同一份数据上重放）
    originals = {"orders.db": b_orders}
    if backup_users:
        originals["users.db"] = backup_users
    migration = _rehearse_migrations(args.backend_dir, workdir, originals)
    report["migration"] = migration
    checks["migration_ok"] = bool(migration.get("ok"))

    after_orders = os.path.join(migration.get("app_dir", workdir), "orders.db")
    after = _inspect_db(after_orders, () if args.strict_fk else BENIGN_FK_RULES) \
        if os.path.isfile(after_orders) else {}
    report["integrity_after"] = after
    rows_after = after.get("tables") or {}
    rows_before = before.get("tables") or {}
    missing = {t: n for t, n in rows_before.items() if rows_after.get(t, -1) != n}
    report["migration"]["rows_before"] = rows_before
    report["migration"]["rows_after"] = rows_after
    report["migration"]["row_count_diff"] = missing
    checks["migration_kept_all_rows"] = bool(after) and not missing
    checks["integrity_after_ok"] = bool(after) and after.get("integrity_check") == "ok"
    # 迁移不得新增非良性外键违例（良性那类只核"没有变多"）
    fk_after = (after.get("foreign_key_detail") or {}) if after else {}
    checks["foreign_keys_after_ok"] = (
        bool(after) and fk_after.get("unexpected", 1) == 0
        and fk_after.get("benign", 0) <= fk_before.get("benign", 0))
    checks["wal_after_migration"] = bool(after) and after.get("journal_mode", "").lower() == "wal"

    # 用户库同样比对（若提供）：用户库没有"保留期历史引用"的豁免，任何外键违例都算异常
    if backup_users:
        users_before = _inspect_db(backup_users, ())
        report["users_before"] = users_before
        users_after_path = os.path.join(migration.get("app_dir", workdir), "users.db")
        users_after = _inspect_db(users_after_path, ()) if os.path.isfile(users_after_path) else {}
        report["users_after"] = users_after
        users_diff = {
            t: n for t, n in (users_before.get("tables") or {}).items()
            if (users_after.get("tables") or {}).get(t, -1) != n
        }
        report["migration"]["users_row_count_diff"] = users_diff
        checks["users_migration_kept_all_rows"] = bool(users_after) and not users_diff
        checks["users_integrity_after_ok"] = (
            bool(users_after) and users_after.get("integrity_check") == "ok")

    # 4) 读写并发实证（在迁移后的副本上做，移动 WAL 语义与线上一致）
    rw = _measure_read_during_write(after_orders, args.read_limit_ms) if os.path.isfile(after_orders) else {}
    report["read_during_write"] = rw
    checks["read_not_blocked_by_write"] = bool(rw.get("read_ok"))
    cw = _measure_concurrent_writes(after_orders) if os.path.isfile(after_orders) else {}
    report["concurrent_writes"] = cw
    checks["concurrent_writes_ok"] = bool(cw.get("ok"))

    # 5) 源库未被改动
    src_hash_after = _sha256(args.source)
    report["source"]["sha256_after"] = src_hash_after
    checks["source_unchanged"] = src_hash_before == src_hash_after

    report["checks"] = checks
    report["ok"] = all(bool(v) for v in checks.values())
    report["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    return report


def main(argv=None) -> int:
    default_backend = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ap = argparse.ArgumentParser(description="S3-02 数据层迁移演练（非破坏性）")
    ap.add_argument("--source", required=True, help="源 orders.db（只读打开）")
    ap.add_argument("--users", default="", help="源 users.db（可选）")
    ap.add_argument("--workdir", default="", help="演练临时目录（默认自动建）")
    ap.add_argument("--report", default="", help="JSON 报告输出路径（默认 <workdir>/report.json）")
    ap.add_argument("--backend-dir", default=default_backend,
                    help="后端源码目录（默认本脚本上一级）")
    ap.add_argument("--read-limit-ms", type=float, default=READ_BLOCK_MS_LIMIT,
                    help="写事务持锁时读耗时阈值（毫秒，默认 250）")
    ap.add_argument("--strict-fk", action="store_true",
                    help="任何外键违例都判失败（默认放过已知良性类 orders.file_id→files.id，"
                         "那是文件到期清理后的历史引用，见 BENIGN_FK_RULES 注释）")
    ap.add_argument("--allow-empty", action="store_true",
                    help="允许源库没有数据行（默认要求有业务数据，否则演练意义不足）")
    args = ap.parse_args(argv)

    if not args.workdir:
        args.workdir = tempfile.mkdtemp(prefix="hn_db_drill_")
    os.makedirs(args.workdir, exist_ok=True)
    if not args.report:
        args.report = os.path.join(args.workdir, "report.json")

    try:
        report = run_drill(args)
    except Exception as e:  # noqa: BLE001
        report = {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}
        _write_report(args.report, report)
        print("[DRILL] 失败（异常）: %s" % report["error"])
        return 2

    _write_report(args.report, report)
    _print_summary(report)
    return 0 if report.get("ok") else 1


def _write_report(path: str, report: dict) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print("[DRILL] 报告已写入: %s" % path)


def _print_summary(report: dict) -> None:
    print("[DRILL] 迁移演练 %s" % ("通过 ✅" if report.get("ok") else "失败 ❌"))
    checks = report.get("checks") or {}
    for name, ok in checks.items():
        print("  %s %s" % ("[PASS]" if ok else "[FAIL]", name))
    rw = report.get("read_during_write") or {}
    if rw.get("read_ms") is not None:
        print("  写事务持锁时读耗时: %s ms（阈值 %s ms，journal=%s）"
              % (rw.get("read_ms"), rw.get("limit_ms"), rw.get("journal_mode")))
    fk = ((report.get("integrity_before") or {}).get("foreign_key_detail") or {})
    if fk.get("total"):
        print("  外键违例: 总 %s（良性 %s / 异常 %s）%s"
              % (fk.get("total"), fk.get("benign"), fk.get("unexpected"),
                 ("明细 " + str(fk.get("detail"))) if fk.get("detail") else ""))
    cw = report.get("concurrent_writes") or {}
    if cw:
        print("  并发写: %s/%s 成功，用时 %s ms"
              % (cw.get("succeeded"), cw.get("requested"), cw.get("elapsed_ms")))
    diff = (report.get("migration") or {}).get("row_count_diff") or {}
    if diff:
        print("  行数差异: %s" % diff)


if __name__ == "__main__":
    sys.exit(main())
