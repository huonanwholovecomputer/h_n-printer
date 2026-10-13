"""duplex_stack.py — 手动双面「攒批模式」的纸叠账本（纯逻辑）。

背景
----
无自动双面器的机器（HP LaserJet 1020 等）双面必须由程序分两趟 + 人工翻纸。
旧行为是**一个打印任务翻一次纸**（一次 PrintWorker 只处理一个标签页/订单），
「任务多、每单文件少」时效率极低：每个订单都要人跑到机器前翻一次。

攒批模式（多任务合并模式）：把连续到达的多个打印任务的**第 1 趟连续打出来**，
纸在出纸槽里叠成一叠；人工一次性确认后，整个纸叠按逆序统一打第 2 趟
—— **一轮只翻一次纸**。

本模块只做账本：谁在叠里、占了多少张纸、还能不能再收一单、清单怎么存/读、
第 2 趟的全局页序怎么由各单拼出来。不碰 Windows / 打印机 / Qt，
因此可以在任何机器上逐组合验收（tests/verify_duplex_stack.py）。

物理前提（与 manual_duplex.py 同一套，别另立一套）
-------------------------------------------------
· 出纸 face-down、后出的纸叠在上面 ⇒ 整叠第 2 趟 = 各单第 2 趟**按单逆序**拼接；
  等价于「把所有单合成一份文档后整体逆序」——因为纸张配对只在单/文件内部完成
  （`manual_duplex.build_sheets` 的配对范围是 FilePlan），绝不跨单配对。
  两种算法在 tests/verify_duplex_stack.py 里被逐组合钉死。
· 一叠纸一旦开始累积，**中途不能插入任何别的打印输出**（哪怕同单里的单面文件、
  封面页、别的订单）——插进去的纸会让第 2 趟的逆序整体错位、串页。
  所以本模块只判断「容量够不够 / 装订方向一致不一致」，
  「现在能不能收」的队列规则由调用方（gui.MainWindow）保证：攒批期间其余打印请求一律排队。

超时不自动跳过
--------------
第 2 趟必须由人把纸叠搬回进纸盒才可能打，**程序不可能自动补第 2 趟**。
所以攒批模式下「较大的超时」只能是"超时不跳过、只预警"：
`DuplexStack.warn_after_sec` 只用来把窗口变红，绝不自动放弃（放弃 = 一次废掉 N 单）。
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field

import manual_duplex

BATCH_VERSION = 1
MANIFEST_NAME = "stack.json"

# 出纸槽容量阈值（纸张数）。HP LaserJet 1020 规格：出纸 up to 100 张（face down），
# 留 10 张安全余量 ⇒ 默认 90。可按机型在打印机档案里改。
DEFAULT_CAPACITY = 90
MIN_CAPACITY = 10
MAX_CAPACITY = 400
# 第 2 趟等待多久开始变红预警（秒）。只预警，不自动跳过。
DEFAULT_WARN_AFTER_SEC = 1800
MIN_WARN_AFTER_SEC = 60
MAX_WARN_AFTER_SEC = 24 * 3600

REASON_CAPACITY = "capacity"     # 加上这一单会超出出纸槽容量
REASON_ROTATE = "rotate"         # 装订方向（长/短边）与本叠不一致，不能混打第 2 趟
REASON_EMPTY = "empty"           # 没什么可打的

STATE_PRINTING = "pass1_printing"   # 第 1 趟正在打（崩溃后状态未知 → 需要人工确认）
STATE_DONE = "pass1_done"           # 第 1 趟已完成，纸在叠里
STATE_FAILED = "pass1_failed"       # 第 1 趟报错（可能只出了一部分纸 → 整叠存疑）


def _bool_or(value, default: bool) -> bool:
    """三态布尔容错：只认 bool 与 0/1，其余（None/字符串/缺键）回落 default。

    与 printer_config._bool_or_default 同一理由：`reverse_second` 的**危险默认值是 False**
    —— 清单被写坏成 null 时若静默变成"正序"，整叠第 2 趟会整体反着打，
    每张纸的背面都错（比不打印更糟）。危险默认值必须回落，不能靠 bool() 的巧合。
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    return default


