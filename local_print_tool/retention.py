"""retention.py — 本地副本生命周期管理器（S1-03 客户端半 / DS-06）

问题背景
--------
安全评估报告 DS-06 实测：`file_cache` 里有 2026-09-15 的顾客原件仍在、
`pdf_cache` 的清理**只在写新缓存时被调度**（184 h 的条目还没删）、
攒批批次目录的 `.keep`（人工保留标记）永不失效、`keep_temp_pdf` 会把含
顾客文档全文的 PDF 复制到桌面且没有任何提示。

服务端 `retention_config.json` 已经做了「客户自选保留期（≤168 h）」，
客户端这一侧必须跟着做同一套承诺，否则「7 天后删除」在店铺机上不成立。

本模块负责的四类本地副本（全部由**启动扫描**兜底，不只靠事件驱动）：

| 副本 | TTL | 说明 |
|------|-----|------|
| `file_cache/<hash>/…`（本地文件工作副本） | 服务端下发的 retention（缺省 24 h，上限 168 h） | **仍被任务引用的副本绝不按时间删**（打印/预约还要用），只在日志里点名预警；任务结束后的按引用清扫照常回收 |
| `file_cache/cloud_<task_id>/…`（云端下载文件） | 同上；目录里若有下载时写入的 `.retention` 侧车（服务端该文件的 retention_hours）则优先用侧车 | 侧车只是小时数，不含任何顾客内容 |
| `pdf_cache/<key>.pdf` + `index.json` | 同上（同一天花板） | 可重建缓存：过期删除后打印时会自动重新转换，不会丢单 |
| `duplex_stack/<batch>/.keep` | 最长 30 天 | 超期自动解除标记并写**警告**日志；解除后该批次按既有规则可被清理（纸叠任务本身仍由攒批账本决定） |

不依赖 Qt；任何 OSError 都降级为「这次少删一点」，绝不让清理把程序搞崩。

调用方
------
- `gui.py` 启动时调用 `startup_sweep(...)`（本地副本 + `.keep`），
  云端未启用时再用 `sweep_pdf_cache_dir()` 扫一遍 PDF 缓存；
- `cloud_client.py` 的 `_cleanup_pdf_cache()` 复用 `sweep_pdf_cache()`
  （保留期夹紧 + 索引/孤儿回收同一套判定），并在 `start()` 时做一次启动扫描。
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# ── 口径常量（与服务端 MAX_RETENTION_HOURS / DEFAULT_FILE_RETENTION_HOURS 对齐）──

MAX_RETENTION_HOURS = 168          # 7 天：客户可选的硬上限，任何本地副本都不得超过
DEFAULT_RETENTION_HOURS = 24       # 取不到服务端值时用 24 h（绝不再"永不过期"）
KEEP_MARKER_NAME = ".keep"         # 攒批批次目录里的「人工保留」标记（duplex_stack.KEEP_MARKER）
KEEP_MARKER_MAX_DAYS = 30          # 人工保留标记最长活 30 天
SIDECAR_NAME = ".retention"        # cloud_<task_id> 目录里的保留期侧车（JSON，无顾客内容）
_EMPTY_DIR_GRACE = 300.0           # 空目录宽限（与 staging._EMPTY_DIR_GRACE 同口径）
_PART_MAX_AGE = 3600.0             # 写了一半的 .part/.tmp 残留最多活 1 h

# 设置页「保存转换副本到桌面」的显式警告文案（GUI 用同一份，避免两处口径漂移）
KEEP_TEMP_WARNING = (
    "开启后，转换出的 PDF（含顾客文档全文）会被复制一份到桌面。\n\n"
    "桌面文件不受本工具的生命周期清理保护，也不会随订单清除而删除——\n"
    "只有你确实需要留存副本时才开启，用完请自行删除。"
)


# ── 口径换算 ──

def clamp_hours(value, default: int = DEFAULT_RETENTION_HOURS,
                maximum: int = MAX_RETENTION_HOURS) -> int:
    """把任意来源的保留期换算成合法小时数。

    - 0 / 负数 / 非数字 / None ⇒ default（24 h）；「0 = 永不过期」的旧口径**不再成立**；
    - 超过上限 ⇒ 上限（168 h）；
    - 1..168 ⇒ 原值。
    """
    try:
        n = int(value)
    except (TypeError, ValueError):
        return int(default)
    if n <= 0:
        return int(default)
    return int(maximum) if n > int(maximum) else n


# `effective_hours` 是调用方语义更明确的别名（"服务端下发值 → 本地实际生效值"）
def effective_hours(server_hours, default: int = DEFAULT_RETENTION_HOURS) -> int:
    return clamp_hours(server_hours, default=default)


# ── 路径工具 ──

def _norm(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


def _age(now: float, path: str) -> float | None:
    try:
        return now - os.path.getmtime(path)
    except OSError:
        return None


def _size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _remove(path: str) -> int:
    """删除一个文件，返回释放的字节数（失败/被占用返回 0）。"""
    size = _size(path)
    try:
        os.remove(path)
        return size
    except OSError:
        return 0


def _atomic_write_json(path: str, data) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _read_json(path: str):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError, json.JSONDecodeError):
        return None


# ── 结果对象 ──

@dataclass
class SweepResult:
    """一次扫描的结论（供 GUI 写日志/状态栏，测试也直接断言这些数字）。"""
    removed: int = 0
    bytes_freed: int = 0
    skipped_referenced: int = 0
    hours: int = DEFAULT_RETENTION_HOURS
    names: list[str] = field(default_factory=list)

    @property
    def mb(self) -> float:
        return self.bytes_freed / 1048576.0

    def summary(self, label: str = "") -> str:
        parts = []
        if self.removed:
            parts.append(f"删除 {self.removed} 个文件（{self.mb:.1f} MB）")
        if self.skipped_referenced:
            parts.append(f"{self.skipped_referenced} 个超期但仍被任务引用、已保留")
        if not parts:
            parts.append("无过期副本")
        return f"{label}保留期 {self.hours}h：" + "，".join(parts)


# ── 云端下载侧车：记录"服务端该文件选了多长保留期" ──

def sidecar_path(target_path: str) -> str:
    return os.path.join(os.path.dirname(os.path.abspath(target_path)), SIDECAR_NAME)


def write_sidecar(target_path: str, hours) -> bool:
    """把服务端下发的 retention_hours 记在副本目录的侧车里（0/无效 ⇒ 删除侧车）。

    侧车只存小时数，不含文件名/内容/用户信息；写入失败不影响下载（返回 False）。
    """
    if not target_path:
        return False
    path = sidecar_path(target_path)
    try:
        n = int(hours or 0)
    except (TypeError, ValueError):
        n = 0
    if n <= 0:
        try:
            if os.path.isfile(path):
                os.remove(path)
        except OSError:
            pass
        return False
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        _atomic_write_json(path, {"retention_hours": clamp_hours(n)})
        return True
    except OSError as e:
        logger.debug(f"写入保留期侧车失败（忽略）: {path} → {e}")
        return False


def read_sidecar(directory: str) -> int:
    """读目录侧车的小时数；没有/损坏 ⇒ 0（调用方回落到全局口径）。"""
    data = _read_json(os.path.join(directory, SIDECAR_NAME))
    if isinstance(data, dict):
        try:
            n = int(data.get("retention_hours", 0) or 0)
            return clamp_hours(n) if n > 0 else 0
        except (TypeError, ValueError):
            return 0
    return 0


# ── 1. file_cache（暂存工作副本 + 云端下载） ──

def sweep_file_cache(root: str, keep_paths=(), hours=None, now: float | None = None) -> SweepResult:
    """按 TTL 清理暂存目录里**不再被引用**的副本。

    keep_paths：仍被任务引用的路径集合（GUI 的 `_staged_reference_paths()`）。
    引用中的副本即使超期也**不删**——删掉会让待打印/预约单在点打印时报"文件不存在"；
    只计入 `skipped_referenced` 并由调用方写日志预警（任务结束后的按引用清扫会回收它）。
    """
    hours = clamp_hours(hours)
    result = SweepResult(hours=hours)
    now = time.time() if now is None else now
    ttl = hours * 3600.0
    keep = {_norm(p) for p in (keep_paths or ()) if p}

    if not root or not os.path.isdir(root):
        return result

    try:
        entries = os.listdir(root)
    except OSError:
        return result

    for name in entries:
        top = os.path.join(root, name)
        if os.path.isfile(top):
            _consider_file(top, result, keep, now, ttl, hours)
            continue
        if not os.path.isdir(top):
            continue
        # cloud_<task_id> 目录可能带服务端该文件的保留期侧车 → 目录内文件用侧车值
        dir_hours = read_sidecar(top) or hours
        dir_ttl = dir_hours * 3600.0
        try:
            sub_names = os.listdir(top)
        except OSError:
            continue
        for sub in sub_names:
            sub_path = os.path.join(top, sub)
            if sub == SIDECAR_NAME:
                continue
            if os.path.isfile(sub_path):
                _consider_file(sub_path, result, keep, now, dir_ttl, dir_hours)
        _maybe_drop_dir(top, now)

    if result.removed:
        logger.info(result.summary("暂存副本"))
    if result.skipped_referenced:
        logger.warning(
            "暂存副本超过保留期但仍被任务引用（不会按时间删除，任务结束/清除后回收）: %s",
            "、".join(result.names[:5]) + ("…" if len(result.names) > 5 else ""))
    return result


def _consider_file(path: str, result: SweepResult, keep: set[str],
                   now: float, ttl: float, hours: int) -> None:
    age = _age(now, path)
    if age is None:
        return
    name = os.path.basename(path)
    # 写了一半的 .part/.tmp：不按 TTL 等，超过 1 h 就收（它们是异常退出的残留）
    if name.endswith((".part", ".tmp")):
        if age > min(ttl, _PART_MAX_AGE):
            result.bytes_freed += _remove(path)
            result.removed += 1
        return
    if age <= ttl:
        return
    if _norm(path) in keep:
        result.skipped_referenced += 1
        if len(result.names) < 20:
            result.names.append(name)
        return
    freed = _remove(path)
    if freed or not os.path.exists(path):
        result.removed += 1
        result.bytes_freed += freed


def _maybe_drop_dir(directory: str, now: float) -> None:
    """目录里只剩侧车（或空）且够旧 → 先删侧车再删目录。"""
    try:
        names = os.listdir(directory)
    except OSError:
        return
    others = [n for n in names if n != SIDECAR_NAME]
    if others:
        return
    if (now - os.path.getmtime(directory)) < _EMPTY_DIR_GRACE:
        return
    if SIDECAR_NAME in names:
        _remove(os.path.join(directory, SIDECAR_NAME))
    try:
        os.rmdir(directory)
    except OSError:
        pass


# ── 2. pdf_cache（可重建缓存） ──

def _parse_created(text: str) -> float | None:
    """index.json 里的 created_at（"%Y-%m-%d %H:%M:%S"，本地时间）→ epoch。"""
    try:
        return time.mktime(time.strptime(str(text or "").strip(), "%Y-%m-%d %H:%M:%S"))
    except (ValueError, OverflowError):
        return None


def sweep_pdf_cache(cache_dir: str, index: dict, hours=None,
                    now: float | None = None) -> tuple[int, dict, int]:
    """清理 pdf_cache 的过期条目 + 无索引孤儿 .pdf。返回 (删除数, 索引, 释放字节)。

    index 原地修改并返回（调用方负责在锁内落盘）。可重建缓存删除后，
    打印时会自动重新转换，不会丢单。
    """
    hours = clamp_hours(hours)
    now = time.time() if now is None else now
    cutoff = now - hours * 3600.0
    removed = 0
    freed = 0
    if not isinstance(index, dict):
        index = {}

    live: set[str] = set()
    for key in list(index.keys()):
        meta = index.get(key)
        meta = meta if isinstance(meta, dict) else {}
        created = _parse_created(meta.get("created_at", ""))
        pdf_path = os.path.join(cache_dir, f"{key}.pdf")
        if created is not None and created < cutoff:
            freed += _remove(pdf_path)
            index.pop(key, None)
            removed += 1
        else:
            live.add(f"{key}.pdf")

    # 孤儿：磁盘上有、索引里没有（历史 key 失配 / 崩溃半途产物）。只收早于 cutoff 的，
    # 避免误删"刚写完文件、索引尚未落盘"的竞态窗口。
    try:
        names = os.listdir(cache_dir)
    except OSError:
        return removed, index, freed
    for name in names:
        full = os.path.join(cache_dir, name)
        age = _age(now, full)
        if age is None:
            continue
        if name.endswith((".tmp", ".part")):
            if age > min(hours * 3600.0, _PART_MAX_AGE):
                freed += _remove(full)
            continue
        if not name.lower().endswith(".pdf") or name in live:
            continue
        if os.path.getmtime(full) < cutoff:
            freed += _remove(full)
            removed += 1
            logger.info(f"回收无索引孤儿缓存: {name}")
    return removed, index, freed


def _cache_index_path(cache_dir: str) -> str:
    return os.path.join(cache_dir, "index.json")


def sweep_pdf_cache_dir(cache_dir: str, hours=None, now: float | None = None) -> SweepResult:
    """云端未启用时的独立入口：自己读/写 index.json（单线程启动阶段调用）。"""
    hours = clamp_hours(hours)
    result = SweepResult(hours=hours)
    if not cache_dir or not os.path.isdir(cache_dir):
        return result
    index = _read_json(_cache_index_path(cache_dir))
    if not isinstance(index, dict):
        index = {}
    removed, index, freed = sweep_pdf_cache(cache_dir, index, hours=hours, now=now)
    result.removed = removed
    result.bytes_freed = freed
    if removed:
        try:
            _atomic_write_json(_cache_index_path(cache_dir), index)
        except OSError as e:
            logger.warning(f"PDF 缓存索引落盘失败（下次再清）: {e}")
        logger.info(result.summary("PDF 缓存"))
    return result


# ── 3. 攒批 `.keep`（人工保留标记） ──

def expire_keep_markers(stack_root: str, now: float | None = None,
                        max_days: int = KEEP_MARKER_MAX_DAYS) -> list[str]:
    """删除超过 max_days 的 `.keep` 标记，返回被解除标记的批次名列表。

    只删标记，不删批次文件：解除后该批次与其它批次一样，由启动续打/清理流程决定去留。
    """
    now = time.time() if now is None else now
    cutoff = now - max_days * 86400
    expired: list[str] = []
    if not stack_root or not os.path.isdir(stack_root):
        return expired
    try:
        entries = os.listdir(stack_root)
    except OSError:
        return expired
    for name in entries:
        bdir = os.path.join(stack_root, name)
        if not os.path.isdir(bdir):
            continue
        marker = os.path.join(bdir, KEEP_MARKER_NAME)
        age = _age(now, marker)
        if age is None:
            continue
        if os.path.getmtime(marker) < cutoff:
            if _remove(marker) or not os.path.exists(marker):
                expired.append(name)
    if expired:
        logger.warning(
            "攒批批次的人工保留标记已超过 %d 天，自动解除（批次文件未删，仍由续打流程处理）: %s",
            max_days, "、".join(expired))
    return expired


# ── 4. 启动扫描（一次跑完，别再只靠"写新缓存"事件驱动） ──

def startup_sweep(file_cache_root: str, keep_paths=(), stack_root: str | None = None,
                  hours=None, now: float | None = None) -> dict:
    """启动时扫描本地副本：file_cache TTL + 攒批 `.keep` 过期。

    返回 {"file_cache": SweepResult, "keep_expired": [批次名], "hours": int}。
    任何一步失败都不影响另一步（各自 try 包住）。
    """
    hours = clamp_hours(hours)
    report: dict = {"hours": hours, "file_cache": SweepResult(hours=hours), "keep_expired": []}
    try:
        report["file_cache"] = sweep_file_cache(
            file_cache_root, keep_paths=keep_paths, hours=hours, now=now)
    except Exception as e:  # noqa: BLE001 —— 清理失败不能挡住启动
        logger.warning(f"启动清理暂存副本失败: {e}")
    if stack_root:
        try:
            report["keep_expired"] = expire_keep_markers(stack_root, now=now)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"启动清理攒批保留标记失败: {e}")
    return report


# ── 自检（python -m retention 可直接跑，不依赖 Qt/配置） ──

def _selftest() -> int:
    import tempfile
    tmp = tempfile.mkdtemp(prefix="hn_ret_selftest_")
    try:
        # 20 小时前的文件保留、30 小时前的删除（默认 24 h）
        old = os.path.join(tmp, "hash1", "old.pdf")
        new = os.path.join(tmp, "hash2", "new.pdf")
        for path, age_h in ((old, 30), (new, 20)):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as f:
                f.write(b"x" * 10)
            t = time.time() - age_h * 3600
            os.utime(path, (t, t))
        rep = sweep_file_cache(tmp, hours=24)
        assert rep.removed == 1 and not os.path.exists(old) and os.path.exists(new), rep
        # 引用中的超期副本不删
        kept = os.path.join(tmp, "hash3", "kept.pdf")
        os.makedirs(os.path.dirname(kept), exist_ok=True)
        open(kept, "wb").write(b"x")
        t = time.time() - 40 * 3600
        os.utime(kept, (t, t))
        rep = sweep_file_cache(tmp, keep_paths=[kept], hours=24)
        assert rep.skipped_referenced == 1 and os.path.exists(kept), rep
        # 侧车优先
        cdir = os.path.join(tmp, "cloud_7")
        os.makedirs(cdir, exist_ok=True)
        cfile = os.path.join(cdir, "a.pdf")
        open(cfile, "wb").write(b"x")
        t = time.time() - 3 * 3600
        os.utime(cfile, (t, t))
        write_sidecar(cfile, 1)
        rep = sweep_file_cache(tmp, hours=24)
        assert not os.path.exists(cfile), rep
        # 夹紧矩阵
        assert [clamp_hours(v) for v in (0, -5, "abc", None, 24, 999)] == [24, 24, 24, 24, 24, 168]
        print("SELFTEST OK")
        return 0
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":  # pragma: no cover
    logging.basicConfig(level=logging.INFO)
    raise SystemExit(_selftest())
