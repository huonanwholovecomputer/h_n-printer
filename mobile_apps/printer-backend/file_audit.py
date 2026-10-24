#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""S3-04 文件访问审计日志（谁、读了哪个文件、何时）——独立模块，不 import Flask。

依据：docs/安全加固修复计划_2026-10-23.md §S3-04（DS-24）"安全运营：文件访问审计日志"。

设计要点（改之前先读）：

1. **存储**：`logs/audit/file_access-YYYYMMDD.jsonl`（服务器**本地时区**，与 app 其他地方的
   `datetime.now()` 同一口径，按日轮转）。一行一个 JSON 对象，UTF-8。
   时间戳字段 `ts` 统一为 **ISO8601 带本地时区偏移**（如 `2026-10-24T21:30:00+08:00`），
   既可人读、也能直接 `datetime.fromisoformat()`；不写 UTC 是为了和库里的
   `%Y-%m-%d %H:%M:%S` 本地时间口径对得上（部署要求服务器时区 = Asia/Shanghai）。

2. **fail-open（硬约束）**：审计是旁路 —— 磁盘满 / 目录不可写 / 编码异常，**绝不许**
   影响下载、删除、列表这些主流程。`record()` 内部吞掉一切异常，只 `print("[AUDIT] ...")`，
   调用方无需 try/except（app 里仍有一层兜底，见 `_audit_file_access`）。

3. **日志卫生（S2-11 口径）**：只记**扩展名**（`.pdf`），**禁止**原文件名与绝对路径。
   调用方只传 `ext`；本模块的 `_safe_ext()` 再做一次白名单化（哪怕误传整个文件名/路径，
   也只会留下扩展名）。顾客原文件名属于隐私，日志会进 journald 并被打包回传。

4. **追加写**：`os.open(O_APPEND | O_CREAT | O_WRONLY | O_BINARY, 0o600)` + 一次 `os.write`
   （整行原子；O_BINARY 保证 Windows 上不把 `\n` 翻成 `\r\n`），进程内再加
   `threading.Lock`；目录 0o700。不用 `open(..., "a")` 是为了明确的 0600 与跨进程
   O_APPEND 语义。

5. **保留期**：默认 180 天，**惰性清理**（写日志时顺带检查，最多每 6 小时真正扫一次目录），
   不依赖 APScheduler（进程长期不重启也不会把保留期拖失效）。`prune(force=True)` 供测试/运维
   强制清理。

6. **查询**：`query()` 按时间倒序、限量扫描（最近 N 个文件 + 行数上限），防止一个管理接口
   把整个日志目录读进内存；`stats()` 供季度报告 `--stats --json`（同一套有界扫描）。
