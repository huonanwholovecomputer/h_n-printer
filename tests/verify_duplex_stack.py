# -*- coding: utf-8 -*-
"""攒批模式（多任务合并翻纸）纸叠账本验收（纯逻辑，不需要打印机）

为什么值得单独验收：攒批把「一个任务翻一次纸」变成「多个任务的第 1 趟堆成一叠、
统一翻一次纸打第 2 趟」。页序的正确性从此多了一层**跨单拼接**：

    整叠第 2 趟 = 各单第 2 趟按**单的逆序**拼接
    ⇔ 把所有单合成一份文档后整体逆序（因为纸张配对只在单/文件内部完成，绝不跨单）

这两句话是两个独立表述，本脚本按物理过程独立模拟一遍再断言两者一致：
第 1 趟按各单顺序出纸（出纸盒底→顶 = 印刷顺序）→ 整叠不翻面搬回进纸盒 →
从最上面取纸 ⇒ 第 2 趟抽纸顺序 = 整叠第 1 趟顺序的逆序。

覆盖：张数估算（单/双面/份数）、准入（空叠/容量/装订方向）、
跨单页序等效性（随机逐组合）、物理模拟两面配对、清单存取与容错、批次目录管理。

用法: python tests/verify_duplex_stack.py   （exit 0 = 全部通过）
"""
import io
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "local_print_tool"))

import manual_duplex as md          # noqa: E402
import duplex_stack as ds           # noqa: E402

ok_all = True


def check(name, cond, detail=""):
    global ok_all
    if not cond:
        ok_all = False
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


# ============================================================
# ① 张数估算：与 manual_duplex.build_sheets 的配对规则一一对应
# ============================================================
print("\n=== ① 张数估算 sheets_for ===")
bad = []
for pages in range(1, 10):
    for duplex in (True, False):
        for copies in (1, 2, 3):
            fp = md.FilePlan("F", list(range(1, pages + 1)), duplex)
            truth = len(md.build_sheets([fp], copies))
            got = ds.sheets_for(pages, duplex, copies)
            if got != truth:
                bad.append((pages, duplex, copies, got, truth))
check("sheets_for == build_sheets 张数（1~9 页 × 单双面 × 1~3 份 = 54 组合）",
      not bad, f"{bad[:3]}")

# 打印前估算：页数未知按 1 页兜底并置 unknown
class _J:
    def __init__(self, pc, dup="on", copies=1, rng=""):
        self.page_count = pc
        self.duplex = dup
        self.copies = copies
        self.page_range = rng


est, unknown = ds.estimate_jobs_sheets([_J(5), _J(1, "off")])
check("estimate_jobs_sheets：5 页双面(3 张) + 1 页单面(1 张) = 4 张",
      est == 4 and not unknown, f"est={est} unknown={unknown}")
est2, unknown2 = ds.estimate_jobs_sheets([_J(0)])
check("estimate_jobs_sheets：页数未知 → 按 1 页兜底且 unknown=True",
      est2 == 1 and unknown2, f"est={est2} unknown={unknown2}")
est3, _ = ds.estimate_jobs_sheets([_J(10, "on", 1, "1-4")],
                                  count_selected=lambda r, n: 4)
check("estimate_jobs_sheets：页码范围生效（10 页里选 4 页双面 = 2 张）",
      est3 == 2, f"est={est3}")
est4, _ = ds.estimate_jobs_sheets([_J(3, "off", 2)])
check("estimate_jobs_sheets：单面 3 页 × 2 份 = 6 张（单面不配对）",
      est4 == 6, f"est={est4}")


# ============================================================
# ② 准入判断：空叠永远收；非空叠看容量与装订方向
# ============================================================
print("\n=== ② 准入判断 admit ===")
st = ds.DuplexStack(capacity=10)
a = st.admit(50, True)
check("空叠永远能收（单张数超容量也放行，否则这单永远打不出来）", a.ok, a.reason)

e1 = ds.StackEntry(seq=1, sheet_count=6, rotate=True, file_plans=[
    {"label": "A", "pages": [1, 2, 3, 4, 5, 6], "duplex": True}], page_count=6)
st.add(e1)
check("单加入后 rotate 被本叠锁定", st.rotate is True)
check("容量 10、已堆 6：再加 4 张放行", st.admit(4, True).ok)
a_cap = st.admit(5, True)
check("再加 5 张超容量 → 拒绝且原因是容量",
      (not a_cap.ok) and a_cap.reason == ds.REASON_CAPACITY, a_cap.reason)
a_rot = st.admit(1, False)
check("装订方向不一致 → 拒绝且原因是方向",
      (not a_rot.ok) and a_rot.reason == ds.REASON_ROTATE, a_rot.reason)
check("拒绝文案含容量与已堆张数",
      "放不下" in a_cap.message(st) and "6" in a_cap.message(st), a_cap.message(st))
check("方向不一致的文案解释『旋转补偿』",
      "旋转补偿" in a_rot.message(st))
check("total_sheets / remaining / sheet_text",
      st.total_sheets == 6 and st.remaining == 4 and st.sheet_text() == "6/10 张")

st2 = ds.DuplexStack(capacity=10)
check("空叠 + rotate=False 也能收（第一单定方向）", st2.admit(3, False).ok)
check("空叠 + sheets<=0 → empty 原因",
      st2.admit(0, True).reason == ds.REASON_EMPTY)


# ============================================================
# ③ 跨单页序等效性（核心）：全局逆序 == 各单逆序拼接
# ============================================================
print("\n=== ③ 跨单页序等效性（随机逐组合）===")


def _mk_entry(seq, file_specs):
    """file_specs = [(label, pages, duplex)]，pages 是**单内** 1-based 页码。"""
    plans, off = [], 0
    page_count = 0
    for label, pages, dup in file_specs:
        plans.append({"label": label, "pages": [off + p for p in pages], "duplex": dup})
        off += max(pages)
    page_count = off
    # 单内张数：按 build_sheets 算（含空白占位）
    sheets = len(md.build_sheets(
        [md.FilePlan(p["label"], p["pages"], p["duplex"]) for p in plans]))
    return ds.StackEntry(seq=seq, title=f"#{seq}", page_count=page_count,
                         sheet_count=sheets, rotate=True, file_plans=plans)


def _k(x):
    return (1, 0) if x is None else (0, x)


def _key(a, b):
    return sorted([a, b], key=_k)


random.seed(20261012)
mismatch = []
for trial in range(200):
    n_entries = random.randint(1, 5)
    entries = []
    for i in range(n_entries):
        n_files = random.randint(1, 3)
        specs = []
        for f in range(n_files):
            pages = random.randint(1, 7)
            dup = random.random() < 0.7
            specs.append((f"E{i}F{f}", list(range(1, pages + 1)), dup))
        entries.append(_mk_entry(i + 1, specs))

    stack = ds.DuplexStack(capacity=9999)
    for e in entries:
        stack.add(e)
    for first in (md.FIRST_ODD, md.FIRST_EVEN):
        for rev in (True, False):
            stack.first, stack.reverse_second = first, rev
            # 全局法：整叠合成一份文档后整体排序（= DuplexStack.build_plan，被测对象）
            gp = stack.build_plan()
            global_seq = [(s.label, s.page) for s in gp.second_pass]
            # 各单法：逐单**按同一个全局偏移**单独排 → 取各单第 2 趟 → 按单序拼接
            # （reverse_second 时整叠是"单逆序"，而每单自己内部也已经逆序）
            per = []
            off = 0
            for e in entries:
                plans = [md.FilePlan(fp["label"], [off + int(p) for p in fp["pages"]],
                                     fp["duplex"]) for fp in e.file_plans]
                pp = md.plan_passes(plans, copies=1, first=first, reverse_second=rev)
                per.append([(s.label, s.page) for s in pp.second_pass])
                off += e.page_count
            by_entry = []
            for chunk in (reversed(per) if rev else per):
                by_entry.extend(chunk)
            if global_seq != by_entry:
                mismatch.append((trial, first, rev, global_seq[:6], by_entry[:6]))
                break
    if mismatch:
        break
