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
- **订单来源（`orders.source`/`openid`）不得被本地工具改写（2026-10-18 现场 🔴）**: 现场截图：小程序订单 HN20261018-0023 打印后，结算页「订单明细」来源徽标变成 **本地**。根因是**双端都缺闸门**的一条链路 —— 云端任务的 `PrintJob` 带着云端订单号（`local_print_tool/cloud_client.py`：`order_number=task.order_number`）⇒ 本地工具 `_acquire_order_number_and_upload` "复用已有订单号" ⇒ `POST /api/local_orders`（status=sent）⇒ 本接口对**已存在**的订单行无条件 `UPDATE orders SET ... source='local'`，按 `order_number` 正好命中那行云端单 ⇒ `source` 被改成 `local`、`owner_name` 被本地归属覆盖（`openid` 仍是微信 ⇒ 小程序里订单还在，只是来源标错，且「订单构成」「按来源筛选/对账」全错位）。**两侧闸门都已落地**：① 服务端 `local_orders()` 只允许改写 `openid='local'` 或 `source∈{'','local'}` 的行，否则原样返回 `{"success":true,"ignored":true}` + WARNING（**热部署即对全部存量客户端生效**）；② 客户端（4.5.25+）`is_cloud_order`（任一 job `task_id>0`）时**根本不发起**该请求（`_acquire_order_number_and_upload_net` / `_upload_reserved_order` 两处）。零回归依据：`/api/next_order_number` 预留单写 `openid='local', source='local'`；离线补报（`OfflineSync.upload_order`）只读本地离线库；本地换号上报（`_sync_local_orders_to_cloud`）只挑 `-L`/`LOCAL-` 号 ⇒ 没有合法流程需要改写云端单。验收 `tests/verify_order_source_local_guard.py`（真实 Flask+SQLite 6 组：微信/APP 单拒改 + 归属人不被覆盖 + 不产生重复行 + 本地预留单照旧可写 + 空 openid 老数据不误伤 + 终态幂等；**删闸门即 6 条转红**）与 `tests/verify_copy_reserved_sync.py` 的 4b（客户端双向：云端单 0 次上报、本地单照旧上报）。存量数据甄别（生产库只读）：`SELECT COUNT(*) FROM orders o WHERE o.source='local' AND o.openid<>'local' AND EXISTS (SELECT 1 FROM order_files f WHERE f.order_id=o.id);` 命中即被误改写的云端单（本地直接打印的单**不建** `order_files` 行），恢复时按 `openid` 前缀把 `source` 写回 `wechat`/`app`
- **父子订单模型**: `orders` 是聚合容器，`order_files` 是实际打印子任务。每个子任务有独立的 `copies`, `page_range`, `duplex`, `image_orientation`, `status`。父订单状态通过 `aggregate_order_status()` 从子任务聚合（优先级: failed > printing > queued > sent > canceled）。
- **任务分发**: 双通道 — ① SocketIO `print_task` 事件实时推送（`push_print_task_to_client`）② HTTP `GET /api/pull_queued_orders` 供客户端主动拉取（`fetch_and_lock_task` 原子取锁防重复）。两种方式都先将子任务标记为 `printing` 再推送/返回。**两通道独立保活（2026-09）**：设备「在线」= `printer_clients[client_id]["heartbeat"]` 在 90s 内（`CLIENT_HEARTBEAT_TIMEOUT`），`/api/pull_queued_orders` 入口也调 `touch_printer_heartbeat(client_id)` —— 否则 socket 掉线但网络正常时（本机代理掐断长连接、握手超时）设备被判离线，HTTP 拉单永远返回空，「双通道」实为单通道。HTTP 建的条目 `sid=None`：`push_print_task_to_client` 取不到 sid 会把子任务回滚 `queued`（`[WAIT]` 日志）等下次 HTTP 拉取，其余 emit 路径都有 `if sid` 守卫，不会误广播。**接单唯一接管（2026-11）**：多设备共连时两条通道都只发给「启用接单」的设备——`printer_claim.json` 持久化唯一接管者，`get_active_printer_client()` 判定在线接管者，页数分析/预约下发同样只走接单设备。
- **设备注册表与授权**: `devices.json` 记录每台设备的 `device_name`/`owner_name`(授权页绑定成员)/首末次在线时间；`GET /api/printer/devices` 展示全部设备（计算机名/在线/所有者/是否接单），`POST /api/printer/claim|release|bind_owner` 管理接管与所有者绑定，`POST /api/printer/devices/delete` 删除设备（清理升级产生的历史遗留 ID，删除接单设备时自动清空接管）。**测试设备/陌生设备清理（2026-10）**：联调脚本每次连接都会注册设备（线上曾攒到 48 台 `debug-*`/`socketio-debug-*`），故设备列表**默认隐藏**命中 `TEST_DEVICE_PREFIXES` 的 id（只回 `hidden_test_devices` 计数），`purge_test_devices` 每 30 分钟清掉其中**离线**的；`POST /api/printer/devices/cleanup`（默认 `dry_run`，跳过在线 / 已绑定所有者 / 人工盖过「自动双面」章的设备）配合授权页「🧹 清理测试设备」「🧹 清理无主离线设备」两个按钮。标准动作（备份→预演→执行→复核）与踩坑经验见 `printer-backend/DEPLOY.md` 第 11 节。**自动双面能力：只记录 + 提示，不再拦单（2026-10；拦单已按要求全面放开）**：`auto_duplex` 三态（true/false/null）+ 来源（manual 优先于 probe）随 `/api/claiming_devices`、`/api/printer/devices`、`printer_state` 下发 —— 移动端设备列表标「🖐 需人工翻纸」（仅提示）、本地工具据此调整双面 UI/提示。**曾经的拦单已全部移除**：服务端 `check_auto_print_duplex_conflict`（`submit_order` 的 409 + `code=auto_print_duplex_conflict`）与小程序/APP 的「双面 × 无人值守」互斥（`_targetNeedsManualFlip`/`targetNeedsManualFlip`、`_hasDuplexFile`/`hasDuplexFile`、切换设备自动关无障碍、提交兜底）都不再存在 —— 无障碍/预约 + 双面提交一律放行，无人值守翻纸由设备端自己负责（官方本地工具仍会挂起等人翻纸）。验收：`python tests/verify_duplex_capability.py`（⑥ 节专门钉住「拦截确已移除」）。**攒批模式远程开关（2026-10）**：`/api/claiming_devices` 每台设备额外下发 `stack_mode`（true/false/null=从未上报）、`stack_busy`、`stack_can_toggle`、`stack_reason`；移动端设备列表显示「🖐 攒批中 / 攒批未开」并给 **admin/super_admin** 一个开关，`POST /api/printer/stack_mode {client_id, enable}`（**一律 HTTP 200**，看 body 的 success/message）→ 服务端按 `_stack_toggle_problem` 校验 → SocketIO `set_stack_mode` 下发到设备端 → 设备端**再自查一次**忙闲后执行并回报 `stack_state`。前提（两侧同口径）：开关都必须**打印机空闲**（忙 = 正在打印 / 等人工翻纸打第二趟 / 有排队待打的请求），忙时移动端灰显弹 `stack_reason`、服务端拦下不下发、本地工具勾选框也回滚并提示。**适用性前提（2026-10 用户要求）：攒批只对「无自动双面器、双面需人工翻纸」的机型有意义** —— 移动端**只对 `auto_duplex === false` 的设备渲染整个攒批区块**（徽标 + 开关；支持自动双面 / 能力未确认的连区块都不渲染，点击入口另有兜底），服务端 `_stack_needs_manual_flip`（`get_device_capability(...)["auto_duplex"] is False`）在 `_stack_toggle_problem` 里再拦一次（适用性不满足时**不下发指令**），本地工具 `gui._stack_applicable` 干脆不显示复选框且 `_stack_mode_on()` 对它恒为 False（配置里的偏好留着，换回需翻纸的机型自动恢复）。推送 payload 附带 `bound_owner_name`（从 `finance_config.memberBindings` 反查下单 openid 的绑定成员），本地工具据此设置订单标签页归属。
- **在线设备日志收集**: `POST /api/log/collect_all`（printer token）向所有在线客户端推送 SocketIO `request_log`，客户端回报本机日志（`logs` 事件，request_id 防串台，每台上限 200KB，超时标记 error）；配合本地工具「日志管理」一键收集。
- **订单状态人工订正（2026-10-18）**: `POST /api/admin/order_status`（printer token）供收支清算页「云端数据 → 订单明细」的状态列订正入口调用 —— 把父订单与其**全部**子任务一起改写成**终态白名单** `MANUAL_FIX_STATUSES = sent/failed/abandoned/canceled/rejected`（`db_lock` + `BEGIN IMMEDIATE`；`locked_at` 一并清空）。**只允许终态**：改成 queued/printing/waiting 这类工作状态会被推送与预约调度重新捡起来打印，故一律 400（前端只列同一份清单，`ORDER_FIXABLE_STATUSES` ⟷ `MANUAL_FIX_STATUSES`）。三个已修的坑（别再踩）：① 请求体先做 `isinstance(dict)` 守卫再取 token，畸形 JSON（数组/字符串/数字）不能把鉴权表达式打成 **未认证 500**；② `order_id` 只认**规范十进制**（纯数字、无前后空白、无前导零）——SQLite 的 INTEGER 亲和性会把 `'1.0'/'01'/'+1'/' 1 '/'1e0'` 都折算成 `id=1` 并**真的改写订单 1**；③ 父/子必须同一事务改写（只改 `orders.status` 会被 `aggregate_order_status()` 刷回去）。验收 `tests/verify_order_status_edit.py`（真实 Flask+SQLite）、`tests/verify_order_status_edit_render.mjs`（Playwright 真渲染）、对抗式 `tests/verify_order_status_edit_adversarial.{py,mjs}`。**修复批 v7.0b（2026-10-18，审查 `docs/审查报告_2026-10/findings_v7.0_orderstatus.md` 🔴1/🟡8，同一批的客户端/结算页侧见根 `CLAUDE.md`）**——四条新契约（别再踩）：④ **设备是这张单的最终裁决者**（2026-10-18 用户口径「决定权在打印这个订单的机器手里」）：`NO_LONGER_PRINTABLE_STATUSES = ("canceled","abandoned","rejected")` 只表达「云端不要再主动让它打印」⇒ 后端订正成功后**必须通知设备端** `_notify_order_no_longer_printable()`（复用 `order_canceled` 事件，payload 多带 `reason:"manual_fix"`+`status`；目标取子任务 `operator_client`/`last_operator_client` 与 `orders.target_client` 中在线者，一个都不在线则兜底广播在线接单设备；`socketio.emit(to=sid)` + `printer_clients_lock`，通知失败只记日志）；而 `on_print_success` 的白名单**含这三态与 failed** —— 设备离线没收到通知、照旧把纸打出来并如实回报成功时，云端按回报记 `sent`（纸确实出来了、账要算；人工订正不否定设备已出纸的事实），日志写明「设备成功回报覆盖云端终态 X → sent」。反向**不**成立：`on_print_fail` 的白名单不含这三态与 sent —— 那是「取消正在打印的订单」时剩余任务回报失败的副产物，放行只会把「已取消」标签弄成「打印失败」（与钱无关），理由在该函数注释里。`start_printing` 的父订单守卫也从只认 `canceled` 扩到该常量（预约单订正后不许到点复活）。⑤ **深嵌套请求体**（depth ≥ 1000）会让 `json.loads` 抛 `RecursionError` —— 它不是 `ValueError`、`get_json(silent=True)` 挡不住 ⇒ 解析本身也要 `try/except`（无 token 403 / 带 token 400，绝不 500）。⑥ `check_printing_timeout` / `recover_orphaned_printing_tasks` 的扫描都按活跃状态筛选，人工订正已把状态写成终态并清 `locked_at` ⇒ 这两条定时任务不会把订正过的单捡起来（新增改状态逻辑时别绕过这一点）。验收：`tests/verify_order_status_edit.py` B5（真实 SocketIO 收事件与 payload 契约）/B6（迟到回报语义）/B7（深嵌套）、另加 `tests/verify_order_canceled_client.py`（客户端 handler 三参 emit + 畸变载荷 fail-closed）；反向验证基线一律 `0ea71f3`。
- **角色体系**: `compute_role(openid)` → super_admin / admin / user / guest。admin 创建限时许可密钥(temp 1-10分钟或 admin 永久)，guest 兑换后获得临时 `temp_until`。提交订单时消费临时授权(`temp_until` 清空，关联 `license_keys.order_id`)。
- **文件存储**: 按扩展名分子目录 (`pdf/`, `docx/`, `png/` 等)，MD5 索引文件 (`uploads/md5_index.json`) 去重。可配置保留时间，APScheduler 定时清理过期文件。
- **定时任务** (APScheduler): `process_pending_orders`(30s), `check_printing_timeout`(60s), `cleanup_expired_files`(10min), `recover_orphaned_printing_tasks`(2min), `cleanup_expired_license_keys`(10min)。
- **断线恢复**: 客户端 disconnect 时回滚其名下所有 `printing` 子任务→`queued`。启动/定时扫描回收超过 5 分钟的孤儿 `printing` 任务。
- **打印中保活（2026-10，`print_keepalive` 事件）**: `check_printing_timeout` 对**已 accept** 的子任务按 `locked_at` 起算 `PRINT_FEEDBACK_TIMEOUT`（3 分钟）判失败、`recover_orphaned_printing_tasks` 又把 `locked_at` 超 5 分钟的 `printing` 回退 `queued` 重推；而手动双面（无自动双面器机型，如 HP LaserJet 1020）与**攒批模式**（多任务合并翻纸）一次作业要分两趟 + 人工翻纸，从 accept 到 `print_success` 轻易超过 3 分钟 → 顾客会先看到「打印失败」。本地工具在作业进行中每 60 秒发一次 `print_keepalive`（`{task_ids:[...]}`）刷新这些子任务的 `locked_at`；作业真正结束后停止发送，超时机制照常兜底（进程崩溃/断线时不再有保活）。处理器校验 `operator_client`，**不替别的设备在打的任务续命**，且只刷新 `printing`/`accepted` 两个状态的行。
- **认证**: `login_required` 装饰器验证 Bearer token (itsdangerous 签名, 7天有效)。`require_printer_access` 额外检查非 guest 角色。

