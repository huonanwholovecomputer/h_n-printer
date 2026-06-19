# HN 本地打印工具

一个基于 PySide6 的 Windows 桌面打印工具，支持多种文件格式的一键批量打印、自动计费、双面打印和浅色/深色主题切换。

## 功能特性

- **多格式支持**：PDF、Word (`.doc`/`.docx`)、Markdown、HTML、TXT、CSV、图片 (JPG/PNG/BMP/GIF/WEBP)
- **拖放添加**：从资源管理器直接拖拽文件到任务列表
- **自动转换**：所有非 PDF 文件自动转换为 PDF 后再打印
- **静默打印**：优先使用 Windows 原生 GDI API，无需弹出打印对话框
- **双面打印**：支持长边/短边翻转
- **页码范围**：精确指定要打印的页面，支持多段范围（如 `1-5,7,10-15`）
- **份数控制**：每个文件独立设置打印份数
- **自动计费**：可配置单面/双面单价，自动计算每项任务费用和总费用
- **浅色/深色主题**：支持跟随系统、浅色、深色三种模式
- **配置自动保存**：打印机选择、任务列表、价格参数自动持久化
- **右键菜单**：移除选中任务、双击打开文件预览

## 核心流程

```
文件 → 自动检测类型 → 转换引擎 → PDF → Windows GDI 打印
```

| 文件类型 | 转换引擎 |
|----------|----------|
| PDF | 直接打印（PyPDF2 读取信息） |
| Word (.doc/.docx) | LibreOffice 无头模式 |
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
┌─────────────────────────────────────────────────────────┐
│  菜单栏: 文件 | 主题 | 帮助                              │
├─────────────────────────────────────────────────────────┤
│  打印机选择 | 双面模式 | 保留临时PDF | 单面/双面单价     │
├───────────────────────────┬─────────────────────────────┤
│  文件列表表格              │  编辑面板                    │
│  - 文件名                  │  - 份数 [+/-]              │
│  - 份数                    │  - 单/双面                  │
│  - 双面                    │  - 页码范围                 │
│  - 页码范围                │                             │
│  - 页数 / 方向 / 费用     │  当前文件信息                │
├───────────────────────────┴─────────────────────────────┤
│  进度条                                                   │
│  [清空列表]                        [开始打印]             │
│  日志输出区域                                             │
│  状态栏                                                   │
└─────────────────────────────────────────────────────────┘
```

## 项目结构

```
local_print_tool/
├── main.py              # 程序入口，依赖检查，启动窗口
├── gui.py               # PySide6 主窗口界面 (MainWindow)
├── converter.py         # 通用文件转换器 (→ PDF)
├── pdf_printer.py       # PDF 静默打印模块 (Windows GDI)
├── printer_config.py    # 配置管理 (JSON 读写 + 计费)
├── theme_manager.py     # 主题管理器 (浅色/深色/系统)
├── styles_dark.qss      # 深色主题样式表
├── styles_light.qss     # 浅色主题样式表
├── requirements.txt     # Python 依赖清单
├── print_config.json    # 用户配置文件 (自动生成)
├── theme_settings.json  # 主题设置 (自动生成)
└── HN_printer.png       # 程序图标
```

## 技术栈

- **UI 框架**: [PySide6](https://wiki.qt.io/Qt_for_Python) (Qt for Python)
- **PDF 处理**: PyPDF2, pdfkit, reportlab
- **Office 兼容**: python-docx
- **Windows 集成**: pywin32 (GDI 打印 API)
- **Markdown 渲染**: markdown

## License

MIT

## 致谢

本项目依赖以下开源软件：
- [LibreOffice](https://www.libreoffice.org/) — LGPLv3
- [wkhtmltopdf](https://wkhtmltopdf.org/) — LGPLv3
- [SumatraPDF](https://www.sumatrapdfreader.org/) — GPLv3
