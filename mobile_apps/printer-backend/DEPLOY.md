# HN 云打印 — 部署指南

## 环境要求

- Ubuntu 22.04 LTS
- Python 3.10+
- Nginx
- Git（可选）

---

## 1. 上传代码

将 `printer-backend/` 目录上传到服务器 `/opt/printer-backend/`：

```bash
# 方式一：Git
cd /opt
git clone <your-repo-url> printer-backend

# 方式二：SCP
scp -r ./printer-backend user@your-server:/opt/
```

---

## 2. 创建 Python 虚拟环境

```bash
cd /opt/printer-backend
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

---

## 3. 配置 config.py

```bash
cp config.py.example config.py
nano config.py
```

至少修改以下项：
- `WECHAT_APPID`、`WECHAT_APPSECRET` — 微信小程序凭证
- `SECRET_KEY` — 换一个随机字符串
- `ADMIN_OPENIDS` — 你的管理员 openid
- `CLOUD_API_URL`、`WEBSOCKET_URL` — 改为你的云服务器地址
- `TOKEN` — 打印机客户端认证 token
- `PRINTER_NAME` — 打印机名称

---

## 4. 创建必要目录

```bash
mkdir -p /opt/printer-backend/uploads
mkdir -p /opt/printer-backend/backups
chown -R www-data:www-data /opt/printer-backend
```

> ⚠ **勿直出上传目录（uploads/）** —— 不要在 nginx 里加
> `location /uploads/ { alias /opt/printer-backend/uploads/; }`。
> 该目录下既有顾客上传的原始文档，也有几个**文件名固定、无需爆破**的服务端元数据：
> `md5_index.json`（md5 → 原始文件名 + 相对路径 + 页数）、`security_config.json`、
> `retention_config.json`、`order_counter.json`、`avatars/`。nginx `alias` 完全不鉴权，
> 加了这一条就等于对外开放一个「任意文件读取 + 全体顾客文件名索引泄露」入口 ——
> `curl https://你的域名/uploads/md5_index.json` 一条命令即可命中。
> `nginx-http.conf` / `nginx-https.conf` **已刻意不含该 location**（原配置有，现已删除），
> 升级时也**别加回来**；确需内部重定向才用 `internal;`。
> 文件下发一律走后端鉴权路由（`/api/file/<id>` 带归属校验、`/api/avatar?openid=…`）。
> 守护脚本：`python tests/verify_nginx_uploads.py`。

---

## 5. 测试 Gunicorn

```bash
cd /opt/printer-backend
source venv/bin/activate
gunicorn -c gunicorn_config.py app:app
```

确认启动成功（`Ctrl+C` 退出），检查输出无报错。

---

## 6. 注册 systemd 服务

```bash
# 复制服务文件
cp /opt/printer-backend/printer-backend.service /etc/systemd/system/

# 重载配置
systemctl daemon-reload

# 启动服务
systemctl start printer-backend

# 开机自启
systemctl enable printer-backend

# 查看状态
systemctl status printer-backend

# 查看日志
journalctl -u printer-backend -f
```

常用命令：
```bash
systemctl restart printer-backend   # 重启
systemctl stop printer-backend      # 停止
journalctl -u printer-backend -n 50 # 最近50条日志
```

---

## 7. 配置 Nginx

> **注意（防滥用限速）**：`nginx-http.conf` / `nginx-https.conf` 已内置 IP 维度 `limit_req`
> 限速（上传接口 30r/m、其余 /api/ 60r/m，Socket.IO 豁免）。**升级时若服务器上还是旧版配置，
> 需要重新 `cp` 一份新版配置并 `nginx -t && systemctl reload nginx`**，否则限速不生效。
> 共享出口 IP（学校/办公 NAT）场景若误杀，调大 rate/burst 或删除对应 `limit_req` 即可，
> 后端每用户配额与排队超时淘汰仍是核心防线。

### 仅 HTTP（测试阶段）

```bash
# 复制配置
cp /opt/printer-backend/nginx-http.conf /etc/nginx/sites-available/printer-backend

# 修改 server_name
nano /etc/nginx/sites-available/printer-backend
# 将 your-domain.com 改为你的域名或 IP

# 启用站点
ln -s /etc/nginx/sites-available/printer-backend /etc/nginx/sites-enabled/

# 测试配置
nginx -t

# 重载
systemctl reload nginx
```

### HTTPS（生产环境）

