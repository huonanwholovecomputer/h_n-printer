#!/bin/bash
# ============================================================================
# HN 云打印 — 备机提级（S3-03 高可用 / failover promotion，2026-10-24）
#
# 在**备机**上把 $DEST/current 的最新快照装成线上库并起服务。真实切换时它只负责
# "把数据换成主库最后一份快照 + 服务能起来 + 行数没倒退"，DNS 切换是人工动作
# （本脚本**不持有任何 DNS 凭据**，只把步骤和校验命令打出来）。
#
# 用法：
#   DEST=/srv/hn-standby APP_DIR=/home/printer-backend SERVICE=printer-backend \
#     bash ops/ha_promote.sh [--force] [--no-start]
#
#   --force     允许在 last_sync 已陈旧（age > RPO_MIN*2）时仍然提级（会打 WARN；
#               意味着接受"丢掉最近一段时间的订单写入"）
#   --no-start  只换库，不起服务、不做 /api/ping 冒烟（演练/预演用；DNS 切换前必须手动起）
#
# 环境变量：DEST（默认 /srv/hn-standby）、APP_DIR（默认 /home/printer-backend）、
#           SERVICE（默认 printer-backend）、RPO_MIN（默认 5）、
#           PING_URL（默认 http://127.0.0.1:5000/api/ping）、PING_TIMEOUT（默认 120 秒）
#
# 退出码：0 成功；2 参数/IO；3 校验失败（快照 md5/完整性）；4 last_sync 陈旧且没给 --force
#
# 顺序（每一步失败都会打印已执行步骤 + 回滚命令，并尽力自动回滚）：
#   0) 读 $DEST/last_sync.json：age ≤ RPO_MIN*2，否则 --force
#   1) 校验 $DEST/current 库的 md5 与完整性（坏库不许提级）
#   2) **安全备份**现有 $APP_DIR 两个库到 backups/pre_promote_<ts>/（SQLite backup API +
#      integrity_check + manifest.json）——这是回滚的唯一依据，先做再动
#   3) systemctl stop $SERVICE（停写，避免提级过程中还有写入）
#   4) 原子安装：cp 到临时名 → fsync → mv；**并清掉旧的 -wal/-shm**
#   5) systemctl start $SERVICE
#   6) 轮询 $PING_URL 到 200（≤ PING_TIMEOUT 秒）
#   7) 校验线上库行数 ≥ last_sync 里的行数（**允许更高**，绝不允许倒退）
#   8) 打印 DNS 切换步骤（A 记录 / TTL 300 / 外部 dig + curl 校验）+ 回滚命令
# ============================================================================
set -Eeuo pipefail

DEST="${DEST:-/srv/hn-standby}"
APP_DIR="${APP_DIR:-/home/printer-backend}"
SERVICE="${SERVICE:-printer-backend}"
RPO_MIN="${RPO_MIN:-5}"
PING_URL="${PING_URL:-http://127.0.0.1:5000/api/ping}"
PING_TIMEOUT="${PING_TIMEOUT:-120}"

FORCE=0
DO_START=1
for arg in "$@"; do
  case "$arg" in
    --force)    FORCE=1 ;;
    --no-start) DO_START=0 ;;
    -h|--help)  sed -n '3,32p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "[FAIL] 未知参数：$arg（支持 --force / --no-start / --help）" >&2; exit 2 ;;
  esac
done

LOG_TS() { date '+%Y-%m-%d %H:%M:%S'; }
say()  { echo "[$(LOG_TS)] $*"; }
warn() { echo "[$(LOG_TS)] [WARN] $*" >&2; }

PY=""
if [[ -x "$APP_DIR/venv/bin/python" ]]; then PY="$APP_DIR/venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then PY=python3
elif command -v python >/dev/null 2>&1; then PY=python
else echo "[FAIL] 找不到 python（提级需要它做 backup/integrity/行数校验）" >&2; exit 2; fi

