# -*- coding: utf-8 -*-
"""验收 · S3-02 数据层迁移演练（非破坏性）+ 读写并发实证

依据：docs/安全加固修复计划_2026-10-23.md §S3-02（R9）："写锁不再阻塞读；迁移演练"。

交付物：`mobile_apps/printer-backend/ops/db_migration_drill.py`
  · 源库只读 + SQLite backup API 拷出（不是 cp，WAL 未 checkpoint 的页也进备份）；
  · integrity_check / foreign_key_check / 逐表行数；
  · 用**当前代码的 init_db()** 在副本上重放迁移（幂等性 + 行数不丢 + 迁移后仍是 WAL）；
  · WAL 下"写事务持锁时另一连接读"的耗时（阈值默认 250ms）+ N 并发写全部成功；
  · 源库 SHA-256 前后一致（演练绝不碰生产库）；
  · JSON 报告 + 退出码（0 通过 / 1 检查失败 / 2 参数或 IO 错误）。

本脚本：
  A. 静态接线：备份用 backup API、源库 mode=ro、完整性/外键、BEGIN IMMEDIATE、
     源库指纹校验、诊断开关 HN_DRILL_MODE 只影响演练（app 启动清理/MD5 索引跳过）；
  B. 行为：用真实后端建一份带数据的源库 → 跑演练子进程 → 全部检查通过、
     源库字节未变、报告字段齐全；缺库/坏库 → 非 0 退出且报告写明原因；
  C. 变异自证：把写事务改成 rollback journal + BEGIN EXCLUSIVE ⇒ 读被写锁挡住，
     B 的"写锁不阻塞读"判据必须能红。

用法：python tests/verify_db_migration.py     （exit 0 = 全部通过）
"""
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(REPO, "mobile_apps", "printer-backend")
DRILL = os.path.join(BACKEND, "ops", "db_migration_drill.py")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _sec_harness as H  # noqa: E402

H.install_cleanup_hook()

_TMPS: list[str] = []


def _mkroot(prefix="hn_s302_"):
    d = tempfile.mkdtemp(prefix=prefix)
    _TMPS.append(d)
    return d


def _read(path):
    with open(path, "r", encoding="utf-8", newline="") as f:
        return f.read()


# ============================================================
# A. 静态接线
# ============================================================
print("=== A. 静态接线（演练工具 / 诊断开关）===")
drill_src = _read(DRILL)
app_src = H.read_app_source()

H.chk("演练工具存在且用 SQLite backup API 备份（不是 cp）",
      os.path.isfile(DRILL) and ".backup(" in drill_src and "mode=ro" in drill_src)
H.chk("备份前后做 integrity_check / foreign_key_check",
      "PRAGMA integrity_check" in drill_src and "PRAGMA foreign_key_check" in drill_src)
H.chk("★ 用当前代码的 init_db() 在副本上重放迁移（同一份迁移代码）",
      "import app as backend" in drill_src and "backend.init_db()" in drill_src)
H.chk("★ 写事务用 BEGIN IMMEDIATE（WAL 下写串行、读不阻塞）",
      'writer.execute("BEGIN IMMEDIATE")' in drill_src)
H.chk("★ 演练前后校验源库 SHA-256 未变化（绝不改生产库）",
      "sha256_before" in drill_src and "sha256_after" in drill_src
      and "source_unchanged" in drill_src)
H.chk("演练模式显式跳过 app 启动清理与 MD5 索引（副本没有 uploads）",
      app_src.count('os.environ.get("HN_DRILL_MODE") == "1"') >= 2
      and "HN_DRILL_MODE" in drill_src)
H.chk("报告含检查项/读耗时/并发写/路由结果（可留档）",
      all(k in drill_src for k in ("read_during_write", "concurrent_writes",
                                   "checks", "row_count_diff", "journal_mode")))