关键配置从 `printer-backend/config.py` 加载 (需手动创建，含 WECHAT_APPID/SECRET_KEY/TOKEN/ADMIN_OPENIDS 等)。`/api/admin/statistics/revenue` 订单明细的 `nickname` 实时查 `users` 表（APP 设备绑定微信后订单 openid 迁移为微信 openid → 昵称即微信昵称，修改昵称自动同步）。

## 微信小程序 (`h_n_print/`)

- **页面**: `pages/index/index`(首页，文件选择+上传+提交), `pages/me/me`(个人中心，订单列表+许可密钥+管理员面板), `pages/order-detail/order-detail`, `pages/my-performance/my-performance`(月度统计), `pages/authorized-users/authorized-users`(历史授权用户列表，管理员/超管可见), `pages/user-orders/user-orders`(按 openid/来源查看订单列表，含分页与状态过滤，供管理员查看某用户/本地任务的打印记录)
- **自定义滚动引擎**: index 和 me 页面都实现了手写的橡皮筋物理滚动（`_initScrollEngine` / `_startPhysics` / `_snapBack`），通过 `translateY` 驱动，含惯性衰减、阻尼过拉、方向锁定。非原生 scroll-view。小程序侧实现在 **WXS**（`utils/scroll.wxs`，视图层 rAF+setStyle，0 setData），APP 侧是同构的 JS `FlingEngine`（`www/app.js`）。
- **嵌套滚动接力（内层文件列表 ↔ 外层页面）**: 首页文件列表是**有界 scroll-view**（内容高 > 列表高时可内部滚动），手势落到列表内时默认让位原生；滚到列表**贴顶/贴底**后，必须把滚动交给外层页面接管，否则手指一直被列表吞掉、整页滚不动；反向（外层已到边界、手指反向拖）再交还内层。两端同口径：
  - 小程序：逻辑层 `bindscroll` → `_refreshListEdges`（仅贴边状态翻转时 setData）→ `scrollConfig.listAtTop/listAtBottom` → WXS `touchmove` 据此决定让原生 / 外层接管。`.scroller` 只驱动 transform，列表为原生滚动；`_measure` 额外 `scrollOffset()` 同步边界，避免内容增减后状态失效。
    ⚠ **必须用滚动事件里的真实 `scrollHeight` 判定是否到底**：`FILE_CARD_HEIGHT_RPX` 是**估算常量**（Chromium 实测比真实渲染高 4~7%），拿它算 `maxScroll` 会恒大于真实值 → `atBottom` 永远为 false → 内层滚到底后外层永不接管（"滑动列表带不动页面"的真实根因）。`_fileListRealPx` 缓存实测值，取不到才退回常量估值；`listOverflow=false`（内容不足一屏）时两边直接置贴边。
  - APP：`FlingEngine.onTouchMove` 读内层元素**实时** `scrollTop` 判定；`.scroller.js-scroll` 的 `touch-action` 必须是 **`pan-y`** 而非 `none` —— `touch-action` 取"元素 + 祖先交集"，祖先写 `none` 会让内层真滚动容器彻底滚不动（手势既滚不了列表也滚不了页面），引擎靠每帧 `preventDefault` 在自己接管的范围内取消原生滚动。
    ⚠ **起手在内层可滚动容器时必须把 `touchmove` 切成 passive**（`FlingEngine._usePassiveNested`，touchend 恢复）：祖先挂着非 passive 的 `touchmove` 会让浏览器无法把内层滚动交给合成器线程（每帧等 JS），表现为"滑到文件列表区域特别卡"。被动回调里仍按 `scrollTop` 判边界并驱动外层，只是不 `preventDefault`（边界处内层本就滚不动、祖先也不可滚，无冲突）。`.file-list-scroll` / `.wheel-viewport` 另加 `will-change: transform` 独立合成层，滚动不重绘外层那个被 transform 驱动的大层。
  - 验收：`node tests/verify_nested_scroll_handoff.js`（加载真实 `scroll.wxs`、真实 `index.js` 与从 `app.js` 抽取的真实 `FlingEngine`：双向接力、真实 scrollHeight 判定 + 常量估值反证、passive 切换/恢复）。
