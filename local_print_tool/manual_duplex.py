"""manual_duplex.py — 手动双面（无自动双面器的打印机）的两趟页序规划。

纯逻辑，不碰 Windows / 打印机 / 文件系统，因此可以在任何机器上逐组合验收
（tests/verify_manual_duplex.py）。打印侧（pdf_printer）只负责把这里给出的两趟页序列
渲染出来，不再自己算页序——**页序只在这一处定义**。

物理依据（HP LaserJet 1020 用户手册 + 驱动「双面打印说明」弹窗原文）：

  · 装纸规则「顶边朝里、要打印的一面朝上」；出纸盒 face down（手册规格表
    "Top (face down) output bin"）。
  · 弹窗指令：「不改变方向，从出纸槽中取出纸叠」→「保持相同方向，将纸叠插入纸盘 2，
    使已打印面朝下」——即整叠**不翻面、不重排**；进纸盒从**最上面**一张取纸，
    于是第 2 趟的进纸顺序 = 第 1 趟印刷顺序的**逆序**
    ⇒ **第 2 趟必须整体逆序打印**（Word / Adobe 的手动双面同此）。
  · 弹窗「不要丢弃空白页面」⇒ 驱动自己在第 1 趟插空白占位页。本模块同理：
    **每张纸在两趟里各占且只占一个槽位，没有内容就补空白页**（漏一个，后面整本错位一页）。

两种约定（只影响"空白落在哪一趟"和**成品哪一面朝上**，配对与逆序不变）：

  first="odd"（**默认**，2026-10 HP LaserJet 1020 实机实测选定）
      第 1 趟打每张纸的前一页（双面文件的奇数页；单面文件该面留空），第 2 趟打后一页。
      成品叠**最上面就是第 1 页**（往下 P3、P5…），符合"拿起一叠从第 1 页开始翻"的直觉。
      代价：空白占位页落在第 2 趟（页数为奇数时，第 2 趟开头要送一张空白占位纸）。
  first="even"
      镜像方案：第 1 趟打后一页（偶数页），空白占位页只落在第 1 趟、第 2 趟恒为纯内容流。
      实测效果：配对同样正确，但成品叠**最上面是第 2 页**（往下 P4、P6…），观感像"从背面开始"。
      它对齐的是 HP 驱动弹窗那句「偶数页打印完后…打印奇数页」——即 HP 的**打印顺序**；但
      HP 驱动在内部还做了自己的页序/旋转补偿，我们照搬它的分趟顺序并得不到它的成品观感，
      所以最终按实测选了 odd。

  reverse_second 默认 True（对应「整叠原样放回、不翻面」）。若实机自检发现第 2 趟
  整体反了（例如操作员把整叠翻过面再放回），置 False。

⚠ 与"第 2 趟 180° 旋转补偿"的分工（别混）：
  · **本模块**决定"哪一页在哪一趟、以什么顺序"——即配对与叠序；
  · **打印侧**（pdf_printer.print_plan_pass 的 second_rotate_180）决定"内容要不要转 180°"来
    抵消纸路径翻转。两者无关：改约定不影响旋转，旋转也不影响叠序。
"""

from __future__ import annotations

from dataclasses import dataclass, field

# 约定取值（别用裸字符串散落在调用方）
FIRST_EVEN = "even"
FIRST_ODD = "odd"


@dataclass(frozen=True)
class FilePlan:
    """批内一个文件的打印计划（页码范围已应用，pages 为实际要打的 1-based 页码）。"""

    label: str
    pages: list[int]
    duplex: bool = True
    # 这份文件的第 2 面要不要转 180°（None = 跟 plan_passes 的默认值走）。
    # 值一般来自 rotate_second_for(档案 second_rotate_180, 该文件的 duplex_mode)：同一个批次
    # 里长边/短边可以混排 —— 操作员的动作完全相同（整叠原样放回），差异只在内容转不转。
    # 旧写法把"转不转"当成**整趟**一个开关，于是混排只能拆成两批、多翻一次纸（2026-10-27）。
    second_rotate: bool | None = None


@dataclass(frozen=True)
class Sheet:
    """一张纸：side_a = 先出现的那一页，side_b = 后出现的那一页（None = 该面空白）。"""

    label: str
    side_a: int | None
    side_b: int | None
    rotate_second: bool = False   # 这张纸第 2 面的内容要不要转 180°（来自所属 FilePlan）


@dataclass(frozen=True)
class PassSlot:
    """一趟里的一个槽位 = 进纸盒里被抽走的一张纸。page=None 表示打一张空白占位页。"""

    label: str
    page: int | None
    sheet_index: int
    rotate: bool = False          # 这一面要不要转 180°（第 1 趟恒 False；空白面无所谓）


