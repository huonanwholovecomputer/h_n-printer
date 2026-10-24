# -*- coding: utf-8 -*-
"""验收 · S3-03 高可用与容灾（滚动快照 / 备机同步 / 提级 / 切换演练 / Runbook）

依据：docs/安全加固修复计划_2026-10-23.md §S3-03（RTO ≤30 min、RPO ≤5 min、订单不丢）、
DDoS 报告 §8.4、docs/高可用与容灾Runbook_2026-10-24.md。

交付物（本脚本逐条钉住）：
  · mobile_apps/printer-backend/ops/ha_snapshot.sh      主库侧 5 分钟滚动快照（RPO ≤5 min）
  · mobile_apps/printer-backend/ops/ha_standby_sync.sh  备机侧拉取 + 校验 + 原子换向
  · mobile_apps/printer-backend/ops/ha_promote.sh       备机提级（安全备份/原子安装/冒烟/DNS 清单）
  · mobile_apps/printer-backend/ops/ha_failover_drill.py 非破坏性端到端切换演练（RTO/RPO 实测）
  · docs/高可用与容灾Runbook_2026-10-24.md              拓扑/复制矩阵/切换 7 步/回滚/边界

三段式（与本仓库其它 verify_* 一致）：
  A. 静态：脚本用 SQLite backup API（不是 cp 活动库）、integrity、原子 mv、--check 退出码 4、
     备机同步的 md5/行数校验与原子换向、提级的 pre_promote 安全备份与行数不倒退判据、
     DNS TTL 300、Runbook 的 RTO/RPO 表；
  B. 行为（真 Python + 真 SQLite + 真 HTTP + 真 bash 语法检查）：
     · 用 _sec_harness.make_copy 建临时后端 → init_db → 插 2 单/2 文件（含物理上传文件）
       → 跑 drill 子进程 ⇒ exit 0、报告契约字段齐全、行数/最新订单一致、**源库 SHA-256 未变**；
     · --last-sync-at 设 2 小时前 ⇒ exit 1 且 reason 含 rpo（RPO 超标必须红）；
     · 快照里 orders.db 被破坏 ⇒ exit 1（完整性/读失败）；
     · --require-uploads 且缺物理文件 ⇒ exit 1；
     · ha_standby_sync.sh --dry-run 只打印、不落地；三个 .sh 过 `bash -n`。
  C. 变异自证（改回"没有判据"的形态 ⇒ B 的用例必须转红）：
     · 删掉 RPO 判据 ⇒ RPO 用例从 exit 1 变 exit 0；
     · 删掉完整性判据 ⇒ 坏库用例从 exit 1 变 exit 0。

用法：PYTHONPATH= PYTHONUTF8=1 <python> tests/verify_ha_drill.py     （exit 0 = 全绿）
"""

import hashlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _sec_harness as H  # noqa: E402

H.install_cleanup_hook()
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(REPO, "mobile_apps", "printer-backend")
OPS = os.path.join(BACKEND, "ops")
SNAP_SH = os.path.join(OPS, "ha_snapshot.sh")
SYNC_SH = os.path.join(OPS, "ha_standby_sync.sh")
PROMOTE_SH = os.path.join(OPS, "ha_promote.sh")
DRILL = os.path.join(OPS, "ha_failover_drill.py")
POLICY = os.path.join(OPS, "db_fk_policy.py")   # S3-02/S3-03 共用的外键分类（唯一真相）
RUNBOOK = os.path.join(REPO, "docs", "高可用与容灾Runbook_2026-10-24.md")
PY = sys.executable
BASH = shutil.which("bash") or (
    r"C:\Program Files\Git\bin\bash.exe"
    if os.path.exists(r"C:\Program Files\Git\bin\bash.exe") else None)

ENV = dict(os.environ)
ENV["PYTHONPATH"] = ""
ENV["PYTHONUTF8"] = "1"


def _read(path):
    with open(path, "r", encoding="utf-8", newline="") as f:
        return f.read()