check("整叠第 2 趟 == 各单第 2 趟按单逆序拼接（200 组 × 奇数/偶数先打 × 逆序/正序）",
      not mismatch, f"{mismatch[:1]}")

# 全局页序必须与"各单页数之和"对得上（偏移算错就会串单）
bad_off = []
for trial in range(50):
    entries = [_mk_entry(1, [("A", [1, 2, 3], True)]),
               _mk_entry(2, [("B", [1], False)]),
               _mk_entry(3, [("C", [1, 2, 3, 4, 5], True)])]
    stack = ds.DuplexStack(capacity=9999)
    for e in entries:
        stack.add(e)
    plans = stack.file_plans()
    expect_off = 0
    for e, pl in zip(entries, plans):
        if pl.pages[0] != expect_off + 1:
            bad_off.append((e.seq, pl.pages[0], expect_off + 1))
        expect_off += e.page_count
check("跨单偏移正确（第 k 单的页码 = 前面各单页数之和 + 单内页码）", not bad_off, f"{bad_off[:3]}")


# ============================================================
# ④ 物理模拟：整叠两面配对（跨单也不串页）
# ============================================================
print("\n=== ④ 物理模拟：整叠两面配对 ===")


def simulate_stack(stack):
    """按物理过程独立模拟：各单第 1 趟依次堆进出纸盒 → 整叠搬回 → 逆序抽纸打第 2 趟。

    返回 (phys, ref)：
      · phys：{全局纸张序号: {"first": 第 1 趟内容, "second": 第 2 趟内容}}
        第 2 趟内容取自 **被测对象** `stack.build_plan()`；
      · ref：{全局纸张序号: (side_a, side_b, label)}，逐单**按全局偏移**独立排一遍算出的
        应有配对（不经过 build_plan）。
    两侧对得上，才说明"整叠一次性逆序"与"各单分别逆序再按单拼"确实等价、且偏移没错。
    """
    off = 0
    ref = {}
    order = []          # 第 1 趟实际出纸顺序（全局纸张序号 → (label, page)）
    gi = 0
    for e in stack.entries:
        plans = [md.FilePlan(fp["label"], [off + int(p) for p in fp["pages"]], fp["duplex"])
                 for fp in e.file_plans]
        pp = md.plan_passes(plans, copies=1, first=stack.first,
                            reverse_second=stack.reverse_second)
        for slot in pp.first_pass:
            order.append((slot.label, slot.page))
        for sh in pp.sheets:
            ref[gi] = (sh.side_a, sh.side_b, sh.label)
            gi += 1
        off += e.page_count

    gp = stack.build_plan()
    assert len(gp.second_pass) == len(order), \
        f"整叠两趟槽位数不守恒: {len(gp.second_pass)} vs {len(order)}"
    phys = {}
    for idx, (_lbl, page) in enumerate(order):
        phys[idx] = {"first": page}
    # 整叠搬回进纸盒、从最上面取纸 → 抽纸顺序 = 第 1 趟顺序的逆序（reverse_second 时）；
    # reverse_second=False（操作员把整叠翻过面放回）→ 抽纸顺序与第 1 趟同序
    for pos, slot in enumerate(gp.second_pass):
        target = (len(order) - 1 - pos) if stack.reverse_second else pos
        phys.setdefault(target, {})["second"] = slot.page
    return phys, ref


bad_pair = []
for trial in range(120):
    random.seed(1000 + trial)
    n_entries = random.randint(1, 4)
    stack = ds.DuplexStack(capacity=9999)
    for i in range(n_entries):
        n_files = random.randint(1, 3)
        specs = [(f"E{i}F{f}", list(range(1, random.randint(1, 7) + 1)),
                  random.random() < 0.7) for f in range(n_files)]
        stack.add(_mk_entry(i + 1, specs))
    for first in (md.FIRST_ODD, md.FIRST_EVEN):
        for rev in (True, False):
            stack.first, stack.reverse_second = first, rev
            phys, ref = simulate_stack(stack)
            for gi, (sa, sb, label) in ref.items():
                p = phys.get(gi, {})
                got_first = p.get("first")
                got_second = p.get("second")
                # 页码在同一份文档里是全局唯一的，可直接比 pair
                if _key(got_first, got_second) != _key(sa, sb):
                    bad_pair.append((trial, first, rev, gi, label, got_first, got_second, sa, sb))
                    break
            if bad_pair:
                break
        if bad_pair:
            break
    if bad_pair:
        break
check("物理模拟（整叠堆纸→搬回→逆序抽纸）每张纸两面 == 各单配对（120 组 × 4 模式）",
      not bad_pair, f"{bad_pair[:1]}")

# 单面文件在攒批里也占独立一张纸、背面留空（不与其他文件配对）
st1 = ds.DuplexStack(capacity=99)
st1.add(_mk_entry(1, [("s", [1, 2, 3], False)]))
st1.add(_mk_entry(2, [("d", [1, 2], True)]))
gp = st1.build_plan()
check("单面 3 页占 3 张纸 + 双面 2 页占 1 张纸 = 整叠 4 张",
      gp.sheet_count == 4 and st1.total_sheets == 4, f"sheets={gp.sheet_count}")
check("单面文件的第 2 趟槽位是空白占位（不走墨但要走纸、保证不串页）",
      sum(1 for s in gp.second_pass if s.page is None) == 3,
      [s.page for s in gp.second_pass])
check("整叠页序里单面文件的每张纸背面都是空白（不与其它文件配对）",
      [sh.side_b for sh in gp.sheets] == [None, None, None, 5],
      [ (sh.label, sh.side_a, sh.side_b) for sh in gp.sheets])