- **文件卡片列表 key 与入场动画**: `wx:for` 的 `wx:key` 必须是稳定身份 **`_uid`**（不是 `index`）—— 用 index 时删中间卡片后，被删那张的节点会被就地复用给上移的卡片，节点 class 从 `card-removing`（cardRemove）变成 `card-entering`（cardExpand/cardFadeIn）→ animation-name 变化 → **入场动画重播**（"删除后下方卡片挪上来又播了一次入场"）。同时 `entering` 必须可靠清除：添加路径按 uid 清（不能按索引，删除后索引已前移）、重印恢复路径也要清（旧实现从不清 → 卡片永久挂 `card-entering`）。APP 侧对应问题是"整表 `innerHTML` 重渲染会让 `card-entering` 重播"：上传进度改为只更新 `.upload-pct`/`.progress-fill` 两个节点（`updateUploadProgressDOM`），不再每 500ms 重建卡片 DOM。
  - 验收：`node tests/verify_file_card_lifecycle.js`。
- **自定义 tabBar**: `custom-tab-bar/` 组件。
- **多文件上传**: 每个文件独立进度条（`wx.uploadFile` + `onProgressUpdate`），支持上传中移除。
- **API 地址**: `utils/config.js` 中的 `BASE_URL`，部署时修改。

