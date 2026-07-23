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

需安装外部依赖：LibreOffice、wkhtmltopdf、SumatraPDF（详见 `local_print_tool/README.md`）。

### 核心特性

- 多引擎 Word 转换：Microsoft Word COM → WPS COM → LibreOffice 智能降级
- 三级打印降级：Windows GDI → SumatraPDF → 应用层循环
- 单面/双面、页码范围、渲染 DPI 逐文件可调
- 自动计费（单面 0.2 元/页，双面 0.3 元/张）
- 云任务接收：通过 SocketIO 连接后端，接收远程打印任务

---

## 云打印系统

微信小程序提交打印任务 → Flask 后端存储分发 → Windows 客户端自动打印。

```
mobile_apps/
├── h_n_print/              # 微信小程序（原生框架）
├── android_app/            # Android WebView 应用（Capacitor）
└── printer-backend/        # Flask + SocketIO + SQLite 后端
```

### 后端部署

```bash
cd mobile_apps/printer-backend
pip install -r requirements.txt
cp config.py.example config.py   # 填写配置
# 开发模式
python app.py
# 生产模式（Gunicorn）
gunicorn -c gunicorn_config.py app:app
```

### 小程序开发

用微信开发者工具打开 `mobile_apps/h_n_print/`，修改 `utils/config.js` 中的 `BASE_URL` 指向你的后端地址。

---

## 架构要点

- **父子订单模型**：`orders`（父订单）+ `order_files`（子任务），支持一单多文件
- **双通道分发**：SocketIO 实时推送 + HTTP 拉取，保证多打印机并发不重复分配
- **MD5 去重**：文件上传后计算 MD5，同文件复用磁盘存储
- **许可密钥系统**：管理员创建限时密钥，访客兑换后获得临时打印权限
- **断线恢复**：多层防护（超时回滚、定时扫描、崩溃恢复）

详细架构见 `CLAUDE.md` 和 `mobile_apps/CLAUDE.md`。

---

## License

MIT
