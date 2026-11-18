#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""other/tests/verify_print_scale.py — 打印缩放（调整为可打印区域大小 / 实际大小 / 自定义 %）验收

背景（2026-11-18 用户需求，原话「这个设置我的打印工具貌似并没有涉及到？做一下这个设置。」）：
用户贴的是 Windows 打印对话框里的「缩放(%)」三段，本工具此前**只会按可打印区等比缩小**
（`plan_page_raster` 里 `fit = min(1.0, 可打印区/物理尺寸)`），既不能按 100% 实际大小出纸，
也没有自定义百分比。改动后这一项成为全局配置（`PrinterConfig.scale_mode/scale_percent`），
PDF/Word/图片（凡走光栅管线的）都吃它；表格仍由 Excel 版式自己缩放，**不叠加**。

本脚本钉住五件事：
  A. 词表/纯函数：normalize_scale 收敛（未知模式→fit、百分比夹 10–400）、scale_factor、
     describe_scale（中文文案里**不许**出现 Markdown 星号）；
  B. 零回归：默认 fit 下的 (draw, render) 与改动前的实现**逐值相等**（120 组笛卡尔积），
     并做变异自证 —— 若 fit 分支被误改成"总是按 100%"，同一组断言会红；
  C. 几何：actual = 100% 物理尺寸（超出可打印区 → 由调用方居中裁剪）、percent = 倍率、
     与 dpi_cap / pixel_limit 的先后关系不变；
  D. 透传：print_pdf / print_plan_pass 把 (scale_mode, scale_percent) 一路带到
     _print_pdf_native（两趟/整叠必须同口径，否则正反面一大一小）；
  E. 配置与台账：printer_config 往返（老 JSON 缺键 ⇒ fit/100）、duplex_remedy 记录补打时
     原样复现（旧台账没有这两个键也要能读）；gui.py 的 8 个出纸点是否都接了缩放。

跑法：python other/tests/verify_print_scale.py     （退出码 0 = 全绿）
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
LOCAL_TOOL = os.path.join(ROOT, "local_print_tool")
sys.path.insert(0, LOCAL_TOOL)

import pdf_printer as pp                        # noqa: E402
import printer_config as pc                     # noqa: E402
import duplex_remedy as dr                      # noqa: E402

FAILS = []
OKS = 0


def check(name, cond, detail=""):
    global OKS
    if cond:
        OKS += 1
        print(f"[PASS] {name}" + (f"  -- {detail}" if detail else ""))
    else:
        FAILS.append(name)
        print(f"[FAIL] {name}" + (f"  -- {detail}" if detail else ""))


# HP LaserJet 1020 实测：600dpi、A4 可打印区 4736×6824（竖）
CAP_W, CAP_H = 4736, 6824
A4W, A4H = 595.276, 841.89          # 纵向 A4（pt）
A4LW, A4LH = A4H, A4W               # 横向 A4


# ============================================================
# A. 词表与纯函数
# ============================================================
print("① 缩放词表 / normalize_scale / scale_factor / describe_scale")

check("默认模式是 fit、默认百分比是 100",
      (pp.SCALE_FIT, pp.SCALE_PERCENT_DEFAULT) == ("fit", 100))
check("三档词表齐全", tuple(pp.SCALE_MODES) == ("fit", "actual", "percent"))
check("百分比上下限 10 / 400", (pp.SCALE_PERCENT_MIN, pp.SCALE_PERCENT_MAX) == (10, 400))

cases_norm = [
    (("fit", 100), ("fit", 100)),
    (("actual", 100), ("actual", 100)),
    (("percent", 150), ("percent", 150)),
    ((" percent ", "150"), ("percent", 150)),     # 前后空格 / 字符串数字
    (("Percent", 33.4), ("percent", 33)),         # 大小写 + 银行家舍入外的四舍五入
    (("bogus", 100), ("fit", 100)),               # 未知模式回落 fit
    ((None, None), ("fit", 100)),                  # 老配置/老台账缺键
    (("percent", 5), ("percent", 10)),             # 低于下限夹到 10
    (("percent", 9999), ("percent", 400)),         # 高于上限夹到 400
    (("percent", "abc"), ("percent", 100)),        # 非法数字 → 默认 100
    (("percent", True), ("percent", 10)),          # bool 当 1 处理，再被下限夹到 10
]
for (args, want) in cases_norm:
    got = pp.normalize_scale(*args)
    check(f"normalize_scale{args} == {want}", got == want, f"got={got}")

