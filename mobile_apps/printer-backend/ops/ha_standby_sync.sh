#!/bin/bash
# ============================================================================
# HN 云打印 — 备机侧同步（S3-03 高可用 / warm standby，2026-10-24）
#
# 在**备机**上跑：把主机的最新滚动快照拉过来，校验后原子替换 $DEST/current/。
#   PRIMARY=root@82.156.66.18 PRIMARY_DIR=/home/printer-backend DEST=/srv/hn-standby \
#     RPO_MIN=5 bash ops/ha_standby_sync.sh [--dry-run] [--with-uploads] [--with-config]
#
# 它做的事（顺序即依赖顺序）：
#   1) 通过 ssh 在主机上执行 `$PRIMARY_DIR/ops/ha_snapshot.sh`（先打一份新快照）；
#      主机上没有这个脚本 ⇒ 明确报错并打印安装命令（**不**自己 cp 活动库顶上）；
#   2) scp 两个库 + last_snapshot.json 到 $DEST/incoming/<快照ts>/；
#   3) 校验：md5 与主机元数据一致、integrity_check=ok、逐表行数与主机元数据一致；
#   4) 全过才把 $DEST/current 原子替换成 incoming/<ts>（符号链接 + `mv -T` 原子换向）；
#   5) 可选 `--with-uploads`：rsync -a --delete --partial 镜像 uploads/；
#      ⚠ --delete 会**删掉备机上主机没有的文件**（镜像语义）——这正是"备机等于主机"想要的，
#      但如果备机上还存着别的东西/还要回滚，就会一起没。默认关闭。
#   6) 可选 `--with-config`：scp /etc/hn-printer/config.py（600）到 $DEST/config/config.py；
#   7) 写 $DEST/last_sync.json：snapshot_at / synced_at / 每库 rows / md5 / 校验结论。
#
# 环境变量：PRIMARY（必填 user@host）、PRIMARY_DIR（默认 /home/printer-backend）、
#           DEST（默认 /srv/hn-standby）、RPO_MIN（默认 5）、SSH_PORT（默认 22）、
#           SSH_OPTS（可选，原样传给 ssh，如 "-o StrictHostKeyChecking=accept-new"）、
#           CONFIG_PATH（默认 /etc/hn-printer/config.py）
#
# 退出码：0 成功且新鲜；2 参数/IO/传输错误（含主机没有快照脚本）；3 校验失败（md5/完整性/行数）；
#         4 同步成功但快照已陈旧（> RPO_MIN*2，告警用；备机数据仍被更新）
#
# crontab（备机）：
#   */5 * * * * PRIMARY=root@<主库IP> bash /srv/hn-standby/ops/ha_standby_sync.sh \
#     >> /var/log/hn-ha-standby.log 2>&1
# ============================================================================
set -euo pipefail

PRIMARY="${PRIMARY:-}"
PRIMARY_DIR="${PRIMARY_DIR:-/home/printer-backend}"
DEST="${DEST:-/srv/hn-standby}"
RPO_MIN="${RPO_MIN:-5}"
SSH_PORT="${SSH_PORT:-22}"
SSH_OPTS="${SSH_OPTS:-}"
CONFIG_PATH="${CONFIG_PATH:-/etc/hn-printer/config.py}"

DRY_RUN=0
WITH_UPLOADS=0
WITH_CONFIG=0
for arg in "$@"; do
  case "$arg" in
    --dry-run)      DRY_RUN=1 ;;
    --with-uploads) WITH_UPLOADS=1 ;;
    --with-config)  WITH_CONFIG=1 ;;
    -h|--help)      sed -n '3,40p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "[FAIL] 未知参数：$arg（支持 --dry-run / --with-uploads / --with-config / --help）" >&2
       exit 2 ;;
  esac
done