# ============================================================
# ⑤ 清单持久化：往返、容错、批次目录管理
# ============================================================
print("\n=== ⑤ 清单持久化与批次目录 ===")
sandbox = tempfile.mkdtemp(prefix="hn_stack_verify_")
try:
    stx = ds.DuplexStack(capacity=42, warn_after_sec=600, printer_name="HP LaserJet 1020")
    stx.new_batch("batch_test_1")
    stx.add(_mk_entry(1, [("A", [1, 2, 3], True)]))
    stx.add(_mk_entry(2, [("B", [1], False)]))
    stx.taint("测试标记")
    bdir = ds.batch_dir(sandbox, stx.batch_id)
    check("save_manifest 成功", ds.save_manifest(bdir, stx))
    back = ds.load_manifest(bdir)
    check("load_manifest 往返成功", back is not None and back.count == 2)
    check("往返后容量/预警/旋转/污染标记保留",
          back.capacity == 42 and back.warn_after_sec == 600 and back.rotate is True
          and back.tainted and back.taint_reason == "测试标记",
          f"{back.capacity}/{back.warn_after_sec}/{back.rotate}/{back.tainted}")
    check("往返后页序仍一致（清单是续打的唯一依据）",
          [(s.label, s.page) for s in back.build_plan().second_pass]
          == [(s.label, s.page) for s in stx.build_plan().second_pass])
    check("往返后单内文件计划完整（label/pages/duplex）",
          back.entries[0].file_plans == stx.entries[0].file_plans,
          f"{back.entries[0].file_plans}")

    # 坏 JSON / 空目录 / 无 entries 的清单
    bad_dir = os.path.join(sandbox, "batch_broken")
    os.makedirs(bad_dir, exist_ok=True)
    with open(os.path.join(bad_dir, ds.MANIFEST_NAME), "w", encoding="utf-8") as f:
        f.write("{不是 JSON")
    check("坏清单返回 None（不抛异常）", ds.load_manifest(bad_dir) is None)
    empty_dir = os.path.join(sandbox, "batch_empty")
    os.makedirs(empty_dir, exist_ok=True)
    check("没有清单的目录返回 None", ds.load_manifest(empty_dir) is None)
    with open(os.path.join(empty_dir, ds.MANIFEST_NAME), "w", encoding="utf-8") as f:
        f.write('{"entries": []}')
    check("清单里没有单 → 视为无遗留（None）", ds.load_manifest(empty_dir) is None)
    check("find_pending_batch 只认有单的批次", ds.find_pending_batch(sandbox) is not None
          and ds.find_pending_batch(sandbox)[0] == bdir)

    # 坏字段容错：rotate 非 bool → 回落"取第一单的 rotate"；reverse_second 是**危险默认值**
    # （坏成 False 会让整叠第 2 趟整体反打）→ 必须回落 True 而不是 bool(None)=False
    st_bad = ds.DuplexStack.from_dict({
        "rotate": "yes", "first": "sideways", "reverse_second": None,
        "entries": [{"seq": "x", "file_plans": [{"label": "A", "pages": ["1", "坏"]},
                                                {"pages": []}],
                     "pass1_pages": ["2", None], "tasks": "不是列表"}]})
    check("坏字段容错：rotate 回落第一单的 rotate、first 回落 odd、"
          "reverse_second 回落 True（危险默认值不靠 bool(None)）",
          st_bad.rotate is True and st_bad.first == md.FIRST_ODD
          and st_bad.reverse_second is True,
          f"{st_bad.rotate}/{st_bad.first}/{st_bad.reverse_second}")
    st_no_entry = ds.DuplexStack.from_dict({"rotate": "yes", "reverse_second": "x"})
    check("没有单时 rotate 为 None（未定方向）",
          st_no_entry.rotate is None and st_no_entry.reverse_second is True)

    # StackEntry.rotate 的三态容错（D5：这里原先写的是 bool(d.get("rotate", True))）：
    # 清单被写坏成 null / "" 时 bool() 给 False，而 rotate 的**危险默认值是 True**
    # ——False 意味着整叠第 2 趟不做 180° 补偿，每张纸背面整体倒置，且**静默无提示**。
    # 同文件的 _bool_or 正是为这类三态布尔写的（reverse_second 已用它），此处必须一致。
    _e_null = ds.StackEntry.from_dict({"rotate": None})
    _e_empty = ds.StackEntry.from_dict({"rotate": ""})
    _e_str = ds.StackEntry.from_dict({"rotate": "no"})
    _e_miss = ds.StackEntry.from_dict({})
    _e_zero = ds.StackEntry.from_dict({"rotate": 0})
    _e_one = ds.StackEntry.from_dict({"rotate": 1})
    _e_false = ds.StackEntry.from_dict({"rotate": False})
    check("StackEntry.rotate 三态容错：null/空串/非 0-1 回落 True（危险默认值），"
          "bool 与 0/1 照常认",
          _e_null.rotate is True and _e_empty.rotate is True and _e_str.rotate is True
          and _e_miss.rotate is True and _e_zero.rotate is False
          and _e_one.rotate is True and _e_false.rotate is False,
          "null=%s 空串=%s str=%s 缺=%s 0=%s 1=%s False=%s"
          % (_e_null.rotate, _e_empty.rotate, _e_str.rotate, _e_miss.rotate,
             _e_zero.rotate, _e_one.rotate, _e_false.rotate))

    # 端到端：坏 rotate 会把整叠方向也带偏（StackState.from_dict 取第一单的 rotate 定方向）
    _st_null = ds.DuplexStack.from_dict({
        "entries": [{"seq": 1, "rotate": None,
                     "file_plans": [{"label": "A", "pages": [1, 2]}]}]})
    check("清单里 rotate 被写坏成 null -> 整叠方向仍取 True（否则第 2 趟背面全倒置）",
          _st_null.rotate is True and _st_null.entries[0].rotate is True,
          "st.rotate=%r entry.rotate=%r" % (_st_null.rotate, _st_null.entries[0].rotate))
    check("坏页数的文件计划整条丢弃（宁可存疑也不留半截页序）",
          st_bad.entries[0].file_plans == [],
          f"{st_bad.entries[0].file_plans}")
    check("非列表的 tasks 容错成空", st_bad.entries[0].tasks == [])

    # 清单自洽校验：张数与文件计划算出来的不一致 → 标存疑（启动弹窗会要求人工核对）
    incons_dir = os.path.join(sandbox, "batch_inconsistent")
    ds.save_manifest(incons_dir, stx)
    _mp = os.path.join(incons_dir, ds.MANIFEST_NAME)
    import json as _json
    with open(_mp, "r", encoding="utf-8") as f:
        _d = _json.load(f)
    _d["entries"][0]["sheet_count"] = 99          # 手工改坏
    with open(_mp, "w", encoding="utf-8") as f:
        _json.dump(_d, f, ensure_ascii=False)
    _st_i = ds.load_manifest(incons_dir)
    check("清单自洽校验：张数不一致 → 整叠标存疑并给出原因",
          _st_i is not None and _st_i.tainted and "不一致" in _st_i.taint_reason,
          f"{getattr(_st_i, 'taint_reason', '')}")
    ds.discard_batch(incons_dir)

    # 批次目录清理
    other = os.path.join(sandbox, "batch_other")
    ds.save_manifest(other, stx)
    removed = ds.keep_only_batch(sandbox, bdir)
    check("keep_only_batch 清掉其它批次、保留指定批次",
          removed >= 1 and os.path.isdir(bdir) and not os.path.isdir(other), f"removed={removed}")
    check("discard_batch 删除批次目录", ds.discard_batch(bdir) and not os.path.isdir(bdir))
    check("discard_batch 对不存在的目录返回 True（幂等）", ds.discard_batch(bdir))

    # 容量/预警阈值夹取
    check("容量阈值夹取（0→默认、9999→上限）",
          ds.clamp_capacity(0) == ds.MIN_CAPACITY
          and ds.clamp_capacity(9999) == ds.MAX_CAPACITY
          and ds.clamp_capacity("坏") == ds.DEFAULT_CAPACITY,
          f"{ds.clamp_capacity(0)}/{ds.clamp_capacity(9999)}/{ds.clamp_capacity('坏')}")
    check("预警阈值夹取", ds.clamp_warn_after(1) == ds.MIN_WARN_AFTER_SEC
          and ds.clamp_warn_after(10 ** 9) == ds.MAX_WARN_AFTER_SEC
          and ds.clamp_warn_after(None) == ds.DEFAULT_WARN_AFTER_SEC)
    check("默认容量 90（LaserJet 1020 出纸规格 100 张留余量）", ds.DEFAULT_CAPACITY == 90)
finally:
    shutil.rmtree(sandbox, ignore_errors=True)


