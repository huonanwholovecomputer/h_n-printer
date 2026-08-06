# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

HN 云打印 — 微信小程序云打印系统，两个组件协作：

| 组件 | 目录 | 技术栈 |
|---|---|---|
| 微信小程序前端 | `h_n_print/` | 微信原生框架 (Component 模式, 自定义 tabBar) |
| 后端 API 服务 | `printer-backend/` | Flask + Flask-SocketIO + SQLite + APScheduler |

**数据流**: 用户小程序上传文件 → 后端存储(MD5去重) → SocketIO 实时推送/HTTP 拉取 → Windows 客户端下载渲染 → GDI 直打打印机

## 后端架构 (`printer-backend/app.py`)

单文件 Flask 应用 (约 5700 行)，核心子系统：

- **数据库**: SQLite (WAL 模式)，3 张主表 — `files`(MD5去重), `orders`(父订单), `order_files`(子任务，v5引入，支持一单多文件)。`users` 表存头像/昵称/角色。`license_keys` 表存临时许可密钥。
- **父子订单模型**: `orders` 是聚合容器，`order_files` 是实际打印子任务。每个子任务有独立的 `copies`, `page_range`, `duplex`, `status`。父订单状态通过 `aggregate_order_status()` 从子任务聚合（优先级: failed > printing > queued > sent > canceled）。
- **任务分发**: 双通道 — ① SocketIO `print_task` 事件实时推送（`push_print_task_to_client`）② HTTP `GET /api/pull_queued_orders` 供客户端主动拉取（`fetch_and_lock_task` 原子取锁防重复）。两种方式都先将子任务标记为 `printing` 再推送/返回。
- **角色体系**: `compute_role(openid)` → super_admin / admin / user / guest。admin 创建限时许可密钥(temp 1-10分钟或 admin 永久)，guest 兑换后获得临时 `temp_until`。提交订单时消费临时授权(`temp_until` 清空，关联 `license_keys.order_id`)。
- **文件存储**: 按扩展名分子目录 (`pdf/`, `docx/`, `png/` 等)，MD5 索引文件 (`uploads/md5_index.json`) 去重。可配置保留时间，APScheduler 定时清理过期文件。
- **定时任务** (APScheduler): `process_pending_orders`(30s), `check_printing_timeout`(60s), `cleanup_expired_files`(10min), `recover_orphaned_printing_tasks`(2min), `cleanup_expired_license_keys`(10min)。
- **断线恢复**: 客户端 disconnect 时回滚其名下所有 `printing` 子任务→`queued`。启动/定时扫描回收超过 5 分钟的孤儿 `printing` 任务。
- **认证**: `login_required` 装饰器验证 Bearer token (itsdangerous 签名, 7天有效)。`require_printer_access` 额外检查非 guest 角色。

关键配置从 `printer-backend/config.py` 加载 (需手动创建，含 WECHAT_APPID/SECRET_KEY/TOKEN/ADMIN_OPENIDS 等)。

## 微信小程序 (`h_n_print/`)

- **页面**: `pages/index/index`(首页，文件选择+上传+提交), `pages/me/me`(个人中心，订单列表+许可密钥+管理员面板), `pages/order-detail/order-detail`, `pages/my-performance/my-performance`(月度统计), `pages/authorized-users/authorized-users`(历史授权用户列表，管理员/超管可见), `pages/user-orders/user-orders`(按 openid/来源查看订单列表，含分页与状态过滤，供管理员查看某用户/本地任务的打印记录)
- **自定义滚动引擎**: index 和 me 页面都实现了手写的橡皮筋物理滚动（`_initScrollEngine` / `_startPhysics` / `_snapBack`），通过 `translateY` 驱动，含惯性衰减、阻尼过拉、方向锁定。非原生 scroll-view。
- **自定义 tabBar**: `custom-tab-bar/` 组件。
- **多文件上传**: 每个文件独立进度条（`wx.uploadFile` + `onProgressUpdate`），支持上传中移除。
- **API 地址**: `utils/config.js` 中的 `BASE_URL`，部署时修改。

#### 首页文件卡片动态高度模型（`pages/index/index.js`）

首页文件列表是**有界 scroll-view**（显式 `height` 驱动），卡片高度**按类型硬编码为常量**、同步求和得出列表高度——刻意不做异步实测以避免闪烁。**任何改动不得破坏"每类型恒定高度"前提**：

- `FILE_CARD_HEIGHT_RPX`：`image` / `word-grid` / `word-grid-single` / `word-text` / `word-text-single` / `word-single` / `excel` 七种类型常量（rpx）。
- `_fileCardTypeKey(file)`：按 `pageCount` / `pageCountStatus` / `singlePage` 分派类型。
- `_fileCardHeightRpx(file)`：类型基础高 + 文本模式（页数未知）每多一行范围 +60rpx。
- `_recalcFileListHeight()`：重算列表高度（补间动画）。**页数确认（文本↔网格切换）、`singlePage` 判定变化、范围行增删等任何会改变卡片高度的操作都必须调用它。**
- `_probeCardHeights()`：开发者工具控制台执行 `getCurrentPages()[0]._probeCardHeights()`，注入 7 张卡片实测高度并输出建议常量。**改卡片布局后必须重新探测校准。**

**`singlePage` 派生判定**（有效选择恰好 1 页）：`_computeSinglePage()` → `file.singlePage`。整份 1 页或范围恰好选中 1 页 → 模式行隐藏、提交强制单面。由 `_refreshSinglePage()` 同步并重算列表高度。

**范围控件三态**：页数已知 → 摘要按钮点击弹数字网格选择器（`onOpenRangePicker`，含全部/单页=奇数/双页=偶数一键，`onConfirmRangePicker` 回写 `rangeLines`+`pageRange`）；页数未知 → 黄色警告 + 多行文本输入（占位符随行号变化、新增行带弹出动画）；整份 1 页 → 整行隐藏。

## 部署

参考 `printer-backend/DEPLOY.md`:
```bash
# 后端 (Ubuntu 22.04)
cd /opt/printer-backend
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp config.py.example config.py  # 填写微信/服务器配置
systemctl start printer-backend  # gunicorn + eventlet

# Nginx 反向代理 (含 WebSocket 升级)
cp nginx-http.conf /etc/nginx/sites-available/printer-backend
ln -s ... && nginx -t && systemctl reload nginx

# 备份
bash backup.sh  # crontab 每天凌晨3点
```

## 价格模型

`calculate_price(page_count, duplex)`:
- 单面: 0.2元/页
- 双面: 0.3元/张 (每张纸印两页，奇数页最后一张按 0.2元 单面计费)
- 管理员提交的订单 `is_free=1`，不计费

## 关键文件索引

| 文件 | 作用 |
|---|---|
| `printer-backend/app.py` | 全部后端逻辑（路由/SocketIO/数据库/定时任务） |
| `printer-backend/config.py` | 后端配置（需手动创建，不提交 git） |
| `printer-backend/DEPLOY.md` | 部署指南 |
| `printer-backend/gunicorn_config.py` | Gunicorn + eventlet 配置 |
| `printer-backend/nginx-http.conf` | Nginx 反向代理配置模板 |
| `printer-backend/backup.sh` | 数据库备份脚本 |
| `h_n_print/app.json` | 小程序页面/窗口/tabBar 注册 |
| `h_n_print/utils/config.js` | 小程序 API 地址配置 |
| `h_n_print/pages/index/index.js` | 首页：文件选择/上传/提交/滚动引擎 |
| `h_n_print/pages/me/me.js` | 个人中心：订单/许可密钥/管理员/滚动引擎 |