"""
import json
import os
import re
import threading
import time
from datetime import date, datetime, timedelta

# ---------------------------------------------------------------- 常量
#: 默认目录 = <backend>/logs/audit（与 app.py 的 LOG_DIR 并列；随部署排除，不进 git）
DEFAULT_AUDIT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", "audit")

#: 测试/临时覆盖用环境变量（app 与 CLI 都认）。生产不设置即为默认目录。
AUDIT_DIR_ENV = "HN_FILE_AUDIT_DIR"

FILE_PREFIX = "file_access-"
FILE_SUFFIX = ".jsonl"
FILE_DATE_FMT = "%Y%m%d"

DEFAULT_RETENTION_DAYS = 180
PRUNE_INTERVAL_SECONDS = 6 * 3600

#: 查询/统计的扫描上限（防放大）：文件个数 + 有效行数
MAX_QUERY_FILES = 200
MAX_QUERY_LINES = 20000
MAX_STATS_FILES = 400
MAX_STATS_LINES = 100000

MAX_LIMIT = 500
DEFAULT_QUERY_LIMIT = 200

_DIR_MODE = 0o700
_FILE_MODE = 0o600

#: Windows 上 os.open 默认文本模式会把 \n 翻成 \r\n（JSONL 仍能读，但落盘字节与写入数据不一致）；
#: 显式 O_BINARY 保证「一次 os.write 写的整行 = 文件里的整行」。Linux 上该常量为 0。
_O_BINARY = getattr(os, "O_BINARY", 0)

#: 进程内写锁：os.write 单次写本身原子，锁只用来串行化"建目录 + open + write"这段，
#: 避免同一进程多线程同时 open 时的重复建目录/竞态（跨进程靠 O_APPEND）。
_WRITE_LOCK = threading.Lock()

#: 保留期清理的模块级时间戳（最多每 PRUNE_INTERVAL_SECONDS 扫一次目录）。
_PRUNE_LOCK = threading.Lock()
_PRUNE_STATE = {"last": 0.0}

#: file_id / openid / actor / ip 的白名单（防止调用方误传带分隔符/空格的整段文本）。
_ID_RE = re.compile(r"[^A-Za-z0-9_.\-@]")

_FILE_NAME_RE = re.compile(r"^" + re.escape(FILE_PREFIX) + r"(\d{8})" + re.escape(FILE_SUFFIX) + r"$")


# ---------------------------------------------------------------- 路径与目录
def audit_dir():
    """审计目录：环境变量优先（测试/临时目录），否则 <backend>/logs/audit。"""
    override = (os.environ.get(AUDIT_DIR_ENV) or "").strip()
    if override:
        return override
    return DEFAULT_AUDIT_DIR


def _ensure_dir(path):
    """建目录（0700）。已存在时也 chmod 一次，避免被 umask/手工创建放宽。"""
    os.makedirs(path, mode=_DIR_MODE, exist_ok=True)
    try:
        os.chmod(path, _DIR_MODE)
    except OSError:
        # Windows 上 chmod 只影响只读位，失败不代表不可用 —— 不阻断写日志。
        pass


def _file_path(day=None):
    day = day or datetime.now()
    name = FILE_PREFIX + day.strftime(FILE_DATE_FMT) + FILE_SUFFIX
    return os.path.join(audit_dir(), name)


# ---------------------------------------------------------------- 字段清洗
def _clean(value, maxlen):
    """只留白名单字符并截断（file_id/openid/actor/ip 用）。"""
    text = str(value if value is not None else "").strip()
    return _ID_RE.sub("", text)[:maxlen]


def _as_int(value):
    """int 字段：空/非法一律 None（JSON null），不编造 0。"""
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _safe_ext(ext):
    """只允许扩展名（`.pdf`）。即使误传完整文件名/绝对路径，也只保留扩展名。"""
    try:
        raw = str(ext or "").strip().lower()
        if not raw:
            return ""
        # 防止调用方把路径/原文件名直接塞进来：先去目录、再只取 splitext 的扩展名。
        raw = os.path.basename(raw.replace("\\", "/"))
        if not raw.startswith("."):
            raw = os.path.splitext(raw)[1] if "." in raw else ("." + raw)
        raw = "." + re.sub(r"[^a-z0-9]", "", raw)[:12]
        return raw if len(raw) > 1 else ""
    except Exception:  # noqa: BLE001  字段清洗绝不允许抛
        return ""


def _iso_ts(ts=None):
    """ISO8601 带本地时区偏移。ts 可传 datetime / epoch 秒 / None(=now)，供测试注入。"""
    if ts is None:
        dt = datetime.now().astimezone()
    elif isinstance(ts, datetime):
        dt = ts.astimezone()
    else:
        dt = datetime.fromtimestamp(float(ts)).astimezone()
    return dt.isoformat(timespec="seconds")


# ---------------------------------------------------------------- 写入
def record(action, result, file_id="", openid="", actor="", task_id=None,
           ip="", ext="", count=None, ts=None):
    """追加一条文件访问审计。**fail-open：任何异常都在内部吞掉，绝不抛出。**

    参数（只记这些，不记原文件名/路径）：
      action   download / delete / list（小写下划线，调用方约定）
      result   ok / denied / not_found / conflict
      file_id  文件 id（不是文件名）
      openid   签名绑定/当前登录的 openid（可能为空：旧式 str payload）
      actor    signed_url / user / admin 等
      task_id  绑定任务 id（没有则 None）
      ip       request.remote_addr（ProxyFix 还原后的真实 IP；不要传 XFF 原文）
      ext      扩展名（如 ".pdf"）；传别的也只会保留扩展名
      count    list 这类动作的条数
      ts       仅测试用（datetime/epoch），默认 now

    返回 True=已写；False=写失败但已按 fail-open 吞掉（调用方通常不需要看）。
    """
    # 惰性保留期清理：最多每 6 小时真正扫目录一次；prune 自身也 fail-open。
    try:
        prune()
    except Exception:  # noqa: BLE001
        pass

    try:
        entry = {
            "ts": _iso_ts(ts),
            "action": _clean(action, 32),
            "result": _clean(result, 16),
            "file_id": _clean(file_id, 64),
            "openid": _clean(openid, 128),
            "actor": _clean(actor, 32),
            "task_id": _as_int(task_id),
            "ip": _clean(ip, 64),
            "ext": _safe_ext(ext),
            "count": _as_int(count),
        }
        data = (json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        path = _file_path()
        with _WRITE_LOCK:
            _ensure_dir(audit_dir())
            fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY | _O_BINARY, _FILE_MODE)
            try:
                os.write(fd, data)  # 一次写完整行（O_APPEND ⇒ 跨进程原子追加）
            finally:
                os.close(fd)
        try:
            os.chmod(path, _FILE_MODE)
        except OSError:
            pass
        return True
    except Exception as e:  # noqa: BLE001  ← fail-open：审计失败绝不影响主流程
        # 只打异常类名（异常文本里可能带绝对路径，S2-11 口径）。
        print("[AUDIT] 文件访问审计写入失败（已忽略，不影响主流程）: %s" % type(e).__name__)
        return False


# ---------------------------------------------------------------- 文件枚举
def _list_files():
    """列出审计目录里的分片，返回 [(date, path)]，按日期**倒序**（新→旧）。"""
    out = []
    try:
        names = os.listdir(audit_dir())
    except OSError:
        return out
    for name in names:
        m = _FILE_NAME_RE.match(name)
        if not m:
            continue
        try:
            day = datetime.strptime(m.group(1), FILE_DATE_FMT).date()
        except ValueError:
            continue
        out.append((day, os.path.join(audit_dir(), name)))
    out.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return out


def _iter_entries(max_files, max_lines):
    """有界迭代审计条目（新文件先、文件内按写入顺序）。解析失败的行跳过（不中断）。"""
    scanned = 0
    for _day, path in _list_files()[:max_files]:
        try:
            fh = open(path, "r", encoding="utf-8", errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
                if scanned >= max_lines:
                    return
                line = line.strip()
                if not line:
                    continue
                scanned += 1
                try:
                    entry = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if isinstance(entry, dict):
                    yield entry


def _to_local(dt):
    """统一成"带本地时区偏移"的 aware datetime，便于跨时区比较。"""
    if dt.tzinfo is None:
        return dt.astimezone()
    return dt.astimezone()


def _parse_filter_ts(value, is_until=False):
    """解析查询时间参数：datetime / epoch / ISO8601 / "%Y-%m-%d[ %H:%M:%S]" / "%Y%m%d"。

    非空但解析不了 ⇒ **显式** ValueError（不静默当成"不过滤"）。
    纯日期作为 until 时补到当天 23:59:59.999999（"查到这一天为止"的直觉口径）。
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return _to_local(value)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value)).astimezone()
    text = str(value).strip()
    if not text:
        return None

    candidates = [text]
    if text.endswith("Z"):
        candidates.insert(0, text[:-1] + "+00:00")

    dt = None
    for cand in candidates:
        try:
            dt = datetime.fromisoformat(cand)
            break
        except ValueError:
            continue
    if dt is None:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y%m%d"):
            try:
                dt = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
    if dt is None:
        raise ValueError("时间格式无法解析: %r（支持 ISO8601 / %%Y-%%m-%%d[ %%H:%%M:%%S]）" % text)

    dt = _to_local(dt)
    # 只给了日期（没有时间部分）的 until：补到当天末尾
    if is_until and re.match(r"^\d{4}-\d{2}-\d{2}$", text):
        dt = dt.replace(hour=23, minute=59, second=59, microsecond=999999)
    return dt


