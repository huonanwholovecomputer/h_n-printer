# -*- coding: utf-8 -*-
"""验收 · S2-06 备份重构（WAL 安全 / 双库 / 恢复演练 / 保留期 / 可选加密与异地）

依据：docs/安全加固修复计划_2026-10-23.md §S2-06（DS-15）。

现场形状（修复前）：`backup.sh` 只有一句 `cp orders.db` ——
  · 库是 WAL 模式，最近的写入可能还**只在 orders.db-wal 里**，`cp` 拿到的是上一次检查点
    的快照（丢最近的订单，且文件本身可能不一致）；
  · 只备 orders.db，**users.db（账号/角色/许可）完全没备**；
  · 没有完整性校验、没有恢复演练、没有加密与异地。

本脚本：
  A. 静态：脚本用 SQLite 在线 backup API（不是 cp）、备两个库、gzip、保留期清理、
     `--restore-check` 恢复演练、`BACKUP_GPG_RECIPIENT`/`BACKUP_REMOTE` 可选分支。
  B. 行为（真 bash + 真 sqlite + WAL）：
     · 造一个 WAL 里**已提交但未 checkpoint** 的订单（连接保持打开），跑备份 ⇒
       备份里必须能看到它（这是 `cp` 与 backup API 的分水岭）；
     · users.db 也被备份且内容一致；
     · `--restore-check` 输出 `RESTORE_CHECK=ok`、退出码 0；
     · 超过保留期的旧备份被清理、未过期的保留。
  C. 变异自证：把备份动作换回 `cp "$SRC" "$OUT"` ⇒ 备份里缺 WAL 行（判据能红）。

用法：python tests/verify_backup_script.py     （exit 0 = 全部通过）
"""
import io
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _sec_harness as H  # noqa: E402

H.install_cleanup_hook()
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(REPO, "mobile_apps", "printer-backend")
BACKUP_SH = os.path.join(BACKEND, "backup.sh")
script_src = open(BACKUP_SH, encoding="utf-8").read()

BASH = shutil.which("bash") or (
    r"C:\Program Files\Git\bin\bash.exe" if os.path.exists(r"C:\Program Files\Git\bin\bash.exe") else None)

# ============================================================
# A. 静态
# ============================================================
print("=== A. 静态 ===")
H.chk("用 SQLite 在线 backup API（src.backup(dst)）", "src.backup(dst)" in script_src)
H.chk("★ 不再用 cp 备份活动库（注释里说明原因也不算代码）",
      'cp "$SRC" "$OUT"' not in script_src)
H.chk("备 orders.db **和** users.db", 'DB_FILES=(orders.db users.db)' in script_src)
H.chk("备份后立刻 integrity_check", "pragma integrity_check" in script_src)
H.chk("gzip 压缩 + 保留期清理", "gzip -f" in script_src and "RETENTION_DAYS" in script_src)
H.chk("支持 --restore-check 恢复演练（行数 + 最新订单 + WAL 缺失检查）",
      "--restore-check" in script_src and "RESTORE_CHECK=" in script_src
      and "最新订单不一致" in script_src and "WAL 未落盘" in script_src)
H.chk("可选加密（gpg --encrypt 并删除明文）与异地副本",
      "BACKUP_GPG_RECIPIENT" in script_src and "gpg --yes --batch --encrypt" in script_src
      and 'rm -f "$OUT.gz"' in script_src and "BACKUP_REMOTE" in script_src)
H.chk("★ PROJECT_DIR 缺省 = 脚本所在目录（不写死 /opt，换部署位置不会静默备份 0 个库）",
      'PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)' in script_src)
H.chk("★ 一个库都没备到 ⇒ 非 0 退出（绝不假绿）",
      "备份失败：在 $PROJECT_DIR 下没有找到任何数据库文件" in script_src and "exit 3" in script_src)
H.chk("journald 容量上限 drop-in 已入库",
      os.path.exists(os.path.join(BACKEND, "ops", "journald-hn-limits.conf"))
      and "SystemMaxUse=300M" in open(os.path.join(BACKEND, "ops", "journald-hn-limits.conf"),
                                      encoding="utf-8").read())


# ============================================================
# B. 行为
# ============================================================
def _mk_db(path, tables, extra_rows=None):
    c = sqlite3.connect(path)
    for ddl in tables:
        c.execute(ddl)
    for sql, args in (extra_rows or []):
        c.execute(sql, args)
    c.commit()
    c.close()


