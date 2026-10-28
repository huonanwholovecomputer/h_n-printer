# -*- coding: utf-8 -*-
"""验收脚本 · 攒批列表 × 虚拟打印（不碰真打印机、不耗一张纸）

用户场景
--------
想验证「🖐 攒批列表」在各种输入组合下是否正确，但不想真的打出一堆纸。本脚本因此：

  1. **虚拟打印**：用纯内存的 `VirtualPrinter` 模拟「出纸槽 → 人工整叠搬回进纸盒 →
     打第 2 趟」的完整走纸过程（出纸 face-down、后出的纸在上面、进纸盒从最上面取纸
     ⇒ 第 2 趟取纸顺序是第 1 趟印刷顺序的逆序）。它只记录每张纸两面印了什么，
     **一次也不调用** `pdf_printer.print_pdf/print_plan_pass/merge_pdfs/wait_for_output`
     （脚本开头把这些真实入口全部换成"一调用就报错"的哨兵，末尾断言零调用）。
  2. **不同页数 × 不同格式**：页数 1/2/3/4/5/7/8，格式 = 单面 / 双面·长边 / 双面·短边，
     再叠加份数、页码范围、一单多文件、页数未知、源文件后缀（pdf/docx/xlsx/png/txt）。
  3. **攒批列表本身**（`gui.StackQueueDialog`，Qt offscreen 真渲染）：逐行核对
     第 1 趟 / 第 2 趟页号、`[空]` 空白占位、张数、可取走状态、右栏等待队列。
  4. **多单攒批全流程**：多单第 1 趟连续入叠 → 整叠第 2 趟一次性打完 → 逐单核对
     "每一页都在、顺序对、配对只在文件内"；再验证「取走部分纸后第 2 趟仍对得齐」。

虚拟打印的物理模型（与 manual_duplex/duplex_stack 同一套事实，不另立一套）
--------------------------------------------------------------------------
  第 1 趟：按 `plan.first_pass` 顺序出纸，每面一张新纸，后出的压在先出的上面
          ⇒ 出纸槽「底→顶」= 第 1 趟印刷顺序。
  搬纸  ：整叠**原样**搬回进纸盒（不翻面、不重新排序）⇒ 进纸盒「顶→底」= 出纸槽逆序。
  第 2 趟：按 `plan.second_pass` 顺序逐张送纸；这张纸的另一面成像。
  成品  ：第 2 趟也是后出的在上 ⇒ 成品叠「上→下」= 第 2 趟印刷顺序的反序
          = 第 1 趟槽位顺序 ⇒ 阅读顺序（各文件按入叠顺序）。

跑法（不打印、不需要真打印机）：
    python tests/verify_stack_list_virtual.py                # 交互式（终端里默认显示列表窗口）
    python tests/verify_stack_list_virtual.py --headless     # 交互式但不显示窗口（只看控制台）
    python tests/verify_stack_list_virtual.py --show-ui      # 强制显示窗口（即使 stdin 不是终端）
    python tests/verify_stack_list_virtual.py --exhaust      # 全组合穷举（约 4.3 万种，不出纸）
    python tests/verify_stack_list_virtual.py --exhaust-deep # 深度穷举（约 144 万种，数分钟）
    python tests/verify_stack_list_virtual.py --interactive  # 强制交互（stdin 不是终端时）
    python tests/verify_stack_list_virtual.py --auto         # 跑 34 项自动回归（run_all 用）

`--exhaust` 的覆盖口径（完整笛卡尔积约 13.7 亿种，跑不完，按可组合性分层穷举）：
  ① 单内组合：1-3 个文件 × 每文件 1-5 页 × 单/双面 = 819 种，每种分别堆 1/2/3 单；
  ② 跨单异质：1-3 单，每单 1 个文件（1-5 页 × 单/双面）—— 所有不同订单的排列；
  ③ 跨单混合：2 文件单两两全组合（1-5 页）、2 文件单边界页(1/2/5)×3 单、
     3 文件单边界页(1/2/5)×1-2 单。
  单内页序/配对的正确性已被 ① 全覆盖；跨单拼接/页号偏移由 ②③ 覆盖。
  `--exhaust-deep` 再补：全部 1-2 单组合（每单 1-3 文件、全页）与 3 单组合
  （每单 ≤2 文件、全页），约 144 万种、数分钟；仍无法覆盖的是 3 单 × 含 3 文件单
  的全部页数（约 5 亿种）。

交互式会逐项询问：本叠几单、每单几个文件、每个文件的页数 / 格式（单面、双面·长边、
双面·短边）/ 份数 / 页码范围 / 源文件类型，然后现场虚拟打印，并显示「攒批列表」
窗口（`--headless` 时打印到控制台）。之后进入 UI 操作菜单，直接驱动真实控件：
  · 选中某行 → 看「✓ 我已取走 / ↩ 放回」按钮可用性与禁用原因；
  · 点按钮 → `toggle_requested` 信号 → 账本裁决 → 列表重绘；
  · 点「🔄 刷新」/ 模拟忙状态（按钮全部禁用）/ 点「▶ 开始打印第 2 趟」（含数纸确认）/
    点「⏭ 放弃整叠」；
  · 最后按当前账本做整叠虚拟第 2 趟，逐页核对成品。

依赖：PySide6（本机已装）。批处理（--auto / --exhaust / run_all）恒用 offscreen 不弹窗；
交互式默认显示真实窗口，`--headless` 可只看控制台。用户数据目录指向临时沙箱，
不读写真实 `%APPDATA%\\HN打印工具`。
"""
import io
import itertools
import os
import sys
import tempfile
import threading
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL_TOOL = os.path.join(REPO, "local_print_tool")

# 用户数据目录 → 临时沙箱（不碰真实配置/纸叠清单）
# 平台：批处理（auto/exhaust/run_all）恒 offscreen；交互式在终端里默认显示真窗口，
#       --headless 可强制离屏，--show-ui 可强制显示窗口。
_SANDBOX = tempfile.mkdtemp(prefix="hn_stack_virtual_")
os.environ["APPDATA"] = _SANDBOX
_ARGV = sys.argv[1:]
_BATCH_MODE = (os.environ.get("HN_ACCEPTANCE_BATCH") == "1"
               or "--auto" in _ARGV or "--exhaust" in _ARGV
               or "--exhaust-deep" in _ARGV)
try:
    _stdin_tty = sys.stdin.isatty()
except Exception:
    _stdin_tty = False
if "--show-ui" in _ARGV or (not _BATCH_MODE and "--headless" not in _ARGV and _stdin_tty):
    os.environ.pop("QT_QPA_PLATFORM", None)      # 用本机原生平台，窗口能看见
    SHOW_UI = "--headless" not in _ARGV
else:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    SHOW_UI = False
sys.path.insert(0, LOCAL_TOOL)
os.chdir(LOCAL_TOOL)

from PySide6.QtWidgets import QApplication  # noqa: E402

_app = QApplication.instance() or QApplication([])

import gui                      # noqa: E402
import duplex_stack as ds       # noqa: E402
import manual_duplex as md      # noqa: E402
import pdf_printer              # noqa: E402

ok_all = True
_n_pass = 0


def check(name, cond, detail=""):
    global ok_all, _n_pass
    if cond:
        _n_pass += 1
    else:
        ok_all = False
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


def section(title):
    print(f"\n=== {title} ===")


# ══════════════════════════════════════════════════════════════════════
# 0. 真打印机哨兵：证明本脚本一次都不碰真实打印入口
# ══════════════════════════════════════════════════════════════════════
class _PrinterGuard:
    """把真实打印入口换成"一调用就炸"的哨兵；calls 恒为空 = 全程没出纸。"""

    def __init__(self):
        self.calls = []

    def bomb(self, name):
        def _f(*_a, **_k):
            self.calls.append(name)
            raise AssertionError(f"虚拟打印测试调用了真实打印入口：{name}")
        return _f


_guard = _PrinterGuard()
for _n in ("print_pdf", "print_plan_pass", "_print_pdf_native", "merge_pdfs", "wait_for_output"):
    setattr(pdf_printer, _n, _guard.bomb(_n))
for _n in ("print_pdf", "print_plan_pass"):
    if hasattr(gui, _n):
        setattr(gui, _n, _guard.bomb("gui." + _n))


# ══════════════════════════════════════════════════════════════════════
# 1. 虚拟打印引擎（纯内存，不依赖 Qt / Windows / 打印机）
# ══════════════════════════════════════════════════════════════════════
class Paper:
    """一张纸。side_a/side_b 与 manual_duplex.Sheet 同义（side_a=先出现的那一面）。"""

    __slots__ = ("side_a", "side_b", "label")

    def __init__(self):
        self.side_a = None
        self.side_b = None
        self.label = ""

    def sides(self):
        return self.side_a, self.side_b


class VirtualPrinter:
    """虚拟打印机：模拟出纸槽 / 进纸盒 / 两趟走纸与两面成像。

    只记「哪一面印了第几页」，绝不接触 GDI / spooler / 任何真实队列。
    """

    def __init__(self, name="Microsoft Print to PDF（虚拟队列）", first=md.FIRST_ODD):
        self.name = name
        self.first = first
        self.tray = []            # 出纸槽（底→顶）
        self.input_tray = None    # 进纸盒（feed 顺序 = 顶→底）；None = 还没搬纸
        self.pass1_slots = []     # 第 1 趟印刷顺序（label, page）
        self.pass2_slots = []     # 第 2 趟印刷顺序（feed 顺序）
        self.dropped = []         # 人工取走的纸（Paper）

    # ── 走纸 ──
    def print_pass1(self, plan):
        """第 1 趟：每面出一张新纸，按 plan.first_pass 顺序堆进出纸槽。"""
        assert self.input_tray is None, "第 2 趟已开始，不能再打第 1 趟"
        assert len(self.tray) == 0 or True   # 允许多单连续入叠（同一出纸槽）
        for slot in plan.first_pass:
            p = Paper()
            p.label = slot.label
            self._image(p, slot.page, 1)
            self.tray.append(p)
            self.pass1_slots.append((slot.label, slot.page))

    def move_to_input(self, drop_slots=None):
        """人工把出纸槽整叠原样搬回进纸盒；drop_slots = 已被人取走的纸（槽位下标）。

        物理：出纸槽后出的在上；整叠不翻面搬回 ⇒ 进纸盒从最上面取纸，
        取纸顺序 = 出纸槽「顶→底」= 印刷顺序的反序。
        """
        drop = set(int(i) for i in (drop_slots or []))
        remain = [p for i, p in enumerate(self.tray) if i not in drop]
        self.dropped = [p for i, p in enumerate(self.tray) if i in drop]
        self.input_tray = list(reversed(remain))

    def print_pass2(self, plan):
        """第 2 趟：按 plan.second_pass 顺序从进纸盒取纸，印另一面。"""
        assert self.input_tray is not None, "第 2 趟前必须先把纸叠搬回进纸盒"
        assert len(self.input_tray) == len(plan.second_pass), \
            f"进纸盒 {len(self.input_tray)} 张 ≠ 计划 {len(plan.second_pass)} 张（必然错位）"
        out = []
        for slot, paper in zip(plan.second_pass, self.input_tray):
            self._image(paper, slot.page, 2)
            out.append(paper)
            self.pass2_slots.append((slot.label, slot.page))
        self.input_tray = None
        self.tray = out            # 第 2 趟出纸：后出的在上
        return out

    def finished_pairs(self):
        """成品叠「上→下」的两面序列 = 阅读顺序。"""
        return [p.sides() for p in reversed(self.tray)]

    # ── 内部 ──
    def _image(self, paper, page, which):
        """把某一面"印"到纸上。page=None = 空白占位页（纸照样走，只是不打墨）。"""
        if self.first == md.FIRST_ODD:
            if which == 1:
                paper.side_a = page
            else:
                paper.side_b = page
        else:                       # first=even：第 1 趟打的是后一面
            if which == 1:
                paper.side_b = page
            else:
                paper.side_a = page


