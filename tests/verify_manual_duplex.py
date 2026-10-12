# -*- coding: utf-8 -*-
"""手动双面「两趟页序」验收（纯逻辑，不需要打印机）

为什么值得单独验收：这套页序是「纸怎么翻、哪一趟打哪一面」的全部正确性所在，错了就整本
错位（打出来是一叠好纸但页序全乱）。它不是"看起来对就行"，而是有可推导的物理过程：

    · 第 1 趟按 first_pass 顺序出纸，出纸盒是 face down → 盒内**底→顶 = 第 1 趟印刷顺序**；
    · 弹窗指令「不改变方向…保持相同方向…已打印面朝下」⇒ 整叠不翻面、不重排地放回进纸盒，
      进纸盒从**最上面**取纸 ⇒ 第 2 趟抽纸顺序 = 第 1 趟顺序的**逆序**；
    · 每张纸在两趟里各占一个槽位（没内容就补空白页），漏一个后面全错位。

本脚本不自己证明自己：**按上述物理过程独立模拟一遍**（`_simulate_physical`），
再断言模拟出来的每张纸两面 == 引擎给出的配对。机器模型与页序算法是两个独立的表述，
两侧对得上才算通过。

用法: python tests/verify_manual_duplex.py   （exit 0 = 全部通过）
"""
import io
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "local_print_tool"))

import manual_duplex as md  # noqa: E402

ok_all = True


def check(name, cond, detail=""):
    global ok_all
    if not cond:
        ok_all = False
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


def _k(x):
    """排序键：页码在前、空白(None)在后（避免 None 与 int 直接比较）。"""
    return (1, 0) if x is None else (0, x)


def _pair_key(a, b):
    return sorted([a, b], key=_k)


def _simulate_physical(plan):
    """按物理过程独立模拟一趟手动双面，返回 {sheet_index: {"first": 面, "second": 面}}。

    · 第 1 趟：按 first_pass 顺序出纸 → 出纸盒底→顶 = 印刷顺序（face down 出纸）；
    · 整叠不翻面搬回进纸盒 → 盒内底→顶不变，但**从最上面取纸** → 抽纸顺序是逆序；
    · 第 2 趟：按 second_pass 顺序在"当前被抽走的那张纸"上打印。
    """
    n = plan.sheet_count
    bin_stack = list(range(n))                      # 出纸盒：底→顶
    got: dict[int, dict] = {}
    for slot in plan.first_pass:
        got.setdefault(slot.sheet_index, {})["first"] = slot.page
    feed_order = list(reversed(bin_stack))          # 进纸盒从最上面取
    assert len(plan.second_pass) == len(feed_order), \
        f"两趟槽位数不守恒: 第1趟 {len(plan.first_pass)} / 第2趟 {len(plan.second_pass)} / 纸张 {n}"
    for slot, sheet_idx in zip(plan.second_pass, feed_order):
        got.setdefault(sheet_idx, {})["second"] = slot.page
    return got


def _check_pairs(name, plan):
    """核心断言：物理模拟出来的每张纸两面 == 引擎配对（含空白面）。"""
    sim = _simulate_physical(plan)
    bad = []
    for idx, sh in enumerate(plan.sheets):
        phys = sim.get(idx, {})
        if _pair_key(phys.get("first"), phys.get("second")) != _pair_key(sh.side_a, sh.side_b):
            bad.append((idx, sh.label, sh.side_a, sh.side_b, phys))
    check(f"{name}：物理模拟的两面配对 == 引擎配对", not bad, f"{bad[:3]}")
    # 槽位守恒
    check(f"{name}：两趟槽位数都等于纸张数",
          len(plan.first_pass) == plan.sheet_count and len(plan.second_pass) == plan.sheet_count,
          f"{len(plan.first_pass)}/{len(plan.second_pass)}/{plan.sheet_count}")
    return sim, bad


# ============================================================
# ① 用户举的真实场景：三个文件合成一批（4页双面 + 1页单面 + 5页双面）
# ============================================================
print("\n=== ① 三文件合成的批（4d + 1s + 5d）===")
files = [md.FilePlan("F1", [1, 2, 3, 4], True),
         md.FilePlan("F2", [1], False),
         md.FilePlan("F3", [1, 2, 3, 4, 5], True)]
plan = md.plan_passes(files)
check("纸张数 = 2 + 1 + 3", plan.sheet_count == 6, f"{plan.sheet_count}")
check("★ 第 1 趟 = 每张纸的前一面（F1 奇数页 + F2 的单面页 + F3 奇数页）",
      plan.pass_pages(1) == [1, 3, 1, 1, 3, 5], f"{plan.pass_pages(1)}")
