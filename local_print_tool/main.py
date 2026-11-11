"""
main.py — HN 本地打印工具 程序入口
启动 PySide6 应用，检查依赖，加载样式，显示主窗口。
"""

from __future__ import annotations

import faulthandler
import logging
import os
import sys
import threading
import time

# 确保本地模块可以导入
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ═══════════════════════════════════════════════════════════════
# S2-03：隔离转换 worker 入口 —— 必须在导入 PySide6 / 创建 QApplication 之前分发
# ═══════════════════════════════════════════════════════════════
# office_sandbox 以 `python convert_worker.py <spec>`（源码）或
# `HN打印工具.exe --convert-worker <spec>`（打包）拉起本进程；后者会走到这里。
# 静态 import convert_worker 是刻意的：PyInstaller 只收集静态可达模块，动态
# `__import__("convert_worker")` 不会被打进包 ⇒ 打包版沙箱永远 unavailable。
import convert_worker as _convert_worker  # noqa: E402

if "--convert-worker" in sys.argv:
    _idx = sys.argv.index("--convert-worker")
    sys.exit(_convert_worker.main(sys.argv[_idx + 1:]))

from PySide6.QtWidgets import QApplication, QMessageBox
from PySide6.QtGui import QIcon
from PySide6.QtCore import Qt, qInstallMessageHandler, QtMsgType

# 用户数据目录管理（%APPDATA%\HN打印工具）
import paths


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
    # 打包(console=False)后 sys.stdout 可能为 None；有有效 stream 才设置，
    # 否则用 logging 默认（stderr / lastResort），避免 basicConfig 以 None 做 stream 报错。
    kw = dict(
        level=logging.INFO,
        format=fmt,
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    try:
        if sys.stdout is not None:  # type: ignore[comparison-overlap]
            kw["stream"] = sys.stdout
    except Exception:
        pass
    logging.basicConfig(**kw)


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


# ═══════════════════════════════════════════════════════════════
# 崩溃取证（2026-11-04 现场）
# ═══════════════════════════════════════════════════════════════
# 现场：13:28:57 本地工具规划完一批手动双面（3 个文件 / 3 张纸）后就**直接消失**，
# 日志停在那两行「第 1 趟 / 第 2 趟」上，用户看到的是一次崩溃退出。事后取证：
#   · logs/crash_traceback.txt 不存在（Python 层异常一个都没走到 excepthook）；
#   · 应用事件日志里没有任何 Application Error / WER 记录，CrashDumps 里也没有新 dump
#     —— 本机 WER 是 HKLM ...\Windows Error Reporting\Disabled = 1（全局关掉）。
# 结论：这是一次**原生层**的死亡（GDI 打印 / 驱动 / fitz / Pillow 那一段），Python 和
# 系统都没有留下任何栈，只能靠程序自己。
# 所以这里补两层自我取证，都不改变业务行为：
#   ① faulthandler：访问违例（0xC0000005）等致命信号到来时，把**所有线程**的 Python 栈
#      追加写进 logs/crash_faulthandler.log（不依赖 WER，也不依赖 excepthook）；
#   ② threading.excepthook：后台线程里没被捕获的异常以前直接蒸发（云端下载 / 上报 /
#      页数分析全是后台线程，GUI 主线程看不到），现在落盘 + 进当日日志。
_CRASH_FH = None          # 保活：faulthandler 只在文件对象上持弱引用，被 GC 就静默失效


def install_crash_handlers(logger=None) -> str:
    """启用 faulthandler 与线程 excepthook，返回 faulthandler 文件路径（失败返回 ""）。"""
    global _CRASH_FH
    try:
        fh_path = os.path.join(paths.logs_dir(), "crash_faulthandler.log")
    except Exception:
        fh_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "crash_faulthandler.log")

    # ① faulthandler —— 致命信号 → 全线程的 Python 栈
    if _CRASH_FH is None:
        try:
            _CRASH_FH = open(fh_path, "a", encoding="utf-8", errors="replace")
            _CRASH_FH.write(f"\n===== 进程启动 {time.strftime('%Y-%m-%d %H:%M:%S')} "
                            f"(pid={os.getpid()}) =====\n")
            _CRASH_FH.flush()
            faulthandler.enable(file=_CRASH_FH, all_threads=True)
            if logger:
                logger.info(f"崩溃取证已启用（致命信号栈 → {fh_path}）")
        except Exception as e:
            _CRASH_FH = None
            if logger:
                logger.warning(f"启用 faulthandler 失败（崩溃取证能力受限）: {e}")

    # ② 后台线程未捕获异常 → 落盘 + 当日日志（原来一声不响地蒸发）
    def _thread_excepthook(args):
        name = getattr(getattr(args, "thread", None), "name", "?")
        text = "".join(__import__("traceback").format_exception(
            args.exc_type, args.exc_value, args.exc_traceback))
        line = (f"\n=== 线程异常 at {time.strftime('%Y-%m-%d %H:%M:%S')} "
                f"(thread={name}) ===\n{text}=== END ===\n")
        try:
            if _CRASH_FH is not None:
                _CRASH_FH.write(line)
                _CRASH_FH.flush()
        except Exception:
            pass
        if logger:
            logger.error(f"后台线程 {name} 未捕获异常: {args.exc_type.__name__}: "
                         f"{args.exc_value}")
        try:
            print(line, file=sys.stderr, flush=True)
        except Exception:
            pass

    threading.excepthook = _thread_excepthook
    return fh_path if _CRASH_FH is not None else ""


