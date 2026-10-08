# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

HN 云打印 — 微信小程序云打印系统，三个组件协作：

| 组件 | 目录 | 技术栈 |
|---|---|---|
| 微信小程序前端 | `h_n_print/` | 微信原生框架 (Component 模式, 自定义 tabBar) |
| Android App | `android_app/` | Capacitor WebView（页面逻辑与小程序同源，`www/` 即前端源码） |
| 后端 API 服务 | `printer-backend/` | Flask + Flask-SocketIO + SQLite + APScheduler |

**数据流**: 用户小程序上传文件 → 后端存储(MD5去重) → SocketIO 实时推送/HTTP 拉取 → Windows 客户端下载渲染 → GDI 直打打印机

## 后端架构 (`printer-backend/app.py`)

单文件 Flask 应用 (约 7100 行)，核心子系统：

- **数据库**: SQLite (WAL 模式)，主表 — `files`(MD5去重), `orders`(父订单), `order_files`(子任务，v5引入，支持一单多文件), `finance_config`(收支清算云端配置)。`users` 表存头像/昵称/角色/`bound_openid`(APP 设备账号绑定微信)。`license_keys` 表存临时许可密钥。
- **父子订单模型**: `orders` 是聚合容器，`order_files` 是实际打印子任务。每个子任务有独立的 `copies`, `page_range`, `duplex`, `image_orientation`, `status`。父订单状态通过 `aggregate_order_status()` 从子任务聚合（优先级: failed > printing > queued > sent > canceled）。
- **任务分发**: 双通道 — ① SocketIO `print_task` 事件实时推送（`push_print_task_to_client`）② HTTP `GET /api/pull_queued_orders` 供客户端主动拉取（`fetch_and_lock_task` 原子取锁防重复）。两种方式都先将子任务标记为 `printing` 再推送/返回。**两通道独立保活（2026-09）**：设备「在线」= `printer_clients[client_id]["heartbeat"]` 在 90s 内（`CLIENT_HEARTBEAT_TIMEOUT`），`/api/pull_queued_orders` 入口也调 `touch_printer_heartbeat(client_id)` —— 否则 socket 掉线但网络正常时（本机代理掐断长连接、握手超时）设备被判离线，HTTP 拉单永远返回空，「双通道」实为单通道。HTTP 建的条目 `sid=None`：`push_print_task_to_client` 取不到 sid 会把子任务回滚 `queued`（`[WAIT]` 日志）等下次 HTTP 拉取，其余 emit 路径都有 `if sid` 守卫，不会误广播。**接单唯一接管（2026-11）**：多设备共连时两条通道都只发给「启用接单」的设备——`printer_claim.json` 持久化唯一接管者，`get_active_printer_client()` 判定在线接管者，页数分析/预约下发同样只走接单设备。
- **设备注册表与授权**: `devices.json` 记录每台设备的 `device_name`/`owner_name`(授权页绑定成员)/首末次在线时间；`GET /api/printer/devices` 展示全部设备（计算机名/在线/所有者/是否接单），`POST /api/printer/claim|release|bind_owner` 管理接管与所有者绑定，`POST /api/printer/devices/delete` 删除设备（清理升级产生的历史遗留 ID，删除接单设备时自动清空接管）。推送 payload 附带 `bound_owner_name`（从 `finance_config.memberBindings` 反查下单 openid 的绑定成员），本地工具据此设置订单标签页归属。
- **在线设备日志收集**: `POST /api/log/collect_all`（printer token）向所有在线客户端推送 SocketIO `request_log`，客户端回报本机日志（`logs` 事件，request_id 防串台，每台上限 200KB，超时标记 error）；配合本地工具「日志管理」一键收集。
- **角色体系**: `compute_role(openid)` → super_admin / admin / user / guest。admin 创建限时许可密钥(temp 1-10分钟或 admin 永久)，guest 兑换后获得临时 `temp_until`。提交订单时消费临时授权(`temp_until` 清空，关联 `license_keys.order_id`)。
- **文件存储**: 按扩展名分子目录 (`pdf/`, `docx/`, `png/` 等)，MD5 索引文件 (`uploads/md5_index.json`) 去重。可配置保留时间，APScheduler 定时清理过期文件。
- **定时任务** (APScheduler): `process_pending_orders`(30s), `check_printing_timeout`(60s), `cleanup_expired_files`(10min), `recover_orphaned_printing_tasks`(2min), `cleanup_expired_license_keys`(10min)。
- **断线恢复**: 客户端 disconnect 时回滚其名下所有 `printing` 子任务→`queued`。启动/定时扫描回收超过 5 分钟的孤儿 `printing` 任务。
- **认证**: `login_required` 装饰器验证 Bearer token (itsdangerous 签名, 7天有效)。`require_printer_access` 额外检查非 guest 角色。

