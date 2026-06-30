"""
converter.py — 通用文件转换器 UniversalConverter
将所有可打印文件格式统一转换为 PDF，支持:
  - TXT / CSV  → reportlab
  - 图片 (JPG/PNG/BMP/GIF/WEBP) → reportlab
  - Markdown    → markdown → HTML → pdfkit (wkhtmltopdf)
  - Office 文档 → LibreOffice 无头模式
"""

from __future__ import annotations

import logging
import os
import platform
import shutil
import subprocess
import tempfile
import threading
import time
from typing import Callable

logger = logging.getLogger(__name__)


# ============================================================
# 多引擎转换缓存
# ============================================================

# 引擎可用性缓存 (None=未检测, True=可用, False=不可用)
_engine_cache: dict[str, bool | None] = {
    "word": None,
    "wps": None,
}

# WPS COM ProgID 缓存 (检测到可用时记录)
_wps_progid: str | None = None

# Word COM 后台预热
_warm_word_thread: threading.Thread | None = None
_warm_word_running = False


def start_word_warmup() -> None:
    """
    后台线程预热 Word COM — 启动 WINWORD.EXE 并保持存活。
    后续所有 COM Dispatch 不再冷启动，显著加速页数统计和打印。
    """
    global _warm_word_thread, _warm_word_running
    if _warm_word_running:
        return

    _warm_word_running = True

    def _warm_worker() -> None:
        global _warm_word_running
        import pythoncom
        import win32com.client

        pythoncom.CoInitialize()
        try:
            word = win32com.client.dynamic.Dispatch("Word.Application")
            word.Visible = False
            try:
                word.DisplayAlerts = 0
            except Exception:
                pass
            logger.info("Word COM 已预热（后台常驻）")

            # 保持存活，等待外部信号
            while _warm_word_running:
                time.sleep(1)

            try:
                word.Quit()
            except Exception:
                pass
            logger.info("Word COM 预热进程已退出")
        except Exception as e:
            logger.warning(f"Word COM 预热失败（系统可能未安装 Word）: {e}")
            _warm_word_running = False
        finally:
            pythoncom.CoUninitialize()

    _warm_word_thread = threading.Thread(
        target=_warm_worker, daemon=True, name="word-warmup"
    )
    _warm_word_thread.start()


def stop_word_warmup() -> None:
    """停止 Word COM 预热实例。"""
    global _warm_word_running
    _warm_word_running = False


# WPS COM 后台预热
_warm_wps_thread: threading.Thread | None = None
_warm_wps_running = False


def start_wps_warmup() -> None:
    """后台线程预热 WPS COM — 启动 wps.exe 并保持存活。"""
    global _warm_wps_thread, _warm_wps_running
    if _warm_wps_running:
        return

    _warm_wps_running = True

    def _warm_worker() -> None:
        global _warm_wps_running
        import pythoncom
        import win32com.client

        pythoncom.CoInitialize()
        try:
            # 先检测 WPS ProgID
            for pid in ["WPS.Application", "KWPS.Application"]:
                try:
                    wps = win32com.client.dynamic.Dispatch(pid)
                    global _wps_progid
                    _wps_progid = pid
                    break
                except Exception:
                    continue
            else:
                raise RuntimeError("WPS COM ProgID 未注册")

            wps.Visible = False
            try:
                wps.DisplayAlerts = 0
            except Exception:
                pass
            logger.info(f"WPS COM 已预热（后台常驻, ProgID={_wps_progid}）")

            while _warm_wps_running:
                time.sleep(1)

            try:
                wps.Quit()
            except Exception:
                pass
            logger.info("WPS COM 预热进程已退出")
        except Exception as e:
            logger.warning(f"WPS COM 预热失败（系统可能未安装 WPS）: {e}")
            _warm_wps_running = False
        finally:
            pythoncom.CoUninitialize()

    _warm_wps_thread = threading.Thread(
        target=_warm_worker, daemon=True, name="wps-warmup"
    )
    _warm_wps_thread.start()


def stop_wps_warmup() -> None:
    """停止 WPS COM 预热实例。"""
    global _warm_wps_running
    _warm_wps_running = False


def _safe_word_dispatch():
    """
    获取 Word COM 实例。
    有预热时复用预热进程（瞬间连接），无预热时正常 Dispatch。
    配合 _copy_to_temp() 使用——COM 操作副本文件，不干扰用户原文件。
    """
    import win32com.client
    return win32com.client.dynamic.Dispatch("Word.Application")


def get_available_engines() -> dict[str, bool]:
    """
    检测本机可用的 Word 打印引擎。

    有预热时直接认定 Word 可用（避免主线程冷启动 Dispatch）；
    无预热时才通过 _detect_word() 检测。
    """
    return {
        "word": _warm_word_running or _detect_word(),
        "wps": _warm_wps_running or _detect_wps(),
        "libreoffice": _find_libreoffice() is not None,
    }