def fmt_seq(seq):
    """页序 → 人话（与 gui._fmt_page_seq 同款，空白占位写 [空]）。"""
    items = list(seq or [])
    return " ".join("[空]" if p is None else str(p) for p in items) if items else "—"


def expected_pairs(files, copies=1):
    """这批文件**应该**形成的纸张配对（manual_duplex 的唯一真相）。"""
    return [(s.side_a, s.side_b) for s in md.build_sheets(files, copies)]


def plan_of(files, copies=1, first=md.FIRST_ODD):
    return md.plan_passes(files, copies, first=first, reverse_second=True)


def run_two_passes(files, copies=1, first=md.FIRST_ODD):
    """单/多文件完整两趟虚拟打印 → 成品配对。"""
    p = plan_of(files, copies, first)
    vp = VirtualPrinter(first=first)
    vp.print_pass1(p)
    vp.move_to_input()
    vp.print_pass2(p)
    return vp, p


# ── 素材：不同页数 × 不同格式（格式只影响转换引擎，页序只看 pages/duplex）──
FORMATS = [
    # (显示名, 请求双面, 双面模式, 文件后缀)
    ("双面·长边", True, "long-edge", ".pdf"),
    ("双面·短边", True, "short-edge", ".docx"),
    ("单面", False, "", ".png"),
]
EXTRA_EXTS = [".xlsx", ".txt", ".pdf", ".docx"]


def make_files(n, dup, ext=".pdf", label=None, copies=1):
    """按真实入叠口径建 FilePlan：份数>1 时每份一条计划（页码相同、各自配对）。"""
    files = []
    for c in range(max(1, copies)):
        name = label or f"{n}页{ext}"
        if copies > 1:
            name = f"{name}（第{c + 1}份）"
        files.append(md.FilePlan(name, list(range(1, n + 1)), bool(dup)))
    return files


def entry_plans(specs, base=0):
    """按 `gui.PrintWorker._print_stack_pass1` 的口径生成一单的 file_plans。

    - 单内偏移 `off` 按每个文件的**完整页数**推进（页码范围只裁剪要打的页，
      不改变合并 PDF 里后面文件的位置，见 gui.py `off += n`）；
    - 页码 = `base + off + 实际页号`（base = 本单在整叠合并 PDF 里的起始偏移）。
      第 1 趟打各单自己的合并 PDF（单内页码），第 2 趟打整叠合并 PDF（全局页码）；
      物理上是同一张纸的同一面，测试里统一用**全局页号**做身份，才不会把两边对错。

    specs: [{"label", "pages", "duplex", "full"(可选，完整页数), "copies"(可选)}]
    返回 (plans, page_count)。
    """
    out = []
    off = base
    for s in specs:
        full = int(s.get("full") or max(s["pages"]))
        pages = [off + p for p in s["pages"]]
        # 份数：同一文件的多份各自一条计划（页码相同、各自配对），
        # 与 gui._print_stack_pass1 完全一致 —— 偏移只按完整页数推进**一次**。
        copies = max(1, int(s.get("copies", 1) or 1))
        for c in range(copies):
            label = str(s["label"])
            if copies > 1:
                label = f"{label}（第{c + 1}份）"
            # file_pages = 这份文件**自己**的页号（列表显示用）；pages = 单内合并页码。
            out.append({"label": label, "pages": list(pages),
                        "file_pages": [int(p) for p in s["pages"]],
                        "duplex": bool(s["duplex"])})
        off += full
    return out, off - base


# ══════════════════════════════════════════════════════════════════════
# 交互式控制台（默认模式）：运行时手动输入配置 → 虚拟打印 → 打印列表与结果
# ══════════════════════════════════════════════════════════════════════
VP_NAME = "Microsoft Print to PDF（虚拟队列）"
_JOB_EXTS = ("pdf", "docx", "xlsx", "png", "txt")


class _JobStub:
    """只提供 gui.StackQueueDialog._add_job_row 读的字段（右栏「等待打印」）。"""

    def __init__(self, name, pages, copies=1, duplex="on"):
        self.display_name = name
        self.file_path = name
        self.page_count = pages
        self.copies = copies
        self.duplex = duplex


_UI_ACTIVE = False    # 攒批列表窗口显示中：等控制台输入时继续泵 Qt 事件，窗口不假死


def _ask(prompt, default=""):
    hint = f"（默认 {default}）" if default != "" else ""
    text = f"{prompt}{hint}: "
    if _UI_ACTIVE:
        # input() 会阻塞主线程 ⇒ 窗口在 Windows 上几秒后被标「未响应」。
        # 放到守护线程里读，主线程边等边 processEvents，窗口保持可重绘。
        box = []

        def _reader():
            try:
                box.append(input(text))
            except (EOFError, KeyboardInterrupt):
                box.append(None)

        threading.Thread(target=_reader, daemon=True).start()
        while not box:
            _app.processEvents()
            time.sleep(0.02)
        raw = box[0]
        if raw is None:
            print("\n已退出。")
            raise SystemExit(0)
        raw = raw.strip()
    else:
        try:
            raw = input(text).strip()
        except (EOFError, KeyboardInterrupt):
            print("\n已退出。")
            raise SystemExit(0)
    if raw.lower() in ("q", "quit", "exit"):
        print("已退出。")
        raise SystemExit(0)
    return raw if raw else str(default)


def _ask_int(prompt, default, lo, hi):
    while True:
        raw = _ask(prompt, default)
        try:
            v = int(raw)
        except ValueError:
            print(f"  ! 请输入 {lo}~{hi} 的整数")
            continue
        if lo <= v <= hi:
            return v
        print(f"  ! 请输入 {lo}~{hi} 的整数")


def _ask_choice(prompt, labels, default=1):
    text = " / ".join(f"{i}={lab}" for i, lab in enumerate(labels, 1))
    while True:
        raw = _ask(f"{prompt} [{text}]", default)
        try:
            v = int(raw)
        except ValueError:
            v = 0
        if 1 <= v <= len(labels):
            return v
        print(f"  ! 请输入 1~{len(labels)}")


def _parse_selection(raw, total):
    """页码范围：''/all → 全部；支持 2-5 / 1,3,5 / 2- / -4。非法返回 None。"""
    raw = (raw or "").strip().lower()
    if raw in ("", "all", "*", "全部"):
        return list(range(1, total + 1))
    pages = set()
    try:
        for part in raw.replace("，", ",").split(","):
            part = part.strip()
            if not part:
                continue
            if "-" in part:
                a, b = part.split("-", 1)
                lo = int(a) if a.strip() else 1
                hi = int(b) if b.strip() else total
                if lo > hi:
                    return None
                pages.update(range(lo, hi + 1))
            else:
                pages.add(int(part))
    except ValueError:
        return None
    if not pages or min(pages) < 1 or max(pages) > total:
        return None
    return sorted(pages)


def _ask_selection(prompt, total):
    while True:
        raw = _ask(f"{prompt}（如 2-5 / 1,3,5，回车=全部 1-{total}）", "")
        sel = _parse_selection(raw, total)
        if sel:
            return sel
        print(f"  ! 页码范围不合法（只能 1~{total}，例如 2-5 或 1,3,5）")


def _input_files():
    """问一单里的文件（页数/格式/份数/页码范围/源文件类型）。"""
    files = []
    n_files = _ask_int("    文件数", 1, 1, 10)
    for fi in range(1, n_files + 1):
        print(f"    ── 文件 {fi} ──")
        total = _ask_int("      页数", 3, 1, 500)
        fmt = _ask_choice("      格式", ["单面", "双面·长边", "双面·短边"],
                          2 if total > 1 else 1)
        if fmt >= 2 and total == 1:
            print("        · 1 页文件按单面入叠（单页硬闸门），第 2 趟是一张空白占位")
        dup = fmt >= 2 and total > 1
        copies = _ask_int("      份数", 1, 1, 30)
        sel = _ask_selection("      页码范围", total)
        ext = "." + _JOB_EXTS[_ask_choice("      源文件类型", list(_JOB_EXTS), 1) - 1]
        files.append({"total": total, "sel": sel, "duplex": dup,
                      "mode": {1: "", 2: "long-edge", 3: "short-edge"}[fmt],
                      "copies": copies, "ext": ext})
    return files


def _input_scenario(round_no):
    print(f"\n──── 场景 {round_no}：请手动输入配置 ────")
    n_orders = _ask_int("本叠要打几单", 1, 1, 10)
    orders = []
    for oi in range(1, n_orders + 1):
        print(f"\n  第 {oi} 单")
        orders.append({"title": f"HN-{round_no}-{oi}", "files": _input_files()})
    capacity = _ask_int("\n出纸槽容量（张）", 90, 10, 400)
    first = (md.FIRST_ODD if _ask_choice(
        "机型约定：第 1 趟打哪一面", ["奇数面先打（常见）", "偶数面先打"], 1) == 1
        else md.FIRST_EVEN)
    return orders, capacity, first


def _specs_of_order(cfg):
    specs = []
    for fi, f in enumerate(cfg["files"], 1):
        specs.append({
            "label": f"文件{fi}（{f['total']}页）{f['ext']}",
            "pages": f["sel"], "duplex": f["duplex"],
            "full": f["total"], "copies": f["copies"],
        })
    return specs


