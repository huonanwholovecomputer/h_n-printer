#!/bin/bash
# ============================================================================
# HN 云打印 — 主库侧 5 分钟滚动快照（S3-03 高可用 / 容灾，2026-10-24）
#
# 目标（docs/安全加固修复计划_2026-10-23.md §S3-03；DDoS 报告 §8.4）：
#   · RPO ≤ 5 min、RTO ≤ 30 min、订单不丢；
#   · 单机（本机磁盘/进程）故障时，用 backups/recent/ 里的快照原地恢复即可；
#   · 主机级丢失（整台 VPS 消失）仍需**异地副本** → BACKUP_REMOTE（第二台主机/对象存储，
#     目前未采购，属未踩的边界，见 docs/高可用与容灾Runbook_2026-10-24.md）。
#
# ⚠ 与 backup.sh 同口径，别另造一套：
#   · 一律用 **SQLite 在线 backup API**（`src.backup(dst)`）——库是 WAL 模式，最近的写入
#     可能只在 orders.db-wal 里，`cp orders.db` 拿到的是上一次检查点的快照（丢订单，
#     文件还可能不一致）；
#   · 备 orders.db **和** users.db；
#   · 备份完立刻 `PRAGMA integrity_check`（坏备份等于没备份）；
#   · 快照不是 .gz 归档：它要能被 ha_standby_sync.sh / ha_failover_drill.py 直接 scp/恢复，
#     所以这里不压缩（0.5 MB 级别，压缩收益不值得多一层解压失败面）。
#
# 与 backup.sh 的分工：
#   · backup.sh        —— 每天 03:00 的**归档**（gz / 可选 GPG / 保留 7 天 / --restore-check）；
#   · ha_snapshot.sh   —— 每 5 分钟的**滚动**快照（只保留 最新 + prev 两版，不归档）。
#   两者互不替代：归档给"误删/逻辑损坏"回看，滚动快照给"故障时 RPO ≤5 min"。
#
# 用法：
#   bash ops/ha_snapshot.sh                # 打一份快照（默认 PROJECT_DIR=脚本上一级）
#   bash ops/ha_snapshot.sh --check        # 只判新鲜度：age 秒数；超 RPO_MIN ⇒ exit 4
#   bash ops/ha_snapshot.sh --check-offsite # 额外要求异地副本也新鲜（BACKUP_REMOTE 配了才有意义）
#   PROJECT_DIR=/home/printer-backend RPO_MIN=5 bash ops/ha_snapshot.sh
#
# 环境变量：
#   PROJECT_DIR   默认 = 脚本上一级（$(dirname $0)/..），生产即 /home/printer-backend
#   SNAP_DIR      默认 $PROJECT_DIR/backups/recent（保留上一位于 $SNAP_DIR/prev/）
#   RPO_MIN       默认 5（--check 的判据；也写进 last_snapshot.json）
#   BACKUP_REMOTE 可选 user@host:/path（配了就 scp 最新快照；失败只 WARN，不影响本机快照）
#   PYTHON        可选；默认自动探测 venv/bin/python → python3 → python
#
# 产出：
#   $SNAP_DIR/orders.db            最新快照（原子替换，读到的永远是完整文件）
#   $SNAP_DIR/users.db             同上
#   $SNAP_DIR/prev/orders.db       上一版（回滚用；同样原子）
#   $SNAP_DIR/last_snapshot.json   元数据：ts / 每库 md5 / integrity / rows / journal_mode /
#                                  source_sha256 前后值 / offsite 状态
#
# 退出码：0 成功或新鲜；2 参数/IO/找不到任何库；3 完整性失败；4 快照陈旧/缺失（--check）
#
# crontab（生产）：
#   */5 * * * * cd /home/printer-backend && bash ops/ha_snapshot.sh >> /var/log/hn-ha-snapshot.log 2>&1
#   # 可选：每 15 分钟自检一次新鲜度，陈旧就非 0（交给外部拨测/监控告警）
#   */15 * * * * cd /home/printer-backend && bash ops/ha_snapshot.sh --check >> /var/log/hn-ha-snapshot.log 2>&1
# ============================================================================
set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
SNAP_DIR="${SNAP_DIR:-$PROJECT_DIR/backups/recent}"
PREV_DIR="$SNAP_DIR/prev"
META="$SNAP_DIR/last_snapshot.json"
RPO_MIN="${RPO_MIN:-5}"
DB_FILES=(orders.db users.db)
LOG_TS() { date '+%Y-%m-%d %H:%M:%S'; }

