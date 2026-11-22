# -*- coding: utf-8 -*-
"""云端订单文件的缩略图（2026-11-19 阶段二；2026-11-21 改为 1024 多页）。

为什么放在本地打印工具里做：

* 服务端渲染 .doc/.xls 必须串行起 soffice（线上 2GB 内存只许一个实例，超时 120s），
  而本地工具本来就要把同一份文件转成 PDF 打印 —— 顺手光栅化几页几乎零成本；
* **必须从「实际打印用的那份 PDF」出图**：本地 Word/WPS 转出来的版式、字体、分页
  才是用户真正打印出来的样子；服务端 LibreOffice 重渲同一份 docx 会换字体、换换行，
  缩略图跟纸面不一致（2026-11-21 用户现场反馈）。
* 纯文本 txt/csv/md 服务端**没有**「首页」这个概念（`_has_preview_for()` 明确不含），
  只有本地能渲：这里用 PIL 排一页文本，缩略图就长成「文件第一页的样子」；
* 缩略图跟文件保留期同删（服务端 `_drop_previews` 按前缀清理），本地不承担云端存储。

契约（与后端 `POST /api/local/file_thumb` 对应）：

* 长边 1024（后端 z 档 = 结算页「放大」那一档），WebP q80，编不出才回落 JPEG；
* 每页一个 `(page_no, bytes, fmt)`，页号 1 基；只传前 `MAX_PAGES` 页；
* 服务端按**真实字节**判定格式，所以这里返回 `(bytes, fmt)`，不要凭扩展名猜；
* 任何一步失败都返回 None/[] —— 缩略图是锦上添花，**绝不允许**影响下载/打印主流程。
"""
from __future__ import annotations

import io
import os

# 与后端 _PREVIEW_SIZES["z"] 同档（1024）。三档 t=320/p=640/z=1024 由服务端下采样，
# 客户端只备最大的一档，避免同一页传三份。
THUMB_EDGE = 1024
JPEG_QUALITY = 82
WEBP_QUALITY = 80
# 一单最多传多少页。超过这个数目的文档，放大查看前 20 页足够定位，剩下的翻页按需
# 由服务端渲（服务端有 10 分钟失败负缓存 + 全局渲染锁，不会把机器拖死）。
MAX_PAGES = 20

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp", ".tif", ".tiff"}
TEXT_EXTS = {".txt", ".csv", ".md"}
# 这些类型必须先把文件转成 PDF 才能出图；转不出来（没有缓存）就交给服务端自己渲。
OFFICE_EXTS = {".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx"}

_CJK_FONT_CANDIDATES = (
    r"C:\Windows\Fonts\msyh.ttc",       # 微软雅黑
    r"C:\Windows\Fonts\msyhl.ttc",
    r"C:\Windows\Fonts\simsun.ttc",     # 宋体
    r"C:\Windows\Fonts\simhei.ttf",
)


def _webp_ready() -> bool:
    try:
        from PIL import features
        return bool(features.check("webp"))
    except Exception:  # noqa: BLE001  Pillow 缺失/异常都当不支持
        return False


class _Noop:
    """拿不到 MuPDF 最小线宽 API 时的空操作（缩略图宁可不修，也不能因此做不出来）。"""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _min_line_width(width: float):
    """渲染缩略图时临时抬高 MuPDF 最小线宽（设备像素）。

    实现借自 `pdf_printer._MinLineWidth`（同一把进程级锁、进出恢复原值）：MuPDF 的
    这个设置是全局的，打印线程与缩略图线程同时在渲染，不能各自裸改。
    """
    try:
        from pdf_printer import _MinLineWidth
        return _MinLineWidth(float(width))
    except Exception:  # noqa: BLE001
        return _Noop()


def _encode(im, edge: int = THUMB_EDGE):
    """等比缩到长边 ≤ edge 并编码，返回 `(bytes, "webp"|"jpeg")`。"""
    from PIL import Image

    if im.mode not in ("RGB", "L"):
        im = im.convert("RGB")
    w, h = im.size
    if max(w, h) > edge:
        scale = edge / float(max(w, h))
        im = im.resize((max(1, int(round(w * scale))), max(1, int(round(h * scale)))),
                       Image.LANCZOS)
    buf = io.BytesIO()
    if _webp_ready():
        try:
            im.save(buf, "WEBP", quality=WEBP_QUALITY, method=4)
            return buf.getvalue(), "webp"
        except Exception:  # noqa: BLE001  个别图编不出 WebP 就退回 JPEG
            buf = io.BytesIO()
    im.save(buf, "JPEG", quality=JPEG_QUALITY, optimize=True)
    return buf.getvalue(), "jpeg"


def _from_image(path: str):
    from PIL import Image

    with Image.open(path) as im:
        im.load()
        return _encode(im)


