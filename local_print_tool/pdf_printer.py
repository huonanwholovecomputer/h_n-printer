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
import tempfile
import threading
import time
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# P1-9: 标记 GDI 打印已中途出纸（StartPage 已执行）。带此前缀的失败消息不得再降级整单重打。
MID_PRINT_MARKER = "[已出纸] "

# P0(2026-10): 标记"驱动不接受双面设置"。带此前缀的失败消息不得降级到 Sumatra/循环——
# 那些路径同样依赖驱动，只会静默单面出纸，等于把"没双面却报成功"换个地方重演。
DUPLEX_UNSUPPORTED_MARKER = "[双面不可用] "

# 单页渲染的像素上限 —— 按**进程位宽**分档，防止极端尺寸的页面把内存打爆。
#
# 旧值 400_000_000 的注释写"约 32 位进程上限"，但这个数经不起换算：400 Mpx × 3 字节(RGB,
# alpha=False) = 1.14 GiB **单**缓冲；而单页流水线的实测瞬时系数是 5.7×（pixmap +
# transpose 副本 + ImageWin.Dib 副本 + 分配器抖动，见 tests/verify_render_memory.py），
# 即 ≈ 6.5 GiB —— 对 32 位进程（用户地址空间仅 2 GiB）等于**没有保护**。
#
# 现在按"可接受的瞬时峰值"反推像素上限：
#   · 64 位：预算 ≈1.5 GiB 瞬时 → 约 94 Mpx
#     （A2@400dpi=62Mpx 仍全分辨率；A0@400dpi=248Mpx 会被降 DPI —— 降 DPI 远好于 OOM）
#   · 32 位：预算 ≈300 MiB 瞬时 → 约 18 Mpx
#     （A4@400dpi=15.5Mpx 恰好全分辨率；A3@400dpi=31Mpx 降 DPI）
RENDER_TRANSIENT_FACTOR = 5.7      # 实测：单页流水线峰值 ≈ 5.7 个单页位图
_PIXEL_BYTES_RGB = 3               # alpha=False → RGB，3 字节/像素
_PIXEL_BYTES_GRAY = 1              # 单色机：MuPDF 先出 8bit 灰度（600dpi A4 ≈ 32 MiB），再转 1bit（≈4 MiB）


def max_render_pixels(bytes_per_pixel: int = _PIXEL_BYTES_RGB) -> int:
    """按当前进程位宽给出单页渲染像素上限（超过 → 自动降分辨率；仍超 → 拒绝该页）。

    bytes_per_pixel 决定"一页位图多大"：RGB 24bit 是 3，单色灰度是 1。
    单色路径的中间缓冲只有 1 字节/像素，同样的内存预算允许 3 倍的像素。
    """
    bpp = max(1, int(bytes_per_pixel))
    budget = (1536 * 1024 * 1024) if sys.maxsize > 2 ** 32 else (300 * 1024 * 1024)
    return max(1_000_000, int(budget / (RENDER_TRANSIENT_FACTOR * bpp)))


MAX_RENDER_PIXELS = max_render_pixels()            # RGB（彩色机）基准
MONO_RENDER_PIXELS = max_render_pixels(_PIXEL_BYTES_GRAY)   # 单色机（灰度中间缓冲）基准


# ── 渲染策略（2026-10 重构）──
#
# 旧路径：按「设定 DPI」渲染（默认 400）→ 再按打印机原生 DPI 算出目标矩形 →
# StretchDIBits 把 400dpi 位图**最近邻放大到 600dpi 设备像素**，最后单色驱动再做半色调。
# 实测一次打印里发生了两次重采样（1.43× 放大 + 0.955 适配缩放），小字锯齿就是从这里来的；
# 而"600 太慢/太费内存"的顾虑来自 RGB 24bit（A4@600 = 99.6 MiB/页）。
#
# 现在：先算目标设备像素，再**直接渲染到该尺寸**（1:1，MuPDF 只采样一次）；
# 单色设备输出 1bit（阈值/抖动），驱动无需半色调，内存也从 99.6 MiB 降到 ~4 MiB/页。
# dpi 参数只作为**上限**（低于设备分辨率时才主动降采样换速度）。
MONO_THRESHOLD = 136               # 8bit 灰度 → 1bit 的硬阈值（< 阈值判黑）
# 页面含非纯黑内容（图片/灰底纹/彩字）时整页改抖动：阈值会把浅灰内容抹成白色（见 _page_needs_dither）


def plan_page_raster(page_w_pts: float, page_h_pts: float,
                     native_dpi_x: float, native_dpi_y: float,
                     printable_w: int, printable_h: int,
                     dpi_cap: int = 0, pixel_limit: int = 0) -> tuple[int, int, int, int, bool]:
    """纯逻辑：算一页的「目标设备尺寸」与「实际渲染尺寸」（离线可验收，不碰打印机）。

    返回 (draw_w, draw_h, render_w, render_h, capped)
      · draw_*   = 页面在设备上的物理像素尺寸（原生 DPI 换算 + 适配可打印区）。
                   贴图按这个矩形画，保证物理尺寸与渲染 DPI 无关；
      · render_* = 实际让 MuPDF 渲染的位图尺寸；默认 == draw_* ⇒ 1:1、零重采样；
      · capped   = 是否因 dpi_cap / pixel_limit 降了分辨率（此时 GDI 会放大一次，属主动取舍）。
    """
    dev_w = page_w_pts * native_dpi_x / 72.0
    dev_h = page_h_pts * native_dpi_y / 72.0
    # 设备查询异常（HORZRES/VERTRES=0）时不缩成 1px 图，退回按页面物理尺寸
    if printable_w <= 0 or printable_h <= 0:
        printable_w = max(1, int(round(dev_w)))
        printable_h = max(1, int(round(dev_h)))
    fit = min(1.0, printable_w / max(1.0, dev_w), printable_h / max(1.0, dev_h))
    draw_w = max(1, int(round(dev_w * fit)))
    draw_h = max(1, int(round(dev_h * fit)))

    render_w, render_h = draw_w, draw_h
    capped = False

    # ① dpi 上限：只在低于设备分辨率时生效（高于设备 = 无意义，保持 1:1）
    cap = int(dpi_cap or 0)
    if cap > 0:
        cap_w = max(1, int(round(page_w_pts * cap / 72.0)))
        cap_h = max(1, int(round(page_h_pts * cap / 72.0)))
        if cap_w < draw_w or cap_h < draw_h:
            render_w, render_h = cap_w, cap_h
            capped = True

    # ② 单页像素上限（防极端尺寸页面 OOM；在 StartPage 之前判定，不浪费纸）
    limit = int(pixel_limit or 0)
    if limit > 0 and render_w * render_h > limit:
        k = (limit / float(render_w * render_h)) ** 0.5
        render_w = max(1, int(render_w * k))
        render_h = max(1, int(render_h * k))
        capped = True

    return draw_w, draw_h, render_w, render_h, capped


def plan_page_draw(page_w_pts: float, page_h_pts: float,
                   native_dpi_x: float, native_dpi_y: float,
                   printable_w: int, printable_h: int,
                   dpi_cap: int = 0, pixel_limit: int = 0,
                   landscape_fill: bool = True) -> tuple[int, int, int, int, int, bool]:
    """一页该怎么摆到纸上：要不要**整页转 90°**，以及转完之后的绘制/渲染尺寸。

    返回 (angle, draw_w, draw_h, render_w, render_h, capped)。
    angle ∈ {0, 90}：90 表示这一页整页转 90° 再贴图（页横纸竖、或页竖纸横）。

    为什么要有它（2026-10-27 现场）：横向 PDF（841.9×595.3pt，A4 横）在**纵向纸**上
    按旧的等比缩放只能占纸张 49%（LJ1020 可打印区 4736×6824 @600dpi ⇒ 画成
    4736×3349、上下各留 1737px 空白），现场读成"横向文件打出来是竖着的"。
    正解不是缩小，而是把这一页转 90° 铺满纸张 —— 与 Word/Adobe 的「自动旋转」同一效果，
    读者把纸横过来看即可（纸耗不变、字大约 ×1.48）。

    ⚠ 转 90° 与手动双面第 2 趟的 180° 补偿是**两件独立的事**，叠加顺序不影响结果
    （90+180 与 180+90 都等于 270）。这里只负责 90°，180° 仍由调用方逐面决定。

    draw_*/render_*/capped 的语义与 plan_page_raster 完全一致，只是都变成**转完之后**的
    尺寸（转置只换宽高、不改像素数），所以调用方拿到位图后直接用 img.size 即可。
    """
    paper_portrait = int(printable_w) <= int(printable_h)
    page_landscape = float(page_w_pts) > float(page_h_pts)
    # 页面朝向与纸张朝向**不一致**时才转：横页 + 竖纸、竖页 + 横纸。
    # （纸是不是竖的 ⇔ paper_portrait；"页横" 与 "纸竖" 同真 ⇒ 要转 90°。）
    angle = 90 if (landscape_fill and page_landscape == paper_portrait) else 0
    # 转 90° ⇒ 目标框按"转置后的页面"算（等效于把纸横过来量）
    eff_w, eff_h = (page_h_pts, page_w_pts) if angle else (page_w_pts, page_h_pts)
    draw_w, draw_h, render_w, render_h, capped = plan_page_raster(
        eff_w, eff_h, native_dpi_x, native_dpi_y, printable_w, printable_h,
        dpi_cap=dpi_cap, pixel_limit=pixel_limit,
    )
    return angle, draw_w, draw_h, render_w, render_h, capped


