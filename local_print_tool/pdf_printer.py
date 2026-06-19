"""
pdf_printer.py — 静默打印 PDF 模块 (Windows)
优先使用 Windows 原生 GDI API 打印，SumatraPDF 降级。
支持双面、页码范围参数。
"""

from __future__ import annotations

import logging
import os
import platform
import subprocess
import sys
from typing import Optional

logger = logging.getLogger(__name__)


# ============================================================
# PDF 信息读取
# ============================================================

def get_image_info(image_path: str) -> dict:
    """
    读取图片尺寸信息，返回页数和方向。
    单张图片始终为 1 页，方向由宽高比决定。
    """
    try:
        from PIL import Image
        img = Image.open(image_path)
        w, h = img.size
        orientation = "landscape" if w > h else "portrait"
        return {"page_count": 1, "orientation": orientation}
    except Exception as e:
        logger.warning(f"读取图片信息失败 ({image_path}): {e}")
        return {"page_count": 0, "orientation": ""}


def get_docx_orientation(docx_path: str) -> str:
    """读取 .docx 页面方向，失败返回空字符串。"""
    try:
        from docx import Document
        doc = Document(docx_path)
        for section in doc.sections:
            w = section.page_width  # EMU
            h = section.page_height
            if w and h and w > h:
                return "landscape"
        return "portrait"
    except ImportError:
        return ""
    except Exception as e:
        logger.warning(f"读取 docx 方向失败 ({docx_path}): {e}")
        return ""


def count_pdf_pages(pdf_path: str) -> int:
    """统计 PDF 文件页数，失败返回 0。"""
    return get_pdf_info(pdf_path).get("page_count", 0)


def get_pdf_info(pdf_path: str) -> dict:
    """
    获取 PDF 信息：页数 + 页面方向。

    Returns:
        {
            "page_count": int,       # 总页数，0 = 失败
            "orientation": str,      # "portrait" | "landscape" | "mixed" | "unknown"
        }
    """
    try:
        from PyPDF2 import PdfReader
        reader = PdfReader(pdf_path)
        page_count = len(reader.pages)
        if page_count == 0:
            return {"page_count": 0, "orientation": "unknown"}

        has_portrait = False
        has_landscape = False

        for page in reader.pages:
            mb = page.mediabox
            if mb is None:
                continue
            w = float(mb.width)
            h = float(mb.height)
            if w > h:
                has_landscape = True
            else:
                has_portrait = True

        if has_landscape and has_portrait:
            orientation = "mixed"
        elif has_landscape:
            orientation = "landscape"
        else:
            orientation = "portrait"

        return {"page_count": page_count, "orientation": orientation}

    except ImportError:
        logger.warning("PyPDF2 未安装，无法读取 PDF 信息")
        return {"page_count": 0, "orientation": "unknown"}
    except Exception as e:
        logger.warning(f"读取 PDF 信息失败 ({pdf_path}): {e}")
        return {"page_count": 0, "orientation": "unknown"}


# ============================================================
# 系统打印机列表
# ============================================================

def list_system_printers() -> list[str]:
    """
    枚举系统中所有可用的打印机名称。
    返回打印机名称列表，失败时返回空列表。
    """
    printers: list[str] = []

    try:
        import win32print
        flags = win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
        for info in win32print.EnumPrinters(flags, None, 1):
            name = info[2]
            if name:
                printers.append(name)
    except ImportError:
        logger.warning("pywin32 未安装，无法枚举打印机列表")
    except Exception as e:
        logger.warning(f"枚举打印机列表失败: {e}")

    return printers


# ============================================================
# 辅助函数
# ============================================================

def _parse_page_range(page_range: str, total_pages: int) -> list[int]:
    """
    解析用户输入的页码范围，返回 0-based 页码列表。
    支持智能拆分，如 "23-4" → 页码 2 + 范围 3-4。

    输入示例: "1-5", "1,3,5-7", "1-5、7、9"
    """
    if not page_range or not page_range.strip():
        return list(range(total_pages))

    raw = page_range.strip()
    raw = raw.replace("、", ",").replace("，", ",").replace("；", ",").replace(" ", "")

    pages: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            try:
                a, b = part.split("-", 1)
                start, end = int(a), int(b)
                if start < end:
                    for p in range(start, end + 1):
                        if 1 <= p <= total_pages:
                            pages.add(p - 1)
                elif start > end and len(a) > 1:
                    # 智能拆分: "23-4" → 页码 2 + 范围 3-4
                    prefix = int(a[:-1])
                    last = int(a[-1])
                    if prefix < end:
                        for p in range(last, end + 1):
                            if 1 <= p <= total_pages:
                                pages.add(p - 1)
                        if 1 <= prefix <= total_pages:
                            pages.add(prefix - 1)
            except ValueError:
                continue
        else:
            try:
                p = int(part)
                if 1 <= p <= total_pages:
                    pages.add(p - 1)
            except ValueError:
                continue

    if not pages:
        return list(range(total_pages))
    return sorted(pages)