def clamp_capacity(value) -> int:
    """容量阈值归一化（坏值/超范围都夹到合法区间）。"""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return DEFAULT_CAPACITY
    return max(MIN_CAPACITY, min(MAX_CAPACITY, n))


def clamp_warn_after(value) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return DEFAULT_WARN_AFTER_SEC
    return max(MIN_WARN_AFTER_SEC, min(MAX_WARN_AFTER_SEC, n))


def sheets_for(pages: int, duplex: bool = True, copies: int = 1) -> int:
    """一个文件在纸叠里占几张纸（= 第 1 趟要走的槽位数）。

    双面 n 页占 ceil(n/2) 张；单面 n 页占 n 张（每页各一张纸，背面留空）。
    与 `manual_duplex.build_sheets` 的配对规则一一对应——改那里要同步改这里。
    """
    n = max(0, int(pages or 0))
    per = ((n + 1) // 2) if duplex else n
    return per * max(1, int(copies or 1))


def estimate_jobs_sheets(jobs, count_selected=None) -> tuple[int, bool]:
    """打印前估算一批任务占几张纸 → (张数, 是否有页数未知的文件)。

    count_selected(page_range, page_count) -> 实际要打的页数（调用方传，避免本模块依赖
    pdf_printer/printer_config 的解析口径；不传则用 page_count 原值）。

    页数未知（云端 Word 还没分析出来，page_count=0）时按 1 页兜底并置 unknown=True：
    宁可**低估**（让这一单先打进来），也不要在没数据时把单子永久挡在队列里；
    真正入叠时的张数以 worker 排完页序的实测值为准（见 StackEntry.sheet_count）。
    """
    total = 0
    unknown = False
    for j in jobs or []:
        n = int(getattr(j, "page_count", 0) or 0)
        dup = (getattr(j, "duplex", "off") or "off") == "on"
        copies = int(getattr(j, "copies", 1) or 1)
        rng = getattr(j, "page_range", "") or ""
        if n <= 0:
            unknown = True
            total += sheets_for(1, dup, copies)
            continue
        sel = n
        if count_selected is not None:
            try:
                got = int(count_selected(rng, n))
                if got > 0:
                    sel = got
            except Exception:
                sel = n
        total += sheets_for(sel, dup, copies)
    return total, unknown


@dataclass
class StackEntry:
    """纸叠里的一「单」= 一次打印请求（一个标签页/订单）的第 1 趟产出。

    单的边界很重要：纸张配对与逆序都只在单内/文件内进行，绝不跨单。
    """

    seq: int = 0
    title: str = ""
    tab_key: str = ""
    order_id: int = 0
    order_number: str = ""
    page_count: int = 0            # 本单合并 PDF（含封面）的页数
    sheet_count: int = 0           # 本单在纸叠里占的纸张数
    state: str = STATE_PRINTING
    rotate: bool = True
    created_at: str = ""
    pdf: str = ""                  # 本单合并 PDF 的文件名（批次目录内）
    # 单内文件计划（1-based、单内页码；重建第 2 趟的全局页序要用）
    file_plans: list = field(default_factory=list)
    pass1_pages: list = field(default_factory=list)   # 展示用（页码 / None=空白占位）
    pass2_pages: list = field(default_factory=list)
    # 回报云端/标记完成所需的任务快照（每项 = 一个云端子任务）
    tasks: list = field(default_factory=list)
    # 用来把本单的 job 从标签页里找回来（task_id / 文件路径）
    job_keys: list = field(default_factory=list)
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "seq": int(self.seq),
            "title": self.title,
            "tab_key": self.tab_key,
            "order_id": int(self.order_id or 0),
            "order_number": self.order_number,
            "page_count": int(self.page_count or 0),
            "sheet_count": int(self.sheet_count or 0),
            "state": self.state,
            "rotate": bool(self.rotate),
            "created_at": self.created_at,
            "pdf": self.pdf,
            "file_plans": [dict(fp) for fp in (self.file_plans or [])],
            "pass1_pages": [p if p is None else int(p) for p in (self.pass1_pages or [])],
            "pass2_pages": [p if p is None else int(p) for p in (self.pass2_pages or [])],
            "tasks": [dict(t) for t in (self.tasks or [])],
            "job_keys": [list(k) for k in (self.job_keys or [])],
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> "StackEntry":
        d = data or {}
        plans = []
        for fp in (d.get("file_plans") or []):
            if not isinstance(fp, dict):
                continue
            try:
                pages = [int(p) for p in (fp.get("pages") or [])]
            except (TypeError, ValueError):
                continue
            if not pages:
                continue
            plans.append({"label": str(fp.get("label", "") or ""),
                          "pages": pages,
                          "duplex": bool(fp.get("duplex", True))})
        def _int_or_none(seq):
            out = []
            for x in (seq or []):
                try:
                    out.append(None if x is None else int(x))
                except (TypeError, ValueError):
                    out.append(None)
            return out
        try:
            seq = int(d.get("seq", 0) or 0)
        except (TypeError, ValueError):
            seq = 0
        return cls(
            seq=seq,
            title=str(d.get("title", "") or ""),
            tab_key=str(d.get("tab_key", "") or ""),
            order_id=int(d.get("order_id", 0) or 0),
            order_number=str(d.get("order_number", "") or ""),
            page_count=int(d.get("page_count", 0) or 0),
            sheet_count=int(d.get("sheet_count", 0) or 0),
            state=str(d.get("state", STATE_PRINTING) or STATE_PRINTING),
            rotate=bool(d.get("rotate", True)),
            created_at=str(d.get("created_at", "") or ""),
            pdf=str(d.get("pdf", "") or ""),
            file_plans=plans,
            pass1_pages=_int_or_none(d.get("pass1_pages")),
            pass2_pages=_int_or_none(d.get("pass2_pages")),
            tasks=[dict(t) for t in (d.get("tasks") or []) if isinstance(t, dict)],
            job_keys=[k for k in (d.get("job_keys") or []) if isinstance(k, (list, tuple))],
            note=str(d.get("note", "") or ""),
        )

    def sheets_text(self) -> str:
        return f"第 {self.seq} 单 · {self.sheet_count} 张"


@dataclass
class Admission:
    """能否把新的一单收进当前纸叠。"""

    ok: bool
    reason: str = ""
    estimated_sheets: int = 0
    unknown_pages: bool = False

    def message(self, stack: "DuplexStack | None" = None) -> str:
        if self.ok:
            return ""
        if self.reason == REASON_ROTATE:
            return ("本单的装订方向（长边/短边）与当前纸叠不一致 —— "
                    "一叠纸的第 2 趟只能有一种旋转补偿，混打会有一半页面倒置。"
                    "请先打完/放弃当前纸叠。")
        if self.reason == REASON_CAPACITY:
            cap = stack.capacity if stack else DEFAULT_CAPACITY
            used = stack.total_sheets if stack else 0
            return (f"出纸槽放不下：已堆 {used} 张，本单还要 {self.estimated_sheets} 张，"
                    f"上限 {cap} 张。请先打第 2 趟腾空出纸槽。")
        return "本单暂不能入叠。"


class DuplexStack:
    """一叠正在累积的手动双面第 1 趟纸张（主线程状态机，gui.MainWindow 持有）。"""

    def __init__(self, capacity: int = DEFAULT_CAPACITY,
                 warn_after_sec: int = DEFAULT_WARN_AFTER_SEC,
                 printer_name: str = ""):
        self.capacity = clamp_capacity(capacity)
        self.warn_after_sec = clamp_warn_after(warn_after_sec)
        self.printer_name = printer_name or ""
        self.batch_id = ""
        self.created_at = ""
        self.rotate: bool | None = None      # 本叠统一的第 2 趟旋转补偿
        self.first: str = manual_duplex.FIRST_ODD
        self.reverse_second: bool = True
        self.entries: list[StackEntry] = []
        self.tainted: bool = False           # 有单的第 1 趟失败/崩溃 → 整叠页序存疑
        self.taint_reason: str = ""

    # ── 只读视图 ──

    @property
    def is_empty(self) -> bool:
        return not self.entries

    @property
    def count(self) -> int:
        return len(self.entries)

    @property
    def total_sheets(self) -> int:
        return sum(max(0, int(e.sheet_count or 0)) for e in self.entries)

    @property
    def remaining(self) -> int:
        return max(0, self.capacity - self.total_sheets)

    @property
    def last_seq(self) -> int:
        return max((int(e.seq or 0) for e in self.entries), default=0)

    def sheet_text(self) -> str:
        return f"{self.total_sheets}/{self.capacity} 张"

    def next_seq(self) -> int:
        return self.last_seq + 1

    # ── 准入判断 ──

    def admit(self, sheets: int, rotate: bool, unknown_pages: bool = False) -> Admission:
        """判断一张新单能不能收进当前纸叠。

        空叠永远能收（哪怕单张数超容量 —— 否则这单永远打不出来）；
        非空叠要同时满足「装订方向一致」与「容量够」。
        """
        sheets = max(0, int(sheets or 0))
        adm = Admission(ok=False, estimated_sheets=sheets, unknown_pages=bool(unknown_pages))
        if sheets <= 0:
            adm.reason = REASON_EMPTY
            return adm
        if self.is_empty:
            adm.ok = True
            return adm
        if bool(rotate) != bool(self.rotate):
            adm.reason = REASON_ROTATE
            return adm
        if self.total_sheets + sheets > self.capacity:
            adm.reason = REASON_CAPACITY
            return adm
        adm.ok = True
        return adm

    # ── 变更 ──

    def new_batch(self, batch_id: str = "", now: float | None = None) -> str:
        """开一批新叠（清空并重置旋转/污染标记），返回 batch_id。"""
        self.entries = []
        self.rotate = None
        self.tainted = False
        self.taint_reason = ""
        self.created_at = _fmt_time(now)
        if not batch_id:
            batch_id = "batch_" + time.strftime("%Y%m%d_%H%M%S", time.localtime(now or time.time()))
        self.batch_id = batch_id
        return batch_id

    def add(self, entry: StackEntry) -> StackEntry:
        """把一单收进纸叠（第一单定下本叠的装订方向）。"""
        if not self.entries:
            # ⚠ 顺序很关键：new_batch() 会把 rotate 重置成 None，必须在它之后再定方向，
            # 否则第一单的方向会被自己抹掉（然后第二单才"定"方向 → 第一单整叠错位）。
            if not self.batch_id or not self.created_at:
                keep = self.batch_id
                self.new_batch(keep)
            self.rotate = bool(entry.rotate)
            if not self.created_at:
                self.created_at = _fmt_time(None)
        entry.rotate = bool(self.rotate)
        self.entries.append(entry)
        return entry

    def find(self, seq: int) -> StackEntry | None:
        for e in self.entries:
            if int(e.seq or 0) == int(seq or 0):
                return e
        return None

    def clear(self) -> None:
        self.entries = []
        self.rotate = None
        self.tainted = False
        self.taint_reason = ""
        self.batch_id = ""
        self.created_at = ""

    def taint(self, reason: str) -> None:
        """标记整叠页序存疑（某单第 1 趟失败/崩溃遗留）——只提示，不自动放弃。"""
        self.tainted = True
        if reason and reason not in (self.taint_reason or ""):
            self.taint_reason = (self.taint_reason + "；" if self.taint_reason else "") + reason

    # ── 第 2 趟全局页序（唯一真相，与 manual_duplex 同源）──

    def file_plans(self) -> list:
        """把各单的文件计划偏移成**整叠合并 PDF** 的全局页码（1-based）。"""
        plans: list = []
        off = 0
        for e in self.entries:
            for fp in (e.file_plans or []):
                pages = [off + int(p) for p in (fp.get("pages") or [])]
                if not pages:
                    continue
                plans.append(manual_duplex.FilePlan(
                    str(fp.get("label", "") or ""), pages, bool(fp.get("duplex", True))))
            off += max(0, int(e.page_count or 0))
        return plans

    def build_plan(self):
        """整叠的第 2 趟计划（只用于打第 2 趟；第 1 趟早就在各单里打完了）。"""
        return manual_duplex.plan_passes(
            self.file_plans(), copies=1, first=self.first,
            reverse_second=self.reverse_second)

    def entry_pdfs(self, batch_dir: str) -> list[str]:
        """按加入顺序给出各单合并 PDF 的绝对路径（合并成整叠 PDF 用）。"""
        out = []
        for e in self.entries:
            name = e.pdf or f"entry_{int(e.seq or 0):04d}.pdf"
            out.append(os.path.join(batch_dir, name))
        return out

    def describe_lines(self, limit: int = 12) -> list[str]:
        """给翻纸窗/日志用的每单摘要。"""
        lines = []
        for e in self.entries[:max(1, int(limit))]:
            mark = ""
            if e.state == STATE_FAILED:
                mark = "（第 1 趟失败）"
            elif e.state == STATE_PRINTING:
                mark = "（第 1 趟状态未知）"
            title = e.title or f"第 {e.seq} 单"
            lines.append(f"  #{e.seq} {title} —— {e.sheet_count} 张{mark}")
        if len(self.entries) > max(1, int(limit)):
            lines.append(f"  …另有 {len(self.entries) - max(1, int(limit))} 单")
        return lines

    # ── 持久化（崩溃/重启后续打第 2 趟）──

    def to_dict(self) -> dict:
        return {
            "version": BATCH_VERSION,
            "batch_id": self.batch_id,
            "created_at": self.created_at,
            "printer": self.printer_name,
            "capacity": self.capacity,
            "warn_after_sec": self.warn_after_sec,
            "rotate": self.rotate,
            "first": self.first,
            "reverse_second": self.reverse_second,
            "tainted": self.tainted,
            "taint_reason": self.taint_reason,
            "entries": [e.to_dict() for e in self.entries],
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> "DuplexStack":
        d = data or {}
        st = cls(capacity=d.get("capacity", DEFAULT_CAPACITY),
                 warn_after_sec=d.get("warn_after_sec", DEFAULT_WARN_AFTER_SEC),
                 printer_name=str(d.get("printer", "") or ""))
        st.batch_id = str(d.get("batch_id", "") or "")
        st.created_at = str(d.get("created_at", "") or "")
        r = d.get("rotate")
        st.rotate = bool(r) if isinstance(r, bool) else None
        first = str(d.get("first", manual_duplex.FIRST_ODD) or manual_duplex.FIRST_ODD)
        st.first = first if first in (manual_duplex.FIRST_ODD, manual_duplex.FIRST_EVEN) \
            else manual_duplex.FIRST_ODD
        st.reverse_second = _bool_or(d.get("reverse_second"), True)
        st.tainted = bool(d.get("tainted", False))
        st.taint_reason = str(d.get("taint_reason", "") or "")
        st.entries = [StackEntry.from_dict(x) for x in (d.get("entries") or [])
                      if isinstance(x, dict)]
        if st.entries and st.rotate is None:
            st.rotate = bool(st.entries[0].rotate)
        return st


def _fmt_time(now: float | None = None) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now or time.time()))


