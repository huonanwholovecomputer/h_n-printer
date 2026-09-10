"""
staging.py — 添加文件的「工作副本」暂存管理（压缩包拖拽兼容）

问题背景
--------
从压缩包（zip/rar/7z）里直接把文件拖进列表时，资源管理器给的并不是压缩包内的条目，
而是解压软件在系统临时目录里解出来的文件，例如：

    C:\\Users\\xxx\\AppData\\Local\\Temp\\Rar$DIa0.123\\报告.docx

这个文件的生命周期由解压软件决定：解压软件退出、临时目录被磁盘清理/安全软件清理、
系统重启后它就消失了。列表里的任务还指着这条路径，用户点「开始打印」时才报
「文件不存在」，现场很难理解。

对策
----
1. 任何加入任务列表的文件，先由 stage_file() 复制一份到程序自己的暂存目录：

       %APPDATA%\\HN打印工具\\file_cache\\<源路径哈希>\\<原文件名>

   列表中的 PrintJob.file_path 只用这份副本（打印/转换/数页数都读副本），
   PrintJob.source_path 记住用户原来的路径（双击打开、界面显示路径用）。
2. 副本按「引用」存活：还挂在任务上的副本永不被清理；任务被移除/清空/打印完成后，
   由 sweep() 回收 —— 启动、添加、移除、清空撤回窗口结束、退出时各扫一次。
3. 暂存目录刻意不放在 %TEMP% 下：系统清理 %TEMP% 正是本问题的一部分；放 %APPDATA%
   才能扛住重启与磁盘清理，崩溃重启后未完成的任务仍能继续打印。

目录结构用「每个源路径一个子目录 + 保留原文件名」，是为了让显示名/报表名/封面页/
转换输出名（各处都直接取 basename）零改动沿用原文件名。

本模块只做文件操作，不依赖 Qt；任何失败都退化为「返回原路径」，功能不因暂存而不可用。
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import threading
import time
from typing import Iterable

import paths

logger = logging.getLogger(__name__)

# 副本文件名里保留的原始主名最大长度（防超长路径触碰 MAX_PATH）
_MAX_STEM = 120
# 复制中途改用 .part 后缀，扫尾时按「未被引用」一并清掉
_PART_SUFFIX = ".part"


# ── 路径工具 ──

def staging_root() -> str:
    """暂存目录根：%APPDATA%\\HN打印工具\\file_cache"""
    return paths.file_cache_dir()


def _norm(path: str) -> str:
    """规范化用于比较的路径（绝对 + Windows 大小写不敏感）。"""
    return os.path.normcase(os.path.abspath(path))


def is_staged(path: str) -> bool:
    """判断路径是否位于暂存目录内（暂存目录里的文件不再二次复制）。"""
    if not path:
        return False
    try:
        root = _norm(staging_root())
        p = _norm(path)
    except (OSError, ValueError):
        return False
    return p == root or p.startswith(root + os.sep)


def _safe_basename(name: str) -> str:
    """把文件名净化成磁盘安全形式（仅取 basename、替换非法字符、截断超长主名）。"""
    base = os.path.basename(name or "") or "file"
    base = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", base).strip() or "file"
    stem, ext = os.path.splitext(base)
    if len(stem) > _MAX_STEM:
        stem = stem[:_MAX_STEM]
    if len(ext) > 16:
        ext = ext[:16]
    return (stem or "file") + ext


def staged_path_for(src: str) -> str:
    """源文件 → 对应的副本路径（同一源路径恒定映射到同一副本，便于复用/去重）。"""
    tag = hashlib.sha1(_norm(src).encode("utf-8", "replace")).hexdigest()[:12]
    return os.path.join(staging_root(), tag, _safe_basename(src))


def cloud_download_path(task_id: int, original_name: str) -> str:
    """云端任务的下载目标路径：暂存目录下 cloud_<task_id>\\<原文件名>。

    云端任务的文件原先下载到 %TEMP%\\hn_cloud_*：临时目录被系统清理/安全软件清理/
    重启后文件消失，而预约打印可能要等数小时才到点，中途文件没了就会「文件不存在」；
    旧版「启动时清理 24h 以上 hn_cloud_*」也会误删仍在列表里的任务文件。
    下载到暂存目录后，这些文件与本地添加的文件享受同一套按引用的保护与清理。
    """
    try:
        tag = f"cloud_{int(task_id)}"
    except (TypeError, ValueError):
        tag = "cloud_unknown"
    return os.path.join(staging_root(), tag, _safe_basename(original_name))


def ensure_dir_for(path: str) -> bool:
    """确保 path 的父目录存在，返回是否可用（不可用则调用方走降级路径）。"""
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        return bool(parent)
    except OSError as e:
        logger.warning(f"暂存子目录不可用: {path} → {e}")
        return False


# ── 「使用中」标记（下载/写入中的文件，清扫时必须保留） ──

_reserved: set[str] = set()
_reserved_lock = threading.Lock()

# 空目录宽限：新建的副本/下载子目录在写入前是空的，清扫不能顺手把它删掉
# （否则紧随其后的 open(dest,"wb") 会因目录消失直接失败）。目录本身不占空间，
# 留到下次清扫再删无妨。
_EMPTY_DIR_GRACE = 300.0


def reserve(path: str) -> None:
    """标记路径正在被写入/使用（例如云端任务正在下载）。

    清扫只看「有没有任务引用」，而下载完成到挂到任务上之间有一个短暂窗口
    （写缓存、emit 事件等），此时文件已关闭却还没人引用 —— 万一这时触发清扫
    （用户移除任务/清空列表/退出）就会把刚下好的文件删掉。用显式标记兜住这个窗口。
    进程退出即失效（崩溃遗留交给启动清扫的宽限期兜底）。"""
    if not path:
        return
    try:
        key = _norm(path)
    except (OSError, ValueError):
        return
    with _reserved_lock:
        _reserved.add(key)


def unreserve(path: str) -> None:
    """解除「使用中」标记（下载结束/文件已挂到任务上或已删除时调用）。"""
    if not path:
        return
    try:
        key = _norm(path)
    except (OSError, ValueError):
        return
    with _reserved_lock:
        _reserved.discard(key)


def _reserved_snapshot() -> set[str]:
    with _reserved_lock:
        return set(_reserved)


# ── 复制（入列表前的暂存） ──

def stage_file(src: str) -> str:
    """把源文件复制一份到暂存目录，返回供任务使用的路径。

    返回原路径的情形（功能照旧，只是没有副本保护）：
      - src 为空/不存在
      - src 已经在暂存目录内（无需二次复制）
      - 复制失败（磁盘满/无权限/文件被独占），此时记日志并回退原路径

    同一源路径已有同尺寸、且不比源文件旧的副本 → 直接复用，不重复复制。
    """
    if not src:
        return src
    try:
        src = os.path.abspath(src)
    except (OSError, ValueError):
        return src
    if is_staged(src) or not os.path.isfile(src):
        return src

    dst = ""
    try:
        dst = staged_path_for(src)
        st = os.stat(src)
        if os.path.isfile(dst):
            dt = os.stat(dst)
            if dt.st_size == st.st_size and dt.st_mtime >= st.st_mtime:
                return dst  # 同源同内容的旧副本仍在 → 复用
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        tmp = dst + _PART_SUFFIX
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
        shutil.copy2(src, tmp)          # 先写 .part，避免半截副本被当成可用副本
        try:
            os.replace(tmp, dst)
        except OSError:
            # 目标被占用（例如正在打印同一副本）→ 退化为直接覆盖，失败则清理 .part
            shutil.copy2(src, dst)
            try:
                os.remove(tmp)
            except OSError:
                pass
        if st.st_size > 20 * 1024 * 1024:
            logger.info(f"已暂存大文件副本 {os.path.basename(src)} ({st.st_size / 1048576:.1f} MB)")
        return dst
    except Exception as e:
        # 磁盘满/无权限/路径非法等一律回退用原文件：暂存只是增强，绝不能挡住「添加文件」
        logger.warning(f"暂存副本创建失败（将直接使用原文件）: {src} → {e}")
        try:
            if dst and os.path.isfile(dst + _PART_SUFFIX):
                os.remove(dst + _PART_SUFFIX)
        except OSError:
            pass
        return src


# ── 清理（引用计数清扫） ──

def sweep(keep_paths: Iterable[str], min_age_seconds: float = 0.0) -> int:
    """删除暂存目录中不再被引用的副本，返回删除数量。

    keep_paths：仍然被任务引用的副本路径集合（调用方从各标签页/撤回备份/打印批次/
                未落地的云端任务收集）。
    min_age_seconds：只清理「修改时间早于该秒数」的文件，用于启动兜底时留出宽限，
                     避免误删别的实例/刚写好还没来得及挂到任务上的副本。
    另外，被 reserve() 标记为「使用中」的文件（下载/写入中）一律保留。
    """
    try:
        root = staging_root()
    except Exception as e:
        logger.warning(f"暂存目录不可用，跳过清理: {e}")
        return 0
    if not os.path.isdir(root):
        return 0
    reserved = _reserved_snapshot()
    keep = {_norm(p) for p in keep_paths if p} | reserved
    now = time.time()
    removed = 0

    def _try_remove(p: str) -> None:
        nonlocal removed
        try:
            if _norm(p) in keep:
                return
            if min_age_seconds > 0 and (now - os.path.getmtime(p)) < min_age_seconds:
                return
            os.remove(p)
            removed += 1
        except OSError:
            pass  # 被占用/已删除：下次再扫

    try:
        entries = os.listdir(root)
    except OSError:
        return 0

    for name in entries:
        p = os.path.join(root, name)
        if os.path.isfile(p):
            _try_remove(p)          # 根目录散落的副本/残留 .part（异常情况兜底）
            continue
        if not os.path.isdir(p):
            continue
        try:
            for sub in os.listdir(p):
                sp = os.path.join(p, sub)
                if os.path.isfile(sp):
                    _try_remove(sp)
        except OSError:
            continue
        # 目录空了才删（还有任务引用其副本时留空目录无妨）；刚创建/正在写入的目录不删
        try:
            if os.listdir(p):
                continue
            _norm_p = _norm(p)
            if any(k.startswith(_norm_p + os.sep) for k in reserved):
                continue
            if (now - os.path.getmtime(p)) < _EMPTY_DIR_GRACE:
                continue
            os.rmdir(p)
        except OSError:
            pass
    if removed:
        logger.info(f"暂存副本清理：删除 {removed} 个文件")
    return removed


def cache_size() -> tuple[int, int]:
    """返回 (文件数, 总字节数)，用于日志/诊断。"""
    try:
        root = staging_root()
    except Exception:
        return 0, 0
    if not os.path.isdir(root):
        return 0, 0
    count = 0
    total = 0
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            try:
                total += os.path.getsize(os.path.join(dirpath, name))
                count += 1
            except OSError:
                pass
    return count, total