MODE="snapshot"
CHECK_OFFSITE=0
for arg in "$@"; do
  case "$arg" in
    --check)          MODE="check" ;;
    --check-offsite)  MODE="check"; CHECK_OFFSITE=1 ;;
    -h|--help)
      sed -n '3,50p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *)
      echo "[$(LOG_TS)] [FAIL] 未知参数：$arg（支持 --check / --check-offsite / --help）" >&2
      exit 2 ;;
  esac
done

if [[ -z "${PYTHON:-}" ]]; then
  if [[ -x "$PROJECT_DIR/venv/bin/python" ]]; then PYTHON="$PROJECT_DIR/venv/bin/python"
  elif command -v python3 >/dev/null 2>&1; then PYTHON=python3
  else PYTHON=python; fi
fi
if ! command -v "$PYTHON" >/dev/null 2>&1; then
  echo "[$(LOG_TS)] [FAIL] 找不到可用的 python（设 PYTHON=/path/to/python）" >&2
  exit 2
fi

START_TS=$(date +%s)

# ---------------------------------------------------------------- --check
if [[ "$MODE" == "check" ]]; then
  if [[ ! -f "$META" ]]; then
    echo "[$(LOG_TS)] [FAIL] 没有快照元数据：$META（还没跑过快照？）"
    echo "CHECK=missing"
    exit 4
  fi
  # python 退出码直接透传（set -e）：陈旧/缺失/负 age ⇒ exit 4
  "$PYTHON" - "$META" "$RPO_MIN" "$CHECK_OFFSITE" <<'PY'
import json, sys, time

meta_path, rpo_raw, check_offsite = sys.argv[1], sys.argv[2], sys.argv[3] == "1"
try:
    with open(meta_path, "r", encoding="utf-8") as f:
        meta = json.load(f)
except Exception as e:  # noqa: BLE001
    print("[FAIL] 快照元数据不可读：%s: %s" % (type(e).__name__, e))
    print("CHECK=unreadable")
    raise SystemExit(4)

try:
    rpo = float(meta.get("rpo_min") or rpo_raw)
except (TypeError, ValueError):
    rpo = float(rpo_raw)
try:
    ts = float(meta.get("ts") or 0)
except (TypeError, ValueError):
    ts = 0.0
now = time.time()
age = now - ts
limit = rpo * 60.0

print("SNAPSHOT_AT=%s" % (meta.get("ts_readable") or "-"))
print("SNAPSHOT_TS=%d" % int(ts))
print("AGE_SEC=%.0f" % age)
print("RPO_LIMIT_SEC=%.0f" % limit)
print("RPO_MIN=%g" % rpo)

problems = []
if ts <= 0:
    problems.append("快照时间戳缺失或非法（ts=%r）" % meta.get("ts"))
elif age < 0:
    problems.append("快照时间戳在未来（系统时钟倒退？）age=%.0fs" % age)
elif age > limit:
    problems.append("快照已陈旧：age=%.0fs > RPO 上限 %.0fs（RPO=%g min）" % (age, limit, rpo))

if check_offsite:
    off = meta.get("offsite") or {}
    remote = off.get("remote") or ""
    print("OFFSITE_REMOTE=%s" % (remote or "-"))
    print("OFFSITE_OK=%s" % bool(off.get("ok")))
    if not remote:
        problems.append("未配置/未执行异地复制（--check-offsite 需要 BACKUP_REMOTE）")
    else:
        try:
            ots = float(off.get("ts") or 0)
        except (TypeError, ValueError):
            ots = 0.0
        oage = now - ots
        print("OFFSITE_AGE_SEC=%.0f" % oage)
        if not off.get("ok"):
            problems.append("最近一次异地复制失败：%s" % (off.get("error") or "未知原因"))
        elif ots <= 0:
            problems.append("异地复制时间戳缺失")
        elif oage > limit:
            problems.append("异地副本陈旧：age=%.0fs > %.0fs" % (oage, limit))

if problems:
    for p in problems:
        print("[FAIL] " + p)
    print("CHECK=stale")
    raise SystemExit(4)
print("CHECK=ok")
PY
  exit 0
fi