# ============================================================
# B. 行为：真实后端建一份带数据的库 → 跑演练
# ============================================================
print("\n=== B. 行为（真实后端 + 带数据的源库）===")
TMP, DST = H.make_copy("hn_s302_src_")
backend = H.import_backend(DST)
backend.init_db()

# 造数据：orders / order_files / files 各 2 行；users 1 行
os.makedirs(os.path.join(DST, "uploads"), exist_ok=True)
conn = backend.get_db()
for i in (1, 2):
    conn.execute(
        "INSERT INTO orders (id, file, copies, status, created_at, openid) "
        "VALUES (?, ?, 1, 'sent', '2026-10-01 10:00:00', 'u1')", (i, "a%d.pdf" % i))
    conn.execute(
        "INSERT INTO files (id, original_name, saved_name, path, size, created_at) "
        "VALUES (?, ?, ?, ?, 10, '2026-10-01 10:00:00')",
        ("f%d" % i, "a%d.pdf" % i, "a%d.pdf" % i, "uploads/a%d.pdf" % i))
    conn.execute(
        "INSERT INTO order_files (id, order_id, file_id, file_name, copies, page_count, "
        "status, created_at) VALUES (?, ?, ?, ?, 1, 1, 'sent', '2026-10-01 10:00:00')",
        (i, i, "f%d" % i, "a%d.pdf" % i))
conn.commit()
conn.close()
uconn = backend.get_user_db()
uconn.execute("INSERT INTO users (openid, nickname, updated_at) VALUES ('u1','测试','2026-10-01 10:00:00')")
uconn.commit()
uconn.close()
src_orders = os.path.join(DST, "orders.db")
src_users = os.path.join(DST, "users.db")

work = _mkroot("hn_s302_work_")
report_path = os.path.join(work, "report.json")
proc = subprocess.run(
    [sys.executable, DRILL,
     "--source", src_orders, "--users", src_users,
     "--workdir", work, "--report", report_path, "--backend-dir", BACKEND],
    capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=240)
report = {}
if os.path.isfile(report_path):
    with open(report_path, "r", encoding="utf-8") as f:
        report = json.load(f)
else:
    print("drill stdout tail:", (proc.stdout or "")[-800:])
    print("drill stderr tail:", (proc.stderr or "")[-800:])

checks = report.get("checks") or {}
H.chk("演练退出码 0 且报告 ok", proc.returncode == 0 and report.get("ok") is True,
      f"rc={proc.returncode} checks={checks}")
H.chk("★ 迁移后行数一行不丢（orders/order_files/files 与用户库）",
      checks.get("migration_kept_all_rows") and checks.get("users_migration_kept_all_rows"),
      {k: v for k, v in (report.get("migration") or {}).items() if "diff" in k})
H.chk("迁移后仍是 WAL + integrity_check=ok",
      checks.get("wal_after_migration") and checks.get("integrity_after_ok")
      and (report.get("integrity_after") or {}).get("integrity_check") == "ok")
H.chk("★ 写事务持锁时读不阻塞（读耗时 < 阈值，journal=wal）",
      checks.get("read_not_blocked_by_write")
      and (report.get("read_during_write") or {}).get("journal_mode", "").lower() == "wal"
      and (report.get("read_during_write") or {}).get("read_ms", 9999) <
      (report.get("read_during_write") or {}).get("limit_ms", 0),
      report.get("read_during_write"))
H.chk("★ 8 个并发写全部成功（写路径正确串行化，无 database is locked）",
      checks.get("concurrent_writes_ok")
      and (report.get("concurrent_writes") or {}).get("succeeded") == 8,
      report.get("concurrent_writes"))
H.chk("★ 源库 SHA-256 前后一致（演练非破坏）",
      checks.get("source_unchanged")
      and report["source"]["sha256_before"] == report["source"]["sha256_after"])
