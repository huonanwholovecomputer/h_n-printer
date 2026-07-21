# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

HN 打印系统 — 两个子系统共享定价模型和打印管线：

| 子系统 | 目录 | 入口 | 用途 |
|--------|------|------|------|
| 本地打印工具 | `local_print_tool/` | `python main.py` | Windows 桌面应用，拖放文件一键批量打印 |
| 云打印系统 | `mobile_apps/` | 见 `mobile_apps/CLAUDE.md` | 四组件：小程序 + Android App + Flask 后端 + Windows 打印客户端 |

**桥接点**: `local_print_tool/cloud_client.py` 让本地打印工具也能接收云端任务（SocketIO 长连接 + HTTP 拉取双通道），但本地工具的主职仍是手动批量打印。

云打印系统包含四个组件：
- `h_n_print/` — 微信小程序（原生框架，自定义 tabBar）
- `android_app/` — Capacitor 打包的 Android WebView 应用（纯 HTML/CSS/JS，用 `/api/device_login` 代替微信登录）
- `printer-backend/` — Flask + SocketIO + SQLite 后端
- `printer_client/` — 独立 Windows 命令行打印客户端（不依赖 PySide6）

详细架构见 `README.md`（本地工具）和 `mobile_apps/CLAUDE.md`（云打印系统），此处仅补充跨系统关键信息。

## 开发命令

```bash
# 本地打印工具
cd local_print_tool
pip install -r requirements.txt
python main.py

# 云打印后端（开发模式）
cd mobile_apps/printer-backend
pip install -r requirements.txt
cp config.py.example config.py   # 填写微信/服务器配置
python app.py                     # 监听 127.0.0.1:5000

# 独立打印机客户端（命令行，不依赖 PySide6）
cd mobile_apps/printer_client
pip install -r requirements.txt
# 创建 config.py（CLOUD_API_URL, WEBSOCKET_URL, TOKEN, PRINTER_NAME）
python printer_client.py

# Android App（Capacitor WebView，无需微信）
cd mobile_apps/android_app
npx cap sync android
npx cap open android   # 在 Android Studio 中 Build APK
# 开发预览：直接用浏览器打开 index.html

# 微信小程序：用微信开发者工具打开 mobile_apps/h_n_print/
```

## 部署（生产环境）

```bash
# 后端部署到 Ubuntu 22.04（参考 更新后端服务.txt）
cd mobile_apps/printer-backend
rsync -avz --exclude 'orders.db' --exclude 'uploads/' \
       --exclude '__pycache__/' --exclude 'venv/' --exclude '*.pyc' \
       . root@82.156.66.18:/home/printer-backend/
ssh root@82.156.66.18
cd /home/printer-backend
source venv/bin/activate
pip install -r requirements.txt
systemctl restart printer-backend
systemctl status printer-backend
journalctl -u printer-backend --since "1 minute ago" -f
curl http://127.0.0.1:5000/api/ping

# 数据库备份
cd mobile_apps/printer-backend
scp root@82.156.66.18:/home/printer-backend/orders.db .
```

## 跨系统架构要点

### 父子订单模型 (v5+)

后端用两张表建模一次提交包含多个文件的场景：
- `orders` — 父订单容器，存聚合状态和附加服务参数（派送/加急/首页/地址）
- `order_files` — 子任务，每个文件一行，有独立的 `copies`、`page_range`、`duplex`、`status`

父订单状态通过 `aggregate_order_status()` 从子任务聚合：优先级 `failed > printing > queued > sent > canceled`。全部 `sent` 才算完成。

本地打印工具的 `PrintJob` 数据类也对应子任务概念，`task_id` 字段存 `order_files.id`（0 = 本地任务）。

### 双通道任务分发

云端任务通过两条路径到达打印机：