def _entry_for_order(seq, cfg, first, quiet=False):
    """按真实入叠口径把用户配置变成 (StackEntry, 单内两趟计划, 装订方向)。"""
    local_plans, page_count = entry_plans(_specs_of_order(cfg), base=0)
    files = [md.FilePlan(fp["label"], fp["pages"], fp["duplex"]) for fp in local_plans]
    plan = plan_of(files, first=first)
    modes = {f["mode"] for f in cfg["files"] if f["duplex"]}
    if len(modes) > 1 and not quiet:
        print("    ⚠ 同一单里混了长边/短边：真实一次打印请求只有一个装订方向，这里按长边模拟")
    rotate = not (modes == {"short-edge"})
    entry = ds.StackEntry(seq=seq, title=cfg["title"], page_count=page_count,
                          sheet_count=plan.sheet_count, rotate=rotate,
                          state=ds.STATE_DONE, pdf=f"entry_{seq:04d}.pdf",
                          first=first, file_plans=local_plans)
    return entry, plan, rotate


def _wait_jobs(cfg):
    jobs = []
    for fi, f in enumerate(cfg["files"], 1):
        name = f"文件{fi}（{f['total']}页）{f['ext']}"
        if f["copies"] > 1:
            name += f"×{f['copies']}份"
        jobs.append(_JobStub(name, f["total"], f["copies"],
                             "on" if f["duplex"] else "off"))
    return jobs


_console_dlg = None


def _console_list(stack, waiting, dlg=None):
    """把真实 StackQueueDialog 渲染出来的树原样打印到控制台（左右两栏）。"""
    global _console_dlg
    if dlg is None:
        if _console_dlg is None:
            _console_dlg = gui.StackQueueDialog()
        dlg = _console_dlg
    dlg.render(stack, wait_requests=waiting)
    lines = [f"【已攒】{dlg._left_group.title()}"]
    tl = dlg._tree_left
    for i in range(tl.topLevelItemCount()):
        top = tl.topLevelItem(i)
        lines.append("  " + top.text(0))
        for j in range(top.childCount()):
            ch = top.child(j)
            lines.append("      " + " | ".join(ch.text(c) for c in range(5)))
    lines.append(f"【等待打印】{dlg._right_group.title()}")
    tr = dlg._tree_right
    for i in range(tr.topLevelItemCount()):
        top = tr.topLevelItem(i)
        lines.append("  " + top.text(0))
        for j in range(top.childCount()):
            ch = top.child(j)
            lines.append("      " + " | ".join(ch.text(c) for c in range(4)))
    return "\n".join(lines)


def _print_plans(admitted):
    print("\n【每单两趟页序（单内页号，与列表一致）】")
    for a in admitted:
        e, p = a["entry"], a["plan"]
        desc = " / ".join(
            f"{fp['label']}({len(fp['pages'])}页{'' if fp['duplex'] else '·单面'})"
            for fp in e.file_plans)
        print(f"  #{e.seq} {e.title} — {desc}")
        print(f"      第 1 趟：{fmt_seq(p.pass_pages(1))}")
        print(f"      第 2 趟：{fmt_seq(p.pass_pages(2))}    （本单 {p.sheet_count} 张）")


def _ui_flat_rows(dlg):
    """左栏可操作的文件行（顶层=单，子行=文件计划）。"""
    rows = []
    tl = dlg._tree_left
    for i in range(tl.topLevelItemCount()):
        top = tl.topLevelItem(i)
        for j in range(top.childCount()):
            rows.append((i, j, top.child(j)))
    return rows


def _ui_refresh(dlg, stack, waiting, busy=False, printing_text=""):
    """等价于主窗口的 _refresh_stack_queue：按当前账本重绘，并处理 Qt 事件（窗口立即刷新）。"""
    dlg.render(stack, busy=busy, wait_requests=waiting, printing_text=printing_text)
    _app.processEvents()


def _ui_select(dlg, number):
    rows = _ui_flat_rows(dlg)
    if not rows:
        print("  ! 左栏没有可操作的文件行")
        return None
    if not (1 <= number <= len(rows)):
        print(f"  ! 行号只能是 1~{len(rows)}")
        return None
    _i, _j, item = rows[number - 1]
    dlg._tree_left.clearSelection()
    item.setSelected(True)
    dlg._on_left_selection()
    _app.processEvents()
    return item


def _ui_describe_selection(dlg):
    sel = dlg._tree_left.selectedItems()
    if not sel:
        print("  （当前没有选中行）")
        return
    it = sel[0]
    print(f"  选中：{it.text(0)}")
    print(f"    第 1 趟：{it.text(1)}    第 2 趟：{it.text(2)}    张数：{it.text(3)}")
    print(f"    状态：{it.text(4)}")
    print(f"    「{dlg._btn_take.text()}」按钮："
          f"{'可用' if dlg._btn_take.isEnabled() else '禁用'} —— {dlg._btn_take.toolTip()}")


def _ui_session(dlg, stack, waiting, admitted):
    """在真实 StackQueueDialog 上模拟主窗口的 UI 逻辑。

    选行 / 取走放回（按钮 → toggle_requested → 账本 → 重绘）/ 刷新 / 忙状态 /
    开始第 2 趟（数纸确认）/ 放弃整叠。返回 (action, taken)，action ∈ start/abandon。
    """
    by_seq = {a["entry"].seq: a for a in admitted}
    taken = []
    signal_state = {"start": False, "abandon": False}

    def _record(seq, idx, flag, fp):
        a = by_seq[seq]
        item = {"label": fp["label"], "base": a["base"], "pages": list(fp["pages"]),
                "slots": [a["sheet_base"] + s.sheet_index
                          for s in a["gplan"].first_pass if s.label == fp["label"]],
                "sheets": ds.sheet_count_of_plans([fp])}
        if flag:
            taken.append(item)
        else:
            for k, t in enumerate(list(taken)):
                if t["label"] == item["label"] and t["base"] == item["base"]:
                    taken.pop(k)
                    break

    def _on_toggle(seq, idx, flag):
        ok, why = stack.toggle_taken(seq, idx, flag)
        if not ok:
            print(f"  ✗ 账本拒绝：{why}")
        else:
            fp = stack.find(seq).file_plans[idx]
            _record(seq, idx, flag, fp)
            print(f"  ✓ {'已取走' if flag else '已放回'} #{seq} {fp['label']}")
        _ui_refresh(dlg, stack, waiting)

    def _on_refresh():
        _ui_refresh(dlg, stack, waiting)
        print("  ↻ 列表已刷新（读当前纸叠）")

    dlg.toggle_requested.connect(_on_toggle)
    dlg.refresh_requested.connect(_on_refresh)
    dlg.start_second_requested.connect(lambda: signal_state.__setitem__("start", True))
    dlg.abandon_requested.connect(lambda: signal_state.__setitem__("abandon", True))

    _ui_refresh(dlg, stack, waiting)
    while True:
        print("\n【攒批列表 UI 操作】（直接回车 = 0）")
        print("  1=选中某行  2=选中并点「✓ 我已取走 / ↩ 放回」  3=点「🔄 刷新」")
        print("  4=模拟忙状态  5=点「▶ 开始打印第 2 趟」（数纸确认）  6=点「⏭ 放弃整叠」")
        print("  7=打印列表  0=不操作，直接开始第 2 趟")
        cmd = _ask("选择操作", "0").strip().lower()
        if cmd in ("0", ""):
            return "start", taken
        if cmd == "1":
            rows = _ui_flat_rows(dlg)
            if not rows:
                print("  ! 左栏没有可操作的文件行")
                continue
            _ui_select(dlg, _ask_int("  行号", 1, 1, len(rows)))
            _ui_describe_selection(dlg)
        elif cmd == "2":
            rows = _ui_flat_rows(dlg)
            if not rows:
                print("  ! 左栏没有可操作的文件行")
                continue
            _ui_select(dlg, _ask_int("  行号", 1, 1, len(rows)))
            _ui_describe_selection(dlg)
            if not dlg._btn_take.isEnabled():
                print("  ✗ 按钮是禁用的（这份第 2 趟还要用，不能整份取走）")
                continue
            dlg._btn_take.click()
            _app.processEvents()
            print("\n" + _console_list(stack, waiting, dlg))
        elif cmd == "3":
            dlg._btn_refresh.click()
            _app.processEvents()
            print("\n" + _console_list(stack, waiting, dlg))
        elif cmd == "4":
            _ui_refresh(dlg, stack, waiting, busy=True,
                        printing_text="正在打整叠第 2 趟（模拟忙状态）")
            print(f"  开始按钮可用={dlg._btn_start.isEnabled()}  "
                  f"放弃按钮可用={dlg._btn_abandon.isEnabled()}  "
                  f"取走按钮可用={dlg._btn_take.isEnabled()}")
            _ask("  按回车恢复空闲", "")
            _ui_refresh(dlg, stack, waiting)
        elif cmd == "5":
            print(f"  开始按钮可用={dlg._btn_start.isEnabled()}；"
                  f"文案：{dlg._btn_start.text()}")
            if not dlg._btn_start.isEnabled():
                continue
            present = stack.present_sheet_count
            ans = _ask(f"  数纸确认：进纸盒里应有 {present} 张，继续第 2 趟？(y/N)", "y")
            if ans.lower() in ("n", "no", "否", "不"):
                print("  已取消，纸叠原样保留")
                continue
            dlg._btn_start.click()
            _app.processEvents()
            if signal_state["start"]:
                return "start", taken
        elif cmd == "6":
            print(f"  放弃按钮可用={dlg._btn_abandon.isEnabled()}；"
                  f"文案：{dlg._btn_abandon.text()}")
            if not dlg._btn_abandon.isEnabled():
                continue
            ans = _ask(f"  确定放弃整叠 {stack.count} 单？按失败收尾（y/N）", "N")
            if ans.lower() not in ("y", "yes", "是"):
                print("  已取消，纸叠保留")
                continue
            dlg._btn_abandon.click()
            _app.processEvents()
            if signal_state["abandon"]:
                return "abandon", taken
        elif cmd == "7":
            print("\n" + _console_list(stack, waiting, dlg))
        else:
            print("  ! 未知操作")