# ============================================================
# ⑥ 描述文本 / 状态
# ============================================================
print("\n=== ⑥ 描述与状态 ===")
sty = ds.DuplexStack(capacity=20)
sty.new_batch("b")
e = _mk_entry(1, [("A", [1, 2, 3, 4], True)])
sty.add(e)
e2 = _mk_entry(2, [("B", [1, 2], True)])
sty.add(e2)
e2.state = ds.STATE_FAILED
lines = sty.describe_lines()
check("describe_lines 逐单列出张数与失败标记",
      len(lines) == 2 and "2 张" in lines[0] and "失败" in lines[1], f"{lines}")
check("next_seq 递增", sty.next_seq() == 3)
check("find 按 seq 定位", sty.find(2) is e2 and sty.find(99) is None)
sty.clear()
check("clear 清空并重置旋转/污染/batch_id",
      sty.is_empty and sty.rotate is None and not sty.tainted and sty.batch_id == "")

# ============================================================
# ⑦ 撤走后第 2 趟 == 剩余各单拼接后整体逆序（逐组合、随机）
# ============================================================
print("\n=== ⑦ 撤走后第 2 趟等价性（随机逐组合 × 四模式）===")


def _mk_entry_copies(seq, specs):
    """specs = [(label, pages, duplex, copies)]。

    与 gui._print_stack_pass1 一致：**每「份」在 file_plans 里独立一条**（撤走一"份"
    就是撤走对应那一条），页码按累计页数偏移。
    """
    plans, off = [], 0
    for label, pages, dup, copies in specs:
        n = max(pages)
        for c in range(max(1, copies)):
            base = off + c * n
            plans.append({"label": (label if copies <= 1 else f"{label}#{c + 1}"),
                          "pages": [base + p for p in pages], "duplex": dup})
        off += n * max(1, copies)
    sheets = ds.sheet_count_of_plans(plans)
    # 与 gui._print_stack_pass1 同一姿势：入叠这一刻把"原始张数"定格
    # （gui.py:8602 `entry.original_sheet_count = payload["sheet_count"]`）——
    # 之后 toggle_taken 会把 sheet_count 收缩成"当前在叠张数"。
    return ds.StackEntry(seq=seq, title=f"#{seq}", page_count=off, rotate=True,
                         sheet_count=sheets, original_sheet_count=sheets,
                         file_plans=plans)


def _ref_plan_blank_second(fp, first=md.FIRST_ODD):
    """独立判定「该文件计划第 2 趟全为空白占位」（不碰 StackEntry.plan_blank_second）。"""
    sheets = md.build_sheets([md.FilePlan(fp["label"], list(fp["pages"]), fp["duplex"])])
    tails = [s.side_b if first == md.FIRST_ODD else s.side_a for s in sheets]
    return bool(tails) and all(t is None for t in tails)


def _ref_active_layout(entries, taken_by_seq, first, rev):
    """独立参考实现（**不调 build_plan / file_plans / active_plans / toggle_taken**）：

      ① 逐单滤掉已撤走的文件计划；
      ② 各单按全局偏移重排页码（偏移 = 前序各单 page_count 之和；撤走不改变它 ——
         合并 PDF 在各单撤走之前生成，跳过偏移会让后面各单打到别人的纸上）；
      ③ 逐文件用 manual_duplex.build_sheets 配对成纸（跨单绝不配对）；
      ④ 各单纸张顺序拼接，第 2 趟取每张纸的"后一面"；⑤ rev=True 时整体逆序。

    返回 (second_seq, sheet_seq)；second_seq = [(label, page 或 None)]。
    """
    second, sheet_seq = [], []
    off = 0
    for e in entries:
        taken = set(taken_by_seq.get(int(e.seq), ()))
        fps = []
        for i, fp in enumerate(e.file_plans or []):
            if i in taken or not isinstance(fp, dict):
                continue
            pages = [off + int(p) for p in (fp.get("pages") or [])]
            if not pages:
                continue
            fps.append(md.FilePlan(str(fp.get("label", "") or ""), pages,
                                   bool(fp.get("duplex", True))))
        for sh in md.build_sheets(fps, 1):
            sheet_seq.append((sh.label, sh.side_a, sh.side_b))
            second.append((sh.label, sh.side_b if first == md.FIRST_ODD else sh.side_a))
        off += max(0, int(e.page_count or 0))
    if rev:
        second.reverse()
    return second, sheet_seq


def _ref_cross_pair(entries, taken_by_seq, gp):
    """返回第一张"两面分属不同文件/单"的纸（正常应为 None）——钉死跨单绝不配对。"""
    page_label = {}
    off = 0
    for e in entries:
        taken = set(taken_by_seq.get(int(e.seq), ()))
        for i, fp in enumerate(e.file_plans or []):
            if i in taken:
                continue
            for p in (fp.get("pages") or []):
                page_label[off + int(p)] = str(fp.get("label", "") or "")
        off += max(0, int(e.page_count or 0))
    for sh in gp.sheets:
        if sh.side_a is None or sh.side_b is None:
            continue
        if page_label.get(sh.side_a) != page_label.get(sh.side_b):
            return (sh.label, sh.side_a, sh.side_b)
    return None


random.seed(20261018)
check("sheet_count_of_plans 与 manual_duplex 同源（单面 3 页=3 张 / 双面 3 页=2 张）",
      ds.sheet_count_of_plans([{"label": "a", "pages": [1, 2, 3], "duplex": False}]) == 3
      and ds.sheet_count_of_plans([{"label": "b", "pages": [1, 2, 3], "duplex": True}]) == 2,
      f"{ds.sheet_count_of_plans([{'label': 'a', 'pages': [1, 2, 3], 'duplex': False}])}")
bad7, cross7 = [], []
for trial in range(160):
    stack = ds.DuplexStack(capacity=9999)
    for i in range(random.randint(1, 5)):
        specs = [(f"E{i}F{f}", list(range(1, random.randint(1, 7) + 1)),
                  random.random() < 0.7, random.choice([1, 1, 1, 2]))
                 for f in range(random.randint(1, 3))]
        stack.add(_mk_entry_copies(i + 1, specs))

    # 随机撤走一批「第 2 趟无内容」的文件计划（含"全撤光"这一组合）
    taken_by_seq = {}
    for e in stack.entries:
        cand = [i for i, fp in enumerate(e.file_plans) if _ref_plan_blank_second(fp)]
        take = list(cand) if (cand and random.random() < 0.3) \
            else [i for i in cand if random.random() < 0.5]
        for i in take:
            ok_t, why = e.toggle_taken(i, True)
            if not ok_t:
                bad7.append(("toggle 拒绝了应可撤走的计划", trial, e.seq, i, why))
            else:
                taken_by_seq.setdefault(int(e.seq), set()).add(i)
    if bad7:
        break

    for first in (md.FIRST_ODD, md.FIRST_EVEN):
        for rev in (True, False):
            stack.first, stack.reverse_second = first, rev
            gp = stack.build_plan()
            got_second = [(s.label, s.page) for s in gp.second_pass]
            got_sheets = [(s.label, s.side_a, s.side_b) for s in gp.sheets]
            ref_second, ref_sheets = _ref_active_layout(
                stack.entries, taken_by_seq, first, rev)
            if got_second != ref_second:
                bad7.append(("第 2 趟序列不等", trial, first, rev,
                             got_second[:8], ref_second[:8]))
            elif got_sheets != ref_sheets:
                bad7.append(("纸张配对不等", trial, first, rev,
                             got_sheets[:8], ref_sheets[:8]))
            elif stack.present_sheet_count != len(gp.second_pass):
                bad7.append(("present_sheet_count 不符", trial, first, rev,
                             stack.present_sheet_count, len(gp.second_pass)))
            c = _ref_cross_pair(stack.entries, taken_by_seq, gp)
            if c is not None and not cross7:
                cross7.append((trial, first, rev, c))
        if bad7:
            break
    if bad7:
        break

