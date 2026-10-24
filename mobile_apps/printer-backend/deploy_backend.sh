#!/usr/bin/env bash
# ============================================================
# 后端部署脚本（S1-10：部署链的**唯一入口**，别再手写 tar 命令）
#
# 为什么要有这个文件：`更新后端服务.txt` 里原来是一条手写 tar 管道，排除项靠人记。
# 一次漏 `--exclude=config.py` 就把开发机的微信 AppSecret / SECRET_KEY / printer token
# 覆盖到生产（DS-10 / DS-18）。排除项写进脚本 + 有验收脚本盯着，才不会靠"记得"。
#
# 用法：
#   bash deploy_backend.sh --dry-run    # 只打印将要上传的文件清单（不连服务器）
#   bash deploy_backend.sh              # 打包 + 上传 + 重启（需要 SSH 免密/密钥）
#   SSH_TARGET=root@1.2.3.4 bash deploy_backend.sh   # 覆盖目标机
#
# 排除项（**不要删任何一条**）：
#   config.py / config.py.example  配置由服务器侧维护（/etc/hn-printer/config.py，600）
#   *.bak* / *.db / uploads / logs 运行时数据与备份（含 token、用户文件、订单）
#   devices.json / printer_claim.json / order_counter.json  运行时状态
#   __pycache__ / venv / *.pyc     构建垃圾
# ============================================================
set -euo pipefail
cd "$(dirname "$0")"

SSH_TARGET="${SSH_TARGET:-root@82.156.66.18}"
# 额外 ssh 参数（如 -i /path/to/key），空格分隔
SSH_OPTS="${SSH_OPTS:-}"
REMOTE_DIR="${REMOTE_DIR:-/home/printer-backend}"
SERVICE="${SERVICE:-printer-backend}"

EXCLUDES=(
  --exclude='./config.py'
  --exclude='./config.py.example'
  --exclude='./*.bak'
  --exclude='./*.bak-*'
  --exclude='./*.bak.*'
  --exclude='./orders.db'
  --exclude='./orders.db-wal'
  --exclude='./orders.db-shm'
  --exclude='./users.db'
  --exclude='./users.db-wal'
  --exclude='./users.db-shm'
  --exclude='./uploads'
  --exclude='./logs'
  --exclude='./devices.json'
  --exclude='./device_secrets.json'
  --exclude='./printer_claim.json'
  --exclude='./order_counter.json'
  --exclude='./__pycache__'
  --exclude='./venv'
  --exclude='./*.pyc'
  --exclude='./_sec-baseline-*'
  --exclude='./_deploy-*'
  --exclude='./_s110-*'
  --exclude='./.git'
)

if [[ "${1:-}" == "--dry-run" ]]; then
  echo "# 将要上传的文件清单（排除项已生效）"
  tar czf - "${EXCLUDES[@]}" . | tar tzf - | sort
  exit 0
fi

echo "==> 打包上传到 $SSH_TARGET:$REMOTE_DIR"
tar czf - "${EXCLUDES[@]}" . \
  | ssh $SSH_OPTS "$SSH_TARGET" "cd $REMOTE_DIR && tar xzf -"

# ── 行尾归一化（2026-10-24 实测补上，别删）─────────────────────────────
# Windows 工作区是 core.autocrlf=true + 各种编辑器混排：**同一个目录里既有 LF 也有
# CRLF 文件**（2026-10-24 实测：app.py 是 CRLF、gunicorn_config.py/requirements.txt 是 LF）。
# 直接 tar 会把 CRLF 带上线 ⇒ 线上文件与 `git show <rev>:path` 的 blob **字节不同**，
# 而"线上 LF-md5 == 某个 commit 的 blob"正是判断『线上有没有仓库外手改』的唯一口径
# （覆盖前必须先核，见部署前检查清单）。带上 CRLF 会让下一次部署误判成"有手工热修"。
# Python 对 CRLF 无感，所以这个偏差**不会**以运行错误的形式暴露，只能靠这里兜住。
echo "==> 归一化行尾（CRLF → LF）"
ssh $SSH_OPTS "$SSH_TARGET" "REMOTE_DIR='$REMOTE_DIR' SERVICE='$SERVICE' bash -s" <<'REMOTE_EOF'
set -euo pipefail
cd "$REMOTE_DIR"
fixed=0
while IFS= read -r -d '' f; do
  if grep -qU $'\r' "$f" 2>/dev/null; then
    sed -i 's/\r$//' "$f"; echo "    LF <- $f"; fixed=$((fixed + 1))
  fi
done < <(find . -maxdepth 2 -type f \
  \( -name '*.py' -o -name '*.sh' -o -name '*.md' -o -name '*.txt' -o -name '*.json' \
     -o -name '*.conf' -o -name '*.service' -o -name '*.yml' \) \
  -not -path './venv/*' -not -path './uploads/*' -not -path './logs/*' -not -path './static/*' \
  -not -path './_sec-*' -not -path './_deploy-*' -not -path './_s110-*' \
  -not -path './local_print_tool/*' -print0)
echo "    归一化 $fixed 个文件"
systemctl restart "$SERVICE"
echo "==> 重启完成，等待 6 秒后冒烟"
sleep 6
systemctl is-active "$SERVICE"
curl -sS -o /dev/null -w 'ping=%{http_code}\n' http://127.0.0.1:5000/api/ping
REMOTE_EOF
