# -*- coding: utf-8 -*-
"""验收脚本 · 手动双面「半成品补救」台账（纯逻辑，不起 GUI、不碰打印机）

背景（2026-11-10 用户现场要求）：
    「跳过」以前是**直接判失败**：第 1 趟的纸留在出纸槽里，程序既不记住它、也不
    提供任何补打入口 —— 一单 20 页的文件从此只剩前十页，操作员只能手工重来，而
    云端订单已经被回报成 failed。这个台账就是那道补救：跳过/超时/第 2 趟失败时，
    把「第 1 趟已打出」这件事连同一份可补打的数据（合并 PDF + 页序计划 + 任务元数据）
    一起落到 %APPDATA%\\HN打印工具\\duplex_pending.json，之后可以：
        ▶ 补打第 2 趟（只打背面，纸张不用重来）
        🖨 整单重打（纸叠已经乱了/第 2 趟打了一半时用）
        ✖ 作废（按失败收尾，明确放弃）
    本脚本只验台账本身：落盘/读回/状态机/崩溃自愈/清理/文案。
    真正的打印路径由 tests/verify_manual_duplex_gui.py ②③ 验。

    python tests/verify_duplex_remedy.py     # exit 0 = 全绿
"""
import io
import json
import os
import sys
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LOCAL_TOOL = os.path.join(REPO, "local_print_tool")
SANDBOX = os.path.join(os.environ.get("TEMP", "."), f"hn_remedy_log_{os.getpid()}")
os.environ["APPDATA"] = SANDBOX          # 台账根目录 → 沙箱（绝不碰真实待补救记录）
os.makedirs(SANDBOX, exist_ok=True)
sys.path.insert(0, LOCAL_TOOL)

import duplex_remedy as R  # noqa: E402
import manual_duplex      # noqa: E402

_ok_all = True


def check(name, cond, detail=""):
    global _ok_all
    if not cond:
        _ok_all = False
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


def _fake_pdf(name="half.pdf", size=2000):
    path = os.path.join(SANDBOX, name)
    with open(path, "wb") as f:
        f.write(b"%PDF-1.4\n" + b"0" * size)
    return path


def _record(file_plans, items=None, pdf="", **kw):
    """按当前批次的真实页序造一条台账记录（页序仍由 manual_duplex 算，不手搓）。"""
    return R.make_record(
        stage=kw.pop("stage", R.STAGE_PASS1_DONE),
        reason=kw.pop("reason", "翻纸未确认/被跳过（第 1 趟已打出，第 2 趟未打）"),
        printer=kw.pop("printer", "Fake Print"),
        paper=kw.pop("paper", "A4"),
        sheet_count=kw.pop("sheet_count", 5),
        total_sides=kw.pop("total_sides", 10),
        file_plans=file_plans,
        items=items or [],
        pdf=pdf,
        **kw)


def _wipe():
    """每次从干净台账开始（同一个脚本里多段互不污染）。"""
    for rec in R.load_all():
        R.remove(rec["id"])


print(f"台账根目录：{R.manifest_path()}")
print(f"PDF 目录：  {R.store_root()}")

# ============================================================
# ① 造记录 → 落盘 → 读回
# ============================================================
print("\n=== ① 记录生成 / 落盘 / 读回 ===")
_wipe()
check("空台账：pending() 为空、pending_count() == 0",
      R.pending() == [] and R.pending_count() == 0)

plans = (manual_duplex.file_plans_for_pages("a.pdf", [1, 2, 3, 4, 5], True, [])
         + manual_duplex.file_plans_for_pages("b.pdf", [6, 7, 8], True, []))
pdf_src = _fake_pdf()
rec = _record(plans,
              items=[{"idx": 0, "tab_key": "t1", "task_id": 4321, "order_id": 99,
                      "order_number": "HN20261110-0099", "display_name": "论文.pdf",
                      "file_path": "D:/x/论文.pdf", "copies": 1, "duplex": "on",
                      "page_range": "", "page_count": 5, "owner_name": "张三",
                      "is_admin_print": False}],
              tray_foreign=3)
check("make_record 造出的记录 state=pending、张数/面数原样带出",
      rec["state"] == R.STATE_PENDING and rec["sheet_count"] == 5 and rec["total_sides"] == 10)
check("make_record 会把 FilePlan 转成可 JSON 的形状（second_rotate 允许 None）",
      len(rec["file_plans"]) == 2 and set(rec["file_plans"][0]) ==
      {"label", "pages", "duplex", "second_rotate"}, f"{rec['file_plans'][0]}")