check("scale_factor(fit) 是 None（倍率要看页面）", pp.scale_factor("fit", 100) is None)
check("scale_factor(actual) == 1.0", pp.scale_factor("actual", 100) == 1.0)
check("scale_factor(percent 50) == 0.5", pp.scale_factor("percent", 50) == 0.5)
check("scale_factor(percent 200) == 2.0", pp.scale_factor("percent", 200) == 2.0)
check("scale_factor 未知模式按 fit 处理（None）", pp.scale_factor("nope", 100) is None)

check("describe_scale(fit)", pp.describe_scale("fit", 100) == "调整为可打印区域大小",
      pp.describe_scale("fit", 100))
check("describe_scale(actual)", pp.describe_scale("actual", 100) == "实际大小（100%）",
      pp.describe_scale("actual", 100))
check("describe_scale(percent 150)", pp.describe_scale("percent", 150) == "自定义缩放 150%",
      pp.describe_scale("percent", 150))
for _m in pp.SCALE_MODES:
    _d = pp.describe_scale(_m, 120)
    check(f"describe_scale({_m}) 不含 Markdown 星号", "*" not in _d, _d)


# ============================================================
# B. 零回归：默认 fit 与改动前实现逐值相等
# ============================================================
print("\n② 默认 fit ＝改动前的「只缩不放」（逐值相等，老用户零感知）")


def old_plan_raster(page_w_pts, page_h_pts, ndx, ndy, printable_w, printable_h,
                    dpi_cap=0, pixel_limit=0):
    """2026-11-18 之前的实现（保留作变异基线，别当死代码删掉）。"""
    dev_w = page_w_pts * ndx / 72.0
    dev_h = page_h_pts * ndy / 72.0
    if printable_w <= 0 or printable_h <= 0:
        printable_w = max(1, int(round(dev_w)))
        printable_h = max(1, int(round(dev_h)))
    fit = min(1.0, printable_w / max(1.0, dev_w), printable_h / max(1.0, dev_h))
    draw_w = max(1, int(round(dev_w * fit)))
    draw_h = max(1, int(round(dev_h * fit)))
    render_w, render_h = draw_w, draw_h
    capped = False
    cap = int(dpi_cap or 0)
    if cap > 0:
        cap_w = max(1, int(round(page_w_pts * cap / 72.0)))
        cap_h = max(1, int(round(page_h_pts * cap / 72.0)))
        if cap_w < draw_w or cap_h < draw_h:
            render_w, render_h = cap_w, cap_h
            capped = True
    limit = int(pixel_limit or 0)
    if limit > 0 and render_w * render_h > limit:
        k = (limit / float(render_w * render_h)) ** 0.5
        render_w = max(1, int(render_w * k))
        render_h = max(1, int(render_h * k))
        capped = True
    return draw_w, draw_h, render_w, render_h, capped


PAGES = [(A4W, A4H), (A4LW, A4LH), (420.0, 595.0), (1190.55, 1683.78), (612.0, 792.0)]
DPIS = [(600, 600), (300, 300)]
CANS = [(CAP_W, CAP_H), (0, 0), (2480, 3508)]
CAPS = [(0, 0), (300, 100_000_000)]
same = 0
diff = []
for _pw, _ph in PAGES:
    for _dx, _dy in DPIS:
        for _cw, _ch in CANS:
            for _cap, _lim in CAPS:
                a = pp.plan_page_raster(_pw, _ph, _dx, _dy, _cw, _ch,
                                        dpi_cap=_cap, pixel_limit=_lim)
                b = old_plan_raster(_pw, _ph, _dx, _dy, _cw, _ch,
                                    dpi_cap=_cap, pixel_limit=_lim)
                if a == b:
                    same += 1
                else:
                    diff.append(((_pw, _ph, _dx, _cw, _cap, _lim), a, b))