check("★ 第 2 趟 = 整体逆序的补打面（末张纸先被抽走；F2/F3 各一个空白占位）",
      plan.pass_pages(2) == [None, 4, 2, None, 4, 2], f"{plan.pass_pages(2)}")
check("第 2 趟标签顺序为 F3,F3,F3,F2,F1,F1",
      [s.label for s in plan.second_pass] == ["F3", "F3", "F3", "F2", "F1", "F1"],
      f"{[s.label for s in plan.second_pass]}")
check("第 2 趟槽位索引是逆序（0..n-1 反转）",
      [s.sheet_index for s in plan.second_pass] == list(range(5, -1, -1)))
_check_pairs("三文件批", plan)
check("★ 空白占位页只出现在第 2 趟（odd 默认；第 1 趟恒无空白）",
      plan.blank_slots_first == 0 and plan.blank_slots_second == 2,
      f"{plan.blank_slots_first}/{plan.blank_slots_second}")
check("物理面数 = 纸张数 × 2", plan.total_sides == 12, f"{plan.total_sides}")

# ============================================================
# ② 单文件：奇/偶数页 × 多份
# ============================================================
print("\n=== ② 单文件 奇/偶数页 ===")
p6 = md.plan_single([1, 2, 3, 4, 5, 6])
check("6 页：第 1 趟 = 1 3 5，第 2 趟 = 6 4 2（成品最上面是第 1 页）",
      p6.pass_pages(1) == [1, 3, 5] and p6.pass_pages(2) == [6, 4, 2],
      f"{p6.pass_pages(1)} / {p6.pass_pages(2)}")
check("6 页：无空白占位", p6.blank_slots_first == 0 and p6.blank_slots_second == 0)
_check_pairs("6 页", p6)

p5 = md.plan_single([1, 2, 3, 4, 5])
check("★ 5 页（奇数）：第 1 趟 = 1 3 5，第 2 趟 = 〔空白〕 4 2（末张纸背面留空）",
      p5.pass_pages(1) == [1, 3, 5] and p5.pass_pages(2) == [None, 4, 2],
      f"{p5.pass_pages(1)} / {p5.pass_pages(2)}")
check("5 页：空白在第 2 趟且只有 1 个（odd 约定的代价）",
      p5.blank_slots_first == 0 and p5.blank_slots_second == 1)
_check_pairs("5 页", p5)

p1 = md.plan_single([1])
check("★ 1 页双面 = 1 张纸：第 1 趟打正文、第 2 趟空白占位（占位纸同样不能丢）",
      p1.pass_pages(1) == [1] and p1.pass_pages(2) == [None],
      f"{p1.pass_pages(1)} / {p1.pass_pages(2)}")
_check_pairs("1 页", p1)

pc = md.plan_single([1, 2, 3, 4, 5], copies=2)
check("5 页 × 2 份 → 6 张纸", pc.sheet_count == 6, f"{pc.sheet_count}")
check("多份：第 1 趟 = 两份序列顺序拼接",
      pc.pass_pages(1) == [1, 3, 5, 1, 3, 5], f"{pc.pass_pages(1)}")
check("多份：第 2 趟 = 整批统一逆序（各份分组自动对齐）",
      pc.pass_pages(2) == [None, 4, 2, None, 4, 2], f"{pc.pass_pages(2)}")
_check_pairs("5 页 × 2 份", pc)

# ============================================================
# ③ 镜像约定（first="odd"）与第 2 趟正序（reverse_second=False）
# ============================================================
print("\n=== ③ 约定开关 ===")
# odd 是现在的默认约定（2026-10 HP LaserJet 1020 实机选定：成品叠最上面是第 1 页）
check("默认约定 = odd", md.plan_single([1]).first == md.FIRST_ODD)
po = md.plan_single([1, 2, 3, 4, 5], first=md.FIRST_ODD)
check("odd 约定：第 1 趟 = 1 3 5（纯内容），第 2 趟 = 〔空白〕 4 2",
      po.pass_pages(1) == [1, 3, 5] and po.pass_pages(2) == [None, 4, 2],
      f"{po.pass_pages(1)} / {po.pass_pages(2)}")
check("odd 约定：空白只出现在第 2 趟",
      po.blank_slots_first == 0 and po.blank_slots_second == 1)
_check_pairs("odd 约定 5 页", po)