def _seed(project_dir):
    os.makedirs(project_dir, exist_ok=True)
    _mk_db(os.path.join(project_dir, "orders.db"), [
        "CREATE TABLE IF NOT EXISTS orders (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " order_number TEXT, status TEXT, created_at TEXT)",
        "CREATE TABLE IF NOT EXISTS order_files (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " order_id INTEGER, status TEXT)",
        "CREATE TABLE IF NOT EXISTS files (id TEXT PRIMARY KEY, path TEXT)",
    ], [("INSERT INTO orders (order_number,status,created_at) VALUES (?,?,?)",
         ("HN20261023-0001", "sent", "2026-10-23 10:00:00"))])
    _mk_db(os.path.join(project_dir, "users.db"), [
        "CREATE TABLE IF NOT EXISTS users (openid TEXT PRIMARY KEY, role TEXT)",
    ], [("INSERT INTO users (openid, role) VALUES (?,?)", ("u1", "admin"))])


def _run_script(env_extra, args=()):
    env = dict(os.environ)
    env.update(env_extra)
    env["PYTHON"] = sys.executable
    return subprocess.run([BASH, BACKUP_SH, *args], cwd=BACKEND, env=env,
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=120)


def _latest_backup(backup_dir, prefix):
    names = sorted(n for n in os.listdir(backup_dir)
                   if n.startswith(prefix) and n.endswith(".db.gz"))
    return os.path.join(backup_dir, names[-1]) if names else None


def _read_backup_orders(gz_path):
    import gzip
    tmp = gz_path + ".tmpcheck"
    with gzip.open(gz_path, "rb") as f, open(tmp, "wb") as out:
        out.write(f.read())
    c = sqlite3.connect(tmp)
    rows = {r[0] for r in c.execute("select order_number from orders").fetchall()}
    c.close()
    os.remove(tmp)
    return rows


if not BASH:
    print("SKIP: 本机没有 bash（无法跑 backup.sh 的行为验收）")
    H.finish()
    sys.exit(0)

print("\n=== B. 行为（真 bash + WAL）===")
TMP = tempfile.mkdtemp(prefix="hn_backup_")
H._created.append(TMP)
proj = os.path.join(TMP, "proj")
bdir = os.path.join(TMP, "backups")
_seed(proj)

# WAL 分水岭：写入后**保持连接打开**，数据只在 -wal 里
wal_conn = sqlite3.connect(os.path.join(proj, "orders.db"))
wal_conn.execute("PRAGMA journal_mode=WAL")
wal_conn.execute("INSERT INTO orders (order_number,status,created_at) VALUES (?,?,?)",
                 ("HN-WAL-9999", "sent", "2026-10-23 22:59:00"))
wal_conn.commit()
wal_exists = os.path.exists(os.path.join(proj, "orders.db-wal"))
H.chk("前置：WAL 文件已产生（否则本用例没有判别力）", wal_exists)

r = _run_script({"PROJECT_DIR": proj, "BACKUP_DIR": bdir})
H.chk("备份脚本退出码 0", r.returncode == 0, (r.returncode, r.stderr[-400:]))
orders_bk = _latest_backup(bdir, "orders_")
users_bk = _latest_backup(bdir, "users_")
H.chk("orders 备份已产出（gz）", orders_bk is not None)
H.chk("users.db 也被备份（修复前完全没备）", users_bk is not None)
if orders_bk:
    rows = _read_backup_orders(orders_bk)
    H.chk("★ 备份里含 WAL 中已提交、未 checkpoint 的订单（backup API 语义）",
          "HN-WAL-9999" in rows, sorted(rows))
if users_bk:
    import gzip
    tmp = users_bk + ".tmpusers"
    with gzip.open(users_bk, "rb") as f, open(tmp, "wb") as out:
        out.write(f.read())
    c = sqlite3.connect(tmp)
    got = c.execute("select openid, role from users").fetchall()
    c.close(); os.remove(tmp)
    H.chk("users 备份内容一致", got == [("u1", "admin")], got)

wal_conn.close()          # 放掉连接，后续 restore-check 不再有未 checkpoint 的写入

# 保留期：造一个 10 天前的旧备份，必须被清理；再造一个 1 天前的不许删
old_bk = os.path.join(bdir, "orders_20000101_000000.db.gz")
new_bk = os.path.join(bdir, "orders_20261022_000000.db.gz")
for p in (old_bk, new_bk):
    open(p, "wb").write(b"x")