# ============================================================
# 打印函数
# ============================================================

def print_pdf(
    pdf_path: str,
    printer_name: str = "",
    copies: int = 1,
    duplex: str = "on",
    duplex_mode: str = "long-edge",
    page_range: str = "",
) -> tuple[bool, str]:
    """
    静默打印 PDF 文件。

    Args:
        pdf_path: PDF 文件路径
        printer_name: 目标打印机名称（空字符串 = 系统默认打印机）
        copies: 打印份数
        duplex: 'on' 开启双面, 'off' 关闭双面
        duplex_mode: 双面模式 'long-edge' | 'short-edge'
        page_range: 页码范围，如 "1-5"

    Returns:
        (success, message)
    """
    if not os.path.isfile(pdf_path):
        return False, f"PDF 文件不存在: {pdf_path}"

    system = platform.system()

    if system == "Windows":
        # 方案 1: Windows 原生 GDI 打印（DEVMODE.Copies 一次设置）
        ok, msg = _print_pdf_native(pdf_path, printer_name, duplex, duplex_mode, page_range, copies)
        if ok:
            return True, msg

        # 方案 2: SumatraPDF 降级（-print-settings copies=N）
        sumatra = _find_sumatra_pdf()
        if sumatra:
            logger.info("原生打印失败，降级为 SumatraPDF")
            return _print_via_sumatra(sumatra, pdf_path, printer_name, duplex, duplex_mode, page_range, copies)

        # 方案 3: 应用层循环打印（最可靠兜底）
        logger.warning("原生和 SumatraPDF 均不可用，使用循环打印")
        return _print_via_loop(pdf_path, printer_name, duplex, duplex_mode, page_range, copies)
    else:
        return _print_pdf_fallback(pdf_path, printer_name)


def _print_pdf_native(
    pdf_path: str,
    printer_name: str,
    duplex: str,
    duplex_mode: str,
    page_range: str,
    copies: int = 1,
) -> tuple[bool, str]:
    """
    Windows 原生 GDI 打印：PyMuPDF 渲染 + win32ui 输出到打印机。
    份数通过 DEVMODE.dmCopies 设置，文档只传输一次。
    """
    import fitz

    try:
        import win32print
        import win32ui
        import win32con
    except ImportError:
        return False, "pywin32 未安装"

    doc = None
    hdc = None
    started = False

    try:
        doc = fitz.open(pdf_path)
        total_pages = len(doc)
        if total_pages == 0:
            return False, "PDF 无页面"

        pages_to_print = _parse_page_range(page_range, total_pages)
        printer = printer_name or win32print.GetDefaultPrinter()

        # -- 创建打印机 DC --
        hdc = win32ui.CreateDC()
        hdc.CreatePrinterDC(printer)

        # 获取打印机可打印区域和分辨率
        dpi_x = hdc.GetDeviceCaps(win32con.LOGPIXELSX)
        dpi_y = hdc.GetDeviceCaps(win32con.LOGPIXELSY)
        printable_w = hdc.GetDeviceCaps(win32con.HORZRES)
        printable_h = hdc.GetDeviceCaps(win32con.VERTRES)

        # -- 设置双面模式（在 StartDoc 之前）--
        _set_printer_settings(printer, duplex, duplex_mode)

        title = os.path.basename(pdf_path)
        hdc.StartDoc(title)
        started = True

        from PIL import Image, ImageWin

        for copy_idx in range(copies):
            for page_idx in pages_to_print:
                hdc.StartPage()

                page = doc[page_idx]
                mat = fitz.Matrix(dpi_x / 72.0, dpi_y / 72.0)
                pix = page.get_pixmap(matrix=mat, alpha=False)

                img = Image.frombuffer(
                    "RGB", (pix.width, pix.height),
                    pix.samples, "raw", "RGB", pix.stride, 1,
                )

                x = (printable_w - pix.width) // 2
                y = (printable_h - pix.height) // 2

                dib = ImageWin.Dib(img)
                dib.draw(hdc.GetHandleOutput(), (x, y, x + pix.width, y + pix.height))

                hdc.EndPage()

        hdc.EndDoc()
        started = False
        total_pages_printed = len(pages_to_print) * copies
        return True, f"打印成功 (GDI, {total_pages_printed} 页, {copies} 份)"

    except ImportError as e:
        return False, f"缺少依赖: {e}"
    except Exception as e:
        if started and hdc:
            try:
                hdc.AbortDoc()
            except Exception:
                pass
        logger.warning(f"原生 GDI 打印失败: {e}")
        return False, str(e)
    finally:
        if doc:
            try:
                doc.close()
            except Exception:
                pass
        if hdc:
            try:
                hdc.DeleteDC()
            except Exception:
                pass


