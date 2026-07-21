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

## 常见问题

| 问题 | 解决 |
|---|---|
| 502 Bad Gateway | Gunicorn 未启动，`systemctl status printer-backend` 查看 |
| WebSocket 连接失败 | 检查 Nginx 中 `Upgrade` 和 `Connection` 头配置 |
| 文件上传失败 | 检查 `client_max_body_size`（Nginx）和 `MAX_CONTENT_LENGTH`（Flask） |
| 备份没有执行 | 检查 crontab：`crontab -l`；确保 backup.sh 有执行权限 |
| 服务无法启动 | `journalctl -u printer-backend -n 30` 查看错误日志 |