LOG_TS() { date '+%Y-%m-%d %H:%M:%S'; }
say()  { echo "[$(LOG_TS)] $*"; }
warn() { echo "[$(LOG_TS)] [WARN] $*" >&2; }
die()  { local code="$1"; shift; echo "[$(LOG_TS)] [FAIL] $*" >&2; exit "$code"; }

[[ -n "$PRIMARY" ]] || die 2 "必须设置 PRIMARY=user@host（备机从主机拉快照）"
command -v ssh  >/dev/null 2>&1 || die 2 "未安装 ssh"
command -v scp  >/dev/null 2>&1 || die 2 "未安装 scp"

PY=""
if command -v python3 >/dev/null 2>&1; then PY=python3
elif command -v python >/dev/null 2>&1; then PY=python
else die 2 "备机需要 python3（校验 md5/完整性/行数）"; fi

REMOTE_SNAP="$PRIMARY_DIR/backups/recent"
REMOTE_META="$REMOTE_SNAP/last_snapshot.json"

# ssh/scp 统一出口（--dry-run 时只打印，不连主机）
run_ssh() {
  if [[ "$DRY_RUN" -eq 1 ]]; then
    printf '[dry-run] ssh -p %s %s %s\n' "$SSH_PORT" "$PRIMARY" "$*"
    return 0
  fi
  # shellcheck disable=SC2086  # SSH_OPTS 需要按词拆分
  ssh -p "$SSH_PORT" ${SSH_OPTS} "$PRIMARY" "$@"
}
run_scp() {
  local src="$1"; local dst="$2"
  if [[ "$DRY_RUN" -eq 1 ]]; then
    printf '[dry-run] scp -P %s %s %s\n' "$SSH_PORT" "$src" "$dst"
    return 0
  fi
  # shellcheck disable=SC2086
  scp -P "$SSH_PORT" ${SSH_OPTS} -q "$src" "$dst"
}

say "备机同步：PRIMARY=$PRIMARY:$PRIMARY_DIR → DEST=$DEST（RPO=${RPO_MIN}min$([[ $DRY_RUN -eq 1 ]] && echo ', dry-run')）"

# ---------------------------------------------------------------- 1) 主机侧快照
if [[ "$DRY_RUN" -eq 0 ]] && ! run_ssh "test -f '$PRIMARY_DIR/ops/ha_snapshot.sh'"; then
  cat >&2 <<EOF
[$(LOG_TS)] [FAIL] 主机上没有 $PRIMARY_DIR/ops/ha_snapshot.sh
       备机同步**只认**主机自己打出来的滚动快照（SQLite backup API + integrity_check），
       不会在这里 cp 活动库顶上（WAL 下会丢最近订单）。
       先在仓库里把这个脚本装到主机（二选一）：
         # A. 从开发机直传
         scp mobile_apps/printer-backend/ops/ha_snapshot.sh \\
             $PRIMARY:$PRIMARY_DIR/ops/
         ssh $PRIMARY 'chmod +x $PRIMARY_DIR/ops/ha_snapshot.sh'
         # B. 主机上已经有仓库副本时
         ssh $PRIMARY 'cp <仓库>/mobile_apps/printer-backend/ops/ha_snapshot.sh \\
             $PRIMARY_DIR/ops/ && chmod +x $PRIMARY_DIR/ops/ha_snapshot.sh'
       然后给主机加 5 分钟 cron：
         ssh $PRIMARY "(crontab -l 2>/dev/null | grep -v ha_snapshot.sh; \\
           echo '*/5 * * * * cd $PRIMARY_DIR && bash ops/ha_snapshot.sh >> /var/log/hn-ha-snapshot.log 2>&1') | crontab -"
       手动跑一次确认：ssh $PRIMARY 'cd $PRIMARY_DIR && bash ops/ha_snapshot.sh'
EOF
  exit 2
fi

