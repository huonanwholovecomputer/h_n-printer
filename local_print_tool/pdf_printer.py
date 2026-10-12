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
import threading
import time
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# P1-9: 标记 GDI 打印已中途出纸（StartPage 已执行）。带此前缀的失败消息不得再降级整单重打。
MID_PRINT_MARKER = "[已出纸] "

# P0(2026-10): 标记"驱动不接受双面设置"。带此前缀的失败消息不得降级到 Sumatra/循环——
# 那些路径同样依赖驱动，只会静默单面出纸，等于把"没双面却报成功"换个地方重演。
DUPLEX_UNSUPPORTED_MARKER = "[双面不可用] "


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
# 打印机能力探测：是否支持**自动**双面
# ============================================================

DC_DUPLEX = 7  # wingdi.h: DeviceCapabilities 的能力位

# 能力缓存：键 =（打印队列名, 驱动名, 端口）小写 —— 改名/换驱动/换端口即失效重探
_DUPLEX_CAP_CACHE: dict[tuple, dict] = {}
_DUPLEX_CAP_LOCK = threading.Lock()


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
          "detail": str,                    # 人类可读结论（日志用；不输出 token 之类）
        }

    缓存按（队列名+驱动名+端口）：`refresh=True` 强制重探。
    """
    printer = (printer_name or "").strip()
    out = {"auto_duplex": None, "confidence": "low", "printer": printer,
           "driver": "", "port": "", "dc_duplex": None, "dm_accepted": None, "detail": ""}
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
        pages_override: **手动双面/校准专用**：精确的逐面序列（0-based 页号，None=空白占位页）。
                        给出时只用 GDI 一条路（Sumatra 只认整份 PDF，降级过去会把整份文件
                        按它自己的页序又打一遍 —— 那正是手动双面最怕的重复/错位）。
        rotate_180: 内容整体旋转 180°（手动双面第 2 趟用，补偿纸路径的 180° 翻转）。
                    一般不要直接调它 —— 用 print_plan_pass()，那里把这条规则封在一处。

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

    # ── 请求双面时先核一遍打印机能力（防"静默打成单面却报成功"）──
    # 依据：实测无自动双面的驱动会把 DEVMODE 的 Duplex 改回 1(SIMPLEX)，此时任何策略
    # （GDI/Sumatra/循环）都会单面出纸，而客户端按双面计费 → 店里出双倍纸。
    # 只在两条证据都指向"不支持"（confidence=high）时才拒打，避免误伤探测失败的机型；
    # 判不准或证据不足时照常打，但留下日志。
    if duplex == "on" and system == "Windows" and pages_override is None:
        verdict = get_printer_duplex_support(printer_name)
        if verdict.get("auto_duplex") is False and verdict.get("confidence") == "high":
            msg = (f"该打印机不支持自动双面（{verdict.get('detail')}）："
                   f"{verdict.get('printer')}。请改用单面打印；"
                   f"若要双面，需走手动双面流程（人工翻纸）")
            logger.error(f"拒绝双面打印: {msg}")
            print(f"[打印] ✗ {msg}")
            return False, msg
        if verdict.get("auto_duplex") is None:
            logger.warning(f"双面能力判不准，按请求继续打印: {verdict.get('detail')}")
            print(f"[打印] ⚠ 双面能力判不准（{verdict.get('detail')}），仍按双面提交")

    if system == "Windows":
        # 方案 1: Windows 原生 GDI 打印（驱动级双面/份数控制，最可靠）
        print("[打印] 方案1: 尝试 Windows 原生 GDI 打印...")
        ok, msg = _print_pdf_native(pdf_path, printer_name, duplex, duplex_mode, page_range, copies, orientation, progress_callback, dpi, pages_override, rotate_180)
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
                                  orientation, progress_callback, dpi, pages_override, rotate_180)
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
    """
    which = 2 if int(which) == 2 else 1
    return print_pdf(
        pdf_path, printer_name, copies=1, duplex="off",
        pages_override=plan.pass_page_indices(which),
        rotate_180=(which == 2 and second_rotate_180),
        progress_callback=progress_callback, dpi=dpi,
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
) -> tuple[bool, str]:
    """
    Windows 原生 GDI 打印：PyMuPDF 渲染 + win32ui 输出到打印机。
    progress_callback: 每完成一面的 EndPage 后调用 (current_side, total_sides)
    dpi: 渲染 DPI，0=使用打印机原生 DPI
    pages_override: **手动双面/校准专用** —— 精确的逐面渲染序列（0-based 页号，
                    None = 打一张空白占位页）。给出时忽略 page_range/copies，
                    copies 由调用方自己循环（见 manual_duplex.PrintPlan.pass_pages）。
    rotate_180: 把本次所有内容页渲染后旋转 180°（空白页不受影响）。**手动双面第 2 趟必须开**：
                纸张在出纸路径里被转了 180°，不补偿则第 2 面的文字整体倒置
                （HP LaserJet 1020 实机实测确认；HP 驱动自己分两趟时也在内部做了这个补偿）。
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
        if duplex == "on" and devmode is not None and applied_duplex not in (2, 3):
            msg = (f"驱动未采纳双面设置（Duplex={applied_duplex}）—— 该打印机不支持自动双面，"
                   f"继续打只会单面出纸: {printer}")
            logger.error(f"GDI: {msg}")
            print(f"[GDI] ✗ {msg}")
            return False, DUPLEX_UNSUPPORTED_MARKER + msg

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

        # 180° 原位旋转用 transpose（无损、精确），不用 rotate（会重采样）。
        # Pillow ≥10 移除了 Image.ROTATE_180，只剩 Image.Transpose.ROTATE_180 —— 两边都兼容。
        _rot180 = getattr(getattr(Image, "Transpose", None), "ROTATE_180", None) \
            or getattr(Image, "ROTATE_180", None)
        if rotate_180 and _rot180 is None:
            logger.warning("GDI: 当前 Pillow 版本取不到 180° 旋转常量，rotate_180 被忽略")

        # 单数页 + 双面 + 多份 → 每份之间需插入空白页，确保副本独立（已在 render_seq 里排好）
        needs_separator = (pages_override is None and duplex == "on" and copies > 1
                           and len(pages_to_print) % 2 == 1)
        sep_count = (copies - 1) if needs_separator else 0
        total_sides = len(render_seq)
        if needs_separator:
            logger.info(f"GDI: 奇数页({len(pages_to_print)}页)双面多份打印，将在副本间插入空白分隔页")
            print(f"[GDI] ⚙ 奇数页({len(pages_to_print)}页) + 双面 + 多份({copies}份) → 副本间插入空白分隔页")

        page_seq = 0
        for page_idx in render_seq:
            page_seq += 1

            # ── 空白占位页（手动双面补位 / 副本分隔页）：只送一张白纸，不渲染内容 ──
            if page_idx is None:
                hdc.StartPage()
                page_started = True
                blank_img = Image.new("RGB", (printable_w, printable_h), "white")
                blank_dib = ImageWin.Dib(blank_img)
                blank_dib.draw(hdc.GetHandleOutput(), (0, 0, printable_w, printable_h))
                hdc.EndPage()
                print(f"[GDI]   ✓ 第 {page_seq}/{total_sides} 面 (空白占位页)")
                if progress_callback:
                    progress_callback(page_seq, total_sides)
                continue

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

            # 手动双面第 2 趟：纸张在出纸路径里被转了 180°，必须把这一趟的内容也转 180° 补偿，
            # 否则第 2 面的文字整体倒置（实机实测 HP LaserJet 1020：不补偿就是"上下颠倒"）。
            # 只影响本次渲染的位图，不影响纸张顺序、页序、居中位置。
            if rotate_180 and _rot180 is not None:
                img = img.transpose(_rot180)

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
            print(f"[GDI]   ✓ 第 {page_seq}/{total_sides} 面 (PDF p.{page_idx + 1})")
            if progress_callback:
                progress_callback(page_seq, total_sides)

        hdc.EndDoc()
        started = False
        total_pages_printed = len(pages_to_print) * copies
        msg = f"打印成功 (GDI, {len(render_seq)} 面)"
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
) -> tuple[bool, str]:
    """终极兜底：应用层循环打印，每次只打 1 份。成功率最高。

    pages_override / rotate_180 原样透传（手动双面/校准的精确序列与第 2 趟旋转
    必须逐趟保持一致，这里只是把"份数"拆成多次单份调用，序列本身不受影响）。"""
    print(f"[循环打印] 共 {copies} 份，每次 1 份...")
    for i in range(copies):
        print(f"[循环打印] 第 {i + 1}/{copies} 份...")
        ok, msg = _print_pdf_native(pdf_path, printer_name, duplex, duplex_mode, page_range,
                                    1, orientation, progress_callback, dpi, pages_override,
                                    rotate_180)
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