def _entry_dt(entry):
    """条目 ts → aware datetime；解析失败返回 None。"""
    try:
        return _to_local(datetime.fromisoformat(str(entry.get("ts") or "")))
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------- 查询 / 统计
def query(file_id=None, openid=None, action=None, result=None,
          since=None, until=None, limit=DEFAULT_QUERY_LIMIT):
    """按条件查审计，**时间倒序**返回最多 limit 条（默认 200，上限 500）。

    有界：最多扫描最近 MAX_QUERY_FILES 个分片、MAX_QUERY_LINES 行 —— 管理接口不会因为
    日志目录变大而变成一次全量读取。
    """
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = DEFAULT_QUERY_LIMIT
    limit = max(1, min(MAX_LIMIT, limit))

    fid = _clean(file_id, 64) if file_id else ""
    oid = _clean(openid, 128) if openid else ""
    act = _clean(action, 32) if action else ""
    res = _clean(result, 16) if result else ""
    since_dt = _parse_filter_ts(since)
    until_dt = _parse_filter_ts(until, is_until=True)

    out = []
    try:
        for entry in _iter_entries(MAX_QUERY_FILES, MAX_QUERY_LINES):
            if fid and str(entry.get("file_id") or "") != fid:
                continue
            if oid and str(entry.get("openid") or "") != oid:
                continue
            if act and str(entry.get("action") or "") != act:
                continue
            if res and str(entry.get("result") or "") != res:
                continue
            if since_dt is not None or until_dt is not None:
                dt = _entry_dt(entry)
                if dt is None:
                    continue
                if since_dt is not None and dt < since_dt:
                    continue
                if until_dt is not None and dt > until_dt:
                    continue
            out.append(entry)
    except Exception as e:  # noqa: BLE001
        print("[AUDIT] 审计查询失败（返回已收集部分）: %s" % type(e).__name__)

    # 分片内是按写入顺序；跨文件也不保证全局有序 ⇒ 统一按 ts 倒序（同一时区偏移下字符串序=时间序）
    out.sort(key=lambda e: str(e.get("ts") or ""), reverse=True)
    return out[:limit]