check(f"fit 默认与旧实现完全一致（{same}/{len(PAGES)*len(DPIS)*len(CANS)*len(CAPS)} 组）",
      not diff, str(diff[:2]))

# 变异自证：把倍率强行当 100% 用，同一组里的 A4 大页必然与旧行为不同 → 断言有牙
_dev_w = A4W * 600 / 72.0
_mutant = (max(1, int(round(_dev_w))), None)
check("变异：fit 若被当成 100% 物理尺寸，A4 会撑出可打印区（旧行为是缩到 4736 宽）",
      _mutant[0] > CAP_W and old_plan_raster(A4W, A4H, 600, 600, CAP_W, CAP_H)[0] == CAP_W,
      f"100%={_mutant[0]}px, 旧fit={old_plan_raster(A4W, A4H, 600, 600, CAP_W, CAP_H)[0]}px")


# ============================================================
# C. 几何：三档各自的物理含义
# ============================================================
print("\n③ actual = 100% 物理尺寸；percent = 倍率；溢出交给调用方居中裁剪")

dev_a4_w = int(round(A4W * 600 / 72.0))
dev_a4_h = int(round(A4H * 600 / 72.0))
dw, dh, rw, rh, capped = pp.plan_page_raster(A4W, A4H, 600, 600, CAP_W, CAP_H,
                                             scale_mode="actual")
check(f"actual：A4 纵向 600dpi ⇒ 物理 {dev_a4_w}×{dev_a4_h}（不缩）",
      (dw, dh) == (dev_a4_w, dev_a4_h), f"got={dw}x{dh}")
check("actual：渲染尺寸与贴图尺寸 1:1（零重采样）", (rw, rh) == (dw, dh) and not capped)
check("actual：A4(4961) 比可打印区(4736) 宽 ⇒ 确实溢出，由 GDI 居中裁掉两侧",
      dw > CAP_W and dh > CAP_H,
      f"{dw}x{dh} vs 可打印区 {CAP_W}x{CAP_H}")

dw, dh, *_ = pp.plan_page_raster(A4W, A4H, 600, 600, CAP_W, CAP_H,
                                 scale_mode="percent", scale_percent=50)
check("percent 50：恰好是物理尺寸的一半（4961→2480 / 7016→3508）",
      (dw, dh) == (int(round(dev_a4_w * 0.5)), int(round(dev_a4_h * 0.5))), f"got={dw}x{dh}")

dw, dh, *_ = pp.plan_page_raster(A4W, A4H, 600, 600, CAP_W, CAP_H,
                                 scale_mode="percent", scale_percent=200)
# 注意：期望值必须从**未取整的物理尺寸**算（A4 宽 595.276pt=4960.63px，×2=9921.27→9921；
# 若拿取整后的 4961×2 去比会差 1px，那是断言写错不是实现错）
check("percent 200：放大到两倍物理尺寸（可打印区自动裁剪）",
      (dw, dh) == (int(round(A4W * 600 / 72.0 * 2)), int(round(A4H * 600 / 72.0 * 2))),
      f"got={dw}x{dh}")

dw, dh, *_ = pp.plan_page_raster(420.0, 595.0, 600, 600, CAP_W, CAP_H,
                                 scale_mode="percent", scale_percent=10)
check("percent 10：小页面也能缩到 1/10（不会退化成 1px）",
      dw >= 1 and dh >= 1 and dw < 420.0 * 600 / 72.0 / 5, f"got={dw}x{dh}")

dw, dh, rw, rh, capped = pp.plan_page_raster(A4W, A4H, 600, 600, CAP_W, CAP_H,
                                             dpi_cap=300, scale_mode="percent",
                                             scale_percent=200)
check("percent 200 + dpi_cap 300：贴图仍是 200%，渲染位图被限到 300dpi（GDI 放大一次）",
      (dw, dh) == (int(round(A4W * 600 / 72.0 * 2)), int(round(A4H * 600 / 72.0 * 2)))
      and capped and rw < dw,
      f"draw={dw}x{dh} render={rw}x{rh} capped={capped}")

