# -*- coding: utf-8 -*-
"""本地订单「首页缩略图」：打印时渲染、存在本机、由 stats_server 现取现发。

为什么需要（2026-11-19 用户反馈）：收支清算的订单明细里，文件名预览只认云端的
`file_id` + `has_preview`；可**本地单**（本机手动批量打印）上报 `/api/local_orders` 时
根本不带 file_id —— 后端那几行 order_files 是现建的，file_id 为 NULL。于是"本机打的单"
永远点不出缩略图，而云端单又只在客户端下载时上传过一次。

这里的做法不碰云端：打印结束（PDF 已生成）时顺手把首页渲染成 640px WebP 存进
%APPDATA%\\HN打印工具\\order_thumbs\\<订单号>\\<文件名哈希>.webp，结算页改走
`/api/local/order_thumb/<订单号>?name=<文件名>` —— 离线也能看，也不占云端存储。

约定：
  · 渲染绝不影响打印主流程（任何异常都吞掉，调用方只能拿到 None/0）；
  · 一个订单的文件按**文件名**定位（同名文件内容相同，共用一张图就够）；
  · 只出第 1 页（结算页的"略缩图"就是这个用途，翻页仍走云端预览）。
"""
import hashlib
import os
import re
import threading

import paths

DIR_NAME = "order_thumbs"
# 单订单最多留几张：一单几十个文件已经超出"快速看一眼"的用途，太多也别把磁盘塞满
MAX_PER_ORDER = 60
# 整个目录的总量上限（超出时按 mtime 清最旧订单，启动/写入时顺手做）
MAX_TOTAL_BYTES = 256 * 1024 * 1024

_SAFE_ORDER_RE = re.compile(r"^[0-9A-Za-z._-]{1,64}$")
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
    """文件名的稳定键（同名文件共用一张图；大小写与路径分隔符不敏感）。"""
    base = os.path.basename(str(name or "").replace("\\", "/")).strip().lower()
    return hashlib.sha1(base.encode("utf-8", "replace")).hexdigest()[:20]


def save_bytes(order_number: str, name: str, data: bytes, fmt: str = "webp") -> str:
    """把**已经编码好的**图片字节写进该订单目录，返回路径（失败返回空串）。"""
    if not data or not name:
        return ""
    d = order_dir(order_number)
    if not d:
        return ""
    ext = ".webp" if str(fmt).lower() == "webp" else ".jpg"
    path = os.path.join(d, name_key(name) + ext)
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


def find(order_number: str, name: str = "") -> str:
    """按订单号 + 文件名找现成的缩略图（没有返回空串）。"""
    d = order_dir(order_number, create=False)
    if not d or not name:
        return ""
    key = name_key(name)
    for ext in (".webp", ".jpg"):
        p = os.path.join(d, key + ext)
        if os.path.isfile(p):
            return p
    return ""


def render_job(order_number: str, display_name: str, file_path: str = "",
               pdf_path: str = "") -> str:
    """给一个任务渲染首页缩略图并落盘（失败返回空串，绝不抛）。"""
    try:
        import cloud_thumb            # 复用同一套编码（640px / WebP q80 / JPEG 兜底）
        made = cloud_thumb.make_thumb(file_path, display_name, pdf_path=pdf_path)
        if not made:
            return ""
        data, fmt = made
        return save_bytes(order_number, display_name or file_path, data, fmt)
    except Exception:  # noqa: BLE001  缩略图失败绝不影响打印
        return ""


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
        if render_job(num, name, str(f.get("path") or ""), str(f.get("pdf") or "")):
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