def stats(since=None, until=None):
    """聚合统计（供季度报告 `--stats --json`）。

    返回 {files, lines, by_action, by_result, denied}；files=实际扫描的分片数，
    lines=有效审计条目数，denied=result=="denied" 的条数。
    有界：最多 MAX_STATS_FILES 个分片 / MAX_STATS_LINES 行。
    """
    since_dt = _parse_filter_ts(since)
    until_dt = _parse_filter_ts(until, is_until=True)
    result = {"files": 0, "lines": 0, "by_action": {}, "by_result": {}, "denied": 0}

    files = _list_files()[:MAX_STATS_FILES]
    scanned = 0
    for _day, path in files:
        try:
            fh = open(path, "r", encoding="utf-8", errors="replace")
        except OSError:
            continue
        with fh:
            result["files"] += 1
            for line in fh:
                if scanned >= MAX_STATS_LINES:
                    break
                line = line.strip()
                if not line:
                    continue
                scanned += 1
                try:
                    entry = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if not isinstance(entry, dict):
                    continue
                if since_dt is not None or until_dt is not None:
                    dt = _entry_dt(entry)
                    if dt is None:
                        continue
                    if since_dt is not None and dt < since_dt:
                        continue
                    if until_dt is not None and dt > until_dt:
                        continue
                result["lines"] += 1
                act = str(entry.get("action") or "")
                res = str(entry.get("result") or "")
                if act:
                    result["by_action"][act] = result["by_action"].get(act, 0) + 1
                if res:
                    result["by_result"][res] = result["by_result"].get(res, 0) + 1
                if res == "denied":
                    result["denied"] += 1
    return result


# ---------------------------------------------------------------- 保留期
def prune(retention_days=DEFAULT_RETENTION_DAYS, force=False):
    """删除超过保留期的分片（默认 180 天）。最多每 6 小时真正扫一次目录。

    · `force=True`：跳过 6 小时间隔（测试 / 运维手工清理）；
    · 文件名里的日期就是分片日（file_access-YYYYMMDD.jsonl），不依赖 mtime（拷贝/备份
      恢复后 mtime 会变）；
    · fail-open：任何 IO/权限异常只 print，不抛。
    返回实际删除的分片数。
    """
    removed = 0
    try:
        now = time.time()
        with _PRUNE_LOCK:
            if not force and (now - _PRUNE_STATE["last"]) < PRUNE_INTERVAL_SECONDS:
                return 0
            _PRUNE_STATE["last"] = now

        try:
            days = int(retention_days)
        except (TypeError, ValueError):
            days = DEFAULT_RETENTION_DAYS
        days = max(1, days)
        cutoff = date.today() - timedelta(days=days)

        for day, path in _list_files():
            if day >= cutoff:
                continue
            try:
                os.remove(path)
                removed += 1
            except OSError:
                pass
    except Exception as e:  # noqa: BLE001  ← fail-open
        print("[AUDIT] 审计日志保留期清理失败（已忽略）: %s" % type(e).__name__)
    return removed