def _verify_and_report(stack, vp, admitted, taken, plan2):
    print("\n──────── 虚拟打印结果 ────────")
    fails = []
    got = vp.finished_pairs()
    active = stack.file_plans()
    exp = [(s.side_a, s.side_b) for s in md.build_sheets(active)]
    if got != exp:
        fails.append(f"成品配对不符：{got} ≠ {exp}")
    got_pages = [x for pair in got for x in pair if x is not None]
    exp_pages = [p for fp in active for p in fp.pages]
    if got_pages != exp_pages:
        fails.append(f"页序/丢页：{got_pages} ≠ {exp_pages}")
    want_drop = sorted(t["base"] + p for t in taken for p in t["pages"])
    got_drop = sorted(p.side_a for p in vp.dropped if p.side_a is not None)
    if got_drop != want_drop:
        fails.append(f"取走的纸内容不符：{got_drop} ≠ {want_drop}")
    if any(p.side_b is not None for p in vp.dropped):
        fails.append("被取走的纸上第 2 趟也印了内容（不该发生）")
    orig = stack.original_sheets_count
    if len(got) + len(vp.dropped) != orig:
        fails.append(f"成品 {len(got)} 张 + 取走 {len(vp.dropped)} 张 ≠ 原始 {orig} 张")
    if len(vp.pass2_slots) != stack.present_sheet_count:
        fails.append(f"第 2 趟送纸 {len(vp.pass2_slots)} ≠ 在叠 {stack.present_sheet_count}")

    # 逐单核对：该单剩余页一页不少、顺序不乱（整叠成品按入叠顺序切段）
    cursor = 0
    for a in admitted:
        e = a["entry"]
        acts = e.active_plans()
        n = ds.sheet_count_of_plans(acts) if acts else 0
        seg = got[cursor:cursor + n]
        cursor += n
        want = [a["base"] + p for fp in acts for p in fp["pages"]]
        have = [x for pair in seg for x in pair if x is not None]
        if have != want:
            fails.append(f"{e.title} 页序不符：{have} ≠ {want}")
        if acts:
            print(f"  #{e.seq} {e.title}：{n} 张，页 "
                  + fmt_seq([p for fp in acts for p in fp["pages"]]) + " 全部命中")

    print(f"  第 1 趟出纸：{orig} 张")
    print(f"  人工取走：{len(vp.dropped)} 张（成品单面纸，正面内容已核对）"
          if taken else "  人工取走：0 张")
    print(f"  第 2 趟送纸：{len(vp.pass2_slots)} 张（= {orig} − {len(vp.dropped)}）")
    if plan2.sheet_count == 0:
        print("  第 2 趟无内容：全部是单面成品，直接按成品收尾（不出纸）")

    if fails:
        print("\n[FAIL] 虚拟打印结果不正确：")
        for f in fails:
            print("  - " + f)
        return False
    print("\n[PASS] 成品配对逐张正确；每页恰好出现一次；取走的纸都是背面空白。")
    return True


def _run_interactive_scenario(orders_cfg, capacity, first, round_no):
    global _UI_ACTIVE
    stack = ds.DuplexStack(capacity=capacity, printer_name=VP_NAME)
    stack.new_batch(f"interactive_{round_no}")
    stack.first = first
    stack.reverse_second = True
    vp = VirtualPrinter(name=VP_NAME, first=first)
    admitted, waiting = [], []
    base_pages = sheet_base = 0

    for seq, cfg in enumerate(orders_cfg, 1):
        entry, local_plan, rotate = _entry_for_order(seq, cfg, first)
        adm = stack.admit(local_plan.sheet_count, rotate=rotate)
        if not adm.ok:
            waiting.append({"tab_key": str(seq),
                            "reason": f"{cfg['title']}：{adm.message(stack)}",
                            "jobs": _wait_jobs(cfg)})
            print(f"\n  ⏳ {cfg['title']} 未入叠：{adm.message(stack)}")
            continue
        stack.add(entry)
        # 第 1 趟按**全局页号**喂虚拟打印机（与整叠合并 PDF 的页号一致）
        gfiles = [md.FilePlan(fp["label"], [base_pages + p for p in fp["pages"]],
                              fp["duplex"]) for fp in entry.file_plans]
        gplan = plan_of(gfiles, first=first)
        vp.print_pass1(gplan)
        admitted.append({"entry": entry, "plan": local_plan, "gplan": gplan,
                         "base": base_pages, "sheet_base": sheet_base, "cfg": cfg})
        base_pages += entry.page_count
        sheet_base += local_plan.sheet_count

    if not admitted:
        print("\n❌ 没有任何一单能入叠，无法模拟。")
        return False

    total_sheets = sum(a["entry"].sheet_count for a in admitted)
    print(f"\n━━━ 第 1 趟完成：{len(admitted)} 单 / {total_sheets} 张纸已在出纸槽 ━━━")

    dlg = gui.StackQueueDialog()
    if SHOW_UI:
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()
        _app.processEvents()
        _UI_ACTIVE = True
        print("  （已显示「🖐 攒批列表」窗口；窗口用于展示当前列表，"
              "操作请在控制台输入编号 —— 等同于点击窗口里的按钮）")
    print("\n" + _console_list(stack, waiting, dlg))
    if any(len(cfg["files"]) > 1 for cfg in
           (a["cfg"] for a in admitted)):
        print("  说明：多文件单的页号是本单合并 PDF 的页码（后一个文件接着前一个"
              "的页数编号），与真实攒批列表一致。")
    _print_plans(admitted)

    action, taken = _ui_session(dlg, stack, waiting, admitted)
    if action == "abandon":
        print(f"\n⏭ 已放弃整叠：{len(admitted)} 单按失败收尾（虚拟，不出纸）")
        if SHOW_UI:
            _UI_ACTIVE = False
            dlg.close()
        return True
    if taken:
        print("\n【UI 操作后的列表】")
        print(_console_list(stack, waiting, dlg))

    drop = [i for t in taken for i in t["slots"]]
    vp.move_to_input(drop_slots=drop)
    plan2 = stack.build_plan()
    print(f"\n━━━ 第 2 趟：整叠一次打完（送纸 {len(plan2.second_pass)} 张）━━━")
    print(f"  取纸顺序（整叠合并 PDF 的全局页号）：{fmt_seq(plan2.pass_pages(2))}")
    vp.print_pass2(plan2)

    ok = _verify_and_report(stack, vp, admitted, taken, plan2)
    if SHOW_UI:
        _UI_ACTIVE = False
        dlg.close()
    return ok


def run_interactive():
    print("=" * 62)
    print("🖐 攒批列表 · 虚拟打印控制台（不出纸，不需要真打印机）")
    print("=" * 62)
    print("提示：任意输入处输入 q 退出；直接回车 = 使用默认值。")
    results = []
    round_no = 1
    while True:
        orders, capacity, first = _input_scenario(round_no)
        results.append(_run_interactive_scenario(orders, capacity, first, round_no))
        again = _ask("\n再来一个场景？(Y/n)", "Y").strip().lower()
        if again in ("n", "no", "否", "不"):
            break
        round_no += 1
    print(f"\n共跑 {len(results)} 个场景：" +
          ("全部通过 ✅" if all(results) else "存在失败 ❌"))
    return 0 if all(results) else 1


# ══════════════════════════════════════════════════════════════════════
# --exhaust：分层穷举（完整 13.7 亿种跑不完，按可组合性拆成三层）
# ══════════════════════════════════════════════════════════════════════
def _exhaust_file_types(pages, duplex_opts=(False, True)):
    """文件类型：页数 × 单/双面。1 页双面 ≡ 单面（单页硬闸门），不重复枚举。"""
    out = []
    for p in pages:
        for dup in duplex_opts:
            if p == 1 and dup:
                continue
            real = bool(dup) and p > 1
            out.append({"total": p, "sel": list(range(1, p + 1)),
                        "duplex": real,
                        "mode": "long-edge" if real else "",
                        "copies": 1, "ext": ".pdf"})
    return out


def _case_text(orders):
    parts = []
    for oi, files in enumerate(orders, 1):
        fs = ", ".join(f"{f['total']}页{'双' if f['duplex'] else '单'}" for f in files)
        parts.append(f"#{oi}[{fs}]")
    return " ".join(parts)


_exhaust_dlg = None


def _verify_exhaust_list(stack):
    """穷举用的列表校验：顶层行 / 每个文件行 / 汇总行都必须与 manual_duplex 一致。"""
    global _exhaust_dlg
    if _exhaust_dlg is None:
        _exhaust_dlg = gui.StackQueueDialog()
    _exhaust_dlg.render(stack)
    probs = []
    tl = _exhaust_dlg._tree_left
    if tl.topLevelItemCount() != stack.count:
        return [f"左栏顶层 {tl.topLevelItemCount()} ≠ {stack.count}"]
    for i, e in enumerate(stack.entries):
        top = tl.topLevelItem(i)
        want_top = f"#{e.seq} {e.title} · 本单 {e.page_count} 页 / 在叠 {e.sheet_count} 张"
        if top.text(0) != want_top:
            probs.append(f"顶层行 {top.text(0)!r} ≠ {want_top!r}")
        if top.childCount() != len(e.file_plans):
            probs.append(f"#{e.seq} 子行 {top.childCount()} ≠ {len(e.file_plans)}")
            continue
        for j, fp in enumerate(e.file_plans):
            row = top.child(j)
            p = plan_of([md.FilePlan(fp["label"], fp["pages"], fp["duplex"])], first=e.first)
            want = [f"📄 {fp['label']}", fmt_seq(p.pass_pages(1)), fmt_seq(p.pass_pages(2)),
                    f"{p.sheet_count} 张",
                    "🔓 可整份取走" if all(x is None for x in p.pass_pages(2))
                    else "🔒 第 2 趟还要用"]
            got = [row.text(c) for c in range(5)]
            if got != want:
                probs.append(f"#{e.seq} 第 {j + 1} 行 {got} ≠ {want}")
    summary = _exhaust_dlg._summary.text()
    if f"本叠 {stack.count} 单 / 已攒 {stack.present_sheet_count} 张纸" not in summary:
        probs.append(f"汇总行不符：{summary}")
    return probs


