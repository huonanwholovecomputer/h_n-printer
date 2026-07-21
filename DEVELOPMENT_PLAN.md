# HN 打印系统 — 综合开发计划

> 生成日期：2026-07-21
> 范围：本地打印工具 (`local_print_tool/`)、后端 (`printer-backend/`)、前端 (微信小程序 `h_n_print/`)

---

## 目录

- [一、Bug 修复](#一bug-修复)
  - [B1. MD5 缓存命中与文件名显示](#b1-md5-缓存命中与文件名显示)
  - [B2. 标签页管理双击崩溃](#b2-标签页管理双击崩溃)
  - [B3. 超级管理员任务列表权限过滤](#b3-超级管理员任务列表权限过滤)
- [二、功能新增](#二功能新增)
  - [F1. 标签页管理界面新增"订单号"列](#f1-标签页管理界面新增订单号列)
  - [F2. 订单号体系建立](#f2-订单号体系建立)
  - [F3. 空标签页自动删除（"−"按钮增强）](#f3-空标签页自动删除按钮增强)
  - [F4. "删除空标签页"按钮](#f4-删除空标签页按钮)
  - [F5. 标签页管理器中新增"删除空标签页"按钮](#f5-标签页管理器中新增删除空标签页按钮)
  - [F6. 前端取消任务 → 本地工具响应](#f6-前端取消任务--本地工具响应)
  - [F7. 云端任务列表窗口（替代弹窗）](#f7-云端任务列表窗口替代弹窗)
  - [F8. 日志记录与回传功能](#f8-日志记录与回传功能)
  - [F9. 主界面显示当前订单号](#f9-主界面显示当前订单号)
  - [F10. 前端"历史授权用户"模块](#f10-前端历史授权用户模块)
  - [F11. 管理员订单查看](#f11-管理员订单查看)
  - [F12. 前端"本地打印任务"列表](#f12-前端本地打印任务列表)
  - [F13. 任务列表缓冲加载](#f13-任务列表缓冲加载)
  - [F14. 列表分页与单页容量选择](#f14-列表分页与单页容量选择)
- [三、功能优化](#三功能优化)
  - [O1. 日志与提示信息优化](#o1-日志与提示信息优化)
  - [O2. "忽略订单"→"打回订单"](#o2-忽略订单打回订单)

---

## 一、Bug 修复

### B1. MD5 缓存命中与文件名显示

**现状分析**：

当前流程存在冗余下载和显示名不准确的问题：

1. **页数分析阶段**（`cloud_client.py:606` `_analyze_and_report_page_count`）：
   - 下载 Word 文件 → 计算源文件 MD5 → 转 PDF → 将 **PDF 存入 `pdf_cache/{source_md5}.pdf`**，缓存 key = Word 文件的 MD5
   - 回报页数后**删除临时文件**

2. **打印任务推送阶段**（`cloud_client.py:318` `_on_print_task`）：
   - 收到 `print_task` 事件 → 自动调用 `accept_task()` → `_download_file()` **重新下载同一个 Word 文件**
   - 下载时计算 MD5（同文件，MD5 相同）→ 存入 `task.source_md5`
   - 由于是 `.docx`，第 482 行 `if ext == ".pdf"` 为 False，**不存入 pdf_cache**

3. **添加到标签页**（`gui.py:3097` `_add_cloud_task_to_new_tab`）：
   - 用 `task.source_md5` 查 `pdf_cache/` → **此时能命中**（因为第 1 步已缓存）
   - 但 `job.file_path` 仍设为下载的 `.docx` 临时文件，`job.cached_pdf` 设为缓存 PDF
   - 显示文件名使用 `task.file_name`（来自后端），正确

**根本问题**：
- 后端 `push_print_task_to_client` 发送的数据中**没有 `source_md5` 字段**（`app.py:1040-1052`）
- 本地 `_download_file` **无条件重新下载**，即使 PDF 缓存已存在
- 下载的 Word 文件在打印时用不到（因为 `cached_pdf` 已命中），白白浪费带宽和磁盘

**修复方案**：

| 步骤 | 文件 | 修改内容 |
|------|------|----------|
| 1 | `printer-backend/app.py` | `push_print_task_to_client()` 和 `fetch_and_lock_task()` 返回数据中**新增 `source_md5` 字段**：从 `files` 表关联查询文件的 MD5（通过 `md5_index.json` 反查或直接在 `files` 表加 `md5` 列） |
| 2 | `local_print_tool/cloud_client.py` | `CloudTask.__init__` 解析 `source_md5`（已预留 `self.source_md5` 字段，只需从 `data` 读取） |
| 3 | `local_print_tool/cloud_client.py` | `_download_file()` 开头检查：如果 `task.source_md5` 已在 `pdf_cache/` 中 → 跳过下载，直接标记 `task.status = 'ready'`，并设 `task.local_path` 为缓存的 PDF 路径 |
| 4 | `local_print_tool/gui.py` | `_add_cloud_task_to_new_tab()` 中：当缓存命中时，`job.file_path` 使用 `cached_pdf` 而非下载的临时文件；`display_name` 使用 `task.file_name`（后端原始文件名，已有） |
| 5 | `printer-backend/app.py` | `files` 表新增 `md5 TEXT DEFAULT ''` 列（迁移），上传时填充，避免每次都从 md5_index.json 反查 |

**预期效果**：
- 同 MD5 文件第二次推送时**不再重新下载**，直接命中本地 PDF 缓存
- 标签页中显示的文件名始终是前端上传时的原始文件名

---

### B2. 标签页管理双击崩溃

**现状**：`gui.py:1766` `_on_double_click(idx)` — `QTableWidget.cellDoubleClicked` 信号传递的是 `(int row, int column)` 两个独立参数，而非 `QModelIndex` 对象。代码中 `idx.row()` 将 int 当作对象调用，触发 `AttributeError`。

**修复**：

```python
# 修改前（gui.py:1766）
def _on_double_click(idx):
    row = idx.row()
    ...

# 修改后
def _on_double_click(row, col):
    if 0 <= row < len(tab_keys):
        key = tab_keys[row]
        ...
        dlg.accept()  # 切换后关闭管理器
```

双击行为：切换到目标标签页 → 关闭标签页管理器。

**影响文件**：`local_print_tool/gui.py` 第 1766-1778 行

---

### B3. 超级管理员任务列表权限过滤

**现状**：`app.py:2048-2055`，超级管理员默认 `WHERE 1=1`，看到**全部订单**。用户预期：所有角色默认只看**自己的**订单，超级管理员/管理员可通过 `?openid=xxx` 参数查看指定用户订单。

**修复**：

```python
# app.py get_orders() 修改默认逻辑
if is_super:
    if target_openid:
        where_clause = "openid = ?"
        params = [target_openid]
    else:
        # 超级管理员默认也只看自己的
        where_clause = "openid = ?"
        params = [g.openid]
elif role == "admin":
    if target_openid:
        # 验证授权关系...
        where_clause = "openid = ?"
        params = [target_openid]
    else:
        # 管理员默认看自己的
        where_clause = "openid = ?"
        params = [g.openid]
else:
    # 普通用户/游客：只看自己的
    where_clause = "openid = ?"
    params = [g.openid]
```

新增：`GET /api/orders?view=all` 参数，仅超级管理员可用，返回全部订单（用于管理面板）。

**影响文件**：`printer-backend/app.py` `get_orders()` 函数

---

## 二、功能新增

### F1. 标签页管理界面新增"订单号"列

**需求**：标签页管理窗口表格新增一列显示订单号。云端标签显示后端订单号，本地标签留空或显示本地分配的订单号。

**实现**：

1. `gui.py:_show_tab_manager()` 表格从 5 列扩展为 6 列，新增"订单号"列
2. `PrintJob` 数据类新增 `order_number: str = ""` 字段
3. `_add_cloud_task_to_new_tab()` 中传入 `task.order_number`
4. 本地打印任务在生成订单号后回填到 `PrintJob.order_number`
5. 标签页管理窗口的"来源"列逻辑调整为：遍历标签内所有 jobs，取第一个非空 `order_number` 显示

**影响文件**：`local_print_tool/gui.py`、`local_print_tool/printer_config.py`

---

### F2. 订单号体系建立

#### F2.1 前端提交时分配订单号

**后端**（`app.py:submit_order`）：
- 生成订单号 `HN{yyyymmdd}-{NNNN}`（已有逻辑在 `printer_config.py:generate_order_number`）
- 后端需要在 `orders` 表写入 `order_number`（已有字段）
- 返回的 JSON 中包含 `order_number`，前端在提交成功提示中显示
- 小程序"我"页面的任务列表中每项显示订单号

#### F2.2 本地任务订单号

- 点击"复制"或"复制计费明细"时：
  - 如果该标签页尚未分配 `order_number`，则即时生成一个
  - 生成的订单号写入 `PrintJob.order_number`，保存到 `print_config.json`
  - 复制的内容中包含订单号
- 本地任务数据通过 `POST /api/local_orders` 上报后端存储
- 后端新增 `local_orders` 表（或复用 `orders` 表加 `source = 'local'` 字段）
- 仅超级管理员和管理员可查看本地打印任务列表

**影响文件**：`local_print_tool/gui.py`、`local_print_tool/printer_config.py`、`printer-backend/app.py`

---

### F3. 空标签页自动删除（"−"按钮增强）

**需求**：点击 `−` 切换到上一标签页后，检查序号更大的标签页中是否有空标签页，有则删除。

**实现**：`gui.py:_switch_tab(-1)` 中，切换到上一页后：
```python
def _switch_tab(self, delta):
    # ... 现有切换逻辑 ...
    if delta == -1:
        self._cleanup_empty_tabs(after_key=self._current_tab)
```

`_cleanup_empty_tabs(after_key)` — 遍历所有 key > after_key 的标签页，若 `len(jobs) == 0` 则删除。

**注意**：不删除当前标签页（即使为空）。

**影响文件**：`local_print_tool/gui.py`

---

### F4. "删除空标签页"按钮

**需求**：在主界面 `+` 按钮右侧新增"删除空标签页"按钮。存在空标签页（排除当前）时可点击，否则灰色。

**实现**：

1. 在 `_setup_file_table()` 的标签页导航行（`+` 按钮右侧）新增 `QPushButton("🗑 空")`
2. 每次 `_refresh_tab_display()` 后调用 `_update_empty_tab_btn()` 更新按钮状态
3. 点击后调用 `_cleanup_empty_tabs(after_key=None)`（删除全部非当前空标签页）
4. 逻辑：
```python
def _has_empty_tabs_except_current(self):
    for key, jobs in self._config.tabs.items():
        if key != self._current_tab and len(jobs) == 0:
            return True
    return False
```

**影响文件**：`local_print_tool/gui.py`

---

### F5. 标签页管理器中新增"删除空标签页"按钮

在 `_show_tab_manager()` 的按钮行中新增该按钮，逻辑与主界面一致。删除后调用 `_rebuild_dialog_table()` 刷新。

**影响文件**：`local_print_tool/gui.py`

---

### F6. 前端取消任务 → 本地工具响应

**现状**：前端取消任务后，后端更新数据库状态，但本地工具无感知。

#### F6.1 后端推送取消事件

在 `app.py:cancel_order()` 中，取消成功后：
```python
socketio.emit("order_canceled", {
    "order_id": order_id,
    "task_ids": [子任务ID列表],
}, to=<已连接的打印机sid>)
```

需在 `order_files` 表查询 `operator_client` 获取打印机 sid（如果任务已被拉取）。

#### F6.2 本地工具拦截/更新弹窗

在 `cloud_client.py` 新增 SocketIO 事件处理：

```python
@self._sio.on("order_canceled")
def _on_order_canceled(data):
    order_id = data.get("order_id")
    task_ids = data.get("task_ids", [])
    # 通过 Signal 通知 GUI
    self.order_canceled.emit(order_id, task_ids)
```

新增信号：`order_canceled = Signal(int, list)`

#### F6.3 GUI 三种响应

`gui.py` 中根据任务当前状态分三种情况：

| 情况 | 任务状态 | 响应 |
|------|----------|------|
| 弹窗前 | `_pending_tasks` 中有，但尚未 `_show_cloud_task_dialog` | 直接从 `_cloud_tasks` / `_pending_tasks` 移除，不弹窗 |
| 弹窗已弹出未确认 | 弹窗已显示 | 弹窗内容更新为"该任务已被取消"，5 秒倒计时自动关闭；或关闭旧弹窗，弹出新提示窗 |
| 已添加到标签页 | PrintJob 已在标签中 | 弹出 `QMessageBox` "标签页 {key} 的任务被取消！"，确认后删除对应标签页 |

**影响文件**：`printer-backend/app.py`、`local_print_tool/cloud_client.py`、`local_print_tool/gui.py`

---

### F7. 云端任务列表窗口（替代弹窗）

**需求**：将当前的单任务弹窗 (`_show_cloud_task_dialog`) 替换为一个**统一的云端任务列表窗口**，管理所有待确认的云端任务。

**设计**：

#### 窗口结构

```
┌─────────────────────────────────────────┐
│  ☁ 云端任务列表                    _ □ X │
├─────────────────────────────────────────┤
│  自动关闭: [5] 分钟 [0] 秒 后无待确认任务  │  ← SpinBox 调节
├─────────────────────────────────────────┤
│  ┌─────────────────────────────────┐    │
│  │ 任务列表 (QTableWidget)          │    │
│  │  # | 订单号 | 文件名 | 状态 | 操作│    │
│  │  1 | HN...  | a.docx | 待确认 │[+]│   │
│  │  2 | HN...  | b.pdf  | 已取消 │ ✗ │   │
│  └─────────────────────────────────┘    │
├─────────────────────────────────────────┤
│  [📥 全部添加到新标签页]  [↩ 全部打回]  │
│  [🗑 删除空标签页]                       │
│                              [关闭]     │
└─────────────────────────────────────────┘
```

#### 核心逻辑

- **触发**：收到云端任务 → 不弹独立对话框，加入任务列表窗口；若窗口未打开则创建并显示
- **每行操作**：`📥 添加`（添加到新标签页）、`↩ 打回`（单个打回）
- **批量操作**：全部添加（每个任务独立标签页）、全部打回
- **取消响应（F6）**：收到 `order_canceled` → 更新对应行状态为"已取消"，5 秒后自动移除该行
- **自动关闭**：
  - 待确认任务数变为 0 时启动计时器
  - 计时器时长可调（分钟 + 秒，输入框同主页价格输入框风格）
  - 计时器到期自动关闭窗口
  - 如果在计时期间收到新任务，取消计时器，窗口保持打开
  - 鼠标滚轮调节步长为 1，支持分钟+秒精度

#### 数据模型

```python
@dataclass
class PendingCloudTask:
    task: CloudTask
    status: str  # "pending" | "canceled" | "accepted" | "rejected"
    canceled_at: float | None  # 被取消的时间戳
```

使用 `QTableWidget` + 自定义 delegate 实现行内按钮。

#### 兼容 F6 取消响应

- 收到取消事件 → 查找 `PendingCloudTask` → 更新状态为 `"canceled"` → 行显示变灰 + "该任务已被取消"提示
- 如果已过期 5 秒 → 自动从列表移除
- 如果用户手动点击确认 → 立即移除

**影响文件**：`local_print_tool/gui.py`（新增 `CloudTaskListWindow` 类，重构 `_show_cloud_task_dialog` / `_on_cloud_task_received` / `_on_cloud_task_updated`）、`local_print_tool/cloud_client.py`（新增 `order_canceled` 信号）

---

### F8. 日志记录与回传功能

**设计**：

#### 后端日志存储

- `printer-backend/logs/` 目录
- 两个日志文件：
  - `server.log` — 后端自身日志（Flask 请求、数据库操作、定时任务、异常）
  - `frontend.log` — 前端上报的错误日志
- 新增 API：`POST /api/log/report` — 前端错误上报
- 新增 API：`GET /api/log/fetch?type=server|frontend` — 本地工具拉取日志（需 token 认证）

#### 本地工具日志

- `local_print_tool/logs/` 目录
- 文件：`local_tool.log` — 本地工具所有日志
- 使用 Python `logging` 模块，同时输出到文件和控制台
- 日志级别：DEBUG（文件）/ INFO（界面文本框）

#### GUI 界面

在 `Alt` → "帮助"菜单中新增"日志"项，点击弹出日志窗口：

```
┌──────────────────────────────────────┐
│  📋 日志管理                         │
├──────────────────────────────────────┤
│  [📥 拉取前后端日志]                 │
│  状态: 已拉取 12,345 字节            │
│  ────────────────────────────────    │
│  [📂 打开本地日志目录]               │
│  [🗑 清空本地日志]                   │
│                              [关闭]  │
└──────────────────────────────────────┘
```

- 点击"拉取前后端日志" → `GET /api/log/fetch?type=server` + `GET /api/log/fetch?type=frontend`
- 返回 0 字节 → "前后端没有产生警告和错误日志"
- 返回 >0 字节 → "返回了 {n} 字节的内容"，保存到 `local_print_tool/logs/remote_server.log` 和 `remote_frontend.log`

**影响文件**：`printer-backend/app.py`、`local_print_tool/gui.py`、`local_print_tool/cloud_client.py`

---

### F9. 主界面显示当前订单号

**需求**：在主界面"复制"按钮行最左侧空白区域显示当前标签页的订单号。

**实现**：

在 `total_row` 布局最左侧（`addStretch()` 之前）插入 `QLabel`：
```python
self._order_number_label = QLabel("")
self._order_number_label.setObjectName("orderNumberLabel")
total_row.insertWidget(0, self._order_number_label)
```

标签页切换和订单号变更时更新显示：
- 当前标签页有订单号 → 显示 `📋 HN20260721-0001`
- 无订单号 → 显示 `📋 未分配订单号`（灰色）

**影响文件**：`local_print_tool/gui.py`

---

### F10. 前端"历史授权用户"模块

**需求**：超级管理员和管理员的"我"页面新增"历史授权用户"入口。

**实现**：

1. **后端新增 API** `GET /api/admin/authorized_users`：
   - 查询 `license_keys` 表中 `created_by = g.openid` 的所有记录
   - JOIN `users` 表获取用户昵称/头像
   - 返回：用户 openid、昵称、头像、授权时间、类型（temp/admin）

2. **前端 `pages/me/me`**：
   - 新增"历史授权用户"列表项，仅对 admin/super_admin 显示
   - 点击进入新页面 `pages/authorized-users/authorized-users`
   - 列表展示所有授权过的用户
   - 点击任意用户 → 进入该用户的订单列表（复用 `get_orders?openid=xxx`）

**影响文件**：`printer-backend/app.py`、`h_n_print/pages/me/me.*`、`h_n_print/pages/authorized-users/`（新建）

---

### F11. 管理员订单查看

**需求**：超级管理员在"管理管理员"列表中点击任意管理员，查看该管理员发起的全部打印任务。

**实现**：

1. **后端**：已有 `GET /api/admin/users` 返回用户列表（`app.py:2855`），需确认返回了管理员列表
2. **前端**：在"管理管理员"列表项中添加点击事件 → 调用 `GET /api/orders?openid={admin_openid}` → 展示该管理员的全部订单
3. 可复用 F10 的订单列表展示页面

**影响文件**：`printer-backend/app.py`、`h_n_print/pages/me/me.*`

---

### F12. 前端"本地打印任务"列表

**需求**：前端新增"本地打印任务"入口，显示通过 F2.2 上报的本地打印任务。

**实现**：

1. **后端**：
   - 新增 `local_orders` 表或在 `orders` 表加 `source TEXT DEFAULT 'cloud'` 字段
   - `GET /api/orders?source=local` 返回本地任务
   - 权限：仅 admin/super_admin 可查看

2. **前端**：
   - "我"页面新增"本地打印任务"列表项（仅对 admin/super_admin 显示）
   - 点击进入本地任务列表页（复用订单列表组件，加筛选参数）

**影响文件**：`printer-backend/app.py`、`h_n_print/pages/me/me.*`

---

### F13. 任务列表缓冲加载

**需求**：任务列表不再一次性全部加载，滑动到底部后继续加载下一页。

**实现**：

- 后端 `GET /api/orders` 已有分页支持（`page`、`per_page` 参数）
- 返回数据新增 `has_more: bool` 标识是否还有更多数据
- 前端（`me.js`）：
  - 初始加载第 1 页，`per_page=20`
  - 监听滚动到底部事件 → `page += 1` → 追加数据
  - 全部加载完毕后显示"—— 没有更多内容啦 ——"

**注意**：当前前端使用自定义滚动引擎（`_initScrollEngine`），需在橡皮筋物理模型中增加触底检测回调。

**影响文件**：`printer-backend/app.py`、`h_n_print/pages/me/me.js`

---

### F14. 列表分页与单页容量选择

**需求**：任务列表分页显示，顶部有页码导航和单页容量选择。

**UI 设计**：

```
┌────────────────────────────────────┐
│  任务列表    [20▼]    < 1 / 5 >    │
├────────────────────────────────────┤
│  订单 #1 ...                       │
│  订单 #2 ...                       │
│  ...                               │
├────────────────────────────────────┤
│  —— 第 1 页，共 5 页 ——           │
└────────────────────────────────────┘
```

**实现**：

- 顶部右侧：`< 1 >` 页码切换（`<` 上一页，`>` 下一页，数字显示当前页）
- 页码左侧：容量选择下拉按钮 `[20▼]`，选项：10 / 20 / 50 / 100
- 底部显示"第 X 页，共 Y 页"
- 切换页码或容量时重新请求数据

**影响文件**：`h_n_print/pages/me/me.wxml`、`h_n_print/pages/me/me.js`、`h_n_print/pages/me/me.wxss`

---

## 三、功能优化

### O1. 日志与提示信息优化

**目标**：所有错误、警告、进度信息需精准、无歧义、一目了然。

#### 本地打印工具日志窗口优化

| 现状问题 | 优化方案 |
|----------|----------|
| 所有消息同一格式 `[HH:MM:SS] msg` | 分级显示：`[HH:MM:SS] ✓ 成功`（绿）/ `⚠ 警告`（黄）/ `✗ 错误`（红）/ `ℹ 信息`（白） |
| 日志无分类 | 添加标签前缀：`[打印]` `[转换]` `[云端]` `[系统]` |
| 错误信息不明确 | 统一错误信息格式：`操作描述 + 失败原因 + 建议操作` |

#### 示例

```
[14:32:01] [云端] ✓ 已连接到服务器
[14:32:15] [云端] ℹ 收到云任务 #42: 报告.docx
[14:32:18] [转换] ⚠ Word COM 不可用，降级为 LibreOffice 转换
[14:32:25] [转换] ✓ 转换完成: 报告.docx → 12 页
[14:32:30] [打印] ✗ 打印失败: HP LaserJet — 打印机缺纸，请检查纸盒
```

**影响文件**：`local_print_tool/gui.py` `_log()` 方法及相关调用处

#### 后端日志优化

- 后端已有 `print()` 日志，统一改为 `logging` 模块
- 日志写入文件（F8），带时间戳和级别
- 关键操作（推送任务、取消、异常）记录结构化日志

---

### O2. "忽略订单"→"打回订单"

**需求**：将"忽略订单"改为"打回订单"，打回后订单状态在前端可见。

#### 后端新增状态

- `order_files` / `orders` 状态枚举新增 `"rejected"`（被打回）
- 优先级：`failed > printing > queued > sent > rejected > canceled`
- 与 `canceled` 的区别：`rejected` 是**打印机主动打回**，`canceled` 是**用户主动取消**

#### 后端新增 API

`POST /api/reject_order`：
- 将子任务和父订单状态设为 `rejected`
- 同 `cancel_order` 的权限模型（仅订单所有者可操作）

#### 本地工具修改

- 弹窗（F7 的云端任务列表窗口）中按钮文本：
  - 单个：`✕ 忽略` → `↩ 打回`
  - 批量：`全部忽略` → `全部打回`
- 点击打回 → 确认对话框 "确定要打回此订单吗？打回后订单将返回给用户" → 调用 `POST /api/reject_order`
- 关闭窗口时若有未确认订单 → "还有 {n} 个未确认的订单，全部打回吗？" → 确认后批量打回

#### 前端显示

- 任务列表中 `rejected` 状态显示为**红色"被打回"**标签
- 可附带打回原因（可选扩展）

**影响文件**：`printer-backend/app.py`、`local_print_tool/gui.py`、`local_print_tool/cloud_client.py`、`h_n_print/pages/me/me.*`

---

## 实施优先级建议

| 优先级 | 编号 | 说明 |
|--------|------|------|
| 🔴 P0 | B1, B2, B3 | Bug 修复，影响正常使用 |
| 🟠 P1 | F6, F7, O2 | 取消机制 + 任务列表窗口 + 打回重构，三者紧密关联，建议一起实施 |
| 🟡 P2 | F1, F2, F9 | 订单号体系，F7 完成后立即跟进 |
| 🟡 P2 | F3, F4, F5 | 空标签页管理，独立功能 |
| 🟢 P3 | F8, O1 | 日志系统 |
| 🟢 P3 | F10, F11, F12 | 前端管理功能 |
| 🔵 P4 | F13, F14 | 列表加载优化 |

---

## 文件变更清单

| 文件 | 涉及编号 |
|------|----------|
| `local_print_tool/gui.py` | B1, B2, F1, F2, F3, F4, F5, F6, F7, F9, O1, O2 |
| `local_print_tool/cloud_client.py` | B1, F6, F7, F8 |
| `local_print_tool/printer_config.py` | B1, F1, F2 |
| `printer-backend/app.py` | B1, B3, F2, F6, F8, F10, F11, F12, F13, O1, O2 |
| `h_n_print/pages/me/me.js` | F2, F10, F12, F13, F14 |
| `h_n_print/pages/me/me.wxml` | F2, F10, F12, F14 |
| `h_n_print/pages/me/me.wxss` | F2, F10, F12, F14 |
| `h_n_print/pages/authorized-users/` | F10（新建） |
