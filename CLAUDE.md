# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

HN 打印系统 — 两个子系统共享定价模型和打印管线：

| 子系统 | 目录 | 入口 | 用途 |
|--------|------|------|------|
| 本地打印工具 | `local_print_tool/` | `python main.py` | Windows 桌面应用（PySide6），拖放文件一键批量打印 |
| 云打印系统 | `mobile_apps/` | 见 `mobile_apps/CLAUDE.md` | 三组件：微信小程序 + Android App + Flask 后端 |

**桥接点**: `local_print_tool/cloud_client.py` 让本地打印工具也能接收云端任务（SocketIO 长连接 + HTTP 拉取双通道），但本地工具的主职仍是手动批量打印。

还有一个已废弃的备份目录 `放弃的项目（已重启）/`，是早期版本副本，已不再维护。

## 本地打印工具架构 (`local_print_tool/`)

### 模块职责

| 文件 | 职责 |
|------|------|
| `main.py` | 程序入口：stderr 过滤（滤掉 iCCP/PointSize 无害警告）、依赖检查、启动 QApplication |
| `gui.py` | PySide6 主窗口（~5500 行）：文件列表表格、编辑面板（份数/双面/页码范围/DPI）、进度条（逐面更新）、拖放/Ctrl+V 粘贴、浅色/深色主题、打印队列管理 |
| `converter.py` | 通用文件转 PDF：TXT/CSV/图片→reportlab，Markdown→wkhtmltopdf，Office 文档→Word COM→WPS COM→LibreOffice 三引擎智能降级，COM 后台预热 |
| `pdf_printer.py` | PDF 静默打印（Windows GDI 原生 → SumatraPDF → 应用层循环，三级降级），DEVMODE 设份数/双面/翻转模式 |
| `printer_config.py` | `PrintJob` 数据类、配置读写（`print_config.json`）、计费函数（`calc_cost`，与后端 `calculate_price` 核心逻辑一致）、单面 0.2/双面 0.3 |
| `order_tabs.py` | Tab 标签栏 + 文件卡片组件（QLabel+QSS 渲染，替代自定义 paintEvent 避免 IndexError） |
| `cloud_client.py` | `CloudClient(QObject)`：SocketIO 长连接接收云端任务 → Signal 发射到 GUI 主线程，支持断线重连/HTTP 拉取补充 |
| `offline_sync.py` | `OfflineSync` 类：离线时订单暂存本地 SQLite，联网后自动上传，最多重试 5 次 |
| `theme_manager.py` | 主题管理器：跟随系统/浅色/深色三种模式，写 `theme_settings.json` |
| `main.py:76` 附近 | `_disable_combo_wheel()`、`_truncate_filename()`、`_enable_smooth_scroll()` — 通用 UI 辅助函数 |

### 打印管线

```
文件 → 类型检测 → 转换引擎 → PDF → Windows GDI 打印（三级降级）
```

- **PDF** → PyMuPDF 渲染 300 DPI 位图 → GDI 打印（DEVMODE 配置）
- **Word** → COM 自动化（Word → WPS → LibreOffice 兜底）
- **图片** → reportlab 排入单页 PDF
- **Markdown/HTML** → markdown 库 → wkhtmltopdf
- **TXT/CSV** → reportlab 简单排版

### 打包发布

两个 PyInstaller `.spec` 文件：
- `HN打印工具.spec` — Release 打包（无控制台）
- `HN打印工具_debug.spec` — Debug 打包（带控制台，用于现场排查崩溃）

构建输出在 `local_print_tool/dist/` 和 `local_print_tool/build/`，已有 Release 版本。

### 调试辅助

- `crash_traceback.txt` — 记录未捕获异常的完整 traceback，Python 打包后崩溃时优先查看此文件
- 日志输出到 `logs/local_tool.log`，logging 级别在模块内各自设置

## 云打印系统架构

详见 `mobile_apps/CLAUDE.md`，此处仅提跨系统要点。

### 父子订单模型

后端用两张表建模一次提交包含多个文件的场景：
- `orders` — 父订单容器，存聚合状态和附加服务参数（派送/加急/首页/地址）
- `order_files` — 子任务，每个文件一行，有独立的 `copies`、`page_range`、`duplex`、`status`

父订单状态通过 `aggregate_order_status()` 从子任务聚合：优先级 `failed > printing > queued > sent > canceled`。全部 `sent` 才算完成。

本地工具的 `PrintJob` 数据类也对应子任务概念，`task_id` 字段存 `order_files.id`（0 = 本地任务）。

### 双通道任务分发