def _copy_to_temp(file_path: str) -> str:
    """将文件复制到临时目录，返回临时路径。用于 COM 安全操作。"""
    ext = os.path.splitext(file_path)[1]
    fd, temp_path = tempfile.mkstemp(suffix=ext, prefix="_word_")
    os.close(fd)
    shutil.copy2(file_path, temp_path)
    logger.debug(f"已复制到临时文件: {os.path.basename(temp_path)}")
    return temp_path


# ============================================================
# 工具函数
# ============================================================

def _find_executable(name: str, search_paths: list[str]) -> str | None:
    """在指定路径列表中查找可执行文件，返回首个存在的路径。"""
    for p in search_paths:
        if os.path.isfile(p):
            return p
    # 尝试 PATH 中查找
    which = shutil.which(name)
    return which


def _find_libreoffice() -> str | None:
    """跨平台查找 LibreOffice 可执行文件路径。"""
    system = platform.system()
    if system == "Windows":
        candidates = [
            os.path.expandvars(r"%ProgramFiles%\LibreOffice\program\soffice.exe"),
            os.path.expandvars(r"%ProgramFiles(x86)%\LibreOffice\program\soffice.exe"),
            r"C:\Program Files\LibreOffice\program\soffice.exe",
            r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
        ]
    elif system == "Darwin":
        candidates = [
            "/Applications/LibreOffice.app/Contents/MacOS/soffice",
        ]
    else:  # Linux
        candidates = [
            "/usr/bin/soffice",
            "/usr/lib/libreoffice/program/soffice",
            "/usr/lib64/libreoffice/program/soffice",
        ]
    return _find_executable("soffice", candidates)


def _find_wkhtmltopdf() -> str | None:
    """查找 wkhtmltopdf 可执行文件。"""
    system = platform.system()
    if system == "Windows":
        candidates = [
            os.path.expandvars(r"%ProgramFiles%\wkhtmltopdf\bin\wkhtmltopdf.exe"),
            os.path.expandvars(r"%ProgramFiles(x86)%\wkhtmltopdf\bin\wkhtmltopdf.exe"),
            r"C:\Program Files\wkhtmltopdf\bin\wkhtmltopdf.exe",
        ]
    elif system == "Darwin":
        candidates = [
            "/usr/local/bin/wkhtmltopdf",
            "/opt/homebrew/bin/wkhtmltopdf",
        ]
    else:
        candidates = [
            "/usr/bin/wkhtmltopdf",
            "/usr/local/bin/wkhtmltopdf",
        ]
    return _find_executable("wkhtmltopdf", candidates)


# ============================================================
# Office 引擎检测 (Word / WPS)
# ============================================================