#### 「我」页内容布局顺序（2026-10 重排，改之前先读）

「我」页原先按"功能上线的历史顺序"堆块：身份卡 → 生成许可密钥 → 账号绑定 → 许可区块 →
服务器存储 → 防滥用 → 历史授权 → 本地任务 → 管理管理员 → 临时授权用户 → **我的打印任务**。
两个后果：① 全页唯一的高频内容「我的打印任务」被压在最底部（超管实测 121 单，首屏全是管理面板）；
② 访客/普通用户的许可区块，小程序排在存储统计之前、APP 排在所有管理区块之后（同源逻辑两端不同款）。

现在统一为**个人区在前、管理区在后**（两端一致，APP 另有"检查更新"）：

```
个人区（全角色）：身份卡 → 许可（访客:验证许可密钥 / 普通用户:打印许可）
                 → 我的打印任务 → 账号绑定（APP 另有 检查更新）
管理区（admin/super）：生成许可密钥 → 已临时授权的普通用户 → 管理管理员（super）
                     → 历史授权用户 → 本地打印任务 → 服务器存储 → 防滥用设置
```

- APP 的 `账号绑定 / 检查更新` **必须留在 `#adminSections` 之外**（它们不是管理功能，之前夹在管理
  区块中间，导致访客先看到它们、后看到"验证许可密钥"）；APP 的管理区块仍整体包在 `#adminSections` 内。
