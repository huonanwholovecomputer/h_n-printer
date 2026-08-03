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
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# P1-9: 标记 GDI 打印已中途出纸（StartPage 已执行）。带此前缀的失败消息不得再降级整单重打。
MID_PRINT_MARKER = "[已出纸] "


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
        logger.warning("PyPDF2 未安装，无法读取 PDF 信息，回退 fitz 统计页数")
        return _get_pdf_info_via_fitz(pdf_path)
    except Exception as e:
        # P2-13: PyPDF2 失败（如加密 PDF 页数读出 0）→ 回退 fitz 统计真实页数，进度条按真实页数
        logger.warning(f"PyPDF2 读取 PDF 信息失败 ({pdf_path}): {e}，回退 fitz 统计页数")
        return _get_pdf_info_via_fitz(pdf_path)


def _get_pdf_info_via_fitz(pdf_path: str) -> dict:
    """PyPDF2 失败（如加密 PDF）时，用 PyMuPDF(fitz) 回退统计页数。

    多数加密 PDF fitz 可直接打开读取，页数统计比 PyPDF2 更可靠。
    """
    try:
        import fitz
        doc = fitz.open(pdf_path)
        try:
            page_count = len(doc)
            has_portrait = False
            has_landscape = False
            for page in doc:
                r = page.rect
                if r.width > r.height:
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
        finally:
            doc.close()
    except Exception as e2:
        logger.warning(f"fitz 读取 PDF 信息也失败 ({pdf_path}): {e2}")
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

def check_page_range_truncation(page_range: str, total_pages: int) -> dict | None:
    """检测页码范围是否会被截断。

    Returns:
        None – 无需截断（空范围或全部有效）
        dict – {"original": str, "effective": str, "skipped": [str], "total": int}
    """
    if not page_range or not page_range.strip():
        return None
    if total_pages <= 0:
        return None

    raw = page_range.strip()
    raw = raw.replace("、", ",").replace("，", ",").replace("；", ",").replace(" ", "")

    skipped: list[str] = []
    valid_parts: list[str] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            try:
                a, b = part.split("-", 1)
                start, end = int(a), int(b)
                if 1 <= start < end:
                    if end > total_pages:
                        actual_end = total_pages
                        if actual_end > start:
                            valid_parts.append(f"{start}-{actual_end}")
                            skipped.append(part)
                        else:
                            skipped.append(part)
                    else:
                        valid_parts.append(part)
                else:
                    skipped.append(part)
            except ValueError:
                skipped.append(part)
        else:
            try:
                p = int(part)
                if 1 <= p <= total_pages:
                    valid_parts.append(part)
                else:
                    skipped.append(part)
            except ValueError:
                skipped.append(part)

    if not skipped:
        return None
    if not valid_parts:
        return None  # 全部无效 → 回退全打印，不标记截断

    return {
        "original": page_range.strip(),
        "effective": ",".join(valid_parts),
        "skipped": skipped,
        "total": total_pages,
    }


