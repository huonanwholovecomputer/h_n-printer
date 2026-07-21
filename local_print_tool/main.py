"""
main.py — HN 本地打印工具 程序入口
启动 PySide6 应用，检查依赖，加载样式，显示主窗口。
"""

from __future__ import annotations

import logging
import os
import sys
import threading

# 确保本地模块可以导入
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PySide6.QtWidgets import QApplication, QMessageBox
from PySide6.QtGui import QIcon
from PySide6.QtCore import Qt, qInstallMessageHandler, QtMsgType


# ═══════════════════════════════════════════════════════════════
# stderr 过滤器 — 在文件描述符层面拦截 C 库的 fprintf(stderr, ...)
# ═══════════════════════════════════════════════════════════════

_STDERR_FILTERS = [
    b"iCCP: known incorrect sRGB profile",
    b"setPointSize: Point size <= 0",
]


def _install_stderr_filter() -> None:
    """劫持 fd 2 (stderr)，后台线程过滤无害的 C 库警告。"""
    # 保存原始 stderr fd
    saved_fd = os.dup(2)

    # 创建管道
    pipe_r, pipe_w = os.pipe()

    # 将 fd 2 指向管道写端（此后所有 fprintf(stderr,...) 进入管道）
    os.dup2(pipe_w, 2)
    os.close(pipe_w)

    def _worker() -> None:
        try:
            with os.fdopen(pipe_r, "rb", buffering=0) as reader:
                while True:
                    line = reader.readline()
                    if not line:
                        break
                    if any(f in line for f in _STDERR_FILTERS):
                        continue
                    os.write(saved_fd, line)
        except OSError:
            # 管道关闭 / 进程退出 — 正常退出路径
            pass
        except Exception as e:
            # 意外错误，尝试写回原始 stderr
            try:
                os.write(saved_fd, f"\n[stderr-filter 线程异常] {e}\n".encode())
            except Exception:
                pass
        finally:
            try:
                os.close(saved_fd)
            except OSError:
                pass

    t = threading.Thread(target=_worker, daemon=True, name="stderr-filter")
    t.start()


# ═══════════════════════════════════════════════════════════════
# Qt 消息过滤器（双重保险）
# ═══════════════════════════════════════════════════════════════

_qt_default_handler = None


def _qt_message_filter(msg_type: QtMsgType, context, msg: str) -> None:
    """过滤已知无害的 Qt 内部警告。"""
    if "setPointSize: Point size <= 0" in msg:
        return
    if "iCCP: known incorrect sRGB profile" in msg:
        return
    if _qt_default_handler is not None:
        _qt_default_handler(msg_type, context, msg)


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
    if not os.path.isfile(path):
        return ""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except (OSError, UnicodeDecodeError) as e:
        print(f"[警告] 无法读取样式表 {path}: {e}", file=sys.stderr)
        return ""


def main():
    """主函数。"""
    # ── 在 stderr 被劫持前保存原始 fd，用于异常追踪 ──
    _saved_stderr_fd = os.dup(2)

    # ── 安装 excepthook：traceback 写文件 + 打印到控制台 ──
    def _debug_excepthook(etype, value, tb):
        import traceback, datetime as _dt
        crash_log = os.path.join(os.path.dirname(os.path.abspath(__file__)), "crash_traceback.txt")
        lines = []
        lines.append(f"\n=== CRASH at {_dt.datetime.now()} ===\n")
        lines.extend(traceback.format_exception(etype, value, tb))
        lines.append("=== END CRASH ===\n")
        text = "".join(lines)
        with open(crash_log, "a", encoding="utf-8") as f:
            f.write(text)
        # 直接写回原始 stderr fd（绕过管道过滤器）
        try:
            os.write(_saved_stderr_fd, text.encode("utf-8", errors="replace"))
        except Exception:
            pass
        # 也输出到 stdout
        try:
            print(text, file=sys.stdout, flush=True)
        except Exception:
            pass
    sys.excepthook = _debug_excepthook

    # ── 必须在一切输出之前安装，拦截 C 库直接写 stderr ──
    _install_stderr_filter()

    setup_logging()
    logger = logging.getLogger("main")

    # 切换到脚本所在目录
    script_dir = os.path.dirname(os.path.abspath(__file__))
    os.chdir(script_dir)

    # 检查依赖
    missing = check_dependencies()
    app = QApplication(sys.argv)

    # 安装 Qt 消息过滤器，屏蔽已知无害警告
    global _qt_default_handler
    _qt_default_handler = qInstallMessageHandler(_qt_message_filter)

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

    # 配置路径
    config_path = os.path.join(script_dir, "print_config.json")

    # 启动主窗口（UI 优先显示，其他检查放入后台）
    from gui import MainWindow
    window = MainWindow(config_path=config_path, theme_manager=theme_manager)
    window.show()

    # 后台：外部工具检测（日志记录，不阻塞 UI）
    _check_external_tools(logger)

    # 后台：预热 Word COM + WPS COM
    from converter import start_word_warmup, stop_word_warmup, start_wps_warmup, stop_wps_warmup
    start_word_warmup()
    start_wps_warmup()

    # 后台：刷新引擎可用性（灰度 UI 下拉框）
    from PySide6.QtCore import QTimer
    QTimer.singleShot(500, window._refresh_engine_availability)

    # 应用退出时清理预热实例
    app.aboutToQuit.connect(stop_word_warmup)
    app.aboutToQuit.connect(stop_wps_warmup)

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