# ═══════════════════════════════════════════════════════════════
# 打印原生模块预热（2026-11-11 现场卡死）
# ═══════════════════════════════════════════════════════════════
# 现场：打印机断电、但 USB 还连着，点「开始打印」后整个进程**无声消失**（应用日志
# 从 20:57:11 直接空白到 21:03 重启），用户看到的就是"卡死"。
# 取证（logs/crash_faulthandler.log，20:55:05 那次）：
#   · 打印线程：`<frozen importlib._bootstrap> create_module` ←
#     `pdf_printer.py:1490 in _print_pdf_native`（该函数体里才 `import win32print /
#     win32ui / win32con` —— 惰性 import 全在打印线程上）
#   · 主线程：`Garbage-collecting` ← `enum.py` 建类 ← `gui.py:1286 in announce()`
#   ⇒ 打印线程在**首次加载 pywin32 原生扩展**（win32ui 会初始化 MFC），同一刻主线程在
#     垃圾回收里分配对象 ⇒ 访问违例（0xC0000005），Python 层没有 traceback。
# 修法：所有原生扩展都在**主线程、任何 QThread 起来之前**加载完；打印线程里那些
#      `import win32print/win32ui/win32con` 就退化成查 sys.modules 的 no-op，不再是
#      "边打边加载 C 扩展"。打印路径一行都没改（惰性 import 保留，兼容不装 pywin32 的场景）。
_PRELOAD_MODULES = ("fitz", "win32print", "win32con", "pywintypes", "win32api",
                    "win32ui", "win32gui")


def preload_native_modules(logger=None) -> list:
    """主线程预热打印路径会用到的原生扩展，返回加载失败的模块名（空列表 = 全好）。"""
    failed: list = []
    for name in _PRELOAD_MODULES:
        try:
            __import__(name)
        except Exception as e:      # 缺 pywin32 / 没有显示器环境等都不该拦住启动
            failed.append(name)
            if logger:
                logger.warning(f"预热 {name} 失败（打印路径可能退化）: "
                               f"{type(e).__name__}: {e}")
    if logger and not failed:
        logger.info("打印原生模块已预热（" + " / ".join(_PRELOAD_MODULES) + "）")
    return failed


