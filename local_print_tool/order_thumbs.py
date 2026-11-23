# -*- coding: utf-8 -*-
"""本地订单缩略图：打印时渲染、存在本机、由 stats_server 现取现发。

为什么需要（2026-11-19 用户反馈）：收支清算的订单明细里，文件名预览只认云端的
`file_id` + `has_preview`；可**本地单**（本机手动批量打印）上报 `/api/local_orders` 时
根本不带 file_id —— 后端那几行 order_files 是现建的，file_id 为 NULL。于是"本机打的单"
永远点不出缩略图，而云端单又只在客户端下载时上传过一次。

这里的做法不碰云端：打印结束（PDF 已生成）时顺手把**打印实际用的那份 PDF** 渲成
1024px WebP 存进 %APPDATA%\\HN打印工具\\order_thumbs\\<订单号>\\<文件名哈希>[_pN].webp，
结算页改走 `/api/local/order_thumb/<订单号>?name=<文件名>&page=N` —— 离线也能看、
多页可翻，也不占云端存储。

约定：
  · 渲染绝不影响打印主流程（任何异常都吞掉，调用方只能拿到 None/0/[]）；
  · 一个订单的文件按**文件名**定位（同名文件内容相同，共用同一组图就够）；
  · 首页不带后缀（`<key>.webp`），第 N 页 `<key>_pN.webp`；换号时整个目录跟着改名。
"""
import hashlib
import os
import re
import threading

import paths

DIR_NAME = "order_thumbs"
# 单订单最多留几个文件（多页的算一个文件位；几十个文件已超出"快速看一眼"的用途）
MAX_PER_ORDER = 60
# 整个目录的总量上限（超出时按 mtime 清最旧订单，启动/写入时顺手做）
MAX_TOTAL_BYTES = 256 * 1024 * 1024

_SAFE_ORDER_RE = re.compile(r"^[0-9A-Za-z._-]{1,64}$")
_PAGE_SUFFIX_RE = re.compile(r"^(?P<key>[0-9a-f]{20})(?:_p(?P<page>\d{1,3}))?\.(?P<ext>webp|jpg)$")
_cache_lock = threading.Lock()


def thumbs_dir() -> str:
    d = os.path.join(paths.get_app_data_dir(), DIR_NAME)
    os.makedirs(d, exist_ok=True)
    return d


def order_dir(order_number: str, create: bool = True) -> str:
    """订单目录（订单号不合法时用哈希兜底，绝不把用户输入拼进路径）。"""
    num = str(order_number or "").strip()
    if not num:
        return ""
    if not _SAFE_ORDER_RE.match(num):
        num = "x" + hashlib.sha1(num.encode("utf-8", "replace")).hexdigest()[:16]
    d = os.path.join(thumbs_dir(), num)
    if create:
        os.makedirs(d, exist_ok=True)
    return d


def name_key(name: str) -> str:
    """文件名的稳定键（同名文件共用一组图；大小写与路径分隔符不敏感）。"""
    base = os.path.basename(str(name or "").replace("\\", "/")).strip().lower()
    return hashlib.sha1(base.encode("utf-8", "replace")).hexdigest()[:20]


def _file_name(key: str, page: int, fmt: str) -> str:
    e = str(fmt or "").lower().lstrip(".")
    ext = ".webp" if e == "webp" else ".jpg"
    return key + ("" if int(page) <= 1 else "_p%d" % int(page)) + ext


def save_bytes(order_number: str, name: str, data: bytes, fmt: str = "webp",
               page: int = 1) -> str:
    """把**已经编码好的**一页图片写进该订单目录，返回路径（失败返回空串）。"""
    if not data or not name:
        return ""
    d = order_dir(order_number)
    if not d:
        return ""
    path = os.path.join(d, _file_name(name_key(name), page, fmt))
    tmp = path + ".part"
    try:
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return ""
    return path