# 镜像约定仍要可用（配置项：别的机型/别的成品观感可能需要它）
pe = md.plan_single([1, 2, 3, 4, 5], first=md.FIRST_EVEN)
check("even 约定（镜像）：第 1 趟 = 2 4 〔空白〕，第 2 趟 = 5 3 1",
      pe.pass_pages(1) == [2, 4, None] and pe.pass_pages(2) == [5, 3, 1],
      f"{pe.pass_pages(1)} / {pe.pass_pages(2)}")
check("even 约定：空白只出现在第 1 趟（与 odd 恰好镜像）",
      pe.blank_slots_first == 1 and pe.blank_slots_second == 0)
_check_pairs("even 约定 5 页", pe)

pf = md.plan_single([1, 2, 3, 4, 5], reverse_second=False)
check("reverse_second=False（odd 默认）：第 1 趟 = 1 3 5、第 2 趟正序 = 2 4 空白",
      pf.pass_pages(1) == [1, 3, 5] and pf.pass_pages(2) == [2, 4, None],
      f"{pf.pass_pages(1)} / {pf.pass_pages(2)}")
# 反证：正序时物理模拟必然配对错位 —— 这就是"该开关只能在实机核对后才敢改"的依据
sim_bad = _simulate_physical(pf)
mismatch = [i for i, sh in enumerate(pf.sheets)
            if _pair_key(sim_bad.get(i, {}).get("first"), sim_bad.get(i, {}).get("second"))
            != _pair_key(sh.side_a, sh.side_b)]
check("★ 反证：第 2 趟正序会导致配对错位（所以默认必须逆序）",
      bool(mismatch), f"错位张数={len(mismatch)}")

try:
    md.plan_single([1, 2], first="nonsense")
    check("非法约定应抛 ValueError", False)
except ValueError:
    check("非法约定 → ValueError", True)

# ============================================================
# ④ 单面文件 / 混合批 / 页码范围
# ============================================================
print("\n=== ④ 单面文件与混合批 ===")
ps = md.plan_passes([md.FilePlan("A", [1, 2, 3], False)])
check("单面多页文件：每页独占一张纸（不配对）",
      [(s.side_a, s.side_b) for s in ps.sheets] == [(1, None), (2, None), (3, None)],
      f"{[(s.side_a, s.side_b) for s in ps.sheets]}")
check("单面文件的内容全在第 1 趟，第 2 趟是纯空白占位（odd 约定内容先打）",
      ps.pass_pages(1) == [1, 2, 3] and ps.pass_pages(2) == [None, None, None],
      f"{ps.pass_pages(1)} / {ps.pass_pages(2)}")
_check_pairs("单面 3 页", ps)
check("⚠ 整批全单面 → 给出『应直接单面打印』提示", any("直接单面打印" in n for n in ps.notes),
      f"{ps.notes}")

psd = md.plan_passes([md.FilePlan("A", [1, 2], False), md.FilePlan("B", [1, 2, 3], True)])
check("混合批：单面每页一张纸 + 双面两页一张纸",
      [(s.label, s.side_a, s.side_b) for s in psd.sheets] ==
      [("A", 1, None), ("A", 2, None), ("B", 1, 2), ("B", 3, None)],
      f"{[(s.label, s.side_a, s.side_b) for s in psd.sheets]}")
_check_pairs("混合批", psd)

pr = md.plan_single([1, 2, 5, 6])          # 页码范围选出的不连续页
check("页码范围：按选中页顺序配对（1/2、5/6），不按原文档页码奇偶",
      [(s.side_a, s.side_b) for s in pr.sheets] == [(1, 2), (5, 6)],
      f"{[(s.side_a, s.side_b) for s in pr.sheets]}")
_check_pairs("不连续页码范围", pr)

check("空输入 → 0 张纸 + 提示", md.plan_single([]).sheet_count == 0
      and any("没有任何页面" in n for n in md.plan_single([]).notes))

# ============================================================
# ⑤ 通用不变式（在多种组合上跑物理模拟）
# ============================================================
print("\n=== ⑤ 不变式（多组合物理模拟）===")
combos = []
for n in range(1, 9):
    for copies in (1, 2, 3):
        for first in (md.FIRST_EVEN, md.FIRST_ODD):
            combos.append((f"{n}页×{copies}份/{first}", md.plan_single(list(range(1, n + 1)),
                                                                        copies=copies, first=first)))