print("\n④ plan_page_draw：横向页转 90° 与缩放互不干扰")
ang0, dw0, dh0, *_ = pp.plan_page_draw(A4W, A4H, 600, 600, CAP_W, CAP_H)
check("竖版 A4 + 竖纸：不转（angle=0）", ang0 == 0, f"angle={ang0}")
ang1, dw1, dh1, *_ = pp.plan_page_draw(A4LW, A4LH, 600, 600, CAP_W, CAP_H)
check("横版 A4 + 竖纸 fit：仍转 90° 铺满（铺满率 > 95%）",
      ang1 == 90 and (dw1 * dh1) / float(CAP_W * CAP_H) > 0.95,
      f"angle={ang1} {dw1}x{dh1}")
ang2, dw2, dh2, *_ = pp.plan_page_draw(A4LW, A4LH, 600, 600, CAP_W, CAP_H,
                                         landscape_fill=False)
check("landscape_fill=False：横版页不转（旧路径可回退）",
      ang2 == 0 and (dw2, dh2) != (dw1, dh1), f"angle={ang2} {dw2}x{dh2}")

ang3, dw3, dh3, *_ = pp.plan_page_draw(A4W, A4H, 600, 600, CAP_W, CAP_H,
                                        scale_mode="actual")
check("actual + 竖版 A4：转不转都不影响 100% 物理尺寸（4961×7016）",
      (dw3, dh3) == (dev_a4_w, dev_a4_h), f"angle={ang3} {dw3}x{dh3}")


# ============================================================
# D. 透传：print_pdf / print_plan_pass → _print_pdf_native
# ============================================================
print("\n⑤ print_pdf / print_plan_pass 把缩放一路送到 _print_pdf_native")
try:
    import fitz
except Exception as exc:                              # pragma: no cover
    print(f"[SKIP] PyMuPDF 不可用，跳过透传段：{exc}")
    fitz = None

if fitz is not None:
    tmpdir = tempfile.mkdtemp(prefix="verify_scale_")
    pdf = os.path.join(tmpdir, "one.pdf")
    _doc = fitz.open()
    _doc.new_page(width=A4W, height=A4H)
    _doc.save(pdf)
    _doc.close()

    orig_native = pp._print_pdf_native
    seen = {}

    def _fake_native(*args, **kwargs):
        seen["tail"] = args[-2:]
        return True, "stub"

    pp._print_pdf_native = _fake_native
    try:
        ok, _msg = pp.print_pdf(pdf, "HP LaserJet 1020", duplex="off", copies=1,
                                scale_mode="percent", scale_percent=150)
        check("print_pdf(percent 150) → 原生层收到 ('percent', 150)",
              ok and seen.get("tail") == ("percent", 150), str(seen.get("tail")))
        pp.print_pdf(pdf, "HP LaserJet 1020", duplex="off", copies=1)
        check("print_pdf 默认 → 原生层收到 ('fit', 100)（零回归）",
              seen.get("tail") == ("fit", 100), str(seen.get("tail")))
        pp.print_pdf(pdf, "HP LaserJet 1020", duplex="off", copies=1,
                     scale_mode="nonsense", scale_percent=9999)
        check("print_pdf 非法缩放 → 原生层收到已归一化的 ('fit', 400)",
              seen.get("tail") == ("fit", 400), str(seen.get("tail")))
    finally:
        pp._print_pdf_native = orig_native

    class _Plan:
        total_sides = 2
        sheet_count = 1

        def pass_page_indices(self, which):
            return [0] if int(which) == 1 else [0]

        def pass_rotate(self, which):
            return []

    orig_print = pp.print_pdf
    got = {}

    def _fake_print(*args, **kwargs):
        got.update(kwargs)
        return True, "stub"

    pp.print_pdf = _fake_print
    try:
        ok, _msg = pp.print_plan_pass(_Plan(), pdf, "HP LaserJet 1020", 2,
                                      second_rotate_180=True,
                                      scale_mode="actual", scale_percent=100)
        check("print_plan_pass(第 2 趟) → print_pdf 带 ('actual', 100)",
              ok and got.get("scale_mode") == "actual" and got.get("scale_percent") == 100,
              f"scale_mode={got.get('scale_mode')} scale_percent={got.get('scale_percent')}")
        got.clear()
        ok, _msg = pp.print_plan_pass(_Plan(), pdf, "HP LaserJet 1020", 1)
        check("print_plan_pass 默认 → print_pdf 带 ('fit', 100)",
              ok and got.get("scale_mode") == "fit" and got.get("scale_percent") == 100,
              f"scale_mode={got.get('scale_mode')}")
    finally:
        pp.print_pdf = orig_print

    src_pp = open(os.path.join(LOCAL_TOOL, "pdf_printer.py"), encoding="utf-8").read()
    check("原生层有「缩放后超出可打印区」的一次性告警（现场能看出两侧被裁）",
          "overflow_noted = False" in src_pp and "cut_x = max(0," in src_pp
          and "cut_y = max(0," in src_pp)
    check("Sumatra 降级明确声明缩放不生效（而不是静默按原尺寸出纸）",
          "if scale_mode != SCALE_FIT:" in src_pp
          and "SumatraPDF" in src_pp)
    check("_print_via_loop 也接了缩放（多份之间不能一张缩一张不缩）",
          "scale_mode=scale_mode, scale_percent=scale_percent" in src_pp
          and src_pp.count("scale_mode=scale_mode") >= 3,
          f"count={src_pp.count('scale_mode=scale_mode')}")


