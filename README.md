# HN 本地打印工具

一个基于 PySide6 的 Windows 桌面打印工具，支持多种文件格式的一键批量打印、自动计费、双面打印和浅色/深色主题切换。

## 功能特性

- **多格式支持**：PDF、Word (`.doc`/`.docx`)、Markdown、HTML、TXT、CSV、图片 (JPG/PNG/BMP/GIF/WEBP)
- **拖放添加**：从资源管理器直接拖拽文件到任务列表
- **粘贴文件**：`Ctrl+V` 或右键粘贴剪贴板中的文件（文件管理器复制优先）
- **键盘快捷键**：`Ctrl+C` 复制总价、`Ctrl+Shift+C` 复制计费明细、`Ctrl+V` 粘贴文件、`Delete`/`Ctrl+D` 删除任务
- **多引擎转换**：Word 文件支持 Microsoft Word COM / WPS COM / LibreOffice 三引擎智能降级，启动时 COM 预热加速
- **静默打印**：三级降级策略——Windows GDI 原生 API → SumatraPDF → 应用层循环打印，无需弹出打印对话框
- **双面打印**：每个任务独立设置双面/单面和长边/短边翻转模式
- **页码范围**：精确指定要打印的页面，支持多段范围（如 `1-5,7,10-15`），自动排序，格式错误实时高亮
- **渲染 DPI 调节**：全局默认（200/300/400/600）可调，逐文件可覆盖，低 DPI 大幅提速
- **份数控制**：每个文件独立设置打印份数
- **图片处理**：自动识别图片为单面单页，双面/页码范围控件自动禁用
- **自动计费**：可配置单面/双面单价，自动计算每项任务费用和总费用；计费明细支持一键复制
- **清空撤回**：清空列表后 5 秒内可通过琥珀色按钮撤回操作
- **精确进度条**：进度条精确到每面（非任务级别），支持预估总面数
- **平滑滚动**：任务列表、任务参数面板、日志区域均启用缓动平滑滚动
- **浅色/深色主题**：支持跟随系统、浅色、深色三种模式，窄滚动条蓝色 hover
- **自检工具**：帮助菜单 → 自检，一键检查外部工具/COM/打印机状态
- **配置自动保存**：打印机选择、任务列表、价格参数、DPI 设置自动持久化

## 核心流程

```
文件 → 自动检测类型 → 转换引擎 → PDF → Windows GDI 打印
```

| 文件类型 | 转换引擎 |
|----------|----------|
| PDF | 直接打印（PyPDF2 读取信息，GDI 渲染） |
| Word (.doc/.docx) | Microsoft Word COM → WPS COM → LibreOffice (智能降级) |
| Markdown / HTML | markdown → HTML → wkhtmltopdf |
| TXT / CSV | reportlab |
| 图片 (JPG/PNG/BMP/GIF/WEBP) | reportlab |

## 安装说明

### 1. Python 环境要求

- Python 3.9+
- Windows 10/11（目前仅支持 Windows）

### 2. 安装 Python 依赖

```bash
pip install -r requirements.txt
```

### 3. 安装外部程序（必须）

本工具依赖以下三款第三方软件，请下载并安装：

