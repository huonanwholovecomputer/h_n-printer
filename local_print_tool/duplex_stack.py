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

「纸还在不在」（2026-10 现场问题）
----------------------------------
第 1 趟与第 2 趟之间，程序**看不见**出纸槽/进纸盒：它只知道"第 1 趟打了什么"，
不知道"那叠纸现在还剩几张"。现场因此踩到：

  · 一个 3 页**单面**文件的第 2 趟槽位全是空白占位；用户打完直接把纸拿走，
    程序仍按原计划去数进纸盒 → 日志「没有待打的第二趟的纸叠」（它其实压根没有内容要打）；
  · 更糟：若此后又有含双面内容的单进叠，程序会照样送纸，
    第 2 趟的内容就打到别人的纸/错误的纸上 —— **整叠错位串页**。

修法 = 给账本加"存在状态"：`StackEntry.taken_plans`（已取走的文件计划下标）、
`original_sheet_count`（第 1 趟原始张数）。三条规则：

  1. **只允许撤走"第 2 趟无内容"的文件计划**（单面文件的纸、双面末张的空白槽、封面页）
     —— `plan_blank_second()`；撤走它 ≡ 第 2 趟少送几张纸，配对与叠序不受影响。
     第 2 趟还要内容的槽位撤走会被 `toggle_taken()` 硬拒（必然错页）。
  2. **第 2 趟只送"在叠"的槽位**：`build_plan()` 只吃 `active_plans()`。
     正确性依据与跨单等效性同源 —— 配对只在单/文件内完成，剔除槽位 ≡
     「把剩余各单拼接后整体逆序」（`tests/verify_duplex_stack.py` ⑦ 逐组合钉死）。
  3. **落机对账**：`present_sheet_count` = 进纸盒里**应该**有的张数
     （原始 − 已取走）。GUI 在开第 2 趟前要求操作员数够这个数再点 ——
     只要这个数对得上，逆序配对就成立；对不上必然错位，宁可停下问。
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field

import manual_duplex

BATCH_VERSION = 2
MANIFEST_NAME = "stack.json"
# 「人工保留」标记文件名：放在批次目录里。`keep_only_batch` 见到它就**不自动清理**
# （操作员在启动弹窗里选了"先保留、人工核对"的那种批次）。
KEEP_MARKER = ".keep"

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


def sheet_count_of_plans(plans) -> int:
    """一组文件计划（dict: label/pages/duplex）在纸叠里占几张纸。

    与 `manual_duplex.plan_passes` 同源 —— 单面文件每页各占一张纸、双面文件每两张配一张，
    绝不在本模块里另立一套算式（两处算错一处就是整叠错位）。
    计划为空 → 0（没有可打的）；计划**非空但每条都坏**（页数解析不出来）→ -1
    （与旧 `entry_sheet_count` 的哨兵一致：调用方据此判"文件计划损坏"，不要当成 0 张）。
    """
    file_plans = []
    given = 0
    for fp in (plans or []):
        if not isinstance(fp, dict):
            continue
        given += 1
        try:
            pages = [int(p) for p in (fp.get("pages") or [])]
        except (TypeError, ValueError):
            continue
        if not pages:
            continue
        file_plans.append(manual_duplex.FilePlan(str(fp.get("label", "") or ""), pages,
                                                 bool(fp.get("duplex", True))))
    if not file_plans:
        return -1 if given else 0
    try:
        return manual_duplex.plan_passes(file_plans, copies=1).sheet_count
    except Exception:
        return -1


def _int_index_list(value) -> list[int]:
    """把清单里的 `taken_plans` 归一化成合法下标列表。

    只认非负整数（bool 不算 —— `True` 是 1 的语法糖，但把它当"取走第 2 个计划"纯属误读）。
    非法项（非 int / bool / 负数）**丢弃**：它们不影响页序，留着只会让后续 save 把垃圾
    再写回清单。调用方据"原始项数 − 合法项数"判断有没有被丢弃，并据此把整叠标「存疑」。
    """
    if not isinstance(value, (list, tuple)):
        return []
    out: list[int] = []
    for x in value:
        if isinstance(x, bool) or not isinstance(x, int):
            continue
        if x < 0:
            continue
        out.append(x)
    return out