def _detect_word() -> bool:
    """
    检测 Microsoft Word 是否可用。
    先快速检查 EXE 文件，再通过 COM Dispatch 确认，结果缓存。
    """
    global _engine_cache
    if _engine_cache["word"] is not None:
        return _engine_cache["word"]

    # Step 1: 快速文件检测
    exe_found = False
    word_candidates = [
        os.path.expandvars(r"%ProgramFiles%\Microsoft Office\root\Office16\WINWORD.EXE"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft Office\root\Office16\WINWORD.EXE"),
        r"C:\Program Files\Microsoft Office\Office16\WINWORD.EXE",
        r"C:\Program Files (x86)\Microsoft Office\Office16\WINWORD.EXE",
        # Office 365 / Click-to-Run 路径
        os.path.expandvars(r"%ProgramFiles%\Microsoft Office\root\Office15\WINWORD.EXE"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft Office\root\Office15\WINWORD.EXE"),
    ]
    for p in word_candidates:
        if os.path.isfile(p):
            exe_found = True
            break
    if not exe_found and shutil.which("WINWORD.EXE"):
        exe_found = True

    if not exe_found:
        logger.info("未检测到 Microsoft Word (WINWORD.EXE)")
        _engine_cache["word"] = False
        return False

    # Step 2: COM Dispatch 确认（有预热时不杀进程）
    try:
        import win32com.client
        word = win32com.client.dynamic.Dispatch("Word.Application")
        if not _warm_word_running:
            try:
                word.Quit()
            except Exception:
                pass
        _engine_cache["word"] = True
        logger.info("✓ 检测到 Microsoft Word (COM 可用)")
        return True
    except Exception as e:
        logger.warning(f"检测到 WINWORD.EXE 但 COM 调用失败: {e}")
        _engine_cache["word"] = False
        return False


def _detect_wps() -> bool:
    """
    检测 WPS Office 是否可用。
    先搜索 wps.exe，再尝试多种 ProgID COM Dispatch，结果缓存。
    """
    global _engine_cache, _wps_progid
    if _engine_cache["wps"] is not None:
        return _engine_cache["wps"]

    # Step 1: 搜索 wps.exe
    exe_found = False
    wps_candidates = [
        os.path.expandvars(r"%LocalAppData%\Kingsoft\WPS Office\wps.exe"),
        os.path.expandvars(r"%ProgramFiles%\WPS Office\wps.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\WPS Office\wps.exe"),
        r"C:\Program Files\WPS Office\wps.exe",
        r"C:\Program Files (x86)\WPS Office\wps.exe",
    ]

    for p in wps_candidates:
        if os.path.isfile(p):
            exe_found = True
            break

    # 在 WPS 目录树中搜索（WPS 路径常带版本号）
    if not exe_found:
        search_bases = []
        for base in [
            os.path.expandvars(r"%ProgramFiles%\WPS Office"),
            os.path.expandvars(r"%ProgramFiles(x86)%\WPS Office"),
            os.path.expandvars(r"%LocalAppData%\Kingsoft\WPS Office"),
        ]:
            if os.path.isdir(base):
                search_bases.append(base)
        for base in search_bases:
            try:
                for root, dirs, files in os.walk(base):
                    if "wps.exe" in files:
                        exe_found = True
                        logger.info(f"搜索到 WPS: {os.path.join(root, 'wps.exe')}")
                        break
            except (PermissionError, OSError):
                continue
            if exe_found:
                break

    if not exe_found and shutil.which("wps.exe"):
        exe_found = True

    if not exe_found:
        logger.info("未检测到 WPS Office (wps.exe)")
        _engine_cache["wps"] = False
        return False

    # Step 2: COM Dispatch 确认（尝试不同 ProgID）
    try:
        import win32com.client
        for prog_id in ["WPS.Application", "KWPS.Application"]:
            try:
                wps = win32com.client.dynamic.Dispatch(prog_id)
                try:
                    wps.Quit()
                except Exception:
                    pass
                _engine_cache["wps"] = True
                _wps_progid = prog_id
                logger.info(f"✓ 检测到 WPS Office (ProgID={prog_id})")
                return True
            except Exception:
                continue
        logger.warning("检测到 wps.exe 但所有 ProgID COM 调用均失败")
        _engine_cache["wps"] = False
        return False
    except Exception as e:
        logger.warning(f"WPS COM 检测异常: {e}")
        _engine_cache["wps"] = False
        return False


# ============================================================
# Office COM 转换器 (Word / WPS)
# ============================================================

def _convert_via_word_com(file_path: str, output_pdf: str) -> None:
    """
    使用 Microsoft Word COM 将 .doc/.docx 文档转为 PDF。
    布局保真度最高 — 使用 Word 自身渲染引擎。
    操作临时副本，不干扰原文件。
    """
    import pythoncom
    import win32com.client

    abs_output = os.path.abspath(output_pdf)
    temp_input = _copy_to_temp(file_path)

    logger.info(f"Word COM: 开始转换 {os.path.basename(file_path)}")

    # COM 线程初始化（仅在首次初始化时才 CoUninitialize）
    com_init = pythoncom.CoInitialize()

    word = None
    doc = None
    try:
        word = _safe_word_dispatch()
        word.Visible = False
        # 兼容不同 Word 版本的 DisplayAlerts 属性名
        try:
            word.DisplayAlerts = 0  # wdAlertsNone
        except Exception:
            pass

        # wdOpenFormat = 自动检测, ReadOnly=True 避免修改原文件
        doc = word.Documents.Open(os.path.abspath(temp_input), ReadOnly=True)

        # ExportFormat=17 = wdExportFormatPDF
        doc.ExportAsFixedFormat(abs_output, ExportFormat=17)

        # wdDoNotSaveChanges = 0
        doc.Close(SaveChanges=0)
        doc = None

        if not _warm_word_running:
            word.Quit()
        word = None

        logger.info(f"Word COM → PDF 完成: {output_pdf}")
    except Exception as e:
        logger.error(f"Word COM 转换失败: {e}")
        raise RuntimeError(f"Microsoft Word 转换失败: {e}") from e
    finally:
        # 确保 COM 资源释放
        if doc is not None:
            try:
                doc.Close(SaveChanges=0)
            except Exception:
                pass
        if word is not None:
            try:
                if not _warm_word_running:
                    word.Quit()
            except Exception:
                pass
        # 仅当我们是首次初始化 COM 时才反初始化
        if com_init is None:
            pythoncom.CoUninitialize()
        if os.path.isfile(temp_input):
            try:
                os.remove(temp_input)
            except OSError:
                pass


def _convert_via_wps_com(file_path: str, output_pdf: str) -> None:
    """
    使用 WPS Office COM 将 .doc/.docx 文档转为 PDF。
    对中文文档排版兼容性好。
    """
    global _wps_progid
    import pythoncom
    import win32com.client

    abs_input = os.path.abspath(file_path)
    abs_output = os.path.abspath(output_pdf)

    # 复用检测阶段缓存的 ProgID
    prog_id = _wps_progid

    logger.info(f"WPS COM: 开始转换 {os.path.basename(file_path)} (ProgID={prog_id})")

    com_init = pythoncom.CoInitialize()

    wps = None
    doc = None
    try:
        wps = win32com.client.dynamic.Dispatch(prog_id)
        wps.Visible = False
        try:
            wps.DisplayAlerts = 0
        except Exception:
            pass

        doc = wps.Documents.Open(abs_input, ReadOnly=True)

        # WPS 兼容 Word ExportFormat 常量 (17 = PDF)
        doc.ExportAsFixedFormat(abs_output, ExportFormat=17)

        doc.Close(SaveChanges=0)
        doc = None

        wps.Quit()
        wps = None

        logger.info(f"WPS COM → PDF 完成: {output_pdf}")
    except Exception as e:
        logger.error(f"WPS COM 转换失败: {e}")
        raise RuntimeError(f"WPS Office 转换失败: {e}") from e
    finally:
        if doc is not None:
            try:
                doc.Close(SaveChanges=0)
            except Exception:
                pass
        if wps is not None:
            try:
                wps.Quit()
            except Exception:
                pass
        if com_init is None:
            pythoncom.CoUninitialize()


# ============================================================
# .docx 元数据检测
# ============================================================

def _read_docx_last_editor(file_path: str) -> str | None:
    """
    读取 .docx 文档最后编辑者。
    从 ZIP 内部 docProps/app.xml 的 <Application> 标签获取。

    Returns:
        "wps"  — 最后被 WPS 编辑
        "word" — 最后被 Microsoft Word 编辑
        None   — 无法判断（标签缺失、非 .docx 文件、读取失败）
    """
    ext = os.path.splitext(file_path)[1].lower()
    if ext != ".docx":
        return None

    try:
        import zipfile
        import xml.etree.ElementTree as ET

        with zipfile.ZipFile(file_path, 'r') as z:
            if 'docProps/app.xml' not in z.namelist():
                return None
            with z.open('docProps/app.xml') as f:
                ns = '{http://schemas.openxmlformats.org/officeDocument/2006/extended-properties}'
                tree = ET.parse(f)
                app = tree.find(f'{ns}Application')
                if app is None or not app.text:
                    return None
                text = app.text.upper()
                if "WPS" in text:
                    return "wps"
                if "MICROSOFT" in text or "WORD" in text:
                    return "word"
        return None
    except Exception:
        return None


# ============================================================
# COM 快速页数获取 (Word / WPS ComputeStatistics + 批量)
# ============================================================

def _get_com_page_counts_batch(
    file_paths: list[str],
    prefer_engine: str = "word",
) -> dict[str, int]:
    """
    一次 COM 会话批量获取多个 Word 文件的页数。
    比逐个调用 _get_word_page_count() 快 N 倍（只启动一次 COM 进程）。

    Args:
        file_paths: Word 文件路径列表
        prefer_engine: 首选引擎 "word" | "wps"

    Returns:
        {file_path: page_count}，失败的条目不会出现在结果中
    """
    if not file_paths:
        return {}

    results: dict[str, int] = {}

    def _batch_via_engine(prog_id: str, label: str) -> list[str]:
        """用指定引擎批量处理，返回未成功的文件列表。"""
        import pythoncom
        import win32com.client

        remaining: list[str] = []
        com_init = pythoncom.CoInitialize()

        app = None
        try:
            if prog_id == "Word.Application":
                app = _safe_word_dispatch()
            else:
                app = win32com.client.dynamic.Dispatch(prog_id)
            app.Visible = False
            try:
                app.DisplayAlerts = 0
            except Exception:
                pass

            for fp in file_paths:
                doc = None
                temp_fp: str | None = None
                try:
                    # 复制到临时文件，避免文件锁冲突
                    temp_fp = _copy_to_temp(fp)
                    doc = app.Documents.Open(os.path.abspath(temp_fp), ReadOnly=True)
                    results[fp] = int(doc.ComputeStatistics(2))  # 2 = wdStatisticPages
                    doc.Close(SaveChanges=0)
                    doc = None
                    logger.info(f"{label} 页数: {os.path.basename(fp)} → {results[fp]} 页")
                except Exception as e:
                    logger.warning(f"{label} 页数获取失败 ({os.path.basename(fp)}): {e}")
                    remaining.append(fp)
                    if doc is not None:
                        try:
                            doc.Close(SaveChanges=0)
                        except Exception:
                            pass
                finally:
                    if temp_fp and os.path.isfile(temp_fp):
                        try:
                            os.remove(temp_fp)
                        except OSError:
                            pass

            # 不要杀预热线程维持的 Word 进程
            if not _warm_word_running or "Word" not in label:
                try:
                    app.Quit()
                except Exception:
                    pass
            app = None
        except Exception as e:
            logger.warning(f"{label} 批量页数初始化失败: {e}")
            remaining = list(file_paths)
        finally:
            if app is not None:
                if not _warm_word_running or "Word" not in label:
                    try:
                        app.Quit()
                    except Exception:
                        pass
            if com_init is None:
                pythoncom.CoUninitialize()

        return remaining

    # 首选引擎
    first_choice = prefer_engine
    if first_choice == "word":
        remaining = _batch_via_engine("Word.Application", "Word")
        if remaining:
            _batch_via_engine(_wps_progid or "WPS.Application", "WPS")
    elif first_choice == "wps":
        remaining = _batch_via_engine(_wps_progid or "WPS.Application", "WPS")
        if remaining:
            _batch_via_engine("Word.Application", "Word")

    return results


def _get_word_page_count(file_path: str) -> int:
    """
    使用 Microsoft Word COM ComputeStatistics 快速获取页数。
    不生成 PDF，秒级响应。操作临时副本，不干扰原文件。
    """
    import pythoncom
    import win32com.client

    temp_fp = _copy_to_temp(file_path)
    com_init = pythoncom.CoInitialize()
    word = None
    doc = None
    try:
        word = _safe_word_dispatch()
        word.Visible = False
        try:
            word.DisplayAlerts = 0
        except Exception:
            pass

        doc = word.Documents.Open(os.path.abspath(temp_fp), ReadOnly=True)
        # 2 = wdStatisticPages
        page_count = doc.ComputeStatistics(2)
        doc.Close(SaveChanges=0)
        doc = None
        if not _warm_word_running:
            word.Quit()
        word = None

        logger.info(f"Word COM 页数: {os.path.basename(file_path)} → {page_count} 页")
        return int(page_count)
    finally:
        if doc is not None:
            try:
                doc.Close(SaveChanges=0)
            except Exception:
                pass
        if word is not None:
            try:
                if not _warm_word_running:
                    word.Quit()
            except Exception:
                pass
        if com_init is None:
            pythoncom.CoUninitialize()
        if os.path.isfile(temp_fp):
            try:
                os.remove(temp_fp)
            except OSError:
                pass


def _get_wps_page_count(file_path: str) -> int:
    """
    使用 WPS Office COM ComputeStatistics 快速获取页数。
    """
    global _wps_progid
    import pythoncom
    import win32com.client

    abs_input = os.path.abspath(file_path)
    prog_id = _wps_progid

    com_init = pythoncom.CoInitialize()
    wps = None
    doc = None
    try:
        wps = win32com.client.dynamic.Dispatch(prog_id)
        wps.Visible = False
        try:
            wps.DisplayAlerts = 0
        except Exception:
            pass

        doc = wps.Documents.Open(abs_input, ReadOnly=True)
        # 2 = wdStatisticPages (WPS 兼容此常量)
        page_count = doc.ComputeStatistics(2)
        doc.Close(SaveChanges=0)
        doc = None
        wps.Quit()
        wps = None

        logger.info(f"WPS COM 页数: {os.path.basename(file_path)} → {page_count} 页")
        return int(page_count)
    finally:
        if doc is not None:
            try:
                doc.Close(SaveChanges=0)
            except Exception:
                pass
        if wps is not None:
            try:
                wps.Quit()
            except Exception:
                pass
        if com_init is None:
            pythoncom.CoUninitialize()


# ============================================================
# 各格式转换器
# ============================================================

def _convert_txt_to_pdf(file_path: str, output_pdf: str) -> None:
    """TXT 文本 → PDF（reportlab，支持中文）。"""
    from reportlab.pdfgen import canvas
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    # 注册中文字体
    _register_chinese_font()

    # 读取文本内容
    with open(file_path, "r", encoding="utf-8", errors="replace") as f:
        text = f.read()

    c = canvas.Canvas(output_pdf, pagesize=A4)
    width, height = A4

    margin_left = 20 * mm
    margin_right = 20 * mm
    margin_top = 15 * mm
    margin_bottom = 15 * mm
    usable_width = width - margin_left - margin_right
    usable_height = height - margin_top - margin_bottom

    font_name = _get_chinese_font_name()
    font_size = 11
    line_height = font_size + 4

    lines = text.split("\n")
    y = height - margin_top

    for line in lines:
        if not line.strip():
            y -= line_height
        else:
            # 中文自动换行
            wrapped = _wrap_text_line(line, font_name, font_size, usable_width, c)
            for wline in wrapped:
                if y < margin_bottom:
                    c.showPage()
                    c.setFont(font_name, font_size)
                    y = height - margin_top
                c.setFont(font_name, font_size)
                c.drawString(margin_left, y, wline)
                y -= line_height

        if y < margin_bottom:
            c.showPage()
            c.setFont(font_name, font_size)
            y = height - margin_top

    c.save()
    logger.info(f"TXT → PDF 完成: {output_pdf}")


def _convert_csv_to_pdf(file_path: str, output_pdf: str) -> None:
    """CSV 表格 → PDF（reportlab 绘制表格）。"""
    import csv
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.units import mm
    from reportlab.lib import colors
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import PageBreak

    _register_chinese_font()
    font_name = _get_chinese_font_name()

    # 读取 CSV
    with open(file_path, "r", encoding="utf-8", errors="replace") as f:
        reader = csv.reader(f)
        rows = list(reader)

    if not rows:
        # 空 CSV，写个占位
        from reportlab.pdfgen import canvas as cvs
        c = cvs.Canvas(output_pdf, pagesize=A4)
        c.drawString(100, 500, "(空 CSV 文件)")
        c.save()
        return

    # 使用 landscape 方向（宽表友好）
    doc = SimpleDocTemplate(output_pdf, pagesize=landscape(A4),
                            leftMargin=10*mm, rightMargin=10*mm,
                            topMargin=10*mm, bottomMargin=10*mm)

    styles = getSampleStyleSheet()
    style_normal = styles["Normal"]
    style_normal.fontName = font_name
    style_normal.fontSize = 9

    # 将每格包装为 Paragraph
    def cell(text: str) -> Paragraph:
        return Paragraph(str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"), style_normal)

    table_data = [[cell(val) for val in row] for row in rows]

    max_rows_per_page = 40  # 横向每页大约行数
    elements = []

    for i in range(0, len(table_data), max_rows_per_page):
        chunk = table_data[i:i + max_rows_per_page]
        tbl = Table(chunk, repeatRows=1 if i == 0 else 0)
        tbl.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#45475a")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#888888")),
            ("FONTNAME", (0, 0), (-1, -1), font_name),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 2),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
            ("LEFTPADDING", (0, 0), (-1, -1), 4),
            ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ]))
        elements.append(tbl)
        if i + max_rows_per_page < len(table_data):
            elements.append(PageBreak())

    doc.build(elements)
    logger.info(f"CSV → PDF 完成: {output_pdf}")