def _pages_from_pdf(path: str, max_pages: int = MAX_PAGES, edge: int = THUMB_EDGE):
    """PDF：用 PyMuPDF 渲前 max_pages 页（与后端同一个渲染器，版式一致）。

    返回 `[(page_no, bytes, fmt)]`（1 基）；空/损坏 PDF 返回 []。
    """
    import fitz

    out = []
    doc = fitz.open(path)
    try:
        total = min(int(doc.page_count or 0), max(1, int(max_pages)))
        for i in range(total):
            page = doc.load_page(i)
            # 目标长边 edge：page.rect 是 pt（72dpi），zoom = edge / 长边
            long_side = max(page.rect.width, page.rect.height) or 1.0
            zoom = max(0.2, min(4.0, edge / long_side))
            # 细线保真（2026-11-19 现场）：CAD/工程图里 stroke width = 0 的线在缩略图上
            # 只有零点几个像素，抗锯齿后几乎全透明 ⇒ 缩略图一片白。把最小线宽抬到 1 个
            # 设备像素，线条至少占满一格，缩略图才看得出是一张图纸（正文文字本来也 ≥1px）。
            with _min_line_width(1.0):
                pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
            from PIL import Image
            im = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            enc = _encode(im, edge)
            if enc:
                out.append((i + 1, enc[0], enc[1]))
    finally:
        doc.close()
    return out


def _from_pdf(path: str):
    """PDF 首页（兼容旧调用方；多页请用 `_pages_from_pdf`）。"""
    pages = _pages_from_pdf(path, max_pages=1)
    return (pages[0][1], pages[0][2]) if pages else None


def _load_font(size: int):
    from PIL import ImageFont

    for p in _CJK_FONT_CANDIDATES:
        try:
            if os.path.isfile(p):
                return ImageFont.truetype(p, size)
        except Exception:  # noqa: BLE001
            continue
    try:
        return ImageFont.load_default(size)
    except Exception:  # noqa: BLE001  Pillow<10.1 的 default 不接受 size
        return ImageFont.load_default()


def _from_text(path: str, name: str = ""):
    """纯文本/CSV/Markdown：排一页「文件第一屏」，让缩略图有内容可看。

    不追求排版还原（前端本来也没法预览这些类型），只要让人一眼认出
    「这是我传的那份表格/笔记」即可：白底、等宽感字体、自动折行、超出裁剪。
    画布就是 724×1024（A4 比例、长边正好 1024，编码时不再缩放）。
    """
    from PIL import Image, ImageDraw

    W, H = 724, 1024
    margin = 48
    line_h = 32
    try:
        with open(path, "rb") as fh:
            raw = fh.read(64 * 1024)
        text = raw.decode("utf-8", errors="replace")
    except OSError:
        return None
    if not text.strip():
        text = "（空文件）"
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\t", "    ")

    font = _load_font(24)
    title_font = _load_font(28)
    im = Image.new("RGB", (W, H), (255, 255, 255))
    draw = ImageDraw.Draw(im)
    y = margin
    if name:
        draw.text((margin, y), os.path.basename(name)[:64], font=title_font,
                  fill=(40, 40, 46))
        y += 56
    max_w = W - margin * 2
    for line in text.split("\n"):
        while line:
            # 逐字符折行（中英混排按像素宽度）
            cut = len(line)
            while cut > 0 and draw.textlength(line[:cut], font=font) > max_w:
                cut -= 1
            cut = max(1, cut)
            if y + line_h > H - margin:
                break
            draw.text((margin, y), line[:cut], font=font, fill=(70, 70, 78))
            y += line_h
            line = line[cut:]
        if y + line_h > H - margin:
            break
    return _encode(im)


def make_thumb(path: str, name: str = "", pdf_path: str = ""):
    """返回首页 `(bytes, fmt)`；做不出来返回 None（调用方必须容忍）。

    `pdf_path`：该文件已转换好的 PDF（打印/页数分析顺手留下的缓存）。
    Office 文档只有拿到它才能出图 —— 这里**不**自己去起 Word/WPS/LibreOffice，
    否则会和打印抢同一个 COM 实例池。
    """
    pages = make_pages(path, name=name, pdf_path=pdf_path, max_pages=1)
    return (pages[0][1], pages[0][2]) if pages else None


def make_pages(path: str, name: str = "", pdf_path: str = "",
               max_pages: int = MAX_PAGES, edge: int = THUMB_EDGE):
    """返回 `[(page_no, bytes, fmt)]`（1 基页号）；做不出来返回 []。

    与 `make_thumb` 同一套取图口径，只是多页。图片/文本永远只有 1 页；PDF（含 Office
    转换出来的 PDF）按实际页数，最多 `max_pages` 页。Office 同样**只认**已转换的
    `pdf_path` —— 现起 Word/LibreOffice 会和打印抢 COM 实例池。
    """
    if not path or not os.path.isfile(path):
        return []
    ext = os.path.splitext(name or path)[1].lower()
    try:
        if ext in IMAGE_EXTS:
            one = _from_image(path)
            return [(1, one[0], one[1])] if one else []
        if ext in TEXT_EXTS:
            one = _from_text(path, name)
            return [(1, one[0], one[1])] if one else []
        if ext == ".pdf":
            return _pages_from_pdf(path, max_pages, edge)
        if ext in OFFICE_EXTS:
            if pdf_path and os.path.isfile(pdf_path):
                return _pages_from_pdf(pdf_path, max_pages, edge)
            return []
        # 未知类型：图片能打开就当图片（上传白名单里有 tiff 这类扩展名不常见写法）
        try:
            one = _from_image(path)
            return [(1, one[0], one[1])] if one else []
        except Exception:  # noqa: BLE001
            return []
    except Exception:  # noqa: BLE001  缩略图失败绝不影响主流程
        return []
