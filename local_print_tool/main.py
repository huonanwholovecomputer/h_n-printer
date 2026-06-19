"""
main.py — HN 本地打印工具 程序入口
启动 PySide6 应用，检查依赖，加载样式，显示主窗口。
"""

from __future__ import annotations

import logging
import os
import sys

# 确保本地模块可以导入
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PySide6.QtWidgets import QApplication, QMessageBox
from PySide6.QtGui import QIcon
from PySide6.QtCore import Qt

from theme_manager import ThemeManager


def setup_logging():
    """配置日志系统。"""
    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    logging.basicConfig(
        level=logging.INFO,
        format=fmt,
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )


def check_dependencies() -> list[str]:
    """
    检查 Python 依赖是否可导入。
    返回缺少的模块名称列表。
    """
    required = {
        "PySide6": "PySide6",
        "PIL": "Pillow",
        "reportlab": "reportlab",
        "markdown": "markdown",
    }
    missing: list[str] = []
    for mod, pkg in required.items():
        try:
            __import__(mod)
        except ImportError:
            missing.append(pkg)
    return missing


def load_stylesheet(path: str) -> str:
    """加载 QSS 样式表文件。"""
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    return ""


def main():
    """主函数。"""
    setup_logging()
    logger = logging.getLogger("main")

    # 切换到脚本所在目录
    script_dir = os.path.dirname(os.path.abspath(__file__))
    os.chdir(script_dir)

    # 检查依赖
    missing = check_dependencies()
    app = QApplication(sys.argv)
    app.setApplicationName("HN 本地打印工具")
    app.setOrganizationName("HN-Print")
    # 设置程序图标
    icon_path = os.path.join(script_dir, "HN_printer.png")
    if os.path.isfile(icon_path):
        app.setWindowIcon(QIcon(icon_path))

    # 加载样式表（浅色 + 深色）
    dark_qss_path = os.path.join(script_dir, "styles_dark.qss")
    light_qss_path = os.path.join(script_dir, "styles_light.qss")
    dark_qss = load_stylesheet(dark_qss_path)
    light_qss = load_stylesheet(light_qss_path)

    # 初始化主题管理器
    theme_settings_path = os.path.join(script_dir, "theme_settings.json")
    theme_manager = ThemeManager(theme_settings_path)
    theme_manager.init(app, dark_qss, light_qss)

    if dark_qss and light_qss:
        logger.info("已加载浅色/深色双主题样式表")
    else:
        logger.warning("部分样式表文件未找到")

    # 如果有缺失的 Python 依赖，弹窗警告
    if missing:
        QMessageBox.warning(
            None,
            "缺少 Python 依赖",
            f"以下 Python 包未安装:\n{', '.join(missing)}\n\n"
            f"请运行: pip install -r requirements.txt\n\n"
            f"程序可能部分功能无法使用。"
        )

    # 检查外部工具（非阻塞提示，仅日志记录）
    _check_external_tools(logger)

    # 配置路径
    config_path = os.path.join(script_dir, "print_config.json")

    # 启动主窗口
    from gui import MainWindow
    window = MainWindow(config_path=config_path, theme_manager=theme_manager)
    window.show()

    logger.info("HN 本地打印工具已启动")
    sys.exit(app.exec())


def _check_external_tools(logger: logging.Logger):
    """检查并记录外部工具可用性。"""
    from converter import _find_libreoffice, _find_wkhtmltopdf
    from pdf_printer import _find_sumatra_pdf

    lo = _find_libreoffice()
    if lo:
        logger.info(f"✓ LibreOffice: {lo}")
    else:
        logger.warning("✗ 未找到 LibreOffice — Office 文档无法转换")

    wk = _find_wkhtmltopdf()
    if wk:
        logger.info(f"✓ wkhtmltopdf: {wk}")
    else:
        logger.warning("✗ 未找到 wkhtmltopdf — HTML/Markdown 无法转换")

    sumatra = _find_sumatra_pdf()
    if sumatra:
        logger.info(f"✓ SumatraPDF: {sumatra}")
    else:
        logger.warning("✗ 未找到 SumatraPDF — 将使用降级打印方案（可能弹出对话框）")


if __name__ == "__main__":
    main()
