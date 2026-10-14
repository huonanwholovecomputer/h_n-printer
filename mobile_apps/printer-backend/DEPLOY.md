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

## 8. 配置备份

```bash
# 设置每天凌晨 3 点自动备份
echo "0 3 * * * /opt/printer-backend/backup.sh >> /var/log/printer-backup.log 2>&1" | crontab -

# 手动执行一次测试
bash /opt/printer-backend/backup.sh
```

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