# ============================================================
# E. 配置与台账
# ============================================================
print("\n⑥ printer_config：默认 fit/100、往返、老配置缺键")
cfg = pc.PrinterConfig()
check("新配置默认 print_scale() == ('fit', 100)", cfg.print_scale() == ("fit", 100),
      str(cfg.print_scale()))
check("scale_text() 与 describe_scale 同源",
      cfg.scale_text() == pp.describe_scale("fit", 100), cfg.scale_text())
cfg.scale_mode = "percent"
cfg.scale_percent = 150
_d = cfg.to_dict()
check("to_dict 写出 scale_mode / scale_percent",
      _d.get("scale_mode") == "percent" and _d.get("scale_percent") == 150,
      f"{_d.get('scale_mode')}/{_d.get('scale_percent')}")
check("from_dict 往返保真", pc.PrinterConfig.from_dict(_d).print_scale() == ("percent", 150),
      str(pc.PrinterConfig.from_dict(_d).print_scale()))
check("老 print_config.json（没有这两个键）⇒ fit/100",
      pc.PrinterConfig.from_dict({}).print_scale() == ("fit", 100))
check("配置里的脏值（未知模式 + 越界百分比）⇒ ('fit', 400)，不会崩",
      pc.PrinterConfig.from_dict({"scale_mode": "x", "scale_percent": 9999}).print_scale()
      == ("fit", 400))
_d2 = pc.PrinterConfig.from_dict(_d).to_dict()
check("旧配置读进来再存回去不会丢掉缩放", _d2.get("scale_mode") == "percent"
      and _d2.get("scale_percent") == 150)

print("\n⑦ duplex_remedy：补打原样复现同一套缩放")
rec = dr.make_record(stage=dr.STAGE_PASS1_DONE, reason="测试", printer="P",
                     scale_mode="percent", scale_percent=125)
check("make_record 记下 scale_mode/scale_percent",
      rec.get("scale_mode") == "percent" and rec.get("scale_percent") == 125,
      f"{rec.get('scale_mode')}/{rec.get('scale_percent')}")
rec_def = dr.make_record(stage=dr.STAGE_PASS1_DONE, reason="测试", printer="P")
check("make_record 缺省 fit/100", rec_def.get("scale_mode") == "fit"
      and rec_def.get("scale_percent") == 100)
old_rec = dict(rec)
old_rec.pop("scale_mode", None)
old_rec.pop("scale_percent", None)
_txt = dr.describe(old_rec)
check("旧台账（没有缩放键）也能描述，且按 fit 显示",
      "缩放" in _txt and "调整为可打印区域大小" in _txt, _txt.splitlines()[1] if "\n" in _txt else _txt)
check("台账描述里不含 Markdown 星号", "*" not in _txt)
_txt2 = dr.describe(rec)
check("新台账描述里出现「自定义缩放 125%」", "自定义缩放 125%" in _txt2)

print("\n⑧ gui.py：8 个出纸点全接缩放、菜单/对话框/槽就位")
src_gui = open(os.path.join(LOCAL_TOOL, "gui.py"), encoding="utf-8").read()