pdf_keep = R.keep_pdf(pdf_src, R.make_id())
check("keep_pdf：把合并后的半成品复制进台账目录（原件不在台账目录里）",
      bool(pdf_keep) and os.path.isfile(pdf_keep)
      and os.path.dirname(pdf_keep) == R.store_root(),
      pdf_keep)

rec["pdf"] = pdf_keep
saved = R.add(rec)
check("add()：补出 id 并落盘，id 形如 20261110-132045-1234",
      bool(saved.get("id")) and len(saved["id"].split("-")) == 3, saved.get("id"))
check("读回：pending() 里只有刚写进去的那一条，重要字段一个不少",
      len(R.pending()) == 1
      and R.pending()[0]["id"] == saved["id"]
      and R.pending()[0]["printer"] == "Fake Print"
      and R.pending()[0]["tray_foreign"] == 3
      and R.pending()[0]["items"][0]["task_id"] == 4321
      and R.pending()[0]["items"][0]["order_number"] == "HN20261110-0099")
check("读回：pdf 指向的文件还在（pdf_available 为真）",
      R.pdf_available(R.pending()[0]))
check("get(rid) 只按 id 取，不会串到别的记录",
      (R.get(saved["id"]) or {}).get("id") == saved["id"] and R.get("nope") is None)

# ============================================================
# ② 写盘是原子的（tmp + replace），不留半截文件
# ============================================================
print("\n=== ② 原子写盘 ===")
check("清单文件存在且是合法 JSON（含 version / records）",
      os.path.isfile(R.manifest_path())
      and json.load(open(R.manifest_path(), encoding="utf-8"))["version"] == R.VERSION)
check("没有残留的 .tmp（写失败也不留半截文件）",
      not os.path.isfile(R.manifest_path() + ".tmp"))
_before = os.path.getsize(R.manifest_path())
R.update(saved["id"], reason="改一下原因")
check("update() 后文件仍然合法（不是就地改写覆盖一半）",
      os.path.getsize(R.manifest_path()) > 0
      and json.load(open(R.manifest_path(), encoding="utf-8"))["records"][0]["reason"] == "改一下原因",
      f"{_before} → {os.path.getsize(R.manifest_path())}")

# ============================================================
# ③ 页序重建：补打与第一次打印走**同一条** plan_passes
# ============================================================
print("\n=== ③ 页序重建（唯一真相仍是 manual_duplex）===")
ref = manual_duplex.plan_passes(plans, copies=1,
                                first=manual_duplex.FIRST_ODD,
                                reverse_second=True, second_rotate_180=True)
got = R.build_plan(R.pending()[0])
check("★ 第 1 趟页序与当场计算的完全一致",
      got.pass_pages(1) == ref.pass_pages(1), f"{got.pass_pages(1)} / {ref.pass_pages(1)}")
check("★ 第 2 趟页序（含空白占位）与当场计算的完全一致",
      got.pass_pages(2) == ref.pass_pages(2), f"{got.pass_pages(2)} / {ref.pass_pages(2)}")
check("★ 张数/面数重建后不变（补打时进度分母与当时同口径）",
      got.sheet_count == ref.sheet_count and got.total_sides == ref.total_sides,
      f"{got.sheet_count}/{got.total_sides} vs {ref.sheet_count}/{ref.total_sides}")
check("★ 逐面 180° 补偿表也重建得出来（补打第 2 趟仍要补偿）",
      got.pass_rotate(2) == ref.pass_rotate(2), f"{got.pass_rotate(2)}")

# rotate_mode=fixed：整批一个方向（长边/短边混排被人工指定成统一方向时）
_fixed_plans = (manual_duplex.file_plans_for_pages("a.pdf", [1, 2, 3, 4], True, [], True)
                + manual_duplex.file_plans_for_pages("b.pdf", [5, 6], True, [], False))
_fixed = R.add(_record(_fixed_plans, rotate_mode=R.ROTATE_FIXED, rotate=True, pdf=pdf_keep))
check("★ rotate_mode=fixed：逐文件方向被压平成统一值（补打不能各打各的）",
      all(bool(x) for x in R.build_plan(_fixed).pass_rotate(2)), f"{R.build_plan(_fixed).pass_rotate(2)}")
R.remove(_fixed["id"])

# ============================================================
# ④ 状态机：pending → 补打失败仍是 pending → 成功/作废各自收尾
# ============================================================
print("\n=== ④ 状态机 ===")
rid = saved["id"]
st1 = R.mark_stalled(rid, reason="补打时纸没放回去", stage=R.STAGE_PASS2_PARTIAL)
check("mark_stalled：仍然是 pending（**不能**因为一次补打失败就判成失败）",
      st1 is not None and st1["state"] == R.STATE_PENDING, f"{st1 and st1['state']}")