# ---------------------------------------------------------------- 快照
if ! mkdir -p "$SNAP_DIR" "$PREV_DIR" 2>/dev/null; then
  echo "[$(LOG_TS)] [FAIL] 无法创建快照目录：$SNAP_DIR" >&2
  exit 2
fi

# 上一轮被 kill 留下的临时文件（60 分钟前的才清，避免误删正在跑的）
find "$SNAP_DIR" -maxdepth 1 -name '.ha_tmp.*' -mmin +60 -delete 2>/dev/null || true

RECORDS="$SNAP_DIR/.ha_records.$$"
: > "$RECORDS"
cleanup_tmp() { rm -f "$SNAP_DIR"/.ha_tmp.*.$$ "$RECORDS" 2>/dev/null || true; }
trap cleanup_tmp EXIT

echo "[$(LOG_TS)] 开始滚动快照 → $SNAP_DIR（RPO=${RPO_MIN}min, project=$PROJECT_DIR）"

SNAP_COUNT=0
for db in "${DB_FILES[@]}"; do
  SRC="$PROJECT_DIR/$db"
  if [[ ! -f "$SRC" ]]; then
    echo "[$(LOG_TS)] [WARN] 跳过（不存在）：$db"
    continue
  fi
  TMP="$SNAP_DIR/.ha_tmp.$db.$$"

  # SQLite 在线 backup API：一致性快照（WAL 未 checkpoint 的写入也包含）+ integrity_check
  # + fsync + md5/行数/源库指纹。输出 KEY=VALUE 行给 bash 判据，外加一条 RECORD=<json>。
  if ! OUT=$("$PYTHON" - "$SRC" "$TMP" "$db" <<'PY'
import hashlib, json, os, sqlite3, sys

src_path, dst_path, db_name = sys.argv[1], sys.argv[2], sys.argv[3]


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _md5(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


src_sha_before = _sha256(src_path)

# —— 在线 backup API（与 backup.sh 同口径）。绝不 cp 活动库。——
src = sqlite3.connect(src_path, timeout=30)
dst = sqlite3.connect(dst_path)
try:
    with dst:
        src.backup(dst)
finally:
    dst.close()
    src.close()

src_sha_after = _sha256(src_path)

conn = sqlite3.connect(dst_path)
try:
    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    try:
        fk_bad = len(conn.execute("PRAGMA foreign_key_check").fetchall())
    except sqlite3.Error:
        fk_bad = -1
    row = conn.execute("PRAGMA journal_mode").fetchone()
    journal = (row[0] if row else "?") or "?"
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
        "ORDER BY name")]
    counts = {}
    for t in tables:
        try:
            counts[t] = int(conn.execute(
                'SELECT COUNT(*) FROM "%s"' % t.replace('"', '""')).fetchone()[0])
        except sqlite3.Error:
            counts[t] = -1
finally:
    conn.close()

# 落盘后再 rename：掉电也不会留下"看起来完整、实际还差几页"的快照
# （Windows 上 os.fsync 要求句柄可写，所以用 O_RDWR；Linux 上 O_RDWR 同样没问题）
fd = os.open(dst_path, os.O_RDWR | getattr(os, "O_BINARY", 0))
try:
    os.fsync(fd)
finally:
    os.close(fd)

record = {
    "db": db_name,
    "snapshot": dst_path,
    "size": os.path.getsize(dst_path),
    "md5": _md5(dst_path),
    "integrity": integrity,
    "foreign_key_violations": fk_bad,
    "journal_mode": journal,
    "tables": counts,
    "rows": sum(v for v in counts.values() if v > 0),
    "source": src_path,
    "source_sha256": src_sha_before,
    "source_sha256_after": src_sha_after,
    "source_unchanged": src_sha_before == src_sha_after,
}