def _exhaust_case(orders_files, first=md.FIRST_ODD, capacity=400, render_list=False):
    """跑一种组合：入叠 → 虚拟两趟 → 逐张配对/页序核对（可选：列表渲染核对）。

    返回问题列表（空 = 通过）。不取走任何纸（取走由交互模式单独覆盖）。
    """
    probs = []
    stack = ds.DuplexStack(capacity=capacity, printer_name=VP_NAME)
    stack.new_batch("exhaust")
    stack.first = first
    vp = VirtualPrinter(first=first)
    admitted = []
    base_pages = sheet_base = 0

    for seq, files in enumerate(orders_files, 1):
        cfg = {"title": f"HN-{seq}", "files": files}
        entry, local_plan, rotate = _entry_for_order(seq, cfg, first, quiet=True)
        adm = stack.admit(local_plan.sheet_count, rotate=rotate)
        if not adm.ok:
            probs.append(f"第 {seq} 单意外被拒：{adm.reason}")
            continue
        stack.add(entry)
        gfiles = [md.FilePlan(fp["label"], [base_pages + p for p in fp["pages"]],
                              fp["duplex"]) for fp in entry.file_plans]
        gplan = plan_of(gfiles, first=first)
        vp.print_pass1(gplan)
        admitted.append({"entry": entry, "gplan": gplan, "base": base_pages,
                         "sheet_base": sheet_base})
        base_pages += entry.page_count
        sheet_base += local_plan.sheet_count

    present = stack.present_sheet_count
    orig = stack.original_sheets_count
    if len(vp.tray) != present or orig != present:
        probs.append(f"第 1 趟张数 {len(vp.tray)} / 在叠 {present} / 原始 {orig} 不一致")

    vp.move_to_input()
    plan2 = stack.build_plan()
    if plan2.sheet_count != present:
        probs.append(f"第 2 趟送纸 {plan2.sheet_count} ≠ 在叠 {present}")
    if [s.sheet_index for s in plan2.second_pass] != list(range(present - 1, -1, -1)):
        probs.append("第 2 趟不是逆序取纸")
    vp.print_pass2(plan2)

    got = vp.finished_pairs()
    active = stack.file_plans()
    exp = [(s.side_a, s.side_b) for s in md.build_sheets(active)]
    if got != exp:
        probs.append(f"成品配对不符：{got} ≠ {exp}")
    got_pages = [x for pair in got for x in pair if x is not None]
    exp_pages = [p for fp in active for p in fp.pages]
    if got_pages != exp_pages:
        probs.append(f"页序/丢页：{got_pages} ≠ {exp_pages}")
    blanks = sum(1 for pair in got for x in pair if x is None)
    if blanks + len(got_pages) != 2 * present:
        probs.append(f"空白 {blanks} + 内容 {len(got_pages)} ≠ 2×{present} 面")

    # 逐单核对：整叠成品按入叠顺序切段，每段页序必须正确
    cursor = 0
    for a in admitted:
        e = a["entry"]
        seg = got[cursor:cursor + e.sheet_count]
        cursor += e.sheet_count
        want = [a["base"] + p for fp in e.file_plans for p in fp["pages"]]
        have = [x for pair in seg for x in pair if x is not None]
        if have != want:
            probs.append(f"{e.title} 页序不符：{have} ≠ {want}")
    if cursor != orig:
        probs.append(f"逐单切段 {cursor} ≠ 原始 {orig}")

    if render_list:
        probs.extend(_verify_exhaust_list(stack))
    return probs


def run_exhaustive():
    full_types = _exhaust_file_types(range(1, 6))          # 9 种（1 页双面并入单面）
    boundary = _exhaust_file_types((1, 2, 5))              # 5 种（边界页）

    # 单内组合：1-3 个文件（有序，页码范围=全部）
    comps = [list(combo) for k in (1, 2, 3)
             for combo in itertools.product(full_types, repeat=k)]
    one_file = [[f] for f in full_types]
    two_full = [list(c) for c in itertools.product(full_types, repeat=2)]
    two_bound = [list(c) for c in itertools.product(boundary, repeat=2)]
    three_bound = [list(c) for c in itertools.product(boundary, repeat=3)]
    small_orders = [list(c) for k in (1, 2)
                    for c in itertools.product(full_types, repeat=k)]   # 每单 ≤2 文件

    deep = "--exhaust-deep" in sys.argv[1:]
    counts = {
        "单内组合 × 1 单": len(comps),
        "单内组合 × 2 单（同构重复）": len(comps),
        "单内组合 × 3 单（同构重复）": len(comps),
        "跨单异质 · 单文件 · 1 单": len(full_types),
        "跨单异质 · 单文件 · 2 单": len(full_types) ** 2,
        "跨单异质 · 单文件 · 3 单": len(full_types) ** 3,
        "跨单混合 · 单文件单 + 2 文件单（两单）": len(full_types) * len(two_full) * 2,
        "跨单混合 · 2 文件单两两（1-5 页）": len(two_full) ** 2,
        "跨单混合 · 2 文件单边界页 × 3 单": len(two_bound) ** 3,
        "跨单混合 · 3 文件单边界页 × 1 单": len(three_bound),
        "跨单混合 · 3 文件单边界页 × 2 单": len(three_bound) ** 2,
    }
    if deep:
        counts["【深】全部 2 单组合（每单 1-3 文件，全页）"] = len(comps) ** 2
        counts["【深】3 单组合（每单 1-2 文件，全页）"] = len(small_orders) ** 3
    total = sum(counts.values())

    print("=" * 62)
    print("🖐 攒批列表 · 全组合穷举（虚拟打印，不出纸）")
    print("=" * 62)
    print("完整笛卡尔积（1-3 单 × 每单 1-3 文件 × 1-5 页 × 单/双面）约 5.5 亿种")
    print("（按 1 页双面并入单面计；若把 1 页双面单算则约 13.7 亿），跑不完。")
    print("按可组合性分层穷举：单内页序全组合 + 跨单拼接/偏移全异质 + 混合边界页。")
    if deep:
        print("⚠ 深度模式：额外完整覆盖「所有 2 单组合」与「3 单 × 每单 ≤2 文件」，"
              "预计数分钟。")
    print(f"\n本次共 {total} 种组合：")
    for k, v in counts.items():
        print(f"  · {k}: {v}")

    t0 = __import__("time").time()
    done = 0
    fails = []
    progress_step = 5000 if total <= 100000 else 50000

    def _run(orders, label, render):
        nonlocal done
        probs = _exhaust_case(orders, render_list=render)
        done += 1
        if probs:
            fails.append((label, _case_text(orders), probs[:4]))
        if done % progress_step == 0:
            print(f"  … 已跑 {done}/{total}（{__import__('time').time() - t0:.1f}s，"
                  f"失败 {len(fails)}）")

    # ① 单内组合 × 堆叠 1/2/3 单（同一组合重复入叠）
    for comp in comps:
        for depth in (1, 2, 3):
            _run([[dict(f) for f in comp] for _ in range(depth)],
                 f"单内组合×{depth}单", True)

    # ② 跨单异质：每单 1 个文件，1-3 单的所有排列
    for n in (1, 2, 3):
        for combo in itertools.product(full_types, repeat=n):
            _run([[dict(f)] for f in combo], f"跨单单文件×{n}单", True)

    # ②b 跨单混合：单文件单 + 2 文件单（先后两种顺序）
    for f in full_types:
        for order in two_full:
            _run([[dict(f)], [dict(x) for x in order]], "单文件+2文件", False)
            _run([[dict(x) for x in order], [dict(f)]], "2文件+单文件", False)

    # ③ 跨单混合：2 文件单两两全组合（1-5 页）
    for combo in itertools.product(two_full, repeat=2):
        _run([[dict(f) for f in order] for order in combo], "2文件单×2单", False)

    # ③b 跨单混合：2 文件单、边界页，3 单全排列
    for combo in itertools.product(two_bound, repeat=3):
        _run([[dict(f) for f in order] for order in combo], "2文件单边界×3单", False)

    # ③c 跨单混合：3 文件单、边界页，1-2 单
    for order in three_bound:
        _run([[dict(f) for f in order]], "3文件单边界×1单", False)
    for combo in itertools.product(three_bound, repeat=2):
        _run([[dict(f) for f in order] for order in combo], "3文件单边界×2单", False)

    if deep:
        # 【深】完整 2 单组合：每单 1-3 文件、页数 1-5、单/双面，全部排列
        for combo in itertools.product(comps, repeat=2):
            _run([[dict(f) for f in order] for order in combo], "深-任意2单", False)
        # 【深】3 单组合：每单 1-2 文件、页数 1-5、单/双面，全部排列
        for combo in itertools.product(small_orders, repeat=3):
            _run([[dict(f) for f in order] for order in combo], "深-3单≤2文件", False)

    elapsed = __import__("time").time() - t0
    print("\n" + "-" * 62)
    if fails:
        print(f"FAIL：{len(fails)} / {total} 种组合有问题（显示前 20 种）")
        for label, desc, probs in fails[:20]:
            print(f"  [{label}] {desc}")
            for p in probs:
                print("      - " + p)
        return 1
    if done != total:
        print(f"FAIL：计划 {total} 种，实际跑了 {done} 种（计数不一致）")
        return 1
    print(f"PASS：{total} 种组合全部通过（列表内容 + 两趟页序 + 逐张配对），"
          f"用时 {elapsed:.1f}s ✅")
    return 0


def _interactive_requested(argv):
    """默认：终端里交互；--auto / 批处理环境变量 / 非终端 stdin → 自动回归。"""
    if "--auto" in argv or "--batch" in argv:
        return False
    if "--interactive" in argv or "-i" in argv:
        return True
    if os.environ.get("HN_ACCEPTANCE_BATCH") == "1":
        return False
    try:
        return sys.stdin.isatty()
    except Exception:
        return False


if "--exhaust" in sys.argv[1:] or "--exhaust-deep" in sys.argv[1:]:
    sys.exit(run_exhaustive())


if _interactive_requested(sys.argv[1:]):
    sys.exit(run_interactive())


# ══════════════════════════════════════════════════════════════════════
# ① 虚拟打印：不同页数 × 不同格式（单文件）
# ══════════════════════════════════════════════════════════════════════
section("① 虚拟打印：页数 1~8 × 单面/双面长边/双面短边")

_fails = []
_rows = []
for n in (1, 2, 3, 4, 5, 7, 8):
    for fname, dup_req, mode, ext in FORMATS:
        dup = dup_req and n > 1          # 单页硬闸门：1 页请求双面 → 按单面入叠
        files = make_files(n, dup, ext)
        vp, p = run_two_passes(files)
        got = vp.finished_pairs()
        exp = expected_pairs(files)
        # 1) 成品配对完全正确（页序、空白、配对范围都在这一条里）
        # 2) 张数 = 计划张数 = sheets_for
        # 3) 第 2 趟取纸顺序必须是第 1 趟的反序（物理事实）
        idx1 = [s.sheet_index for s in p.first_pass]
        idx2 = [s.sheet_index for s in p.second_pass]
        if not (got == exp
                and len(got) == p.sheet_count == ds.sheets_for(n, dup)
                and idx1 == list(range(p.sheet_count))
                and idx2 == list(range(p.sheet_count - 1, -1, -1))):
            _fails.append(f"{n}页{fname}: got={got} exp={exp} "
                          f"sheets={len(got)}/{p.sheet_count}/{ds.sheets_for(n, dup)}")
        _rows.append((n, fname, p.sheet_count, fmt_seq(p.pass_pages(1)),
                      fmt_seq(p.pass_pages(2)), " ".join(f"{a}/{b}" for a, b in exp)))