- me 页历史上那些 `style="animation-delay: 0.xx"` 是**死代码**（me 页没有任何元素挂 animation，
  数值却编码着旧顺序），已清除，别再加回来。
- 验收：`python tests/verify_me_layout_order.py`（静态：两端顺序/层级/角色门控/关键标记各一份 +
  6 条变异自证）与 `node tests/verify_me_layout_render.mjs`（Playwright 真实渲染：桩后端 + 真实
  `www/`，量"首屏能否看到我的打印任务"与 DOM 先后，并**同时渲染 git HEAD 的旧版作对照**——旧版
  必须量不到，否则判据不成立；截图落在 `截图展示/我界面布局/`）。
- ⚠ 取舍：管理区被订单列表推后（管理员要滑过最多 10 张订单卡才到「生成许可密钥」）。若现场反馈
  发密钥路径太长，下一步选项是把订单列表折叠成"最近 N 条 + 查看全部"，或把管理区收进独立页。

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
- 前端在小程序 `pages/index/index.js`（`_checkPriceFiles` / `_isFilePriceReliable`）与 APP `www/print.js`（`checkPriceFiles` / `isFilePriceReliable`）两处同口径校验「复制价格 / 复制详细价格」：只要有一个文件不可信 → **不写剪贴板**，弹「需打印的文件中包含X类型，且页数未完成计算，价格暂无法确定。页数计算完成后价格会自动更新，请稍后在「我」→ 订单详情查看该订单的最终价格」；接管设备离线时文案额外点明离线。成功弹窗内也同步亮出该提示。
- 页数在提交后才回报 → 轮询成功处调 `_applyPageCountToOrder` / `applyPageCountToOrder` 写回提交快照（`_lastOrderResult.files`），拦下自动解除、提示消失（后端 `page_count_result` → `_recalc_prices_for_file` 已回溯重算订单价，此时复制到的就是实收价）。
- 验收：`node tests/verify_copy_price_page_guard.js`（真实源码 + 桩环境：离线 Word、md、在线未返回、已验证放行、PDF/图片、混合订单、页数回报后解除）。