CURRENT="$DEST/current"
LAST_SYNC="$DEST/last_sync.json"
TS="$(date +%Y%m%d_%H%M%S)"
PRE_PROMOTE="$APP_DIR/backups/pre_promote_$TS"
ROLLBACK_CMD="DEST=$DEST APP_DIR=$APP_DIR SERVICE=$SERVICE bash $0 --force"

EXECUTED=()
DB_TOUCHED=0
SERVICE_STOPPED=0
step() { EXECUTED+=("$1"); say "$1"; }

print_steps() {
  echo "--- 已执行步骤 ---" >&2
  if [[ ${#EXECUTED[@]} -eq 0 ]]; then echo "  （无）" >&2; fi
  for s in ${EXECUTED[@]+"${EXECUTED[@]}"}; do echo "  ✔ $s" >&2; done
}

print_rollback() {
  cat >&2 <<EOF
--- 回滚（把线上库换回提级前的安全备份，然后把 DNS 改回原主机）---
  systemctl stop $SERVICE
  for db in orders.db users.db; do
    [ -f "$PRE_PROMOTE/\$db" ] || continue
    rm -f "$APP_DIR/\$db-wal" "$APP_DIR/\$db-shm"
    cp -f "$PRE_PROMOTE/\$db" "$APP_DIR/.\$db.rollback.tmp"
    mv -f "$APP_DIR/.\$db.rollback.tmp" "$APP_DIR/\$db"
  done
  systemctl start $SERVICE
  curl -fsS -o /dev/null -w '%{http_code}\\n' $PING_URL        # 期望 200
  # DNS 回切到原主机（TTL 300）：dig +short hn-space.cn @8.8.8.8
  # 提级前的库副本在：$PRE_PROMOTE/（含 manifest.json）
EOF
}

on_error() {
  fail_body "$?"
}

# 统一的失败收尾：打印已执行步骤 + 尽力自动回滚 + 打印回滚命令 + 非 0 退出。
# 刻意做成函数而不是只在 ERR trap 里用：有些失败是"python 块返回非 0"，那种要显式调用。
fail_body() {
  local rc="${1:-1}"
  trap - ERR
  echo >&2
  echo "[$(LOG_TS)] [FAIL] 提级失败（第 ${#EXECUTED[@]} 步之后，exit=$rc）" >&2
  print_steps
  if [[ "$DB_TOUCHED" -eq 1 ]]; then
    echo "[$(LOG_TS)] 正在尽力自动回滚（把 pre_promote 备份装回去）…" >&2
    systemctl stop "$SERVICE" >/dev/null 2>&1 || true
    for db in orders.db users.db; do
      [[ -f "$PRE_PROMOTE/$db" ]] || continue
      rm -f "$APP_DIR/$db-wal" "$APP_DIR/$db-shm" 2>/dev/null || true
      if cp -f "$PRE_PROMOTE/$db" "$APP_DIR/.$db.rollback.tmp" 2>/dev/null && \
         mv -f "$APP_DIR/.$db.rollback.tmp" "$APP_DIR/$db" 2>/dev/null; then
        echo "  已回滚 $db ← $PRE_PROMOTE/$db" >&2
      else
        echo "  回滚 $db 失败，请按下面的命令手工恢复" >&2
      fi
    done
    if [[ "$SERVICE_STOPPED" -eq 1 ]]; then
      systemctl start "$SERVICE" >/dev/null 2>&1 && echo "  服务已重新启动（旧库）" >&2
    fi
  elif [[ "$SERVICE_STOPPED" -eq 1 ]]; then
    systemctl start "$SERVICE" >/dev/null 2>&1 && echo "  服务已重新启动（库未动）" >&2
  fi
  print_rollback
  exit "$rc"
}
trap on_error ERR

say "备机提级：$CURRENT → $APP_DIR（service=$SERVICE, RPO=${RPO_MIN}min）"

# ---------------------------------------------------------------- 0) 新鲜度闸门
[[ -f "$LAST_SYNC" ]] || {
  echo "[FAIL] 没有 $LAST_SYNC —— 先跑 ha_standby_sync.sh（否则不知道备机数据有多旧）" >&2
  exit 4
}
read -r SYNCED_TS SYNC_STALE SYNC_SNAPSHOT_AT <<< "$("$PY" - "$LAST_SYNC" <<'PY'
import json, sys
with open(sys.argv[1], encoding="utf-8") as f:
    d = json.load(f)
print(int(d.get("synced_ts") or 0), "1" if d.get("stale") else "0",
      (d.get("snapshot_at") or "-").replace(" ", "_"))
PY
)"
NOW=$(date +%s)
AGE=$(( NOW - SYNCED_TS ))
LIMIT=$(( RPO_MIN * 60 * 2 ))
say "last_sync：snapshot_at=$SYNC_SNAPSHOT_AT age=${AGE}s（上限 ${LIMIT}s = RPO*2）"
if [[ "$SYNCED_TS" -le 0 ]]; then
  echo "[FAIL] last_sync.json 里没有有效 synced_ts" >&2
  exit 4
fi
if [[ "$AGE" -gt "$LIMIT" || "$SYNC_STALE" == "1" ]]; then
  if [[ "$FORCE" -eq 1 ]]; then
    warn "备机数据已陈旧（age=${AGE}s > ${LIMIT}s 或上次同步已标记 stale）——因 --force 继续提级"
    warn "⚠ 这意味着最近一段时间的订单写入会丢：先确认主机确实不可恢复、再继续"
  else
    echo "[$(LOG_TS)] [FAIL] 备机数据陈旧：age=${AGE}s > RPO*2=${LIMIT}s（或 last_sync 已标记 stale）" >&2
    echo "        先重跑 ha_standby_sync.sh；确认主机已不可恢复、只能接受数据损失时加 --force" >&2
    exit 4
  fi
fi

# ---------------------------------------------------------------- 1) 快照校验
step "① 校验 $CURRENT 的 md5/integrity（坏库不许提级）"
if ! "$PY" - "$CURRENT" "$LAST_SYNC" <<'PY'
import hashlib, json, os, sqlite3, sys

current, last_sync = sys.argv[1], sys.argv[2]
with open(last_sync, encoding="utf-8") as f:
    sync = json.load(f)


def _md5(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


problems = []
for name, want in sorted((sync.get("dbs") or {}).items()):
    path = os.path.join(current, name)
    if not os.path.isfile(path):
        problems.append("%s: $CURRENT 下没有这个库" % name)
        continue
    got = _md5(path)
    if want.get("md5") and got != want["md5"]:
        problems.append("%s: md5 与 last_sync 不一致 %s != %s"
                        % (name, got[:12], want["md5"][:12]))
    conn = None
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % path.replace("\\", "/"), uri=True, timeout=20)
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    except sqlite3.Error as e:
        problems.append("%s: 打不开/校验异常 %s: %s" % (name, type(e).__name__, e))
        print("  %-10s md5=%s integrity=%s" % (name, got[:12], "ERROR"))
        continue
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
    if integrity != "ok":
        problems.append("%s: integrity_check=%s" % (name, integrity))
    print("  %-10s md5=%s integrity=%s" % (name, got[:12], integrity))
if problems:
    for p in problems:
        print("[FAIL] " + p)
    raise SystemExit(3)
print("PROMOTE_SOURCE_CHECK=ok")
PY
then
  fail_body 3
fi

# ---------------------------------------------------------------- 2) 安全备份
step "② 安全备份现有 $APP_DIR 两个库 → $PRE_PROMOTE（SQLite backup API + integrity）"
if ! "$PY" - "$APP_DIR" "$PRE_PROMOTE" <<'PY'
import json
import os
import sqlite3
import sys
import time

app_dir, out_dir = sys.argv[1], sys.argv[2]
os.makedirs(out_dir, exist_ok=True)
manifest = {"created_at": time.strftime("%Y-%m-%d %H:%M:%S"), "app_dir": app_dir, "dbs": {}}

for name in ("orders.db", "users.db"):
    src = os.path.join(app_dir, name)
    if not os.path.isfile(src):
        print("  跳过（不存在）：%s" % name)
        continue
    dst = os.path.join(out_dir, name)
    s = sqlite3.connect(src, timeout=30)
    d = sqlite3.connect(dst)
    try:
        with d:
            s.backup(d)       # 安全备份：绝不 cp 活动库（WAL 里未 checkpoint 的写入也要拿到）
    finally:
        d.close()
        s.close()
    c = sqlite3.connect(dst)
    try:
        integrity = c.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise SystemExit("[FAIL] pre_promote 备份 %s integrity_check=%s" % (name, integrity))
        names = [r[0] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        rows = 0
        for t in names:
            try:
                rows += int(c.execute('SELECT COUNT(*) FROM "%s"' % t.replace('"', '""')).fetchone()[0])
            except sqlite3.Error:
                pass
    finally:
        c.close()
    manifest["dbs"][name] = {"path": dst, "size": os.path.getsize(dst),
                             "integrity": integrity, "rows": rows}
    print("  已安全备份 %-10s rows=%s integrity=ok" % (name, rows))

tmp = os.path.join(out_dir, "manifest.json.tmp")
with open(tmp, "w", encoding="utf-8") as f:
    json.dump(manifest, f, ensure_ascii=False, indent=2, sort_keys=True)
os.replace(tmp, os.path.join(out_dir, "manifest.json"))
print("PRE_PROMOTE_BACKUP=ok -> %s" % out_dir)
PY
then
  fail_body 2
fi

# ---------------------------------------------------------------- 3) 停服务
step "③ 停止服务：systemctl stop $SERVICE"
systemctl stop "$SERVICE"
SERVICE_STOPPED=1

# ---------------------------------------------------------------- 4) 原子安装
step "④ 原子安装 $CURRENT 的两个库到 $APP_DIR"
DB_TOUCHED=1
for db in orders.db users.db; do
  SRC="$CURRENT/$db"
  if [[ ! -f "$SRC" ]]; then
    say "   跳过（快照里没有）：$db"
    continue
  fi
  # ⚠ 旧的 -wal/-shm 必须先删：它们属于**旧库**，留着会被当成新库的未 checkpoint 页
  #   应用上去 ⇒ 读到"两个库混在一起"的鬼数据（SQLite 会拒绝或给出幻读）。
  rm -f "$APP_DIR/$db-wal" "$APP_DIR/$db-shm"
  cp -f "$SRC" "$APP_DIR/.$db.promote.$TS"
  "$PY" - "$APP_DIR/.$db.promote.$TS" <<'PY'
import os, sys
fd = os.open(sys.argv[1], os.O_RDWR | getattr(os, "O_BINARY", 0))
try:
    os.fsync(fd)
finally:
    os.close(fd)
PY
  mv -f "$APP_DIR/.$db.promote.$TS" "$APP_DIR/$db"
  say "   已安装 $db（$(stat -c%s "$APP_DIR/$db" 2>/dev/null || echo '?') B）"
done

# ---------------------------------------------------------------- 5)+6) 起服务 + 冒烟
if [[ "$DO_START" -eq 1 ]]; then
  step "⑤ 启动服务：systemctl start $SERVICE"
  systemctl start "$SERVICE"
  SERVICE_STOPPED=0

  step "⑥ 冒烟：轮询 $PING_URL 到 200（≤${PING_TIMEOUT}s）"
  PING_OK=0
  DEADLINE=$(( $(date +%s) + PING_TIMEOUT ))
  while [[ "$(date +%s)" -lt "$DEADLINE" ]]; do
    CODE="$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 "$PING_URL" 2>/dev/null || true)"
    if [[ "$CODE" == "200" ]]; then PING_OK=1; break; fi
    sleep 2
  done
  if [[ "$PING_OK" -ne 1 ]]; then
    echo "[$(LOG_TS)] [FAIL] 冒烟失败：${PING_TIMEOUT}s 内 $PING_URL 没返回 200" >&2
    journalctl -u "$SERVICE" -n 50 --no-pager >&2 || true
    false        # 触发 ERR → 自动回滚 + 回滚命令
  fi
  say "   /api/ping 200 ok"
else
  step "⑤⑥ 跳过启动与冒烟（--no-start）"
  warn "--no-start：库已换但服务未起。DNS 切换前必须 systemctl start $SERVICE 并自测 /api/ping"
fi

# ---------------------------------------------------------------- 7) 行数不倒退
step "⑦ 校验线上库行数 ≥ last_sync 记录（允许更高，绝不倒退）"
if ! "$PY" - "$APP_DIR" "$LAST_SYNC" <<'PY'
import json
import os
import sqlite3
import sys

app_dir, last_sync = sys.argv[1], sys.argv[2]
with open(last_sync, encoding="utf-8") as f:
    sync = json.load(f)

problems = []
for name, want in sorted((sync.get("dbs") or {}).items()):
    want_tables = (want.get("tables") or {})
    if not want_tables:
        continue
    path = os.path.join(app_dir, name)
    if not os.path.isfile(path):
        problems.append("%s: 线上没有这个库" % name)
        continue
    conn = sqlite3.connect("file:%s?mode=ro" % path.replace("\\", "/"), uri=True, timeout=20)
    try:
        for table, n in sorted(want_tables.items()):
            try:
                got = int(conn.execute(
                    'SELECT COUNT(*) FROM "%s"' % table.replace('"', '""')).fetchone()[0])
            except sqlite3.Error as e:
                problems.append("%s.%s: 读不出来（%s）" % (name, table, e))
                continue
            print("  %-10s %-14s 线上=%s last_sync=%s" % (name, table, got, n))
            if got < n:
                problems.append("%s.%s: 行数倒退（线上 %s < 快照 %s）——提级装错了库"
                                % (name, table, got, n))
    finally:
        conn.close()
if problems:
    for p in problems:
        print("[FAIL] " + p)
    raise SystemExit(3)
print("PROMOTE_ROWS_CHECK=ok")
PY
then
  fail_body 3
fi

# ---------------------------------------------------------------- 8) DNS 切换清单
step "⑧ 提级完成，下面是人工 DNS 切换清单（脚本不持有 DNS 凭据）"
cat <<EOF

================ DNS 切换（人工，一批做完，别分步）================
域名：hn-space.cn（主域名；www 与 API 同源，按需一起改）
 1) **提前 24h** 把 hn-space.cn 的 A 记录 TTL 从默认值改成 **300**（秒）。
    提前改是为了切的那一刻等 300s 就能全网生效；TTL 还是 3600 时切完要飘一小时。
 2) 确认备机已经就绪：nginx 在 443 监听、证书是同一张（hn-space.cn）、
    /var/www/updates 与 /api 反代都指向 127.0.0.1:5000（本脚本刚验过 /api/ping 200）。
 3) 把 A 记录改成**备机公网 IP**（一条 A 记录，不要加第二条做"负载均衡"）。
 4) 外部校验（两条都要过，别只看本机）：
      dig +short hn-space.cn @8.8.8.8            # 应等于备机公网 IP
      curl -sS -o /dev/null -w '%{http_code}\n' https://hn-space.cn/api/ping   # 200
      curl -sS https://hn-space.cn/api/ping      # 期望 {"msg": "pong", ...}
 5) 观察 5 分钟：/api/ping、两台门店设备重连（授权页"在线"）、一次真实拉单/回报。
 6) 设备侧无需改配置（域名没变）；若某台设备因 TLS/证书报错，先查 nginx 证书链。
 7) **回切**：把 A 记录改回原主机 IP（TTL 300，等 300s 生效）——回切之前**不要**停备机，
    否则生效窗口内会有一段全站 502。
================ 回滚（数据装错时）================
$PRE_PROMOTE/ 里是提级前的两个库（SQLite backup API 产物 + manifest.json）。
按上面的回滚命令装回去即可；DNS 未切换时不需要动 DNS。
EOF

say "提级成功。证据：$LAST_SYNC 、$PRE_PROMOTE/manifest.json"
exit 0