check("撤走后第 2 趟 == 剩余各单按全局偏移重排后整体逆序"
      "（160 组随机构造 × odd/even × 逆序/正序 四模式）",
      not bad7, f"{bad7[:1]}")
check("在叠每张纸的两面配对 == manual_duplex 逐文件配对（跨单/跨文件绝不配对）",
      not cross7 and not bad7, f"{cross7[:1]}")

st7z = ds.DuplexStack(capacity=999)
st7z.add(_mk_entry_copies(1, [("A", [1, 2, 3], False, 1), ("B", [1], True, 1)]))
st7z.add(_mk_entry_copies(2, [("C", [1, 2], False, 2)]))
all_ok7 = all(e.toggle_taken(i, True)[0]
              for e in st7z.entries for i in range(len(e.file_plans)))
gpz7 = st7z.build_plan()
check("全部撤光：build_plan().sheet_count == 0、present_sheet_count == 0、active_entries 空",
      all_ok7 and gpz7.sheet_count == 0 and len(gpz7.second_pass) == 0
      and st7z.present_sheet_count == 0 and st7z.total_sheets == 0
      and st7z.active_entries() == [],
      f"ok={all_ok7} sheets={gpz7.sheet_count} present={st7z.present_sheet_count}")


# ============================================================
# ⑧ 撤销的边界
# ============================================================
print("\n=== ⑧ 撤销边界 ===")

e8 = _mk_entry_copies(1, [("S", [1, 2, 3], False, 1),      # 0：单面 3 张，第 2 趟全空白 → 可撤
                          ("D", [1, 2, 3, 4], True, 1),    # 1：双面 2 张，第 2 趟有内容 → 不可撤
                          ("C", [1], True, 1)])            # 2：1 页（末张空白槽）→ 可撤
init_second = [(s.label, s.page) for s in e8.second_plan().second_pass]
init_sheets = e8.sheet_count
init_blanks = [e8.plan_blank_second(i) for i in range(3)]
check("初始：3 个计划共 6 张、plan_blank_second = [True, False, True]",
      init_sheets == 6 and init_blanks == [True, False, True],
      f"sheets={init_sheets} blank={init_blanks}")

ok_oob, why_oob = e8.toggle_taken(99, True)
ok_neg, _why_neg = e8.toggle_taken(-1, True)
ok_oob_rel, _why_rel = e8.toggle_taken(99, False)
check("越界下标（99 / -1，撤走与放回）被拒且状态不变",
      (not ok_oob) and (not ok_neg) and (not ok_oob_rel) and bool(why_oob)
      and e8.taken_plans == [] and e8.sheet_count == init_sheets,
      f"ok=({ok_oob},{ok_neg},{ok_oob_rel}) why={why_oob!r}")

ok_bad, why_bad = e8.toggle_taken(1, True)
check("『第 2 趟还有内容』的计划撤走被拒、状态不变",
      (not ok_bad) and bool(why_bad) and e8.taken_plans == [] and e8.sheet_count == init_sheets,
      f"ok={ok_bad} why={why_bad!r}")

ok_t1, _ = e8.toggle_taken(0, True)
snap = (list(e8.taken_plans), e8.sheet_count)
ok_t2, _ = e8.toggle_taken(0, True)
check("同一项反复切换幂等（撤走两次状态不变）",
      ok_t1 and ok_t2 and (list(e8.taken_plans), e8.sheet_count) == snap == ([0], 3),
      f"{snap}")
check("撤走 plan0 后 second_plan 只剩其余计划（3 张）、原张数仍记着 6",
      e8.plan_sheet_count() == 3 and e8.second_plan().sheet_count == 3
      and e8.original_sheet_value() == 6,
      f"{e8.plan_sheet_count()}/{e8.second_plan().sheet_count}/{e8.original_sheet_value()}")

ok_r1, _ = e8.toggle_taken(0, False)
ok_r2, _ = e8.toggle_taken(0, False)
restored = [(s.label, s.page) for s in e8.second_plan().second_pass]
check("撤走再放回：页序与张数完全恢复到初始",
      ok_r1 and ok_r2 and e8.taken_plans == [] and e8.sheet_count == init_sheets
      and restored == init_second and e8.plan_sheet_count() == init_sheets,
      f"{e8.taken_plans}/{e8.sheet_count}")

sty8 = ds.DuplexStack(capacity=99)
sty8.add(_mk_entry_copies(1, [("S", [1, 2, 3], False, 1), ("D", [1, 2, 3, 4], True, 1)]))
rows = sty8.entries[0].ui_lines()
keys_ok = all({"index", "label", "pages", "sheets", "second", "blank_second", "taken"} <= set(r)
              for r in rows)
check("ui_lines：逐计划一行、字段齐全、blank_second/sheets 与接口同源",
      len(rows) == 2 and keys_ok
      and rows[0]["blank_second"] is True and rows[0]["sheets"] == 3
      and rows[0]["taken"] is False and rows[1]["blank_second"] is False
      and rows[1]["sheets"] == 2,
      f"{rows}")
check("removable_plans 只列『第 2 趟无内容且未撤走』的计划",
      sty8.removable_plans() == [(1, 0)], f"{sty8.removable_plans()}")
ok8, why8 = sty8.toggle_taken(1, 0, True)
check("DuplexStack.toggle_taken(seq, plan, True) 生效；该项从 removable_plans 消失、在叠张数更新",
      ok8 and sty8.removable_plans() == [] and sty8.present_sheet_count == 2,
      f"ok={ok8} {why8!r} {sty8.removable_plans()} {sty8.present_sheet_count}")
check("DuplexStack.toggle_taken 越界 / 无此单 / 内容计划被拒",
      (not sty8.toggle_taken(1, 99, True)[0]) and (not sty8.toggle_taken(99, 0, True)[0])
      and (not sty8.toggle_taken(1, 1, True)[0]), "")
sty8.toggle_taken(1, 0, False)
check("放回后 removable_plans 复原",
      sty8.removable_plans() == [(1, 0)] and sty8.present_sheet_count == 5)

st_act = ds.DuplexStack(capacity=99)
st_act.add(_mk_entry_copies(1, [("A", [1], True, 1)]))            # 1 张，可撤
st_act.add(_mk_entry_copies(2, [("B", [1, 2, 3, 4], True, 1)]))   # 2 张，不可撤
st_act.toggle_taken(1, 0, True)
check("active_entries 排除已无可打计划的单（但仍留在 entries 里记账）",
      [e.seq for e in st_act.active_entries()] == [2] and st_act.count == 2,
      f"{[e.seq for e in st_act.active_entries()]}")

stz = ds.DuplexStack(capacity=99)
stz.add(_mk_entry_copies(1, [("A", [1, 2], False, 1)]))
stz.add(_mk_entry_copies(2, [("B", [1], True, 1)]))
all_ok8 = all(e.toggle_taken(i, True)[0]
              for e in stz.entries for i in range(len(e.file_plans)))