os.utime(old_bk, (time.time() - 10 * 86400, time.time() - 10 * 86400))
os.utime(new_bk, (time.time() - 1 * 86400, time.time() - 1 * 86400))

r2 = _run_script({"PROJECT_DIR": proj, "BACKUP_DIR": bdir}, args=("--restore-check",))
out2 = (r2.stdout or "") + (r2.stderr or "")
H.chk("--restore-check 退出码 0", r2.returncode == 0, out2[-500:])
H.chk("★ 恢复演练输出 RESTORE_CHECK=ok", "RESTORE_CHECK=ok" in out2, out2[-300:])
H.chk("超过保留期的旧备份被清理", not os.path.exists(old_bk))
H.chk("未过期的备份保留", os.path.exists(new_bk))

# 空目录（没有任何库）必须失败：这是"假绿"的守卫
empty_proj = os.path.join(TMP, "emptyproj")
os.makedirs(empty_proj, exist_ok=True)
r_empty = _run_script({"PROJECT_DIR": empty_proj, "BACKUP_DIR": os.path.join(TMP, "b_empty")},
                      args=("--restore-check",))
out_empty = (r_empty.stdout or "") + (r_empty.stderr or "")
H.chk("★ 没有任何库文件 ⇒ 非 0 退出且不给 RESTORE_CHECK=ok（防止空跑假绿）",
      r_empty.returncode != 0 and "RESTORE_CHECK=ok" not in out_empty, out_empty[-300:])

# 缺省 PROJECT_DIR：把脚本放进"库所在目录"，不传 PROJECT_DIR 也必须能备到
proj3 = os.path.join(TMP, "proj3")
_seed(proj3)
shutil.copy(BACKUP_SH, os.path.join(proj3, "backup.sh"))
env3 = dict(os.environ)
env3["PYTHON"] = sys.executable
env3.pop("PROJECT_DIR", None)
r3b = subprocess.run([BASH, os.path.join(proj3, "backup.sh")], cwd=proj3, env=env3,
                     capture_output=True, text=True, encoding="utf-8", errors="replace",
                     timeout=120)
H.chk("★ 不传 PROJECT_DIR ⇒ 用脚本所在目录（备份到 proj3/backups）",
      r3b.returncode == 0 and any(n.startswith("orders_") for n in
                                  os.listdir(os.path.join(proj3, "backups"))
                                  if os.path.isdir(os.path.join(proj3, "backups"))),
      (r3b.returncode, (r3b.stdout or "")[-200:]))

# ============================================================
# C. 变异自证：备份动作换回 cp
# ============================================================
print("\n=== C. 变异自证（改用 cp 备份）===")
mut_sh = os.path.join(TMP, "backup_cp.sh")
src = script_src
start = src.find('  "$PYTHON" - "$SRC" "$OUT" <<\'PY\'')
end = src.find("PY\n", src.find("dst.close(); src.close()", start)) + 3 if start > 0 else -1
if start < 0 or end <= 0:
    H.chk("★ 变异锚点可定位（否则本节的判别力不成立）", False, (start, end))
else:
    mutated = src[:start] + '  cp "$SRC" "$OUT"\n' + src[end:]
    open(mut_sh, "w", encoding="utf-8", newline="\n").write(mutated)
    proj2 = os.path.join(TMP, "proj2")
    bdir2 = os.path.join(TMP, "backups2")
    _seed(proj2)
    c2 = sqlite3.connect(os.path.join(proj2, "orders.db"))
    c2.execute("PRAGMA journal_mode=WAL")
    c2.execute("INSERT INTO orders (order_number,status,created_at) VALUES (?,?,?)",
               ("HN-WAL-MUT", "sent", "2026-10-23 23:00:00"))
    c2.commit()
    env = dict(os.environ)
    env.update({"PROJECT_DIR": proj2, "BACKUP_DIR": bdir2, "PYTHON": sys.executable})
    r3 = subprocess.run([BASH, mut_sh], cwd=BACKEND, env=env, capture_output=True,
                        text=True, encoding="utf-8", errors="replace", timeout=120)
    c2.close()
    bk = _latest_backup(bdir2, "orders_")
    got = _read_backup_orders(bk) if bk else set()
    H.chk("★ 变异（cp 备份）⇒ 备份里缺 WAL 行（判据能红）",
          "HN-WAL-MUT" not in got, (r3.returncode, sorted(got)))

H.finish()