关键配置从 `printer-backend/config.py` 加载 (需手动创建，含 WECHAT_APPID/SECRET_KEY/TOKEN/ADMIN_OPENIDS 等)。`/api/admin/statistics/revenue` 订单明细的 `nickname` 实时查 `users` 表（APP 设备绑定微信后订单 openid 迁移为微信 openid → 昵称即微信昵称，修改昵称自动同步）。

## 微信小程序 (`h_n_print/`)

- **页面**: `pages/index/index`(首页，文件选择+上传+提交), `pages/me/me`(个人中心，订单列表+许可密钥+管理员面板), `pages/order-detail/order-detail`, `pages/my-performance/my-performance`(月度统计), `pages/authorized-users/authorized-users`(历史授权用户列表，管理员/超管可见), `pages/user-orders/user-orders`(按 openid/来源查看订单列表，含分页与状态过滤，供管理员查看某用户/本地任务的打印记录)
- **自定义滚动引擎**: index 和 me 页面都实现了手写的橡皮筋物理滚动（`_initScrollEngine` / `_startPhysics` / `_snapBack`），通过 `translateY` 驱动，含惯性衰减、阻尼过拉、方向锁定。非原生 scroll-view。小程序侧实现在 **WXS**（`utils/scroll.wxs`，视图层 rAF+setStyle，0 setData），APP 侧是同构的 JS `FlingEngine`（`www/app.js`）。
- **嵌套滚动接力（内层文件列表 ↔ 外层页面）**: 首页文件列表是**有界 scroll-view**（内容高 > 列表高时可内部滚动），手势落到列表内时默认让位原生；滚到列表**贴顶/贴底**后，必须把滚动交给外层页面接管，否则手指一直被列表吞掉、整页滚不动；反向（外层已到边界、手指反向拖）再交还内层。两端同口径：
  - 小程序：逻辑层 `bindscroll` → `_refreshListEdges`（仅贴边状态翻转时 setData）→ `scrollConfig.listAtTop/listAtBottom` → WXS `touchmove` 据此决定让原生 / 外层接管。`.scroller` 只驱动 transform，列表为原生滚动；`_measure` 额外 `scrollOffset()` 同步边界，避免内容增减后状态失效。
  - APP：`FlingEngine.onTouchMove` 读内层元素**实时** `scrollTop` 判定；`.scroller.js-scroll` 的 `touch-action` 必须是 **`pan-y`** 而非 `none` —— `touch-action` 取"元素 + 祖先交集"，祖先写 `none` 会让内层真滚动容器彻底滚不动（手势既滚不了列表也滚不了页面），引擎靠每帧 `preventDefault` 在自己接管的范围内取消原生滚动。
  - 验收：`node tests/verify_nested_scroll_handoff.js`（加载真实 `scroll.wxs` 与从 `app.js` 抽取的真实 `FlingEngine` 类，逐帧喂手势断言双向接力）。
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

## Android App (`android_app/`)

Capacitor WebView 封装，无需微信登录（设备账号 `dev_` 前缀 + 可绑定微信）。**页面逻辑与小程序同源，`www/` 即前端源码**（浏览器直接打开 `www/index.html` 即可开发预览）；`android/` 是 Capacitor 生成的原生壳，`npx cap sync android` 把 `www/` 复制进 `android/app/src/main/assets/public/` 后由 Gradle 打包。

### APP 打包流程（必须先走脚本，勿手动 gradlew）

```powershell
# Release（签名 + 混淆）：
cd mobile_apps/android_app
powershell -ExecutionPolicy Bypass -File .\build-release-apk.ps1
# 产物：dist\h_n-printer_android_v{versionName}.apk（脚本自动从 build.gradle 读版本号命名）

# Debug：
powershell -ExecutionPolicy Bypass -File .\build-apk.ps1
# 产物：dist\h_n-printer_android_v{versionName}_debug.apk
```

APK 命名约定（2026-09 起）：**`h_n-printer_android_v{版本}.apk`**，与桌面端 `h_n-printer_setup_{版本}.exe`
同一风格（英文 + 下划线，避免中文名在 scp / COS / 浏览器下载时被转码）。
**上传服务器 `/updates/` 时保持同名**，`app_update.json` 的 `url` 也用这个名字。
注意 `www/updater.js` 里 `const fileName = 'hn-cloud-print_v' + version + '.apk'` 只是**APP 本地下载到
缓存时用的临时名**（与 URL 无关、用户看不到），无需随发版改名。
上传前用 `python tests/verify_apk_release.py` 校验（APK 内 web 资源与 `www/` 逐字节一致 + 打印待填清单值）。