def _read_strip_comments(path):
    """判据必须吃代码不吃注释（注释里天然会引用被考的片段）。"""
    return "\n".join("" if ln.lstrip().startswith("#") else ln
                     for ln in _read(path).splitlines())


# ============================================================
# A. 静态
# ============================================================
print("=== A. 静态 ===")

for name, path in (("ha_snapshot.sh", SNAP_SH), ("ha_standby_sync.sh", SYNC_SH),
                   ("ha_promote.sh", PROMOTE_SH), ("ha_failover_drill.py", DRILL),
                   ("Runbook", RUNBOOK)):
    H.chk("交付物存在：%s" % name, os.path.isfile(path), path)

snap = _read(SNAP_SH) if os.path.isfile(SNAP_SH) else ""
sync_sh = _read(SYNC_SH) if os.path.isfile(SYNC_SH) else ""
promote = _read(PROMOTE_SH) if os.path.isfile(PROMOTE_SH) else ""
drill = _read(DRILL) if os.path.isfile(DRILL) else ""
runbook = _read(RUNBOOK) if os.path.isfile(RUNBOOK) else ""
policy = _read(POLICY) if os.path.isfile(POLICY) else ""

H.chk("★ 外键分类复用 ops/db_fk_policy.py（与 S3-02 同一口径；良性不判失败、异常必须失败）",
      "from db_fk_policy import" in drill and "classify_fk_violations" in drill
      and "unexpected" in drill and '"orders", "files"' in policy
      and "BENIGN_FK_RULES" in policy)

H.chk("三个 .sh 都是 set -euo pipefail（promote 额外开 -E errtrace；出错即停，不带着半成品往下跑）",
      all("uo pipefail" in s for s in (snap, sync_sh, promote))
      and "set -Eeuo pipefail" in promote,
      [("uo pipefail" in s) for s in (snap, sync_sh, promote)])

snap_code = _read_strip_comments(SNAP_SH) if os.path.isfile(SNAP_SH) else ""
H.chk("★ 快照用 SQLite 在线 backup API（src.backup(dst)），不是 cp 活动库",
      "src.backup(dst)" in snap
      and 'cp "$SRC"' not in snap_code and 'cp "$PROJECT_DIR/$db"' not in snap_code
      and "cp -f" not in snap_code.split("SQLite 在线 backup API")[0],
      "backup=%s cp_hit=%s" % ("src.backup(dst)" in snap,
                               'cp "$SRC"' in snap_code))

H.chk("快照后立刻 integrity_check，失败 exit 3（坏备份等于没备份）",
      "PRAGMA integrity_check" in snap and "exit 3" in snap
      and 'if [[ "$INTEGRITY" != "ok" ]]' in snap)

H.chk("★ 原子替换 + 保留上一版（$SNAP_DIR/prev/）：临时文件 mv 进正式名",
      'TMP="$SNAP_DIR/.ha_tmp.$db.$$"' in snap and 'mv -f "$TMP" "$SNAP_DIR/$db"' in snap
      and 'mv -f "$SNAP_DIR/$db" "$PREV_DIR/$db"' in snap)

H.chk("last_snapshot.json 字段齐全（ts/ts_readable/md5/integrity/rows/journal_mode/source_sha256）",
      all(k in snap for k in ('"ts"', '"ts_readable"', '"md5"', '"integrity"', '"rows"',
                              '"journal_mode"', '"source_sha256"', '"source_sha256_after"')))

H.chk("★ --check 新鲜度判据：超 RPO*60 ⇒ exit 4；缺失/负 age 也算陈旧",
      "--check" in snap and "CHECK=stale" in snap and "raise SystemExit(4)" in snap
      and "age < 0" in snap and "CHECK=missing" in snap)

H.chk("可选 BACKUP_REMOTE（scp 最新快照）；失败只 WARN 不影响本机快照，--check-offsite 计入失败",
      "BACKUP_REMOTE" in snap and "scp -q" in snap and "[WARN] 异地复制" in snap
      and "--check-offsite" in snap and '"offsite"' in snap)

