# -*- coding: utf-8 -*-
"""按天分文件的日志（2026-10-28 用户要求：每天一个日志文件，如 2026-10-28.log）

为什么不用 logging.handlers.TimedRotatingFileHandler：它产出的是
`local_tool.log` + `local_tool.log.2026-10-27`，文件名不是"当天日期"。用户要的形态是
`%APPDATA%\\HN打印工具\\logs\\2026-10-28.log`，所以这里用一个极小的自定义 Handler：
每次 emit 比对当前日期，变了就换文件（关闭旧句柄），跨午夜自动落到新文件。

**读写两侧都只走本模块**（唯一口径）：
  · 写：`make_handler(log_dir)`（gui 挂到 hn_local_tool logger 上）
  · 读：`read_tail(log_dir)` —— 云端「收集设备日志」回报的就是它
  · 清：`prune_old_logs(log_dir)` —— 只留近 30 天（2026-10-30 用户要求：30 天以上的自动删）
找不到今天的文件时退回最近的日文件、再退回旧版的 `local_tool.log`，
所以升级当天 / 凌晨零点刚过也不会收到空日志。
"""
import logging
import os
import re
from datetime import datetime, timedelta

LOGGER = logging.getLogger(__name__)

DAILY_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}[.]log$")   # 只认「当天日期」这种文件名
LEGACY_NAME = "local_tool.log"                                 # 旧版单文件（老机器上可能还在）
MAX_TAIL_FILES = 3                                             # read_tail 最多往前拼几天

# 日志保留天数（2026-10-30 用户要求：保留近 30 天，30 天以上的自动删除）。
# 判据是**文件名里的日期**，不是 mtime —— 拷贝/解压会改 mtime，会让清理漏删或误删。
# 只删 DAILY_RE 命中的日文件；`local_tool.log`（旧版单文件）与 `finance_audit.log`
# （收支审计流水，要长期可追）**一律不动** —— 清理日志绝不能顺手动审计账。
KEEP_LOG_DAYS = 30

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"
LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"


def daily_path(log_dir: str, day: str | None = None) -> str:
    """某一天的日志文件路径。day 省略 = 今天（YYYY-MM-DD，零填充）。"""
    key = day or datetime.now().strftime("%Y-%m-%d")
    return os.path.join(log_dir, key + ".log")


def daily_files(log_dir: str) -> list[str]:
    """目录里所有按天日志文件，按日期升序（旧的在前）。非 YYYY-MM-DD.log 一律不算。"""
    try:
        names = os.listdir(log_dir)
    except OSError:
        return []
    return sorted(os.path.join(log_dir, n) for n in names if DAILY_RE.match(n))


def latest_log_path(log_dir: str) -> str:
    """最新一份本机日志：最近的日文件 → 旧版 local_tool.log → ""（都没有）。"""
    files = daily_files(log_dir)
    if files:
        return files[-1]
    legacy = os.path.join(log_dir, LEGACY_NAME)
    return legacy if os.path.isfile(legacy) else ""


def _tail_bytes(path: str, limit: int) -> bytes:
    """从文件倒数 limit 字节处开始读；跨过可能截断的半行（保证从完整行开始）。"""
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        if size > limit:
            f.seek(size - limit)
            f.readline()
        return f.read()


def read_tail(log_dir: str, max_bytes: int = 200 * 1024, max_files: int = MAX_TAIL_FILES) -> str:
    """读本机日志尾部（供云端日志收集回报）：从最新的日文件往前拼，总量 ≤ max_bytes。

    为什么要往前拼：按天分文件后，零点刚过时"今天的文件"可能只有一行（甚至还不存在），
    只读它等于什么都收不到。总预算不变（默认 200KB，与后端 request_log 的上限一致），
    只是把预算按天摊开、最多 max_files 天。读不到任何文件返回空串（调用方按无日志处理）。
    """
    try:
        budget = int(max_bytes)
    except (TypeError, ValueError):
        budget = 200 * 1024
    files = daily_files(log_dir)[-max_files:]
    if not files:
        legacy = os.path.join(log_dir, LEGACY_NAME)
        files = [legacy] if os.path.isfile(legacy) else []
    chunks = []
    remaining = budget
    for path in reversed(files):            # 从最新往回读，预算用完就停
        if remaining <= 0:
            break
        try:
            data = _tail_bytes(path, remaining)
        except OSError:
            continue
        if data:
            chunks.append(data)
            remaining -= len(data)
    return b"".join(reversed(chunks)).decode("utf-8", errors="replace")