print("INTEGRITY=%s" % integrity)
print("SRC_SHA=%s" % src_sha_before)
print("SRC_SHA_AFTER=%s" % src_sha_after)
print("MD5=%s" % record["md5"])
print("SIZE=%d" % record["size"])
print("JOURNAL_MODE=%s" % journal)
print("TABLES=%s" % json.dumps(counts, ensure_ascii=False, sort_keys=True))
print("ROWS=%d" % record["rows"])
print("RECORD=%s" % json.dumps(record, ensure_ascii=False, sort_keys=True))
PY
  ); then
    echo "[$(LOG_TS)] [FAIL] $db 快照失败（SQLite/IO 错误，见上面的 traceback）；" \
         "拒绝把半成品装进 $SNAP_DIR" >&2
    exit 2
  fi

  INTEGRITY=""; SRC_SHA=""; SRC_SHA_AFTER=""; MD5=""; SIZE=""; JOURNAL=""; TABLES=""; ROWS=""; RECORD=""
  while IFS= read -r line; do
    key="${line%%=*}"
    val="${line#*=}"
    # 本地用 Windows Python 输出时行尾是 CRLF（生产 Linux 没有），统一剥掉，
    # 否则 integrity 会读成 "ok\r" 而误判失败
    val="${val%$'\r'}"
    case "$key" in
      INTEGRITY)       INTEGRITY="$val" ;;
      SRC_SHA)         SRC_SHA="$val" ;;
      SRC_SHA_AFTER)   SRC_SHA_AFTER="$val" ;;
      MD5)             MD5="$val" ;;
      SIZE)            SIZE="$val" ;;
      JOURNAL_MODE)    JOURNAL="$val" ;;
      TABLES)          TABLES="$val" ;;
      ROWS)            ROWS="$val" ;;
      RECORD)          RECORD="$val" ;;
    esac
  done <<< "$OUT"

  if [[ -z "$RECORD" ]]; then
    echo "[$(LOG_TS)] [FAIL] $db 快照没有产出元数据（内部错误）" >&2
    exit 2
  fi
  if [[ "$INTEGRITY" != "ok" ]]; then
    echo "[$(LOG_TS)] [FAIL] $db 的备份 integrity_check=$INTEGRITY —— 拒绝安装（旧快照保持原样）" >&2
    exit 3
  fi
  if [[ "$SRC_SHA" != "$SRC_SHA_AFTER" ]]; then
    # 并发写入/检查点会让活动库文件的 sha256 变化，这**不是**快照不一致（backup API 保证
    # 快照本身是一致点）。只告警，不用它判失败——否则线上有单在打时永远红。
    echo "[$(LOG_TS)] [WARN] $db 源库在快照期间被写入（sha256 变化）；快照本身仍一致" >&2
  fi

  # 上一版进 prev/（同一文件系统内的 rename，原子）
  if [[ -f "$SNAP_DIR/$db" ]]; then
    mv -f "$SNAP_DIR/$db" "$PREV_DIR/$db"
  fi
  # 原子替换：读者要么看到旧文件、要么看到新文件，绝不会看到半截
  #（-wal/-shm 是打开临时库里检查时可能留下的副产品，主文件已 fsync，一并清掉）
  rm -f "$TMP-wal" "$TMP-shm"
  mv -f "$TMP" "$SNAP_DIR/$db"

  printf '%s\n' "$RECORD" >> "$RECORDS"
  SNAP_COUNT=$((SNAP_COUNT + 1))
  echo "[$(LOG_TS)] 已快照 $db → $(basename "$SNAP_DIR/$db") (${SIZE} B, rows=$ROWS, md5=${MD5:0:12}…, integrity=ok)"
done

# 一个库都没备到 ⇒ 非 0。绝不能"什么都没做却 exit 0"（backup.sh 上踩过：PROJECT_DIR 指错）
if [[ "$SNAP_COUNT" -eq 0 ]]; then
  echo "[$(LOG_TS)] [FAIL] 在 $PROJECT_DIR 下没有找到任何数据库文件（orders.db/users.db）" >&2
  exit 2
fi

# 目录项也要落盘，否则 rename 之后掉电可能丢目录项
# （Windows 不能 os.open 目录，跳过；生产是 Linux，这一句要生效）
"$PYTHON" - "$SNAP_DIR" "$PREV_DIR" <<'PY'
import os, sys
if os.name != "posix":
    raise SystemExit(0)