@dataclass
class PrintPlan:
    """两趟打印计划。"""

    sheets: list[Sheet]
    first_pass: list[PassSlot]
    second_pass: list[PassSlot]
    copies: int = 1
    first: str = FIRST_ODD
    reverse_second: bool = True
    notes: list[str] = field(default_factory=list)

    @property
    def sheet_count(self) -> int:
        return len(self.sheets)

    @property
    def blank_slots_first(self) -> int:
        return sum(1 for s in self.first_pass if s.page is None)

    @property
    def blank_slots_second(self) -> int:
        return sum(1 for s in self.second_pass if s.page is None)

    @property
    def total_sides(self) -> int:
        """两趟合计的物理面数（= 纸张数 × 2；每张纸两趟各走一次，不计空白是否打墨）。"""
        return len(self.first_pass) + len(self.second_pass)

    def pass_pages(self, which: int) -> list[int | None]:
        """第 which 趟（1/2）的页码序列（1-based；None = 空白页）—— 给人看/记日志。"""
        seq = self.first_pass if which == 1 else self.second_pass
        return [s.page for s in seq]

    def pass_page_indices(self, which: int) -> list[int | None]:
        """第 which 趟的 **0-based** 页序列（None = 空白占位页）。

        直接可作 `pdf_printer.print_pdf(..., pages_override=...)` 的入参。
        ⚠ 渲染层要的是 0-based，引擎内部是 1-based 页码 —— 转换只做在这一处，
        避免每个调用点各写一遍再各错一页（多页文档上错一页 = 整本错位）。
        """
        return [None if s.page is None else s.page - 1 for s in self.pass_slots(which)]

    def pass_slots(self, which: int) -> list[PassSlot]:
        return list(self.first_pass if which == 1 else self.second_pass)

    def pass_rotate(self, which: int) -> list[bool]:
        """第 which 趟**逐面**的 180° 补偿表（True=该面内容转 180°）。

        直接作 pdf_printer.print_pdf(..., rotate_slots=...) 的入参 —— 一个批次里长边/短边
        混排时，每张纸按**自己那份文件**的装订方向补偿，而整批仍然只翻一次纸。
        """
        return [bool(s.rotate) for s in self.pass_slots(which)]

    def pair_lines(self) -> list[str]:
        """每张纸的两面（验收/日志用）："F1: 1/2"、"F3: 5/空白"。"""
        out = []
        for sh in self.sheets:
            a = "空白" if sh.side_a is None else str(sh.side_a)
            b = "空白" if sh.side_b is None else str(sh.side_b)
            prefix = f"{sh.label.rstrip(':')}: " if sh.label else ""
            out.append(f"{prefix}{a}/{b}")
        return out

    def describe(self, multi_file: bool | None = None) -> str:
        """人类可读的两趟摘要（打印日志 / 翻纸提示 / 排查工具共用）。"""
        if multi_file is None:
            multi_file = len({sh.label for sh in self.sheets if sh.label}) > 1

        def _fmt(slots: list[PassSlot]) -> str:
            parts = []
            for s in slots:
                if s.page is None:
                    parts.append("〔空白〕")
                elif multi_file:
                    parts.append(f"{s.label.rstrip(':')}:{s.page}")
                else:
                    parts.append(str(s.page))
            return " ".join(parts)

        lines = [
            f"共 {self.sheet_count} 张纸 × {self.copies} 份；"
            f"第 1 趟打{'后一面（偶数面）' if self.first != FIRST_ODD else '前一面（奇数面）'}，"
            f"第 2 趟{'逆序' if self.reverse_second else '正序'}（整叠不翻面、原样放回）",
            f"第 1 趟: {_fmt(self.first_pass)}",
            f"第 2 趟: {_fmt(self.second_pass)}",
        ]
        if self.blank_slots_first or self.blank_slots_second:
            lines.append(f"空白占位页: 第 1 趟 {self.blank_slots_first} 个、"
                         f"第 2 趟 {self.blank_slots_second} 个")
        if self.first != FIRST_ODD and self.blank_slots_second == 0:
            lines.append("（偶数面先打的约定下，第 2 趟每张纸都有内容，故恒为 0）")
        lines.extend(self.notes)
        return "\n".join(lines)


def build_sheets(files: list[FilePlan], copies: int = 1,
                 default_rotate: bool = False) -> list[Sheet]:
    """把整批文件摊成「纸张」列表（按第 1 趟的进纸/印刷顺序）。

    双面文件：第 k 张纸 = (pages[2k], pages[2k+1])，页数为奇数时最后一张的 side_b = None。
    单面文件：**每页各占一张纸** = (page, None) —— 不能把两页配到一张纸上。
    多份：整批重复 copies 次（第 2 趟统一逆序即可自动对齐各份的分组）。
    default_rotate：文件自己没指定 second_rotate 时用的第 2 面补偿（整批同一方向的旧口径）。
    """
    sheets: list[Sheet] = []
    for _ in range(max(1, copies)):
        for fp in files:
            pages = list(fp.pages or [])
            if not pages:
                continue
            rot = bool(default_rotate) if fp.second_rotate is None else bool(fp.second_rotate)
            if fp.duplex:
                for i in range(0, len(pages), 2):
                    sheets.append(Sheet(fp.label, pages[i],
                                        pages[i + 1] if i + 1 < len(pages) else None,
                                        rot))
            else:
                for p in pages:
                    sheets.append(Sheet(fp.label, p, None, rot))
    return sheets