def _is_monochrome_printer(printer_name: str, devmode, hdc, win32con) -> bool:
    """判断目标队列是否黑白设备（决定走 1bit 还是 RGB）。三路证据，任一"确定"即采纳。

    ① DEVMODE.dmColor 最权威（DMCOLOR_MONOCHROME=1 / DMCOLOR_COLOR=2）；
    ② DeviceCapabilities(DC_COLORDEVICE)：0=单色、1=彩色；
    ③ DC 能力位兜底（NUMCOLORS<=2 或 BITSPIXEL==1）。
    """
    color = getattr(devmode, "Color", None) if devmode is not None else None
    try:
        if color is not None:
            if int(color) == 1:      # DMCOLOR_MONOCHROME
                return True
            if int(color) == 2:      # DMCOLOR_COLOR
                return False
    except (TypeError, ValueError):
        pass

    try:
        import win32print
        cap = getattr(win32con, "DC_COLORDEVICE", 32)
        res = win32print.DeviceCapabilities(printer_name, "", cap)
        if res in (0, 1):
            return res == 0
    except Exception:
        pass

    try:
        if hdc is not None:
            if hdc.GetDeviceCaps(win32con.NUMCOLORS) <= 2:
                return True
            if hdc.GetDeviceCaps(win32con.BITSPIXEL) == 1:
                return True
    except Exception:
        pass
    return False


def _to_bilevel(gray, threshold: int = MONO_THRESHOLD):
    """8bit 灰度 → 1bit 硬阈值（Pillow C 实现，A4@600dpi 约几十毫秒）。

    用 `point(lut, "1")` 一步出 1bit：若先 point 成 L 再 convert("1")，中间会多一份
    32 MiB 的整页灰度副本（600dpi A4），纯属浪费。
    """
    t = max(1, min(254, int(threshold)))
    lut = [0] * t + [255] * (256 - t)
    return gray.convert("L").point(lut, "1")


def _is_blackish(color) -> bool:
    """判断文本/图元颜色是否接近纯黑（None 视为不适用）。"""
    try:
        if color is None:
            return True
        vals = list(color)[:3]
        return bool(vals) and all(float(v) <= 0.02 for v in vals)
    except Exception:
        return False


def _page_needs_dither(page) -> bool:
    """页面里存在**非纯黑**内容（图片 / 灰底纹 / 彩色或浅灰文字）时改用抖动。

    为什么不是"统一阈值、统一抖动"：
      · 硬阈值（默认）保锐度 —— 纯黑文字/线条页的最优解（就是实机 A/B 里那张）；
      · 但阈值会把浅灰底纹、浅灰小字、照片里的中间调直接抹成白色（内容丢失）。
        老实现把 RGB 交给驱动做半色调，这些灰阶本来是在的 —— 不能因为改渲染策略而丢内容。
    所以：**只要页面里有任何非纯黑内容，就整页 Floyd–Steinberg**；纯黑文字页保持阈值。
    代价是这类页面的文字边缘略软（600dpi 下那些抖动点只有 42µm，肉眼几乎不可见）。
    """
    try:
        # ① 任何图片（哪怕只占一小块：小照片/证件照被阈值化会很难看）
        if page.get_images(full=False):
            return True
    except Exception:
        pass

    try:
        # ② 文本颜色非纯黑（浅灰批注、彩色正文）
        for block in page.get_text("dict").get("blocks", []):
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    if int(span.get("color", 0) or 0) != 0:
                        return True
    except Exception:
        pass

    try:
        # ③ 矢量填充/描边非纯黑（灰底纹表头、彩色图元）
        for d in page.get_drawings():
            if d.get("fill") is not None and not _is_blackish(d.get("fill")):
                return True
            if d.get("color") is not None and not _is_blackish(d.get("color")):
                return True
    except Exception:
        pass

    return False


# ============================================================
# PDF 信息读取
# ============================================================

def get_image_info(image_path: str) -> dict:
    """
    读取图片尺寸信息，返回页数和方向。
    单张图片始终为 1 页，方向由宽高比决定（已应用 EXIF 方向，
    避免手机竖拍照片因原始像素为横而被误判成横向）。
    """
    try:
        from PIL import Image, ImageOps
        img = Image.open(image_path)
        img = ImageOps.exif_transpose(img)   # 应用 EXIF 旋转，拿到真实显示尺寸
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


def _pdf_page_rotation(page) -> int:
    """读取页面的**实际** /Rotate（含从 /Pages 节点继承的值），归一化到 0/90/180/270。

    ⚠ 这是 2026-10-28 现场 bug 的根因：page.mediabox 是**未旋转**的物理页面框，
    而 /Rotate 90 会让整页在阅读器里转 90° 显示。手机扫描 / 部分学校系统导出的 PDF
    就是「A4 竖 mediabox(595×842) + /Rotate 90」= 看起来横版 —— 只看 mediabox 会判成竖版。
    PyPDF2 的 page.get("/Rotate") 不保证做继承，所以页上没有时手动沿 /Parent 往上找。
    """
    try:
        val = page.get("/Rotate", None)
        node = page.get("/Parent", None) if val is None else None
        depth = 0
        while val is None and node is not None and depth < 32:
            val = node.get("/Rotate", None)
            node = node.get("/Parent", None) if val is None else None
            depth += 1
        return int(val) % 360 if val is not None else 0
    except Exception:
        return 0


def count_pdf_pages(pdf_path: str) -> int:
    """统计 PDF 文件页数，失败返回 0。"""
    return get_pdf_info(pdf_path).get("page_count", 0)


