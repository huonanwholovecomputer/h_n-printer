"""blank_split.py — 「空白页率」判定与拆单口径（2026-11-23 用户需求）

用户口径（2026-11-23 现场，订单 HN20261123-0010）
--------------------------------------------------
一个订单里混着「整份单面」和「双面」两种文件时，那些单面文件在双面页序里各占一张纸、
背面永远空着（第 2 趟往那个槽位打空白占位页）。这类订单的**空白页率**可能高得离谱
（现场那一单：4 个文件、38 张纸、27 张有一面是空白 = 71%），于是：

    空白页率过高 ⇒ 拆成两次打：先打「只需要单面」的那几个文件 → 操作员取走 →
    再打「需要双面」的文件。

口径（唯一，别在 GUI 里另算一套）
--------------------------------
    blanks = 纸叠里有**至少一面是空白**的纸张数（每张纸最多只有一面空，所以它恒等于
             `PrintPlan.blank_slots_first + blank_slots_second`）
    sheets = 纸叠总张数（`PrintPlan.sheet_count`，已含份数）
    ratio  = blanks / sheets

阈值（用户 2026-11-23 选定）
---------------------------
    · 无自动双面器（人工翻纸）        → 默认 40%
    · 自动双面机型                    → 默认 60%（它本来就按文件配对，空白率天然低一些，
                                         但一叠里混进大量单面文件时同样白打纸）
两者都是"达到或超过就触发询问"，不是"超过才触发"。

为什么口径放在这里
------------------
纸张怎么摊（单面文件每页独占一张纸、双面文件两页一张）**只由 `manual_duplex` 决定**。
本模块不重新摊纸，只把 `FileEntry` 喂给 `manual_duplex.build_sheets` 再数纸 —— 于是
"打印出来的纸叠"和"这里算的空白率"永远是同一份东西。任何"按页数自己估算"的写法都会
在混排（封面单独打印 / 页码范围 / 多份）时算偏。

⚠ 不要用 `pdf_printer.detect_trailing_blank_pages`（末尾空白页）替代：那是"文档尾部本来就
   是空白页"的另一件事，管的是裁掉不印，与"纸张背面没内容"无关。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import manual_duplex

# 触发阈值（用户 2026-11-23 选定；GUI 里可改，落在 print_config.json）
THRESHOLD_MANUAL = 0.40     # 无自动双面器（人工翻纸）
THRESHOLD_AUTO = 0.60       # 自动双面机型


@dataclass(frozen=True)
class FileEntry:
    """一份参与判定的文件（页号已应用页码范围、1-based、按文档顺序）。

    duplex=False ⇒ 整份单面（每页各占一张纸）；duplex=True 且 simplex_pages 非空 ⇒
    混排（那几页单面，其余双面配对）—— 与 `manual_duplex.file_plans_for_pages` 同一口径。
    """
    label: str
    pages: tuple[int, ...]
    duplex: bool = True
    simplex_pages: tuple[int, ...] = ()
    copies: int = 1

    @property
    def needs_back_side(self) -> bool:
        """这份文件在纸叠里会不会用到纸的背面（= 是否属于"双面组"）。"""
        return bool(self.duplex) and len(self.pages) > 1


@dataclass(frozen=True)
class Stat:
    """一单的空白页统计。"""
    sheets: int
    blanks: int
    simplex_sheets: int          # 「整份单面」那几份文件占的张数
    duplex_sheets: int           # 其余（双面 / 混排）文件占的张数
    simplex_labels: tuple[str, ...] = field(default_factory=tuple)
    duplex_labels: tuple[str, ...] = field(default_factory=tuple)

    @property
    def ratio(self) -> float:
        return (float(self.blanks) / float(self.sheets)) if self.sheets else 0.0

    @property
    def percent(self) -> float:
        return self.ratio * 100.0

    @property
    def splittable(self) -> bool:
        """有单面组也有双面组才谈得上"拆成两次"（只有一组时拆了也没意义）。"""
        return bool(self.simplex_labels) and bool(self.duplex_labels)


def _entries_to_plans(entries) -> list:
    """FileEntry 列表 → `manual_duplex.FilePlan` 列表（保持顺序，混排按段切）。"""
    plans: list = []
    for e in entries:
        plans.extend(manual_duplex.file_plans_for_pages(
            e.label, list(e.pages), bool(e.duplex),
            tuple(e.simplex_pages or ()), None))
    return plans


def summarize(entries) -> Stat:
    """数一单的纸：总张数 / 有一面空白的张数 / 两个组各占多少张。

    张数口径与真正打印时**同一条路**（`build_sheets`，含份数）；份数按每份自己算
    （`FileEntry.copies`），最后把结果相加 —— 与"整批重复 N 次"的结果相同。
    """
    ents = [e for e in (entries or ()) if e.pages]
    if not ents:
        return Stat(0, 0, 0, 0, (), ())

    sheets = blanks = 0
    simplex_sheets = duplex_sheets = 0
    simplex_labels: list[str] = []
    duplex_labels: list[str] = []
    for e in ents:
        copies = max(1, int(getattr(e, "copies", 1) or 1))
        sh = manual_duplex.build_sheets(_entries_to_plans([e]), copies=copies)
        sheets += len(sh)
        blanks += sum(1 for s in sh if s.side_a is None or s.side_b is None)
        if not e.needs_back_side:
            simplex_sheets += len(sh)
            if e.label not in simplex_labels:
                simplex_labels.append(e.label)
        else:
            duplex_sheets += len(sh)
            if e.label not in duplex_labels:
                duplex_labels.append(e.label)
    return Stat(sheets, blanks, simplex_sheets, duplex_sheets,
                tuple(simplex_labels), tuple(duplex_labels))


def should_split(stat: Stat, threshold: float) -> bool:
    """是否该把这一单拆成「先单面 / 取走 / 再双面」两次。

    两条同时成立才拆：① 有空白页码达到阈值；② 单面组和双面组都存在
    （只打单面的一单空白率也很高，但拆开毫无意义 —— 本来就一次打完）。
    """
    if stat is None or not stat.splittable:
        return False
    try:
        th = float(threshold)
    except (TypeError, ValueError):
        th = THRESHOLD_MANUAL
    if th <= 0:
        return False
    return stat.ratio >= th


def threshold_for(auto_duplex: bool) -> float:
    """按机型取默认阈值（GUI/worker 都走这里，避免两处各写一个数）。"""
    return THRESHOLD_AUTO if auto_duplex else THRESHOLD_MANUAL


def describe(stat: Stat, threshold: float) -> str:
    """给人看的一句话（对话框 / 日志共用）。"""
    if stat is None or not stat.sheets:
        return "没有可统计的纸张。"
    return (f"共 {stat.sheets} 张纸，其中 {stat.blanks} 张有一面是空白"
            f"（{stat.percent:.0f}%，阈值 {float(threshold) * 100:.0f}%）。")