for d in sys.argv[1:]:
    try:
        fd = os.open(d, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError as e:  # noqa: BLE001
        print("[WARN] fsync 目录 %s 失败: %s" % (d, e))
PY

# ---------------------------------------------------------------- 元数据
"$PYTHON" - "$RECORDS" "$META" "$START_TS" "$RPO_MIN" "$PROJECT_DIR" "$SNAP_DIR" <<'PY'
import json, os, socket, sys, time

records_path, meta_path, start_ts, rpo_raw, project_dir, snap_dir = sys.argv[1:7]
recs = []
with open(records_path, "r", encoding="utf-8") as f:
    for line in f:
        line = line.strip()
        if line:
            recs.append(json.loads(line))
if not recs:
    raise SystemExit("[FAIL] 没有快照记录，拒绝写元数据")
# 记录里的 snapshot 路径是"安装前"的临时名，改成 rename 之后的正式路径
for r in recs:
    r["snapshot"] = os.path.join(snap_dir, r["db"])

ts = time.time()
meta = {
    "version": 1,
    "tool": "ha_snapshot.sh",
    "ts": int(ts),
    "ts_readable": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)),
    "rpo_min": float(rpo_raw),
    "host": socket.gethostname(),
    "project_dir": project_dir,
    "snap_dir": snap_dir,
    "elapsed_sec": max(0, int(ts) - int(start_ts)),
    "prev_dir": os.path.join(snap_dir, "prev"),
    "source_unchanged": all(r.get("source_sha256") == r.get("source_sha256_after") for r in recs),
    "dbs": {r["db"]: r for r in recs},
    "offsite": {"remote": "", "ok": False, "ts": 0, "error": "未配置 BACKUP_REMOTE"},
}
tmp = meta_path + ".tmp.%d" % os.getpid()
with open(tmp, "w", encoding="utf-8") as f:
    json.dump(meta, f, ensure_ascii=False, indent=2, sort_keys=True)
    f.flush()
    os.fsync(f.fileno())
os.replace(tmp, meta_path)       # 原子替换：--check 永远读不到半截 JSON
print("快照元数据：%s" % meta_path)
for name, r in sorted(meta["dbs"].items()):
    print("  %-10s rows=%s md5=%s integrity=%s journal=%s"
          % (name, r.get("rows"), (r.get("md5") or "")[:12], r.get("integrity"), r.get("journal_mode")))
PY

# ---------------------------------------------------------------- 异地副本（可选）
OFFSITE_OK=0
OFFSITE_ERR="未配置 BACKUP_REMOTE"
OFFSITE_REMOTE=""
if [[ -n "${BACKUP_REMOTE:-}" ]]; then
  OFFSITE_REMOTE="${BACKUP_REMOTE%/}"
  if ! command -v scp >/dev/null 2>&1; then
    OFFSITE_ERR="未安装 scp"
    echo "[$(LOG_TS)] [WARN] 未安装 scp，跳过异地复制（本机快照已成功；--check-offsite 会失败）" >&2
  else
    OFFSITE_OK=1
    for db in "${DB_FILES[@]}"; do
      f="$SNAP_DIR/$db"
      [[ -f "$f" ]] || continue
      if scp -q "$f" "$OFFSITE_REMOTE/"; then
        echo "[$(LOG_TS)] 已异地复制 $db → $OFFSITE_REMOTE/"
      else
        OFFSITE_OK=0
        OFFSITE_ERR="scp $db → $OFFSITE_REMOTE/ 失败"
        echo "[$(LOG_TS)] [WARN] 异地复制 $db 失败（本机快照仍成功）" >&2
      fi
    done
    # 把异地状态写回元数据（--check-offsite 判据），再送一份元数据过去
    "$PYTHON" - "$META" "$OFFSITE_REMOTE" "$OFFSITE_OK" "$OFFSITE_ERR" "$(date +%s)" <<'PY'
import json, os, sys
meta_path, remote, ok, err, ts = (sys.argv[1], sys.argv[2], sys.argv[3] == "1",
                                  sys.argv[4], int(sys.argv[5]))
with open(meta_path, "r", encoding="utf-8") as f:
    meta = json.load(f)
meta["offsite"] = {"remote": remote, "ok": bool(ok), "ts": ts, "error": "" if ok else err}
tmp = meta_path + ".tmp.%d" % os.getpid()
with open(tmp, "w", encoding="utf-8") as f:
    json.dump(meta, f, ensure_ascii=False, indent=2, sort_keys=True)
    f.flush()
    os.fsync(f.fileno())
os.replace(tmp, meta_path)
PY
    scp -q "$META" "$OFFSITE_REMOTE/" 2>/dev/null || \
      echo "[$(LOG_TS)] [WARN] 异地复制 last_snapshot.json 失败" >&2
  fi
fi

echo "[$(LOG_TS)] 快照完成：$SNAP_COUNT 个库，用时 $(( $(date +%s) - START_TS ))s"
exit 0
