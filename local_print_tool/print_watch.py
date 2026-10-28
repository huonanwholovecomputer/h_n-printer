# -*- coding: utf-8 -*-
"""
print_watch.py — 外部打印监视（A 档最小版：只检测 + 记账，不阻断出纸）

现场问题：操作员/顾客直接用 Word 等**任意软件**向打印机发作业，绕过本工具 ⇒ 收不到钱。
本模块订阅本机 spooler 的作业事件，把"不是本工具提交的"作业记进账本，界面弹出来让人记账。

三条必须记住的现场事实（2026 实测，别照文档推）：

  ① 本工具自己的作业也必须先排除。做法是**自己提交时给文档名加 SELF_TAG 前缀**
     （pdf_printer.print_pdf 的 StartDoc 前一行），否则自家每一单都会被当成外部打印
     再记一次账（同一笔钱收两遍）。
  ② win32print **没有** FindFirstPrinterChangeNotification，只有 ctypes.WinDLL("winspool.drv")
     里那一个；非提权进程可以订阅，空闲队列 WaitForSingleObject 返回 258(WAIT_TIMEOUT) 即句柄有效。
     实测：pPrinterName=NULL 的"整机订阅"模式返回 INVALID_HANDLE_VALUE(err=6)，**必须按队列名订阅**。
  ③ HP LaserJet 1020 这类主机型驱动的 spooler **从不产生 307（文档完成/页数）**事件，
     只有 800/801/805/842，而且 JOB_INFO_2.TotalPages 常为 0 ⇒ 页数只能三级回落
     （TotalPages → PagesPrinted → 未知），未知时由界面人工补录。
     同机上的 HP MFP M232 能出 307 —— 两种情形都得活，所以下面不假设任何事件一定到来。

设计约束：
  · 监视线程里**绝不碰 Qt**（回调只把 ExternalPrint 交出去，GUI 用 Signal 转发到主线程）。
  · 重活（判定/落盘/去重）全在纯函数与 ExternalPrintStore 里，可离线验收。
  · "拿作业列表"抽成可注入的 enum_jobs(printer)，ctypes 订阅只负责唤醒。
"""

from __future__ import annotations

import csv
import json
import logging
import os
import shutil
import tempfile
import threading
import time
from dataclasses import dataclass, asdict, fields as dataclass_fields
from datetime import datetime
from typing import Any, Callable, Iterable, Optional

from printer_config import calc_cost

logger = logging.getLogger(__name__)

# 本工具提交的作业文档名前缀（排除自家作业的唯一依据，见模块头 ①）
SELF_TAG = "HN|"

EXTERNAL_PRINTS_FILENAME = "external_prints.json"
MAX_ENTRIES = 2000                      # 账本上限：超出丢最旧（超出部分早已结账走人）

STATUS_NEW = "new"
STATUS_CONFIRMED = "confirmed"
STATUS_IGNORED = "ignored"
VALID_STATUSES = (STATUS_NEW, STATUS_CONFIRMED, STATUS_IGNORED)

SOURCE_AUTO = "auto"                    # 驱动/spooler 自己报的页数
SOURCE_CONFIRMED = "confirmed"          # 人工补录
SOURCE_UNKNOWN = "unknown"              # 谁也报不出来（1020 打满页数时的常态）

DUPLEX_ON, DUPLEX_OFF, DUPLEX_UNKNOWN = "on", "off", ""
COLOR_UNKNOWN, COLOR_MONO, COLOR_COLOR = 0, 1, 2

# 一条作业在队列里挂这么久还没被扫到，多半是卡住的陈年行（重启后 spooler 残留），
# 记进账本只会把昨天的账重复记一遍 ⇒ 判 stale 只记日志不记账。
STALE_AFTER_SEC = 48 * 3600

# PRINTER_CHANGE_* 位（winspool.h）。只用与"作业"有关的三位：新增/修改（页数在修改里过来）/删除。
PRINTER_CHANGE_ADD_JOB = 0x00000100
PRINTER_CHANGE_SET_JOB = 0x00000200
PRINTER_CHANGE_DELETE_JOB = 0x00000400
WATCH_FLAGS = PRINTER_CHANGE_ADD_JOB | PRINTER_CHANGE_SET_JOB | PRINTER_CHANGE_DELETE_JOB

WAIT_SLICE_MS = 2000                    # 单次等待上限：到点也照扫一遍（事件可能永远不来，见模块头 ③）

CSV_HEADERS = ["时间", "打印机", "文档", "用户", "页数", "页数来源",
               "单双面", "彩色", "份数", "金额", "状态"]

STATUS_LABELS = {STATUS_NEW: "待处理", STATUS_CONFIRMED: "已确认", STATUS_IGNORED: "已忽略"}
SOURCE_LABELS = {SOURCE_AUTO: "自动", SOURCE_CONFIRMED: "人工", SOURCE_UNKNOWN: "未知"}


# ══════════════════════════════════════════════════════════════════
# 自家作业标记
# ══════════════════════════════════════════════════════════════════

def tag_self_title(title: str) -> str:
    """给本工具提交的作业文档名加前缀（幂等：已经带前缀不再叠一层）。"""
    text = str(title or "")
    return text if text.startswith(SELF_TAG) else SELF_TAG + text


def is_self_title(title: str) -> bool:
    return str(title or "").startswith(SELF_TAG)