for label, fp in [("4d+1s+5d", files),
                  ("2s+3d", [md.FilePlan("A", [1, 2], False), md.FilePlan("B", [1, 2, 3], True)]),
                  ("7d+7d", [md.FilePlan("A", list(range(1, 8))), md.FilePlan("B", list(range(1, 8)))])]:
    for copies in (1, 2):
        combos.append((f"{label}×{copies}", md.plan_passes(fp, copies=copies)))

bad_total, bad_blank = [], []
for name, pl in combos:
    sim = _simulate_physical(pl)
    for idx, sh in enumerate(pl.sheets):
        phys = sim.get(idx, {})
        if _pair_key(phys.get("first"), phys.get("second")) != _pair_key(sh.side_a, sh.side_b):
            bad_total.append((name, idx))
    # 偶数面约定下第 2 趟不该有空白（有就说明配对逻辑算漏了一张纸）
    if pl.first == md.FIRST_EVEN and pl.blank_slots_second != 0:
        bad_blank.append((name, pl.blank_slots_second))
check(f"★ {len(combos)} 种组合（1~8 页 × 1~3 份 × 两种约定 × 混合批）物理模拟全对",
      not bad_total, f"错配={bad_total[:5]}")
check("★ 偶数面约定下第 2 趟恒无空白占位页", not bad_blank, f"{bad_blank[:5]}")

bad_odd_blank = [name for name, pl in combos
                 if pl.first == md.FIRST_ODD and pl.blank_slots_first != 0]
check("★ 奇数面（默认）约定下第 1 趟恒无空白占位页（内容面全在第 1 趟）",
      not bad_odd_blank, f"{bad_odd_blank[:5]}")

bad_cover = []
for name, pl in combos:
    got = sorted(p for p in (pl.pass_pages(1) + pl.pass_pages(2)) if p is not None)
    want = sorted(p for sh in pl.sheets for p in (sh.side_a, sh.side_b) if p is not None)
    # 两趟合起来必须恰好、且只覆盖每个内容面一次（多打/漏打都会在这里现形）
    if got != want:
        bad_cover.append((name, len(got), len(want)))
check("内容面多重集守恒（两趟合起来恰好覆盖每个内容面一次）", not bad_cover, f"{bad_cover[:5]}")

# ============================================================
# ⑤b 批次合并（GUI 形态）：**每份文件的每一份都必须整张纸配对**
#     奇数页文件多份打印时，空白要落在"每一份的末尾"（扁平页码里的第 4、第 8 个槽位），
#     绝不能让第 2 份的第 1 页接到第 1 份最后一张纸的背面。
#     GUI 的 _print_manual_batch 就是这么建 FilePlan 的：一份一个 FilePlan、页码复用（不推进）。
# ============================================================
print("\n=== ⑤b 批次合并：每份各自整张纸配对（奇数页 + 多份）===")


def _gui_style_plans(pages_by_file: list[tuple[str, list[int], int]]) -> list:
    """按 GUI 的构造方式生成 FilePlan：[（标签, 页码范围, 份数）] → 一份一个 FilePlan、页码复用。"""
    out = []
    for label, pages, copies in pages_by_file:
        for c in range(copies):
            out.append(md.FilePlan(f"{label}（第{c + 1}份）" if copies > 1 else label,
                                   list(pages), True))
    return out


# 用户举的例子：3 页文件 × 2 份 → 扁平页码第 4、第 8 个槽位是空白
pa = md.plan_passes(_gui_style_plans([("A", [1, 2, 3], 2)]), copies=1)
check("★ 3 页 × 2 份：共 4 张纸，第 1 趟 = 1 3 1 3（每份各自奇偶）",
      pa.sheet_count == 4 and pa.pass_pages(1) == [1, 3, 1, 3], f"{pa.pass_pages(1)}")
check("★ 第 2 趟 = 〔空白〕 2 〔空白〕 2（两份的空白各自落在本份末尾）",
      pa.pass_pages(2) == [None, 2, None, 2], f"{pa.pass_pages(2)}")
check("★ 空白在**第 2 趟**、每份 1 个（扁平页码的第 4、第 8 槽位）",
      pa.blank_slots_first == 0 and pa.blank_slots_second == 2,
      f"{pa.blank_slots_first}/{pa.blank_slots_second}")
_check_pairs("3 页 × 2 份（GUI 形态）", pa)

# 每张纸的两面必须来自**同一组**（同一文件的同一份）—— 这就是"第 2 份第 1 页不许接到第 1 份末页"
sim = _simulate_physical(pa)
mixed = []
for idx, sh in enumerate(pa.sheets):
    labels = {s.label for s in list(pa.first_pass) + list(pa.second_pass)
              if s.sheet_index == idx}
    if len(labels) != 1 or sh.label not in labels:
        mixed.append((idx, sh.label, sim.get(idx), sorted(labels)))