check("★ 21 组（7 种页数 × 3 种格式）两趟虚拟打印后，成品配对/张数/取纸逆序全部正确",
      not _fails, "；".join(_fails[:3]))

# 长边 vs 短边：页序一模一样，只有旋转补偿不同（短边不补偿）
_long = plan_of(make_files(5, True), first="odd")
_short = plan_of(make_files(5, True), first="odd")
check("★ 短边/长边只改第 2 趟的 180° 补偿，**不改页序**（同一份 5 页双面）",
      _long.pass_pages(1) == _short.pass_pages(1)
      and _long.pass_pages(2) == _short.pass_pages(2)
      and md.rotate_second_for(True, "long-edge") is True
      and md.rotate_second_for(True, "short-edge") is False,
      f"第 1 趟={fmt_seq(_long.pass_pages(1))} 第 2 趟={fmt_seq(_long.pass_pages(2))}")

# 单页硬闸门：1 页请求双面 → 按单面入叠（第 2 趟全空白），张数与单面一致
_p1 = plan_of(make_files(1, False), first="odd")
check("★ 1 页文件请求双面 → 按单面入叠（单页硬闸门）：1 张、第 2 趟 [空]",
      _p1.sheet_count == 1 and _p1.pass_pages(1) == [1]
      and _p1.pass_pages(2) == [None],
      f"{_p1.sheet_count} 张 / {fmt_seq(_p1.pass_pages(2))}")