say "① 在主机上打一份新快照（$PRIMARY_DIR/ops/ha_snapshot.sh）"
# 主机的快照退出码原样上报：2=IO、3=完整性、4=陈旧都说明"主机侧已经不对了"
set +e
run_ssh "cd '$PRIMARY_DIR' && bash ops/ha_snapshot.sh"
SNAP_RC=$?
set -e
if [[ "$SNAP_RC" -ne 0 ]]; then
  case "$SNAP_RC" in
    3) die 3 "主机侧快照完整性失败（ha_snapshot.sh exit 3）——拒绝把坏数据拉成备机当前版本" ;;
    4) die 4 "主机侧快照陈旧或缺失（ha_snapshot.sh exit 4）" ;;
    *) die 2 "主机侧快照失败（ha_snapshot.sh exit $SNAP_RC）" ;;
  esac
fi

if [[ "$DRY_RUN" -eq 1 ]]; then
  say "② scp $PRIMARY:$REMOTE_SNAP/{orders.db,users.db,last_snapshot.json} → $DEST/incoming/<ts>/"
  say "③ 校验 md5 / integrity_check / 逐表行数（与主机 last_snapshot.json 比对）"
  say "④ 原子替换 $DEST/current（ln -s + mv -T）"
  if [[ "$WITH_UPLOADS" -eq 1 ]]; then
    say "⑤ rsync -a --delete --partial -e 'ssh -p $SSH_PORT' $PRIMARY:$PRIMARY_DIR/uploads/ $DEST/uploads/"
  fi
  if [[ "$WITH_CONFIG" -eq 1 ]]; then
    say "⑥ scp $PRIMARY:$CONFIG_PATH → $DEST/config/config.py（chmod 600）"
  fi
  say "dry-run：以上命令均**未执行**，$DEST 未改动"
  exit 0
fi

WORK="$(mktemp -d)"
cleanup() { rm -rf "$WORK" 2>/dev/null || true; }
trap cleanup EXIT

# 取主机元数据（快照 ts / 每库 md5 / 行数）
run_ssh "cat '$REMOTE_META'" > "$WORK/remote_meta.json" 2>"$WORK/remote_meta.err" || \
  die 2 "读取主机元数据失败：$REMOTE_META（$(tail -3 "$WORK/remote_meta.err" 2>/dev/null | tr '\n' ' ')）"
SNAP_TS="$("$PY" - "$WORK/remote_meta.json" <<'PY'
import json, sys
with open(sys.argv[1], encoding="utf-8") as f:
    meta = json.load(f)
print(int(meta.get("ts") or 0))
PY
)"
[[ "$SNAP_TS" =~ ^[0-9]+$ ]] || die 2 "主机元数据里的 ts 非法：$SNAP_TS"
[[ "$SNAP_TS" -gt 0 ]] || die 2 "主机元数据里没有有效 ts（快照元数据坏了？）"

INCOMING="$DEST/incoming/$SNAP_TS"
mkdir -p "$INCOMING" || die 2 "无法创建 $INCOMING"
say "② 拉取快照（ts=$SNAP_TS）→ $INCOMING"

DB_LIST="$("$PY" - "$WORK/remote_meta.json" <<'PY'
import json, sys
with open(sys.argv[1], encoding="utf-8") as f:
    meta = json.load(f)
print(" ".join(sorted((meta.get("dbs") or {}).keys())))
PY
)"
[[ -n "$DB_LIST" ]] || die 2 "主机元数据里没有任何数据库记录"
for db in $DB_LIST; do
  run_scp "$PRIMARY:$REMOTE_SNAP/$db" "$INCOMING/$db" || die 2 "scp $db 失败"
done
run_scp "$PRIMARY:$REMOTE_META" "$INCOMING/last_snapshot.json" || die 2 "scp last_snapshot.json 失败"

# ---------------------------------------------------------------- 3)+4) 校验 + 原子换向
# python 负责全部判据（md5 / integrity / 逐表行数）并把结论写进 $DEST/last_sync.json：
#   0 = 校验通过且新鲜；3 = 校验失败（不换 current）；4 = 校验通过但陈旧（换 current 后告警）
say "③ 校验 md5 / integrity_check / 逐表行数"
set +e
"$PY" - "$INCOMING" "$DEST" "$PRIMARY" "$PRIMARY_DIR" "$RPO_MIN" "$WITH_UPLOADS" "$WITH_CONFIG" \
        "$(date +%s)" "$PY" <<'PY'