# TTL 内登记的作业名也按自家作业跳过（见 note_self_job 的注释解释代价）
SELF_TITLE_TTL_SEC = 20 * 60

_self_title_lock = threading.Lock()
_self_title_until: dict[str, float] = {}


def note_self_job(title: str, ttl: Optional[float] = None) -> None:
    """登记「本工具即将提交的作业名」，TTL 内按自家作业跳过。

    存在的唯一理由：**SumatraPDF 兜底路径**（原生 GDI 失败才降级过去）是它自己起 StartDoc，
    作业名取自 PDF 文件名，我们插不进 SELF_TAG 前缀 —— 不登记就会被当成外部打印再收一遍钱。
    代价必须写清楚：TTL 内若有外部程序打印**同名**文件，会被当成自家作业漏记。
    因此跳过时留日志（"跳过自家作业"），不是静默丢弃；TTL 也刻意短（20 分钟）。
    """
    text = str(title or "").strip()
    if not text:
        return
    until = time.time() + float(SELF_TITLE_TTL_SEC if ttl is None else ttl)
    with _self_title_lock:
        _self_title_until[text] = until
        base = os.path.basename(text)
        if base:
            _self_title_until[base] = until


def recent_self_titles() -> set[str]:
    """未过期的自家作业名（顺带清掉过期项，注册表不会无限长）。

    **每轮扫描都要重取**：Sumatra 兜底是运行中临时登记的，构造监视器时算一次会漏掉它。
    """
    now = time.time()
    with _self_title_lock:
        for key in [k for k, until in _self_title_until.items() if until <= now]:
            _self_title_until.pop(key, None)
        return set(_self_title_until)


# ══════════════════════════════════════════════════════════════════
# JOB_INFO_2 取值（dict 是 pywin32 的形状，属性访问留给测试里的假对象）
# ══════════════════════════════════════════════════════════════════

def job_get(job: Any, key: str, default: Any = None) -> Any:
    """取作业字段：优先 dict 键，其次对象属性（两种都容错，缺键不抛）。"""
    if isinstance(job, dict):
        return job.get(key, default)
    return getattr(job, key, default)


