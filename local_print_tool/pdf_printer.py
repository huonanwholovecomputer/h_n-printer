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

    输入示例: "1-5", "1,3,5-7", "1-5、7、9"
    """
    if not page_range or not page_range.strip():
        return list(range(total_pages))

    raw = page_range.strip()
    raw = raw.replace("、", ",").replace("，", ",").replace("；", ",").replace(" ", "")

    pages: set[int] = set()
    skipped: list[str] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            try:
                a, b = part.split("-", 1)
                start, end = int(a), int(b)
                if 1 <= start < end:
                    for p in range(start, end + 1):
                        if 1 <= p <= total_pages:
                            pages.add(p - 1)
                else:
                    skipped.append(part)  # start >= end 视为格式错误
            except ValueError:
                skipped.append(part)
        else:
            try:
                p = int(part)
                if 1 <= p <= total_pages:
                    pages.add(p - 1)
                else:
                    skipped.append(part)
            except ValueError:
                skipped.append(part)

    if skipped:
        logger.warning(f"页码范围包含无效/超限部分（已忽略）: {', '.join(skipped)} (总页数={total_pages})")
        print(f"[打印] ⚠ 页码范围无效/超限部分已忽略: {', '.join(skipped)}")

    if not pages:
        logger.warning(f"页码范围解析后无有效页，退回全打印 (输入: '{page_range}', 总页数={total_pages})")
        print(f"[打印] ⚠ 页码范围解析后无有效页，将打印全部 {total_pages} 页")
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

    # ── 打印参数总览 ──
    duplex_label = f"{'双面' if duplex == 'on' else '单面'}"
    if duplex == "on":
        duplex_label += f"({ '短边翻转' if duplex_mode == 'short-edge' else '长边翻转' })"
    print(f"[打印] 文件: {os.path.basename(pdf_path)}")
    print(f"[打印] 参数: {duplex_label} | {copies} 份 | 页码: '{page_range or '全部'}'")
    logger.info(f"开始打印: pdf={pdf_path}, printer={printer_name or '(默认)'}, "
                f"duplex={duplex}/{duplex_mode}, copies={copies}, pages='{page_range}'")

    if system == "Windows":
        # 方案 1: Windows 原生 GDI 打印
        print("[打印] 方案1: 尝试 Windows 原生 GDI 打印...")
        ok, msg = _print_pdf_native(pdf_path, printer_name, duplex, duplex_mode, page_range, copies)
        if ok:
            print(f"[打印] ✓ 成功 ({msg})")
            return True, msg
        print(f"[打印] ✗ 方案1 失败: {msg}")

        # 方案 2: SumatraPDF 降级
        sumatra = _find_sumatra_pdf()
        if sumatra:
            print(f"[打印] 方案2: 降级为 SumatraPDF ({sumatra})")
            logger.info(f"原生 GDI 失败，降级 SumatraPDF: {sumatra}")
            ok, msg = _print_via_sumatra(sumatra, pdf_path, printer_name, duplex, duplex_mode, page_range, copies)
            print(f"[打印] {'✓ 成功' if ok else '✗ 失败'} ({msg})")
            return ok, msg

        # 方案 3: 应用层循环打印（最可靠兜底）
        print("[打印] 方案3: 应用层循环打印（兜底）...")
        logger.warning("原生和 SumatraPDF 均不可用，使用循环打印")
        ok, msg = _print_via_loop(pdf_path, printer_name, duplex, duplex_mode, page_range, copies)
        print(f"[打印] {'✓ 成功' if ok else '✗ 失败'} ({msg})")
        return ok, msg
    else:
        print(f"[打印] 非 Windows 平台，使用 lp 命令...")
        ok, msg = _print_pdf_fallback(pdf_path, printer_name)
        print(f"[打印] {'✓ 成功' if ok else '✗ 失败'} ({msg})")
        return ok, msg


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

        logger.info(f"GDI 打印: 打印机={printer}, 总页数={total_pages}, "
                    f"待打页数={len(pages_to_print)}, 份数={copies}")
        print(f"[GDI] 目标打印机: {printer}")
        print(f"[GDI] PDF 共 {total_pages} 页，本次打印 {len(pages_to_print)} 页 × {copies} 份 = {len(pages_to_print) * copies} 面")

        # -- 获取配置好双面的 DEVMODE --
        print(f"[GDI] 获取打印机 DEVMODE...")
        devmode = _get_printer_devmode(printer, duplex, duplex_mode)

        # -- 创建打印机 DC（有 DEVMODE 则通过 win32gui.ResetDC 应用）--
        hdc = win32ui.CreateDC()
        hdc.CreatePrinterDC(printer)

        if devmode is not None:
            try:
                import win32gui
                hdc_handle = hdc.GetHandleOutput()
                result = win32gui.ResetDC(hdc_handle, devmode)
                if result:
                    duplex_desc = {"on": {"long-edge": "长边翻转", "short-edge": "短边翻转"},
                                   "off": {"long-edge": "单面", "short-edge": "单面"}}
                    desc = duplex_desc.get(duplex, {}).get(duplex_mode, f"duplex={duplex}")
                    print(f"[GDI] ✓ DEVMODE 已应用 (win32gui.ResetDC): 双面模式={desc}")
                    logger.info(f"GDI: win32gui.ResetDC 成功, duplex={duplex}/{duplex_mode}")
                else:
                    print(f"[GDI] ⚠ win32gui.ResetDC 返回 0，双面设置可能未生效")
                    logger.warning("GDI: win32gui.ResetDC 返回 0")
            except Exception as e:
                print(f"[GDI] ⚠ win32gui.ResetDC 失败（使用默认设置）: {e}")
                logger.warning(f"GDI: win32gui.ResetDC 失败: {e}")
        else:
            print(f"[GDI] ⚠ 未能获取 DEVMODE，使用打印机默认设置")
            logger.warning("GDI: 未能获取 DEVMODE，双面设置可能不生效")

        # 获取打印机可打印区域和分辨率
        dpi_x = hdc.GetDeviceCaps(win32con.LOGPIXELSX)
        dpi_y = hdc.GetDeviceCaps(win32con.LOGPIXELSY)
        printable_w = hdc.GetDeviceCaps(win32con.HORZRES)
        printable_h = hdc.GetDeviceCaps(win32con.VERTRES)
        logger.info(f"GDI: DPI=({dpi_x},{dpi_y}), 可打印区域=({printable_w},{printable_h})")
        print(f"[GDI] 分辨率: {dpi_x}×{dpi_y} DPI, 可打印区域: {printable_w}×{printable_h}")

        title = os.path.basename(pdf_path)
        hdc.StartDoc(title)
        started = True
        print(f"[GDI] StartDoc 完成，开始逐页渲染...")

        from PIL import Image, ImageWin

        page_seq = 0
        total_sheets = len(pages_to_print) * copies
        for copy_idx in range(copies):
            for page_idx in pages_to_print:
                page_seq += 1
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
                print(f"[GDI]   ✓ 第 {page_seq}/{total_sheets} 面 (PDF p.{page_idx + 1}, 第 {copy_idx + 1} 份)")

        hdc.EndDoc()
        started = False
        total_pages_printed = len(pages_to_print) * copies
        msg = f"打印成功 (GDI, {total_pages_printed} 面, {copies} 份)"
        logger.info(f"GDI 打印完成: {msg}")
        return True, msg

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


def _get_printer_devmode(printer_name: str, duplex: str, duplex_mode: str):
    """
    获取并配置打印机的 DEVMODE 结构。

    Returns:
        配置好的 PyDEVMODE 对象，失败返回 None。
    """
    try:
        import win32print
        import win32con

        duplex_map = {1: "单面 (DMDUP_SIMPLEX)", 2: "长边翻转 (DMDUP_VERTICAL)", 3: "短边翻转 (DMDUP_HORIZONTAL)"}

        handle = win32print.OpenPrinter(printer_name)
        try:
            devmode = win32print.GetPrinter(handle, 2)["pDevMode"]
            old_duplex = devmode.Duplex
            if duplex == "on":
                if duplex_mode == "short-edge":
                    devmode.Duplex = 3   # DMDUP_HORIZONTAL (短边翻转)
                else:
                    devmode.Duplex = 2   # DMDUP_VERTICAL (长边翻转)
            else:
                devmode.Duplex = 1       # DMDUP_SIMPLEX

            new_duplex = devmode.Duplex
            print(f"[DEVMODE] 打印机: {printer_name}")
            print(f"[DEVMODE] 双面设置: {duplex_map.get(old_duplex, f'未知({old_duplex})')} → {duplex_map.get(new_duplex, f'未知({new_duplex})')}")
            logger.info(f"DEVMODE: OpenPrinter={printer_name}, Duplex {old_duplex}→{new_duplex}")

            # DocumentProperties 验证/合并 DEVMODE
            result = win32print.DocumentProperties(
                0, handle, printer_name,
                devmode, devmode,
                win32con.DM_IN_BUFFER | win32con.DM_OUT_BUFFER,
            )
            logger.info(f"DEVMODE: DocumentProperties 返回 {result} (正数=成功)")
            if result <= 0:
                print(f"[DEVMODE] ⚠ DocumentProperties 验证返回 {result}（可能无效）")
            else:
                print(f"[DEVMODE] ✓ DocumentProperties 验证通过")

            return devmode
        finally:
            win32print.ClosePrinter(handle)
    except Exception as e:
        print(f"[DEVMODE] ✗ 失败: {e}")
        logger.warning(f"获取/配置打印机 DEVMODE 失败: {e}")
        return None


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

    print(f"[SumatraPDF] 命令: {' '.join(cmd)}")
    logger.info(f"SumatraPDF 执行: {' '.join(cmd)}")

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
        print(f"[SumatraPDF] 退出码: {result.returncode}")
        if result.stdout.strip():
            logger.info(f"SumatraPDF stdout: {result.stdout.strip()[:200]}")
        if result.stderr.strip():
            logger.warning(f"SumatraPDF stderr: {result.stderr.strip()[:200]}")
            print(f"[SumatraPDF] stderr: {result.stderr.strip()[:200]}")

        if result.returncode != 0:
            logger.warning(f"SumatraPDF 返回非零: rc={result.returncode}, stderr={result.stderr.strip()}")
            return False, f"SumatraPDF 失败 (rc={result.returncode}): {result.stderr.strip()[:100]}"
        if result.stderr.strip():
            logger.warning("SumatraPDF 虽然 rc=0 但输出了 stderr 警告")
        return True, "打印成功 (SumatraPDF)"
    except subprocess.TimeoutExpired:
        print("[SumatraPDF] ✗ 超时 (120s)")
        return False, "SumatraPDF 超时"
    except FileNotFoundError:
        print("[SumatraPDF] ✗ 可执行文件未找到")
        return False, "SumatraPDF 未找到"
    except Exception as e:
        print(f"[SumatraPDF] ✗ 异常: {e}")
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
    print(f"[循环打印] 共 {copies} 份，每次 1 份...")
    for i in range(copies):
        print(f"[循环打印] 第 {i + 1}/{copies} 份...")
        ok, msg = _print_pdf_native(pdf_path, printer_name, duplex, duplex_mode, page_range, 1)
        if not ok:
            print(f"[循环打印] ✗ 第 {i + 1} 份失败: {msg}")
            return False, f"第 {i + 1}/{copies} 份打印失败: {msg}"
        print(f"[循环打印] ✓ 第 {i + 1} 份完成")
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