def get_pdf_info(pdf_path: str) -> dict:
    """
    获取 PDF 信息：页数 + 页面方向。

    方向按**阅读器里看到的样子**判：MediaBox 的宽高先按 /Rotate 折算（见
    _pdf_page_rotation），否则「A4 竖 mediabox + /Rotate 90」的扫描件会被判成竖版。

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
            if _pdf_page_rotation(page) % 180:
                # /Rotate 90/270：显示方向被转了 90°，宽高互换后再判朝向
                w, h = h, w
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


# ── 末尾空白页检测（2026-10-28 用户需求）──────────────────────────────
# 现场：学校系统导出的 .doc/.docx 申请表常在最后多带一张完全空白的页（表格后面那个
# 空段落/分页符撑出来的），打出来就是一张白纸。只查**末尾连续**空白页 —— 中间的
# 留白页（装订留白、章节分隔）不动，用户没要求也没有把握替人判断。
BLANK_PAGE_MAX_INK = 0.0002      # 暗像素占比门槛：A4@72dpi ≈ 100 px
BLANK_PAGE_MIN_INK_PX = 40       # 绝对容差：再小的墨点按"扫描噪声/微小污点"算空白
BLANK_SCAN_MAX_PAGES = 8         # 最多往回扫几页（末尾 1~2 页是常态）


def page_is_blank(page, max_ink: float = BLANK_PAGE_MAX_INK) -> bool:
    """这一页是不是"白纸一张"。只认两路判据（实测余量极大，见下）：

    · **页面文字为空** —— 有页脚页码、只有一两个字符都算有内容（那确实会印出来）；
    · **72dpi 灰度栅格化后暗像素占比 < max_ink** —— 栅格看的是"最终印出来什么样"，
      所以白底矩形、空图层、扫描件整页是图都不影响判定。

    2026-10-28 现场标定（打印列表真实文件，A4@72dpi = 501832 px）：
      真空白页 0 个暗像素；最"空"的内容页 9483 个（1.89%，只有 191 个字符）；
      普通内容页 8.8%~26.5%。门槛取 0.02%（≈100 px）离两头都有 100 倍余量。
    """
    try:
        if page.get_text("text").strip():
            return False
    except Exception:
        return False            # 连文字都读不出来 ⇒ 按"有内容"处理（fail-open，绝不误删）
    try:
        import fitz
        pix = page.get_pixmap(dpi=72, colorspace=fitz.csGRAY)
        data = pix.samples
    except Exception:
        return False
    if not data:
        return True
    dark = sum(1 for v in data if v < 245)
    # 两道容差取大：占比口径（页面越大容差越大）+ 绝对值 40 px（≈1mm²，挡扫描噪点）。
    # 任何**会印出来**的内容（一个字符/一条线/一个图章）都远超这个量级。
    return dark <= max(float(BLANK_PAGE_MIN_INK_PX), float(max_ink) * len(data))


def detect_trailing_blank_pages(pdf_path: str, max_scan: int = BLANK_SCAN_MAX_PAGES) -> int:
    """末尾**连续**空白页的页数（0 = 末页有内容 / 打不开文件）。

    从最后一页往回想，遇到第一张有内容的页就停 —— 只处理"最后的空白页"，且不必为一个
    200 页的扫描件把每页都栅格化。任何异常一律返回 0：检测不可用绝不能妨碍打印。
    """
    try:
        import fitz
        doc = fitz.open(pdf_path)
    except Exception as e:
        logger.debug(f"空白页检测打不开 {pdf_path}: {e}")
        return 0
    try:
        n = len(doc)
        if n <= 0:
            return 0
        limit = max(0, int(max_scan))
        count = 0
        for pno in range(n - 1, max(-1, n - 1 - limit), -1):
            if page_is_blank(doc[pno]):
                count += 1
            else:
                break
        return count
    except Exception as e:
        logger.debug(f"空白页检测失败 {pdf_path}: {e}")
        return 0
    finally:
        try:
            doc.close()
        except Exception:
            pass


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
# 打印机能力探测：是否支持**自动**双面
# ============================================================

DC_DUPLEX = 7  # wingdi.h: DeviceCapabilities 的能力位

# 能力缓存：键 =（打印队列名, 驱动名, 端口）小写 —— 改名/换驱动/换端口即失效重探
_DUPLEX_CAP_CACHE: dict[tuple, dict] = {}
_DUPLEX_CAP_LOCK = threading.Lock()

# ── 虚拟打印机（无纸输出）判定 ─────────────────────────────────────────────
# Microsoft Print to PDF / Microsoft XPS Document Writer / OneNote / Adobe PDF /
# 「导出为 WPS PDF」这类队列**不出纸**：Duplex 对它们没有物理意义，驱动把 Duplex 归一化回
# 1(SIMPLEX) 是常态，也不会浪费纸。真实机型上那条「驱动改回单面 = 假成功，必须拒打」
# （防按双面计费却出单面纸）对这类队列不成立 —— 不放开就没法用虚拟打印机跑通含双面文件的
# 整条流程（2026-10 实测报错：拒绝双面打印: 该打印机不支持自动双面…：Microsoft Print to PDF）。
VIRTUAL_PRINTER_KEYWORDS = (
    "print to pdf", "xps document writer", "print to onenote", "onenote",
    "pdfcreator", "cute pdf", "cutepdf", "pdf24", "novapdf", "bullzip", "primopdf",
    "pdf-xchange", "docucom", "adobe pdf", "wps pdf", "foxit pdf", "print to file",
    "virtual printer",
)
# 端口名同样是证据：PORTPROMPT: = 打印时弹「另存为」；FILE:/nul: = 写文件/丢弃；
# "…\*.pdf" 这类端口本身就是一条 PDF 路径。
VIRTUAL_PRINTER_PORT_MARKS = ("portprompt:", "file:", "nul:", ".pdf")

_IDENTITY_CACHE: dict[str, tuple[str, str]] = {}
_IDENTITY_LOCK = threading.Lock()


def _looks_virtual(text: str) -> bool:
    s = (text or "").strip().lower()
    return bool(s) and any(k in s for k in VIRTUAL_PRINTER_KEYWORDS)


def _port_looks_virtual(port: str) -> bool:
    s = (port or "").strip().lower()
    return bool(s) and any(m in s for m in VIRTUAL_PRINTER_PORT_MARKS)


def printer_identity(printer_name: str) -> tuple[str, str]:
    """(驱动名, 端口名)，按队列名缓存；取不到就返回 ("", "")。

    只给 `is_virtual_printer()` 的名字路径兜底用（能力探测自己已读 driver/port，不必走这里）。
    """
    name = (printer_name or "").strip()
    if not name:
        return "", ""
    key = name.lower()
    with _IDENTITY_LOCK:
        hit = _IDENTITY_CACHE.get(key)
    if hit is not None:
        return hit
    driver = port = ""
    try:
        import win32print
        handle = win32print.OpenPrinter(name)
        try:
            info = win32print.GetPrinter(handle, 2)
            driver = info.get("pDriverName", "") or ""
            port = info.get("pPortName", "") or ""
        finally:
            win32print.ClosePrinter(handle)
    except Exception as e:
        logger.debug(f"读取打印机 {name} 的驱动/端口失败（按队列名判虚拟打印机）: {e}")
    with _IDENTITY_LOCK:
        _IDENTITY_CACHE[key] = (driver, port)
    return driver, port


def is_virtual_printer(printer_name: str = "", driver: str = "", port: str = "") -> bool:
    """这台队列是否「虚拟打印机」（不出纸：Print to PDF / XPS / OneNote / PDF 虚拟驱动）。

    判据：队列名或驱动名命中虚拟关键字，或端口名是 PORTPROMPT:/FILE:/nul:/…pdf。
    driver/port 未给出时按队列名查一次驱动/端口（结果缓存），查不到就只按队列名判。
    """
    if _looks_virtual(printer_name):
        return True
    if not driver and not port:
        driver, port = printer_identity(printer_name)
    return _looks_virtual(driver) or _looks_virtual(port) or _port_looks_virtual(port)


def get_printer_duplex_support(printer_name: str = "", refresh: bool = False) -> dict:
    """探测打印机是否具备**自动**双面（物理有没有双面器）。

    两条独立证据，必须一起看（原因见 tests/verify_duplex_probe.py 顶部说明）：

      ① `DeviceCapabilities(dev, port, DC_DUPLEX)` —— 权威能力位：0 = 不支持自动双面；
      ② **DEVMODE 合并回读**：把 Duplex=2(长边) 交给驱动校验一次
         （`DocumentProperties` + DM_IN_BUFFER|DM_OUT_BUFFER，**只写缓冲区、不改打印机默认设置**），
         读回仍为 2 → 驱动接受；被改回 1(SIMPLEX) → 驱动拒绝。
         实测：所有无自动双面的队列（Print to PDF / WPS PDF / Adobe PDF）都会被改回 1，
         而有双面器的 HP M232 保持 2。

    ⚠ 已知盲区：HP LaserJet 1018/1020/1022 这类"只有手动双面"的机型，驱动会把「手动双面」
    也算成双面能力（DC_DUPLEX 可能报 1），甚至不拒绝 Duplex=2。所以：
      · DC_DUPLEX=0 且驱动拒绝 → "high"，可硬判"不支持"；
      · DC_DUPLEX=1 且驱动接受 → 只给 "medium"，**必须由人工/校准页确认**（服务端 devices.json
        里的人工盖章优先，本函数只做本地兜底判定）。

    Returns:
        {
          "auto_duplex": True|False|None,   # None = 判不出来（无 pywin32 / 驱动异常）
          "confidence": "high"|"medium"|"low",
          "printer"/"driver"/"port": str,
          "dc_duplex": 0|1|None, "dm_accepted": bool|None,
          "virtual": bool,                  # True = 虚拟打印机（不出纸）：双面为空操作，
                                            #        调用方**不得**按"不支持自动双面"拦截
          "detail": str,                    # 人类可读结论（日志用；不输出 token 之类）
        }

    缓存按（队列名+驱动名+端口）：`refresh=True` 强制重探。
    """
    printer = (printer_name or "").strip()
    out = {"auto_duplex": None, "confidence": "low", "printer": printer,
           "driver": "", "port": "", "dc_duplex": None, "dm_accepted": None,
           "virtual": _looks_virtual(printer), "detail": ""}
    try:
        import win32con
        import win32print
    except ImportError:
        out["detail"] = "缺少 pywin32，无法探测打印机能力"
        return out

    if not printer:
        try:
            printer = win32print.GetDefaultPrinter()
            out["printer"] = printer
        except Exception as e:
            out["detail"] = f"取系统默认打印机失败: {e}"
            return out

    handle = None
    try:
        handle = win32print.OpenPrinter(printer)
        info = win32print.GetPrinter(handle, 2)
        out["driver"] = info.get("pDriverName", "") or ""
        out["port"] = info.get("pPortName", "") or ""
        if not out["virtual"]:
            # 队列名判不出来时，驱动名/端口名还能判（如「导出为 WPS PDF」→ 驱动带 virtual printer）
            out["virtual"] = (is_virtual_printer("", out["driver"], out["port"]))

        key = (printer.lower(), out["driver"].lower(), out["port"].lower())
        with _DUPLEX_CAP_LOCK:
            if not refresh and key in _DUPLEX_CAP_CACHE:
                return dict(_DUPLEX_CAP_CACHE[key])

        # ① 能力位
        try:
            cap = win32print.DeviceCapabilities(printer, out["port"], DC_DUPLEX)
            if isinstance(cap, (tuple, list)):
                cap = cap[0] if cap else None
            out["dc_duplex"] = int(cap) if isinstance(cap, int) else None
        except Exception as e:
            logger.warning(f"DeviceCapabilities(DC_DUPLEX) 失败: {e}")

        # ② DEVMODE 合并回读（不改默认设置）
        try:
            dm = info["pDevMode"]
            if dm is not None:
                dm.Duplex = win32con.DMDUP_VERTICAL  # 2
                dm.Fields |= win32con.DM_DUPLEX
                win32print.DocumentProperties(
                    0, handle, printer, dm, dm,
                    win32con.DM_IN_BUFFER | win32con.DM_OUT_BUFFER,
                )
                out["dm_accepted"] = (getattr(dm, "Duplex", None) == win32con.DMDUP_VERTICAL)
        except Exception as e:
            logger.warning(f"DEVMODE 双面回读失败: {e}")

        dc, acc = out["dc_duplex"], out["dm_accepted"]
        if dc == 0 and acc is False:
            out["auto_duplex"], out["confidence"] = False, "high"
            out["detail"] = "无自动双面（能力位=0 且驱动把 Duplex 改回单面）"
        elif dc == 0:
            out["auto_duplex"], out["confidence"] = False, "medium"
            out["detail"] = "无自动双面（能力位=0）"
        elif dc == 1 and acc is True:
            out["auto_duplex"], out["confidence"] = True, "medium"
            out["detail"] = ("驱动声明支持双面且未拒绝设置；"
                             "注意 HP 1018/1020/1022 这类只有手动双面的机型也会这样报，"
                             "需用校准页实机确认")
        elif dc == 1 and acc is False:
            out["auto_duplex"], out["confidence"] = False, "high"
            out["detail"] = "驱动声明支持双面但拒绝 Duplex 设置 —— 实际不可用，按不支持处理"
        elif dc == 1:
            # 能力位说支持，但没拿到 DEVMODE 无法验证 → 报"支持但低置信"（调用方照常提交、
            # 由 GUI/人工确认），不硬判"判不出来"，否则会白丢一个可能真支持双面的机型
            out["auto_duplex"], out["confidence"] = True, "low"
            out["detail"] = "驱动声明支持双面，但未能验证 DEVMODE（无法确认，需实机/人工确认）"
        else:
            out["detail"] = f"判不出来（能力位={dc}, 驱动接受={acc}）"
        if out["virtual"]:
            # 能力字段照实报（多数是 False/high），但语义是"双面对它没有意义"，不是
            # "驱动做不到 → 必须拦"。两者靠 virtual 区分，拦截点只认后者。
            out["detail"] += "；虚拟打印机（无纸输出）⇒ 双面为空操作，按请求放行"
        logger.info(f"双面能力探测: {printer} → auto_duplex={out['auto_duplex']} "
                    f"({out['confidence']}): {out['detail']}")
        with _DUPLEX_CAP_LOCK:
            _DUPLEX_CAP_CACHE[key] = dict(out)
        return out
    except Exception as e:
        out["detail"] = f"探测异常: {e}"
        logger.warning(f"双面能力探测失败 ({printer}): {e}")
        return out
    finally:
        if handle is not None:
            try:
                win32print.ClosePrinter(handle)
            except Exception:
                pass


# ============================================================
# 打印函数
# ============================================================

def build_render_sequence(
    pages_to_print: list[int],
    copies: int = 1,
    duplex: str = "off",
    pages_override: Optional[list[Optional[int]]] = None,
) -> list[Optional[int]]:
    """把「要打的页 / 份数 / 手动双面页序」摊平成逐面渲染序列（0-based 页号，None = 空白页）。

    · 常规路径（pages_override=None）：每份按 pages_to_print 顺序，份与份之间在
      「奇数页 + 双面 + 多份」时插一张空白分隔页（保证副本独立，历史行为不变）。
    · 手动双面/校准（pages_override 给出）：**按调用方给的精确序列**渲染（含 None 空白
      占位页），copies 由调用方自己循环（每次 1 份），此处不再插分隔页。

    抽成纯函数是为了能在没有打印机的机器上验收（tests/verify_duplex_probe.py）：
    这个序列就是"纸上到底按什么顺序出页"的唯一真相。
    """
    if pages_override is not None:
        return list(pages_override)
    seq: list[Optional[int]] = []
    needs_separator = duplex == "on" and copies > 1 and len(pages_to_print) % 2 == 1
    for copy_idx in range(max(1, copies)):
        seq.extend(pages_to_print)
        if needs_separator and copy_idx < copies - 1:
            seq.append(None)
    return seq


def estimate_manual_duplex_sides(page_count: int, copies: int, page_range: str = "") -> int:
    """手动双面两趟的**物理面数**（= 纸张数 × 2，含空白占位页），用于精确进度条。

    与 `estimate_print_sides(duplex='on')` 的差别：手动双面**不插副本分隔页** ——
    页数为奇数时那个"空白"是最后一张纸的空面，本来就算在纸张数里，不该再补。
    （多份时 `estimate_print_sides` 会多算 copies-1 个分隔面，进度条会超 100%。）
    """
    n = len(_parse_page_range(page_range, max(0, page_count)))
    if n <= 0:
        n = max(1, page_count)
    sheets = ((n + 1) // 2) * max(1, copies)
    return max(1, sheets * 2)


def wait_for_output(printer_name: str = "", pages: int = 0, timeout: float = 120.0,
                    margin_per_page: float = 1.5, margin_floor: float = 1.0,
                    margin_max: float = 6.0, settle: float = 2.0) -> dict:
    """等这一趟的纸出完（队列排空 + 一小段出纸余量）。手动双面"翻纸"提示前调用。

    为什么要等：GDI 的 EndDoc 只表示"提交完"；像 LaserJet 1020 这种主机型打印机，spooler
    作业在**数据写进端口**后就消失，而纸还在出（600 DPI 一张 A4 光栅一两兆字节，USB 传输远快
    于 14ppm 的机械出纸）。这时弹"请取纸翻面"，用户抽取中的纸会夹纸／漏页。

    但也**不能等太久**：等纸真的全部出完只能靠猜（无逐页传感器），猜大一点就是让操作员干等。
    所以策略是"队列排空 + 短余量"，并把**最终判断权交给站在机器前的操作员** ——
    翻纸窗会写明"出纸槽里应有 N 张纸"，数够了再点继续（比程序猜更准）。
    ⚠ N 必须含**本批之前已经落进托盘的那些纸**（直打的单面文件 / 首页 / 上一批）：
      由 GUI 侧台账 `PrintWorker._tray_sheets` 传给 `_ask_operator_to_flip`。少算是会
      真丢纸的 —— 操作员照"从出纸槽中取出纸叠"整叠搬进纸盘 2，多出来那张就出不来了。

    Returns: 仅用于日志的诊断 dict（本函数不抛异常，探测失败也照样睡余量）。
    """
    out = {"drained": False, "queue_seconds": 0.0, "margin_seconds": 0.0,
           "peak_jobs": 0, "detail": ""}
    t0 = time.time()
    try:
        import win32print
        printer = printer_name or win32print.GetDefaultPrinter()
        while time.time() - t0 < timeout:
            handle = win32print.OpenPrinter(printer)
            try:
                try:
                    jobs = win32print.EnumJobs(handle, 0, -1, 1) or []
                except TypeError:            # 个别 pywin32 版本不接受 -1 表示"全部"
                    jobs = win32print.EnumJobs(handle, 0, 9999, 1) or []
            finally:
                win32print.ClosePrinter(handle)
            out["peak_jobs"] = max(out["peak_jobs"], len(jobs))
            if not jobs:
                out["drained"] = True
                break
            time.sleep(0.4)
    except Exception as e:
        out["detail"] = f"{type(e).__name__}: {e}"
    out["queue_seconds"] = round(time.time() - t0, 1)
    margin = (min(margin_max, max(margin_floor, pages * margin_per_page)) + settle) if pages > 0 \
        else settle
    out["margin_seconds"] = round(margin, 1)
    time.sleep(margin)
    return out


def merge_pdfs(paths: list[str], out_path: str) -> tuple[bool, str]:
    """把多份 PDF 按给定顺序合并成一份（手动双面批次用：一次翻纸要基于同一份文档页码）。

    Returns: (ok, msg)。失败时 out_path 不会留下半成品（尽力删除）。
    """
    if not paths:
        return False, "没有可合并的 PDF"
    try:
        import fitz
        doc = fitz.open()
        try:
            for p in paths:
                src = fitz.open(p)
                try:
                    doc.insert_pdf(src)
                finally:
                    src.close()
            doc.save(out_path)
        finally:
            doc.close()
    except Exception as e:
        try:
            if os.path.isfile(out_path):
                os.remove(out_path)
        except OSError:
            pass
        return False, f"合并 PDF 失败: {type(e).__name__}: {e}"
    return True, f"已合并 {len(paths)} 份 PDF → {os.path.basename(out_path)}"


def measure_print_speed(printer_name: str = "", pages: int = 10, *,
                        dpi: int = 300, on_log: Callable[[str], None] | None = None
                        ) -> tuple[bool, str, float]:
    """打 N 张**空白页**（单面）并计时 → (ok, msg, 秒)。给"第 1 趟耗时预估"做冷启动标定。

    为什么能这么测：空白页**只费纸不费碳粉**，而且走单面 ⇒ **不需要人翻纸**（远程也能标定）。
    两点标定（打 1 页 + 打 10 页）就能定出 T(n) ≈ a + b·n 的 a、b，比"总时长 ÷ 页数"准得多
    （见 printer_config.fit_print_speed）。

    计时口径与"真实打印自动采样"**保持一致**：扣掉 wait_for_output 里我们额外加的
    "队列排空静置余量"（那段是我们自己的保守等待，不是打印机的耗时）。
    """
    def _log(msg: str):
        if on_log:
            try:
                on_log(msg)
            except Exception:
                pass

    n = max(1, int(pages or 1))
    tmp = os.path.join(tempfile.gettempdir(),
                       f"hn_speed_{n}_{os.getpid()}_{int(time.time() * 1000)}.pdf")
    try:
        import fitz
        doc = fitz.open()
        for _ in range(n):
            doc.new_page()
        doc.save(tmp)
        doc.close()
    except Exception as e:
        return False, f"生成测速用空白 PDF 失败: {type(e).__name__}: {e}", 0.0

    try:
        _log(f"  → 测速：正在打印 {n} 张空白页（单面）…")
        t0 = time.perf_counter()
        ok, msg = print_pdf(pdf_path=tmp, printer_name=printer_name, copies=1, duplex="off",
                            duplex_mode="long-edge", page_range="", dpi=dpi)
        if not ok:
            return False, msg, 0.0
        w = wait_for_output(printer_name, pages=n)
        elapsed = time.perf_counter() - t0 - float(w.get("margin_seconds", 0.0) or 0.0)
        elapsed = max(0.0, elapsed)
        _log(f"  → 测速：{n} 张用时 {elapsed:.1f}s"
             f"（队列排空 {w.get('queue_seconds', 0)}s，已扣掉余量 {w.get('margin_seconds', 0)}s）")
        return True, f"{n} 张空白页用时 {elapsed:.1f}s", elapsed
    finally:
        try:
            if os.path.isfile(tmp):
                os.remove(tmp)
        except OSError:
            pass


def measure_print_speed_two_point(printer_name: str = "", *, small: int = 1, large: int = 10,
                                  dpi: int = 300,
                                  on_log: Callable[[str], None] | None = None
                                  ) -> tuple[bool, str, list]:
    """两点标定：打 small 张 + large 张 → 返回 [(张数, 秒数), ...]（交给档案做拟合）。

    选 1 与 10：1 张几乎就是"启动开销 a"，10 张再给出斜率；两点共 11 张纸。
    """
    samples: list = []
    for n in (max(1, int(small)), max(2, int(large))):
        ok, msg, sec = measure_print_speed(printer_name, n, dpi=dpi, on_log=on_log)
        if not ok:
            return False, f"测速失败（{n} 张）：{msg}", samples
        samples.append([n, round(sec, 1)])
    s_small, s_large = samples[0][1], samples[-1][1]
    if s_large <= s_small:
        return False, (f"测速结果异常（{small} 张 {s_small}s ≥ {large} 张 {s_large}s，"
                       f"可能卡纸/缺纸或第一份没打），请重试"), samples
    return True, f"标定完成：{small} 张 {s_small}s / {large} 张 {s_large}s", samples


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
    pages_override: Optional[list[Optional[int]]] = None,
    rotate_180: bool = False,
    rotate_slots: Optional[list[bool]] = None,
    landscape_fill: bool = True,
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
        dpi: 渲染分辨率上限。0（推荐）= 按设备最终像素 1:1 渲染，零缩放零重采样；
       >0 且低于设备原生分辨率时才会降采样（GDI 随后放大一次，换取速度）
        pages_override: **手动双面/校准专用**：精确的逐面序列（0-based 页号，None=空白占位页）。
                        给出时只用 GDI 一条路（Sumatra 只认整份 PDF，降级过去会把整份文件
                        按它自己的页序又打一遍 —— 那正是手动双面最怕的重复/错位）。
        rotate_180: 内容整体旋转 180°（手动双面第 2 趟用，补偿纸路径的 180° 翻转）。
                    一般不要直接调它 —— 用 print_plan_pass()，那里把这条规则封在一处。
        rotate_slots: 逐面 180° 补偿表（与 pages_override 等长，True=该面转 180°）；给出时
                    覆盖 rotate_180。手动双面一个批次里长边/短边混排就靠它（见 _print_pdf_native）。
        landscape_fill: 横向页整页转 90° 铺满纸张（默认开；纵向纸上打横向页不再缩到 49%）。
                    打印机的纸横向进纸时可传 False。

    Returns:
        (success, message)
    """
    if not os.path.isfile(pdf_path):
        return False, f"PDF 文件不存在: {pdf_path}"

    # P0-2: 统一页数校验 — 0 页 PDF 直接失败，不进入 GDI/Sumatra 任何分支（防空文件报成功漏打）
    total_pages = count_pdf_pages(pdf_path)
    if total_pages <= 0:
        return False, f"PDF 无有效页面（页数为 0，文件为空、损坏或无法解析）: {pdf_path}"

    # ── 1 页文件不请求双面（2026-10-17 现场事故的**最深一层**兜底）──
    # 单页（或页码范围只选出 1 页）时双面在物理上不可能成立，但驱动照 "Duplex=2" 处理：
    # HP LaserJet 1018/1020/1022 这类**无自动双面器**的驱动把它当"手动双面"，弹官方
    # 「双面打印说明」向导（偶数页先打 → 提示翻纸 → 再打奇数页）并可能先吐一张空白页
    # —— 现场订单 HN20261017-0002（1 页 docx）就这样打出了空白页 + 正式页。
    # 上层 gui.PrintWorker._effective_duplex 已按**真实页数**改判；这里再兜一层，是为了
    # 任何绕过 PrintWorker 的调用方（工具/脚本/将来的新入口）也不会把 1 页当双面提交。
    # ⚠ pages_override（手动双面/校准专用，逐面序列）不动：那是调用方有意排好的。
    if duplex == "on" and pages_override is None and len(_parse_page_range(page_range, total_pages)) == 1:
        logger.info(f"有效页数=1 → 不请求双面（单页上双面无意义，避免驱动手动双面向导）: "
                    f"{os.path.basename(pdf_path)}")
        print("[打印] ℹ 有效页数 = 1 → 按单面打印（双面对单页文件无意义）")
        duplex = "off"

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

    # ── 请求双面时先核一遍打印机能力（防"静默打成单面却报成功"）──
    # 依据：实测无自动双面的驱动会把 DEVMODE 的 Duplex 改回 1(SIMPLEX)，此时任何策略
    # （GDI/Sumatra/循环）都会单面出纸，而客户端按双面计费 → 店里出双倍纸。
    # 只在两条证据都指向"不支持"（confidence=high）时才拒打，避免误伤探测失败的机型；
    # 判不准或证据不足时照常打，但留下日志。
    #
    # 例外：**虚拟打印机**（Microsoft Print to PDF / XPS / OneNote / PDF 虚拟驱动）不出纸，
    # 双面对它没有物理意义、也不浪费纸 —— 不能拿"真机型假成功"那条规矩拦它，否则本地工具
    # 就没法用虚拟打印机跑通含双面文件的整条流程（测试/开发场景）。放行并注明按单面输出。
    if duplex == "on" and system == "Windows" and pages_override is None:
        verdict = get_printer_duplex_support(printer_name)
        target = verdict.get("printer") or printer_name or "(默认打印机)"
        if verdict.get("virtual") or is_virtual_printer(target):
            note = (f"虚拟打印机「{target}」不出纸：双面设置为空操作，按请求继续"
                    f"（输出按单面生成，不浪费纸）")
            logger.info(f"虚拟打印机放行双面请求: {note}")
            print(f"[打印] ℹ {note}")
        elif verdict.get("auto_duplex") is False and verdict.get("confidence") == "high":
            msg = (f"该打印机不支持自动双面（{verdict.get('detail')}）："
                   f"{verdict.get('printer')}。请改用单面打印；"
                   f"若要双面，需走手动双面流程（人工翻纸）")
            logger.error(f"拒绝双面打印: {msg}")
            print(f"[打印] ✗ {msg}")
            return False, msg
        elif verdict.get("auto_duplex") is None:
            logger.warning(f"双面能力判不准，按请求继续打印: {verdict.get('detail')}")
            print(f"[打印] ⚠ 双面能力判不准（{verdict.get('detail')}），仍按双面提交")

    if system == "Windows":
        # 方案 1: Windows 原生 GDI 打印（驱动级双面/份数控制，最可靠）
        print("[打印] 方案1: 尝试 Windows 原生 GDI 打印...")
        ok, msg = _print_pdf_native(pdf_path, printer_name, duplex, duplex_mode, page_range,
                                    copies, orientation, progress_callback, dpi, pages_override,
                                    rotate_180, rotate_slots, landscape_fill)
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

        # P0(2026-10): 驱动不接受双面 → 终止，不降级。Sumatra(`duplexlong`)与循环兜底同样
        # 依赖驱动，降级过去只会单面出纸还报成功（正是要消灭的"假成功"）。
        if msg.startswith(DUPLEX_UNSUPPORTED_MARKER):
            reason = msg[len(DUPLEX_UNSUPPORTED_MARKER):]
            logger.error(f"打印机不支持自动双面，终止（不降级整单重打）: {reason}")
            print(f"[打印] ✗ {reason}（不再降级其它方案，避免静默单面出纸）")
            return False, reason

        # 方案 2: SumatraPDF 降级
        # ⚠ 手动双面/校准（pages_override）**不走这条**：Sumatra 只接受整份 PDF + 页码 token，
        #   拿不到"每张纸打哪一面"的序列，降级过去会把整份文件按它自己的页序再打一遍
        #   —— 手动双面最怕的重复/错位就是这么来的。这种情况只用 GDI + 应用层循环。
        sumatra = None if pages_override is not None else _find_sumatra_pdf()
        if sumatra:
            print(f"[打印] 方案2: 降级为 SumatraPDF ({sumatra})")
            logger.info(f"原生 GDI 失败，降级 SumatraPDF: {sumatra}")
            ok, msg = _print_via_sumatra(sumatra, pdf_path, printer_name, duplex, duplex_mode, page_range, copies, orientation, progress_callback)
            if ok:
                print(f"[打印] ✓ 成功 ({msg})")
                return True, msg
            print(f"[打印] ✗ SumatraPDF 失败: {msg}")
            logger.info(f"SumatraPDF 也失败: {msg}")

        # 方案 3: 应用层循环打印（最可靠兜底；pages_override 原样透传，保证序列一致）
        print("[打印] 方案3: 应用层循环打印（兜底）...")
        ok, msg = _print_via_loop(pdf_path, printer_name, duplex, duplex_mode, page_range, copies,
                                  orientation, progress_callback, dpi, pages_override, rotate_180,
                                  rotate_slots, landscape_fill)
        print(f"[打印] {'✓ 成功' if ok else '✗ 失败'} ({msg})")
        return ok, msg
    else:
        print(f"[打印] 非 Windows 平台，使用 lp 命令...")
        ok, msg = _print_pdf_fallback(pdf_path, printer_name)
        print(f"[打印] {'✓ 成功' if ok else '✗ 失败'} ({msg})")
        return ok, msg