def _as_int(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def devmode_of(job: Any) -> Any:
    """pDevMode → DEVMODE（dict 或 pywin32 的 PyDEVMODE 对象）；没有就返回 {}。

    ⚠ 真机上 pywin32 的 EnumJobs(level 2) / GetPrinter(level 2) 给的是 **PyDEVMODEW 对象**
    （属性访问），**不是 dict** —— 2026-10-28 实测：只认 dict 的写法让单双面/彩色/份数在实机上
    永远是「待确认/未知」（日志四条检测全是"待确认"，账本里的值全是人工补录的），而验收脚本的
    假作业是 dict，所以从没红过。两种形状都得认。
    """
    dm = job_get(job, "pDevMode")
    if dm is None:
        return {}
    if isinstance(dm, dict):
        return dm
    # PyDEVMODE 之类"带属性的 DEVMODE"：只认它确实长得像 DEVMODE（别把裸指针当对象用）
    if any(hasattr(dm, key) for key in ("Duplex", "Color", "Copies")):
        return dm
    return {}


def devmode_get(dm: Any, key: str, default: Any = None) -> Any:
    """从 DEVMODE 取一个字段：dict 走键、PyDEVMODE 走属性，取不到/读崩都给 default。"""
    if isinstance(dm, dict):
        return dm.get(key, default)
    try:
        return getattr(dm, key, default)
    except Exception:
        return default


def devmode_debug(job: Any) -> str:
    """一行诊断：原始 DEVMODE 的形状 + 三个关键字段。

    现场只有这一行能区分"驱动没写"与"我们没读"——两者在界面上都显示「待确认」。
    """
    dm = job_get(job, "pDevMode")
    if dm is None:
        return "pDevMode=None"
    shape = "dict" if isinstance(dm, dict) else type(dm).__name__
    return (f"pDevMode={shape} Duplex={devmode_get(dm, 'Duplex')!r} "
            f"Color={devmode_get(dm, 'Color')!r} Copies={devmode_get(dm, 'Copies')!r}")


def normalize_duplex(pages: int, duplex: str) -> str:
    """页数已知且只剩 1 页 ⇒ 一律按单面。

    1 页物理上只有一个面可印，驱动收下 Duplex=2 也是空操作 —— 与打印管线「有效页数 == 1 一律按单面」
    的单页硬闸门同一条口径（pdf_printer 三层闸门里的最外层）。**页数未知(<=0)时不动**：那是真不知道，
    不是 1 页，绝不猜。
    """
    if _as_int(pages, 0) == 1 and str(duplex or "") == DUPLEX_ON:
        return DUPLEX_OFF
    return str(duplex or "")


def duplex_mode_of(job: Any) -> str:
    """DEVMODE.Duplex → calc_cost 认的 "on"/"off"；**取不到就返回 ""，绝不猜**。

    为什么不能猜：Duplex=2/3 是双面（2 页 1 张，按 duplex_price 计），1 是单面
    （1 页 1 张，按 simplex_price 计）。猜错的后果是把双面单按单面收（顾客少付）
    或反过来（多收钱），两者都比"显示待确认"更坏。
    """
    dm = devmode_of(job)
    raw = devmode_get(dm, "Duplex")
    if raw is None:
        return DUPLEX_UNKNOWN
    value = _as_int(raw, 0)
    if value == 1:
        return DUPLEX_OFF
    if value in (2, 3):
        return DUPLEX_ON
    return DUPLEX_UNKNOWN


def color_of(job: Any) -> int:
    """DEVMODE.Color：1 单色 / 2 彩色，其余（缺键/怪值）= 未知。"""
    dm = devmode_of(job)
    raw = devmode_get(dm, "Color")
    if raw is None:
        return COLOR_UNKNOWN
    value = _as_int(raw, 0)
    return value if value in (COLOR_MONO, COLOR_COLOR) else COLOR_UNKNOWN


def copies_of(job: Any) -> int:
    dm = devmode_of(job)
    n = _as_int(devmode_get(dm, "Copies"), 0)
    return n if n > 0 else 1


def decide_pages(job: Any) -> tuple[int, str]:
    """页数三级回落：TotalPages → PagesPrinted → 未知（0）。返回 (pages, source)。

    1020 这类主机型驱动 TotalPages 常为 0；PagesPrinted 是"已印面数"，在作业还在队列里
    时可能只到一半 —— 但它比 0 有用得多，且作业被 DELETE 事件扫到时通常已是终值。
    两个都为 0 时**不许拿 1 页糊过去**：交给界面人工补录（source="unknown"）。
    """
    total = _as_int(job_get(job, "TotalPages"), 0)
    if total > 0:
        return total, SOURCE_AUTO
    printed = _as_int(job_get(job, "PagesPrinted"), 0)
    if printed > 0:
        return printed, SOURCE_AUTO
    return 0, SOURCE_UNKNOWN


def job_detected_at(job: Any, now: Optional[datetime] = None) -> str:
    """检测时刻（本地时间 ISO 秒）。优先用 spooler 的 Submitted，让同一作业每次扫到都同一时刻
    —— 账本按 (printer, job_id, detected_at) 去重，时刻漂移就会重复记账。"""
    now = now or datetime.now()
    submitted = _as_int(job_get(job, "Submitted"), 0)
    if submitted > 0:
        try:
            return datetime.fromtimestamp(submitted).strftime("%Y-%m-%d %H:%M:%S")
        except (OverflowError, OSError, ValueError):
            pass
    return now.strftime("%Y-%m-%d %H:%M:%S")


def printer_of(job: Any, fallback: str = "") -> str:
    name = str(job_get(job, "pPrinterName", "") or "").strip()
    return name or str(fallback or "").strip()


# ══════════════════════════════════════════════════════════════════
# 数据模型
# ══════════════════════════════════════════════════════════════════

@dataclass
class ExternalPrint:
    job_id: int = 0
    printer: str = ""
    document: str = ""
    user: str = ""
    pages: int = 0
    pages_source: str = SOURCE_UNKNOWN
    duplex: str = DUPLEX_UNKNOWN          # "on"/"off"/""（"" = DEVMODE 里没有或不认识，待人工确认）
    color: int = COLOR_UNKNOWN
    copies: int = 1
    size_bytes: int = 0
    detected_at: str = ""
    printer_virtual: bool = False
    status: str = STATUS_NEW

    def key(self) -> tuple[str, int, str]:
        return (self.printer, int(self.job_id), self.detected_at)

    def job_key(self) -> tuple[str, int]:
        return (self.printer, int(self.job_id))

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Any) -> Optional["ExternalPrint"]:
        """坏行返回 None（跳过），绝不让一整份账本因一条脏数据读不出来。"""
        if not isinstance(data, dict):
            return None
        names = {f.name for f in dataclass_fields(cls)}
        kwargs = {k: v for k, v in data.items() if k in names}
        try:
            item = cls(**kwargs)
        except (TypeError, ValueError):
            return None
        item.job_id = _as_int(item.job_id, 0)
        item.pages = _as_int(item.pages, 0)
        item.copies = _as_int(item.copies, 0) or 1
        item.size_bytes = _as_int(item.size_bytes, 0)
        item.printer = str(item.printer or "")
        item.document = str(item.document or "")
        item.user = str(item.user or "")
        item.detected_at = str(item.detected_at or "")
        item.pages_source = str(item.pages_source or SOURCE_UNKNOWN)
        if item.pages_source not in (SOURCE_AUTO, SOURCE_CONFIRMED, SOURCE_UNKNOWN):
            item.pages_source = SOURCE_UNKNOWN
        item.duplex = str(item.duplex or DUPLEX_UNKNOWN)
        if item.duplex not in (DUPLEX_ON, DUPLEX_OFF, DUPLEX_UNKNOWN):
            item.duplex = DUPLEX_UNKNOWN
        # 旧账本里「1 页 + 双面」的历史记录同样按单面显示（写入路径已在 build_external/_mutate 归一）
        item.duplex = normalize_duplex(item.pages, item.duplex)
        if item.color not in (COLOR_UNKNOWN, COLOR_MONO, COLOR_COLOR):
            item.color = COLOR_UNKNOWN
        if item.status not in VALID_STATUSES:
            item.status = STATUS_NEW
        item.printer_virtual = bool(item.printer_virtual)
        return item


# ══════════════════════════════════════════════════════════════════
# 金额（与 print_config 的计费同口径，单价一律由调用方从配置对象传进来）
# ══════════════════════════════════════════════════════════════════