| 软件 | 用途 | 下载链接 |
|------|------|----------|
| **LibreOffice** | Office 文档转 PDF | [下载](https://www.libreoffice.org/download/) |
| **wkhtmltopdf** | HTML/Markdown 转 PDF | [下载](https://wkhtmltopdf.org/downloads.html) |
| **SumatraPDF** | Windows 静默打印 PDF（推荐） | [下载](https://www.sumatrapdfreader.org/download-free-pdf-viewer) |

> **安装提示**：
> - LibreOffice 和 wkhtmltopdf 安装后请确保它们已加入系统 PATH，或安装到默认路径（程序会自动查找）
> - SumatraPDF 推荐使用绿色便携版，放到本程序同级目录即可；也可安装到 `C:\Program Files\SumatraPDF\`
> - 如果未安装 SumatraPDF，程序会降级使用其他打印方案（可能会弹出系统对话框）

## 快速开始

```bash
cd local_print_tool
python main.py
```

## 界面概览

```
┌──────────────────────────────────────────────────────────────────┐
│  菜单栏: 文件 | 主题 | 帮助(快捷键/自检/关于)                      │
├──────────────────────────────────────────────────────────────────┤
│  打印机选择 | 保存转换副本 | 单面/双面单价 | 渲染DPI | 主题切换     │
├──────────────────────────────┬───────────────────────────────────┤
│  文件列表表格                 │  编辑面板 (可平滑滚动)              │
│  - 文件名                     │  - 份数 [+/-]                     │
│  - 份数                       │  - 单/双面 + 双面模式              │
│  - 单/双面                    │  - 页码范围 (多行输入, 自动排序)    │
│  - 页码范围                   │  - PDF转换引擎 (Word/WPS/Libre)   │
│  - 页数 / 方向 / 引擎 / 费用 │  - 渲染质量 (逐文件覆盖)            │
├──────────────────────────────┴───────────────────────────────────┤
│  合计: ¥0.00  [📋 复制] [⏷ 计费明细]                              │
│  进度条 (精确到每面)                                               │
│  [📂 添加文件] [✖ 清空列表(可撤回)]         [▶ 开始打印]           │
│  日志区域 (可拖动调整高度, 平滑滚动)                                │
│  状态栏                                                            │
└──────────────────────────────────────────────────────────────────┘
```

## 项目结构

```
local_print_tool/
├── main.py              # 程序入口，依赖检查，stderr 过滤，启动窗口
├── gui.py               # PySide6 主窗口界面 (MainWindow)
├── converter.py         # 通用文件转换器 (→ PDF)
├── pdf_printer.py       # PDF 静默打印模块 (三级降级: GDI → SumatraPDF → 循环)
├── printer_config.py    # 配置管理 (JSON 读写 + 计费)
├── theme_manager.py     # 主题管理器 (浅色/深色/系统)
├── styles_dark.qss      # 深色主题样式表
├── styles_light.qss     # 浅色主题样式表
├── requirements.txt     # Python 依赖清单
├── print_config.json    # 用户配置文件 (自动生成)
├── theme_settings.json  # 主题设置 (自动生成)
├── HN_printer.png       # 程序图标 (PNG)
├── HN_printer.ico       # 程序图标 (ICO)
├── HN打印工具.spec      # PyInstaller 打包配置 (Release)
├── HN打印工具_debug.spec # PyInstaller 打包配置 (Debug/控制台)
└── version_info.txt     # Windows 可执行文件版本信息
```

## 技术栈

- **UI 框架**: [PySide6](https://wiki.qt.io/Qt_for_Python) (Qt for Python)
- **PDF 渲染**: PyMuPDF (fitz) + Pillow (GDI 位图渲染)
- **PDF 信息**: PyPDF2 (页数/方向读取)
- **Office 兼容**: python-docx + win32com (Word/WPS COM 自动化)
- **Windows 集成**: pywin32 (GDI 打印 API、DEVMODE 配置)
- **格式转换**: pdfkit, reportlab, markdown

## 版本历史

| 版本 | 日期 | 主要变更 |
|------|------|----------|
| **v3.0** | 2026-06 | 粘贴文件(Ctrl+V/右键)URL优先解析；帮助→自检(外部工具/COM/打印机)；关于+GitHub+学习用途声明；keep_temp实时同步修复；清空终止转换+撤回重转换 |
| **v2.9** | 2026-06 | 平滑滚动统一(任务表/参数面板/日志)；滚轮事件转发QScrollArea；右键粘贴文件；表格ScrollPerPixel自由滚动；日志区可拖动调整高度 |
| **v2.8** | 2026-06 | DPI渲染质量调节(200/300/400/600)全局+逐文件覆盖；进度条清空归零；转换副本即时保存(转换完成即拷)；中文宽度1.5显示更多 |
| **v2.7** | 2026-06 | 精确进度条(逐面更新)；图片强制单面计费(双面/页码—占位)；清空列表5秒撤回(琥珀色按钮)；计费明细新格式(💰另起行+文件名截断) |
| **v2.6** | 2026-06 | GDI优先打印策略恢复；dmCopies双重份数修复(2份→4份bug)；多引擎Word/WPS/LibreOffice转换+COM预热；PDF/图片禁用引擎选择 |
| **v2.5** | 2026-06 | 单数页双面多份打印副本独立分隔（GDI 插入空白页 + SumatraPDF 逐份打印） |
| **v2.4** | 2026-06 | 横向 PDF 打印方向修复（DEVMODE Orientation）；图片转换 landscape 导入缺失修复；单页双面计费公式零项显示修复 |
| **v2.3** | 2026-06 | 双面翻转修正；页码范围自动排序+错误高亮；合计复制/计费明细复制按钮；键盘快捷键；列宽自适应；Word 重复转换 Bug 修复；图片页数显示修复 |
| **v2.2** | 2026-06 | 修复长边/短边翻转搞反、页码范围顺序问题 |
| **v2.1** | 2026-06 | 打印引擎重写、stderr 过滤、错误处理增强 |
| **v2.0** | 2026-06 | 初始版本 |

## License

MIT

## 致谢

本项目依赖以下开源软件：
- [LibreOffice](https://www.libreoffice.org/) — LGPLv3
- [wkhtmltopdf](https://wkhtmltopdf.org/) — LGPLv3
- [SumatraPDF](https://www.sumatrapdfreader.org/) — GPLv3