H.chk("★ 备机同步：通过 ssh 在主机上跑 ha_snapshot.sh，缺脚本时报错并给安装命令",
      "ops/ha_snapshot.sh" in sync_sh and "test -f" in sync_sh
      and "scp mobile_apps/printer-backend/ops/ha_snapshot.sh" in sync_sh)

H.chk("★ 备机同步：scp 两库 + last_snapshot.json → incoming/<ts>，校验 md5/完整性/逐表行数",
      "incoming" in sync_sh and "_md5(" in sync_sh and "integrity_check" in sync_sh
      and "逐表行数" in sync_sh and "expected.get(\"md5\")" in sync_sh.replace("'", '"'))

H.chk("★ 备机同步：全过才原子换向 current（ln -s + mv -T），校验失败不换",
      "mv -T" in sync_sh and "ln -s" in sync_sh and "不换向" in sync_sh)

H.chk("备机同步：--dry-run 只打印不落地；uploads 用 rsync --delete（并写明镜像删除风险）；config 600",
      "--dry-run" in sync_sh and "dry-run" in sync_sh
      and "未**未执行**" not in sync_sh and "未改动" in sync_sh
      and "rsync -a --delete --partial" in sync_sh and "--delete 会" in sync_sh
      and "chmod 600" in sync_sh)

H.chk("★ 提级：先把线上库安全备份到 pre_promote_<ts>/（backup API + integrity + manifest）",
      "pre_promote_" in promote and "s.backup(d)" in promote
      and "PRE_PROMOTE_BACKUP=ok" in promote and "manifest.json" in promote)

H.chk("★ 提级：行数不倒退判据（线上行数 < last_sync 记录 ⇒ 失败）",
      "行数倒退" in promote and "PROMOTE_ROWS_CHECK=ok" in promote)

H.chk("提级：stop → 原子安装（cp 临时名 + mv）→ start → 轮询 /api/ping 200",
      "systemctl stop" in promote and "systemctl start" in promote
      and 'mv -f "$APP_DIR/.$db.promote.$TS" "$APP_DIR/$db"' in promote
      and "PING_URL" in promote and '"200"' in promote)

H.chk("提级：last_sync 陈旧（>RPO*2）非 0 退出，--force 才绕过并打 WARN",
      "RPO_MIN * 60 * 2" in promote and "--force" in promote and "warn" in promote)

H.chk("★ 提级：DNS 切换步骤含 A 记录 TTL 300 + 外部 dig/curl 校验 + 回滚命令",
      "DNS" in promote and "TTL" in promote and "300" in promote
      and "dig +short hn-space.cn" in promote and "https://hn-space.cn/api/ping" in promote
      and "回滚" in promote and "print_rollback" in promote)

H.chk("提级：清掉旧 -wal/-shm（否则旧库的未 checkpoint 页会盖到新库上）",
      'rm -f "$APP_DIR/$db-wal" "$APP_DIR/$db-shm"' in promote)

H.chk("★ 演练：RTO/RPO 判据 + 目标 30/5 + 子进程 run_simple 起备站",
      "rto_within_target" in drill and "rpo_within_target" in drill
      and "target-rto-min" in drill and "target-rpo-min" in drill
      and "run_simple" in drill and "HN_DRILL_MODE" in drill)

H.chk("演练：报告契约字段 + 退出码 0/1/2",
      all(k in drill for k in ('"rto_min"', '"rpo_min"', '"targets"', '"restore_sec"',
                               '"boot_sec"', '"checks"', '"row_counts"', '"source_unchanged"',
                               '"disaster_at"', '"snapshot_at"'))
      and "return 0 if report.get(\"ok\") else 1" in drill and "return 2" in drill)

H.chk("演练：源库 SHA-256 前后一致判据（演练必须只读）",
      "_sha256" in drill and "source_unchanged" in drill and "sha256_before" in drill)

H.chk("★ Runbook：RTO/RPO 表 + DNS TTL 300 + 切换步骤 + 回滚 + 实测数字 + 未做边界 + 证据路径",
      all(k in runbook for k in ("RTO", "RPO", "300", "dig", "回滚", "实测",
                                 "未做", "ops/reports/ha_", "last_snapshot.json",
                                 "第二台主机", "DNS 自动切换")))