check("对话框类存在", "class PrintScaleDialog" in src_gui)
check("三档文案常量存在且顺序与取值一致",
      "SCALE_MODE_LABELS" in src_gui and "SCALE_MODE_VALUES = (SCALE_FIT, SCALE_ACTUAL,"
      in src_gui.replace("\n", " ").replace("  ", " ")
      or "SCALE_MODE_VALUES = (SCALE_FIT, SCALE_ACTUAL," in src_gui)
check("选项菜单有「打印缩放…」且连到槽",
      "scale_action = QAction(" in src_gui
      and "options_menu.addAction(scale_action)" in src_gui
      and "scale_action.triggered.connect(self._on_print_scale_options)" in src_gui)
check("槽 _on_print_scale_options 存在并落盘", "def _on_print_scale_options" in src_gui
      and "self._config.scale_mode = mode" in src_gui)
_slot_pos = src_gui.find("def _on_print_scale_options")
_slot_body = src_gui[_slot_pos:_slot_pos + 1400] if _slot_pos >= 0 else ""
check("槽内异常自己兜住（PySide6 槽异常会崩进程）",
      _slot_pos >= 0 and "except Exception as e:" in _slot_body
      and "logger.exception(" in _slot_body and "QMessageBox.warning(" in _slot_body)

_markers = [
    ("合并组 print_pdf", "ok, msg = print_pdf("),
    ("单文件/单组 print_pdf", "return print_pdf("),
    ("手动双面第 1 趟", "print_plan_pass(plan, merged, self._printer_name, 1,"),
    ("手动双面第 2 趟", "print_plan_pass(plan, merged, self._printer_name, 2,"),
    ("攒批第 1 趟", "print_plan_pass(plan, self._stack_out_pdf, self._printer_name, 1,"),
    ("首页封面 print_pdf", "cover_ok, cover_msg = print_pdf("),
    ("整叠第 2 趟（DuplexFlushWorker）", "ok2, msg2 = print_plan_pass("),
    ("补救/重打（DuplexRemedyWorker）", "return print_plan_pass("),
]
for label, marker in _markers:
    idx = src_gui.find(marker)
    ok = idx >= 0 and "scale_mode=" in src_gui[idx:idx + 900]
    check(f"出纸点「{label}」带缩放", ok, f"idx={idx}")

check("PrintWorker 构造传全局配置（scale_mode=self._config.scale_mode）",
      "scale_mode=self._config.scale_mode" in src_gui)
check("DuplexFlushWorker 的 __init__ 收缩放参数",
      "scale_mode: str = SCALE_FIT" in src_gui)
check("DuplexFlushWorker 第 2 趟与第 1 趟同口径（self._scale_mode 至少 6 处）",
      src_gui.count("scale_mode=self._scale_mode") >= 6,
      f"count={src_gui.count('scale_mode=self._scale_mode')}")
check("DuplexRemedyWorker 记录值优先（None 才回落台账，不能用 or —— fit/100 是有效值）",
      'scale_mode if scale_mode is not None else self._rec.get("scale_mode")' in src_gui)

_mr = 0
_pos = 0
while True:
    _pos = src_gui.find("duplex_remedy.make_record(", _pos)
    if _pos < 0:
        break
    _mr += 1
    check(f"半成品台账第 {_mr} 处 make_record 带缩放",
          "scale_mode=" in src_gui[_pos:_pos + 1600], f"idx={_pos}")
    _pos += 10
check("半成品台账共 4 处 make_record（合并组/翻纸失败/攒批/取消退出）", _mr == 4, f"count={_mr}")

check("界面文案不含 Markdown 星号（仓库硬规矩）",
      "**" not in pp.describe_scale("percent", 150)
      and "**" not in pp.describe_scale("actual", 100))

# ============================================================
# 汇总
# ============================================================
print("\n" + ("=" * 60))
if FAILS:
    print(f"打印缩放验收: 存在失败 ❌  （{len(FAILS)} 项）")
    for f in FAILS:
        print("  - " + f)
    sys.exit(1)
print(f"打印缩放验收: 全部通过 ✅（{OKS} 项）")