def _normalize_taken_plans(value, plan_count: int) -> tuple[list[int], int]:
    """清单里的 `taken_plans` → (合法下标, 被丢弃项数)。

    丢弃三类：非法项（非 int / bool / 负数）、**越界下标**（指向不存在的文件计划）、
    以及"整个字段不是列表"（这一态也算 1 项被丢弃 —— 它是"清单被动过/被写坏"的信号，
    不能因为拿不到长度就静默放过；独立审查报的就是这一条）。

    ⚠ **重复下标不算丢弃**：语义上 `taken_plans` 是集合，`[0,0]` 等价于 `[0]`，无害。
    旧写法用"原始长度 − 去重后长度"计数，会把重复项误标成"有非法项"（误报 taint）。
    越界下标绝不能留着：`toggle_taken` 之后 `to_dict` 会把垃圾原样写回清单，
    清单就永远自证"有个取走项"，而它对页序毫无作用。
    """
    if not isinstance(value, (list, tuple)):
        return [], (1 if value is not None else 0)
    kept: list[int] = []
    dropped = 0
    for x in value:
        if isinstance(x, bool) or not isinstance(x, int) or x < 0 or x >= plan_count:
            dropped += 1
            continue
        kept.append(x)
    return sorted(set(kept)), dropped


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
    sheet_count: int = 0           # 本单**当前在叠**的纸张数（撤走一部分后会变小）
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
    # ── 「纸还在不在」（2026-10 现场问题：程序不知道自己那叠纸被拿走了一部分）──
    # 第 1 趟实际打出的**原始**张数（含之后被取走的）；0 = 未知，回落 sheet_count。
    original_sheet_count: int = 0
    # 已取走的 file_plans 下标（0-based）。**只允许撤走"第 2 趟无内容"的计划**
    # （单面文件的纸 / 双面末张的空白槽 / 封面页）——见 toggle_taken。
    # 撤走一「份」= 撤走 file_plans 里对应那一条（_print_stack_pass1 每份一条）。
    taken_plans: list = field(default_factory=list)
    # 本单**当初打印时**的 first 约定（odd/even）。整叠的第 2 趟页序由 DuplexStack.first 定，
    # 本单只用它判断"这一份第 2 趟有没有内容"（能不能整份取走）——两者在正常流程里总是相等
    # （`gui._new_stack_batch` 把档案值同时写进两处），存一份是为了**别让判据依赖一个没落盘的
    # 属性**（静默失败：属性若没设上，判据会退回 odd，而 even 档案下"单面文件的第 2 趟有内容"
    # ⇒ 会放行撤走、静默少打内容）。
    first: str = manual_duplex.FIRST_ODD
    # 清单里 `taken_plans` **被丢弃的项数**（非法项 + 越界下标；非列表算 1）。
    # 只用于校验"清单有没有被写坏" ⇒ `DuplexStack.from_dict` 据此把整叠标「存疑」。
    # ⚠ 它**就是**丢弃数，别再减一次"合法项数"（旧写法 `raw_n − len(kept)` 是"原始项数"口径，
    #   换成丢弃数后不减了；否则 `[0, 99]` 会算成 1−1=0 ⇒ 漏报存疑，独立审查抓到过）。
    # 不落盘、不参与页序；运行时（GUI 里直接 toggle_taken）恒为 0。
    raw_taken_count: int = 0

    # ── 在叠视图（第 2 趟只吃这里的东西）──

    def active_plans(self) -> list[dict]:
        """剔除已取走的计划后，仍在纸叠里的文件计划（浅拷贝，保留 label/pages/duplex）。"""
        taken = {int(x) for x in (self.taken_plans or [])
                 if isinstance(x, int) and not isinstance(x, bool)}
        out = []
        for i, fp in enumerate(self.file_plans or []):
            if i in taken or not isinstance(fp, dict):
                continue
            out.append(dict(fp))
        return out

    def plan_sheet_count(self) -> int:
        """当前在叠张数（按 active_plans 实算，与 manual_duplex 同源）。

        没有在叠计划 = 0 张（这是"全部取走"这一合法状态，不是损坏）。
        """
        plans = self.active_plans()
        if not plans:
            return 0
        n = sheet_count_of_plans(plans)
        return max(0, int(n))

    def original_sheet_value(self) -> int:
        """原始张数（第 1 趟实际打出多少张）：写坏/缺省时回落 sheet_count。"""
        try:
            n = int(self.original_sheet_count or 0)
        except (TypeError, ValueError):
            n = 0
        return n if n > 0 else int(self.sheet_count or 0)

    def present_sheet_value(self) -> int:
        """当前在叠张数：优先按计划实算；没有计划（异常清单）时回落 sheet_count。"""
        if self.file_plans:
            return self.plan_sheet_count()
        return int(self.sheet_count or 0)

    def taken_count(self) -> int:
        """已被取走的合法下标个数。"""
        return len({int(x) for x in (self.taken_plans or [])
                    if isinstance(x, int) and not isinstance(x, bool) and int(x) >= 0})

    def is_plan_taken(self, index: int) -> bool:
        return int(index) in {int(x) for x in (self.taken_plans or [])
                              if isinstance(x, int) and not isinstance(x, bool)}

    def _plan(self, index: int):
        try:
            i = int(index)
        except (TypeError, ValueError):
            return None, None
        if i < 0 or i >= len(self.file_plans or []):
            return None, None
        fp = self.file_plans[i]
        return (dict(fp) if isinstance(fp, dict) else None), i

    def plan_second_pages(self, index: int) -> list:
        """该文件计划第 2 趟的槽位序列（**单内**页码 / None=空白占位）。越界返回 []。"""
        fp, _i = self._plan(index)
        if not fp:
            return []
        try:
            pages = [int(p) for p in (fp.get("pages") or [])]
        except (TypeError, ValueError):
            return []
        if not pages:
            return []
        return manual_duplex.plan_passes(
            [manual_duplex.FilePlan(str(fp.get("label", "") or ""), pages,
                                    bool(fp.get("duplex", True)))],
            copies=1, first=self.first).pass_pages(2)

    def plan_blank_second(self, index: int) -> bool:
        """该文件计划第 2 趟是否**全为空白占位** ⇒ 这一份的纸可以整份取走。

        判定按**本单当初打印时的约定**（`first`，见 StackEntry.first）来算，所以对
        odd/even 两种机型档案都自洽：
          · `first="odd"`（默认，产线就是它）：第 2 趟打每张纸的"后一面"——
            单面文件各张背面全空 ⇒ 可撤（这正是 2026-10 现场那个 3 页单面场景）；
            双面文件末张（页数为奇数）的后一面也是空 ⇒ 可撤（它确实没有背面内容）；
          · `first="even"`：第 2 趟打的是"前一页"——单面文件那一趟**装着内容** ⇒ 不可撤。

        ⚠ 这个判据必须与 `first` 同源。旧实现把 `plan_passes()` 的默认 odd 写死，
        机型档案一旦标了 `first="even"`（`tools/print_duplex_calibration.py --first even`
        可写、`gui` 会把它喂进纸叠），就会在**第 2 趟其实有内容**的计划上放行撤走，
        而且文案还谎称"第 1 趟即成品" —— 静默少打顾客的页面。

        ⚠ 只认"全为空白"，不认"末尾一个空白"：一个计划里既有内容又有空白时，
        整体撤走会把内容也撤掉（顾客的纸就少了），那是"放弃这一份"，不该由取纸动作表达。
        """
        pp = self.plan_second_pages(index)
        return bool(pp) and all(p is None for p in pp)

    def plan_second_text(self, index: int) -> str:
        """该文件计划第 2 趟内容的人话摘要（常驻窗/日志用）。

        `first` 约定影响"哪一页在第 2 趟"，而本方法只能按默认约定（odd）描述 ——
        所以措辞上只承诺"第 2 趟要打这些页"，不承诺"第 1 趟即成品"（那需要整叠的 first）。
        """
        fp, _i = self._plan(index)
        if not fp:
            return "—"
        pages = self.plan_second_pages(index)
        if not pages:
            return "无"
        shown = [str(p) for p in pages if p is not None]
        blanks = sum(1 for p in pages if p is None)
        if not shown:
            return "空白（无背面内容）"
        tail = f"（含 {blanks} 个空白占位）" if blanks else ""
        return "第 " + "、".join(shown) + " 页" + tail

    def toggle_taken(self, index: int, taken: bool) -> tuple[bool, str]:
        """切换某个文件计划的"已取走"状态 → (ok, 拒绝原因)。

        拒绝的两种情形：
          · 下标越界 / 不是合法计划；
          · `taken=True` 但该计划**不是"两趟都空白"** —— 撤走它要么让第 2 趟的内容
            打到别人的纸/错误的纸上（整叠错位、串页），要么把这一份已经打好的内容
            一起丢掉（顾客的纸就少了）。两种都不是"取纸"该表达的意思：
            要丢内容请走「放弃整叠」，要让第 2 趟少送纸请只撤"两趟皆空白"的那几份。

        成功后重算 `sheet_count`（= 当前在叠张数），并保证幂等（重复设置同一状态无副作用）。
        """
        fp, i = self._plan(index)
        if fp is None:
            return False, f"没有第 {index} 个文件计划，无法标记取走"
        if taken and not self.plan_blank_second(i):
            pp2 = self.plan_second_pages(i)
            if pp2 and all(p is None for p in pp2):
                # 第 2 趟本来是空白，但第 1 趟有内容：这是双面文件的末张（或 first=even 下的
                # 某一份）。撤走它页序仍然对得齐，但会把已打好的正面内容一起撤掉 —— 说清差别，
                # 别让操作员以为"这只是少一张空白纸"。
                why = ("整份取走会把这一份第 1 趟已经打好的内容也一起撤掉（顾客的纸就少了）。")
            else:
                why = (f"这一份第 2 趟还要打印（{self.plan_second_text(i)}）"
                       f"—— 取走它会让整叠第 2 趟错位串页。")
            return False, (f"「{fp.get('label') or '这一份'}」不能这样取走：{why}"
                           f" 只能整叠放弃，或把这些纸放回进纸盒。")
        cur = {int(x) for x in (self.taken_plans or [])
               if isinstance(x, int) and not isinstance(x, bool)}
        # ⚠ 顺序很关键：**先**定格"原始张数"，再重算"在叠张数"。
        #   `original_sheet_value()` 的兜底是 `sheet_count` —— 若先收缩 sheet_count，
        #   兜底就会拿到收缩后的值，`original − 在叠` 恒等于 0（对账基准静默失真，
        #   操作员按错的数字数纸，第 2 趟必然错位）。
        if int(self.original_sheet_count or 0) <= 0:
            self.original_sheet_count = int(self.sheet_count or 0)
        if taken:
            cur.add(i)
        else:
            cur.discard(i)
        self.taken_plans = sorted(cur)
        self.sheet_count = self.plan_sheet_count()
        return True, ""

    def second_plan(self):
        """本单第 2 趟的槽位计划（只含在叠的计划；页码为**单内**页码）。

        撤走（第 2 趟无内容）的计划被剔除 ⇒ 返回计划里的空白槽位自然变少。
        `first` 用本单**当初打印时的约定**（`StackEntry.first`）：整叠写完清单后若有人改了
        打印机档案，这里的口径仍与该单的实际打法定一致，不会出现"账本说空白、实际有内容"。
        """
        files = [manual_duplex.FilePlan(str(fp.get("label", "") or ""),
                                       [int(p) for p in (fp.get("pages") or [])],
                                       bool(fp.get("duplex", True)))
                 for fp in self.active_plans()]
        if not files:
            return manual_duplex.plan_passes([], first=self.first)
        return manual_duplex.plan_passes(files, copies=1, first=self.first)

    def ui_lines(self) -> list[dict]:
        """常驻窗用：一行一个文件计划（谁、哪几页、占几张纸、第 2 趟打什么、是否已取走）。"""
        rows = []
        for i, fp in enumerate(self.file_plans or []):
            if not isinstance(fp, dict):
                continue
            try:
                pages = [int(p) for p in (fp.get("pages") or [])]
            except (TypeError, ValueError):
                pages = []
            if not pages:
                continue
            one = {"label": str(fp.get("label", "") or "") or f"文件{i + 1}",
                   "pages": list(pages),
                   "duplex": bool(fp.get("duplex", True))}
            rows.append({
                "index": i,
                "label": one["label"],
                "pages": list(one["pages"]),
                "duplex": one["duplex"],
                "sheets": sheet_count_of_plans([one]),
                "second": self.plan_second_text(i),
                "blank_second": self.plan_blank_second(i),
                "taken": self.is_plan_taken(i),
            })
        return rows

    def to_dict(self) -> dict:
        return {
            "seq": int(self.seq),
            "title": self.title,
            "tab_key": self.tab_key,
            "order_id": int(self.order_id or 0),
            "order_number": self.order_number,
            "page_count": int(self.page_count or 0),
            "sheet_count": int(self.sheet_count or 0),
            "original_sheet_count": int(self.original_sheet_value()),
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
            "first": self.first,
            # ⚠ 这里必须**归一化**再落盘，不能裸 `int(x)`：运行时字段可能被外部写脏
            #   （清单被手工编辑/旧版客户端/半截写入），`int("x")` 会抛 ValueError
            #   ⇒ `save_manifest` 只 catch OSError ⇒ 整份清单落盘失败、静默丢状态。
            #   落盘只写合法下标；坏值由 `raw_taken_count` 在**读**的时候标记存疑。
            "taken_plans": _int_index_list(self.taken_plans),
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
        # 「纸还在不在」：v1 清单没有这两个字段 ⇒ taken_plans=[]（**全部在叠**，零回归）、
        # original_sheet_count 回落 sheet_count。
        # 非法项/越界下标/"整个字段不是列表"在这里就丢掉，但"被丢了多少"记进 raw_taken_count
        # ——`DuplexStack.from_dict` 据此把整叠标「存疑」（静默丢弃 = 静默改变第 2 趟送纸数）。
        taken, raw_taken_n = _normalize_taken_plans(d.get("taken_plans"), len(plans))
        try:
            orig = int(d.get("original_sheet_count", 0) or 0)
        except (TypeError, ValueError):
            orig = 0
        if orig < 0:
            orig = 0
        try:
            sheets = int(d.get("sheet_count", 0) or 0)
        except (TypeError, ValueError):
            sheets = 0
        if not orig:
            # 「原始张数」缺省时**用清单里的 sheet_count**（那时它还没被撤走收缩过）；
            # 若清单里的 sheet_count 也已经是"在叠张数"，`original_sheet_value()` 的兜底
            # 至少不会把它算得更小 —— 具体见 toggle_taken 里"重算前先定格原始张数"那道保险。
            orig = sheets
        return cls(
            seq=seq,
            title=str(d.get("title", "") or ""),
            tab_key=str(d.get("tab_key", "") or ""),
            order_id=int(d.get("order_id", 0) or 0),
            order_number=str(d.get("order_number", "") or ""),
            page_count=int(d.get("page_count", 0) or 0),
            sheet_count=sheets,
            original_sheet_count=orig,
            taken_plans=taken,
            raw_taken_count=raw_taken_n,
            state=str(d.get("state", STATE_PRINTING) or STATE_PRINTING),
            # 三态布尔：坏值（null / "" / 其它）必须回落 True —— 这是**危险默认值**。
            #   False 意味着整叠第 2 趟不做 180° 补偿、每张纸背面整体倒置，而且静默无提示。
            #   用 bool() 会让 null 静默变成 False（与同文件 reverse_second 的规则也不一致）。
            rotate=_bool_or(d.get("rotate"), True),
            created_at=str(d.get("created_at", "") or ""),
            pdf=str(d.get("pdf", "") or ""),
            file_plans=plans,
            pass1_pages=_int_or_none(d.get("pass1_pages")),
            pass2_pages=_int_or_none(d.get("pass2_pages")),
            tasks=[dict(t) for t in (d.get("tasks") or []) if isinstance(t, dict)],
            job_keys=[k for k in (d.get("job_keys") or []) if isinstance(k, (list, tuple))],
            note=str(d.get("note", "") or ""),
            first=(lambda _f: _f if _f in (manual_duplex.FIRST_ODD, manual_duplex.FIRST_EVEN)
                   else manual_duplex.FIRST_ODD)(str(d.get("first", "") or "")),
        )

    def sheets_text(self) -> str:
        return f"第 {self.seq} 单 · {self.sheet_count} 张"

    def present_sheets_text(self) -> str:
        """在叠张数文本（撤走过一部分时写明"原始→现在"，日志/窗口都要看得懂）。"""
        orig = self.original_sheet_value()
        cur = self.present_sheet_value()
        if cur == orig:
            return f"第 {self.seq} 单 · {cur} 张"
        return f"第 {self.seq} 单 · {cur} 张（原始 {orig} 张，已取走 {orig - cur} 张）"


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
        """**当前在叠**张数（撤走一部分纸之后会变小 —— 容量与第 2 趟都以它为准）。"""
        return sum(max(0, int(e.sheet_count or 0)) for e in self.entries)

    @property
    def present_sheet_count(self) -> int:
        """第 2 趟**要送**的纸数（= 进纸盒里应该有的张数）。

        这是对账闸门的基准：`第 1 趟原始张数 − 已取走张数`。与 total_sheets 同源，
        但按各单计划实算（清单被写坏时 sheet_count 与计划可能不一致，实算更可信）。
        """
        total = 0
        for e in self.entries:
            total += max(0, e.present_sheet_value())
        return total

    @property
    def original_sheets_count(self) -> int:
        """第 1 趟总共打出的原始张数（含之后被取走的）。

        ⚠ 刻意不叫 `original_sheet_count`：那会与 `StackEntry.original_sheet_count`
        **字段**同名而语义不同（这里是整叠求和），读代码的人极易拿错。
        """
        return sum(max(0, e.original_sheet_value()) for e in self.entries)

    @property
    def taken_sheet_count(self) -> int:
        """已被取走的张数（原始 − 在叠；按计划实算，最小 0）。"""
        return max(0, self.original_sheets_count - self.present_sheet_count)

    def active_entries(self) -> list:
        """还有在叠文件计划的单（第 2 趟真正要处理的单）。"""
        return [e for e in self.entries if e.active_plans()]

    def removable_plans(self) -> list:
        """[(seq, plan_index)] —— 可以整份取走的文件计划（第 2 趟无内容且尚未取走）。"""
        out = []
        for e in self.entries:
            for i, _fp in enumerate(e.file_plans or []):
                if e.is_plan_taken(i):
                    continue
                if e.plan_blank_second(i):
                    out.append((int(e.seq or 0), int(i)))
        return out

    def toggle_taken(self, seq: int, plan_index: int, taken: bool) -> tuple[bool, str]:
        """把某单里的某个文件计划标记为"纸已被取走/放回" → (ok, 拒绝原因)。

        纯账本操作（不落盘）：调用方改完必须 save_manifest，否则崩溃重启就丢了这段事实。
        """
        entry = self.find(seq)
        if entry is None:
            return False, f"纸叠里没有第 {seq} 单"
        return entry.toggle_taken(plan_index, taken)

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
        """把**在叠**的文件计划偏移成整叠合并 PDF 的全局页码（1-based）。

        ⚠ 这里就是 2026-10 现场问题的修复点：**已被人工取走的计划不进来**。
        取走一「份」纸 = 第 2 趟少送那一份对应的槽位数。因为纸张配对只在单/文件内完成，
        剔除槽位 ≡「把剩余各单拼接后整体逆序」——页序仍然完全正确，只是少了那几张纸。
        旧实现无条件吃掉全部计划 ⇒ 用户拿走了纸程序照旧去数进纸盒，
        后面的单就会整体错位串页（第 2 趟内容打到别人的纸上）。
        """
        plans: list = []
        off = 0
        for e in self.entries:
            for fp in e.active_plans():
                pages = [off + int(p) for p in (fp.get("pages") or [])]
                if not pages:
                    continue
                plans.append(manual_duplex.FilePlan(
                    str(fp.get("label", "") or ""), pages, bool(fp.get("duplex", True))))
            # 偏移按整个单的**原始页数**推进：合并 PDF 是在撤走之前生成的，
            # 单内页码是相对本单合并 PDF 的绝对页码，撤走某一份不会改变其它份的偏移。
            off += max(0, int(e.page_count or 0))
        return plans

    def build_plan(self):
        """整叠的第 2 趟计划（只用于打第 2 趟；第 1 趟早就在各单里打完了）。

        只吃在叠的计划：全部被取走时返回一个 0 张纸的**空计划**
        （调用方必须据此走"第 2 趟无内容"的收尾，绝不能真去送纸）。
        """
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
        """给翻纸窗/日志用的每单摘要（已取走一部分的单写明 原始→在叠）。"""
        lines = []
        for e in self.entries[:max(1, int(limit))]:
            mark = ""
            if e.state == STATE_FAILED:
                mark = "（第 1 趟失败）"
            elif e.state == STATE_PRINTING:
                mark = "（第 1 趟状态未知）"
            title = e.title or f"第 {e.seq} 单"
            orig = e.original_sheet_value()
            cur = e.present_sheet_value()
            sheets = (f"{cur} 张" if cur == orig
                      else f"{cur} 张（原始 {orig} 张，已取走 {orig - cur} 张）")
            lines.append(f"  #{e.seq} {title} —— {sheets}{mark}")
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
            # 在叠/原始张数落一份冗余快照：崩在"标记取走"与"重算 sheet_count"之间时，
            # 续打弹窗还能告诉操作员"进纸盒里应该有 N 张"（真正的权威仍是各单计划）。
            "present_sheet_count": int(self.present_sheet_count),
            "original_sheet_count": int(self.original_sheets_count),
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
        raw_entries = [x for x in (d.get("entries") or []) if isinstance(x, dict)]
        st.entries = [StackEntry.from_dict(x) for x in raw_entries]
        # 「已取走」的坏值一律标存疑：`taken_plans` 决定第 2 趟送哪几张纸，
        # 静默丢弃非法/越界下标 = 静默改变第 2 趟的送纸数（页序会错），必须让续打弹窗
        # 要求人工核对，而不是拿一份被悄悄改过的清单去打第 2 趟。
        # ⚠ `raw_taken_count` 现在**就是丢弃项数**（由 `_normalize_taken_plans` 给出），
        #   不要再减"去重后的合法项数" —— 那会让 `[0, 99]` 算成 0 而漏报存疑。
        for entry in st.entries:
            dropped = max(0, int(entry.raw_taken_count or 0))
            if dropped:
                st.taint(f"第 {entry.seq} 单的「已取走」标记有 {dropped} 个非法/越界项"
                         f"（已丢弃，请人工核对进纸盒里的纸）")
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
    """把纸叠清单原子写入批次目录（进程被杀/崩溃后仍能续打第 2 趟）。

    ⚠ 兜底异常类别必须宽：`to_dict()` 里任何一处对写坏的字段做转换都可能抛
    （`ValueError`/`TypeError`/`KeyError`…）。旧写法只 catch `OSError`，
    一次坏值就能让**整份清单落盘失败**并静默丢掉"哪几张纸被取走"这段事实。
    失败一律返回 False（只记日志、不往上抛）：调用方按"清单没落盘"处理，
    绝不能让一个写清单的异常冲进 Qt 事件循环。
    """
    try:
        os.makedirs(bdir, exist_ok=True)
        path = manifest_path(bdir)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(stack.to_dict(), f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        return True
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"攒批清单写入失败: {bdir} → {e!r}")
        return False


def entry_sheet_count(entry: StackEntry) -> int:
    """按单内文件计划重算该单的纸张数（校验清单自洽用；**含**已取走的计划 = 原始张数）。"""
    return sheet_count_of_plans(entry.file_plans or [])


def entry_present_sheet_count(entry: StackEntry) -> int:
    """按**在叠**文件计划重算该单当前张数（撤走过一部分后与 entry_sheet_count 不同）。"""
    return sheet_count_of_plans(entry.active_plans())


def load_manifest(bdir: str) -> DuplexStack | None:
    """读批次清单（损坏/半截文件返回 None，绝不抛）。

    解析是**容错**的（坏字段丢掉/回落），但容错会静默改变页序 —— 所以解析后再做一次
    自洽校验：
      · 每单的 `sheet_count` 必须与它的文件计划算出来的**在叠**张数一致（撤走过一部分后
        两者都比"原始张数"小，比的仍是同一口径；对不上说明清单被改坏）；
      · 「已取走」标记里的非法/越界下标已在 `DuplexStack.from_dict` 里标存疑。
    对不上就把整叠标成「存疑」，让启动续打的弹窗明确要求人工核对，
    而不是拿一份坏清单去打第 2 趟。

    ⚠ **全部单的文件计划都被取走时照样返回**（不返回 None）：这叠纸已经全是成品、没有第 2 趟
    可打，但"谁被取走"只记在清单里，而这一态恰恰最可能是清单被写坏的（缺 `taken_plans`、
    下标越界）。返回 None 会让调用方连"这叠被动过、要人工核对"都看不到。调用方（gui）
    据 `present_sheet_count == 0` / 第 2 趟槽位数为 0 识别它，走「按成品收尾」，
    绝不真去打第 2 趟（白走一遍纸）。
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
        got = entry_present_sheet_count(e)
        if got != int(e.sheet_count or 0):
            st.taint(f"第 {e.seq} 单的纸张数与页序不一致（清单 {e.sheet_count} / 实算 {got}）")
    return st


def find_pending_batch(root: str) -> tuple[str, DuplexStack] | None:
    """扫描批次目录，返回最新的「还欠第 2 趟」的批次 (bdir, stack)。

    只认「有清单 + 有单」的目录。取修改时间最新的那个：
    正常只会有一个（攒批是串行的），多个并存属于异常遗留（例如手工拷贝），
    取最新的最有可能是操作员正在处理的那一叠。

    ⚠ 「全部纸都被标记取走」（第 2 趟无内容）的清单**照样算遗留**：它不是"打完的批次"
    （清单还在磁盘上），而是"得让操作员确认一句、然后按成品收尾"的状态。调用方（gui）
    用 `present_sheet_count` 区分这两者。
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
    """清掉批次根目录下除 keep_dir 以外的遗留批次（启动时调用），返回清理数量。

    ⚠ **带"人工保留"标记的批次一律不动**（`KEEP_MARKER`）：操作员在启动弹窗里选了
    「先保留、人工核对」之后，下一次启动（往往是为了处理**另一个**更新的批次）若把它当
    "遗留批次"清掉，那叠纸对应的云端任务就永远不会被回报（只能等后端超时判失败）——
    静默丢数据。要清它必须先把标记去掉（GUI 那边"续打/放弃"收尾时会清）。
    """
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
        if is_kept(bdir):
            continue                      # 人工保留：宁可留着占点空间，也不能静默丢账
        if discard_batch(bdir):
            removed += 1
    return removed


def keep_mark_path(bdir: str) -> str:
    return os.path.join(bdir, KEEP_MARKER)


def is_kept(bdir: str) -> bool:
    """该批次是否被操作员标记为"人工保留、别自动清"（标记文件存在即真）。"""
    try:
        return os.path.isfile(keep_mark_path(bdir))
    except OSError:
        return False


def mark_keep(bdir: str, on: bool = True) -> bool:
    """打/清"人工保留"标记（失败只记日志返回 False，不影响主流程）。"""
    if not bdir or not os.path.isdir(bdir):
        return False
    try:
        p = keep_mark_path(bdir)
        if on:
            with open(p, "w", encoding="utf-8") as f:
                f.write("manual keep — 人工核对中，勿自动清理\n")
        elif os.path.isfile(p):
            os.remove(p)
        return True
    except OSError as e:
        import logging
        logging.getLogger(__name__).warning(f"攒批保留标记写入失败: {bdir} → {e}")
        return False