```bash
# 先按 HTTP 配置好
# 安装 Certbot
apt install -y certbot python3-certbot-nginx

# 获取证书
certbot --nginx -d your-domain.com

# 证书会自动续期（已内置 timer）
systemctl status certbot.timer
```

如需手动使用带 SSL 的配置：
```bash
cp /opt/printer-backend/nginx-https.conf /etc/nginx/sites-available/printer-backend
# 修改 server_name 和证书路径中的域名
```

---

## 8. 配置备份（S2-06 重构，2026-10-23）

`backup.sh` 现在：用 **SQLite 在线 backup API**（不是 `cp`，WAL 模式下 `cp` 会丢最近未 checkpoint 的订单）、
同时备 **orders.db + users.db**、备份后立刻 `integrity_check`、gzip、按 `RETENTION_DAYS` 清理、
支持 `--restore-check` 做一次**恢复演练**（恢复临时目录 → 核对完整性 + 各表行数 + 最新订单 + WAL 缺失）。

```bash
# 生产目录是 /home/printer-backend（不是 /opt），装 cron：
echo "0 3 * * * /home/printer-backend/backup.sh >> /var/log/printer-backup.log 2>&1" | crontab -

# 手动执行一次 + 当场恢复演练（必须看到 RESTORE_CHECK=ok）
bash /home/printer-backend/backup.sh --restore-check

# 可选：静态加密（配了 GPG 收件人就只留 .gz.gpg、删掉明文）与异地副本
BACKUP_GPG_RECIPIENT=hn-backup@example.com \
BACKUP_REMOTE=root@backup-host:/srv/hn-backups/ \
bash /home/printer-backend/backup.sh
```

⚠ **共享密钥不在仓库里**：GPG 公钥与异地主机需要运维自己准备；没有配这两个时脚本仍会正常出
`.gz` 到本机 `backups/`（并照常 `integrity_check` + 保留期清理），只是少了"加密 + 异地"两层。

## 8.1 依赖治理（S2-07）

`requirements.txt` 现在**逐行锁死版本**，且与线上 venv 的 `pip freeze` 对齐。规则：

- 新增依赖先在这里固定 `==`，再 `pip install -r requirements.txt`；不要在服务器上 `pip install <包>` 裸装。
- `eventlet` 已从依赖里删除（实时通道是 `async_mode="threading"` + gunicorn **gthread**）。
  服务器 venv 里可能还留着它（`pip` 不会自动卸载）；确认 `gunicorn_config.py` 里
  `worker_class` 不是 eventlet 后可以 `pip uninstall eventlet`。
- 发布前跑一次 CVE 扫描：`pip install pip-audit && pip-audit -r requirements.txt`
  （离线环境会失败，属于预期；联网机器上必须跑过）。
- 验收：`python tests/verify_dependencies.py`（锁版本 / 无 eventlet / import 与依赖对齐 + 变异自证）。

---

## 9. 前端配置

修改小程序 `utils/config.js` 中的 `BASE_URL`：

```javascript
BASE_URL: 'https://your-domain.com',   // 云服务器地址
```

然后在微信开发者工具中重新编译上传。

---

## 10. 验证部署

```bash
# 测试健康检查
curl http://127.0.0.1:5000/api/printer_status

# 测试通过 Nginx
curl http://your-domain.com/api/printer_status

# 查看服务状态
systemctl status printer-backend
systemctl status nginx
```

---

## 11. 设备台账维护（测试设备 / 陌生设备 / 僵尸接单记录）

`devices.json` 是设备注册表（谁连过、计算机名、绑了哪个成员、能否自动双面），既是
`/api/printer/devices` 的数据源，也是收支清算「授权」页的设备列表。**联调/冒烟脚本每运行一次
就会注册一台设备**，不清理就会越积越多。

> **线上实测（2026-10-12）**：52 台里 48 台是 `socketio-debug-*` / `debug-*`（历次联调脚本），
> 另有 2 台陌生云主机客户端 —— 授权页一屏全是"测试设备"，其中 4 条还挂着僵尸「接单中」
> （设备已离线，却仍在 `printer_claim.json` 的 `claiming_devices` 里）。清理后只剩 2 台真机。

### 三类设备与判据