def amount_for(item: ExternalPrint, simplex_price: float, duplex_price: float) -> Optional[float]:
    """外部打印的金额。**None = 算不出来**（页数未知或单双面未确认），不是 0。

    口径与 print_config.calc_cost 完全一致（按张计费：单面 1 页 1 张 × simplex_price；
    双面 2 页 1 张 × duplex_price + 余数 1 张 × simplex_price），所以这里直接调它，
    不另写一份。单价必须由调用方从 PrinterConfig 取（config.simplex_price / duplex_price）——
    硬编码默认值会让"配置改了价、账本金额不跟着变"。
    """
    pages = _as_int(getattr(item, "pages", 0), 0)
    if pages <= 0 or str(getattr(item, "duplex", "") or "") not in (DUPLEX_ON, DUPLEX_OFF):
        return None
    copies = _as_int(getattr(item, "copies", 1), 1) or 1
    cost, _formula = calc_cost(pages, copies, str(item.duplex),
                               simplex_price=float(simplex_price),
                               duplex_price=float(duplex_price))
    return round(float(cost), 2)


def duplex_label(duplex: str) -> str:
    return {DUPLEX_ON: "双面", DUPLEX_OFF: "单面"}.get(str(duplex or ""), "待确认")


def color_label(color: int) -> str:
    return {COLOR_MONO: "黑白", COLOR_COLOR: "彩色"}.get(int(color or 0), "未知")


# ══════════════════════════════════════════════════════════════════
# 判定：这条作业要不要记账
# ══════════════════════════════════════════════════════════════════

_virtual_check_cached: Optional[Callable[[str], bool]] = None


def _default_virtual_check(printer: str) -> bool:
    """虚拟打印机判定复用 pdf_printer.is_virtual_printer（唯一真相，别在本模块另写一份）。

    延迟导入原因：pdf_printer 会拉起 PyMuPDF/PIL/win32print（重），而 print_watch 又要被
    pdf_printer 反向导入（tag_self_title）—— 模块级互相 import 就成环了。
    导入失败一律当"真实打印机"：多记一条账本行可以人工忽略，漏记就是漏了钱。
    """
    global _virtual_check_cached
    if _virtual_check_cached is None:
        try:
            from pdf_printer import is_virtual_printer as _ivp
            _virtual_check_cached = _ivp
        except Exception as e:            # pragma: no cover - 依赖缺失/循环导入
            logger.warning(f"外部打印监视：拿不到虚拟打印机判定（{e}），按真实打印机处理")
            _virtual_check_cached = lambda _name: False
    return bool(_virtual_check_cached(printer))


def should_record(job: Any, self_titles: Iterable[str] = (), now: Optional[datetime] = None,
                  *, known_keys: Iterable[tuple[str, int]] = (),
                  virtual_check: Optional[Callable[[str], bool]] = None,
                  printer: str = "") -> tuple[bool, str]:
    """纯函数：这条作业要不要记账。返回 (要不要记, 原因)。

    原因取值：ok / self（自家作业）/ virtual（虚拟打印机）/ duplicate（已在账本）/
             stale（陈年残留）/ no_id（没有 JobId）。

    self_titles 是"本工具提交过的文档名"集合（调用方传 pdf_printer 实际用的标题）；
    正常情况下 SELF_TAG 前缀已经足够，这一份是给"标题被驱动改写/旧版没加前缀"兜底的。
    """
    now = now or datetime.now()
    job_id = _as_int(job_get(job, "JobId"), 0)
    if job_id <= 0:
        return False, "no_id"
    title = str(job_get(job, "pDocument", "") or "")
    if is_self_title(title) or (title and title in set(self_titles or ())):
        return False, "self"
    name = printer_of(job, printer)
    check = virtual_check or _default_virtual_check
    try:
        if name and check(name):
            return False, "virtual"
    except Exception as e:                # 判定本身出错不能把整条作业吞掉
        logger.warning(f"外部打印监视：虚拟打印机判定异常（{name}）: {e}")
    known = set(known_keys or ())
    if (name, job_id) in known:
        return False, "duplicate"
    submitted = _as_int(job_get(job, "Submitted"), 0)
    if submitted > 0:
        try:
            if (now - datetime.fromtimestamp(submitted)).total_seconds() > STALE_AFTER_SEC:
                return False, "stale"
        except (OverflowError, OSError, ValueError):
            pass
    return True, "ok"


def build_external(job: Any, now: Optional[datetime] = None, *,
                   printer: str = "", virtual: Optional[bool] = None,
                   self_titles: Iterable[str] = (),
                   virtual_check: Optional[Callable[[str], bool]] = None) -> ExternalPrint:
    """JOB_INFO_2 → ExternalPrint（页数走三级回落，字段缺一个也不抛）。"""
    name = printer_of(job, printer)
    pages, source = decide_pages(job)
    if virtual is None:
        check = virtual_check or _default_virtual_check
        try:
            virtual = bool(name) and bool(check(name))
        except Exception:
            virtual = False
    return ExternalPrint(
        job_id=_as_int(job_get(job, "JobId"), 0),
        printer=name,
        document=str(job_get(job, "pDocument", "") or ""),
        user=str(job_get(job, "pUserName", "") or ""),
        pages=pages,
        pages_source=source,
        duplex=normalize_duplex(pages, duplex_mode_of(job)),   # 1 页 ⇒ 单面（与单页硬闸门同口径）
        color=color_of(job),
        copies=copies_of(job),
        size_bytes=_as_int(job_get(job, "Size"), 0),
        detected_at=job_detected_at(job, now),
        printer_virtual=bool(virtual),
        status=STATUS_NEW,
    )