# ============================================================
# B. 行为
# ============================================================
print("\n=== B. 行为（真 SQLite + 真 HTTP + 真子进程）===")


def _bash_syntax(path):
    if not BASH or not os.path.isfile(path):
        return None
    return subprocess.run([BASH, "-n", path], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=60)


if BASH:
    results = {os.path.basename(p): _bash_syntax(p)
               for p in (SNAP_SH, SYNC_SH, PROMOTE_SH) if os.path.isfile(p)}
    H.chk("三个 .sh 全部过 `bash -n`",
          len(results) == 3 and all(r.returncode == 0 for r in results.values()),
          {k: (v.returncode, (v.stderr or "")[-200:]) for k, v in results.items()})
else:
    H.chk("SKIP 本机没有 bash（.sh 只做静态判据）", True)


def _mk_seed(dst):
    """在临时后端副本里造 2 单 / 2 文件（含物理上传文件）。"""
    up = os.path.join(dst, "uploads")
    os.makedirs(os.path.join(up, "pdf"), exist_ok=True)
    os.makedirs(os.path.join(up, "docx"), exist_ok=True)
    payload = {"pdf/a.pdf": b"%PDF-1.4\n%ha drill\n", "docx/b.docx": b"PK\x03\x04ha-drill\n"}
    for rel, data in payload.items():
        with open(os.path.join(up, rel), "wb") as f:
            f.write(data)

    o = sqlite3.connect(os.path.join(dst, "orders.db"))
    try:
        o.execute("PRAGMA journal_mode=WAL")
        for i, (fid, rel) in enumerate((("f1", "pdf/a.pdf"), ("f2", "docx/b.docx")), start=1):
            o.execute("INSERT INTO files (id, original_name, saved_name, path, size, created_at)"
                      " VALUES (?,?,?,?,?,?)",
                      (fid, os.path.basename(rel), os.path.basename(rel), rel,
                       len(payload[rel]), "2026-10-24 20:%02d:00" % i))
            o.execute("INSERT INTO orders (file_id, file, copies, status, created_at, openid,"
                      " page_count, total_price) VALUES (?,?,?,?,?,?,?,?)",
                      (fid, rel, 1, "sent", "2026-10-24 20:%02d:00" % i, "u1", 2, 0.6))
            o.execute("INSERT INTO order_files (order_id, file_id, file_name, copies, page_count,"
                      " status, created_at) VALUES (?,?,?,?,?,?,?)",
                      (i, fid, os.path.basename(rel), 1, 2, "sent", "2026-10-24 20:%02d:00" % i))
        o.commit()
        o.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        o.close()

    u = sqlite3.connect(os.path.join(dst, "users.db"))
    try:
        u.execute("INSERT INTO users (openid, nickname, updated_at, role) VALUES (?,?,?,?)",
                  ("u1", "测试用户", "2026-10-24 20:00:00", "admin"))
        u.commit()
        u.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        u.close()


def _counts(db):
    """行数（**在副本上**数：WAL 库即使只读打开也会在源目录留下 -shm，
    本脚本自己不能污染被测目录，否则"drill 没扔垃圾"那条判据就没有判别力）。"""
    tmp = tempfile.mkdtemp(prefix="hn_ha_counts_")
    H._created.append(tmp)
    dst = os.path.join(tmp, os.path.basename(db))
    shutil.copy2(db, dst)
    for suffix in ("-wal", "-shm"):
        if os.path.exists(db + suffix):
            shutil.copy2(db + suffix, dst + suffix)
    c = sqlite3.connect(dst)
    try:
        out = {}
        for (t,) in c.execute("SELECT name FROM sqlite_master WHERE type='table'"
                              " AND name NOT LIKE 'sqlite_%'"):
            try:
                out[t] = c.execute('SELECT COUNT(*) FROM "%s"' % t).fetchone()[0]
            except sqlite3.Error:
                pass
        return out
    finally:
        c.close()