def plan_passes(
    files: list[FilePlan],
    copies: int = 1,
    first: str = FIRST_ODD,
    reverse_second: bool = True,
    second_rotate_180: bool = True,
) -> PrintPlan:
    """给出两趟打印计划（本模块的入口）。

    Args:
        files: 批内文件（label/pages/duplex/second_rotate）。pages 必须是**已应用页码范围**后的页码。
        copies: 份数（整批重复；第 2 趟统一逆序，各份分组自动对齐）。
        first: "odd"（默认）| "even"（镜像约定）。
        reverse_second: 第 2 趟是否逆序（默认 True = 整叠原样放回）。
        second_rotate_180: 这台机器的纸路径会不会把第 2 趟内容整体转 180°（档案实测值）。
            文件没自带 second_rotate 时用它当默认 —— 于是"整批同向"的老口径保持原样，
            而自带 second_rotate 的文件按**自己**的装订方向走（同批可混排）。
    """
    if first not in (FIRST_EVEN, FIRST_ODD):
        raise ValueError(f"first 只能是 '{FIRST_EVEN}' 或 '{FIRST_ODD}'，收到 {first!r}")

    sheets = build_sheets(files, copies, default_rotate=bool(second_rotate_180))
    first_pass: list[PassSlot] = []
    second_pass: list[PassSlot] = []
    for idx, sh in enumerate(sheets):
        # "even" 约定：第 1 趟打 side_b（双面文件的偶数页 / 单面文件的空白面）
        head, tail = (sh.side_b, sh.side_a) if first == FIRST_EVEN else (sh.side_a, sh.side_b)
        # 第 1 趟永远不转（补偿只针对"翻纸后的那一面"）
        first_pass.append(PassSlot(sh.label, head, idx, False))
        second_pass.append(PassSlot(sh.label, tail, idx, bool(sh.rotate_second)))
    if reverse_second:
        second_pass.reverse()

    plan = PrintPlan(sheets=sheets, first_pass=first_pass, second_pass=second_pass,
                     copies=max(1, copies), first=first, reverse_second=reverse_second)
    if not sheets:
        plan.notes.append("⚠ 没有任何页面可打（pages 全空？）")
    elif ((plan.first_pass and all(s.page is None for s in plan.first_pass))
          or (plan.second_pass and all(s.page is None for s in plan.second_pass))):
        # 某一趟整体没有内容 = 整批都是单面文件（那趟纯空白，白走一遍纸）
        plan.notes.append("⚠ 整批都是单面文件：其中一趟全是空白占位页（白走一遍纸）——"
                          "这种情况应直接单面打印，不要走手动双面流程。")
    return plan


def plan_single(
    pages: list[int],
    copies: int = 1,
    label: str = "",
    duplex: bool = True,
    first: str = FIRST_ODD,
    reverse_second: bool = True,
) -> PrintPlan:
    """单文件便捷入口。"""
    return plan_passes([FilePlan(label, list(pages), duplex)], copies, first, reverse_second)


def rotate_second_for(second_rotate_180: bool, duplex_mode: str = "") -> bool:
    """第 2 趟内容要不要旋转 180°：由**纸路径**（实测值）与**装订方向**共同决定。

    实测（HP LaserJet 1020）：出纸 face-down + 整叠原样放回 ⇒ 第 2 趟内容被转了 180°，
    所以 `second_rotate_180=True` 表示"要补偿 180°"。而装订方向是另一回事：
      · 长边/书本式（默认）：补偿 ⇒ 两张面文字都正立；
      · 短边/便签式：**不补偿** ⇒ 得到短边装订（若补偿就成了长边，方向选反了）；
        （短边时操作员动作不变 —— 仍是"原样放回"，只是拿到的装订方向不同。）
    注意：这里只决定"要不要转"，不影响页序/配对/叠序。
    """
    long_edge = (duplex_mode or "long-edge") != "short-edge"
    return bool(second_rotate_180) if long_edge else (not second_rotate_180)


def expected_pairs(pages: list[int], duplex: bool = True) -> list[tuple[int | None, int | None]]:
    """验收用：这批页码**应该**形成的纸张配对（side_a, side_b）序列。"""
    return [(sh.side_a, sh.side_b) for sh in build_sheets([FilePlan("", list(pages), duplex)])]


