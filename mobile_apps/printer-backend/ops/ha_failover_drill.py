"""ha_failover_drill.py — S3-03 高可用/容灾**非破坏性**切换演练（纯 Python，Windows 可跑）

用途
----
在不碰生产库、不占生产端口的前提下回答一个问题：

> 主机没了以后，把最后一次快照恢复出来、把备站起起来、到 `/api/ping` 返回 200，
> **要多久（RTO）**？而这份数据离故障时刻有多旧（**RPO**）？

流程（每一步都有判据，报告里逐条列出）：

1. **源库指纹**：`--source-dir` 下的 `orders.db` / `users.db` 只读计算 SHA-256（演练后必须一致）；
2. **快照**：用 SQLite 在线 backup API 现打一份（`src.backup(dst)`，WAL 里未 checkpoint 的写入
   也包含），或直接用 `--snapshot-dir` 里现成的快照（真实切演练应指向 `ha_standby_sync.sh`
   拉下来的 `$DEST/current` / `incoming/<ts>`）；
3. **恢复**：把快照里的两个库装进 `--workdir/standby`（顺带把后端源码与 uploads 也放进去），
   做 `integrity_check` / `foreign_key_check` / 逐表行数 —— 这段时间记 `restore_sec`；
4. **起备站**：`--workdir/standby` 里用**子进程** `werkzeug.serving.run_simple` 起 app
   （`app.init_db()` 后随机空闲端口，绝不碰 5000），轮询 `/api/ping` 到 200 —— 记 `boot_sec`；
5. **对账**：线上（备站）行数与快照一致、最新订单 id 一致、uploads 引用存在性（可选）；
6. **源库 SHA-256 不变**（演练是只读的，源库被改过就说明这场演练不可信）；
7. **判据**：`rto_min = (restore_sec + boot_sec) / 60 ≤ --target-rto-min`；
   `rpo_min = (disaster_at - 数据时刻) / 60 ≤ --target-rpo-min`，数据时刻取
   `--last-sync-at`（缺省 = 快照时间）——**手工把它设成两小时前**就能构造 RPO 超标场景，
   这是"备机同步断了但没人发现"的日常形态；
8. JSON 报告（契约字段见 README 段落）+ 退出码 0/1/2。

为什么用子进程而不是 `test_client()`
------------------------------------
`app.test_client()` 不经过真实的 socket/服务器启动路径：测不出"进程起来到能服务"的真实
时间（RTO 的分母），也测不出 `init_db()` 在恢复出来的库上重放的耗时。子进程 + 真 HTTP 轮询
才是"备站真的能接客"的证据。

⚠ `HN_DRILL_MODE=1`
-------------------
演练副本里 uploads 可能是空的/不完整的。`app.py` 见到该变量会跳过**启动时的磁盘幽灵清理**与
**MD5 索引补全**（两者都会按"物理文件是否存在"删/改 `files` 行，让行数比对失去意义）。
生产/正常启动**不会**设置它。`--require-uploads` 时本脚本自己逐条核对 `files.path` 在
`uploads/` 下真的存在，缺文件就是**失败**（不会因为跳过启动清理就假绿）。

⚠ 本脚本怎么碰源库（只读，且不往源目录扔东西）
-----------------------------------------------
源库（`--source-dir` / `--snapshot-dir` 里的 db）一律**先连同 `-wal`/`-shm` 拷到临时目录**
再打开（`_open_db_copy`）：`mode=ro` 直接开 WAL 库时 SQLite 仍然会在**源目录**建 `-shm`
（本地实测留下 `orders.db-shm` + 空 `orders.db-wal`），演练不该往生产目录扔垃圾，也不该去抢
线上库的锁；`-wal` 一起拷 ⇒ 未 checkpoint 的写入不丢。`source_unchanged`（SHA-256 前后一致）
是这条纪律的机器判据。

用法
----
    # 本机（对着一个临时副本，最安全）
    python mobile_apps/printer-backend/ops/ha_failover_drill.py \\
        --source-dir /tmp/hn_copy --workdir /tmp/hn_drill \\
        --report ops/reports/ha_failover_$(date +%Y%m%d_%H%M%S).json

    # 用备机已经同步好的快照（真实切换演练）
    python ops/ha_failover_drill.py --source-dir /srv/hn-standby/current \\
        --snapshot-dir /srv/hn-standby/current --require-uploads \\
        --last-sync-at "$(date -d '-2 hours' -Iseconds)"   # 构造 RPO 超标场景

退出码：0 = 全部判据通过；1 = 有判据失败（报告里写明哪一条、为什么）；2 = 参数/IO/环境错误。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

# 外键违例分类与 S3-02 迁移演练共用同一份口径（唯一真相 ops/db_fk_policy.py）：
# 生产 228 条 orders.file_id→files.id 是文件到期清理后的历史引用，不是损坏；
# 其它组合仍必须判失败。脚本以绝对路径被调用时，sys.path[0] 未必是 ops/，显式补一下。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from db_fk_policy import BENIGN_FK_RULES, classify_fk_violations, warning_text  # noqa: E402

DB_NAMES = ("orders.db", "users.db")
# 只对这些表做行数比对：`init_db()` 在恢复出来的库上重放迁移时会新增表/列，
# 拿"全部表"去比会被新表误判成不一致（迁移只加不删，业务表行数必须原样）。
KEY_TABLES = ("orders", "order_files", "files", "users", "license_keys", "finance_config")
# 复制后端源码时排除的东西：库/uploads/日志/密钥/缓解产物都在这里被排除，
# 恢复进 standby 的库只来自快照（这样"演练用的数据" = "快照里的数据"）
SOURCE_IGNORE = shutil.ignore_patterns(
    "__pycache__", "config.py", "config.py.example", "*.db", "*.db-wal", "*.db-shm",
    "uploads", "logs", "*.bak*", ".git", "venv", "node_modules", "backups", "*.pyc")

BOOT_CODE = r'''
# 备站启动器（演练用）：cwd 已是 standby 目录，只做 init_db + run_simple
import os
import sys

port = int(sys.argv[1])
sys.path.insert(0, os.getcwd())
import app as backend          # noqa: E402  （必须 cwd 先进 sys.path）

backend.init_db()
print("[DRILL] init_db done", flush=True)
from werkzeug.serving import run_simple   # noqa: E402

print("[DRILL] serving on 127.0.0.1:%d" % port, flush=True)
run_simple("127.0.0.1", port, backend.app, threaded=True,
           use_reloader=False, use_debugger=False, use_evalex=False)
'''


# ────────────────────────── 小工具 ──────────────────────────

def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _md5(path: str) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt.isoformat(timespec="seconds")


def _parse_iso(text: str, what: str) -> datetime:
    """接受 2026-10-24T21:00:00+08:00 / 2026-10-24 21:00:00 / 2026-10-24（按本地时区）。"""
    raw = (text or "").strip()
    if not raw:
        raise ValueError("%s 为空" % what)
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as e:
        raise ValueError("%s 不是 ISO 时间：%r（%s）" % (what, text, e)) from e
    if dt.tzinfo is None:
        dt = dt.astimezone()          # 无时区 = 本机本地时间
    return dt


def _open_db_copy(path: str):
    """把 db（连同 `-wal`/`-shm`）拷到临时目录再打开，返回 (connection, cleanup_dir)。

    为什么不直接开源库：
      · `mode=ro` 打开 WAL 库时 SQLite 仍会在**源目录**建 `-shm`（本地实测会留下
        `orders.db-shm` / 空 `orders.db-wal`），演练不该往生产目录里扔垃圾；
      · 直接开还会拿共享锁，线上有写在跑时可能撞 `database is locked`；
      · 拷 `-wal`/`-shm` 一起拷，WAL 里未 checkpoint 的写入不会丢。
    ⚠ 代价：这是"热拷贝"，源库正在写时拷到的是某个瞬间的近似状态。**生产演练请优先
      用 `--snapshot-dir`（`ha_snapshot.sh` 用在线 backup API 产出的快照）**，源目录只给
      副本/静态目录用。
    """
    tmp = tempfile.mkdtemp(prefix="ha_drill_dbsrc_")
    dst = os.path.join(tmp, os.path.basename(path))
    shutil.copy2(path, dst)
    for suffix in ("-wal", "-shm"):
        if os.path.exists(path + suffix):
            shutil.copy2(path + suffix, dst + suffix)
    return sqlite3.connect(dst, timeout=30), tmp


def _inspect_db(path: str) -> dict:
    """只读体检：integrity / foreign_key / journal / 逐表行数 / 最新订单 id / files 路径列表。

    任何一步失败都**不抛**——记录到 error 里交给判据处理（坏库必须"红得可解释"，而不是
    让脚本崩掉，否则报告里就没有证据了）。
    """
    out = {"path": path, "ok": False, "error": "", "integrity_check": "",
           "foreign_key_violations": -1, "foreign_key_detail": {},
           "journal_mode": "", "tables": {},
           "rows": 0, "latest_order_id": None, "files_paths": []}
    if not os.path.isfile(path):
        out["error"] = "文件不存在"
        return out
    conn = None
    cleanup = None
    try:
        conn, cleanup = _open_db_copy(path)
        out["integrity_check"] = conn.execute("PRAGMA integrity_check").fetchone()[0]
        fk = classify_fk_violations(conn, BENIGN_FK_RULES)
        out["foreign_key_violations"] = fk.get("total", -1)
        out["foreign_key_detail"] = fk
        try:
            out["journal_mode"] = conn.execute("PRAGMA journal_mode").fetchone()[0]
        except sqlite3.Error:
            out["journal_mode"] = ""
        names = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
            "ORDER BY name")]
        counts = {}
        for t in names:
            try:
                counts[t] = int(conn.execute(
                    'SELECT COUNT(*) FROM "%s"' % t.replace('"', '""')).fetchone()[0])
            except sqlite3.Error:
                counts[t] = -1
        out["tables"] = counts
        out["rows"] = sum(v for v in counts.values() if v > 0)
        if "orders" in counts:
            try:
                out["latest_order_id"] = conn.execute(
                    "SELECT COALESCE(MAX(id), 0) FROM orders").fetchone()[0]
            except sqlite3.Error:
                out["latest_order_id"] = None
        if "files" in counts:
            try:
                out["files_paths"] = [r[0] for r in conn.execute(
                    "SELECT path FROM files WHERE path IS NOT NULL AND path <> ''")]
            except sqlite3.Error:
                out["files_paths"] = []
        out["ok"] = True
    except sqlite3.Error as e:
        out["error"] = "sqlite: %s: %s" % (type(e).__name__, e)
    except OSError as e:
        out["error"] = "io: %s: %s" % (type(e).__name__, e)
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
        if cleanup:
            shutil.rmtree(cleanup, ignore_errors=True)
    return out


def _integrity_ok(info: dict) -> bool:
    """**完整性判据的唯一落点**（两处体检都用它：快照侧 + 恢复侧）。

    单独抽出来是为了它能被变异自证：把它改成 `return True`，坏库用例必须由红转绿
    （tests/verify_ha_drill.py 的 C 节）。
    """
    return bool(info.get("ok")) and info.get("integrity_check") == "ok"


def _online_backup(src: str, dst_path: str) -> str:
    """SQLite 在线 backup API 打一份快照（与 backup.sh / ha_snapshot.sh 同口径）。"""
    conn, cleanup = _open_db_copy(src)
    try:
        out = sqlite3.connect(dst_path)
        try:
            with out:
                conn.backup(out)
        finally:
            out.close()
    finally:
        conn.close()
        if cleanup:
            shutil.rmtree(cleanup, ignore_errors=True)
    return dst_path


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])
    finally:
        s.close()


def _ping(url: str, timeout: float = 2.0) -> tuple:
    """返回 (ok, status, body_tail)。连接阶段失败返回 (False, 0, 原因)。"""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            body = resp.read(200)
            return (resp.status == 200, int(resp.status),
                    body.decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        return (False, int(e.code), str(e)[:200])
    except Exception as e:  # noqa: BLE001  （连接被拒/超时都算"还没起来"）
        return (False, 0, "%s: %s" % (type(e).__name__, e))


# ────────────────────────── 主流程 ──────────────────────────

class Drill:
    def __init__(self, args):
        self.args = args
        self.checks: list = []
        self.reasons: list = []
        self.warnings: list = []
        self.row_counts: dict = {}

    # -- 判据 ------------------------------------------------------------
    def chk(self, name: str, ok: bool, detail: str = "") -> bool:
        ok = bool(ok)
        self.checks.append({"name": name, "ok": ok, "detail": detail})
        if not ok and detail:
            self.reasons.append("%s: %s" % (name, detail))
        return ok

    def _fail_report(self, reason: str) -> dict:
        return {
            "ok": False,
            "tool": "ha_failover_drill.py",
            "generated_at": _iso(datetime.now(timezone.utc).astimezone()),
            "disaster_at": self.args.disaster_at or "",
            "snapshot_at": "",
            "rto_min": 0.0,
            "rpo_min": 0.0,
            "targets": {"rto_min": self.args.target_rto_min,
                        "rpo_min": self.args.target_rpo_min},
            "restore_sec": 0.0,
            "boot_sec": 0.0,
            "ready_sec": 0.0,
            "checks": self.checks,
            "row_counts": self.row_counts,
            "source_unchanged": False,
            "reason": reason,
            "reasons": [reason],
            "error": reason,
        }

    # -- 主流程 ----------------------------------------------------------
    def run(self) -> dict:
        a = self.args
        workdir = os.path.abspath(a.workdir)
        os.makedirs(workdir, exist_ok=True)

        src_dir = os.path.abspath(a.source_dir)
        if not os.path.isdir(src_dir):
            raise FileNotFoundError("--source-dir 不是目录：%s" % src_dir)
        src_dbs = {n: os.path.join(src_dir, n) for n in DB_NAMES
                   if os.path.isfile(os.path.join(src_dir, n))}
        if "orders.db" not in src_dbs:
            raise FileNotFoundError("--source-dir 下没有 orders.db：%s" % src_dir)

        # 0) 演练计时起点 + 源库指纹
        t_start = time.perf_counter()
        disaster_at = _parse_iso(a.disaster_at, "--disaster-at") if a.disaster_at else None
        if disaster_at is None:
            disaster_at = datetime.now(timezone.utc).astimezone()
        src_sha_before = {n: _sha256(p) for n, p in src_dbs.items()}

        # 1) 快照（现打 or 复用）
        snap_dir = os.path.abspath(a.snapshot_dir) if a.snapshot_dir else None
        if snap_dir:
            if not os.path.isdir(snap_dir):
                raise FileNotFoundError("--snapshot-dir 不是目录：%s" % snap_dir)
            snap_dbs = {n: os.path.join(snap_dir, n) for n in DB_NAMES
                        if os.path.isfile(os.path.join(snap_dir, n))}
            if "orders.db" not in snap_dbs:
                raise FileNotFoundError("--snapshot-dir 下没有 orders.db：%s" % snap_dir)
            meta_path = os.path.join(snap_dir, "last_snapshot.json")
            meta = {}
            if os.path.isfile(meta_path):
                try:
                    with open(meta_path, "r", encoding="utf-8") as f:
                        meta = json.load(f)
                except (OSError, ValueError) as e:
                    self.warnings.append("last_snapshot.json 不可读：%s" % e)
            if meta.get("ts"):
                snapshot_at = datetime.fromtimestamp(float(meta["ts"]), tz=timezone.utc).astimezone()
            else:
                snapshot_at = datetime.fromtimestamp(
                    os.path.getmtime(snap_dbs["orders.db"]), tz=timezone.utc).astimezone()
            self.chk("snapshot_present", True, "复用现成快照：%s" % snap_dir)
            snap_method = "snapshot-dir"
            # 用快照里的元数据 md5 顺带验一下快照没被动过（有就比，没有跳过）
            snap_meta_dbs = meta.get("dbs") or {}
            if snap_meta_dbs:
                bad = []
                for n, p in snap_dbs.items():
                    want = (snap_meta_dbs.get(n) or {}).get("md5")
                    if want and _md5(p) != want:
                        bad.append(n)
                self.chk("snapshot_md5_ok", not bad,
                         "" if not bad else "快照文件与 last_snapshot.json 记录的 md5 不一致：%s" % bad)
        else:
            snap_dir = os.path.join(workdir, "snapshot")
            os.makedirs(snap_dir, exist_ok=True)
            snap_dbs = {}
            try:
                for n, p in src_dbs.items():
                    snap_dbs[n] = _online_backup(p, os.path.join(snap_dir, n))
                self.chk("snapshot_created", True, "SQLite backup API → %s" % snap_dir)
            except (sqlite3.Error, OSError) as e:
                self.chk("snapshot_created", False,
                         "backup API 打快照失败（源库损坏/不可读？）%s: %s"
                         % (type(e).__name__, e))
                return self._abort("快照失败，无法继续演练")
            snapshot_at = datetime.now(timezone.utc).astimezone()
            snap_method = "backup-api"
            # 自己写一份与 ha_snapshot.sh 同字段的元数据（留档用，不是运行依赖）
            try:
                payload = {
                    "version": 1,
                    "tool": "ha_failover_drill.py",
                    "ts": int(snapshot_at.timestamp()),
                    "ts_readable": snapshot_at.strftime("%Y-%m-%d %H:%M:%S"),
                    "dbs": {n: {"md5": _md5(os.path.join(snap_dir, n))} for n in snap_dbs},
                }
                with open(os.path.join(snap_dir, "last_snapshot.json"), "w", encoding="utf-8") as f:
                    json.dump(payload, f, ensure_ascii=False, indent=2)
            except OSError as e:
                self.warnings.append("写演练快照元数据失败：%s" % e)

        # 2) 期望值（= 快照里有什么）：行数 / 最新订单
        snap_info = {n: _inspect_db(p) for n, p in snap_dbs.items()}
        for n, info in snap_info.items():
            self.row_counts.setdefault(n, {})["expected"] = {
                "tables": info.get("tables") or {},
                "rows": info.get("rows"),
                "integrity_check": info.get("integrity_check"),
            }
        snap_ok = all(_integrity_ok(i) for i in snap_info.values())
        self.chk("snapshot_integrity_ok", snap_ok,
                 "" if snap_ok else "快照里已有损坏：%s" % {
                     n: (i.get("integrity_check") or i.get("error")) for n, i in snap_info.items()})

        # 3) 恢复进 workdir/standby（这段计入 restore_sec）
        restore_t0 = time.perf_counter()
        standby = os.path.join(workdir, "standby")
        if os.path.isdir(standby):
            shutil.rmtree(standby, ignore_errors=True)
        backend_dir = self._resolve_backend(src_dir)
        try:
            shutil.copytree(backend_dir, standby, ignore=SOURCE_IGNORE, dirs_exist_ok=True)
        except OSError as e:
            raise RuntimeError("复制后端源码到 standby 失败（%s）：%s" % (backend_dir, e)) from e
        os.makedirs(os.path.join(standby, "uploads"), exist_ok=True)
        os.makedirs(os.path.join(standby, "logs"), exist_ok=True)
        for n, p in snap_dbs.items():
            shutil.copy2(p, os.path.join(standby, n))
            for suffix in ("-wal", "-shm"):
                if os.path.exists(p + suffix):        # 快照一般没有；有就一起带上（一致性）
                    shutil.copy2(p + suffix, os.path.join(standby, n + suffix))

        # uploads：--uploads-dir 给了就原地核对（备机上已 rsync 好的那份），否则从源目录复制
        uploads_check_dir = ""
        if a.uploads_dir:
            uploads_check_dir = os.path.abspath(a.uploads_dir)
            self.chk("uploads_available", os.path.isdir(uploads_check_dir),
                     "" if os.path.isdir(uploads_check_dir)
                     else "--uploads-dir 不存在：%s" % uploads_check_dir)
        else:
            up_src = os.path.join(src_dir, "uploads")
            if os.path.isdir(up_src):
                size_mb = 0.0
                for root, _dirs, fs in os.walk(up_src):
                    for f in fs:
                        fp = os.path.join(root, f)
                        if os.path.isfile(fp):
                            try:
                                size_mb += os.path.getsize(fp) / (1024.0 * 1024.0)
                            except OSError:
                                pass
                if size_mb > a.uploads_max_mb:
                    self.chk("uploads_available", False,
                             "源 uploads 有 %.1f MB > --uploads-max-mb=%s，为免演练把磁盘写满而"
                             "不复制；要核对就在备机上用 --uploads-dir 指向已同步的 uploads/"
                             % (size_mb, a.uploads_max_mb))
                else:
                    dst_up = os.path.join(standby, "uploads")
                    shutil.copytree(up_src, dst_up, dirs_exist_ok=True)
                    uploads_check_dir = dst_up
                    self.chk("uploads_available", True, "已复制 %.1f MB uploads" % size_mb)
            else:
                self.chk("uploads_available", not a.require_uploads,
                         "" if not a.require_uploads
                         else "--require-uploads 但源目录没有 uploads/（也没有 --uploads-dir）")

        # 恢复后的体检（在 standby 上，只读）
        restore_info = {n: _inspect_db(os.path.join(standby, n)) for n in snap_dbs}
        restore_sec = time.perf_counter() - restore_t0

        integrity_ok = True
        for n, info in restore_info.items():
            ok = _integrity_ok(info)
            if not ok:
                integrity_ok = False
            self.chk("integrity_%s" % n, ok,
                     "" if ok else "%s: integrity_check=%r error=%r"
                     % (n, info.get("integrity_check"), info.get("error")))
        # 外键：与 S3-02 同一套分类口径 —— 良性（文件到期后的 orders.file_id 历史引用）
        # 计入 warnings 不判失败；非良性（其它 table→parent）必须判失败。
        fk_bad = []
        for n, info in restore_info.items():
            detail = info.get("foreign_key_detail") or {}
            if detail.get("total", -1) == -1:
                continue  # PRAGMA 自身失败：坏库由 integrity 判据负责，不在这里重复归因
            if detail.get("unexpected"):
                fk_bad.append("%s: unexpected=%s %s"
                              % (n, detail.get("unexpected"), detail.get("detail")))
            warn = warning_text(detail)
            if warn:
                self.warnings.append(warn)
        fk_ok = not fk_bad
        self.chk("foreign_key_ok", fk_ok,
                 "" if fk_ok else "恢复后存在非良性外键违规：" + "；".join(fk_bad))

        # 逐表行数（只比业务表/关键表：init_db 之后会多出新表）
        rows_ok = True
        for n in snap_dbs:
            exp = (snap_info[n].get("tables") or {})
            got = (restore_info[n].get("tables") or {})
            for t in KEY_TABLES:
                if t in exp and exp.get(t, -1) >= 0:
                    if got.get(t) != exp[t]:
                        rows_ok = False
                        self.reasons.append(
                            "row_counts_%s.%s: 恢复后 %s 行 vs 快照 %s 行"
                            % (n, t, got.get(t), exp[t]))
            self.row_counts[n]["actual_before_boot"] = {
                t: got.get(t) for t in KEY_TABLES if t in got}
        self.chk("row_counts_match", rows_ok,
                 "" if rows_ok else "恢复后的行数与快照不一致（见 reasons）")

        orders_expected = 0
        for n in snap_dbs:
            t = snap_info[n].get("tables") or {}
            orders_expected += max(0, int(t.get("orders") or 0))
        if orders_expected <= 0 and not a.allow_empty:
            self.chk("has_business_data", False,
                     "快照里 orders 表 0 行（或读不出来）—— 这场演练只验证了 schema。"
                     "确认是空库场景请显式加 --allow-empty")
        else:
            self.chk("has_business_data", True,
                     "" if orders_expected > 0 else "空库（--allow-empty 放行）")

        # 4) 起备站（完整性不过就提前收手：坏库起不来，别浪费时间等超时）
        boot_sec = 0.0
        boot_ok = False
        ready_sec = restore_sec
        ping_info = {"url": "", "status": 0, "body": "", "attempts": 0, "error": ""}
        latest_after = {}
        if not integrity_ok:
            self.chk("standby_boot_ok", False,
                     "完整性失败 ⇒ 不起备站（坏库起不来，RTO 无从谈起）")
        else:
            boot_ok, boot_sec, ping_info = self._boot_and_wait(standby)
            ready_sec = restore_sec + boot_sec
            self.chk("standby_boot_ok", boot_ok,
                     "" if boot_ok else "备站 %ss 内没有返回 /api/ping 200（%s）"
                     % (a.boot_timeout, ping_info.get("error") or ping_info.get("status")))
            # 备站起来之后再体检一次（app.init_db 已重放迁移）：对账用这一份
            after = {n: _inspect_db(os.path.join(standby, n)) for n in snap_dbs}
            for n, info in after.items():
                self.row_counts[n]["actual"] = {
                    "tables": info.get("tables") or {},
                    "rows": info.get("rows"),
                    "latest_order_id": info.get("latest_order_id"),
                }
                latest_after[n] = info.get("latest_order_id")
            live_ok = True
            for n in snap_dbs:
                exp = snap_info[n].get("tables") or {}
                got = after[n].get("tables") or {}
                for t in KEY_TABLES:
                    if t in exp and exp.get(t, -1) >= 0 and got.get(t) != exp[t]:
                        live_ok = False
                        self.reasons.append("live_row_counts_%s.%s: 备站 %s vs 快照 %s"
                                            % (n, t, got.get(t), exp[t]))
            self.chk("live_row_counts_match", live_ok,
                     "" if live_ok else "备站起来后行数与快照不一致（init_db 改动过数据？）")
            if "orders.db" in snap_dbs:
                exp_latest = snap_info["orders.db"].get("latest_order_id")
                got_latest = latest_after.get("orders.db")
                self.chk("latest_order_match", exp_latest == got_latest,
                         "" if exp_latest == got_latest
                         else "最新订单 id：快照=%r 备站=%r" % (exp_latest, got_latest))

        # 5) uploads 引用存在性（--require-uploads 时缺文件 = 失败）
        if uploads_check_dir and os.path.isdir(uploads_check_dir):
            missing = []
            checked = 0
            for n in snap_dbs:
                for rel in (snap_info[n].get("files_paths") or []):
                    checked += 1
                    p = os.path.join(uploads_check_dir, rel)
                    if not os.path.isfile(p):
                        missing.append(rel)
            self.row_counts.setdefault("uploads", {}).update(
                {"checked": checked, "missing": len(missing),
                 "dir": uploads_check_dir})
            if a.require_uploads:
                self.chk("uploads_referenced_present", not missing,
                         "" if not missing else
                         "%d/%d 个 files.path 在 uploads 里找不到（例：%s）"
                         % (len(missing), checked, missing[:3]))
            elif missing:
                self.warnings.append("uploads 里有 %d/%d 个引用缺文件（未加 --require-uploads，"
                                     "不作为判据）" % (len(missing), checked))

        # 6) 源库没被改过（演练必须只读）
        src_sha_after = {n: _sha256(p) for n, p in src_dbs.items()}
        unchanged = src_sha_before == src_sha_after
        self.chk("source_unchanged", unchanged,
                 "" if unchanged else "源库在演练期间被修改了（SHA-256 变化）——演练结果不可信")

        # 7) RTO / RPO
        rto_min = ready_sec / 60.0
        last_sync_at = (_parse_iso(a.last_sync_at, "--last-sync-at")
                        if a.last_sync_at else snapshot_at)
        # 备机里的数据不可能比快照更新：取更旧的那个当"数据时刻"（乐观取值会让 RPO 假达标）
        data_at = min(last_sync_at, snapshot_at)
        rpo_min = max(0.0, (disaster_at - data_at).total_seconds() / 60.0)
        self.chk("rto_within_target", rto_min <= a.target_rto_min,
                 "" if rto_min <= a.target_rto_min else
                 "rto_min=%.2f > %.2f（restore=%.1fs boot=%.1fs）"
                 % (rto_min, a.target_rto_min, restore_sec, boot_sec))
        self.chk("rpo_within_target", rpo_min <= a.target_rpo_min,
                 "" if rpo_min <= a.target_rpo_min else
                 "rpo_min=%.2f > %.2f（数据时刻=%s，故障=%s；--last-sync-at 可构造该场景）"
                 % (rpo_min, a.target_rpo_min, _iso(data_at), _iso(disaster_at)))

        ok = all(c["ok"] for c in self.checks)
        report = {
            "ok": ok,
            "tool": "ha_failover_drill.py",
            "version": 1,
            "generated_at": _iso(datetime.now(timezone.utc).astimezone()),
            "disaster_at": _iso(disaster_at),
            "snapshot_at": _iso(snapshot_at),
            "last_sync_at": _iso(data_at),
            "rto_min": round(rto_min, 4),
            "rpo_min": round(rpo_min, 4),
            "targets": {"rto_min": a.target_rto_min, "rpo_min": a.target_rpo_min},
            "restore_sec": round(restore_sec, 3),
            "boot_sec": round(boot_sec, 3),
            "ready_sec": round(ready_sec, 3),
            "checks": self.checks,
            "row_counts": self.row_counts,
            "source_unchanged": bool(unchanged),
            "source": {n: {"path": p, "sha256_before": src_sha_before[n],
                           "sha256_after": src_sha_after[n],
                           "size": os.path.getsize(p)} for n, p in src_dbs.items()},
            "snapshot": {"method": snap_method, "dir": snap_dir,
                         "dbs": sorted(snap_dbs.keys()),
                         "integrity": {n: i.get("integrity_check") for n, i in snap_info.items()}},
            "standby": {"dir": standby, "backend_dir": backend_dir,
                        "boot_ok": boot_ok, "ping": ping_info,
                        "latest_order_id": latest_after},
            "uploads": {"required": bool(a.require_uploads),
                        "check_dir": uploads_check_dir},
            "warnings": self.warnings,
            "reasons": self.reasons,
            "reason": self.reasons[0] if self.reasons else "",
            "workdir": workdir,
            "elapsed_sec": round(time.perf_counter() - t_start, 3),
        }
        # 完整性失败/未起站时，rto_min 只是"恢复耗时"，别让人误读成达标
        if not boot_ok:
            report["rto_min"] = round(max(rto_min, 0.0), 4)
        return report

    # -- 起备站 + 轮询 ---------------------------------------------------
    def _boot_and_wait(self, standby: str):
        a = self.args
        port = _free_port()
        url = "http://127.0.0.1:%d/api/ping" % port
        log_path = os.path.join(os.path.dirname(standby), "standby-boot.log")
        env = dict(os.environ)
        env.update({
            "PYTHONPATH": "",
            "PYTHONUTF8": "1",
            "PYTHONUNBUFFERED": "1",
            "FLASK_ENV": "development",     # 允许内置默认 SECRET_KEY（演练不碰真实凭据）
            "HN_DRILL_MODE": "1",           # app.py：跳过启动清理/MD5 索引（见模块 docstring）
        })
        # 别把生产配置/运行时开关带进演练副本
        for key in ("HN_CONFIG_PATH", "HN_RUNTIME"):
            env.pop(key, None)

        t0 = time.perf_counter()
        log_f = open(log_path, "w", encoding="utf-8", errors="replace")
        proc = subprocess.Popen([sys.executable, "-c", BOOT_CODE, str(port)],
                                cwd=standby, env=env, stdout=log_f,
                                stderr=subprocess.STDOUT)
        ping = {"url": url, "status": 0, "body": "", "attempts": 0, "error": ""}
        deadline = t0 + float(a.boot_timeout)
        ok = False
        boot_sec = 0.0
        try:
            while time.perf_counter() < deadline:
                if proc.poll() is not None:
                    ping["error"] = "备站进程提前退出（exit=%s）" % proc.returncode
                    break
                ping["attempts"] += 1
                good, status, body = _ping(url)
                ping["status"], ping["body"] = status, body[:200]
                if good:
                    ok = True
                    boot_sec = time.perf_counter() - t0      # 只看"到第一个 200"的时间
                    break
                time.sleep(0.25)
            if not ok and not ping["error"]:
                ping["error"] = "%ss 超时（最后状态 %s）" % (a.boot_timeout, ping["status"])
        finally:
            if not ok:
                boot_sec = time.perf_counter() - t0
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:  # pragma: no cover
                    proc.kill()
            log_f.close()
        # 失败时把备站日志尾巴塞进 ping.error，方便一眼看出是 init_db 崩还是端口问题
        if not ok:
            try:
                with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                    tail = [ln for ln in f.read().splitlines() if ln.strip()][-6:]
                if tail:
                    ping["error"] += " | 备站日志：" + " ⏎ ".join(tail)[-600:]
            except OSError:
                pass
        ping["log"] = log_path
        return ok, boot_sec, ping

    # -- 源码定位 --------------------------------------------------------
    def _resolve_backend(self, src_dir: str) -> str:
        a = self.args
        if a.backend_dir:
            d = os.path.abspath(a.backend_dir)
            if not os.path.isfile(os.path.join(d, "app.py")):
                raise RuntimeError("--backend-dir 下没有 app.py：%s" % d)
            return d
        # --source-dir 本身就是后端目录（生产/测试副本）时直接用它，省一次跨盘复制
        if os.path.isfile(os.path.join(src_dir, "app.py")):
            return src_dir
        d = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if not os.path.isfile(os.path.join(d, "app.py")):
            raise RuntimeError(
                "找不到后端源码：--source-dir 里没有 app.py，脚本上一级也不是后端目录；"
                "请用 --backend-dir 指定")
        return d

    def _abort(self, reason: str) -> dict:
        self.chk("drill_completed", False, reason)
        return self._fail_report(reason)


# ────────────────────────── CLI ──────────────────────────

def _write_report(path: str, report: dict) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = path + ".tmp.%d" % os.getpid()
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _print_summary(report: dict) -> None:
    print("")
    print("[DRILL] 切换演练 %s" % ("通过 ✅" if report.get("ok") else "未通过 ❌"))
    for c in report.get("checks") or []:
        print("  %s %s%s" % ("[PASS]" if c.get("ok") else "[FAIL]", c.get("name"),
                             ("  -- " + c["detail"]) if (not c.get("ok") and c.get("detail")) else ""))
    print("  RTO = %s min（restore %ss + boot %ss，目标 ≤%s min）"
          % (report.get("rto_min"), report.get("restore_sec"), report.get("boot_sec"),
             (report.get("targets") or {}).get("rto_min")))
    print("  RPO = %s min（数据时刻 %s，目标 ≤%s min）"
          % (report.get("rpo_min"), report.get("last_sync_at"),
             (report.get("targets") or {}).get("rpo_min")))
    if report.get("reason"):
        print("  reason: %s" % report["reason"])
    for w in report.get("warnings") or []:
        print("  [WARN] %s" % w)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="S3-03 容灾切换演练（非破坏性：只读源库、随机端口、临时目录）")
    ap.add_argument("--source-dir", required=True,
                    help="含 orders.db/users.db（及 uploads、可选 app.py）的只读源目录")
    ap.add_argument("--snapshot-dir", default="",
                    help="用现成快照（含 orders.db + 可选 last_snapshot.json）；不给就现场 backup API 打一份")
    ap.add_argument("--workdir", default="", help="演练临时目录（默认自动建）")
    ap.add_argument("--report", default="", help="JSON 报告路径（默认 <workdir>/report.json）")
    ap.add_argument("--backend-dir", default="",
                    help="后端源码目录（默认 --source-dir 里有 app.py 就用它，否则本脚本上一级）")
    ap.add_argument("--disaster-at", default="", help="故障时刻 ISO（默认 now）")
    ap.add_argument("--last-sync-at", default="",
                    help="备机数据的时刻 ISO（默认 = 快照时间；设成两小时前可构造 RPO 超标场景）")
    ap.add_argument("--target-rto-min", type=float, default=30.0, help="RTO 目标（分钟，默认 30）")
    ap.add_argument("--target-rpo-min", type=float, default=5.0, help="RPO 目标（分钟，默认 5）")
    ap.add_argument("--require-uploads", action="store_true",
                    help="要求 files.path 指向的上传文件都真实存在（缺一个就失败）")
    ap.add_argument("--uploads-dir", default="",
                    help="就地核对这个 uploads 目录（备机上 rsync 好的那份），不复制")
    ap.add_argument("--uploads-max-mb", type=float, default=512.0,
                    help="自动复制 uploads 的体积上限（MB，默认 512；超了请用 --uploads-dir）")
    ap.add_argument("--boot-timeout", type=float, default=120.0,
                    help="等 /api/ping 200 的超时秒数（默认 120）")
    ap.add_argument("--allow-empty", action="store_true",
                    help="允许快照里没有任何订单（只验 schema 的演练）")
    args = ap.parse_args(argv)

    if not args.workdir:
        args.workdir = tempfile.mkdtemp(prefix="hn_ha_drill_")
    os.makedirs(args.workdir, exist_ok=True)
    if not args.report:
        args.report = os.path.join(args.workdir, "report.json")

    drill = Drill(args)
    try:
        report = drill.run()
    except (ValueError, FileNotFoundError, RuntimeError, OSError) as e:
        report = drill._fail_report("%s: %s" % (type(e).__name__, e))
        report["ok"] = False
        _write_report(args.report, report)
        print("[DRILL] %s" % report["reason"], file=sys.stderr)
        print("[DRILL] 报告已写入: %s" % args.report)
        return 2

    _write_report(args.report, report)
    _print_summary(report)
    print("[DRILL] 报告已写入: %s" % args.report)
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
