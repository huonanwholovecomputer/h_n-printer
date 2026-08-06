# HN 打印系统

一套打印管理解决方案，包含两个子系统：

| 子系统 | 目录 | 说明 |
|--------|------|------|
| **本地打印工具** | `local_print_tool/` | Windows 桌面应用（PySide6），拖放文件一键批量打印 |
| **云打印系统** | `mobile_apps/` | 微信小程序 + Android App + Flask 后端，支持远程提交和打印 |

---

## 本地打印工具

支持 PDF / Word / Markdown / HTML / TXT / CSV / 图片等多种格式，自动转换后通过 Windows GDI 静默打印。

### 快速开始

```bash
cd local_print_tool
pip install -r requirements.txt
python main.py
```

### 外部依赖（Windows，需安装到系统）

- **Microsoft Word / WPS** — Office 文档转 PDF（首选引擎，比 LibreOffice 更可靠）
- **LibreOffice** — Office 文档转 PDF 兜底引擎
- **wkhtmltopdf** — HTML/Markdown 转 PDF
- **SumatraPDF** — Windows 静默打印二级兜底

### 核心特性

- 多引擎 Word 转换：Microsoft Word COM → WPS COM → LibreOffice 智能降级
- 三级打印降级：Windows GDI → SumatraPDF → 应用层循环
- 标签页系统：每标签页独立文件列表与计费设置，打印后锁定不可编辑
- 单面/双面、页码范围、渲染 DPI 逐文件可调
- 自动计费（单面 0.2 元/页，双面 0.3 元/张），内置收支清算页面
- 云任务接收：SocketIO 长连接 + HTTP 拉取双通道，断线自动重连
- 离线同步：离线订单暂存本地 SQLite，联网后自动上传
- 浅色/深色主题跟随系统

---

## 云打印系统

微信小程序提交打印任务 → Flask 后端存储分发 → Windows 客户端自动打印。

```
mobile_apps/
├── h_n_print/              # 微信小程序（原生框架）
├── android_app/            # Android WebView 应用（Capacitor，无需微信）
└── printer-backend/        # Flask + SocketIO + SQLite 后端
```

### 后端部署

```bash
cd mobile_apps/printer-backend
pip install -r requirements.txt
cp config.py.example config.py   # 填写微信/服务器配置
# 开发模式
python app.py
# 生产模式（Gunicorn）
gunicorn -c gunicorn_config.py app:app
```

### 小程序开发

用微信开发者工具打开 `mobile_apps/h_n_print/`，修改 `utils/config.js` 中的 `BASE_URL` 指向你的后端地址。

### Android App 开发

```bash
cd mobile_apps/android_app
npx cap sync android && npx cap open android
# 开发预览：直接用浏览器打开 index.html
```

### 核心特性

- 一单多文件：每文件独立份数 / 双面 / 页码范围 / 页数分析进度
- 页码范围：页数已知时数字网格点选（全部 / 单页=奇数 / 双页=偶数一键），未知时多行文本输入
- 无障碍打印（管理员）：提交后自动开始打印，支持立即 / 指定时间 / 倒计时三种开始方式
- 多文件并行上传，每文件独立进度条，支持上传中移除

---

## 架构要点

- **父子订单模型**：`orders`（父订单）+ `order_files`（子任务），一单多文件，父状态从子任务聚合
- **双通道分发**：SocketIO 实时推送 + HTTP 拉取，多打印机并发不重复分配（先锁后推）
- **MD5 去重**：同文件复用磁盘存储
- **许可密钥系统**：管理员创建限时密钥，访客兑换后获得临时打印权限
- **页数分析**：Word 文档上传后由本地工具 COM 转 PDF 统计页数回报，同文件（同 MD5）复用
- **断线恢复**：超时回滚、定时扫描、崩溃恢复多层防护
- **无障碍预约打印**：提交时打印机离线则先下发文件，到点由本地自触发打印（后端幂等兜底）

详细架构见 `CLAUDE.md` 和 `mobile_apps/CLAUDE.md`。

---

## License

MIT