1. **SocketIO 实时推送** — `push_print_task_to_client()` 原子标记子任务为 `printing` 后 `emit("print_task", ...)`
2. **HTTP 拉取** — `GET /api/pull_queued_orders`，`fetch_and_lock_task()` 用 `db_lock` + `BEGIN IMMEDIATE` 保证多打印机并发拉取不重复分配

两种路径都遵循"先锁后推"原则：数据库状态先变更为 `printing`，再推送/返回。emit 失败则回滚为 `queued`。

### MD5 文件去重

后端 `uploads/md5_index.json` 索引所有上传文件。同 MD5 文件复用磁盘存储，不同用户/订单共享同一物理文件。索引兼容新旧两种格式（旧：`{md5: rel_path}`，新：`{md5: {path, original_name, page_count, page_count_verified}}`）。

本地工具 `cloud_client.py` 也有独立的 PDF 缓存（`pdf_cache/`），按源文件 MD5 索引，用于避免重复转换 Office 文档。

### 定价模型（两端共享）

单面 0.2 元/页，双面 0.3 元/张（每张印两页，奇数页最后一张按单面 0.2 元计费）。

本地工具 `printer_config.py` 和 后端 `app.py` 各自实现了 `calc_cost` / `calculate_price`，核心逻辑一致。附加服务（派送百分比、加急费、首页费）在两端的 config 中都有对应字段。

### 页数分析流程

Word 文档 (.doc/.docx) 上传后页数未知（后端返回 0）。流程：
1. 后端通过 SocketIO `analyze_page_count` 事件推送给本地打印工具
2. 本地工具下载文件 → 用 Word/WPS COM 转 PDF → 统计页数 → `page_count_result` 回报后端
3. 后端更新 `files.page_count` + `files.page_count_verified`，同步写入 MD5 索引
4. 下次同 MD5 文件上传直接复用已验证页数，跳过分析

### 断线恢复

多层防护：
- 客户端 `disconnect` → 回滚该客户端名下所有 `printing` 子任务 → `queued`
- `check_printing_timeout`（60s）：超过 3 分钟未反馈的推送任务标记 `failed`
- `recover_orphaned_printing_tasks`（2min）：超过 5 分钟的孤儿 `printing` 任务回退 `queued`
- 启动时 `recover_stale_printing_tasks()`：进程崩溃后残留的 `printing` 全部重置

### 角色与许可系统

`compute_role(openid)` → `super_admin` / `admin` / `user` / `guest`。
- `admin` 创建限时许可密钥（8 位字母数字，1-10 分钟有效），`guest` 兑换后获得临时 `temp_until`
- 提交订单时消费临时授权（`temp_until` 清空，关联 `license_keys.order_id`）
- `admin` 类型的永久密钥仅超级管理员可创建，兑换后设 `users.role = 'admin'`

## 关键配置文件

| 文件 | 说明 |
|------|------|
| `local_print_tool/print_config.json` | 本地工具配置（打印机、价格、任务列表），自动生成 |
| `local_print_tool/theme_settings.json` | 主题设置，自动生成 |
| `mobile_apps/printer-backend/config.py` | 后端配置（需手动创建，含微信/密钥/管理员），不提交 git |
| `mobile_apps/printer_client/config.py` | 打印机客户端配置（需手动创建），不提交 git |
| `mobile_apps/h_n_print/utils/config.js` | 小程序 API 地址 |
| `mobile_apps/printer-backend/pricing.json` | 定价配置（供前端 `/api/pricing` 接口读取） |

## 外部依赖

本地打印工具和打印机客户端都依赖 Windows 外部程序：
- **LibreOffice** — Office 文档转 PDF 的兜底引擎（`soffice --headless`）
- **wkhtmltopdf** — HTML/Markdown 转 PDF
- **SumatraPDF** — Windows 静默打印（三级降级策略的第二级）
- **Microsoft Word / WPS** — COM 自动化转 PDF（本地工具优先使用，比 LibreOffice 更可靠）

`local_print_tool/converter.py` 实现了 Word COM → WPS COM → LibreOffice 的三引擎智能降级。