H.chk("报告留档字段齐全（时间/表行数/读耗时/并发写）",
      all(k in report for k in ("started_at", "finished_at", "integrity_before",
                                "integrity_after", "read_during_write",
                                "concurrent_writes", "checks")))

# 失败路径：源库不存在 → 非 0（参数/IO）
_p2 = subprocess.run(
    [sys.executable, DRILL, "--source", os.path.join(work, "nope.db"),
     "--workdir", _mkroot("hn_s302_bad_"), "--report", os.path.join(work, "bad.json")],
    capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
H.chk("源库不存在 → 非 0 退出（并写报告说明原因）",
      _p2.returncode != 0 and os.path.isfile(os.path.join(work, "bad.json")),
      f"rc={_p2.returncode}")

# 失败路径：损坏的库 → 非 0
bad_dir = _mkroot("hn_s302_corrupt_")
bad_db = os.path.join(bad_dir, "orders.db")
with open(bad_db, "wb") as f:
    f.write(b"this is not a sqlite database" * 100)
_p3 = subprocess.run(
    [sys.executable, DRILL, "--source", bad_db,
     "--workdir", _mkroot("hn_s302_cw_"), "--report", os.path.join(work, "corrupt.json")],
    capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
H.chk("源库损坏 → 非 0 退出（不会假装演练通过）", _p3.returncode != 0, f"rc={_p3.returncode}")

# ============================================================
# C. 变异自证：写事务换 rollback journal + BEGIN EXCLUSIVE
# ============================================================
print("\n=== C. 变异自证（BEGIN EXCLUSIVE + rollback journal ⇒ 读被写锁挡住）===")
_mut_dir = _mkroot("hn_s302_mut_")
_mut_src = drill_src.replace(
    '        writer.execute("BEGIN IMMEDIATE")',
    '        writer.execute("PRAGMA journal_mode=DELETE")  # 变异：退回 rollback journal\n'
    '        writer.execute("BEGIN EXCLUSIVE")              # 变异：独占锁', 1)
if _mut_src == drill_src:
    raise AssertionError("变异锚点未命中：BEGIN IMMEDIATE")
_mut_path = os.path.join(_mut_dir, "drill_mut.py")
with open(_mut_path, "w", encoding="utf-8") as f:
    f.write(_mut_src)

_probe = os.path.join(_mut_dir, "_probe.py")
with open(_probe, "w", encoding="utf-8") as f:
    f.write(
        "# -*- coding: utf-8 -*-\n"
        "import importlib.util, os, sqlite3, sys, tempfile\n"
        "sys.stdout = __import__('io').TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')\n"
        "spec = importlib.util.spec_from_file_location('drill_mut', %r)\n"
        "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n"
        "d = tempfile.mkdtemp(); db = os.path.join(d, 'x.db')\n"
        "c = sqlite3.connect(db); c.execute('PRAGMA journal_mode=WAL');\n"
        "c.execute('CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)');\n"
        "c.execute(\"INSERT INTO t (v) VALUES ('a')\"); c.commit(); c.close()\n"
        "r = m._measure_read_during_write(db, 100)\n"
        "print('READ_OK=%%d' %% int(bool(r.get('read_ok'))))\n"
        "print('ERR=%%s' %% (r.get('error') or '')[:80])\n" % _mut_path)
env = dict(os.environ)
env["PYTHONPATH"] = ""
env["PYTHONUTF8"] = "1"
_pm = subprocess.run([sys.executable, _probe], capture_output=True, text=True,
                     encoding="utf-8", errors="replace", timeout=120, env=env)
H.chk("变异后退回 rollback journal + 独占锁 ⇒ 读被挡住（B 的判据能红）",
      H.probe_value(_pm, "READ_OK") == "0",
      "probe=%s stderr=%s" % (H.probe_value(_pm, "READ_OK"), (_pm.stderr or "")[-200:]))

for _d in _TMPS:
    shutil.rmtree(_d, ignore_errors=True)

H.finish()