def find(order_number: str, name: str = "", page: int = 1) -> str:
    """按订单号 + 文件名 + 页找现成的缩略图（没有返回空串）。"""
    d = order_dir(order_number, create=False)
    if not d or not name:
        return ""
    key = name_key(name)
    for ext in ("webp", "jpg"):
        p = os.path.join(d, _file_name(key, page, ext))
        if os.path.isfile(p):
            return p
    return ""


def page_count(order_number: str, name: str = "") -> int:
    """这个文件已存了几页（0 = 没有缩略图）。"""
    d = order_dir(order_number, create=False)
    if not d or not name:
        return 0
    key = name_key(name)
    best = 0
    try:
        for n in os.listdir(d):
            m = _PAGE_SUFFIX_RE.match(n)
            if m and m.group("key") == key:
                best = max(best, int(m.group("page") or 1))
    except OSError:
        return 0
    return best


def load_pages(order_number: str, name: str = "", limit: int = 6,
               edge: int = 640) -> list:
    """读出已落盘的缩略图，按需缩小后给**云端回传**用（2026-11-23）。

    为什么单独一条：本机存的是 1024px（结算页放大那一档），云端要给移动端看，
    单页 60 KB × 20 页 = 1.2 MB/文件，900 个订单就能吃掉几个 G。这里只取前
    `limit` 页并缩到 `edge`（默认 640，与移动端详情卡 p 档同宽）—— 用 PIL 就地
    缩，不重新渲 PDF（重渲一页要 1~3 秒）。
    返回 `[(page_no, bytes, fmt)]`，任何异常都当"没有"处理（缩略图绝不打扰主流程）。
    """
    out = []
    try:
        n = min(int(limit or 0) or 0, page_count(order_number, name))
    except (TypeError, ValueError):
        return []
    if n <= 0:
        return []
    try:
        from PIL import Image
        import io as _io
    except Exception:  # noqa: BLE001  没有 PIL 就原样回传（不至于整条链路断）
        Image = None
        _io = None
    for page_no in range(1, n + 1):
        path = find(order_number, name, page_no)
        if not path:
            continue
        try:
            with open(path, "rb") as fh:
                data = fh.read()
        except OSError:
            continue
        fmt = "webp" if path.lower().endswith(".webp") else "jpeg"
        if Image is not None and edge and edge > 0:
            try:
                img = Image.open(_io.BytesIO(data))
                if max(img.size) > int(edge):
                    img.thumbnail((int(edge), int(edge)), Image.LANCZOS)
                    buf = _io.BytesIO()
                    try:
                        img.save(buf, "WEBP", quality=78, method=4)
                        data, fmt = buf.getvalue(), "webp"
                    except Exception:  # noqa: BLE001  编不出 WebP 就回落 JPEG
                        buf = _io.BytesIO()
                        img.convert("RGB").save(buf, "JPEG", quality=82, optimize=True)
                        data, fmt = buf.getvalue(), "jpeg"
            except Exception:  # noqa: BLE001  缩不了就把原图传上去
                pass
        out.append((page_no, data, fmt))
    return out


def _drop_key(d: str, key: str) -> None:
    """同一文件重渲前先清掉旧版本（页数变少时不留残页）。"""
    try:
        for n in os.listdir(d):
            m = _PAGE_SUFFIX_RE.match(n)
            if m and m.group("key") == key:
                try:
                    os.remove(os.path.join(d, n))
                except OSError:
                    pass
    except OSError:
        pass


def save_pages(order_number: str, name: str, pages) -> int:
    """把多页结果 `[(page_no, bytes, fmt)]` 落盘，返回落盘页数。"""
    if not name or not pages:
        return 0
    d = order_dir(order_number)
    if not d:
        return 0
    _drop_key(d, name_key(name))
    done = 0
    for item in pages:
        try:
            page_no, data, fmt = item
        except (TypeError, ValueError):
            continue
        if save_bytes(order_number, name, data, fmt, page=page_no):
            done += 1
    return done