def _convert_image_to_pdf(file_path: str, output_pdf: str) -> None:
    """图片 → PDF（reportlab，居中适应 A4）。"""
    from PIL import Image as PILImage
    from reportlab.pdfgen import canvas
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.units import mm

    img = PILImage.open(file_path)
    if img.mode not in ("RGB", "RGBA", "L"):
        img = img.convert("RGB")

    iw, ih = img.size
    # 根据图片宽高选择页面方向：宽图用横版，最大化打印面积
    if iw > ih:
        page_w, page_h = landscape(A4)
    else:
        page_w, page_h = A4

    c = canvas.Canvas(output_pdf, pagesize=(page_w, page_h))
    margin = 10 * mm
    max_w = page_w - 2 * margin
    max_h = page_h - 2 * margin

    scale = min(max_w / iw, max_h / ih, 1.0)
    draw_w = iw * scale
    draw_h = ih * scale

    x = (page_w - draw_w) / 2
    y = (page_h - draw_h) / 2

    # 保存临时图片用于嵌入
    tmp_img_path = output_pdf + ".tmp.png"
    try:
        img.save(tmp_img_path)
        c.drawImage(tmp_img_path, x, y, width=draw_w, height=draw_h)
    finally:
        if os.path.exists(tmp_img_path):
            os.remove(tmp_img_path)

    c.save()
    logger.info(f"图片 → PDF 完成: {output_pdf}")


