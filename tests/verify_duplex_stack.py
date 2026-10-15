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
import os
import random
import shutil
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

print("\n" + ("=" * 60))
print("全部通过 ✅" if ok_all else "存在失败项 ❌")
sys.exit(0 if ok_all else 1)