check("mark_stalled：attempts 自增（界面能说「已尝试补救 N 次」）",
      int(st1["attempts"]) == 1, f"{st1['attempts']}")
check("mark_stalled：stage 变成「第 2 趟打了一半」→ 这条不能再补第 2 趟",
      st1["stage"] == R.STAGE_PASS2_PARTIAL and "第 2 趟" in R.stage_label(st1),
      R.stage_label(st1))
st2 = R.mark_stalled(rid, reason="又失败一次")
check("再失败一次：attempts 继续累加，记录不会消失",
      int(st2["attempts"]) == 2 and R.pending_count() == 1, f"{st2['attempts']}")

_pdf = R.pending()[0]["pdf"]
R.resolve(rid, note="补打第 2 趟完成")
check("resolve：记录转 resolved，不再出现在待补救列表",
      R.pending() == [] and (R.get(rid) or {}).get("state") == R.STATE_RESOLVED)
check("resolve：半成品 PDF 被删（纸已经是成品了，留着只会占地方）",
      not os.path.isfile(_pdf), _pdf)
check("resolve：记录仍在 load_all() 里（留痕，便于回溯）",
      any(r["id"] == rid for r in R.load_all()))

pdf2 = R.keep_pdf(_fake_pdf("half2.pdf"), "20261110-000000-9999")
rec2 = R.add(_record(plans, pdf=pdf2))
R.abandon(rec2["id"], note="操作员放弃，纸已丢")
check("abandon：转 abandoned + 删 PDF（作废就是明确放弃，不是「不知道」）",
      (R.get(rec2["id"]) or {}).get("state") == R.STATE_ABANDONED
      and not os.path.isfile(pdf2)
      and R.pending_count() == 0)

# ============================================================
# ⑤ 崩溃自愈：清单被写坏/被截断 → 不抛异常、按「没有待补救」处理，并留证
# ============================================================
print("\n=== ⑤ 坏清单自愈 ===")
pdf3 = R.keep_pdf(_fake_pdf("half3.pdf"), "20261110-000001-9999")
R.add(_record(plans, pdf=pdf3))
with open(R.manifest_path(), "w", encoding="utf-8") as f:
    f.write('{"version": 1, "records": [')     # 半截 JSON（断电/磁盘满时的现场）
_n = R.pending_count()
_bads = [p for p in os.listdir(os.path.dirname(R.manifest_path()))
         if ".bad-" in p]
check("★ 坏清单不抛异常：pending_count() 归零（界面不能因为台账坏了就打不开）",
      _n == 0, f"{_n}")
check("★ 坏清单被改名留证（.bad-时间戳），不是被直接覆盖掉",
      bool(_bads), f"{_bads}")
check("坏清单之后还能正常写新记录（自愈，不是从此瘫痪）",
      R.pending_count() == 0 and bool(R.add(_record(plans, pdf=pdf3))["id"])
      and R.pending_count() == 1)
R.remove(R.pending()[0]["id"])

# 记录形状不对（items 不是 list / 缺 id）时静默丢弃，不许炸
with open(R.manifest_path(), "w", encoding="utf-8") as f:
    json.dump({"version": 1, "records": [
        {"id": "", "items": []},                       # 没有 id → 丢弃
        {"id": "ok-1", "items": "坏", "file_plans": "坏"},   # 形状坏 → 修成空
        "这不是字典",                                   # 直接丢弃
    ]}, f)
_alive = R.load_all()
check("★ 形状坏的记录被修/被丢，读出来的东西永远是能用的形状",
      len(_alive) == 1 and _alive[0]["id"] == "ok-1"
      and _alive[0]["items"] == [] and _alive[0]["file_plans"] == [],
      f"{[(r['id'], len(r['items'])) for r in _alive]}")
_wipe()

# ============================================================
# ⑥ 清理：只清「已完成/已作废」的旧记录，待补救的永远不动
# ============================================================
print("\n=== ⑥ 过期清理 ===")
pdf_old = R.keep_pdf(_fake_pdf("old.pdf"), "20200101-000000-9999")
old = R.add(_record(plans, pdf=pdf_old))
R.update(old["id"], created_at="2020-01-01 00:00:00")
R.resolve(old["id"], note="早就补完了")
check("30 天前的已完成记录：prune(30) 清掉（连同 PDF）",
      R.prune(days=30) == 1 and not os.path.isfile(pdf_old)
      and not any(r["id"] == old["id"] for r in R.load_all()))