# ============================================================
# 封面单独打印（2026-11-02 用户需求）：同一份文件里"部分页单面、其余双面"
# ============================================================
# 物理语义（与上面的 build_sheets 完全同源，不另立一套配对规则）：
#   · 单面页 = 自己独占一张纸，背面留空；
#   · 双面段 = 段内相邻两页配一张纸（段是"文档顺序上连续的双面页"），
#     段尾落单的那一页背面留空；
#   · 段与段之间**绝不跨段配对** —— 中间隔着单面页的纸，跨过去就把别人的内容
#     印到那张单面纸的背面上了（= 串页，2026-10-21/2026-11-02 现场事故的同一类错）。
# 例：31 页文件，第 1 页单面、2-15 双面、16-23 单面、24-31 双面
#   → [1]/单面 + [2..15]/双面(7 张) + [16..23]/单面(8 张) + [24..31]/双面(4 张) = 20 张纸；
#   第 1 趟: 1 2 4 6 8 10 12 14 16 17 18 19 20 21 22 23 24 26 28 30
#   第 2 趟（逆序）: 31 29 27 25 〔空〕… 〔空〕 15 13 11 9 7 5 3 〔空〕

def split_simplex_runs(pages, simplex_pages=()) -> list[tuple[list[int], bool]]:
    """把有序页序列按"该页是否单面"切成连续段 → [(页列表, 是否双面)]。

    Args:
        pages: 本次要打的页（**已应用页码范围**、按文档顺序）。
        simplex_pages: 其中要单面打印的页号集合（可迭代；不在 pages 里的会被自然忽略）。

    Returns:
        [(run_pages, duplex)]，顺序 = 文档顺序；单页的段 duplex=False，双面段掉到 1 页时
        也返回 False（length-1 的"双面段"和单面纸在物理上是同一件事，别让它冒充双面）。
    """
    try:
        sx = {int(p) for p in (simplex_pages or ())}
    except (TypeError, ValueError):
        sx = set()
    runs: list[list[int]] = []
    flags: list[bool] = []
    for raw in pages or []:
        try:
            p = int(raw)
        except (TypeError, ValueError):
            continue
        single = p in sx
        if runs and flags[-1] == single:
            runs[-1].append(p)
        else:
            runs.append([p])
            flags.append(single)
    return [(run, (not single) and len(run) > 1)
            for run, single in zip(runs, flags)]


def file_plans_for_pages(label: str, pages, duplex: bool = True, simplex_pages=(),
                         second_rotate: bool | None = None) -> list[FilePlan]:
    """一份文件 → 进 `plan_passes` 的 FilePlan 列表（封面单独打印的唯一入口）。

    `duplex=False`（整份单面）或 simplex_pages 为空 ⇒ 照旧一条计划（老行为一字不改）。
    需要混排时按 `split_simplex_runs` 切成多条计划 —— 每条都带同一 label（翻纸窗/日志里
    仍是"这一份文件"），页号已在本份 PDF 里就地展开，调用方不要再加偏移。
    """
    pl = [int(p) for p in (pages or [])]
    if not pl:
        return []
    if not duplex:
        return [FilePlan(label, pl, False, second_rotate)]
    runs = split_simplex_runs(pl, simplex_pages)
    if all(dup for _run, dup in runs):
        # 全段都是双面（= 没有单面页，或单面页只落在范围外）⇒ 保持老口径的一条计划
        return [FilePlan(label, pl, True, second_rotate)]
    return [FilePlan(label, list(run), bool(dup), second_rotate) for run, dup in runs]


def count_sheets(pages, simplex_pages=()) -> int:
    """这批页在纸叠里占几张纸（含单面页独张、双面段尾落单的那张）。

    与 `build_sheets` 一致：双面段 n 页 = ceil(n/2) 张，单面页 = 每页 1 张。
    """
    n = 0
    for run, dup in split_simplex_runs(pages, simplex_pages):
        n += ((len(run) + 1) // 2) if dup else len(run)
    return n


def charge_units(pages, simplex_pages=()) -> tuple[int, int]:
    """计费单元 → (双面张数, 单面张数)。

    · 双面段每 2 页 = 1 张双面纸；尾部落单的那 1 页 = 1 张单面纸（背面留空）；
    · 单面页每页 = 1 张单面纸。
    与 `printer_config.calc_cost` 的老口径逐字一致（纯双面时就是 pairs/remainder）。
    """
    dup_sheets = single_sheets = 0
    for run, dup in split_simplex_runs(pages, simplex_pages):
        if dup:
            dup_sheets += len(run) // 2
            single_sheets += len(run) % 2
        else:
            single_sheets += len(run)
    return dup_sheets, single_sheets