gpz8 = stz.build_plan()
check("全部撤光：build_plan().sheet_count == 0 且 present_sheet_count == 0",
      all_ok8 and gpz8.sheet_count == 0 and stz.present_sheet_count == 0,
      f"ok={all_ok8} sheets={gpz8.sheet_count} present={stz.present_sheet_count}")


# ============================================================
# ⑨ 装订方向/容量与撤走的交互
# ============================================================
print("\n=== ⑨ 装订方向/容量与撤走的交互 ===")

st9 = ds.DuplexStack(capacity=10)
st9.add(_mk_entry_copies(1, [("S", [1, 2, 3, 4, 5, 6], False, 1)]))    # 单面 6 张
check("初始已堆 6 张、剩余 4", st9.total_sheets == 6 and st9.remaining == 4)
adm_no = st9.admit(6, True)
check("撤走前：再加 6 张超容量 → 拒绝（原因=容量）",
      (not adm_no.ok) and adm_no.reason == ds.REASON_CAPACITY, adm_no.reason)
ok9, why9 = st9.toggle_taken(1, 0, True)
check("单面 6 张整份撤走成功", ok9, why9)
check("撤走后 total_sheets == present_sheet_count == 0（容量随撤走释放）",
      st9.total_sheets == 0 and st9.present_sheet_count == 0 and st9.remaining == 10,
      f"{st9.total_sheets}/{st9.present_sheet_count}/{st9.remaining}")
adm_ok = st9.admit(6, True)
check("撤走释放容量 → 原本超容量的单现在能进", adm_ok.ok, f"{adm_ok.reason}")

st_dir = ds.DuplexStack(capacity=30)
st_dir.add(_mk_entry_copies(1, [("S", [1, 2, 3], False, 1)]))          # 3 张，rotate=True
st_dir.add(_mk_entry_copies(2, [("D", [1, 2, 3, 4, 5], True, 1)]))     # 3 张
check("两单共 6 张且 total_sheets == present_sheet_count",
      st_dir.total_sheets == 6 == st_dir.present_sheet_count)
st_dir.toggle_taken(1, 0, True)                                        # 撤走单面 3 张
check("撤走一单后 total_sheets == present_sheet_count == 3",
      st_dir.total_sheets == 3 == st_dir.present_sheet_count,
      f"{st_dir.total_sheets}/{st_dir.present_sheet_count}")
check("装订方向仍锁定（撤走不改变本叠方向）→ 反向单仍被拒",
      st_dir.rotate is True and st_dir.admit(1, False).reason == ds.REASON_ROTATE)
check("容量文案里的『已堆』用**在叠**张数（不是原始张数）",
      "已堆 3 张" in st_dir.admit(40, True).message(st_dir),
      st_dir.admit(40, True).message(st_dir))


# ============================================================
# ⑩ 清单 v2 迁移与容错
# ============================================================
print("\n=== ⑩ 清单 v2 迁移与容错 ===")
sandbox10 = tempfile.mkdtemp(prefix="hn_stack_v2_")