def _convert_html_to_pdf(html_path_or_content: str, output_pdf: str, is_content: bool = False) -> None:
    """HTML 文件或内容 → PDF（pdfkit / wkhtmltopdf）。"""
    wk_path = _find_wkhtmltopdf()

    if wk_path:
        try:
            import pdfkit
        except ImportError:
            raise RuntimeError(
                "缺少 Python 包 pdfkit。请运行: pip install pdfkit\n"
                f"(已检测到 wkhtmltopdf: {wk_path})"
            )
        options = {
            "page-size": "A4",
            "encoding": "UTF-8",
            "enable-local-file-access": "",
            "no-outline": None,
            "margin-top": "10mm",
            "margin-bottom": "10mm",
            "margin-left": "10mm",
            "margin-right": "10mm",
        }
        config = pdfkit.configuration(wkhtmltopdf=wk_path)
        if is_content:
            pdfkit.from_string(html_path_or_content, output_pdf, options=options, configuration=config)
        else:
            pdfkit.from_file(html_path_or_content, output_pdf, options=options, configuration=config)
    else:
        raise RuntimeError(
            "未找到 wkhtmltopdf。请安装后加入 PATH。\n"
            "下载地址: https://wkhtmltopdf.org/downloads.html"
        )

    logger.info(f"HTML → PDF 完成: {output_pdf}")