pdf_pend = R.keep_pdf(_fake_pdf("pend_old.pdf"), "20200102-000000-9999")
stale = R.add(_record(plans, pdf=pdf_pend))
R.update(stale["id"], created_at="2020-01-02 00:00:00")
check("★ 30 天前**还没处理**的半成品：prune 绝不清理（那是没打完的纸，删了等于把活丢了）",
      R.prune(days=30) == 0 and R.pending_count() == 1
      and os.path.isfile(pdf_pend), f"{R.pending_count()}")
R.remove(stale["id"])

# ============================================================
# ⑦ 能不能补打：problems / pdf_available 说人话
# ============================================================
print("\n=== ⑦ 可补打判定 ===")
pdf_gone = R.keep_pdf(_fake_pdf("gone.pdf"), "20261110-000002-9999")
gan = R.add(_record(plans, pdf=pdf_gone))
check("正常记录：problems() 为空（三个动作都能点）", R.problems(gan) == [], f"{R.problems(gan)}")
os.remove(pdf_gone)
check("★ PDF 被删/被清理：problems() 指出来（界面据此禁用「补打第 2 趟」）",
      any("PDF" in p for p in R.problems(gan)), f"{R.problems(gan)}")

no_plans = R.add(_record([], pdf=pdf_gone))
check("★ 没有页序计划：problems() 指出来（否则补打会对着空计划「成功」）",
      any("页序" in p for p in R.problems(no_plans)), f"{R.problems(no_plans)}")
R.remove(gan["id"])
R.remove(no_plans["id"])

# ============================================================
# ⑧ 文案：给操作员看的话必须能照着做，且不能出现 Markdown 星号
# ============================================================
print("\n=== ⑧ 界面文案 ===")
pdf_txt = R.keep_pdf(_fake_pdf("txt.pdf"), "20261110-000003-9999")
txt = R.add(_record(plans, pdf=pdf_txt,
                    items=[{"idx": 0, "tab_key": "t1", "task_id": 1, "order_id": 2,
                            "order_number": "HN20261110-0002", "display_name": "论文.pdf",
                            "file_path": "D:/x/论文.pdf", "copies": 1, "duplex": "on",
                            "page_range": "", "page_count": 5, "owner_name": "",
                            "is_admin_print": False}],
                    tray_foreign=2))
_desc = R.describe(txt)
check("说明里有两趟页序（第 1 趟已打 / 第 2 趟应打）",
      "第 1 趟已打页：" in _desc and "第 2 趟应打页：" in _desc, _desc.splitlines()[6:8])
check("说明里有「补打第 2 趟」的三步操作（取纸 → 插盘 2 → 点按钮）",
      "1. 从出纸槽取出" in _desc and "2. 保持相同方向插入纸盘 2" in _desc
      and "▶ 补打第 2 趟" in _desc)
check("出纸槽里当时还压着别的纸时，说明会提醒「下面那几张别动」",
      "下面那 2 张别动" in _desc, [l for l in _desc.splitlines() if "别动" in l])
check("★ 文案里没有 Markdown 星号（QTextEdit/QLabel 不解析，会原样画出来）",
      "**" not in _desc and "**" not in R.summary_line(txt))
check("列表行是单行、含时间/状态/张数/订单",
      "\n" not in R.summary_line(txt)
      and "HN20261110-0002" in R.summary_line(txt)
      and "张纸" in R.summary_line(txt), R.summary_line(txt))
R.mark_stalled(txt["id"], reason="第 2 趟打了一半卡纸", stage=R.STAGE_PASS2_PARTIAL)
check("第 2 趟打了一半的记录：文案改口「纸叠不能再用，请整单重打或作废」",
      "纸叠不能再用" in R.describe(R.get(txt["id"]))
      and "整单重打" in R.describe(R.get(txt["id"]))
      and "作废" in R.describe(R.get(txt["id"])),
      R.describe(R.get(txt["id"])).splitlines()[-1])
R.remove(txt["id"])

# ============================================================
print("\n" + "=" * 60)
_done = R.load_all()
if _done:
    print(f"（收尾清理：还有 {len(_done)} 条记录留在沙箱里）")
    for _r in _done:
        R.remove(_r["id"])
print("手动双面「半成品补救」台账验收: " + ("全部通过 ✅" if _ok_all else "有失败 ❌"))
sys.exit(0 if _ok_all else 1)