### 页数分析的设备归属（谁下载 / 转 PDF / 数页）

页数分析要把**整份文件**下载到某台机器、用 Word/WPS 转 PDF 才能数页，所以"发给谁"有成本，不能随便挑第一台在线设备（线上反馈：选了离线设备 A，Word 被在线设备 B 拿去转换）。`request_page_analysis(file_id, file_name, prefer_client)` 的优先级：

1. **调用方指定**：前端当前选中的接单设备（`/api/upload` 的表单字段 `target_client_id`、`/api/file_page/<id>?target_client_id=`）；`on_connect` 补推时传的是**刚上线的设备**（谁上线谁分析自己的待分析文件）。
2. **该文件活跃订单的目标设备**（`order_files` ⋈ `orders.target_client`）—— 谁接这单谁分析。
3. **兜底**：任一在线接单设备（文件与订单无关、或指定设备离线时；早点拿到页数对用户更有价值）。无在线接单设备 → 不推送，等设备上线由 `on_connect` 补推。

相关事实（`tests/verify_analysis_routing.py` 全部钉住）：

- **打印任务严格按订单目标设备分发**：`process_pending_orders` 里目标设备未在线/未接单 → 保持 `queued` 等它上线，**绝不推给其它在线设备**（不会"被在线设备抢打"）。
- **页数是文件级的**（`files.page_count` + MD5 索引，`page_count_verified`），任何设备分析出来全站复用；离线设备上线后拿到的是服务端页码，打印时按需从云端下载 + 本地转 PDF（各机独立缓存，源文件 MD5 命中则跳过下载；分析请求的 `source_md5` 也让它能直接命中自己的缓存）。
- 分析请求是**按需下载**、临时的：本地工具下载到 `%TEMP%\hn_analyze_*` 用完即删，只留下它自己转换出的 PDF 缓存（不构成"文件被搬走/接管"）。

### 深色模式下的「轨道 / 内凹面」对比度（设计令牌陷阱）

浅色卡片上用**半透明浅灰**当"轨道/内凹面"的写法，到了深色模式会失效：深色卡片本身是 `rgba(255,255,255,0.08)`（控制区合成后 ≈ `#3D3D3E`），再叠 8% 的灰几乎不改变颜色。