def _set_printer_settings(printer_name: str, duplex: str, duplex_mode: str) -> None:
    """通过 DEVMODE 设置打印机双面模式。"""
    try:
        import win32print
        import win32con

        handle = win32print.OpenPrinter(printer_name)
        try:
            devmode = win32print.GetPrinter(handle, 2)["pDevMode"]
            if duplex == "on":
                if duplex_mode == "short-edge":
                    devmode.Duplex = 2   # DMDUP_HORIZONTAL
                else:
                    devmode.Duplex = 3   # DMDUP_VERTICAL (长边翻转)
            else:
                devmode.Duplex = 1       # DMDUP_SIMPLEX

            win32print.DocumentProperties(
                0, handle, printer_name,
                devmode, devmode,
                win32con.DM_IN_BUFFER | win32con.DM_OUT_BUFFER,
            )
        finally:
            win32print.ClosePrinter(handle)
    except Exception as e:
        logger.warning(f"设置打印机参数失败: {e}")


# ============================================================
# 降级方案：SumatraPDF
# ============================================================

def _find_sumatra_pdf() -> str | None:
    """查找 SumatraPDF.exe 的安装路径。"""
    system = platform.system()
    if system != "Windows":
        return None

    candidates = [
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "SumatraPDF.exe"),
        os.path.join(os.path.dirname(sys.executable), "SumatraPDF.exe"),
        os.path.expandvars(r"%LOCALAPPDATA%\SumatraPDF\SumatraPDF.exe"),
        os.path.expandvars(r"%APPDATA%\SumatraPDF\SumatraPDF.exe"),
        r"C:\Program Files\SumatraPDF\SumatraPDF.exe",
        r"C:\Program Files (x86)\SumatraPDF\SumatraPDF.exe",
    ]

    for p in candidates:
        if os.path.isfile(p):
            return p

    import shutil
    return shutil.which("SumatraPDF.exe")


def _print_via_sumatra(
    sumatra_path: str,
    pdf_path: str,
    printer_name: str,
    duplex: str,
    duplex_mode: str,
    page_range: str,
    copies: int = 1,
) -> tuple[bool, str]:
    """使用 SumatraPDF 命令行进行静默打印。"""
    settings_parts: list[str] = []

    if copies > 1:
        settings_parts.append(f"copies={copies}")

    if duplex == "on":
        if duplex_mode == "short-edge":
            settings_parts.append("duplex=short")
        else:
            settings_parts.append("duplex=long")
    else:
        settings_parts.append("duplex=simplex")

    if page_range:
        parsed = page_range.strip().replace("、", ",").replace("，", ",").replace(" ", "")
        if parsed:
            settings_parts.append(f"range={parsed}")

    cmd = [sumatra_path, "-print-to", printer_name or "default"]

    if settings_parts:
        cmd += ["-print-settings", ",".join(settings_parts)]

    cmd.append(os.path.abspath(pdf_path))

    logger.info(f"SumatraPDF 打印: {' '.join(cmd)}")

    try:
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = subprocess.SW_HIDE

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
            startupinfo=si,
        )
        if result.returncode != 0:
            return False, f"SumatraPDF 失败 (rc={result.returncode})"
        return True, "打印成功 (SumatraPDF)"
    except subprocess.TimeoutExpired:
        return False, "SumatraPDF 超时"
    except FileNotFoundError:
        return False, "SumatraPDF 未找到"
    except Exception as e:
        return False, f"SumatraPDF 异常: {e}"


def _print_via_loop(
    pdf_path: str,
    printer_name: str,
    duplex: str,
    duplex_mode: str,
    page_range: str,
    copies: int = 1,
) -> tuple[bool, str]:
    """终极兜底：应用层循环打印，每次只打 1 份。成功率最高。"""
    for i in range(copies):
        ok, msg = _print_pdf_native(pdf_path, printer_name, duplex, duplex_mode, page_range, 1)
        if not ok:
            return False, f"第 {i + 1}/{copies} 份打印失败: {msg}"
    return True, f"循环打印完成 ({copies} 份)"


def _print_via_shell_execute(pdf_path: str, printer_name: str) -> tuple[bool, str]:
    """降级方案：ShellExecute — 可能弹出打印对话框。"""
    try:
        import win32api
        win32api.ShellExecute(
            0, "print",
            os.path.abspath(pdf_path),
            f'"{printer_name}"' if printer_name else "",
            ".", 0,
        )
        return True, "打印任务已发送 (ShellExecute)"
    except ImportError:
        return False, "缺少 pywin32"
    except Exception as e:
        return False, f"ShellExecute 失败: {e}"


def _print_pdf_fallback(pdf_path: str, printer_name: str) -> tuple[bool, str]:
    """非 Windows 平台降级打印（lp 命令）。"""
    try:
        if printer_name:
            cmd = ["lp", "-d", printer_name, os.path.abspath(pdf_path)]
        else:
            cmd = ["lp", os.path.abspath(pdf_path)]
        result = subprocess.run(cmd, capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=60)
        if result.returncode == 0:
            return True, "已通过 lp 命令发送到打印机"
        else:
            return False, f"lp 命令失败: {result.stderr}"
    except FileNotFoundError:
        return False, "未找到 lp 命令"
    except Exception as e:
        return False, f"打印异常: {e}"