| 类别 | 判据 | 处理 |
|---|---|---|
| **测试设备** | client_id 命中 `TEST_DEVICE_PREFIXES`（`debug-` / `socketio-debug` / `deploy-check` / `wscheck-` / `fallback-` / `staging-` / `test-device` / `pc-test` / `PRODTEST`） | 列表**默认隐藏**（只回计数）；`purge_test_devices` 每 30 分钟清掉其中**离线**的 |
| **陌生设备** | 从未绑定所有者（`owner_name` 为空）且离线 | 授权页「🧹 清理无主离线设备」/ 接口 `include_offline_unbound=true`（**人工判断**，不自动） |
| **僵尸接单记录** | 仍在 `claiming_devices` 里但设备已离线 | 随设备删除一并清空；授权页显示为「接单中（离线）」并提示可删除 |

**三条永不误伤的保护**（`cleanup` 接口内逐条校验）：① **在线设备**跳过（正在跑的机器不能删）；
② **已绑定所有者**的跳过；③ **人工盖过「自动双面」章**的跳过（`printer.manual_auto_duplex`
非空 —— 那是一条人工结论，例如"这台 1020 不支持自动双面"，删了就得让人重盖）。

真机被误删也无妨：**下次连接会自动重新登记**（只丢所有者绑定与接单开关），所以清理是幂等的。

### 标准动作（先备份、先预演、再执行、后复核）

```bash
# ① 备份（可回滚）
cd /home/printer-backend && cp -a devices.json devices.json.bak-$(date +%Y%m%d-%H%M%S) \
  && cp -a printer_claim.json printer_claim.json.bak-$(date +%Y%m%d-%H%M%S)

# ② 预演（接口默认 dry_run=true，只列不删）
TOKEN=$(/home/printer-backend/venv/bin/python -c "import sys;sys.path.insert(0,'.');import config;print(config.TOKEN)")
curl -s -X POST http://127.0.0.1:5000/api/printer/devices/cleanup \
  -H "X-Printer-Token: $TOKEN" -H 'Content-Type: application/json' -d '{"dry_run": true}'
#   陌生设备改用：-d '{"dry_run": true, "include_offline_unbound": true, "min_offline_days": 0}'

# ③ 核对清单无误后执行（dry_run 改 false）
curl -s -X POST http://127.0.0.1:5000/api/printer/devices/cleanup \
  -H "X-Printer-Token: $TOKEN" -H 'Content-Type: application/json' -d '{"dry_run": false}'

# ④ 复核：只应剩真实设备，接单数 = 真实在线设备数
curl -s "http://127.0.0.1:5000/api/printer/devices?token=$TOKEN"
```

授权页（收支清算 → 🔐 授权）有同样两个按钮，**先预览清单 → 确认 → 才真删**，日常优先用它。
`min_offline_days` 默认 7 天；「无主离线设备」按钮用 0 天（连今天刚连过一次的云主机临时客户端
也能清掉 —— 就是线上那 2 台的情形）。

### 经验：别让测试脚本污染生产台账

- **能本地跑就别连生产**：`/api/pull_queued_orders`、SocketIO 连接都会走 `register_device`，
  连一次就在 `devices.json` 留一条（`debug-*` 家族正是这么攒到 48 台的）。
- 脚本自造 client_id 时**必须带可识别前缀**（见 `TEST_DEVICE_PREFIXES`），后端会隐藏并自动清理；
  真机 id 形如 `<计算机名>-<8~12 位十六进制>`，不会撞上这些前缀。
- 冒烟脚本（如 `verify_deploy_smoke.py`）**跑完要删掉自己注册的设备**
  （`POST /api/printer/devices/delete`），中途失败也要在 `finally` 里清。
- 连过生产后自查一次授权页：有没有多出陌生设备（这一步能立刻发现脚本泄漏）。
- 验收：`python tests/verify_device_cleanup.py`（识别不误伤 / 隐藏与计数 / dry-run 不落盘 /
  在线与盖章设备跳过 / 删除同步清接单记录 / 定时任务幂等 / 页面按钮接线）。

---

## 常见问题

| 问题 | 解决 |
|---|---|
| 502 Bad Gateway | Gunicorn 未启动，`systemctl status printer-backend` 查看 |
| WebSocket 连接失败 | 检查 Nginx 中 `Upgrade` 和 `Connection` 头配置 |
| 文件上传失败 | 检查 `client_max_body_size`（Nginx）和 `MAX_CONTENT_LENGTH`（Flask） |
| 备份没有执行 | 检查 crontab：`crontab -l`；确保 backup.sh 有执行权限 |
| 服务无法启动 | `journalctl -u printer-backend -n 30` 查看错误日志 |