# ══════════════════════════════════════════════════════════════════
# 账本
# ══════════════════════════════════════════════════════════════════

def default_store_path() -> str:
    """账本默认位置：与 print_config.json **同一个目录**（%APPDATA%\\HN打印工具\\）。

    刻意与 printer_config 走同一份 paths 推导：换用户/换 APPDATA 时两处不会分家。
    """
    try:
        import paths as _paths                 # local_print_tool/paths.py（printer_config 用的同一份）
        return os.path.join(os.path.dirname(_paths.config_path()), EXTERNAL_PRINTS_FILENAME)
    except Exception as e:                     # pragma: no cover - 只在国内路径推导失败时走到
        logger.warning(f"外部打印监视：路径推导失败（{e}），回落 %APPDATA%")
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        return os.path.join(base, "HN打印工具", EXTERNAL_PRINTS_FILENAME)


def _atomic_write_json(path: str, data: Any) -> None:
    """同目录临时文件 + os.replace：写一半断电也不会留下半截 JSON。"""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


class ExternalPrintStore:
    """外部打印账本（%APPDATA%\\HN打印工具\\external_prints.json）。

    测试一律传临时路径 —— 本类**绝不**在构造时自己决定写到真实 %APPDATA%。
    """

    def __init__(self, path: str = ""):
        self.path = str(path or "") or default_store_path()
        self._items: list[ExternalPrint] = []
        self._loaded = False
        self._lock = threading.RLock()

    # ── 读 ──

    def load(self, force: bool = False) -> list[ExternalPrint]:
        with self._lock:
            if self._loaded and not force:
                return list(self._items)
            self._items = self._read_file()
            self._loaded = True
            return list(self._items)

    def _read_file(self) -> list[ExternalPrint]:
        if not os.path.exists(self.path):
            return []
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, ValueError, UnicodeDecodeError) as e:
            # 坏文件留证据（.corrupt 副本）但**不阻塞监视**：读不出来就当空账本继续记账，
            # 下一轮写盘会生成干净文件。半截 JSON（断电/并发写）是最常见的现场形态。
            logger.warning(f"外部打印账本损坏，已保留 .corrupt 副本并从空账本继续: {e}")
            try:
                shutil.copy2(self.path, self.path + ".corrupt")
            except OSError:
                pass
            return []
        if isinstance(raw, dict) and isinstance(raw.get("items"), list):
            raw = raw["items"]       # 兼容手工整理成 {"items": [...]} 的形态
        if not isinstance(raw, list):
            logger.warning("外部打印账本根节点不是列表，当作空账本")
            return []
        items: list[ExternalPrint] = []
        seen: set[tuple[str, int, str]] = set()
        for row in raw:
            item = ExternalPrint.from_dict(row)
            if item is None or item.key() in seen:
                continue
            seen.add(item.key())
            items.append(item)
        return items[-MAX_ENTRIES:]

    def items(self) -> list[ExternalPrint]:
        return self.load()

    def __len__(self) -> int:
        return len(self.load())

    def known_job_keys(self) -> set[tuple[str, int]]:
        return {item.job_key() for item in self.load()}

    def has(self, printer: str, job_id: int) -> bool:
        return (str(printer or ""), _as_int(job_id, 0)) in self.known_job_keys()

    # ── 写 ──

    def add(self, item: ExternalPrint) -> bool:
        """追加一条；重复键返回 False（同一作业被多个事件扫到只记一次）。"""
        if not isinstance(item, ExternalPrint):
            raise TypeError("ExternalPrintStore.add() 只接受 ExternalPrint")
        with self._lock:
            items = self.load()
            if any(x.key() == item.key() for x in items):
                return False
            items.append(item)
            if len(items) > MAX_ENTRIES:
                del items[:len(items) - MAX_ENTRIES]
            self._items = items
            self._loaded = True
            self.save()
            return True

    def update(self, item: ExternalPrint) -> bool:
        """按 key 整条替换（补录页数/改状态后落盘）。找不到返回 False。"""
        if isinstance(item, ExternalPrint):
            item.duplex = normalize_duplex(item.pages, item.duplex)   # 与 _mutate 同一条规则
        with self._lock:
            items = self.load()
            for i, old in enumerate(items):
                if old.key() == item.key():
                    items[i] = item
                    self._items = items
                    self.save()
                    return True
            return False

    def set_pages(self, index: int, pages: int) -> Optional[ExternalPrint]:
        """人工补录页数：pages_source 转 confirmed（来源列要能看出是人填的）。"""
        return self._mutate(index, pages=_as_int(pages, 0), pages_source=SOURCE_CONFIRMED)

    def set_status(self, index: int, status: str) -> Optional[ExternalPrint]:
        if status not in VALID_STATUSES:
            raise ValueError(f"未知状态: {status}")
        return self._mutate(index, status=status)

    def _mutate(self, index: int, **changes) -> Optional[ExternalPrint]:
        with self._lock:
            items = self.load()
            if not (0 <= int(index) < len(items)):
                return None
            item = items[int(index)]
            for key, value in changes.items():
                setattr(item, key, value)
            # 页数改成 1 之后单双面跟着归一（补录 1 页的作业不该还挂着「双面」）
            item.duplex = normalize_duplex(item.pages, item.duplex)
            items[int(index)] = item
            self._items = items
            self.save()
            return item

    def clear(self) -> None:
        with self._lock:
            self._items = []
            self._loaded = True
            self.save()

    def save(self) -> None:
        with self._lock:
            _atomic_write_json(self.path, [item.to_dict() for item in self._items])

    # ── 导出 ──

    def to_csv(self, path: str, simplex_price: Optional[float] = None,
               duplex_price: Optional[float] = None) -> int:
        """导出 CSV（utf-8-sig，Excel 直接打开不乱码）。返回导出行数。

        金额列按传入单价现算（同样是 calc_cost 口径）；单价没传就留空 ——
        宁可空着也不拿硬编码默认价糊一个金额给对账的人。
        """
        items = self.load()
        directory = os.path.dirname(str(path or "")) or "."
        os.makedirs(directory, exist_ok=True)
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(CSV_HEADERS)
            for item in items:
                if simplex_price is None or duplex_price is None:
                    amount = ""
                else:
                    value = amount_for(item, simplex_price, duplex_price)
                    amount = "" if value is None else f"{value:.2f}"
                writer.writerow([
                    item.detected_at, item.printer, item.document, item.user,
                    item.pages if item.pages > 0 else "",
                    SOURCE_LABELS.get(item.pages_source, item.pages_source),
                    duplex_label(item.duplex), color_label(item.color),
                    item.copies, amount, STATUS_LABELS.get(item.status, item.status),
                ])
        return len(items)