云端任务通过两条路径到达打印机：
1. **SocketIO 实时推送** — `push_print_task_to_client()` 原子标记子任务为 `printing` 后 `emit("print_task", ...)`
2. **HTTP 拉取** — `GET /api/pull_queued_orders`，`fetch_and_lock_task()` 用 `db_lock` + `BEGIN IMMEDIATE` 保证多打印机并发拉取不重复分配

两种路径都遵循"先锁后推"原则。

### MD5 文件去重

后端 `uploads/md5_index.json` 索引所有上传文件。同 MD5 文件复用磁盘存储，不同用户/订单共享同一物理文件。索引兼容新旧两种格式。

本地工具 `cloud_client.py` 也有独立的 PDF 缓存（`pdf_cache/`），按源文件 MD5 索引，用于避免重复转换 Office 文档。

### 定价模型（两端共享）

单面 0.2 元/页，双面 0.3 元/张（每张印两页，奇数页最后一张按单面 0.2 元计费）。附加服务（派送百分比、加急费、首页费）在两端的 config 中都有对应字段。

### 页数分析流程

Word 文档上传后页数未知（后端返回 0）：
1. 后端通过 SocketIO 推 `analyze_page_count` → 本地工具下载 → COM 转 PDF 统计页数 → 回报后端
2. 后端更新 `files.page_count` + `files.page_count_verified`，写入 MD5 索引
3. 下次同 MD5 文件上传直接复用已验证页数

### 断线恢复

多层防护：
- 客户端 `disconnect` → 回滚该客户端名下所有 `printing` 子任务 → `queued`
- `check_printing_timeout`（60s 定时）：超过 3 分钟未反馈的推送任务标记 `failed`
- `recover_orphaned_printing_tasks`（2min 定时）：超过 5 分钟的孤儿 `printing` 任务回退 `queued`
- 启动时 `recover_stale_printing_tasks()`：进程崩溃后残留的 `printing` 全部重置

### 角色与许可系统

`compute_role(openid)` → `super_admin` / `admin` / `user` / `guest`：
- `admin` 创建限时许可密钥（8 位字母数字，1-10 分钟有效），`guest` 兑换后获得临时 `temp_until`
- 提交订单时消费临时授权（`temp_until` 清空，关联 `license_keys.order_id`）
- `admin` 类型的永久密钥仅超级管理员可创建

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
python app.py                     # Flask 开发服务器，127.0.0.1:5000

# Android App（Capacitor WebView，无需微信）
cd mobile_apps/android_app
npx cap sync android && npx cap open android
# 开发预览：直接用浏览器打开 index.html

# 微信小程序：用微信开发者工具打开 mobile_apps/h_n_print/
```

## 部署（生产环境）

需自行准备 Linux 服务器（Ubuntu 22.04+），配置域名指向、SSL 证书和反向代理。

```bash
# 后端打包 + 上传 + 重启（替换 YOUR_SERVER 为你的服务器地址）
cd mobile_apps/printer-backend
tar czf - --exclude='orders.db' --exclude='users.db' \
    --exclude='uploads' --exclude='__pycache__' \
    --exclude='venv' --exclude='*.pyc' . \
  | ssh root@YOUR_SERVER \
    "cd /home/printer-backend && tar xzf - && systemctl restart printer-backend"

# 验证
ssh root@YOUR_SERVER "systemctl status printer-backend --no-pager"
ssh root@YOUR_SERVER "curl -s http://127.0.0.1:5000/api/ping"
ssh root@YOUR_SERVER "journalctl -u printer-backend --since '5 minutes ago' --no-pager | tail -50"
```

## 关键配置文件

| 文件 | 说明 |
|------|------|
| `local_print_tool/print_config.json` | 本地工具配置（打印机、价格、任务列表），自动生成 |
| `local_print_tool/theme_settings.json` | 主题设置，自动生成 |
| `mobile_apps/printer-backend/config.py` | 后端配置（需手动创建），含 `WECHAT_APPID`/`SECRET_KEY`/`ADMIN_OPENIDS` 等，不提交 git |
| `mobile_apps/printer-backend/pricing.json` | 定价配置，供小程序 `/api/pricing` 接口读取 |
| `mobile_apps/h_n_print/utils/config.js` | 小程序 API 地址 `BASE_URL` |

## 外部依赖（本地打印工具独占）

需安装到 Windows 系统的第三方软件：
- **Microsoft Word / WPS** — COM 自动化转 PDF（首选引擎，比 LibreOffice 更可靠）
- **LibreOffice** — Office 文档转 PDF 的兜底引擎（`soffice --headless`）
- **wkhtmltopdf** — HTML/Markdown 转 PDF
- **SumatraPDF** — Windows 静默打印（三级降级策略的第二级，GDI 优先失败后兜底）