---

## 12. 安全加固（2026-10-23，S0-A）——线上真配置与运维脚本

本节对应 `docs/安全加固修复计划_2026-10-23.md` 的 S0-A 批次。**线上为唯一真相**，
下面这些文件就是 2026-10-23 在生产实际生效的版本（已脱敏、已入库，改动前请比对线上）。

| 文件 | 服务器位置 | 说明 |
|---|---|---|
| `nginx-production-hn-space.conf` | `/etc/nginx/sites-enabled/printer-backend` | 站点配置（`limit_conn_zone hn_conn`、`hn_sio`/`hn_login` 请求桶、`/socket.io/` 限速 + 120s 超时、`/api/` burst 150 + body 2m、上传 50m、profile 3m、登录 64k/conn 3）。**不要**用 `nginx-https.conf` 覆盖它（那是模板：含 `http2`、`proxy_read_timeout 3600s`、无 `hn_login`） |
| `nginx-conf-d-00-security-hardening.conf` | `/etc/nginx/conf.d/00-security-hardening.conf` | http 上下文：`client_header_timeout/client_body_timeout 10s`、`send_timeout 30s`、`keepalive_timeout 30s`、`reset_timedout_connection on`、`limit_req_status 429`、`limit_conn_status 429` |
| `ops/hn_metrics.py` | `/home/printer-backend/ops/` | S0-08 指标采集（cron 每分钟）：`logs/metrics-YYYYMMDD.csv` + `logs/alerts.log`；可选 Prometheus textfile |
| `ops/health_probe.py` | **另一台机器** | 外部拨测（每 60s，连续 2 次失败告警，`--selftest` 演练）；`HN_ALERT_WEBHOOK` 配企业微信/钉钉/自建 |

```bash
# 应用/回滚 nginx（回滚 ≤60s，reload 不断已建连接）
cp /root/nginx-backup-*/printer-backend /etc/nginx/sites-enabled/printer-backend
install -m 644 nginx-conf-d-00-security-hardening.conf /etc/nginx/conf.d/00-security-hardening.conf
nginx -t && systemctl reload nginx

# S0-08 指标 cron
cat > /etc/cron.d/hn-metrics <<'CRON'
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
* * * * * root cd /home/printer-backend && python3 ops/hn_metrics.py >> /home/printer-backend/logs/metrics-cron.log 2>&1
CRON

# 冒烟：匿名 Engine.IO 握手必须 403（S0-02 的门在工作）
curl -sS -o /dev/null -w '%{http_code}\n' "https://hn-space.cn/socket.io/?EIO=4&transport=polling"
```

**部署时的三个坑（本批实测）**

1. **gunicorn 的默认 access log 会把 `?token=` 写进 journald**（每台设备每次心跳一条）。
   `gunicorn_config.py` 必须保留 `access_log_format`（只记 `%(m)s %(U)s`，丢掉 query string），
   否则 S0-06 的"日志里没有 token"永远不成立。
2. **`client_max_body_size` 改成 server 级 2m 后别忘上传**：`/api/upload` 的 50m 是
   **location 级覆盖**，删掉那一行会静默把上传压到 2m。
3. `limit_conn` 命中是**并发**判定、`limit_req` 命中是**速率**判定：现场用单源 IP（服务器自身
   `--resolve` 到 127.0.0.1）才能验证出 429；从校园网多出口 IP 打只会分散到不同桶，永远看不到 429。

**运维动作（代码/配置之外）**

- 轮换 printer token：`config.py` 先写 `TOKEN=<新>` + `TOKEN_PREV=<旧>`（后端双接受），
  逐台更新门店 `print_config.json` 并确认 `/api/printer/devices` 200，最后删 `TOKEN_PREV`。
- 结算页「设置 → 敏感模块密码」7 个模块的默认 `123456` 必须逐个改成强密码（S0-07 的人工部分）。
- 全员重登：S0-07 换了登录 salt（`login-v2`），所有 7 天登录态一次性作废（D-3 已接受）。

---

## 13. 运行时切换（S3-01：gevent）— 可选，默认不启用