- 典型翻车：分段控件轨道 `.duplex-toggle` / `.img-ori-toggle` 用 `rgba(60, 60, 67, 0.08)`，深色下合成 `0.92×61 + 0.08×60 ≈ 61` —— 与卡片完全同色（线上反馈"滑轨和背景色高度重合"，只剩蓝色滑块看得见）。
- 修法：深色下给这类"轨道/内凹面"单独的 `.theme-dark` 覆盖，换成**比卡片更深的内凹面**（`rgba(0, 0, 0, 0.24)`，与份数输入框 `#2C2C2E` 同调、视觉成组）+ 细内描边 `inset … rgba(255,255,255,0.07)` 让边界清晰；浅色模式保持原样（8% 灰在浅色卡片上本来可见）。
- 判据：轨道与所依附卡片的亮度差 |Δ| —— 修复前 ≈ 0.05（同色），修复后 ≈ 14.7（清晰可辨且不刺眼）。
- 验收：`node tests/verify_dark_rail_contrast.mjs`（静态断言两端都有深色覆盖 + **以用户截图实测真实底色为锚**做半透明合成计算 + 渲染像素方向性断言 + 前后对比截图）。
  ⚠ 无头浏览器里卡片的 backdrop 合成与真机不同（渲染出的卡片明显更亮），**不要**拿渲染出的绝对亮度当"是否可见"的判据；可见性判断锚定真机实测底色，渲染只用来看方向性。

## 文件预览（两端同口径，零后端依赖）

预览一律用**用户本机选中的文件**：小程序 `selectedFiles[i].path`（`chooseMessageFile` 临时文件）、APP `selectedFiles[i].file`（内存 File 对象），**不向服务器取**，因此没有新增后端接口（`/api/download/<file_id>` 只认打印机签名 token，用户端拿不到；重印恢复的云端订单文件 `path=''`/`file=null`，预览层提示「该文件来自云端订单，暂不支持预览」）。

| 类型 | 小程序 | APP（WebView） |
|---|---|---|
| 图片 | `wx.previewImage`（原生缩放/左右滑） | `URL.createObjectURL(f.file)` → 预览层 `<img>`（关闭时 revoke） |
| PDF / Word / Excel | `wx.openDocument`（微信内置文档预览，无需转换）；失败（个别机型不认临时路径）→ `copyFile` 到 `wx.env.USER_DATA_PATH` 再试一次 | ❌ WebView 无原生渲染能力 → 明确提示「APP 内暂不支持预览该格式（PDF/Word 请用系统应用打开）」。要支持需原生桥 Intent 或打包 pdf.js（Word 还得先转 PDF） |
| txt / csv / md | `getFileSystemManager().readFile(utf8)` → 预览层 `<text>`（`white-space: pre-wrap`） | `File.text()`/FileReader → 预览层 `<pre>`（内容 `escHtml` 转义） |

- 入口：卡片头部名称区可点 + 右侧「预览」小胶囊（`.file-preview-btn`，**在既有 header 行内、不新增行**，不破坏「每类型卡片恒定高度」前提）。
- 层叠：预览遮罩 `z-index: 10000`，**必须高于悬浮 tabBar**（`.tab-bar`/`custom-tab-bar` 都是 9999）——否则底部抽屉正文被 tabBar 盖住（渲染验收脚本已把这条钉住）。
- 大文件保护：文本类 > `PREVIEW_MAX_BYTES`(2MB) 直接提示不读；预览最多 `PREVIEW_MAX_CHARS`(20000) 字符并显示「仅预览前面部分内容」。
- APP 预览层复用 `.modal-mask`（点遮罩关闭），`closeModal('previewMask')` 里统一回收 objectURL/清空内容。
- 验收：`node tests/verify_file_preview.js`（两端真实源码 + 桩环境：分派、openDocument 失败重试、截断、大文件、云端订单文案、objectURL 回收 + 接线核对）；
  `node tests/verify_preview_render.mjs`（Playwright + 真实 `index.html`：截图到 `截图展示/文件预览/`、断言 header 行高不变、层叠顺序、图片真解码、截断提示）。
  ⚠ 该脚本**必须先把 `BASE_URL` 覆盖到本地桩**（`addInitScript` 写 `localStorage.hn_base_url` + `page.route('**/api/**')` 兜底）——app 的 `DEFAULT_BASE_URL` 是线上域名，不覆盖会打到生产接口（脚本最后一项断言全程无外部请求）。

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