# ══════════════════════════════════════════════════════════════════
# Spooler 访问（ctypes 订阅 + win32print 枚举）
# ══════════════════════════════════════════════════════════════════

class SpoolerApi:
    """winspool.drv 的最小封装（ctypes）。

    为什么不用 win32print：pywin32 **没有** FindFirstPrinterChangeNotification，只有 ctypes
    里那个；而 EnumJobs(level 2) 用 pywin32 的 dict 形状最省事 ⇒ 两条路各用其长。
    所有 ctypes 只在方法里碰：导入本模块不拉 winspool，非 Windows 机器也能 import。
    """

    def __init__(self):
        import ctypes
        from ctypes import wintypes
        self._ctypes = ctypes
        self._w = wintypes
        self._dll = ctypes.WinDLL("winspool.drv", use_last_error=True)
        self._k32 = ctypes.WinDLL("kernel32.dll", use_last_error=True)
        self._dll.OpenPrinterW.restype = wintypes.BOOL
        self._dll.OpenPrinterW.argtypes = [wintypes.LPWSTR, ctypes.POINTER(wintypes.HANDLE), ctypes.c_void_p]
        self._dll.ClosePrinter.argtypes = [wintypes.HANDLE]
        self._dll.ClosePrinter.restype = wintypes.BOOL
        self._dll.FindFirstPrinterChangeNotification.restype = wintypes.HANDLE
        self._dll.FindFirstPrinterChangeNotification.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
        self._dll.FindClosePrinterChangeNotification.argtypes = [wintypes.HANDLE]
        self._dll.FindClosePrinterChangeNotification.restype = wintypes.BOOL
        self._k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self._k32.WaitForSingleObject.restype = wintypes.DWORD
        self._invalid = ctypes.c_void_p(-1).value

    def open(self, printer_name: str):
        handle = self._w.HANDLE()
        if not self._dll.OpenPrinterW(str(printer_name or ""), self._ctypes.byref(handle), None):
            raise OSError(self._ctypes.get_last_error(), f"OpenPrinter({printer_name}) 失败")
        return handle

    def close(self, handle) -> None:
        try:
            self._dll.ClosePrinter(handle)
        except Exception:
            pass

    def subscribe(self, handle, flags: int = WATCH_FLAGS):
        notif = self._dll.FindFirstPrinterChangeNotification(handle, int(flags), 0, None)
        if not notif or notif == self._invalid:
            raise OSError(self._ctypes.get_last_error(), "FindFirstPrinterChangeNotification 失败")
        return notif

    def unsubscribe(self, notif) -> None:
        try:
            self._dll.FindClosePrinterChangeNotification(notif)
        except Exception:
            pass

    def wait(self, notif, timeout_ms: int) -> int:
        """返回 WAIT_OBJECT_0(0)/WAIT_TIMEOUT(258)/…；事件只是"该扫一遍了"的提示。"""
        return int(self._k32.WaitForSingleObject(notif, int(timeout_ms)))


def default_enum_jobs(printer: str) -> list[dict]:
    """EnumJobs(level 2) —— 只读，不发作业、不暂停/删除任何作业（红线）。"""
    import win32print
    handle = None
    try:
        handle = win32print.OpenPrinter(str(printer or ""))
        jobs = win32print.EnumJobs(handle, 0, 99, 2) or []
        return [job for job in jobs if isinstance(job, dict)]
    except Exception as e:
        logger.warning(f"外部打印监视：枚举作业失败（{printer}）: {e}")
        return []
    finally:
        if handle is not None:
            try:
                win32print.ClosePrinter(handle)
            except Exception:
                pass