import hashlib
import json
import os
import sqlite3
import sys
import time

(incoming, dest, primary, primary_dir, rpo_raw, with_uploads, with_config,
 synced_ts, plain_python) = sys.argv[1:10]
rpo = float(rpo_raw)
now = float(synced_ts)


def _md5(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _inspect(path):
    """只读打开，返回 (integrity, tables_dict, error)。坏库不能把整个脚本打崩。"""
    conn = sqlite3.connect("file:%s?mode=ro" % path.replace("\\", "/"), uri=True, timeout=20)
    try:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        names = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
            "ORDER BY name")]
        tables = {}
        for t in names:
            try:
                tables[t] = int(conn.execute(
                    'SELECT COUNT(*) FROM "%s"' % t.replace('"', '""')).fetchone()[0])
            except sqlite3.Error:
                tables[t] = -1
        return integrity, tables, ""
    except Exception as e:  # noqa: BLE001
        return "", {}, "%s: %s" % (type(e).__name__, e)
    finally:
        conn.close()


with open(os.path.join(incoming, "last_snapshot.json"), encoding="utf-8") as f:
    remote = json.load(f)

problems = []
warnings = []
dbs = {}
for name, expected in sorted((remote.get("dbs") or {}).items()):
    path = os.path.join(incoming, name)
    if not os.path.isfile(path):
        problems.append("%s: 备机 incoming 里没有这个文件" % name)
        continue
    md5 = _md5(path)
    if expected.get("md5") and md5 != expected["md5"]:
        problems.append("%s: md5 不一致 主机=%s 备机=%s"
                        % (name, expected["md5"][:12], md5[:12]))
    integrity, tables, err = _inspect(path)
    if err:
        problems.append("%s: 打不开/校验异常 %s" % (name, err))
    elif integrity != "ok":
        problems.append("%s: integrity_check=%s" % (name, integrity))
    exp_tables = expected.get("tables") or {}
    if exp_tables and tables:
        diff = {t: (n, tables.get(t)) for t, n in exp_tables.items() if tables.get(t) != n}
        if diff:
            problems.append("%s: 逐表行数不一致 %s" % (name, diff))
    dbs[name] = {
        "md5": md5,
        "rows": sum(v for v in tables.values() if v > 0),
        "tables": tables,
        "integrity": integrity or "error",
        "size": os.path.getsize(path),
        "installed": os.path.join(dest, "current", name),
    }
    print("  %-10s md5=%s rows=%s integrity=%s" % (name, md5[:12], dbs[name]["rows"],
                                                   dbs[name]["integrity"]))

snap_ts = float(remote.get("ts") or 0)
age = now - snap_ts
stale = age > rpo * 60 * 2
if stale:
    warnings.append("快照已陈旧：age=%.0fs > RPO*2=%.0fs" % (age, rpo * 60 * 2))
if age < 0:
    warnings.append("快照时间戳在未来（备机/主机时钟不一致？age=%.0fs）" % age)

sync = {
    "version": 1,
    "tool": "ha_standby_sync.sh",
    "snapshot_at": remote.get("ts_readable") or "",
    "snapshot_ts": int(snap_ts),
    "synced_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
    "synced_ts": int(now),
    "age_sec": round(age, 0),
    "rpo_min": rpo,
    "stale": bool(stale),
    "primary": primary,
    "primary_dir": primary_dir,
    "dest": dest,
    "incoming": incoming,
    "dbs": dbs,
    "uploads": {"included": with_uploads == "1", "dest": os.path.join(dest, "uploads")},
    "config": {"included": with_config == "1",
               "path": os.path.join(dest, "config", "config.py")},
    "checks": {"md5_and_rows_ok": not problems},
    "problems": problems,
    "warnings": warnings,
    "ok": not problems,
}