**为什么有这个开关**：threading + gthread `threads=16` 时，**每条 WebSocket 连接占住一个
worker 线程**。S2-12 压测（`tests/staging/08_staging_tiers.py`）实测：15 条连接就把 16 个
worker 占满、HTTP 全线超时；按 §11.2 `required_threads = ceil(在线设备×2.5)+4`，
3 台需 12（够）、6 台需 19、10 台需 29。gevent 把一条连接降为一个 greenlet，不再占 OS 线程：
**48 条连接时 `/api/ping` 仍全部 200、OS 线程只有 3 条**（`tests/staging/09_gevent_runtime.py`，
对照同一台机器上 gthread 16 线程 + 24 条连接 ⇒ HTTP 全超时）。

### 切换 / 回滚（只改一个环境变量）

```bash
# 切换：在 systemd unit 的 [Service] 里加一行，然后重启
sudo systemctl edit printer-backend            # 或直接编辑 unit
#   Environment=HN_RUNTIME=gevent
sudo systemctl daemon-reload && sudo systemctl restart printer-backend

# 回滚：删掉那一行（或改成空值），重启即可
#   HN_RUNTIME 未设 = gthread + threads=16，行为与 2026-10-24 之前完全一致
sudo systemctl restart printer-backend
```

**app.py 与 gunicorn_config.py 读的是同一个变量**，不要只改一处：worker_class=gevent 必须配
async_mode=gevent，交叉组合会重演 2026-09 那次 eventlet/gthread 混用的「连接建立与数据面脱节」
（客户端 SSL 握手 EOF、服务端 48 分钟后才发现死连接）。

### 部署前后检查清单

```bash
# 1) 依赖（requirements.txt 已锁 gevent==26.9.0）
/home/printer-backend/venv/bin/pip install -r requirements.txt
/home/printer-backend/venv/bin/python -c "import gevent; print(gevent.__version__)"

# 2) 启动日志必须出现 gevent worker（不是 gthread）
journalctl -u printer-backend -n 30 | grep -E "Booting worker|gevent|gthread"

# 3) 线上冒烟（与 S0 相同）：匿名握手 403、设备能连、HTTP 拉单保活
python tests/verify_deploy_smoke.py          # 在本机跑（scp 到服务器，见脚本说明）
curl -sS -o /dev/null -w '%{http_code}\n' "https://hn-space.cn/socket.io/?EIO=4&transport=polling"  # 403

# 4) 关键业务：门店机重连后接一单，确认打印成功回报（这是 gevent 下 SQLite/推送路径的实机验证）
```

### gevent 模式的已知代价（切之前接受）

1. **SQLite 的 C 调用不受 monkey-patch 影响**：一次 `BEGIN IMMEDIATE` 等写锁会阻塞整个
   greenlet 事件循环（线程模型下只阻塞一个线程）。缓解：写事务保持短（S2-02 已做）、
   `09` 实验里 32 条连接 + 10 个并发写的 p95 约 40 ms、写期间 ping 全部 200 —— **但如果将来
   出现长事务/大文件清理占住写锁，症状会是"全站卡一下"而不是"一个请求慢"**。出现时优先查
   慢查询/长事务，别先怪 gevent。
2. **`workers` 仍必须为 1**：`printer_clients` / `pushed_tasks` / 限速桶 / `sid→client_id`
   都是进程内内存。gevent **不解决**多进程一致性；`workers>1` 是 S3-01 的后半段（要先把这些
   迁到 Redis），本次没做。gevent 只解决「一条连接占一个 worker」。
3. **APScheduler**：BackgroundScheduler 的线程池在 gevent 下变成 greenlet，定时任务里的
   SQLite/网络调用会协程化（`POST /job` 实测 `process_pending_orders` 2.6 ms 成功）；但如果
   某个任务里出现**长时间阻塞的 C 调用**（如一次性扫描巨大目录），同样会卡住事件循环。
   任务侧保持"小而频繁"的既有口径即可。
4. **调试体验**：所有请求在一条 OS 线程里跑，`py-spy dump` 只能看到 hub；排查卡死优先看
   `/api/ping` 是否还响应 + greenlet 数（`tests/staging/srv.py` 的 `/stats`）。

### 什么时候该切

- 现在（3 台）：**不必切**。16 线程需 12，余量 4，S2-12 实测 3 台档 1044 req/s、orders p95 31 ms。
- 设备数将达 5–6 台：**切换 gevent**（或先把 threads 调到 24，二选一，见 §11.2）。
- 设备数 ≥10 台或需要 `workers>1` 做滚动重启不中断：**S3-01 后半段**（实时通道拆进程 + Redis）。