def default_list_printers() -> list[str]:
    """本机全部队列（本地 + 连接）。虚拟打印机不在这里过滤 —— 交给 should_record 记日志。"""
    import win32print
    try:
        flags = win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
        return [str(row[2]) for row in win32print.EnumPrinters(flags) if len(row) > 2 and row[2]]
    except Exception as e:
        logger.warning(f"外部打印监视：枚举打印机失败: {e}")
        return []


# ══════════════════════════════════════════════════════════════════
# 监视器
# ══════════════════════════════════════════════════════════════════

class PrintJobWatcher:
    """后台线程订阅 spooler 作业事件，把外部作业交给 on_external 回调。

    回调在**监视线程**里执行：它绝不许碰 Qt 控件（GUI 侧用 QObject 的 Signal 转发到主线程）。
    扫描逻辑（scan_once/consider）与线程、ctypes 全解耦，验收脚本可离线直接调。
    """

    def __init__(self, store: Optional[ExternalPrintStore] = None, *,
                 enum_jobs: Optional[Callable[[str], list]] = None,
                 printers: Optional[Iterable[str]] = None,
                 on_external: Optional[Callable[[ExternalPrint], None]] = None,
                 on_log: Optional[Callable[[str], None]] = None,
                 self_titles: Iterable[str] = (),
                 virtual_check: Optional[Callable[[str], bool]] = None,
                 spooler: Any = None,
                 wait_ms: int = WAIT_SLICE_MS):
        self.store = store if store is not None else ExternalPrintStore()
        self.enum_jobs = enum_jobs or default_enum_jobs
        self._printers = [str(p) for p in (printers or []) if str(p or "").strip()]
        self.on_external = on_external
        self.on_log = on_log
        # self_titles 可以是静态集合，也可以是**取名字的可调用对象**（GUI 传 recent_self_titles，
        # 让运行中才登记的 Sumatra 兜底作业名也能生效）。
        if callable(self_titles):
            self._self_titles_fn = self_titles
            self.self_titles = set()
        else:
            self._self_titles_fn = None
            self.self_titles = {str(t) for t in (self_titles or ()) if str(t or "")}
        self.virtual_check = virtual_check
        self._spooler = spooler
        self.wait_ms = max(int(wait_ms or 0), 0)

        self._thread: Optional[threading.Thread] = None
        self._stopping = threading.Event()
        self._lifecycle = threading.RLock()
        self._handles: list[tuple[str, Any, Any]] = []      # (队列名, 通知句柄, 打印机句柄)
        self._virtual_logged: set[str] = set()

    # ── 生命周期 ──

    def is_running(self) -> bool:
        thread = self._thread
        return bool(thread and thread.is_alive())

    def list_printers(self) -> list[str]:
        return list(self._printers) if self._printers else default_list_printers()

    def start(self) -> bool:
        with self._lifecycle:
            if self.is_running():
                return True
            try:
                if self._spooler is None:
                    self._spooler = SpoolerApi()
            except Exception as e:
                self._emit_log(f"⚠ 外部打印监视启动失败：拿不到 winspool（{e}）")
                return False
            names = self.list_printers()
            if not names:
                self._emit_log("⚠ 外部打印监视：没有可监视的打印队列")
                return False
            opened = 0
            for name in names:
                try:
                    handle = self._spooler.open(name)
                    notif = self._spooler.subscribe(handle, WATCH_FLAGS)
                except Exception as e:
                    self._emit_log(f"⚠ 外部打印监视：订阅「{name}」失败（{e}）")
                    continue
                self._handles.append((name, notif, handle))
                opened += 1
            if not opened:
                self._emit_log("⚠ 外部打印监视：所有队列都订阅失败，未启动")
                self._close_handles()
                return False
            self._stopping.clear()
            self._thread = threading.Thread(target=self._loop, name="hn-external-print-watch",
                                            daemon=True)
            self._thread.start()
            self._emit_log(f"👀 外部打印监视已启动（{opened} 个队列）")
            return True

    def stop(self, timeout: float = 5.0) -> bool:
        with self._lifecycle:
            self._stopping.set()
            thread, self._thread = self._thread, None
            clean = True
            if thread is not None and thread.is_alive():
                thread.join(timeout=max(float(timeout), 0.0))
                clean = not thread.is_alive()
                if not clean:
                    self._emit_log("⚠ 外部打印监视线程未在超时内退出（句柄仍会被关闭）")
            self._close_handles()
            if thread is not None:
                self._emit_log("👀 外部打印监视已停止")
            return clean

    def _close_handles(self) -> None:
        for _name, notif, handle in self._handles:
            try:
                self._spooler.unsubscribe(notif)
            except Exception:
                pass
            try:
                self._spooler.close(handle)
            except Exception:
                pass
        self._handles = []

    # ── 线程体 ──

    def _loop(self) -> None:
        # 事件只是唤醒信号：1020 从不产生 307、有时连 ADD_JOB 都晚到，所以到点也照扫一遍。
        while not self._stopping.is_set():
            try:
                self._wait_once()
            except Exception as e:
                logger.warning(f"外部打印监视：等待事件出错: {e}")
                self._stopping.wait(1.0)
            if self._stopping.is_set():
                break
            try:
                self.scan_once()
            except Exception as e:
                logger.warning(f"外部打印监视：扫描出错: {e}")

    def _wait_once(self) -> None:
        if not self._handles:
            self._stopping.wait(max(self.wait_ms, 0) / 1000.0)
            return
        # 第一个句柄阻塞等，其余只做非阻塞查询（队列通常只有 1~2 个，够用且不引入
        # WaitForMultipleObjects 的 64 句柄上限问题）。
        for index, (_name, notif, _handle) in enumerate(list(self._handles)):
            self._spooler.wait(notif, self.wait_ms if index == 0 else 0)

    # ── 扫描 ──

    def scan_once(self, now: Optional[datetime] = None) -> int:
        """扫一遍所有被监视队列，返回本轮新记下的条数（可在无线程环境下直接调用）。"""
        now = now or datetime.now()
        added = 0
        for printer in self.list_printers():
            try:
                jobs = self.enum_jobs(printer) or []
            except Exception as e:
                logger.warning(f"外部打印监视：取「{printer}」作业列表失败: {e}")
                continue
            for job in jobs:
                item = self.consider(printer, job, now)
                if item is not None:
                    added += 1
        return added

    def _current_self_titles(self) -> set[str]:
        """本轮要排除的自家作业名 = 静态集合 + 运行中登记的（recent_self_titles）。"""
        titles = set(self.self_titles)
        fn = getattr(self, "_self_titles_fn", None)
        if fn is not None:
            try:
                titles |= {str(t) for t in (fn() or ()) if str(t or "")}
            except Exception as e:
                # 取不到就按"没有登记"处理：多记一条可人工忽略，漏记就是漏钱
                logger.warning(f"外部打印监视：取自家作业名失败（本轮按无登记处理）: {e}")
        return titles

    def consider(self, printer: str, job: Any, now: Optional[datetime] = None,
                 store: Optional[ExternalPrintStore] = None) -> Optional[ExternalPrint]:
        """单条作业的判定 + 记账 + 回调（测试直接调它，不必起线程）。"""
        store = store or self.store
        now = now or datetime.now()
        self_titles = self._current_self_titles()
        ok, reason = should_record(job, self_titles, now,
                                   known_keys=store.known_job_keys(),
                                   virtual_check=self.virtual_check, printer=printer)
        if not ok:
            if reason == "duplicate":
                self._maybe_fill_pages(printer, job, store)
            else:
                self._log_skip(printer, job, reason)
            return None
        item = build_external(job, now, printer=printer, virtual=False,
                              self_titles=self_titles,
                              virtual_check=self.virtual_check)
        if not store.add(item):
            return None
        self._emit_log(f"🧾 检测到外部打印：{item.printer} / {item.document or '(无文档名)'} / "
                       f"{item.pages if item.pages > 0 else '页数未知'} 页")
        # 原始 DEVMODE 一行：现场只有它能区分"驱动没写"与"我们没读"（两者界面上都是待确认）
        self._emit_log(f"🧾 外部打印解析：{devmode_debug(job)}")
        callback = self.on_external
        if callback:
            try:
                callback(item)
            except Exception as e:
                logger.warning(f"外部打印监视：on_external 回调异常: {e}")
        return item

    def _maybe_fill_pages(self, printer: str, job: Any,
                          store: ExternalPrintStore) -> None:
        """已记过的作业后来报出页数 → 把数字补进那条记录（页数来源仍是 auto）。

        为什么值得多这一手：1020 这类主机型驱动在 ADD_JOB 时 TotalPages/PagesPrinted 都是 0
        （只能人工补），但能出 307 / 会填 TotalPages 的机型（同机的 MFP M232）是作业跑起来后
        才报的。不补的话，同一台机器上"本来能自动"的每一单也要人点一遍提示框。
        只对**同一条记录**填空，绝不新增第二条（去重口径不受影响），也不覆盖人工补过的值。
        """
        pages, source = decide_pages(job)
        if pages <= 0:
            return
        key = (str(printer or ""), _as_int(job_get(job, "JobId"), 0))
        for item in store.load():
            if item.job_key() != key or item.pages > 0:
                continue
            item.pages = pages
            item.pages_source = source
            store.update(item)
            self._emit_log(f"🧾 外部打印页数补全：{item.document or '(无文档名)'} → {pages} 页")
            return

    def _log_skip(self, printer: str, job: Any, reason: str) -> None:
        if reason == "virtual":
            # 虚拟打印机只记一行日志（不记账）：见到过"有人往 Print to PDF 发东西"这件事本身
            # 对现场排查有用，但它不出纸、不能收费。每个队列只记一次，免得刷屏。
            if printer not in self._virtual_logged:
                self._virtual_logged.add(printer)
                self._emit_log(f"ℹ 外部打印监视：跳过虚拟打印机「{printer}」（不出纸，不记账）")
            return
        if reason == "duplicate":
            return                       # 同一作业每轮都会扫到，不刷日志
        title = str(job_get(job, "pDocument", "") or "")
        if reason == "self":
            logger.debug(f"外部打印监视：跳过自家作业「{title}」")
        elif reason == "stale":
            self._emit_log(f"ℹ 外部打印监视：跳过陈年残留作业「{title}」（{printer}）")
        else:
            logger.debug(f"外部打印监视：跳过作业「{title}」（{reason}）")

    def _emit_log(self, message: str) -> None:
        logger.info(message)
        callback = self.on_log
        if callback:
            try:
                callback(message)
            except Exception:
                pass