check("★ 物理上每张纸两面都来自同一份（无跨份错位）", not mixed, f"{mixed[:3]}")
per_copy = []
for c in (1, 2):
    lab = f"A（第{c}份）"
    seq = []
    for sh in pa.sheets:
        if sh.label == lab:
            seq += [sh.side_a, sh.side_b]
    per_copy.append(seq)
check("★ 按份展开的槽位 = [1,2,3,空白] / [1,2,3,空白]（等价于驱动插空白的位置）",
      per_copy == [[1, 2, 3, None], [1, 2, 3, None]], f"{per_copy}")

# 多文件 × 多份：每组自成一块，不许互相插队
pm = md.plan_passes(_gui_style_plans([("A", [1, 2, 3], 2), ("B", [4, 5, 6, 7], 2)]), copies=1)
check("3 页×2 + 4 页×2：共 8 张纸（2+2 + 2+2）", pm.sheet_count == 8, f"{pm.sheet_count}")
check("★ 第 1 趟 = 1 3 1 3 4 6 4 6（各份各自，不串）",
      pm.pass_pages(1) == [1, 3, 1, 3, 4, 6, 4, 6], f"{pm.pass_pages(1)}")
check("★ 第 2 趟 = 7 5 7 5 〔空白〕 2 〔空白〕 2（整批统一逆序）",
      pm.pass_pages(2) == [7, 5, 7, 5, None, 2, None, 2], f"{pm.pass_pages(2)}")
_check_pairs("3 页×2 + 4 页×2（GUI 形态）", pm)
sim_m = _simulate_physical(pm)
mixed_m = []
for idx, sh in enumerate(pm.sheets):
    labels = {s.label for s in list(pm.first_pass) + list(pm.second_pass) if s.sheet_index == idx}
    if len(labels) != 1 or sh.label not in labels:
        mixed_m.append((idx, sh.label, sorted(labels)))
check("★ 多文件 × 多份：每张纸两面都来自同一文件同一份", not mixed_m, f"{mixed_m[:3]}")


print("\n=== ⑥ 摘要文案 ===")
desc = md.plan_passes(files).describe()
check("摘要含两趟序列与文件标签", "F1:2" in desc and "F3:5" in desc and "第 1 趟" in desc)
check("摘要含空白占位数与『整叠不翻面』提示",
      "空白占位页" in desc and "整叠不翻面" in desc)
check("单文件摘要不带文件标签（纯页码更易读）", "F1" not in md.plan_single([1, 2, 3]).describe())
check("配对摘要可读（每张纸两面）", md.plan_single([1, 2, 3]).pair_lines() == ["1/2", "3/空白"],
      f"{md.plan_single([1, 2, 3]).pair_lines()}")
check("expected_pairs 与纸张配对一致",
      md.expected_pairs([1, 2, 3, 4, 5]) == [(1, 2), (3, 4), (5, None)],
      f"{md.expected_pairs([1, 2, 3, 4, 5])}")

# 渲染层要 0-based，引擎内部是 1-based：转换只在引擎里做一次（多页文档上错一页 = 整本错位）
check("★ pass_page_indices 是 0-based 且保留空白占位（5 页 odd 默认：第 1 趟 0,2,4、第 2 趟 None,3,1）",
      p5.pass_page_indices(1) == [0, 2, 4] and p5.pass_page_indices(2) == [None, 3, 1],
      f"{p5.pass_page_indices(1)} / {p5.pass_page_indices(2)}")
check("pass_page_indices 与 pass_pages 仅差 1（1-based → 0-based）",
      all((a is None and b is None) or (a is not None and b == a - 1)
          for a, b in zip(p5.pass_pages(1), p5.pass_page_indices(1))),
      f"{p5.pass_pages(1)} → {p5.pass_page_indices(1)}")
check("1 页文档：第 1 趟 = [0]、第 2 趟 = [None]（可直接喂 pages_override）",
      p1.pass_page_indices(1) == [0] and p1.pass_page_indices(2) == [None],
      f"{p1.pass_page_indices(1)} / {p1.pass_page_indices(2)}")

print("\n" + "=" * 56)
print("手动双面页序验收:", "全部通过 ✅" if ok_all else "存在失败 ❌")
sys.exit(0 if ok_all else 1)