关键要点（脚本已内置，直接跑即可）：
- **必须先 `cap sync android`**：脚本内部执行（`build-release-apk.ps1`/`build-apk.ps1` 开头），把最新 `www/` 同步进 `android/app/src/main/assets/public/`。**只跑 `gradlew assembleRelease` 而跳过 sync，APK 会打包上次 sync 的旧 web 资源**（改过 `www/*.js` 后忘 sync 是经典翻车点）。
- JDK 17 + Android SDK 固定路径：`C:\Users\Administrator\android-tools\jdk-17.0.20+8`（JAVA_HOME）与 `...\sdk`（ANDROID_HOME）；系统默认 java 是 SPSS 的 JRE 1.8，**不可用**。
- Release 签名：`android/app/keystore.properties` + `release.keystore`（不入 git，缺失时 release 构建直接报错）。
- **版本号**：`android/app/build.gradle` 的 `versionName`（用户可见，如 `1.1.7`）与 `versionCode`（内部编号）。**升级必须两者同步递增**（versionCode 不增则已装用户无法覆盖安装）。
- 基础 API 地址在 `www/app.js` 的 `DEFAULT_BASE_URL`（默认 `https://hn-space.cn`），可被 localStorage `hn_base_url` 覆盖。

## 部署

参考 `printer-backend/DEPLOY.md`:
```bash
# 后端 (Ubuntu 22.04)
cd /opt/printer-backend
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp config.py.example config.py  # 填写微信/服务器配置
systemctl start printer-backend  # gunicorn + gthread（workers=1 + threads=16）

# Nginx 反向代理 (含 WebSocket 升级)
cp nginx-http.conf /etc/nginx/sites-available/printer-backend
ln -s ... && nginx -t && systemctl reload nginx

# 备份
bash backup.sh  # crontab 每天凌晨3点
```

## 价格模型

`calculate_price(page_count, duplex)`:
- 单面: 0.3元/页
- 双面: 0.4元/张 (每张纸印两页，奇数页最后一张按 0.3元 单面计费)
- 价格以 `pricing.json` 为权威（首页费默认 0.10），小程序/APP 经 `/api/pricing` 同步单价（不再硬编码单价）；提交时服务端按 pricing.json 覆盖客户端金额字段（P1-4.7）
- 管理员提交的订单 `is_free=1`，不计费

### 复制价格的前置校验（页数未计算完成 → 价格无效，禁止复制）

后端只对 PDF 能自己数页（pypdf）；**Word(doc/docx) / md / txt / csv** 必须由本地打印工具转换后才有真实页数，上传/提交时后端只能按 1 页兜底（`submit_order` 里的 `or 1`、`get_file_page_count` 的默认 1 页）。因此接管设备离线（等不到页数回报）或设备在线但转换/页数尚未返回就提交时，这类文件的页数是假值、算出来的价比实收少。

- 判据：`page_count > 0` 且（PDF/图片 → 可信；doc/docx/md/txt/csv → 必须 `page_count_verified=1`，即本地工具转换回报过）。
- 前端在小程序 `pages/index/index.js`（`_checkPriceFiles` / `_isFilePriceReliable`）与 APP `www/print.js`（`checkPriceFiles` / `isFilePriceReliable`）两处同口径校验「复制价格 / 复制详细价格」：只要有一个文件不可信 → **不写剪贴板**，弹「需打印的文件中包含X类型，且页数未完成计算，价格计算无效」；接管设备离线时文案额外点明离线。成功弹窗内也同步亮出该提示。
- 页数在提交后才回报 → 轮询成功处调 `_applyPageCountToOrder` / `applyPageCountToOrder` 写回提交快照（`_lastOrderResult.files`），拦下自动解除、提示消失（后端 `page_count_result` → `_recalc_prices_for_file` 已回溯重算订单价，此时复制到的就是实收价）。
- 验收：`node tests/verify_copy_price_page_guard.js`（真实源码 + 桩环境：离线 Word、md、在线未返回、已验证放行、PDF/图片、混合订单、页数回报后解除）。

## 关键文件索引

| 文件 | 作用 |
|---|---|
| `printer-backend/app.py` | 全部后端逻辑（路由/SocketIO/数据库/定时任务） |
| `printer-backend/config.py` | 后端配置（需手动创建，不提交 git） |
| `printer-backend/DEPLOY.md` | 部署指南 |
| `printer-backend/gunicorn_config.py` | Gunicorn 配置（gthread worker，与 app.py 的 async_mode="threading" 匹配） |
| `printer-backend/nginx-http.conf` | Nginx 反向代理配置模板 |
| `printer-backend/backup.sh` | 数据库备份脚本 |
| `h_n_print/app.json` | 小程序页面/窗口/tabBar 注册 |
| `h_n_print/utils/config.js` | 小程序 API 地址配置 |
| `h_n_print/pages/index/index.js` | 首页：文件选择/上传/提交/滚动引擎 |
| `h_n_print/pages/me/me.js` | 个人中心：订单/许可密钥/管理员/滚动引擎 |