def main():
    """主函数。"""
    # ── 强制 UTF-8 输出（打包后 Windows 默认 stdout/stderr 为 GBK，
    #    日志含 ✓（U+2713）等字符时 logging.StreamHandler 会抛 UnicodeEncodeError，
    #    导致打包版日志输出异常；开发运行 main.py 因（部分）终端/重定向为 UTF-8 而不复现） ──
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    # 供 logging 模块直达文件/恶意写路径，统一 UTF-8
    try:
        os.environ["PYTHONIOENCODING"] = "utf-8"
    except Exception:
        pass

    # ── 在 stderr 被劫持前保存原始 fd，用于异常追踪 ──
    _saved_stderr_fd = os.dup(2)

    # ── 安装 excepthook：traceback 写文件 + 打印到控制台 ──
    def _debug_excepthook(etype, value, tb):
        import traceback, datetime as _dt
        # 写入用户数据目录（安装版程序目录不可写）；paths 不可用时回退程序目录
        try:
            crash_log = os.path.join(paths.logs_dir(), "crash_traceback.txt")
        except Exception:
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

    # ── 崩溃取证：faulthandler + 后台线程 excepthook（2026-11-04 打印中无声消失那次）──
    # 必须在任何工作线程（云端连接、转换、打印）起来之前装好，否则它们的栈抓不到。
    try:
        install_crash_handlers(logger)
    except Exception as e:
        logger.warning(f"崩溃取证安装失败（不影响使用）: {e}")

    # ── 原生扩展主线程预热（2026-11-11 打印中 access violation 那次）──
    # 必须在 QApplication/MainWindow（会起 QThread）之前：让 win32ui 的 MFC 初始化与
    # 其它 C 扩展的加载都发生在主线程，打印线程里就只剩查表。
    try:
        preload_native_modules(logger)
    except Exception as e:
        logger.warning(f"打印原生模块预热失败（不影响使用）: {e}")

    # ── 单实例锁：已有实例在运行时，通知其将窗口置于前台，本实例直接退出 ──
    from single_instance import acquire
    if not acquire():
        logger.info("程序已在运行：已通知既有实例将窗口置于前台，本实例退出")
        return

    # 切换到脚本所在目录（打包版为 exe 目录；安装版为 C:\Program Files\h_n printer）
    script_dir = os.path.dirname(os.path.abspath(__file__))
    os.chdir(script_dir)

    # 用户数据统一迁移到 %APPDATA%\HN打印工具（旧版绿色版数据在程序目录 → 复制迁移，
    # 幂等；安装版/新版数据已在数据目录则跳过）。必须在读配置前执行。
    try:
        paths.migrate_legacy_data(script_dir)
    except Exception as e:
        logger.warning(f"用户数据目录迁移失败（将按新目录空配置启动）: {e}")

    # 检查依赖
    missing = check_dependencies()
    app = QApplication(sys.argv)

    # ── Qt 自带文案中文化（确认框的 Yes/No/Cancel、标准按钮、右键菜单、补录框）──
    # 必须在建任何 QWidget 之前装：Qt 在控件构造时就取了翻译，晚装不会重译已建好的控件。
    # 详见 i18n_zh.py（qtbase_zh_CN 由 PySide6 自带，PyInstaller 的 Qt hook 会一起打进包）。
    try:
        from i18n_zh import install_qt_chinese

        if install_qt_chinese(app):
            logger.info("已加载 Qt 简体中文翻译（确认框按钮为中文）")
        else:
            logger.warning("未找到 Qt 中文翻译文件：确认框按钮可能仍是英文")
    except Exception as e:
        logger.warning(f"安装 Qt 简体中文翻译失败（确认框按钮可能仍是英文）: {e}")

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

    # 初始化主题管理器（主题设置存 %APPDATA%\HN打印工具）
    theme_manager = ThemeManager(paths.theme_settings_path())
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

    # 配置路径（%APPDATA%\HN打印工具\print_config.json；迁移逻辑见上方 migrate_legacy_data）
    config_path = paths.config_path()

    # 启动主窗口（UI 优先显示，其他检查放入后台）
    from gui import MainWindow
    window = MainWindow(config_path=config_path, theme_manager=theme_manager)
    window.show()

    # 后台：外部工具检测（日志记录，不阻塞 UI）
    _check_external_tools(logger)

    # 2026-10-24：**不再**在启动时预热 Word/WPS，也不再定时刷新引擎可用性。
    # 原因：预热会在后台常驻 Office 进程（且旧实现用 Dispatch 会附着用户已打开的实例）；
    # 引擎可用性第一次真正需要时（选中 Word 文件重建下拉 / 转换前）才算，探测本身也不再碰 COM。
    # 需要预热的老办法仍可用：设置 HN_WARM_OFFICE=1（converter.start_*_warmup 会生效）。
    from converter import start_word_warmup, stop_word_warmup, start_wps_warmup, stop_wps_warmup
    start_word_warmup()   # 默认 no-op（HN_WARM_OFFICE!=1）；显式设置时才预热
    start_wps_warmup()
    # 应用退出时清理预热实例（未预热时是 no-op）
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
        logger.warning("✗ 未找到 wkhtmltopdf — Markdown 无法转换（HTML 已不支持）")

    sumatra = _find_sumatra_pdf()
    if sumatra:
        logger.info(f"✓ SumatraPDF: {sumatra}")
    else:
        logger.warning("✗ 未找到 SumatraPDF — 将使用降级打印方案（可能弹出对话框）")


if __name__ == "__main__":
    main()