def print_plan_pass(
    plan,
    pdf_path: str,
    printer_name: str = "",
    which: int = 1,
    *,
    second_rotate_180: bool = True,
    progress_callback: Callable[[int, int], None] | None = None,
    dpi: int = 0,
    landscape_fill: bool = True,
) -> tuple[bool, str]:
    """按 manual_duplex.PrintPlan 打印第 which 趟（1/2）。

    **手动双面第 2 趟的 180° 补偿只在这里定义。** 调用方只需要给 which 与
    second_rotate_180，不要自己拼 pages_override / rotate_180 —— 这两处一旦各写一遍，
    就有把第 2 趟打成倒置、或把页序打乱的风险（实机已踩过一次）。

    second_rotate_180：本机纸路径是否把第 2 趟的纸张整体转了 180°。
      · True（默认；HP LaserJet 1020 实机实测：face-down 出纸 + 整叠原样放回）
        → 第 2 趟内容旋转 180°，操作员照 HP 弹窗"不改变方向、原样放回"即得长边/书本式；
      · False → 不旋转，操作员需把整叠在平面内转 180° 再放（短边/便签式）。
      该值随打印机型号的纸路径而不同，用 tools/print_duplex_calibration.py 实测确定。
      ⚠ 2026-10-27 起：真正逐面生效的是**计划里每张纸自带的 rotate**（plan.pass_rotate），
      本参数退化为"整趟默认/兜底"（传 False = 这一趟整体不转）。这样同一批次里长边/短边
      可以混排，不必再拆成两批各翻一次纸。
    landscape_fill：横向页整页转 90° 铺满纸张（见 plan_page_draw），随打印机档案走。
    """
    which = 2 if int(which) == 2 else 1
    slots = plan.pass_page_indices(which)
    # 逐面 180° 补偿表：计划里每张纸自己带着"要不要转"（同一批可长边/短边混排）。
    # 调用方传 second_rotate_180=False = 这一趟整体不转（旧口径，例如攒批档案里实测不需要），
    # 那就把表全清空，别让计划里的值把它顶掉。
    rotate_slots: Optional[list[bool]] = None
    flags = [bool(x) for x in getattr(plan, "pass_rotate", lambda _w: [])(which)]
    if flags and len(flags) == len(slots):
        if which == 2 and not second_rotate_180:
            flags = [False] * len(flags)
        rotate_slots = flags
    return print_pdf(
        pdf_path, printer_name, copies=1, duplex="off",
        pages_override=slots,
        rotate_180=(which == 2 and second_rotate_180),
        rotate_slots=rotate_slots,
        progress_callback=progress_callback, dpi=dpi,
        landscape_fill=landscape_fill,
    )


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
    pages_override: Optional[list[Optional[int]]] = None,
    rotate_180: bool = False,
    rotate_slots: Optional[list[bool]] = None,
    landscape_fill: bool = True,
) -> tuple[bool, str]:
    """
    Windows 原生 GDI 打印：PyMuPDF 渲染 + win32ui 输出到打印机。
    progress_callback: 每完成一面的 EndPage 后调用 (current_side, total_sides)
    dpi: 渲染分辨率上限。0（推荐）= 按设备最终像素 1:1 渲染，零缩放零重采样；
       >0 且低于设备原生分辨率时才会降采样（GDI 随后放大一次，换取速度）
    pages_override: **手动双面/校准专用** —— 精确的逐面渲染序列（0-based 页号，
                    None = 打一张空白占位页）。给出时忽略 page_range/copies，
                    copies 由调用方自己循环（见 manual_duplex.PrintPlan.pass_pages）。
    rotate_180: 把本次所有内容页渲染后旋转 180°（空白页不受影响）。**手动双面第 2 趟必须开**：
                纸张在出纸路径里被转了 180°，不补偿则第 2 面的文字整体倒置
                （HP LaserJet 1020 实机实测确认；HP 驱动自己分两趟时也在内部做了这个补偿）。
    rotate_slots: **逐面**的 180° 补偿表（与 render_seq 等长，True=该面转 180°）。给出时
                覆盖 rotate_180 —— 手动双面一个批次里可以长边/短边**混排**（同一叠纸、
                同一次翻纸，各文件按自己的装订方向补偿），旧写法只能整趟一个开关，因此
                只能把混排拆成两批、多翻一次纸（2026-10-27 现场 HN20261027-0004）。
    landscape_fill: 横向页要不要整页转 90° 铺满纸张（见 plan_page_draw）。默认开：
                纵向纸上打横向页，缩放居中只占纸张 49%，读起来像"横向文件打成了竖的"。
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

        # 虚拟打印机（Print to PDF / XPS / OneNote…）不出纸：驱动把 Duplex 归一化回单面是常态，
        # 不构成"假成功"（既不浪费纸，也不存在"按双面计费却出单面纸"）⇒ 放行并注明。
        virtual_printer = (duplex == "on") and is_virtual_printer(printer)

        # 逐面渲染序列（唯一真相）：常规路径 = 份数 × 页码（奇数页双面多份时插分隔空白页）；
        # 手动双面/校准 = 调用方给定的精确序列（含空白占位页）
        render_seq = build_render_sequence(pages_to_print, copies, duplex, pages_override)

        logger.info(f"GDI 打印: 打印机={printer}, 总页数={total_pages}, "
                    f"待打页数={len(pages_to_print)}, 份数={copies}, 面数={len(render_seq)}")
        print(f"[GDI] 目标打印机: {printer}")
        if pages_override is not None:
            blanks = sum(1 for p in render_seq if p is None)
            print(f"[GDI] 手动双面/校准序列: 共 {len(render_seq)} 面（含 {blanks} 个空白占位页）")
            if rotate_180:
                print(f"[GDI] ⚙ 内容整体旋转 180°（补偿纸路径翻转，修掉第 2 面倒置）")
        else:
            est_sep = (copies - 1) if (duplex == "on" and copies > 1 and len(pages_to_print) % 2 == 1) else 0
            if est_sep:
                print(f"[GDI] PDF 共 {total_pages} 页，本次打印 {len(pages_to_print)} 页 × {copies} 份 + {est_sep} 空白分隔页 = {len(render_seq)} 面")
            else:
                print(f"[GDI] PDF 共 {total_pages} 页，本次打印 {len(pages_to_print)} 页 × {copies} 份 = {len(render_seq)} 面")

        # -- 获取配置好双面和方向的 DEVMODE --
        print(f"[GDI] 获取打印机 DEVMODE...")
        devmode, applied_duplex = _get_printer_devmode(printer, duplex, duplex_mode, orientation)

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
                    # 日志必须报**驱动校验后的实际值**，不能拿请求值冒充已生效
                    actual = {1: "单面", 2: "长边双面", 3: "短边双面"}.get(applied_duplex,
                                                                        f"未知({applied_duplex})")
                    if duplex == "on" and applied_duplex not in (2, 3):
                        if virtual_printer:
                            print(f"[GDI] ℹ 虚拟打印机：驱动把双面归一化为「{actual}」属正常"
                                  f"（无纸输出），继续按单面渲染")
                            logger.info(f"GDI: 虚拟打印机「{printer}」双面归一化为 {applied_duplex}，继续")
                        else:
                            print(f"[GDI] ✗ DEVMODE 已应用但驱动把双面改回「{actual}」（请求 {desc}）")
                            logger.warning(f"GDI: 驱动把 Duplex 改回 {applied_duplex}（请求双面）")
                    else:
                        print(f"[GDI] ✓ DEVMODE 已应用 (win32gui.ResetDC): 双面模式={desc} → 驱动实际={actual}")
                        logger.info(f"GDI: win32gui.ResetDC 成功, duplex={duplex}/{duplex_mode}, 实际={applied_duplex}")
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

        # P0（2026-10）：驱动把双面改回单面 → 明确失败，不再"静默单面出纸 + 报成功"。
        # 实测所有无自动双面的队列都会这样（能力位=0 且 Duplex 被改回 1）。此处尚未
        # StartDoc（没出过纸），因此允许上层直接报错，不做整单重打。
        # 例外：虚拟打印机（Microsoft Print to PDF / XPS / OneNote…）—— 它本来就不出纸，
        # 双面只是空操作，按单面继续；否则整个测试/开发流程都用不了它。
        if duplex == "on" and devmode is not None and applied_duplex not in (2, 3):
            if virtual_printer:
                print(f"[GDI] ℹ 虚拟打印机「{printer}」不出纸：双面为空操作（Duplex={applied_duplex}），"
                      f"继续打印")
                logger.info(f"GDI: 虚拟打印机「{printer}」双面为空操作（Duplex={applied_duplex}），继续")
            else:
                msg = (f"驱动未采纳双面设置（Duplex={applied_duplex}）—— 该打印机不支持自动双面，"
                       f"继续打只会单面出纸: {printer}")
                logger.error(f"GDI: {msg}")
                print(f"[GDI] ✗ {msg}")
                return False, DUPLEX_UNSUPPORTED_MARKER + msg

        # 获取打印机可打印区域和分辨率（"最终尺寸 1:1 渲染"的两个输入）
        native_dpi_x = hdc.GetDeviceCaps(win32con.LOGPIXELSX)
        native_dpi_y = hdc.GetDeviceCaps(win32con.LOGPIXELSY)
        printable_w = hdc.GetDeviceCaps(win32con.HORZRES)
        printable_h = hdc.GetDeviceCaps(win32con.VERTRES)

        # 单色设备（黑白激光）→ 1bit；彩色设备保持 RGB。上限也随像素字节数分档。
        mono = _is_monochrome_printer(printer, devmode, hdc, win32con)
        pixel_limit = MONO_RENDER_PIXELS if mono else MAX_RENDER_PIXELS

        logger.info(f"GDI: 原生 DPI=({native_dpi_x},{native_dpi_y}), 可打印区域=({printable_w}x{printable_h}), "
                    f"渲染={'1bit(单色)' if mono else 'RGB'}, dpi上限={dpi if dpi > 0 else '自动(设备原生)'}")
        print(f"[GDI] 设备原生: {native_dpi_x}×{native_dpi_y} DPI, 可打印区域: {printable_w}×{printable_h}")
        if mono:
            print(f"[GDI] 单色设备 → 按最终像素渲染 1bit（阈值 {MONO_THRESHOLD}），驱动不再做半色调")
        if dpi > 0:
            print(f"[GDI] 渲染策略: 最终尺寸 1:1；{dpi} DPI 仅作上限（低于设备分辨率时才会放大一次）")
        else:
            print(f"[GDI] 渲染策略: 按设备最终像素 1:1 渲染（零缩放、零重采样）")

        title = os.path.basename(pdf_path)
        try:  # 加 HN| 前缀：外部打印监视据此排除自家作业（导入失败就用原标题，绝不影响打印）
            from print_watch import tag_self_title
            title = tag_self_title(title)
        except Exception:
            pass
        hdc.StartDoc(title)
        started = True
        print(f"[GDI] StartDoc 完成，开始逐页渲染...")

        from PIL import Image, ImageWin

        # 180° 原位旋转用 transpose（无损、精确），不用 rotate（会重采样）。
        # Pillow ≥10 移除了 Image.ROTATE_180，只剩 Image.Transpose.ROTATE_180 —— 两边都兼容。
        _rot180 = getattr(getattr(Image, "Transpose", None), "ROTATE_180", None) \
            or getattr(Image, "ROTATE_180", None)
        # 横向页铺满纸张用的 90°（同样是 transpose，无损、精确）
        _rot90 = getattr(getattr(Image, "Transpose", None), "ROTATE_90", None) \
            or getattr(Image, "ROTATE_90", None)
        if rotate_180 and _rot180 is None:
            logger.warning("GDI: 当前 Pillow 版本取不到 180° 旋转常量，rotate_180 被忽略")
        elif landscape_fill and _rot90 is None:
            logger.warning("GDI: 取不到 90° 旋转常量，横向页将退回等比缩放（留白边）")

        # 单数页 + 双面 + 多份 → 每份之间需插入空白页，确保副本独立（已在 render_seq 里排好）
        needs_separator = (pages_override is None and duplex == "on" and copies > 1
                           and len(pages_to_print) % 2 == 1)
        sep_count = (copies - 1) if needs_separator else 0
        total_sides = len(render_seq)
        if needs_separator:
            logger.info(f"GDI: 奇数页({len(pages_to_print)}页)双面多份打印，将在副本间插入空白分隔页")
            print(f"[GDI] ⚙ 奇数页({len(pages_to_print)}页) + 双面 + 多份({copies}份) → 副本间插入空白分隔页")

        page_seq = 0
        # 空白占位页复用的极小白色 DIB（懒创建，8×8 拉伸到整页）。
        # 旧实现是每张空白页都按 printable_w × printable_h 新建一张**整页** RGB 位图，而
        # printable_* 取的是打印机原生 DPI 的**设备像素**（600 dpi 的 A4 ≈ 99.6 MiB/张，实测），
        # 比内容页还贵 4 倍。手动双面第 2 趟 / 校准序列里空白占位页可以很多，纯属浪费。
        # 填充结果与源尺寸无关（走的是内容页同一条 StretchDIBits 路径，驱动本就在放大）。
        blank_dib = None
        cap_noted = False        # 「本次打印因上限降了分辨率」只提示一次，别刷屏
        for page_idx in render_seq:
            page_seq += 1

            # ── 空白占位页（手动双面补位 / 副本分隔页）：只送一张白纸，不渲染内容 ──
            if page_idx is None:
                hdc.StartPage()
                page_started = True
                if blank_dib is None:
                    blank_dib = ImageWin.Dib(Image.new("RGB", (8, 8), "white"))
                blank_dib.draw(hdc.GetHandleOutput(), (0, 0, printable_w, printable_h))
                hdc.EndPage()
                print(f"[GDI]   ✓ 第 {page_seq}/{total_sides} 面 (空白占位页)")
                if progress_callback:
                    progress_callback(page_seq, total_sides)
                continue

            page = doc[page_idx]

            # ── 目标物理尺寸 + 实际渲染尺寸 + 要不要整页转 90°（纯函数见 plan_page_draw）──
            # 旧路径先按 400dpi 渲染、再把位图拉伸到 600dpi 设备矩形（1.43× 最近邻 + 0.955 适配），
            # 小字锯齿就来自这两次重采样。现在直接渲染到最终尺寸：默认 1:1，MuPDF 只采样一次。
            page_w_pts = page.rect.width
            page_h_pts = page.rect.height
            angle, draw_w, draw_h, render_w, render_h, capped = plan_page_draw(
                page_w_pts, page_h_pts, native_dpi_x, native_dpi_y,
                printable_w, printable_h, dpi_cap=dpi, pixel_limit=pixel_limit,
                landscape_fill=landscape_fill and _rot90 is not None,
            )
            # 这一面要不要 180° 补偿：给了逐面表就按表（同一批里长边/短边可混排），否则按整趟
            slot_rotate_180 = (bool(rotate_slots[page_seq - 1])
                               if rotate_slots and 0 < page_seq <= len(rotate_slots)
                               else bool(rotate_180))
            if angle and _rot90 is None:
                angle = 0            # 取不到常量 → 退回旧的等比缩放（至少不崩）

            # P0-1: 极端尺寸页面在**渲染前**就拒绝（还没 StartPage → 没出纸，允许上层降级）
            if render_w * render_h > pixel_limit:
                raise RuntimeError(
                    f"PDF 页面尺寸极端巨大（{page_w_pts:.0f}×{page_h_pts:.0f}pt），"
                    f"即使降分辨率仍超 {pixel_limit:,} 像素，已拒绝该页以防内存耗尽"
                )
            if capped and not cap_noted:
                cap_noted = True
                logger.warning(f"GDI: 渲染分辨率受限 → 位图 {render_w}x{render_h}，"
                               f"目标 {draw_w}x{draw_h}（本次打印 GDI 会放大一次）")
                print(f"[GDI] ⚠ 渲染位图 {render_w}×{render_h} → 目标 {draw_w}×{draw_h}"
                      f"（dpi上限/像素上限触发，GDI 将放大一次）")

            # 先渲染、后 StartPage：渲染失败时尚未出纸，可安全降级重试
            # 转 90° 时：位图宽对应页面**高**、位图高对应页面**宽**（转置后才是 draw_*）
            if angle:
                mat = fitz.Matrix(render_h / page_w_pts, render_w / page_h_pts)
            else:
                mat = fitz.Matrix(render_w / page_w_pts, render_h / page_h_pts)
            pix = page.get_pixmap(
                matrix=mat,
                colorspace=fitz.csGRAY if mono else fitz.csRGB,
                alpha=False,
            )

            if mono:
                gray = Image.frombuffer(
                    "L", (pix.width, pix.height),
                    pix.samples, "raw", "L", pix.stride, 1,
                )
                # 图片大面积覆盖 → 抖动保灰阶；纯文字/线条 → 硬阈值保锐度
                img = gray.convert("1") if _page_needs_dither(page) else _to_bilevel(gray)
            else:
                img = Image.frombuffer(
                    "RGB", (pix.width, pix.height),
                    pix.samples, "raw", "RGB", pix.stride, 1,
                )

            # ① 横向页铺满纸张：整页转 90°（与 Word/Adobe「自动旋转」同效）。
            if angle:
                img = img.transpose(_rot90)

            # ② 手动双面第 2 趟：纸张在出纸路径里被转了 180°，必须把这一趟的内容也转 180° 补偿，
            # 否则第 2 面的文字整体倒置（实机实测 HP LaserJet 1020：不补偿就是"上下颠倒"）。
            # 只影响本次渲染的位图，不影响纸张顺序、页序、居中位置。1bit/8bit 都支持无损转置。
            # ①②叠加与顺序无关（90+180 == 180+90 == 270）。
            if slot_rotate_180 and _rot180 is not None:
                img = img.transpose(_rot180)

            # 未降采样时以位图**实际**尺寸为贴图矩形：MuPDF 的 ceil 可能比计算值多 1px，
            # 用计算值会让 StretchDIBits 做一次 1px 的缩放（正好是我们要消灭的东西）。
            if not capped:
                draw_w, draw_h = img.size

            x = (printable_w - draw_w) // 2
            y = (printable_h - draw_h) // 2

            hdc.StartPage()
            page_started = True
            dib = ImageWin.Dib(img)
            dib.draw(hdc.GetHandleOutput(), (x, y, x + draw_w, y + draw_h))
            hdc.EndPage()
            _tags = []
            if angle:
                _tags.append("横向→转90°铺满")
            if slot_rotate_180:
                _tags.append("转180°补偿")
            print(f"[GDI]   ✓ 第 {page_seq}/{total_sides} 面 (PDF p.{page_idx + 1})"
                  + (f" 〔{'/'.join(_tags)}〕" if _tags else ""))
            if progress_callback:
                progress_callback(page_seq, total_sides)

        hdc.EndDoc()
        started = False
        total_pages_printed = len(pages_to_print) * copies
        msg = f"打印成功 (GDI, {len(render_seq)} 面)"
        if needs_separator:
            msg += f" [已插入 {sep_count} 张空白分隔页]"
        if virtual_printer:
            msg += " [虚拟打印机：双面为空操作，按单面输出]"
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
        (devmode, applied_duplex)
          · devmode: 配置好的 PyDEVMODE（驱动校验后），失败返回 None；
          · applied_duplex: **驱动校验后**的 Duplex 实际值（1=SIMPLEX / 2=长边 / 3=短边 / None=拿不到）。
            2026-10 起必须用它判断"双面到底生效没有"：无自动双面的驱动会在
            DocumentProperties 合并时把 Duplex 悄悄改回 1，只看返回值/请求值会误判成功。
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

            # 驱动校验后的实际值（DM_OUT_BUFFER 已就地写回）—— 调用方据此判断是否真的双面
            applied_duplex = getattr(devmode, "Duplex", None)
            if applied_duplex != new_duplex:
                print(f"[DEVMODE] ⚠ 驱动把 Duplex 改回 {duplex_map.get(applied_duplex, applied_duplex)}"
                      f"（请求 {duplex_map.get(new_duplex, new_duplex)}）—— 该设置在驱动侧未生效")
                logger.warning(f"DEVMODE: 驱动改回 Duplex={applied_duplex}（请求 {new_duplex}）")

            # 驱动校验后的 devmode 缓冲，供 ResetDC 使用
            return devmode, applied_duplex
        finally:
            win32print.ClosePrinter(handle)
    except Exception as e:
        print(f"[DEVMODE] ✗ 失败: {e}")
        logger.warning(f"获取/配置打印机 DEVMODE 失败: {e}")
        return None, None


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


def _sumatra_page_tokens(pages: list[int]) -> str:
    """0-based 页码列表 → SumatraPDF 页码范围 token（1-based，连续段压缩）。

    例: [0,1,2,4] → "1-3,5"；[2] → "3"；[] → ""。
    使用解析后的页码（而非用户原始输入）拼串，保证输出一定是官方语法。
    """
    if not pages:
        return ""
    nums = sorted({p + 1 for p in pages if p >= 0})
    if not nums:
        return ""
    parts: list[str] = []
    start = prev = nums[0]
    for n in nums[1:]:
        if n == prev + 1:
            prev = n
            continue
        parts.append(f"{start}-{prev}" if prev > start else f"{start}")
        start = prev = n
    parts.append(f"{start}-{prev}" if prev > start else f"{start}")
    return ",".join(parts)


def build_sumatra_print_settings(
    copies: int = 1,
    duplex: str = "off",
    duplex_mode: str = "long-edge",
    page_range: str = "",
    total_pages: int = 0,
) -> str:
    """按 SumatraPDF 官方 `-print-settings` 语法拼装设置串。

    官方 token（https://www.sumatrapdfreader.org/docs/Command-line-arguments）：
      · 份数：`3x`（**不是** `copies=3`）
      · 双面：`duplex` / `duplexshort` / `duplexlong` / `simplex`（**不是** `duplex=long`）
      · 页码范围：与上述 token 混排的**裸页码**，如 `1-3,5,10-8,odd`（**不是** `range=1-5`）
    非法 token 会被 SumatraPDF 忽略（份数/页码/双面全部失效，改为按 PDF 内嵌默认值打印整份），
    或用做未知文件路径导致整条命令失败——故此处只输出官方 token。

    份数恒显式给出（含 `1x`）：PDF 的 ViewerPreferences 可能内嵌 NumCopies，
    不显式覆盖就会出现「设 1 份却印 N 份」。若某版本不认 `1x`，SumatraPDF 返回非 0，
    调用方会降级到方案 3（应用层循环），仍能正确出纸。
    """
    parts: list[str] = []
    if total_pages > 0 and page_range and page_range.strip():
        tokens = _sumatra_page_tokens(_parse_page_range(page_range, total_pages))
        if tokens:
            parts.append(tokens)
    parts.append(f"{max(1, int(copies))}x")
    if duplex == "on":
        parts.append("duplexshort" if duplex_mode == "short-edge" else "duplexlong")
    else:
        # 显式 simplex：同时覆盖 PDF 内嵌的 Duplex 默认值
        parts.append("simplex")
    return ",".join(parts)


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

    份数/双面/页码范围全部经 `-print-settings` 官方 token 传递。
    **不再**先去改打印机的全局 DEVMODE：那是系统级默认设置的写入，
    会污染用户在 Windows 里配置的打印机偏好，且对 SumatraPDF 本身没有约束力
    （SumatraPDF 用自己提交的 DEVMODE，不读刚写进去的全局值）。

    `orientation` 参数保留仅为签名兼容、此处**刻意不使用**：SumatraPDF 对
    「页宽 > 页高」的页面已默认旋转 90° 适配纸张，再传 `landscape` token 会二次旋转。
    """
    # P2-15: SumatraPDF 超时随页数放大（120s 起步，每页 2s，上限 600s）
    total_pages = get_pdf_info(pdf_path).get("page_count", 0)
    sumatra_timeout = min(max(120, total_pages * 2), 600)

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

    settings = build_sumatra_print_settings(
        copies=copies, duplex=duplex, duplex_mode=duplex_mode,
        page_range=page_range, total_pages=total_pages,
    )

    # -silent：抑制 SumatraPDF 自身的错误弹窗——模态框会阻塞子进程直到超时
    cmd = [sumatra_path, "-print-to", printer_name or "default", "-silent"]

    if settings:
        cmd += ["-print-settings", settings]

    cmd.append(os.path.abspath(pdf_path))

    # SumatraPDF 自己起 StartDoc，作业名取自 PDF 文件名 —— 加不了 HN| 前缀，只能先登记
    # 这个名字（TTL 20 分钟），否则外部打印监视会把它当成外部作业再记一遍账（同一单收两遍钱）。
    try:
        from print_watch import note_self_job
        note_self_job(os.path.basename(pdf_path))
    except Exception:
        pass

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
    pages_override: Optional[list[Optional[int]]] = None,
    rotate_180: bool = False,
    rotate_slots: Optional[list[bool]] = None,
    landscape_fill: bool = True,
) -> tuple[bool, str]:
    """终极兜底：应用层循环打印，每次只打 1 份。成功率最高。

    pages_override / rotate_180 原样透传（手动双面/校准的精确序列与第 2 趟旋转
    必须逐趟保持一致，这里只是把"份数"拆成多次单份调用，序列本身不受影响）。"""
    print(f"[循环打印] 共 {copies} 份，每次 1 份...")
    for i in range(copies):
        print(f"[循环打印] 第 {i + 1}/{copies} 份...")
        ok, msg = _print_pdf_native(pdf_path, printer_name, duplex, duplex_mode, page_range,
                                    1, orientation, progress_callback, dpi, pages_override,
                                    rotate_180, rotate_slots, landscape_fill)
        if not ok:
            print(f"[循环打印] ✗ 第 {i + 1} 份失败: {msg}")
            # 双面不可用是终止型失败：原样上抛，别被改写成"第 N 份失败"而丢掉语义
            if msg.startswith(DUPLEX_UNSUPPORTED_MARKER):
                return False, msg
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
