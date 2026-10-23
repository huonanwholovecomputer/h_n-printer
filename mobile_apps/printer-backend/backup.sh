#!/bin/bash
# ============================================================
# HN 云打印 — 数据库备份（S2-06 重构版，2026-10-23）
#
# 用法：
#   bash backup.sh                 # 备份 orders.db + users.db
#   bash backup.sh --restore-check # 备份后立刻把两个库恢复到临时目录并核对（恢复演练）
#
# 可用环境变量覆盖（验收脚本靠它在临时目录里跑）：
#   PROJECT_DIR          默认 /opt/printer-backend
#   BACKUP_DIR           默认 $PROJECT_DIR/backups
#   RETENTION_DAYS       默认 7（按 mtime 清旧备份）
#   BACKUP_GPG_RECIPIENT 可选；配了就 gpg --encrypt 成 .gz.gpg 并**删掉明文 .gz**（静态加密）
#   BACKUP_REMOTE        可选；如 root@backup-host:/srv/hn-backups/（配了就 scp 一份 == 异地）
#   PYTHON               可选；默认自动探测 $PROJECT_DIR/venv/bin/python → python3 → python
#
# 为什么不再用 `cp`：库是 WAL 模式，最近的写入可能**只在 orders.db-wal 里**，`cp orders.db`
# 拿到的是"上一次检查点"的快照（丢最近订单，文件本身还可能处于不一致状态）。这里一律用
# SQLite 的在线 backup API（WAL 安全、不阻塞读写），备份完立刻做 integrity_check。
#
# crontab 建议：
#   0 3 * * * /home/printer-backend/backup.sh >> /var/log/printer-backup.log 2>&1
# ============================================================
set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-/opt/printer-backend}"
BACKUP_DIR="${BACKUP_DIR:-$PROJECT_DIR/backups}"
RETENTION_DAYS="${RETENTION_DAYS:-7}"
DB_FILES=(orders.db users.db)
TS=$(date +"%Y%m%d_%H%M%S")
LOG_TS() { date '+%Y-%m-%d %H:%M:%S'; }

if [[ -z "${PYTHON:-}" ]]; then
  if [[ -x "$PROJECT_DIR/venv/bin/python" ]]; then PYTHON="$PROJECT_DIR/venv/bin/python"
  elif command -v python3 >/dev/null 2>&1; then PYTHON=python3
  else PYTHON=python; fi
fi

mkdir -p "$BACKUP_DIR"

echo "[$(LOG_TS)] 开始备份 → $BACKUP_DIR（retention=${RETENTION_DAYS}d）"

PRODUCED=()
for db in "${DB_FILES[@]}"; do
  SRC="$PROJECT_DIR/$db"
  if [[ ! -f "$SRC" ]]; then
    echo "[$(LOG_TS)] 跳过（不存在）：$db"
    continue
  fi
  OUT="$BACKUP_DIR/${db%.db}_${TS}.db"
  # SQLite 在线备份 API：一致性快照，WAL 里未 checkpoint 的写入也会包含
  "$PYTHON" - "$SRC" "$OUT" <<'PY'
import sqlite3, sys
src_path, dst_path = sys.argv[1], sys.argv[2]
src = sqlite3.connect(src_path)
dst = sqlite3.connect(dst_path)
with dst:
    src.backup(dst)
dst.close(); src.close()
PY
  # 立刻校验备份可读且完整（坏备份等于没备份）
  "$PYTHON" - "$OUT" <<'PY'
import sqlite3, sys
c = sqlite3.connect(sys.argv[1])
ok = c.execute("pragma integrity_check").fetchone()[0]
c.close()
if ok != "ok":
    raise SystemExit("备份完整性校验失败: %s" % ok)
PY
  echo "[$(LOG_TS)] 已备份 $db → $(basename "$OUT") ($(stat -c%s "$OUT" 2>/dev/null || echo '?') B, integrity=ok)"
  gzip -f "$OUT"
  PRODUCED+=("$OUT.gz")

  if [[ -n "${BACKUP_GPG_RECIPIENT:-}" ]] && command -v gpg >/dev/null 2>&1; then
    gpg --yes --batch --encrypt --recipient "$BACKUP_GPG_RECIPIENT" "$OUT.gz"
    rm -f "$OUT.gz"          # 静态加密：不留明文
    echo "[$(LOG_TS)] 已加密 $db → $(basename "$OUT").gz.gpg（明文已删除）"
    PRODUCED[-1]="$OUT.gz.gpg"
  fi
done