os.makedirs(dest, exist_ok=True)
tmp = os.path.join(dest, "last_sync.json.tmp.%d" % os.getpid())
with open(tmp, "w", encoding="utf-8") as f:
    json.dump(sync, f, ensure_ascii=False, indent=2, sort_keys=True)
    f.flush()
    os.fsync(f.fileno())
os.replace(tmp, os.path.join(dest, "last_sync.json"))

if problems:
    for p in problems:
        print("[FAIL] " + p)
    print("SYNC=verify_failed")
    raise SystemExit(3)
for w in warnings:
    print("[WARN] " + w)
print("SYNC=%s" % ("stale" if stale else "ok"))
raise SystemExit(4 if stale else 0)
PY
VERIFY_RC=$?
set -e

if [[ "$VERIFY_RC" -ne 0 && "$VERIFY_RC" -ne 4 ]]; then
  die 3 "备机快照校验失败（md5/完整性/行数）——$DEST/current 保持不变（不换向）"
fi

# 原子换向：current 是符号链接，`mv -T` 把新链接整体替换上去（同一目录内的 rename）
say "④ 原子替换 $DEST/current → $DEST/incoming/$SNAP_TS"
if [[ -e "$DEST/current" && ! -L "$DEST/current" ]]; then
  # 兼容早期把 current 做成了真目录的部署：先挪走再换链接
  mv -T "$DEST/current" "$DEST/current.legacy.$SNAP_TS"
  warn "旧的 current 是真目录，已挪到 $DEST/current.legacy.$SNAP_TS"
fi
ln -s "$DEST/incoming/$SNAP_TS" "$DEST/.current.tmp.$$"
mv -T "$DEST/.current.tmp.$$" "$DEST/current"
# 只保留最近 3 份 incoming，别把备机磁盘当备份仓库（归档归 backup.sh / 异地）
ls -1dt "$DEST"/incoming/*/ 2>/dev/null | tail -n +4 | while IFS= read -r old; do
  rm -rf "$old" 2>/dev/null || true
done
say "current → $(readlink "$DEST/current" 2>/dev/null || echo "$DEST/current")"

# ---------------------------------------------------------------- 5) uploads（可选）
if [[ "$WITH_UPLOADS" -eq 1 ]]; then
  command -v rsync >/dev/null 2>&1 || die 2 "指定了 --with-uploads 但备机没有 rsync"
  say "⑤ rsync uploads（镜像模式：--delete 会删除主机上已不存在的备机文件）"
  mkdir -p "$DEST/uploads"
  rsync -a --delete --partial -e "ssh -p $SSH_PORT ${SSH_OPTS}" \
    "$PRIMARY:$PRIMARY_DIR/uploads/" "$DEST/uploads/" || die 2 "rsync uploads 失败"
  say "uploads 已镜像到 $DEST/uploads（$(du -sh "$DEST/uploads" 2>/dev/null | cut -f1)）"
fi

# ---------------------------------------------------------------- 6) config（可选）
if [[ "$WITH_CONFIG" -eq 1 ]]; then
  say "⑥ scp $CONFIG_PATH → $DEST/config/config.py（600）"
  mkdir -p "$DEST/config"
  run_scp "$PRIMARY:$CONFIG_PATH" "$DEST/config/config.py" || die 2 "scp config.py 失败"
  chmod 600 "$DEST/config/config.py"
  say "config.py 已落地（600）"
fi

say "同步完成：$DEST/current 已指向 incoming/$SNAP_TS（元数据 $DEST/last_sync.json）"
if [[ "$VERIFY_RC" -eq 4 ]]; then
  warn "快照已陈旧（> RPO_MIN*2）——数据仍被更新，但请立即查主机快照 cron/磁盘"
  exit 4
fi
exit 0