def _convert_markdown_to_pdf(file_path: str, output_pdf: str) -> None:
    """Markdown → HTML → PDF。"""
    import markdown

    with open(file_path, "r", encoding="utf-8", errors="replace") as f:
        md_text = f.read()

    # Markdown → HTML（包含基本样式）
    md_body = markdown.markdown(md_text, extensions=["tables", "fenced_code", "codehilite"])

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<style>
  body {{ font-family: "Microsoft YaHei", "Segoe UI", sans-serif; font-size: 12pt; line-height: 1.8; max-width: 780px; margin: 0 auto; padding: 20px; color: #222; }}
  pre {{ background: #f4f4f4; padding: 12px; border-radius: 4px; overflow-x: auto; }}
  code {{ background: #f4f4f4; padding: 2px 6px; border-radius: 3px; font-family: "Consolas", "Courier New", monospace; }}
  table {{ border-collapse: collapse; width: 100%; }}
  th, td {{ border: 1px solid #ccc; padding: 8px; text-align: left; }}
  th {{ background: #eee; }}
  img {{ max-width: 100%; }}
</style>
</head>
<body>
{md_body}
</body>
</html>"""

    _convert_html_to_pdf(html, output_pdf, is_content=True)
    logger.info(f"Markdown → PDF 完成: {output_pdf}")


def _convert_office_to_pdf(file_path: str, output_pdf: str) -> None:
    """
    Office 文档 → PDF（多引擎智能降级）。

    Word 文档 (.doc/.docx):
      1. Microsoft Word COM  →  最佳布局保真度
      2. WPS Office COM      →  中文排版兼容性好
      3. LibreOffice 无头模式 →  兜底

    其他 Office 格式 (.xls/.xlsx/.ppt/.pptx):
      → LibreOffice 无头模式
    """
    ext = os.path.splitext(file_path)[1].lower()

    # Word 文档：多引擎降级
    if ext in (".doc", ".docx"):
        # ── 引擎 1: Microsoft Word ──
        if _detect_word():
            try:
                logger.info(f"引擎选择: Microsoft Word COM ({os.path.basename(file_path)})")
                _convert_via_word_com(file_path, output_pdf)
                return
            except Exception as e:
                logger.warning(f"Microsoft Word 转换失败，降级到下一引擎: {e}")

        # ── 引擎 2: WPS Office ──
        if _detect_wps():
            try:
                logger.info(f"引擎选择: WPS Office COM ({os.path.basename(file_path)})")
                _convert_via_wps_com(file_path, output_pdf)
                return
            except Exception as e:
                logger.warning(f"WPS Office 转换失败，降级到下一引擎: {e}")

        # ── 引擎 3: LibreOffice ──
        logger.info(f"引擎选择: LibreOffice (降级) ({os.path.basename(file_path)})")

    # 其他 Office 格式 / Word 文档最终降级
    _convert_office_via_libreoffice(file_path, output_pdf)


def _convert_office_via_libreoffice(file_path: str, output_pdf: str) -> None:
    """
    Office 文档 → PDF（LibreOffice 无头模式）。
    支持 .doc / .docx / .xls / .xlsx / .ppt / .pptx
    """
    libreoffice = _find_libreoffice()
    if not libreoffice:
        raise RuntimeError(
            "未找到 LibreOffice。请安装 LibreOffice 来转换 Office 文档。\n"
            "下载地址: https://www.libreoffice.org/download/"
        )

    output_dir = os.path.dirname(output_pdf) or tempfile.gettempdir()
    abs_input = os.path.abspath(file_path)
    abs_output_dir = os.path.abspath(output_dir)

    cmd = [
        libreoffice,
        "--headless",
        "--convert-to", "pdf",
        "--outdir", abs_output_dir,
        abs_input,
    ]

    logger.info(f"执行 LibreOffice 转换: {' '.join(cmd)}")

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("LibreOffice 转换超时（60 秒）")
    except FileNotFoundError:
        raise RuntimeError(f"LibreOffice 可执行文件未找到: {libreoffice}")

    if result.returncode != 0:
        raise RuntimeError(f"LibreOffice 转换失败 (rc={result.returncode}): {result.stderr}")

    # LibreOffice 输出的 PDF 文件名 = 原始文件名（改扩展名 .pdf）
    base_name = os.path.splitext(os.path.basename(file_path))[0]
    generated_pdf = os.path.join(abs_output_dir, base_name + ".pdf")

    if not os.path.isfile(generated_pdf):
        raise RuntimeError(f"LibreOffice 未生成预期的 PDF: {generated_pdf}")

    # 如果输出路径与生成路径不同，移动/重命名
    if os.path.abspath(generated_pdf) != os.path.abspath(output_pdf):
        if os.path.exists(output_pdf):
            os.remove(output_pdf)
        shutil.move(generated_pdf, output_pdf)

    logger.info(f"LibreOffice → PDF 完成: {output_pdf}")


# ============================================================
# 字体工具
# ============================================================

_chinese_font_registered = False
_chinese_font_name = "Helvetica"


def _register_chinese_font() -> None:
    """注册系统中可用的中文字体到 reportlab。"""
    global _chinese_font_registered, _chinese_font_name
    if _chinese_font_registered:
        return

    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    system = platform.system()

    # 字体候选列表：(字体名, 路径列表)
    candidates: list[tuple[str, list[str]]] = []
    if system == "Windows":
        candidates = [
            ("Microsoft YaHei", [
                os.path.expandvars(r"%SystemRoot%\Fonts\msyh.ttc"),
                os.path.expandvars(r"%SystemRoot%\Fonts\msyh.ttf"),
            ]),
            ("SimSun", [
                os.path.expandvars(r"%SystemRoot%\Fonts\simsun.ttc"),
                os.path.expandvars(r"%SystemRoot%\Fonts\simsun.ttf"),
            ]),
            ("SimHei", [
                os.path.expandvars(r"%SystemRoot%\Fonts\simhei.ttf"),
            ]),
        ]
    elif system == "Darwin":
        candidates = [
            ("PingFang SC", [
                "/System/Library/Fonts/PingFang.ttc",
                "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
            ]),
        ]
    else:
        candidates = [
            ("Noto Sans CJK SC", [
                "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
                "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
            ]),
            ("WenQuanYi Micro Hei", [
                "/usr/share/fonts/wenquanyi/wqy-microhei/wqy-microhei.ttc",
            ]),
            ("Droid Sans Fallback", [
                "/usr/share/fonts/droid/DroidSansFallbackFull.ttf",
            ]),
        ]

    for font_name, paths in candidates:
        for path in paths:
            if os.path.isfile(path):
                try:
                    pdfmetrics.registerFont(TTFont(font_name, path))
                    _chinese_font_name = font_name
                    _chinese_font_registered = True
                    logger.info(f"已注册中文字体: {font_name} ({path})")
                    return
                except Exception as e:
                    logger.warning(f"注册字体失败 {font_name} ({path}): {e}")
                    continue

    # 回退
    logger.warning("未找到任何可用中文字体，将使用 Helvetica（中文可能无法正常显示）")
    _chinese_font_registered = True


def _get_chinese_font_name() -> str:
    return _chinese_font_name


def _wrap_text_line(text: str, font_name: str, font_size: int, max_width: float,
                    canvas_obj) -> list[str]:
    """将单行文本按宽度自动换行，返回分行列表。"""
    result: list[str] = []
    current = ""
    for ch in text:
        test = current + ch
        w = canvas_obj.stringWidth(test, font_name, font_size)
        if w > max_width and current:
            result.append(current)
            current = ch
        else:
            current = test
    if current:
        result.append(current)
    return result if result else [""]


# ============================================================
# UniversalConverter
# ============================================================

class UniversalConverter:
    """
    通用文件转 PDF 转换器。
    根据文件扩展名自动选择转换策略。
    """

    # 扩展名 → 转换函数 映射
    CONVERSION_MAP: dict[str, Callable[[str, str], None]] = {}

    def __init__(self) -> None:
        self._init_conversion_map()

    def _init_conversion_map(self) -> None:
        """初始化扩展名→转换函数映射。"""
        cm = self.CONVERSION_MAP
        # TXT / 文本类
        cm[".txt"] = _convert_txt_to_pdf
        cm[".csv"] = _convert_csv_to_pdf
        # 图片类
        for ext in (".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp"):
            cm[ext] = _convert_image_to_pdf
        # Office 文档
        for ext in (".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx"):
            cm[ext] = _convert_office_to_pdf
        # Markdown
        cm[".md"] = _convert_markdown_to_pdf
        # HTML
        cm[".html"] = _convert_html_to_pdf
        cm[".htm"] = _convert_html_to_pdf

    def convert(self, file_path: str, output_pdf: str | None = None) -> str:
        """
        将文件转换为 PDF。

        Args:
            file_path: 源文件路径（含扩展名）
            output_pdf: 输出 PDF 路径；若为 None 则自动生成临时文件

        Returns:
            生成的 PDF 文件路径

        Raises:
            ValueError: 不支持的文件类型
            FileNotFoundError: 源文件不存在
            RuntimeError: 转换失败
        """
        if not os.path.isfile(file_path):
            raise FileNotFoundError(f"文件不存在: {file_path}")

        ext = os.path.splitext(file_path)[1].lower()

        # PDF 无需转换
        if ext == ".pdf":
            if output_pdf and os.path.abspath(file_path) != os.path.abspath(output_pdf):
                shutil.copy2(file_path, output_pdf)
                return output_pdf
            return file_path

        convert_func = self.CONVERSION_MAP.get(ext)
        if convert_func is None:
            raise ValueError(f"不支持的文件类型: {ext}  (文件: {file_path})")

        created_temp = False
        if output_pdf is None:
            fd, output_pdf = tempfile.mkstemp(suffix=".pdf", prefix="_conv_")
            os.close(fd)
            created_temp = True

        try:
            logger.info(f"开始转换: {os.path.basename(file_path)} ({ext}) → PDF")
            convert_func(file_path, output_pdf)

            if not os.path.isfile(output_pdf) or os.path.getsize(output_pdf) == 0:
                raise RuntimeError(f"转换为 PDF 后文件为空或不存在: {output_pdf}")

            return output_pdf
        except Exception:
            if created_temp and output_pdf and os.path.isfile(output_pdf):
                try:
                    os.remove(output_pdf)
                    logger.debug(f"已清理失败的临时 PDF: {output_pdf}")
                except OSError:
                    pass
            raise


# 模块级单例
_converter_instance: UniversalConverter | None = None


def get_converter() -> UniversalConverter:
    """获取 UniversalConverter 单例。"""
    global _converter_instance
    if _converter_instance is None:
        _converter_instance = UniversalConverter()
    return _converter_instance