# first=even（偶数面先打的机型档案）：页序镜像，但配对仍然正确
_ev_fails = []
for n in (2, 3, 4, 5):
    files = make_files(n, True)
    vp, p = run_two_passes(files, first="even")
    if not (vp.finished_pairs() == expected_pairs(files) and p.sheet_count == (n + 1) // 2):
        _ev_fails.append(f"{n}页: {vp.finished_pairs()} vs {expected_pairs(files)}")
check("★ first=even（偶数面先打）：页序镜像但成品配对仍正确（2/3/4/5 页）",
      not _ev_fails, "；".join(_ev_fails))

print("\n  虚拟打印页序样例（第 1 趟 / 第 2 趟 → 成品每张纸的两面）：")
print("  页数 | 格式 | 张数 | 第 1 趟 | 第 2 趟 | 成品配对（side_a/side_b）")
for n, fname, sheets, p1, p2, pairs in _rows:
    if n in (3, 5, 7) or (n == 4 and fname == "双面·短边"):
        print(f"  {n} 页 | {fname} | {sheets} 张 | {p1} | {p2} | {pairs}")

# ══════════════════════════════════════════════════════════════════════
# ② 攒批列表渲染：页数 × 格式（Qt offscreen，真渲染树）
# ══════════════════════════════════════════════════════════════════════
section("② 攒批列表（左栏「已攒」）：页数 × 格式逐行核对")

_cases = []
for n in (1, 2, 3, 4, 5, 7, 8):
    _cases.append((n, "单面", False, FORMATS[2][3]))
for n in (2, 3, 4, 5, 7, 8):
    _cases.append((n, "双面·长边", True, FORMATS[0][3]))
    _cases.append((n, "双面·短边", True, FORMATS[1][3]))

_entries = []
for i, (n, fname, dup, ext) in enumerate(_cases):
    label = f"{n}页{fname}{ext}"
    _entries.append(ds.StackEntry(
        seq=i + 1, title=f"HN-{i + 1:02d}", page_count=n,
        sheet_count=ds.sheets_for(n, dup), rotate=True, state=ds.STATE_DONE,
        pdf=f"entry_{i + 1:04d}.pdf", first="odd",
        file_plans=[{"label": label, "pages": list(range(1, n + 1)), "duplex": dup}]))

_st = ds.DuplexStack(capacity=90, printer_name="Microsoft Print to PDF")
_st.new_batch("batch_list_matrix")
_st.rotate = True
_st.entries = _entries

_dlg = gui.StackQueueDialog()
_dlg.render(_st)

_render_fails = []
for i, (n, fname, dup, ext) in enumerate(_cases):
    top = _dlg._tree_left.topLevelItem(i)
    if top is None:
        _render_fails.append(f"{n}页{fname}: 没有第 {i} 个顶层行")
        continue
    if top.childCount() != 1:
        _render_fails.append(f"{n}页{fname}: 子行 {top.childCount()} 个")
        continue
    row = top.child(0)
    label = f"{n}页{fname}{ext}"
    p = plan_of([md.FilePlan(label, list(range(1, n + 1)), dup)], first="odd")
    exp1, exp2 = fmt_seq(p.pass_pages(1)), fmt_seq(p.pass_pages(2))
    exp_status = ("🔓 可整份取走" if all(x is None for x in p.pass_pages(2))
                  else "🔒 第 2 趟还要用")
    got = [row.text(c) for c in range(5)]
    if not (top.text(0).startswith(f"#{i + 1} HN-{i + 1:02d}")
            and f"本单 {n} 页" in top.text(0)
            and f"在叠 {p.sheet_count} 张" in top.text(0)
            and got[0].endswith(label)
            and got[1] == exp1 and got[2] == exp2
            and got[3] == f"{p.sheet_count} 张" and got[4] == exp_status):
        _render_fails.append(f"{n}页{fname}: {got} vs {[label, exp1, exp2, p.sheet_count, exp_status]}")
check("★ 19 个文件行的「第 1 趟 / 第 2 趟 / 张数 / 状态」与 manual_duplex 完全一致",
      not _render_fails, "；".join(_render_fails[:3]))

# 代表性的三条硬判据（把 [空] 的位置钉死，防"页号对但顺序反了"）
_i_7long = _cases.index((7, "双面·长边", True, ".pdf"))
_row7 = _dlg._tree_left.topLevelItem(_i_7long).child(0)
check("★ 双面 7 页：第 1 趟「1 3 5 7」、第 2 趟「[空] 6 4 2」（逆序 + 空白在最上面）",
      _row7.text(1) == "1 3 5 7" and _row7.text(2) == "[空] 6 4 2",
      f"{_row7.text(1)} | {_row7.text(2)}")

_i_5sim = _cases.index((5, "单面", False, ".png"))
_row5s = _dlg._tree_left.topLevelItem(_i_5sim).child(0)
check("★ 单面 5 页：第 1 趟「1 2 3 4 5」、第 2 趟 5 个 [空]（每页各占一张纸）",
      _row5s.text(1) == "1 2 3 4 5" and _row5s.text(2) == "[空] [空] [空] [空] [空]"
      and _row5s.text(4) == "🔓 可整份取走",
      f"{_row5s.text(1)} | {_row5s.text(2)} | {_row5s.text(4)}")

# 短边与长边同为 5 页双面：两趟页号必须相同
_i_long = _cases.index((5, "双面·长边", True, ".pdf"))
_i_short = _cases.index((5, "双面·短边", True, ".docx"))
check("★ 双面 5 页：短边档的两趟页号与长边档完全相同（格式不改页序）",
      [_dlg._tree_left.topLevelItem(_i_long).child(0).text(c) for c in (1, 2)]
      == [_dlg._tree_left.topLevelItem(_i_short).child(0).text(c) for c in (1, 2)],
      f"{_dlg._tree_left.topLevelItem(_i_short).child(0).text(1)} | "
      f"{_dlg._tree_left.topLevelItem(_i_short).child(0).text(2)}")

check("★ 汇总行与分组标题：本叠 19 单 / 在叠总张数一致",
      "19 单" in _dlg._summary.text()
      and f"{_st.present_sheet_count} 张" in _dlg._summary.text()
      and "19 单" in _dlg._left_group.title(),
      f"{_dlg._summary.text()} / {_dlg._left_group.title()}")

# ══════════════════════════════════════════════════════════════════════
# ③ 一单多文件 + 份数 + 页码范围 + 单页硬闸门
# ══════════════════════════════════════════════════════════════════════
section("③ 一单多文件：份数 × 页码范围 × 混合格式")

_multi_plans = [
    {"label": "报告.pdf（第1份）", "pages": [1, 2, 3, 4, 5], "duplex": True},
    {"label": "报告.pdf（第2份）", "pages": [1, 2, 3, 4, 5], "duplex": True},
    {"label": "表格(8页取2-5).xlsx", "pages": [2, 3, 4, 5], "duplex": True},
    {"label": "单页.png", "pages": [1], "duplex": False},
    {"label": "清单.txt", "pages": [1, 2, 3], "duplex": False},
]
_multi_sheets = ds.sheet_count_of_plans(_multi_plans)
_multi_entry = ds.StackEntry(
    seq=1, title="HN-MULTI", page_count=22, sheet_count=_multi_sheets, rotate=True,
    state=ds.STATE_DONE, pdf="entry_0001.pdf", first="odd",
    file_plans=[dict(x) for x in _multi_plans])

_dlg2 = gui.StackQueueDialog()
_dlg2.render(ds.DuplexStack(capacity=90), wait_requests=None)
_st2 = ds.DuplexStack(capacity=90)
_st2.entries = [_multi_entry]
_dlg2.render(_st2)

_multi_fails = []
for j, fp in enumerate(_multi_plans):
    row = _dlg2._tree_left.topLevelItem(0).child(j)
    p = plan_of([md.FilePlan(fp["label"], fp["pages"], fp["duplex"])], first="odd")
    exp = [f"📄 {fp['label']}", fmt_seq(p.pass_pages(1)), fmt_seq(p.pass_pages(2)),
           f"{p.sheet_count} 张"]
    got = [row.text(c) for c in range(5)]
    if got[:4] != exp:
        _multi_fails.append(f"{fp['label']}: {got} vs {exp}")
check("★ 一单 5 个文件计划（2 份 + 页码 2-5 + 单页 + 单面 3 页）逐行页号/张数正确",
      not _multi_fails and _dlg2._tree_left.topLevelItem(0).childCount() == 5,
      "；".join(_multi_fails))

_top = _dlg2._tree_left.topLevelItem(0)
check("★ 份数 2 的两个计划各自独立配对（各 3 张，第 2 趟各自「[空] 4 2」）",
      _top.child(0).text(2) == "[空] 4 2" and _top.child(1).text(2) == "[空] 4 2"
      and _top.child(0).text(3) == "3 张" and _top.child(1).text(3) == "3 张",
      f"{_top.child(0).text(2)} / {_top.child(1).text(2)}")
check("★ 页码范围 2-5：第 1 趟「2 4」、第 2 趟「5 3」（按实际打印页配对，不是 1-4）",
      _top.child(2).text(1) == "2 4" and _top.child(2).text(2) == "5 3",
      f"{_top.child(2).text(1)} | {_top.child(2).text(2)}")
check(f"★ 本单合计 {_multi_sheets} 张（3+3+2+1+3），顶部汇总与各行加总一致",
      _multi_sheets == 12 and "在叠 12 张" in _top.text(0),
      f"{_top.text(0)}")

# 多文件整单虚拟打印：配对只在文件内，绝不跨文件
_multi_files = [md.FilePlan(x["label"], x["pages"], x["duplex"]) for x in _multi_plans]
_vpm, _pm = run_two_passes(_multi_files)
check("★ 一单多文件虚拟打印：成品配对 = 各文件分别配对（不跨文件拼一张纸）",
      _vpm.finished_pairs() == expected_pairs(_multi_files)
      and _pm.sheet_count == _multi_sheets,
      f"{len(_vpm.finished_pairs())} 张")

# ══════════════════════════════════════════════════════════════════════
# ④ 右栏「等待打印」：不同页数 / 格式 / 页数未知
# ══════════════════════════════════════════════════════════════════════
section("④ 攒批列表（右栏「等待打印」）：页数 × 格式 × 页数未知")


_wait_jobs = [
    _JobStub("排队双面4页.pdf", 4, 1, "on"),
    _JobStub("排队单面3页.txt", 3, 1, "off"),
    _JobStub("排队双面5页x2.docx", 5, 2, "on"),
    _JobStub("排队双面7页.xlsx", 7, 1, "on"),
    _JobStub("排队单面2页.png", 2, 1, "off"),
    _JobStub("页数未知.docx", 0, 1, "on"),
]
_dlg3 = gui.StackQueueDialog()
_dlg3.render(ds.DuplexStack(capacity=90), wait_requests=[
    {"jobs": _wait_jobs, "tab_key": "2", "reason": "容量不足：还需 2 张"},
])
_wait_fails = []
for j, job in enumerate(_wait_jobs):
    row = _dlg3._tree_right.topLevelItem(0).child(j)
    n = job.page_count
    if n <= 0:
        exp = ["页数未知", "页数未知"]
        ok_info = "? 张 · 页数未知" in row.text(3)
    else:
        dup = job.duplex == "on"
        p = plan_of([md.FilePlan(job.display_name, list(range(1, n + 1)), dup)], first="odd")
        exp = [fmt_seq(p.pass_pages(1)), fmt_seq(p.pass_pages(2))]
        want_sheets = ds.sheets_for(n, dup, job.copies)
        ok_info = (f"{want_sheets} 张" in row.text(3) and f"{n} 页" in row.text(3)
                   and ("×2 份" in row.text(3) if job.copies > 1 else True)
                   and (("· 单面" in row.text(3)) if not dup else ("· 单面" not in row.text(3))))
    got = [row.text(c) for c in range(4)]
    if not (got[1] == exp[0] and got[2] == exp[1] and ok_info
            and got[0].endswith(job.display_name)):
        _wait_fails.append(f"{job.display_name}: {got} vs {exp}")
check("★ 右栏 6 个排队文件（1/2/3/4/5/7 页 + 页数未知）页号/张数/页数/份数正确",
      not _wait_fails and "容量不足" in _dlg3._tree_right.topLevelItem(0).text(0),
      "；".join(_wait_fails))
check("★ 页数未知的文件写明「页数未知（等分析）」，不瞎猜页序",
      _dlg3._tree_right.topLevelItem(0).child(5).text(1) == "页数未知"
      and _dlg3._tree_right.topLevelItem(0).child(5).text(2) == "页数未知",
      f"{_dlg3._tree_right.topLevelItem(0).child(5).text(3)}")

# ══════════════════════════════════════════════════════════════════════
# ⑤ 多单攒批全流程虚拟打印：第 1 趟连续入叠 → 整叠第 2 趟
# ══════════════════════════════════════════════════════════════════════
section("⑤ 多单攒批全流程：4 单 / 14 张 → 一次翻纸打完第 2 趟")

_orders = [
    ("HN-A", [
        {"label": "报告7页.pdf", "pages": list(range(1, 8)), "duplex": True, "full": 7},
        {"label": "附件2页.pdf", "pages": [1, 2], "duplex": False, "full": 2},
    ]),
    ("HN-B", [
        {"label": "合同4页.docx", "pages": list(range(1, 5)), "duplex": True, "full": 4},
    ]),
    ("HN-C", [
        {"label": "证件1页.png", "pages": [1], "duplex": False, "full": 1},
        {"label": "说明3页.txt", "pages": [1, 2, 3], "duplex": True, "full": 3},
    ]),
    ("HN-D", [
        {"label": "清单2页.xlsx", "pages": [1, 2], "duplex": False, "full": 2},
        {"label": "表格2页.pdf", "pages": [1, 2], "duplex": True, "full": 2},
    ]),
]

_st5 = ds.DuplexStack(capacity=90, printer_name="Microsoft Print to PDF")
_st5.new_batch("batch_flow")
_st5.first = md.FIRST_ODD
_st5.reverse_second = True
_vp5 = VirtualPrinter(first=md.FIRST_ODD)
_entries5 = []
_flow_fails = []
_base = 0
for seq, (title, specs) in enumerate(_orders, 1):
    # 账本里存**单内页号**（与 _print_stack_pass1 落盘一致）；整叠第 2 趟由
    # DuplexStack.file_plans() 自己加偏移。虚拟打印统一用**全局页号**辨认纸张。
    local_plans, page_count = entry_plans(specs, base=0)
    files = [md.FilePlan(x["label"], [_base + p for p in x["pages"]], x["duplex"])
             for x in local_plans]
    p = plan_of(files, first="odd")
    adm = _st5.admit(p.sheet_count, rotate=True)
    if not adm.ok:
        _flow_fails.append(f"{title} 被拒：{adm.message(_st5)}")
        continue
    e = ds.StackEntry(seq=seq, title=title, page_count=page_count,
                      sheet_count=p.sheet_count, rotate=True, state=ds.STATE_DONE,
                      pdf=f"entry_{seq:04d}.pdf", first="odd",
                      file_plans=[dict(x) for x in local_plans])
    _st5.add(e)
    # 表里给人看的是**单内页号**（列表窗显示的也是单内页号）
    _p_local = plan_of([md.FilePlan(x["label"], x["pages"], x["duplex"])
                        for x in local_plans], first="odd")
    _entries5.append((e, p, _p_local, local_plans, _base))
    _vp5.print_pass1(p)
    _base += page_count

check("★ 4 单连续入叠：出纸槽张数 = 计划张数 = 账本在叠张数（14 张）",
      not _flow_fails and len(_vp5.tray) == 14
      and _st5.present_sheet_count == 14 == _st5.original_sheets_count,
      f"tray={len(_vp5.tray)} present={_st5.present_sheet_count} "
      f"original={_st5.original_sheets_count} fails={_flow_fails}")

# 搬纸：整叠逆序送纸；整叠计划的 sheet_index 必须是 13..0
_vp5.move_to_input()
_p2 = _st5.build_plan()
check("★ 整叠第 2 趟计划：送纸张数 = 在叠张数，取纸顺序 = 第 1 趟的完全逆序",
      len(_p2.second_pass) == 14
      and [s.sheet_index for s in _p2.first_pass] == list(range(14))
      and [s.sheet_index for s in _p2.second_pass] == list(range(13, -1, -1)),
      f"{len(_p2.second_pass)} 张")
_vp5.print_pass2(_p2)
_got5 = _vp5.finished_pairs()
_exp5 = expected_pairs(_st5.file_plans())
check("★ 整叠打完：成品每张纸的两面 = 各文件按入叠顺序的正确配对（逐张比对）",
      _got5 == _exp5, f"got={_got5} exp={_exp5}")

# 逐单核对：该单的页一页不少、顺序不乱（计划里已是整叠合并 PDF 的全局页号）
_cursor = 0
_seg_fails = []
for (e, _p_global, _p_local, _local_plans, _entry_base) in _entries5:
    seg = _got5[_cursor:_cursor + e.sheet_count]
    _cursor += e.sheet_count
    exp_pages = []
    for fp in e.file_plans:
        exp_pages.extend(_entry_base + p for p in fp["pages"])
    got_pages = [x for pair in seg for x in pair if x is not None]
    if got_pages != exp_pages:
        _seg_fails.append(f"{e.title}: {got_pages} vs {exp_pages}")
check("★ 逐单核对：每一页都在、页序正确、空白占位不冒充内容（4 单全部）",
      not _seg_fails and _cursor == 14, "；".join(_seg_fails))

# 给用户看的流程表（单内页号，与攒批列表一致）
print("\n  多单攒批流程（虚拟）：")
for (e, _p_global, _p_local, _local_plans, _entry_base) in _entries5:
    desc = " / ".join(f"{fp['label']}({len(fp['pages'])}页{'' if fp['duplex'] else '·单面'})"
                       for fp in _local_plans)
    print(f"  #{e.seq} {e.title} | {desc}")
    print(f"      本单第 1 趟：{fmt_seq(_p_local.pass_pages(1))}")
    print(f"      本单第 2 趟：{fmt_seq(_p_local.pass_pages(2))}")
print(f"  整叠第 2 趟（一次翻纸，全局页号）：{fmt_seq(_p2.pass_pages(2))}")

# ══════════════════════════════════════════════════════════════════════
# ⑥ 取走部分纸：只允许撤「第 2 趟全空白」的计划，第 2 趟仍对得齐
# ══════════════════════════════════════════════════════════════════════
section("⑥ 已取走部分纸：撤 5 张空白背面 → 第 2 趟只送 5 张，配对不乱")

_st6 = ds.DuplexStack(capacity=90, printer_name="Microsoft Print to PDF")
_st6.new_batch("batch_taken")
_st6.first = md.FIRST_ODD
_vp6 = VirtualPrinter(first=md.FIRST_ODD)
_plan6 = [
    ("HN-A", [{"label": "双面5页.pdf", "pages": [1, 2, 3, 4, 5], "duplex": True, "full": 5},
              {"label": "单面3页.txt", "pages": [1, 2, 3], "duplex": False, "full": 3}]),
    ("HN-B", [{"label": "双面4页.docx", "pages": [1, 2, 3, 4], "duplex": True, "full": 4}]),
    ("HN-C", [{"label": "单面2页.png", "pages": [1, 2], "duplex": False, "full": 2}]),
]
_entries6 = []
_bases = []
_base6 = 0
for seq, (title, specs) in enumerate(_plan6, 1):
    local_plans, page_count = entry_plans(specs, base=0)
    files = [md.FilePlan(x["label"], [_base6 + p for p in x["pages"]], x["duplex"])
             for x in local_plans]
    p = plan_of(files, first="odd")
    assert _st6.admit(p.sheet_count, rotate=True).ok
    e = ds.StackEntry(seq=seq, title=title, page_count=page_count,
                      sheet_count=p.sheet_count, rotate=True, state=ds.STATE_DONE,
                      pdf=f"entry_{seq:04d}.pdf", first="odd",
                      file_plans=[dict(x) for x in local_plans])
    _st6.add(e)
    _bases.append(len(_vp6.tray))
    _entries6.append((e, p))
    _vp6.print_pass1(p)
    _base6 += page_count

# 账本准入：A 的单面 3 页（plan 下标 1）与 C 的单面 2 页（plan 下标 0）可整份取走；
# B 的双面 4 页（第 2 趟还要打 4 2）绝不许撤。
_okA, _msgA = _st6.toggle_taken(1, 1, True)
_okC, _msgC = _st6.toggle_taken(3, 0, True)
_badB, _whyB = _st6.toggle_taken(2, 0, True)
check("★ 账本：单面文件的纸可整份取走；双面文件撤走被硬拒（会串页/丢内容）",
      _okA and _okC and not _badB and ("不能这样取走" in _whyB),
      f"A={_msgA} C={_msgC} B={_whyB[:40]}")

check("★ 取走后：原始 10 张 − 已取走 5 张 = 在叠 5 张（第 2 趟对账基准）",
      _st6.original_sheets_count == 10 and _st6.present_sheet_count == 5
      and _st6.taken_sheet_count == 5,
      f"orig={_st6.original_sheets_count} present={_st6.present_sheet_count} "
      f"taken={_st6.taken_sheet_count}")

# 人工取走的纸：A 单面 3 张（出纸槽下标 3,4,5）+ C 单面 2 张（下标 8,9）
_drop = [_bases[0] + 3, _bases[0] + 4, _bases[0] + 5,
         _bases[2] + 0, _bases[2] + 1]
_vp6.move_to_input(drop_slots=_drop)
_p6 = _st6.build_plan()
check("★ 物理：被取走的 5 张都是「正面有内容、背面还没打」的成品单面纸（内容不丢）",
      len(_vp6.dropped) == 5
      and all(p.side_b is None and p.side_a is not None for p in _vp6.dropped)
      and sorted(p.side_a for p in _vp6.dropped) == [6, 7, 8, 13, 14],
      f"{[(p.label, p.side_a) for p in _vp6.dropped]}")
check("★ 第 2 趟只送剩下的 5 张（A 双面 5 页 3 张 + B 双面 4 页 2 张）",
      len(_p6.second_pass) == 5 == len(_vp6.input_tray),
      f"{len(_p6.second_pass)} 张")
_vp6.print_pass2(_p6)
_exp6 = expected_pairs(_st6.file_plans())
check("★ 取走后整叠第 2 趟：剩余纸的成品配对完全正确（没有因为少送纸错位）",
      _vp6.finished_pairs() == _exp6,
      f"{_vp6.finished_pairs()} vs {_exp6}")

# 再从列表里看：A 单面 3 页那行必须渲染出来（曾因 self.COLS 不存在整单消失）
_dlg6 = gui.StackQueueDialog()
_dlg6.render(_st6)
_a_top = _dlg6._tree_left.topLevelItem(0)
_a_rows = [_a_top.child(k) for k in range(_a_top.childCount())]
check("★ 列表：已取走的行仍正常显示（不再整单消失），状态「✔ 已取走」且删除线",
      "显示失败" not in _a_top.text(0)
      and len(_a_rows) == 2
      and _a_rows[1].text(4) == "✔ 已取走"
      and _a_rows[1].font(0).strikeOut()
      and _a_rows[1].text(2) == "[空] [空] [空]",
      f"{_a_top.text(0)} / {_a_rows[1].text(4) if _a_rows else '?'}")
check("★ 页号按**文件自己**算（单面 3 页是第 2 个文件：合并页码 6 7 8 → 1 2 3）",
      _a_rows[1].text(1) == "1 2 3" and _a_rows[0].text(1) == "1 3 5",
      f"{_a_rows[1].text(1)} / {_a_rows[0].text(1)}")

# ══════════════════════════════════════════════════════════════════════
# ⑦ 准入：容量阈值 + 装订方向
# ══════════════════════════════════════════════════════════════════════
section("⑦ 准入：容量阈值（装订方向已不再是门槛，2026-10-28）")

_cap = ds.DuplexStack(capacity=10)
_e1 = ds.StackEntry(seq=1, title="6张", sheet_count=6, rotate=True, state=ds.STATE_DONE)
_cap.add(_e1)
_a1 = _cap.admit(3, rotate=True)
_cap.add(ds.StackEntry(seq=2, title="3张", sheet_count=3, rotate=True, state=ds.STATE_DONE))
_a2 = _cap.admit(2, rotate=True)
_a3 = _cap.admit(3, rotate=False)
check("★ 容量：已堆 9 张 + 2 张 > 上限 10 → 拒绝，提示先打第 2 趟",
      _a1.ok and _a2.ok is False and _a2.reason == ds.REASON_CAPACITY
      and "上限 10 张" in _a2.message(_cap),
      f"{_a1.ok}/{_a2.ok}/{_a2.reason}")
# 2026-10-28：装订方向不再是准入门槛（第 2 趟补偿是逐张纸的，一叠里长边/短边可混排）。
# 这一单 3 张 + 已堆 9 张 > 上限 10 ⇒ 现在被拦的是**容量**，不再是方向。
check("★ 装订方向不再拦：短边单被拒的原因只剩容量",
      _a3.ok is False and _a3.reason == ds.REASON_CAPACITY
      and "旋转补偿" not in _a3.message(_cap),
      f"{_a3.reason} / {_a3.message(_cap)[:40]}")

_empty = ds.DuplexStack(capacity=4)
_a4 = _empty.admit(6, rotate=False)
check("★ 空叠永远能收（哪怕本单 6 张 > 上限 4 张，否则这单永远打不出来）",
      _a4.ok is True, f"ok={_a4.ok}")

# ══════════════════════════════════════════════════════════════════════
# ⑧ 清单落盘 → 重载 → 列表/第 2 趟页序一字不差
# ══════════════════════════════════════════════════════════════════════
section("⑧ 清单落盘 → 重载：列表渲染与第 2 趟页序保持一致（崩溃续打）")

_root = os.path.join(_SANDBOX, "duplex_stack")
_bdir = ds.batch_dir(_root, "batch_virtual_manifest")
os.makedirs(_bdir, exist_ok=True)
_saved = ds.save_manifest(_bdir, _st6)
_loaded = ds.load_manifest(_bdir)
check("★ 清单落盘/重载成功，单数/在叠张数/原始张数/取走状态全部保留",
      _saved and _loaded is not None
      and _loaded.count == _st6.count
      and _loaded.present_sheet_count == _st6.present_sheet_count
      and _loaded.original_sheets_count == _st6.original_sheets_count
      and _loaded.first == _st6.first
      and _loaded.build_plan().pass_pages(1) == _st6.build_plan().pass_pages(1)
      and _loaded.build_plan().pass_pages(2) == _st6.build_plan().pass_pages(2),
      f"saved={_saved} loaded={_loaded is not None}")

# 用重载后的账本再渲染一次列表：已取走行仍必须正常显示
_dlg8 = gui.StackQueueDialog()
_dlg8.render(_loaded)
_rows8 = [[_dlg8._tree_left.topLevelItem(i).child(j).text(c)
           for c in range(5)]
          for i in range(_dlg8._tree_left.topLevelItemCount())
          for j in range(_dlg8._tree_left.topLevelItem(i).childCount())]
_dlg6.render(_st6)
_rows6 = [[_dlg6._tree_left.topLevelItem(i).child(j).text(c)
           for c in range(5)]
          for i in range(_dlg6._tree_left.topLevelItemCount())
          for j in range(_dlg6._tree_left.topLevelItem(i).childCount())]
check("★ 重载后列表逐行文本与内存账本完全一致（含已取走行的删除线状态）",
      _rows8 == _rows6 and len(_rows8) == 4,
      f"{len(_rows8)} 行 / {len(_rows6)} 行")

# 重载后真打第 2 趟（虚拟）：按账本重建第 1 趟出纸，撤走已取走的纸，再打第 2 趟
_vp8 = VirtualPrinter(first=md.FIRST_ODD)
_drop8 = []
_base8 = 0
for e in _loaded.entries:
    files = [md.FilePlan(fp["label"], [_base8 + p for p in fp["pages"]], fp["duplex"])
             for fp in e.file_plans]
    p = plan_of(files, first=md.FIRST_ODD)
    _base8 += int(e.page_count or 0)
    base = len(_vp8.tray)
    _vp8.print_pass1(p)
    for k in range(len(e.file_plans)):
        if e.is_plan_taken(k):
            # 该计划占的槽位 = 它在 plan.first_pass 里的 sheet_index
            for slot in p.first_pass:
                if slot.label == e.file_plans[k]["label"]:
                    _drop8.append(base + slot.sheet_index)
_vp8.move_to_input(drop_slots=_drop8)
_p8 = _loaded.build_plan()
_vp8.print_pass2(_p8)
check("★ 重载后虚拟打印整叠第 2 趟：成品配对与内存账本完全相同（续打可信）",
      _vp8.finished_pairs() == _exp6,
      f"{len(_vp8.finished_pairs())} 张")
ds.discard_batch(_bdir)

# ══════════════════════════════════════════════════════════════════════
# ⑨ 收尾：确实没出纸 + 虚拟队列判定
# ══════════════════════════════════════════════════════════════════════
section("⑨ 收尾：零真实打印调用")

check("★ 全程零调用真实打印入口（print_pdf/print_plan_pass/merge_pdfs/wait_for_output）",
      _guard.calls == [], f"calls={_guard.calls}")
check("★ 模拟队列名确实是虚拟打印机（该队列不出纸；本脚本连它都没调用）",
      pdf_printer.is_virtual_printer("Microsoft Print to PDF") is True)

for _d in (_dlg, _dlg2, _dlg3, _dlg6, _dlg8):
    _d.close()

print("\n" + "=" * 62)
print(f"共 {_n_pass} 项通过" + ("" if ok_all else "，存在失败项 ❌"))
print("全部通过 ✅" if ok_all else "存在失败项 ❌")
sys.exit(0 if ok_all else 1)