def now_text() -> str:
    """当前时间的展示文本（清单/日志共用同一口径）。"""
    return _fmt_time(None)


# ── 批次目录（存各单合并 PDF + 清单）──


def batch_dir(root: str, batch_id: str) -> str:
    return os.path.join(root, batch_id or "batch_unknown")


def manifest_path(bdir: str) -> str:
    return os.path.join(bdir, MANIFEST_NAME)


def save_manifest(bdir: str, stack: DuplexStack) -> bool:
    """把纸叠清单原子写入批次目录（进程被杀/崩溃后仍能续打第 2 趟）。"""
    try:
        os.makedirs(bdir, exist_ok=True)
        path = manifest_path(bdir)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(stack.to_dict(), f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        return True
    except OSError as e:
        import logging
        logging.getLogger(__name__).warning(f"攒批清单写入失败: {bdir} → {e}")
        return False


def entry_sheet_count(entry: StackEntry) -> int:
    """按单内文件计划重算该单的纸张数（校验清单自洽用）。"""
    plans = [manual_duplex.FilePlan(str(fp.get("label", "") or ""),
                                    [int(p) for p in (fp.get("pages") or [])],
                                    bool(fp.get("duplex", True)))
             for fp in (entry.file_plans or [])]
    if not plans:
        return 0
    try:
        return manual_duplex.plan_passes(plans, copies=1).sheet_count
    except Exception:
        return -1


def load_manifest(bdir: str) -> DuplexStack | None:
    """读批次清单（损坏/半截文件返回 None，绝不抛）。

    解析是**容错**的（坏字段丢掉/回落），但容错会静默改变页序 —— 所以解析后再做一次
    自洽校验（每单的张数必须与它的文件计划算出来的张数一致）；对不上就把整叠标成
    「存疑」，让启动续打的弹窗明确要求人工核对，而不是拿一份坏清单去打第 2 趟。
    """
    try:
        with open(manifest_path(bdir), "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    try:
        st = DuplexStack.from_dict(data)
    except Exception:
        return None
    if not st.entries:
        return None
    raw_entries = [x for x in (data.get("entries") or []) if isinstance(x, dict)]
    for i, e in enumerate(st.entries):
        if i >= len(raw_entries):
            st.taint(f"第 {e.seq} 单的清单条目缺失")
            break
        raw_plans = raw_entries[i].get("file_plans")
        if not isinstance(raw_plans, list) or len(raw_plans) != len(e.file_plans):
            st.taint(f"第 {e.seq} 单的文件计划有损坏（{len(e.file_plans)}/{len(raw_plans or [])}）")
            continue
        got = entry_sheet_count(e)
        if got != int(e.sheet_count or 0):
            st.taint(f"第 {e.seq} 单的纸张数与页序不一致（清单 {e.sheet_count} / 实算 {got}）")
    return st


def find_pending_batch(root: str) -> tuple[str, DuplexStack] | None:
    """扫描批次目录，返回最新的「还欠第 2 趟」的批次 (bdir, stack)。

    只认「有清单 + 有单 + 至少一单还没作废」的目录。取修改时间最新的那个：
    正常只会有一个（攒批是串行的），多个并存属于异常遗留（例如手工拷贝），
    取最新的最有可能是操作员正在处理的那一叠。
    """
    if not os.path.isdir(root):
        return None
    best: tuple[float, str, DuplexStack] | None = None
    try:
        names = os.listdir(root)
    except OSError:
        return None
    for name in names:
        bdir = os.path.join(root, name)
        if not os.path.isdir(bdir):
            continue
        st = load_manifest(bdir)
        if st is None:
            continue
        try:
            mtime = os.path.getmtime(manifest_path(bdir))
        except OSError:
            mtime = 0.0
        if best is None or mtime > best[0]:
            best = (mtime, bdir, st)
    if best is None:
        return None
    return best[1], best[2]


def discard_batch(bdir: str) -> bool:
    """删掉一个批次目录（打完/放弃后清理；失败只记日志，不影响主流程）。"""
    if not bdir or not os.path.isdir(bdir):
        return True
    ok = True
    try:
        for name in os.listdir(bdir):
            p = os.path.join(bdir, name)
            try:
                if os.path.isfile(p):
                    os.remove(p)
            except OSError:
                ok = False
        try:
            os.rmdir(bdir)
        except OSError:
            ok = False
    except OSError:
        ok = False
    return ok


def keep_only_batch(root: str, keep_dir: str) -> int:
    """清掉批次根目录下除 keep_dir 以外的遗留批次（启动时调用），返回清理数量。"""
    removed = 0
    if not os.path.isdir(root):
        return 0
    keep = os.path.normcase(os.path.abspath(keep_dir)) if keep_dir else ""
    try:
        names = os.listdir(root)
    except OSError:
        return 0
    for name in names:
        bdir = os.path.join(root, name)
        if not os.path.isdir(bdir):
            continue
        try:
            if keep and os.path.normcase(os.path.abspath(bdir)) == keep:
                continue
        except OSError:
            continue
        if discard_batch(bdir):
            removed += 1
    return removed