def render_pages(order_number: str, display_name: str, file_path: str = "",
                 pdf_path: str = "") -> int:
    """从**实际打印用的 PDF** 渲染全部（≤ MAX_PAGES）页并落盘，返回页数（绝不抛）。"""
    if not display_name:
        return 0
    try:
        import cloud_thumb            # 复用同一套编码（1024px / WebP q80 / JPEG 兜底）
        pages = cloud_thumb.make_pages(file_path, display_name, pdf_path=pdf_path)
        return save_pages(order_number, display_name, pages)
    except Exception:  # noqa: BLE001  缩略图失败绝不影响打印
        return 0


def render_job(order_number: str, display_name: str, file_path: str = "",
               pdf_path: str = "") -> str:
    """给一个任务渲染多页缩略图并落盘，返回首页路径（失败返回空串，绝不抛）。"""
    if not render_pages(order_number, display_name, file_path, pdf_path):
        return ""
    return find(order_number, display_name, page=1)


def render_order(order_number: str, files, limit: int = MAX_PER_ORDER) -> int:
    """批量渲染（`files` 是 dict：name / path / pdf）。返回成功张数。"""
    num = str(order_number or "").strip()
    if not num:
        return 0
    done = 0
    for f in list(files or [])[:max(1, int(limit or MAX_PER_ORDER))]:
        if not isinstance(f, dict):
            continue
        name = str(f.get("name") or f.get("display_name") or "").strip()
        if not name:
            continue
        if find(num, name):
            done += 1
            continue
        if render_pages(num, name, str(f.get("path") or ""), str(f.get("pdf") or "")):
            done += 1
    if done:
        _prune()
    return done


def order_has_thumbs(order_number: str) -> bool:
    d = order_dir(order_number, create=False)
    if not d:
        return False
    try:
        return any(not n.endswith(".part") for n in os.listdir(d))
    except OSError:
        return False


def rename_order(old: str, new: str) -> str:
    """订单换号（离线单 -L → 云端号）时把缩略图目录一起改名/合并，返回新目录。

    目标目录已存在同文件时保留目标那份、删掉旧的 —— 两边都是同一单的图，
    谁先谁后只影响分辨率新旧，不影响正确性。
    """
    a = order_dir(old, create=False)
    b = order_dir(new)
    if not a or not b or os.path.normcase(a) == os.path.normcase(b):
        return b
    if not os.path.isdir(a):
        return b
    try:
        for n in list(os.listdir(a)):
            if n.endswith(".part"):
                continue
            src = os.path.join(a, n)
            dst = os.path.join(b, n)
            try:
                if os.path.exists(dst):
                    os.remove(src)
                else:
                    os.replace(src, dst)
            except OSError:
                continue
        try:
            os.rmdir(a)
        except OSError:
            pass
    except OSError:
        return ""
    return b


def _prune() -> None:
    """总量控制：超过 MAX_TOTAL_BYTES 时按订单目录的 mtime 从旧往新删（尽力而为）。"""
    with _cache_lock:
        root = thumbs_dir()
        try:
            entries = [os.path.join(root, n) for n in os.listdir(root)]
        except OSError:
            return
        dirs, total = [], 0
        for d in entries:
            if not os.path.isdir(d):
                continue
            size = 0
            try:
                for dirpath, _dirs, names in os.walk(d):
                    for n in names:
                        size += os.path.getsize(os.path.join(dirpath, n))
            except OSError:
                continue
            total += size
            dirs.append((os.path.getmtime(d), size, d))
        if total <= MAX_TOTAL_BYTES:
            return
        dirs.sort()
        for _mtime, size, d in dirs:
            if total <= MAX_TOTAL_BYTES:
                break
            try:
                for dirpath, _dirs, names in os.walk(d, topdown=False):
                    for n in names:
                        os.remove(os.path.join(dirpath, n))
                    os.rmdir(dirpath)
            except OSError:
                continue
            total -= size