# 异地副本（可选）
if [[ -n "${BACKUP_REMOTE:-}" ]]; then
  for f in ${PRODUCED[@]+"${PRODUCED[@]}"}; do
    [[ -f "$f" ]] || continue
    if command -v scp >/dev/null 2>&1; then
      scp -q "$f" "$BACKUP_REMOTE" && echo "[$(LOG_TS)] 已异地复制 $(basename "$f") → $BACKUP_REMOTE"
    else
      echo "[$(LOG_TS)] [WARN] 未安装 scp，跳过异地复制" >&2
    fi
  done
fi

# 清理过期备份（orders_/users_ 两类、明文与加密两种后缀都算）
DELETED=$(find "$BACKUP_DIR" -maxdepth 1 -type f \
            \( -name 'orders_*.db.gz' -o -name 'orders_*.db.gz.gpg' \
               -o -name 'users_*.db.gz'  -o -name 'users_*.db.gz.gpg' \) \
            -mtime +"$RETENTION_DAYS" -print -delete | wc -l)
echo "[$(LOG_TS)] 清理超过 ${RETENTION_DAYS} 天的旧备份：$DELETED 个"

# 恢复演练：把备份恢复到一个空目录，核对完整性 + 行数 + 最新订单，再删掉临时目录
if [[ "${1:-}" == "--restore-check" ]]; then
  echo "[$(LOG_TS)] 恢复演练开始"
  RESTORE_DIR=$(mktemp -d)
  trap 'rm -rf "$RESTORE_DIR"' EXIT
  for f in ${PRODUCED[@]+"${PRODUCED[@]}"}; do
    [[ -f "$f" ]] || continue
    case "$f" in
      *.gpg)
        if command -v gpg >/dev/null 2>&1; then
          gpg --quiet --batch --decrypt "$f" > "$RESTORE_DIR/$(basename "${f%.gpg}")"
        else
          echo "[$(LOG_TS)] [WARN] 备份已加密但本机无 gpg，跳过恢复演练" >&2
        fi
        ;;
      *.gz)
        gzip -dc "$f" > "$RESTORE_DIR/$(basename "${f%.gz}")"
        ;;
      *) cp "$f" "$RESTORE_DIR/" ;;
    esac
  done
  "$PYTHON" - "$RESTORE_DIR" "$PROJECT_DIR" <<'PY'
import os, sqlite3, sys
restore_dir, project_dir = sys.argv[1], sys.argv[2]
problems = []
for name in ("orders.db", "users.db"):
    src_path = os.path.join(project_dir, name)
    if not os.path.exists(src_path):
        continue
    prefix = name[:-3] + "_"          # orders_ / users_
    cands = [f for f in os.listdir(restore_dir) if f.startswith(prefix) and f.endswith(".db")]
    if not cands:
        problems.append(f"{name}: 恢复目录里没有对应的备份文件")
        continue
    rp = os.path.join(restore_dir, sorted(cands)[-1])
    src = sqlite3.connect(src_path)
    dst = sqlite3.connect(rp)
    if dst.execute("pragma integrity_check").fetchone()[0] != "ok":
        problems.append(f"{name}: 恢复后 integrity_check 失败")
    for table in ("orders", "order_files", "files", "users"):
        try:
            a = src.execute(f"select count(*) from {table}").fetchone()[0]
            b = dst.execute(f"select count(*) from {table}").fetchone()[0]
        except sqlite3.OperationalError:
            continue
        if a != b:
            problems.append(f"{name}.{table}: 行数不一致 src={a} bak={b}")
    if name == "orders.db":
        try:
            a = src.execute("select order_number from orders order by id desc limit 1").fetchone()
            b = dst.execute("select order_number from orders order by id desc limit 1").fetchone()
            if a != b:
                problems.append(f"orders: 最新订单不一致 src={a} bak={b}")
        except sqlite3.OperationalError:
            pass
        # WAL 安全性判据：备份里必须能看到源库**已提交**的全部订单（含还只在 -wal 里的）
        try:
            src_ids = {r[0] for r in src.execute("select id from orders").fetchall()}
            dst_ids = {r[0] for r in dst.execute("select id from orders").fetchall()}
            missing = src_ids - dst_ids
            if missing:
                problems.append(f"orders: 备份缺 {len(missing)} 行（WAL 未落盘？）")
        except sqlite3.OperationalError:
            pass
    src.close(); dst.close()
print("RESTORE_CHECK=" + ("ok" if not problems else "FAIL"))
for p in problems:
    print("  [RESTORE] " + p)
raise SystemExit(0 if not problems else 2)
PY
  echo "[$(LOG_TS)] 恢复演练结束"
fi

echo "[$(LOG_TS)] 备份完成，当前备份目录："
ls -lh "$BACKUP_DIR" | tail -20