def _run_drill(script, args, workdir, timeout=240):
    report_path = os.path.join(workdir, "report.json")
    proc = subprocess.run([PY, script, "--workdir", workdir, "--report", report_path] + args,
                          cwd=REPO, env=ENV, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout)
    data = {}
    if os.path.isfile(report_path):
        with open(report_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    return proc, data


def _corrupt_notnull(path):
    """制造一种 integrity_check **能发现**、但普通读/建表仍可用的损坏（NOT NULL 约束被破坏）。

    刻意不用"把文件写成垃圾"：那种库连 init_db 都起不来，坏库用例会红在"起不来"上，
    分不清是"完整性判据生效"还是"运气不好"。NOT NULL 违例正好只让 completeness 判据红。
    """
    c = sqlite3.connect(path)
    try:
        c.execute("CREATE TABLE _drill_junk (id INTEGER PRIMARY KEY, v TEXT)")
        c.execute("INSERT INTO _drill_junk (v) VALUES (NULL)")
        c.commit()
        c.execute("PRAGMA writable_schema=ON")
        c.execute("UPDATE sqlite_master SET sql=? WHERE name='_drill_junk'",
                  ("CREATE TABLE _drill_junk (id INTEGER PRIMARY KEY, v TEXT NOT NULL)",))
        c.commit()
    finally:
        c.close()


TMP = tempfile.mkdtemp(prefix="hn_ha_")
H._created.append(TMP)

COPY_TMP, COPY = H.make_copy("hn_ha_backend_")
probe = H.run_probe(COPY, "print('INIT_DONE')\n")
H.chk("临时副本 init_db 成功（与线上同一份迁移代码）",
      probe.returncode == 0 and "INIT_DONE" in (probe.stdout or ""),
      (probe.returncode, (probe.stderr or "")[-300:]))
_mk_seed(COPY)
seeded_counts = _counts(os.path.join(COPY, "orders.db"))
H.chk("前置：临时副本造出 2 单 / 2 文件（否则后面对账没有判别力）",
      seeded_counts.get("orders") == 2 and seeded_counts.get("files") == 2, seeded_counts)

src_sha_before = {n: hashlib.sha256(open(os.path.join(COPY, n), "rb").read()).hexdigest()
                  for n in ("orders.db", "users.db")}

# --- B1: 正常演练 ---------------------------------------------------------
wd = os.path.join(TMP, "d_ok")
proc, rep = _run_drill(DRILL, ["--source-dir", COPY, "--require-uploads"], wd)
H.chk("★ 正常演练 exit 0", proc.returncode == 0,
      (proc.returncode, (proc.stdout or "")[-600:], (proc.stderr or "")[-600:]))
contract = ["ok", "generated_at", "disaster_at", "snapshot_at", "rto_min", "rpo_min",
            "targets", "restore_sec", "boot_sec", "checks", "row_counts", "source_unchanged"]
H.chk("报告契约字段齐全（ok/generated_at/disaster_at/snapshot_at/rto_min/rpo_min/targets/"
      "restore_sec/boot_sec/checks/row_counts/source_unchanged）",
      all(k in rep for k in contract), [k for k in contract if k not in rep])
H.chk("报告：ok=true、目标 30/5、rto_min 与 rpo_min 是真实数字（不是 0 占位）",
      rep.get("ok") is True
      and rep.get("targets") == {"rto_min": 30.0, "rpo_min": 5.0}
      and isinstance(rep.get("rto_min"), (int, float)) and rep.get("rto_min") > 0
      and isinstance(rep.get("rpo_min"), (int, float)) and rep.get("rpo_min") >= 0,
      {k: rep.get(k) for k in ("ok", "targets", "rto_min", "rpo_min")})
H.chk("★ 备站是**真子进程 + 随机端口**起的（不是 5000，不是 test_client）",
      isinstance(rep.get("standby", {}).get("ping", {}).get("url"), str)
      and ":5000" not in rep["standby"]["ping"]["url"]
      and rep["standby"]["ping"].get("status") == 200
      and rep["standby"].get("boot_ok") is True,
      rep.get("standby"))
H.chk("★ 行数对账：恢复后 orders=2 / files=2（与快照一致）",
      (rep.get("row_counts", {}).get("orders.db", {}).get("actual", {})
       .get("tables", {}).get("orders") == 2)
      and (rep.get("row_counts", {}).get("orders.db", {}).get("actual", {})
           .get("tables", {}).get("files") == 2),
      rep.get("row_counts"))
H.chk("最新订单 id 一致（备站 = 快照）",
      rep.get("row_counts", {}).get("orders.db", {}).get("actual", {})
      .get("latest_order_id") == 2,
      rep.get("row_counts", {}).get("orders.db", {}).get("actual"))
H.chk("★ --require-uploads：2 个物理上传文件都被核对到（files.path 存在）",
      rep.get("row_counts", {}).get("uploads", {}).get("checked") == 2
      and rep.get("row_counts", {}).get("uploads", {}).get("missing") == 0,
      rep.get("row_counts", {}).get("uploads"))
src_sha_after = {n: hashlib.sha256(
    open(os.path.join(COPY, n), "rb").read()).hexdigest() for n in ("orders.db", "users.db")}
H.chk("★ 源库 SHA-256 演练前后一致（演练只读，绝不改生产库）",
      rep.get("source_unchanged") is True and src_sha_before == src_sha_after,
      (rep.get("source_unchanged"), src_sha_before, src_sha_after))
H.chk("★ 演练没往源目录扔 -wal/-shm（WAL 库只读打开也会在源目录建 -shm，必须避讳）",
      not any(os.path.exists(os.path.join(COPY, n + s))
              for n in ("orders.db", "users.db") for s in ("-wal", "-shm")),
      [n + s for n in ("orders.db", "users.db") for s in ("-wal", "-shm")
       if os.path.exists(os.path.join(COPY, n + s))])

# --- B1b: 生产同款"良性外键违例"（orders.file_id → 已过期 files 行）------
# 2026-10-24 生产快照实测 228 条：文件到期被清理，订单保留历史 file_id。
# 演练绝不能因此判失败（否则每次对着真实生产快照都是假红），但也必须写进 warnings。
FK_TMP, FK_COPY = H.make_copy("hn_ha_backend_fk_")
H.run_probe(FK_COPY, "print('INIT_DONE')\n")
_mk_seed(FK_COPY)
_c = sqlite3.connect(os.path.join(FK_COPY, "orders.db"))
_c.execute("INSERT INTO orders (file_id, file, copies, status, created_at, openid,"
           " page_count, total_price) VALUES (?,?,?,?,?,?,?,?)",
           ("expired-file-id", "gone.pdf", 1, "sent", "2026-10-24 20:10:00", "u1", 2, 0.6))
_c.commit()
_c.close()
proc_fk, rep_fk = _run_drill(DRILL, ["--source-dir", FK_COPY], os.path.join(TMP, "d_fk"))
H.chk("★ 良性外键违例（文件到期后 orders.file_id 仍指向旧 id）不判失败",
      proc_fk.returncode == 0
      and any(c.get("name") == "foreign_key_ok" and c.get("ok")
              for c in rep_fk.get("checks") or []),
      (proc_fk.returncode,
       [c for c in rep_fk.get("checks") or [] if "foreign_key" in c.get("name", "")]))
H.chk("★ 良性违例写进 warnings（生产 228 条同类，不能静默）",
      any("良性" in w for w in rep_fk.get("warnings") or []), rep_fk.get("warnings"))

# 非良性：别的 table→parent 组合必须判失败（否则"良性规则"会把真损坏一起放过）
_c = sqlite3.connect(os.path.join(FK_COPY, "orders.db"))
_c.execute("CREATE TABLE IF NOT EXISTS probe_child (id INTEGER PRIMARY KEY,"
           " oid INTEGER REFERENCES orders(id))")
_c.execute("INSERT INTO probe_child (oid) VALUES (999999)")
_c.commit()
_c.close()
proc_fkx, rep_fkx = _run_drill(DRILL, ["--source-dir", FK_COPY], os.path.join(TMP, "d_fk_bad"))
H.chk("★ 非良性外键违例（其它 table→parent）仍判失败",
      proc_fkx.returncode == 1
      and any(c.get("name") == "foreign_key_ok" and not c.get("ok")
              for c in rep_fkx.get("checks") or []),
      (proc_fkx.returncode,
       [c for c in rep_fkx.get("checks") or [] if "foreign_key" in c.get("name", "")]))

# --- B2: RPO 超标（备机同步断了两小时没人发现）-----------------------------
rpo_args = ["--source-dir", COPY,
            "--last-sync-at", (datetime.now(timezone.utc).astimezone()
                               - timedelta(hours=2)).isoformat(timespec="seconds")]
proc_rpo, rep_rpo = _run_drill(DRILL, rpo_args, os.path.join(TMP, "d_rpo"))
H.chk("★ RPO 超标（--last-sync-at 2 小时前）⇒ exit 1", proc_rpo.returncode == 1,
      (proc_rpo.returncode, (proc_rpo.stdout or "")[-400:]))
H.chk("★ RPO 超标时 reason 明确指向 rpo，且 rpo_within_target=False",
      "rpo" in (rep_rpo.get("reason") or "").lower()
      and any(c.get("name") == "rpo_within_target" and not c.get("ok")
              for c in rep_rpo.get("checks") or []),
      (rep_rpo.get("reason"), rep_rpo.get("rpo_min")))

# --- B3: 快照里的库损坏（integrity 违例）----------------------------------
snap_bad = os.path.join(TMP, "snap_bad_bits")
os.makedirs(snap_bad, exist_ok=True)
for n in ("orders.db", "users.db"):
    shutil.copy2(os.path.join(COPY, n), os.path.join(snap_bad, n))
_corrupt_notnull(os.path.join(snap_bad, "orders.db"))
chk_int = sqlite3.connect(os.path.join(snap_bad, "orders.db")).execute(
    "PRAGMA integrity_check").fetchone()[0]
H.chk("前置：坏库确实被 integrity_check 抓出来（否则用例没有判别力）",
      chk_int != "ok", chk_int)
proc_bad, rep_bad = _run_drill(DRILL, ["--source-dir", COPY, "--snapshot-dir", snap_bad],
                               os.path.join(TMP, "d_bad"))
H.chk("★ 快照库有完整性违例 ⇒ exit 1", proc_bad.returncode == 1,
      (proc_bad.returncode, (proc_bad.stdout or "")[-400:]))
H.chk("★ 坏库用例红在**完整性判据**上（不是红在崩库/异常上）",
      any(c.get("name", "").startswith("integrity") and not c.get("ok")
          for c in rep_bad.get("checks") or [])
      and not rep_bad.get("standby", {}).get("boot_ok"),
      [c for c in rep_bad.get("checks") or [] if not c.get("ok")])

# --- B4: 库根本读不出来（垃圾文件）----------------------------------------
snap_garbage = os.path.join(TMP, "snap_garbage")
os.makedirs(snap_garbage, exist_ok=True)
with open(os.path.join(snap_garbage, "orders.db"), "wb") as f:
    f.write(b"this is not a sqlite database\n" * 200)
shutil.copy2(os.path.join(COPY, "users.db"), os.path.join(snap_garbage, "users.db"))
proc_g, rep_g = _run_drill(DRILL, ["--source-dir", COPY, "--snapshot-dir", snap_garbage],
                           os.path.join(TMP, "d_garbage"))
H.chk("★ 快照里的 orders.db 是垃圾文件 ⇒ exit 1（读失败也必须是失败，不许假绿）",
      proc_g.returncode == 1, (proc_g.returncode, (proc_g.stdout or "")[-400:]))

# --- B5: --require-uploads 但物理文件缺失 ---------------------------------
COPY2_TMP, COPY2 = H.make_copy("hn_ha_backend2_")
probe2 = H.run_probe(COPY2, "print('INIT_DONE')\n")
_mk_seed(COPY2)
os.remove(os.path.join(COPY2, "uploads", "pdf", "a.pdf"))
proc_u, rep_u = _run_drill(DRILL, ["--source-dir", COPY2, "--require-uploads"],
                           os.path.join(TMP, "d_uploads"))
H.chk("★ --require-uploads 且缺物理文件 ⇒ exit 1（uploads 引用必须真实存在）",
      proc_u.returncode == 1
      and any(c.get("name") == "uploads_referenced_present" and not c.get("ok")
              for c in rep_u.get("checks") or []),
      (proc_u.returncode, rep_u.get("row_counts", {}).get("uploads")))

# --- B6: 备机同步 --dry-run 只打印不落地 ----------------------------------
if BASH and os.path.isfile(SYNC_SH):
    dry_dest = os.path.join(TMP, "standby_dry")
    env = dict(ENV)
    env.update({"PRIMARY": "root@example.invalid", "DEST": dry_dest, "PRIMARY_DIR": "/home/printer-backend"})
    dr = subprocess.run([BASH, SYNC_SH, "--dry-run", "--with-uploads", "--with-config"],
                        cwd=BACKEND, env=env, capture_output=True, text=True,
                        encoding="utf-8", errors="replace", timeout=120)
    out = (dr.stdout or "") + (dr.stderr or "")
    H.chk("★ ha_standby_sync.sh --dry-run：exit 0、打印 ssh/scp/rsync 计划、DEST 一个字节都没写",
          dr.returncode == 0 and "[dry-run]" in out and "rsync -a --delete --partial" in out
          and not os.path.exists(dry_dest),
          (dr.returncode, out[-400:], os.path.exists(dry_dest)))
else:
    H.chk("SKIP 本机没有 bash（sync --dry-run 未执行）", True)


# ============================================================
# C. 变异自证
# ============================================================
print("\n=== C. 变异自证（把判据删掉 ⇒ B 的用例必须转红）===")


def _mutated_drill(old, new, tag):
    src = _read(DRILL)
    if src.count(old) != 1:
        H.chk("变异锚点可定位（%s，命中 %d 次）" % (tag, src.count(old)), False, old[:120])
        return None
    out = os.path.join(TMP, "hanf_drill_mut_%s.py" % tag)
    with open(out, "w", encoding="utf-8", newline="\n") as f:
        f.write(src.replace(old, new))
    # 变异副本与 db_fk_policy.py 同目录（drill 从自己所在目录 import 该模块）
    if os.path.isfile(POLICY):
        shutil.copy2(POLICY, os.path.join(os.path.dirname(out), "db_fk_policy.py"))
    return out


# C1：删掉 RPO 判据 ⇒ RPO 用例必须从 exit 1 变 exit 0
mut_rpo = _mutated_drill(
    'self.chk("rpo_within_target", rpo_min <= a.target_rpo_min,',
    'self.chk("rpo_within_target", True,', "rpo")
if mut_rpo:
    proc_m, rep_m = _run_drill(mut_rpo, rpo_args, os.path.join(TMP, "d_mut_rpo"))
    H.chk("★ 变异（删掉 RPO 判据）⇒ 同一个 RPO 场景变 exit 0 ⇒ B2 会转红",
          proc_m.returncode == 0,
          (proc_m.returncode, (rep_m.get("reason") or ""), (proc_m.stdout or "")[-300:]))

# C2：删掉完整性判据 ⇒ 坏库用例必须从 exit 1 变 exit 0
mut_int = _mutated_drill(
    '    return bool(info.get("ok")) and info.get("integrity_check") == "ok"',
    '    return True', "integrity")
if mut_int:
    proc_m2, rep_m2 = _run_drill(mut_int, ["--source-dir", COPY, "--snapshot-dir", snap_bad],
                                 os.path.join(TMP, "d_mut_int"))
    H.chk("★ 变异（删掉完整性判据）⇒ 坏库用例变 exit 0 ⇒ B3 会转红",
          proc_m2.returncode == 0,
          (proc_m2.returncode,
           [c for c in rep_m2.get("checks") or [] if not c.get("ok")],
           (proc_m2.stdout or "")[-400:]))

H.finish()