def _write_manifest(bdir, data):
    os.makedirs(bdir, exist_ok=True)
    with open(os.path.join(bdir, ds.MANIFEST_NAME), "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)


def _load10(bdir):
    try:
        return ds.load_manifest(bdir), ""
    except Exception as exc:        # 清单解析绝不能抛（抛了就是直接判红）
        return None, f"{type(exc).__name__}: {exc}"


def _valid_taken(raw, n_plans):
    """契约口径的合法下标：非负 int（bool 不算）、越界丢弃、去重。"""
    if not isinstance(raw, (list, tuple)):
        return set()
    return {x for x in raw
            if isinstance(x, int) and not isinstance(x, bool) and 0 <= x < n_plans}


try:
    check("清单版本 BATCH_VERSION == 2", ds.BATCH_VERSION == 2, f"{ds.BATCH_VERSION}")
    base10 = ds.DuplexStack(capacity=50, warn_after_sec=900)
    base10.new_batch("batch_v2")
    base10.add(_mk_entry_copies(1, [("S1", [1, 2, 3], False, 1),
                                    ("S2", [1, 2, 3], False, 1)]))   # 6 张
    base10.add(_mk_entry_copies(2, [("D", [1, 2, 3, 4], True, 1)]))   # 2 张
    base_second = [(s.label, s.page) for s in base10.build_plan().second_pass]

    # ── v1 迁移（零回归）──
    d1 = base10.to_dict()
    d1["version"] = 1
    for ent in d1["entries"]:
        ent.pop("taken_plans", None)
        ent.pop("original_sheet_count", None)
    b1 = os.path.join(sandbox10, "v1")
    _write_manifest(b1, d1)
    st1m, err1 = _load10(b1)
    check("v1 清单（无 taken_plans/original_sheet_count）→ 0 异常、全部在叠、张数不变",
          st1m is not None and not err1 and not st1m.tainted
          and st1m.present_sheet_count == base10.present_sheet_count == 8
          and all(e.taken_plans == [] for e in st1m.entries)
          and [(s.label, s.page) for s in st1m.build_plan().second_pass] == base_second,
          f"{err1!r} present={getattr(st1m, 'present_sheet_count', None)}")
    check("v1：缺 original_sheet_count → 回落 sheet_count（原始张数不丢）",
          all(e.original_sheet_value() == e.sheet_count for e in st1m.entries)
          and st1m.original_sheets_count == 8,
          f"{st1m.original_sheets_count}")

    # ── v2 往返（带 taken_plans）──
    t10 = ds.DuplexStack(capacity=50)
    t10.new_batch("batch_v2b")
    t10.add(_mk_entry_copies(1, [("S1", [1, 2, 3], False, 1),
                                 ("S2", [1, 2, 3], False, 1)]))
    t10.add(_mk_entry_copies(2, [("D", [1, 2, 3, 4], True, 1)]))
    ok10, why10 = t10.toggle_taken(1, 0, True)      # 撤走 S1 → 在叠 3+2 = 5
    b2 = os.path.join(sandbox10, "v2")
    ds.save_manifest(b2, t10)
    st2m, err2 = _load10(b2)
    check("v2 带 taken_plans 往返一致（taken / 在叠张数 / 原始张数 / 页序）",
          st2m is not None and not err2 and not st2m.tainted
          and st2m.entries[0].taken_plans == [0]
          and st2m.present_sheet_count == t10.present_sheet_count == 5
          and st2m.original_sheets_count == 8
          and [(s.label, s.page) for s in st2m.build_plan().second_pass]
              == [(s.label, s.page) for s in t10.build_plan().second_pass],
          f"ok={ok10} {why10!r} {err2!r} "
          f"taken={getattr(st2m.entries[0], 'taken_plans', None) if st2m else None}")

    # ── 坏值容错 ──
    def _case(name, mutate):
        d = base10.to_dict()
        mutate(d["entries"][0])
        bdir = os.path.join(sandbox10, "case_" + name)
        _write_manifest(bdir, d)
        st, err = _load10(bdir)
        eff = _valid_taken(d["entries"][0].get("taken_plans"),
                           len(base10.entries[0].file_plans))
        want = _ref_active_layout(base10.entries, {1: eff}, md.FIRST_ODD, True)[0]
        got = [(s.label, s.page) for s in st.build_plan().second_pass] if st else None
        return st, err, got, want

    stA, errA, gotA, wantA = _case(
        "taken_not_list", lambda e: e.__setitem__("taken_plans", {"x": 1}))
    check("坏值容错：taken_plans 不是列表 → 0 异常、视为全部在叠、页序不变",
          stA is not None and not errA and gotA == wantA
          and stA.present_sheet_count == 8,
          f"{errA!r}")

    stB, errB, gotB, wantB = _case(
        "taken_oob", lambda e: e.__setitem__("taken_plans", [0, 99]))
    check("坏值容错：越界下标 → 0 异常、页序按合法下标走、整叠标存疑（理由写清）",
          stB is not None and not errB and gotB == wantB and stB.tainted
          and bool(stB.taint_reason) and stB.present_sheet_count == 5,
          f"{errB!r} reason={getattr(stB, 'taint_reason', '')!r}")
    check("坏值容错：越界下标被丢弃（不作为『已取走』生效）",
          stB is not None and stB.entries[0].taken_plans == [0]
          and not stB.entries[0].is_plan_taken(99)
          and stB.entries[0].taken_count() == 1,
          f"taken={getattr(stB.entries[0], 'taken_plans', None)}")

    stC, errC, gotC, wantC = _case(
        "taken_noint", lambda e: e.__setitem__("taken_plans", [0, "x", 1.5, True, None]))
    check("坏值容错：非整数下标 → 被丢弃、0 异常、页序正确、整叠标存疑",
          stC is not None and not errC and gotC == wantC and stC.tainted
          and bool(stC.taint_reason) and stC.entries[0].taken_plans == [0],
          f"{errC!r} taken={getattr(stC.entries[0], 'taken_plans', None)}")

    stD, errD, gotD, wantD = _case(
        "taken_dup", lambda e: e.__setitem__("taken_plans", [0, 0, 0]))
    check("坏值容错：重复下标 → 0 异常、页序与在叠张数正确（集合语义）",
          stD is not None and not errD and gotD == wantD
          and stD.present_sheet_count == 5,
          f"{errD!r} taken={getattr(stD.entries[0], 'taken_plans', None)}")

    stE, errE, _gotE, _wantE = _case(
        "orig_neg", lambda e: e.__setitem__("original_sheet_count", -5))
    check("坏值容错：original_sheet_count 为负 → 0 异常、原始张数回落 sheet_count",
          stE is not None and not errE
          and stE.entries[0].original_sheet_value() == stE.entries[0].sheet_count == 6,
          f"{errE!r}")

    stF, errF, _gotF, _wantF = _case(
        "orig_str", lambda e: e.__setitem__("original_sheet_count", "abc"))
    check("坏值容错：original_sheet_count 非整数 → 0 异常、回落 sheet_count",
          stF is not None and not errF
          and stF.entries[0].original_sheet_value() == 6, f"{errF!r}")

    # ── 自洽校验按「在叠张数」比对 ──
    d_ic = t10.to_dict()                       # entry0: taken=[0] → 在叠 3；原始 6
    d_ic["entries"][0]["sheet_count"] = 6      # 谎报成"全部在叠"
    b_ic = os.path.join(sandbox10, "inconsistent")
    _write_manifest(b_ic, d_ic)
    st_ic, err_ic = _load10(b_ic)
    check("自洽校验按『在叠张数』比对：taken_plans 与张数不符 → 必须 tainted",
          st_ic is not None and not err_ic and st_ic.tainted and bool(st_ic.taint_reason)
          and st_ic.present_sheet_count == 5,
          f"{err_ic!r} tainted={getattr(st_ic, 'tainted', None)} "
          f"reason={getattr(st_ic, 'taint_reason', '')!r}")

    d_ok = t10.to_dict()
    b_ok = os.path.join(sandbox10, "consistent")
    _write_manifest(b_ok, d_ok)
    st_ok, err_ok = _load10(b_ok)
    check("对照：同一份清单张数正确 → 不标存疑（别把好清单也判存疑）",
          st_ok is not None and not err_ok and not st_ok.tainted,
          f"{getattr(st_ok, 'taint_reason', '')!r}")

    # 全部取走 = 没有待打的第 2 趟。契约（duplex_stack.load_manifest 文档）：
    # 照样返回清单（好让启动续打弹窗能提示"这叠被动过、要人工核对"，批次目录也不会
    # 静默烂在 %APPDATA% 里），由 present_sheet_count == 0 / 第 2 趟槽位数 == 0
    # 让调用方识别并走成品收尾。这里钉"可区分性"，不是一句 is not None。
    d_all = t10.to_dict()
    d_all["entries"][0]["taken_plans"] = [0, 1, 99, "x"]   # 合法项 + 越界 + 非整数
    d_all["entries"][0]["sheet_count"] = 0
    d_all["entries"][1]["taken_plans"] = [0]
    d_all["entries"][1]["sheet_count"] = 0
    all_root = os.path.join(sandbox10, "all_root")
    b_all = os.path.join(all_root, "batch_all")
    _write_manifest(b_all, d_all)
    st_all, err_all = _load10(b_all)
    found = ds.find_pending_batch(all_root)
    check("全部取走（在叠 0 张）→ 清单照读且可区分：present==0、第 2 趟为空、"
          "original>0、taken 已归一化、find_pending_batch 仍能发现它（可提示/收尾/清理）",
          st_all is not None and not err_all
          and st_all.present_sheet_count == 0 and st_all.build_plan().sheet_count == 0
          and st_all.total_sheets == 0 and st_all.original_sheets_count == 8
          and st_all.entries[0].taken_plans == [0, 1]
          and found is not None and found[0] == b_all
          and found[1].present_sheet_count == 0,
          f"{err_all!r} present={getattr(st_all, 'present_sheet_count', None)} "
          f"orig={getattr(st_all, 'original_sheets_count', None)} "
          f"taken={getattr(st_all.entries[0], 'taken_plans', None) if st_all else None} "
          f"found={found[0] if found else None}")
finally:
    shutil.rmtree(sandbox10, ignore_errors=True)


# ============================================================
# ⑪ 变异自证：把修复退掉，两条断言必须转红
# ============================================================
print("\n=== ⑪ 变异自证 ===")

_CHILD = r'''
import os, sys
tmp, repo = sys.argv[1], sys.argv[2]
sys.path.insert(0, os.path.join(repo, "local_print_tool"))
sys.path.insert(0, tmp)
import manual_duplex as md
import duplex_stack as ds


def mk(seq, specs):
    plans, off = [], 0
    for label, pages, dup in specs:
        plans.append({"label": label, "pages": [off + p for p in pages], "duplex": dup})
        off += max(pages)
    return ds.StackEntry(seq=seq, page_count=off, rotate=True,
                         sheet_count=ds.sheet_count_of_plans(plans), file_plans=plans)


def ref(entries, taken, first, rev):
    second, off = [], 0
    for e in entries:
        fps = []
        t = set(taken.get(int(e.seq), ()))
        for i, fp in enumerate(e.file_plans):
            if i in t:
                continue
            fps.append(md.FilePlan(fp["label"], [off + p for p in fp["pages"]], fp["duplex"]))
        for s in md.build_sheets(fps, 1):
            second.append((s.label, s.side_b if first == md.FIRST_ODD else s.side_a))
        off += int(e.page_count)
    if rev:
        second.reverse()
    return second


# 断言 A（= ⑦ 的核心）：撤走"第 2 趟无内容"的计划后，第 2 趟只送在叠槽位
stA = ds.DuplexStack(capacity=9999)
stA.add(mk(1, [("S", [1, 2, 3], False)]))
stA.add(mk(2, [("D", [1, 2, 3], True)]))
okA, _whyA = stA.entries[0].toggle_taken(0, True)
gotA = [(s.label, s.page) for s in stA.build_plan().second_pass]
wantA = ref(stA.entries, {1: {0}}, md.FIRST_ODD, True)
A = bool(okA) and gotA == wantA and stA.present_sheet_count == len(gotA)

# 断言 B（= ⑧ 的核心）：第 2 趟还有内容的计划撤走必须被拒
stB = ds.DuplexStack(capacity=9999)
stB.add(mk(1, [("D", [1, 2, 3, 4], True)]))
okB, _whyB = stB.entries[0].toggle_taken(0, True)
B = (okB is False)

sys.stdout.write("MUT_RESULT A=%d B=%d\n" % (1 if A else 0, 1 if B else 0))
'''

_MUT_A = r'''
# ── 变异 A（仅临时副本）：把 build_plan 退回"不过滤 taken_plans" ──
def _mut_file_plans(self):
    plans, off = [], 0
    for e in self.entries:
        for fp in (e.file_plans or []):
            pages = [off + int(p) for p in (fp.get("pages") or [])]
            if not pages:
                continue
            plans.append(manual_duplex.FilePlan(str(fp.get("label", "") or ""), pages,
                                                bool(fp.get("duplex", True))))
        off += max(0, int(e.page_count or 0))
    return plans


def _mut_build_plan(self):
    return manual_duplex.plan_passes(_mut_file_plans(self), copies=1, first=self.first,
                                     reverse_second=self.reverse_second)


DuplexStack.file_plans = _mut_file_plans
DuplexStack.build_plan = _mut_build_plan
'''

_MUT_B = r'''
# ── 变异 B（仅临时副本）：toggle_taken 放行"第 2 趟还有内容" ──
def _mut_toggle_taken(self, index, taken):
    fp, i = self._plan(index)
    if fp is None:
        return False, "越界"
    cur = {int(x) for x in (self.taken_plans or [])
           if isinstance(x, int) and not isinstance(x, bool)}
    if taken:
        cur.add(i)
    else:
        cur.discard(i)
    self.taken_plans = sorted(cur)
    self.sheet_count = self.plan_sheet_count()
    return True, ""


StackEntry.toggle_taken = _mut_toggle_taken
'''

mut_root = tempfile.mkdtemp(prefix="hn_stack_mut_")


def _run_child(child_path, mod_dir):
    env = dict(os.environ)
    env["PYTHONPATH"] = ""
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        proc = subprocess.run([sys.executable, child_path, mod_dir, REPO],
                              capture_output=True, text=True, timeout=120, env=env)
    except subprocess.TimeoutExpired:
        return None, "timeout"
    out = (proc.stdout or "") + (proc.stderr or "")
    m = re.search(r"MUT_RESULT A=(\d) B=(\d)", out)
    if not m:
        return None, out.strip()[-300:]
    return (int(m.group(1)), int(m.group(2))), ""


try:
    child_path = os.path.join(mut_root, "mut_child.py")
    with open(child_path, "w", encoding="utf-8") as fh:
        fh.write(_CHILD)
    with open(os.path.join(REPO, "local_print_tool", "duplex_stack.py"),
              "r", encoding="utf-8") as fh:
        raw_src = fh.read()

    variants = {}
    for name, extra in (("base", ""), ("mut_a", _MUT_A), ("mut_b", _MUT_B)):
        vdir = os.path.join(mut_root, name)
        os.makedirs(vdir, exist_ok=True)
        with open(os.path.join(vdir, "duplex_stack.py"), "w", encoding="utf-8") as fh:
            fh.write(raw_src + ("\n\n" + extra if extra else ""))
        variants[name] = vdir

    base_res, base_err = _run_child(child_path, variants["base"])
    a_res, a_err = _run_child(child_path, variants["mut_a"])
    b_res, b_err = _run_child(child_path, variants["mut_b"])

    check("变异自证 · 基线：未变异的源码副本上两条断言都绿（证明断言非恒红）",
          base_res == (1, 1), f"{base_res} {base_err}")
    check("变异自证 A：build_plan 退回『不过滤 taken_plans』→ 断言 A 必然转红",
          a_res is not None and a_res[0] == 0, f"{a_res} {a_err}")
    check("变异自证 B：toggle_taken 放行『第 2 趟有内容』→ 断言 B 必然转红",
          b_res is not None and b_res[1] == 0, f"{b_res} {b_err}")
finally:
    shutil.rmtree(mut_root, ignore_errors=True)


# ============================================================
# ⑫ 可撤走判据必须跟本单的 first 约定走（first="even" 下"单面文件的第 2 趟"是有内容的）
# ============================================================
# 背景（2026-10-18，独立验证者发现的 🟡）：旧 `plan_blank_second` 把 `plan_passes()` 的默认
# odd 写死。机型档案标了 `first="even"`（tools/print_duplex_calibration.py --first even 可写、
# gui 会把它喂进纸叠）时，"单面 3 页"的真实第 2 趟是 **[3, 2, 1]**（有内容！），
# 旧判据却报 blank_second=True、文案谎称"第 1 趟即成品"、并**放行撤走** ⇒ 静默少打顾客的内容。
# 修法：`StackEntry.first` 记住本单打印时的约定，第 2 趟页序列与判据都按它算。
print("\n=== ⑫ 可撤走判据与 first 约定同源 ===")
_e_odd = ds.StackEntry(seq=1, sheet_count=3, first=md.FIRST_ODD,
                       file_plans=[{"label": "S", "pages": [1, 2, 3], "duplex": False}])
_e_even = ds.StackEntry(seq=1, sheet_count=3, first=md.FIRST_EVEN,
                        file_plans=[{"label": "S", "pages": [1, 2, 3], "duplex": False}])
check("odd 约定：单面 3 页的第 2 趟全空白 ⇒ 可整份取走",
      _e_odd.plan_second_pages(0) == [None, None, None] and _e_odd.plan_blank_second(0)
      and _e_odd.toggle_taken(0, True)[0],
      f"{_e_odd.plan_second_pages(0)} / {_e_odd.plan_blank_second(0)}")
check("★ even 约定：同一份的第 2 趟是 [3, 2, 1]（**有内容**）⇒ 不许取走、判据必须为假",
      _e_even.plan_second_pages(0) == [3, 2, 1] and not _e_even.plan_blank_second(0),
      f"{_e_even.plan_second_pages(0)} / {_e_even.plan_blank_second(0)}")
_ok_even, _why_even = _e_even.toggle_taken(0, True)
check("★ even 约定：toggle_taken 必须拒绝，且**状态不变**（在叠张数仍 3）",
      (not _ok_even) and _e_even.taken_plans == [] and _e_even.present_sheet_value() == 3,
      f"ok={_ok_even} taken={_e_even.taken_plans} present={_e_even.present_sheet_value()}")
check("even 约定：拒绝文案要指向『第 2 趟还要打印』（不能谎称「第 1 趟即成品」）",
      "第 2 趟还要打印" in _why_even, _why_even)
check("first 要落盘并往返（否则重启续打会退回 odd，判据静默变回错的）",
      ds.StackEntry.from_dict(_e_even.to_dict()).first == md.FIRST_EVEN,
      ds.StackEntry.from_dict(_e_even.to_dict()).first)
check("first 坏值回落 odd（危险默认值：宁可判'有内容、不许撤'，也不能误放行）",
      ds.StackEntry.from_dict({"first": "sideways"}).first == md.FIRST_ODD
      and ds.StackEntry.from_dict({}).first == md.FIRST_ODD)

print("\n" + ("=" * 60))
print("全部通过 ✅" if ok_all else "存在失败项 ❌")
sys.exit(0 if ok_all else 1)