def _parse_page_range(page_range: str, total_pages: int) -> list[int]:
    """
    解析用户输入的页码范围，返回 0-based 页码列表。

    输入示例: "1-5", "1,3,5-7", "1-5、7、9"

    注意：拆分语义必须与 printer_config._parse_range_parts / _count_pages_in_range
    保持同步（否则计费与实打不一致）：
      - "23-4" 智能拆分为页码 {2, 3, 4}
      - 超长区间 "1-9999" 先收窄到 [1, total_pages] 再迭代，防 range() 冻结
      - 输入非空但全部无效 → 警告后仍打印全部（打印全部比不打印安全）
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
                if start < end:
                    # P1-3: 超长区间收窄后再迭代，避免 range() 冻结/耗尽内存
                    start = max(start, 1)
                    end = min(end, total_pages)
                    for p in range(start, end + 1):
                        if 1 <= p <= total_pages:
                            pages.add(p - 1)
                elif start > end and len(a) > 1:
                    # 智能拆分（与 printer_config 一致）: "23-4" → 页码 2 + 范围 3-4
                    prefix = int(a[:-1])
                    last = int(a[-1])
                    if prefix < end:
                        for p in range(last, end + 1):
                            if 1 <= p <= total_pages:
                                pages.add(p - 1)
                        if 1 <= prefix <= total_pages:
                            pages.add(prefix - 1)
                else:
                    skipped.append(part)  # start >= end 且无法智能拆分 → 格式错误
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
        # P2-1: 输入非空但全部无效 → 不再静默回退，明确警告后仍打印全部
        logger.warning(f"页码范围全部无效，退回全打印 (输入: '{page_range}', 总页数={total_pages})")
        print(f"[打印] ⚠ 页码范围解析后无有效页，将打印全部 {total_pages} 页")
        return list(range(total_pages))
    return sorted(pages)


def estimate_print_sides(
    total_pages: int,
    copies: int,
    duplex: str,
    page_range: str,
) -> int:
    """预估打印总面数，用于精确进度条。

    Args:
        total_pages: PDF 总页数
        copies: 打印份数
        duplex: 'on' | 'off'
        page_range: 页码范围，如 "1-5"

    Returns:
        预估总面数（含分隔页），最小为 1
    """
    if total_pages <= 0:
        return max(1, copies)
    pages = _parse_page_range(page_range, total_pages)
    n = len(pages)
    sides = n * copies
    if duplex == "on" and copies > 1 and n % 2 == 1:
        sides += copies - 1  # 每份间插入空白分隔页
    return max(1, sides)


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
    orientation: str = "",
    progress_callback: Callable[[int, int], None] | None = None,
    dpi: int = 0,
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
        orientation: 页面方向 "portrait" | "landscape" | "" (空=不强制)
        progress_callback: 进度回调 (current, total)，每打印一面调用一次
        dpi: 渲染 DPI，0=使用打印机原生 DPI

    Returns:
        (success, message)
    """
    if not os.path.isfile(pdf_path):
        return False, f"PDF 文件不存在: {pdf_path}"

    # P0-2: 统一页数校验 — 0 页 PDF 直接失败，不进入 GDI/Sumatra 任何分支（防空文件报成功漏打）
    total_pages = count_pdf_pages(pdf_path)
    if total_pages <= 0:
        return False, f"PDF 无有效页面（页数为 0，文件为空、损坏或无法解析）: {pdf_path}"

    system = platform.system()

    # ── 打印参数总览 ──
    duplex_label = f"{'双面' if duplex == 'on' else '单面'}"
    if duplex == "on":
        duplex_label += f"({ '短边翻转' if duplex_mode == 'short-edge' else '长边翻转' })"
    orient_label = {"portrait": "竖向", "landscape": "横向"}.get(orientation, "")
    print(f"[打印] 文件: {os.path.basename(pdf_path)}")
    print(f"[打印] 参数: {duplex_label} | {copies} 份 | 页码: '{page_range or '全部'}'" +
          (f" | 方向: {orient_label}" if orient_label else ""))
    logger.info(f"开始打印: pdf={pdf_path}, printer={printer_name or '(默认)'}, "
                f"duplex={duplex}/{duplex_mode}, copies={copies}, pages='{page_range}', orientation='{orientation}'")

    if system == "Windows":
        # 方案 1: Windows 原生 GDI 打印（驱动级双面/份数控制，最可靠）
        print("[打印] 方案1: 尝试 Windows 原生 GDI 打印...")
        ok, msg = _print_pdf_native(pdf_path, printer_name, duplex, duplex_mode, page_range, copies, orientation, progress_callback, dpi)
        if ok:
            print(f"[打印] ✓ 成功 ({msg})")
            return True, msg
        print(f"[打印] ✗ 方案1 失败: {msg}")

        # P1-9: GDI 中途失败（已开始出纸，部分页已打印）→ 不再降级 Sumatra/循环整单重打，避免重复页
        if msg.startswith(MID_PRINT_MARKER):
            reason = msg[len(MID_PRINT_MARKER):]
            logger.error(f"GDI 打印中途失败（部分页面已出纸），为防重复页不再降级整单重打: {reason}")
            print(f"[打印] ✗ GDI 打印中途失败（部分页面已出纸），为避免重复页不再降级整单重打: {reason}")
            return False, reason

        # 方案 2: SumatraPDF 降级
        sumatra = _find_sumatra_pdf()
        if sumatra:
            print(f"[打印] 方案2: 降级为 SumatraPDF ({sumatra})")
            logger.info(f"原生 GDI 失败，降级 SumatraPDF: {sumatra}")
            ok, msg = _print_via_sumatra(sumatra, pdf_path, printer_name, duplex, duplex_mode, page_range, copies, orientation, progress_callback)
            if ok:
                print(f"[打印] ✓ 成功 ({msg})")
                return True, msg
            print(f"[打印] ✗ SumatraPDF 失败: {msg}")
            logger.info(f"SumatraPDF 也失败: {msg}")

        # 方案 3: 应用层循环打印（最可靠兜底）
        print("[打印] 方案3: 应用层循环打印（兜底）...")
        ok, msg = _print_via_loop(pdf_path, printer_name, duplex, duplex_mode, page_range, copies, orientation, progress_callback, dpi)
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
    orientation: str = "",
    progress_callback: Callable[[int, int], None] | None = None,
    dpi: int = 0,
) -> tuple[bool, str]:
    """
    Windows 原生 GDI 打印：PyMuPDF 渲染 + win32ui 输出到打印机。
    progress_callback: 每完成一面的 EndPage 后调用 (current_side, total_sides)
    dpi: 渲染 DPI，0=使用打印机原生 DPI
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
    page_started = False  # P1-9: 是否已开始出纸（StartPage 已执行）

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
        # 预估总面数（如需要空白分隔页则后面再补）
        est_sep = (copies - 1) if (duplex == "on" and copies > 1 and len(pages_to_print) % 2 == 1) else 0
        est_sides = len(pages_to_print) * copies + est_sep
        if est_sep:
            print(f"[GDI] PDF 共 {total_pages} 页，本次打印 {len(pages_to_print)} 页 × {copies} 份 + {est_sep} 空白分隔页 = {est_sides} 面")
        else:
            print(f"[GDI] PDF 共 {total_pages} 页，本次打印 {len(pages_to_print)} 页 × {copies} 份 = {est_sides} 面")

        # -- 获取配置好双面和方向的 DEVMODE --
        print(f"[GDI] 获取打印机 DEVMODE...")
        devmode = _get_printer_devmode(printer, duplex, duplex_mode, orientation)

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
            # P1-8: 获取 DEVMODE 失败 — 请求了双面则明确报错（不再静默默认设置）；仅单面时用默认设置继续
            if duplex == "on":
                print(f"[GDI] ✗ 获取 DEVMODE 失败，且请求了双面打印 — 中止以防双面不生效")
                logger.error(f"GDI: 获取 DEVMODE 失败（duplex=on），中止打印: {printer}")
                return False, f"获取打印机 DEVMODE 失败，无法保证双面设置: {printer}"
            print(f"[GDI] ⚠ 未能获取 DEVMODE，使用打印机默认设置（单面打印不受影响）")
            logger.warning("GDI: 未能获取 DEVMODE，双面设置可能不生效")

        # 获取打印机可打印区域和分辨率
        native_dpi_x = hdc.GetDeviceCaps(win32con.LOGPIXELSX)
        native_dpi_y = hdc.GetDeviceCaps(win32con.LOGPIXELSY)
        # 使用指定 DPI 或打印机原生 DPI
        dpi_x = dpi if dpi > 0 else native_dpi_x
        dpi_y = dpi if dpi > 0 else native_dpi_y
        printable_w = hdc.GetDeviceCaps(win32con.HORZRES)
        printable_h = hdc.GetDeviceCaps(win32con.VERTRES)
        if dpi > 0:
            logger.info(f"GDI: DPI=({dpi_x},{dpi_y}) [指定], 原生=({native_dpi_x},{native_dpi_y}), 可打印区域=({printable_w},{printable_h})")
            print(f"[GDI] 分辨率: {dpi_x}×{dpi_y} DPI (指定, 原生{native_dpi_x}×{native_dpi_y}), 可打印区域: {printable_w}×{printable_h}")
        else:
            logger.info(f"GDI: DPI=({dpi_x},{dpi_y}), 可打印区域=({printable_w},{printable_h})")
            print(f"[GDI] 分辨率: {dpi_x}×{dpi_y} DPI, 可打印区域: {printable_w}×{printable_h}")

        title = os.path.basename(pdf_path)
        hdc.StartDoc(title)
        started = True
        print(f"[GDI] StartDoc 完成，开始逐页渲染...")

        from PIL import Image, ImageWin

        # 单数页 + 双面 + 多份 → 每份之间需插入空白页，确保副本独立
        needs_separator = duplex == "on" and copies > 1 and len(pages_to_print) % 2 == 1
        sep_count = (copies - 1) if needs_separator else 0
        total_sides = len(pages_to_print) * copies + sep_count
        if needs_separator:
            logger.info(f"GDI: 奇数页({len(pages_to_print)}页)双面多份打印，将在副本间插入空白分隔页")
            print(f"[GDI] ⚙ 奇数页({len(pages_to_print)}页) + 双面 + 多份({copies}份) → 副本间插入空白分隔页")

        page_seq = 0
        for copy_idx in range(copies):
            for page_idx in pages_to_print:
                page_seq += 1
                page = doc[page_idx]

                # P0-1: 渲染前估算像素总量 — 超 4 亿像素（约 32 位进程上限）自动降 DPI；
                #       仍超限则拒绝该页（在 StartPage 之前检查，避免浪费纸张并可降级）
                page_w_pts = page.rect.width
                page_h_pts = page.rect.height
                MAX_RENDER_PIXELS = 400_000_000
                render_dpi_x = dpi_x
                render_dpi_y = dpi_y
                pixel_count = (page_w_pts * render_dpi_x / 72.0) * (page_h_pts * render_dpi_y / 72.0)
                if pixel_count > MAX_RENDER_PIXELS:
                    reduced_dpi = max(1, int((MAX_RENDER_PIXELS / (page_w_pts * page_h_pts)) ** 0.5 * 72.0))
                    render_dpi_x = min(render_dpi_x, reduced_dpi)
                    render_dpi_y = min(render_dpi_y, reduced_dpi)
                    logger.warning(f"GDI: 页面过大 ({page_w_pts:.0f}x{page_h_pts:.0f}pt)，"
                                   f"像素量 {int(pixel_count):,} > {MAX_RENDER_PIXELS:,}，渲染 DPI 由 {dpi_x} 自动降为 {render_dpi_x}")
                    print(f"[GDI] ⚠ 页面过大，渲染 DPI 由 {dpi_x} 自动降为 {render_dpi_x}")
                    if (page_w_pts * render_dpi_x / 72.0) * (page_h_pts * render_dpi_y / 72.0) > MAX_RENDER_PIXELS:
                        raise RuntimeError(
                            f"PDF 页面尺寸极端巨大（{page_w_pts:.0f}×{page_h_pts:.0f}pt），"
                            f"即使降 DPI 至 {render_dpi_x} 像素量仍超 {MAX_RENDER_PIXELS:,}，已拒绝该页以防内存耗尽"
                        )

                hdc.StartPage()
                page_started = True

                mat = fitz.Matrix(render_dpi_x / 72.0, render_dpi_y / 72.0)
                pix = page.get_pixmap(matrix=mat, alpha=False)

                img = Image.frombuffer(
                    "RGB", (pix.width, pix.height),
                    pix.samples, "raw", "RGB", pix.stride, 1,
                )

                # ── 将渲染 DPI 的 pixmap 映射到打印机原生 DPI 的物理尺寸 ──
                # pix 尺寸基于 render_dpi（如400），但设备坐标基于 native_dpi（如600）
                # 必须按打印机原生 DPI 计算目标矩形，否则打印尺寸会偏小
                page_rect = page.rect  # PDF 点数 (1/72 inch)
                page_w_device = int(page_rect.width * native_dpi_x / 72.0)
                page_h_device = int(page_rect.height * native_dpi_y / 72.0)

                # P2-3: 超出可打印区域的页面（如 A5 纸型）先等比缩放到可打印区域内再居中
                scale = min(1.0, printable_w / max(1, page_w_device), printable_h / max(1, page_h_device))
                if scale < 1.0:
                    page_w_device = int(page_w_device * scale)
                    page_h_device = int(page_h_device * scale)

                x = (printable_w - page_w_device) // 2
                y = (printable_h - page_h_device) // 2

                dib = ImageWin.Dib(img)
                dib.draw(hdc.GetHandleOutput(),
                         (x, y, x + page_w_device, y + page_h_device))

                hdc.EndPage()
                print(f"[GDI]   ✓ 第 {page_seq}/{total_sides} 面 (PDF p.{page_idx + 1}, 第 {copy_idx + 1} 份)")
                if progress_callback:
                    progress_callback(page_seq, total_sides)

            # 单数页双面多份：每份结束后插入空白页（最后一份除外）
            if needs_separator and copy_idx < copies - 1:
                page_seq += 1
                hdc.StartPage()
                blank_img = Image.new("RGB", (printable_w, printable_h), "white")
                blank_dib = ImageWin.Dib(blank_img)
                blank_dib.draw(hdc.GetHandleOutput(), (0, 0, printable_w, printable_h))
                hdc.EndPage()
                print(f"[GDI]   ✓ 第 {page_seq}/{total_sides} 面 (副本分隔页)")
                if progress_callback:
                    progress_callback(page_seq, total_sides)

        hdc.EndDoc()
        started = False
        total_pages_printed = len(pages_to_print) * copies
        msg = f"打印成功 (GDI, {total_pages_printed} 面, {copies} 份)"
        if needs_separator:
            msg += f" [已插入 {sep_count} 张空白分隔页]"
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
        if page_started:
            # P1-9: 已开始出纸 → 标记中途失败，禁止上层降级整单重打（否则前 N 页已打印 + 整单重打 = 重复页）
            logger.error(f"原生 GDI 打印中途失败（已开始出纸）: {e}")
            return False, MID_PRINT_MARKER + str(e)
        logger.warning(f"原生 GDI 打印失败（未开始出纸，允许降级）: {e}")
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


def _get_printer_devmode(printer_name: str, duplex: str, duplex_mode: str, orientation: str = ""):
    """
    获取并配置打印机的 DEVMODE 结构。

    Args:
        printer_name: 打印机名称
        duplex: 'on' | 'off'
        duplex_mode: 'long-edge' | 'short-edge'
        orientation: 'portrait' | 'landscape' | '' (空=使用默认)

    Returns:
        配置好的 PyDEVMODE 对象，失败返回 None。
    """
    try:
        import win32print
        import win32con

        duplex_map = {1: "单面 (DMDUP_SIMPLEX)", 2: "长边翻转 (DMDUP_VERTICAL)", 3: "短边翻转 (DMDUP_HORIZONTAL)"}
        orient_map = {1: "纵向 (DMORIENT_PORTRAIT)", 2: "横向 (DMORIENT_LANDSCAPE)"}

        handle = win32print.OpenPrinter(printer_name)
        try:
            devmode = win32print.GetPrinter(handle, 2)["pDevMode"]

            # -- 双面设置 --
            old_duplex = devmode.Duplex
            if duplex == "on":
                if duplex_mode == "short-edge":
                    devmode.Duplex = 3   # DMDUP_HORIZONTAL (短边翻转)
                else:
                    devmode.Duplex = 2   # DMDUP_VERTICAL (长边翻转)
            else:
                devmode.Duplex = 1       # DMDUP_SIMPLEX
            new_duplex = devmode.Duplex
            # P1-8: 显式置位 DM_DUPLEX，告知驱动 Duplex 字段已设置（否则驱动可能忽略双面设置）
            devmode.Fields |= win32con.DM_DUPLEX

            # -- 方向设置 --
            old_orient = getattr(devmode, 'Orientation', 0)
            dm_flags = win32con.DM_IN_BUFFER | win32con.DM_OUT_BUFFER
            if orientation == "landscape":
                devmode.Orientation = win32con.DMORIENT_LANDSCAPE  # 2
                dm_flags |= win32con.DM_ORIENTATION
                devmode.Fields |= win32con.DM_ORIENTATION  # P1-8: 显式置位方向字段
            elif orientation == "portrait":
                devmode.Orientation = win32con.DMORIENT_PORTRAIT   # 1
                dm_flags |= win32con.DM_ORIENTATION
                devmode.Fields |= win32con.DM_ORIENTATION  # P1-8: 显式置位方向字段
            new_orient = getattr(devmode, 'Orientation', 0)

            print(f"[DEVMODE] 打印机: {printer_name}")
            print(f"[DEVMODE] 双面设置: {duplex_map.get(old_duplex, f'未知({old_duplex})')} → {duplex_map.get(new_duplex, f'未知({new_duplex})')}")
            if orientation:
                print(f"[DEVMODE] 方向设置: {orient_map.get(old_orient, f'未知({old_orient})')} → {orient_map.get(new_orient, f'未知({new_orient})')}")
            logger.info(f"DEVMODE: OpenPrinter={printer_name}, Duplex {old_duplex}→{new_duplex}, "
                        f"Orientation {old_orient}→{new_orient} (orientation='{orientation}')")

            # DocumentProperties 验证/合并 DEVMODE
            # P1-8: DM_OUT_BUFFER 模式下驱动校验结果直接写回 devmode 缓冲，
            # 返回/使用的正是 ResetDC 应采纳的校验后缓冲（返回值语义不丢弃）
            result = win32print.DocumentProperties(
                0, handle, printer_name,
                devmode, devmode,
                dm_flags,
            )
            logger.info(f"DEVMODE: DocumentProperties 返回 {result} (正数=成功)")
            if result <= 0:
                print(f"[DEVMODE] ⚠ DocumentProperties 验证返回 {result}（可能无效，双面/方向设置可能未被驱动采纳）")
                logger.warning(f"DEVMODE: DocumentProperties 校验失败 (result={result})")
            else:
                print(f"[DEVMODE] ✓ DocumentProperties 验证通过，驱动已采纳 DEVMODE")

            # 驱动校验后的 devmode 缓冲（DM_OUT_BUFFER 已就地写回），供 ResetDC 使用
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
    orientation: str = "",
    progress_callback: Callable[[int, int], None] | None = None,
) -> tuple[bool, str]:
    """使用 SumatraPDF 命令行进行静默打印。

    先通过 DEVMODE 配置打印机（双面/份数），确保打印机驱动不忽略参数。
    """
    # P2-15: SumatraPDF 超时随页数放大（120s 起步，每页 2s，上限 600s）
    total_pages = get_pdf_info(pdf_path).get("page_count", 0)
    sumatra_timeout = min(max(120, total_pages * 2), 600)

    # 先配置打印机 DEVMODE，确保驱动层面参数正确
    if printer_name:
        try:
            _get_printer_devmode(printer_name, duplex, duplex_mode, orientation)
        except Exception as e:
            logger.warning(f"SumatraPDF 前 DEVMODE 配置失败: {e}")

    # 单数页 + 双面 + 多份 → 逐份打印，确保副本独立
    if duplex == "on" and copies > 1:
        info = get_pdf_info(pdf_path)
        total_pages = info["page_count"]
        if total_pages > 0:
            pages_to_print = _parse_page_range(page_range, total_pages)
            if len(pages_to_print) % 2 == 1:
                print(f"[SumatraPDF] ⚙ 奇数页({len(pages_to_print)}页) + 双面 + 多份({copies}份) → 逐份打印确保副本独立")
                logger.info(f"SumatraPDF: 奇数页双面多份，改为逐份打印")
                for i in range(copies):
                    print(f"[SumatraPDF] 第 {i + 1}/{copies} 份...")
                    ok, msg = _print_via_sumatra(
                        sumatra_path, pdf_path, printer_name,
                        duplex, duplex_mode, page_range, 1, orientation, progress_callback,
                    )
                    if not ok:
                        return False, f"第 {i + 1}/{copies} 份失败: {msg}"
                    print(f"[SumatraPDF] ✓ 第 {i + 1} 份完成")
                    if progress_callback:
                        progress_callback(i + 1, copies)
                return True, f"SumatraPDF 逐份打印完成 ({copies} 份)"

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
            # P2-8: `range=` 为非官方 print-settings 键，SumatraPDF 可能忽略而打印全部页 — 保守告警
            logger.warning(f"SumatraPDF: '-print-settings range={parsed}' 为非官方键，驱动可能忽略而打印全部页")
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
            timeout=sumatra_timeout,
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
        print(f"[SumatraPDF] ✗ 超时 ({sumatra_timeout}s)")
        return False, f"SumatraPDF 超时 ({sumatra_timeout}s)"
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
    orientation: str = "",
    progress_callback: Callable[[int, int], None] | None = None,
    dpi: int = 0,
) -> tuple[bool, str]:
    """终极兜底：应用层循环打印，每次只打 1 份。成功率最高。"""
    print(f"[循环打印] 共 {copies} 份，每次 1 份...")
    for i in range(copies):
        print(f"[循环打印] 第 {i + 1}/{copies} 份...")
        ok, msg = _print_pdf_native(pdf_path, printer_name, duplex, duplex_mode, page_range, 1, orientation, progress_callback, dpi)
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