def prune_old_logs(log_dir: str, keep_days: int = KEEP_LOG_DAYS, now_fn=None,
                   on_delete=None) -> list[str]:
    """删除 keep_days 天以上的按天日志（2026-10-30 用户要求：保留近 30 天）。

    判据：**文件名里的日期**比 `今天 - keep_days` 还早 ⇒ 删；正好 keep_days 天前的留着
    （"30 天以上的"才删）。只看 DAILY_RE 命中的文件，别的文件（local_tool.log、
    finance_audit.log、用户自己丢进来的东西）一个都不碰。

    三种"不删"的保守情形，宁可漏删也不误删：
      · 文件名日期解析不出来 / 是未来日期（时钟回拨、测试夹具）；
      · 就是今天那个文件（正被 Handler 写着，Windows 下也删不掉）；
      · os.remove 抛 OSError（被别的进程占着 / 权限不足）—— 记 debug 继续下一个。

    返回真正删掉的文件路径（调用方拿去写一行日志）。整个函数**永不抛异常**：
    它跑在日志链路与启动流程里，清理失败绝对不能影响打印。
    """
    try:
        days = int(keep_days)
    except (TypeError, ValueError):
        days = KEEP_LOG_DAYS
    if days < 1:                                   # 0/负数等于"清空"——不接受这种设置
        days = 1
    today = (now_fn or datetime.now)().date()
    cutoff = today - timedelta(days=days)          # 早于这一天的删
    deleted: list[str] = []
    for path in daily_files(log_dir):
        name = os.path.basename(path)
        try:
            day = datetime.strptime(name[:10], "%Y-%m-%d").date()
        except (ValueError, TypeError):
            continue
        if day >= cutoff or day >= today:          # 保留窗口内 / 今天（含未来日期）
            continue
        try:
            os.remove(path)
        except OSError as e:
            LOGGER.debug(f"日志清理：删除 {name} 失败（{e}），跳过")
            continue
        deleted.append(path)
        if on_delete:
            try:
                on_delete(path)
            except Exception:                   # 回调出错不能把清理带崩
                pass
    return deleted


class DailyFileHandler(logging.Handler):
    """按天落盘的 FileHandler 替代品：文件名 = YYYY-MM-DD.log，跨天自动换文件。

    now_fn 只用于测试注入（模拟跨午夜）；生产用 datetime.now。
    """
    def __init__(self, log_dir: str, encoding: str = "utf-8", now_fn=None,
                 keep_days: int = KEEP_LOG_DAYS, on_prune=None):
        super().__init__()
        self.log_dir = log_dir
        self.encoding = encoding
        self._now_fn = now_fn or datetime.now
        self.keep_days = keep_days
        self.on_prune = on_prune          # 回调(删除列表)：让 GUI 把"清了几个文件"写进日志
        self._fh = None
        self._day = ""

    def _day_key(self) -> str:
        return self._now_fn().strftime("%Y-%m-%d")

    def current_path(self) -> str:
        """当前句柄指向的文件（还没写过日志时为空串）。"""
        return os.path.join(self.log_dir, self._day + ".log") if self._day else ""

    def _ensure_open(self) -> None:
        day = self._day_key()
        if self._fh is not None and day == self._day:
            return
        if self._fh is not None:            # 跨天：先关旧句柄（Windows 下不关就删不掉/挪不走）
            try:
                self._fh.close()
            except Exception:
                pass
            self._fh = None
        os.makedirs(self.log_dir, exist_ok=True)
        self._fh = open(os.path.join(self.log_dir, day + ".log"), "a",
                        encoding=self.encoding, errors="replace")
        self._day = day
        # 换文件时顺手清一次过期日志：启动首次打开 + 每次跨午夜各一次，够用且不占额外定时器。
        # 删的是"keep_days 天以前"的文件，刚打开的今天这个永远不在删除范围内。
        try:
            gone = prune_old_logs(self.log_dir, self.keep_days, now_fn=self._now_fn)
        except Exception:                   # 清理失败绝不影响写日志
            gone = []
        if gone and self.on_prune:
            try:
                self.on_prune(gone)
            except Exception:
                pass

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.acquire()
            try:
                self._ensure_open()
                self._fh.write(self.format(record) + "\n")
                self._fh.flush()
            finally:
                self.release()
        except Exception:
            self.handleError(record)

    def close(self) -> None:
        self.acquire()
        try:
            if self._fh is not None:
                try:
                    self._fh.close()
                except Exception:
                    pass
                self._fh = None
        finally:
            self.release()
        super().close()


def make_handler(log_dir: str, level: int = logging.DEBUG, now_fn=None,
                 keep_days: int = KEEP_LOG_DAYS, on_prune=None) -> DailyFileHandler:
    """按天日志 Handler（带统一格式），给 hn_local_tool logger 用。

    keep_days 以上的日文件在换文件时自动删除（见 prune_old_logs）；
    on_prune 收一个可调用对象(删除的文件列表)，用于把"清了 N 个"写进 GUI 日志。
    """
    handler = DailyFileHandler(log_dir, now_fn=now_fn, keep_days=keep_days, on_prune=on_prune)
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=LOG_DATEFMT))
    return handler
