#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""tests/verify_landscape_fill.py — 横向页「铺满纸张」与逐面 180° 补偿验收（2026-10-27 现场）

现场问题（用户口头 + 日志两处证据）：
  ① 横向 PDF（崔清阳-成绩-横向-所有.pdf，841.9×595.3pt）在手动双面批次里没设纸方向 ⇒
     按纵向纸等比缩放，实测只占纸张 49%（4736×3349 / 4736×6824，上下各留 1737px 空白），
     现场读成「横向文件打出来是竖着的」；
  ② 同一订单被按装订方向劈成两批、翻两次纸（日志 12:58 短边 6 张 / 12:59 长边 2 张），
     第二批还要在出纸槽里"只取最上面 N 张" —— 操作员看不懂，直接点了跳过，整单判失败。

本脚本钉住两件事：
  A. 几何：横向页在纵向纸上**转 90° 铺满**（占纸张 ≥95%），竖版页与"纸也横"的情形不转；
  B. 真管线：拿一张横向 PDF 过**真实渲染循环**（假 DC，不出纸），量出来的是竖版画布、
     且原来在"顶部"的内容落在**左边缘**（读者顺时针转 90° 看）；再叠 180° 补偿仍是竖版画布；
  C. 逐面补偿：manual_duplex 里每个文件按自己的装订方向决定第 2 面转不转（同批可混排）；
  D. 变异自证：关掉 landscape_fill / 让 angle 恒 0 / 忽略逐文件标志 / 不回传 rotate_slots，
     对应断言必须转红。

跑法：python tests/verify_landscape_fill.py     （退出码 0 = 全绿）
"""
import contextlib
import ctypes
import os
import sys
import tempfile
import types

HERE = os.path.dirname(os.path.abspath(__file__))
LOCAL_TOOL = os.path.join(os.path.dirname(HERE), "local_print_tool")
sys.path.insert(0, LOCAL_TOOL)

import fitz                                    # noqa: E402
from PIL import Image, ImageWin                 # noqa: E402

import pdf_printer as pp                        # noqa: E402
import manual_duplex as md                      # noqa: E402

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


# HP LaserJet 1020 实测：600dpi、可打印区 4736×6824、物理 A4 纵向 4961×7016
CAP_W, CAP_H = 4736, 6824
A4W, A4H = 595.276, 841.89          # 纵向 A4（pt）
A4LW, A4LH = A4H, A4W               # 横向 A4 = 宽高对调


# ============================================================
# A. 纯几何
# ============================================================
print("=== A. 几何：横向页要不要转 90°、转完多大 ===")

ang, dw, dh, rw, rh, cap = pp.plan_page_draw(A4W, A4H, 600, 600, CAP_W, CAP_H)
check("竖版页 + 纵向纸 → 不转（angle=0），尺寸与旧口径逐像素一致", ang == 0
      and (dw, dh) == pp.plan_page_raster(A4W, A4H, 600, 600, CAP_W, CAP_H)[:2],
      f"angle={ang} {dw}x{dh}")

ang_l, lw, lh, lrw, lrh, _ = pp.plan_page_draw(A4LW, A4LH, 600, 600, CAP_W, CAP_H)
check("★ 横向页 + 纵向纸 → 转 90°（angle=90）", ang_l == 90, f"angle={ang_l} {lw}x{lh}")
check("★ 转完是**竖版**画布，贴图矩形落在可打印区里（不再溢到纸外）",
      lw <= CAP_W + 2 and lh <= CAP_H + 2 and lh > lw,
      f"{lw}x{lh} vs 可打印 {CAP_W}x{CAP_H}")
usage = (lw * lh) / float(CAP_W * CAP_H)
check("★ 铺满：转完之后占可打印区 ≥95%（旧口径只有 49%）", usage >= 0.95,
      f"{usage * 100:.1f}%")
_old = pp.plan_page_raster(A4LW, A4LH, 600, 600, CAP_W, CAP_H)
_old_usage = (_old[0] * _old[1]) / float(CAP_W * CAP_H)
check("对照：旧口径（不转，等比缩放）确实只有 ~49%", 0.45 <= _old_usage <= 0.52,
      f"{_old_usage * 100:.1f}%")

ang2, dw2, dh2, _, _, _ = pp.plan_page_draw(A4LW, A4LH, 600, 600, CAP_H, CAP_W)
check("纸本身是横的（可打印区 6824×4736）+ 横向页 → 不转（转了就反了）",
      ang2 == 0 and (dw2, dh2) == (6698, 4736), f"angle={ang2} {dw2}x{dh2}")

ang3, dw3, dh3, _, _, _ = pp.plan_page_draw(A4W, A4H, 600, 600, CAP_H, CAP_W)
check("纸是横的 + 竖版页 → 反过来也要转 90°（转完是横版画布，同样铺满）",
      ang3 == 90 and dw3 > dh3, f"angle={ang3} {dw3}x{dh3}")

ang4, dw4, dh4, _, _, _ = pp.plan_page_draw(A4LW, A4LH, 600, 600, CAP_W, CAP_H,
                                            landscape_fill=False)
check("★ 关掉 landscape_fill → 回到旧口径（angle=0，缩到 49%）",
      ang4 == 0 and (dw4, dh4) == (_old[0], _old[1]), f"angle={ang4} {dw4}x{dh4}")

ang5, dw5, dh5, rw5, rh5, cap5 = pp.plan_page_draw(A4LW, A4LH, 600, 600, CAP_W, CAP_H,
                                                   dpi_cap=400)
check("转 90° 时 dpi 上限仍然生效（位图按 400dpi 生成、贴图矩形仍是设备像素）",
      ang5 == 90 and cap5 and abs(rw5 - 3307) <= 3 and (dw5, dh5) == (lw, lh),
      f"render={rw5}x{rh5} draw={dw5}x{dh5}")


# ============================================================
# B. 真管线：假 DC 里跑一遍渲染循环，量位图
# ============================================================
print("\n=== B. 真管线：横向页渲染出来是竖版画布、内容顶部在左边缘 ===")


class _FakeDC:
    def __init__(self, rec, hdc, native_dpi=600, printable=(CAP_W, CAP_H)):
        self.rec, self.hdc = rec, hdc
        self.native_dpi, self.printable = native_dpi, printable

    def GetDeviceCaps(self, cap):
        if cap == 88:       # LOGPIXELSX
            return self.native_dpi
        if cap == 90:       # LOGPIXELSY
            return self.native_dpi
        if cap == 8:        # HORZRES
            return self.printable[0]
        if cap == 10:       # VERTRES
            return self.printable[1]
        return 0

    def CreatePrinterDC(self, *a):
        pass

    def GetHandleOutput(self):
        return self.hdc

    def StartDoc(self, *a):
        pass

    def EndDoc(self):
        pass

    def StartPage(self):
        pass

    def EndPage(self):
        pass

    def AbortDoc(self):
        pass

    def DeleteDC(self):
        pass


@contextlib.contextmanager
def fake_printer():
    saved = {k: sys.modules.get(k) for k in ("win32print", "win32ui")}
    gdi = ctypes.WinDLL("gdi32", use_last_error=True)
    gdi.CreateCompatibleDC.restype = ctypes.c_void_p
    gdi.CreateCompatibleDC.argtypes = [ctypes.c_void_p]
    gdi.DeleteDC.argtypes = [ctypes.c_void_p]
    hdc = gdi.CreateCompatibleDC(None)
    wpr = types.ModuleType("win32print")
    wpr.GetDefaultPrinter = lambda: "HN-LANDSCAPE-PROBE(无设备)"
    wui = types.ModuleType("win32ui")
    wui.CreateDC = lambda: _FakeDC(None, hdc)
    sys.modules["win32print"] = wpr
    sys.modules["win32ui"] = wui
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v
        gdi.DeleteDC(hdc)


def _landscape_pdf(path, marks=0):
    """造一张横向 A4：顶部一条黑带（用来判断转完之后内容朝哪边）。"""
    doc = fitz.open()
    page = doc.new_page(width=A4LW, height=A4LH)
    page.draw_rect(fitz.Rect(0, 0, A4LW, 28), color=None, fill=(0, 0, 0))
    for i in range(marks):
        page.insert_text((40, 120 + i * 30), "LANDSCAPE PROBE TOP EDGE", fontsize=18)
    doc.save(path)
    doc.close()


def run_pipeline(pdf_path, **kw):
    """跑真实 print_pdf，返回 (ok, msg, [(mode, img_w, img_h, draw_w, draw_h, img_copy)])。"""
    got = []
    cur = {}
    orig_init, orig_draw = ImageWin.Dib.__init__, ImageWin.Dib.draw

    def _init(self, image, *a, **k):
        cur["mode"], cur["size"] = image.mode, image.size
        cur["copy"] = image.copy()
        return orig_init(self, image, *a, **k)

    def _draw(self, dib_dc, dst_rect):
        if cur.get("size") and cur["size"] != (8, 8):
            x0, y0, x1, y1 = dst_rect
            got.append((cur["mode"], cur["size"][0], cur["size"][1],
                        x1 - x0, y1 - y0, cur["copy"]))
        return orig_draw(self, dib_dc, dst_rect)

    ImageWin.Dib.__init__, ImageWin.Dib.draw = _init, _draw
    try:
        with fake_printer():
            ok, msg = pp.print_pdf(pdf_path, printer_name="HN-LANDSCAPE-PROBE(无设备)",
                                   copies=1, duplex="off", dpi=0, **kw)
    finally:
        ImageWin.Dib.__init__, ImageWin.Dib.draw = orig_init, orig_draw
    return ok, msg, got


def ink_bbox(img):
    """位图里非白像素的包围盒（(x0, y0, x1, y1), 覆盖率）；纯白返回 None。"""
    g = img.convert("L")
    # 缩到 1/8 再算，够用且快（4736×6698 全量遍历太慢）
    w, h = g.size
    g = g.resize((max(1, w // 8), max(1, h // 8)))
    px = g.load()
    W, H = g.size
    x0, y0, x1, y1 = W, H, -1, -1
    dark = 0
    for y in range(H):
        for x in range(W):
            if px[x, y] < 128:
                dark += 1
                if x < x0:
                    x0 = x
                if x > x1:
                    x1 = x
                if y < y0:
                    y0 = y
                if y > y1:
                    y1 = y
    if x1 < 0:
        return None, 0.0
    # 归一到 0~1：判据不跟位图尺寸/降采样比例纠缠（曾经因为单位混用假红过一次）
    return (x0 / float(W), y0 / float(H), x1 / float(W), y1 / float(H)), dark / float(W * H)


tmp = tempfile.mkdtemp(prefix="hn_landscape_")
pdf_ls = os.path.join(tmp, "landscape.pdf")
_landscape_pdf(pdf_ls)

ok, msg, got = run_pipeline(pdf_ls)
check("横向 PDF 过真渲染循环：打印成功", ok, msg)
check("★ 送去 Dib 的位图是**竖版**画布（高 > 宽，≈4736×6698）", bool(got)
      and got[0][2] > got[0][1] and abs(got[0][1] - CAP_W) <= 3 and abs(got[0][2] - 6698) <= 6,
      str(got[0][1:5] if got else None))
check("位图 == 贴图矩形（仍是 1:1、零缩放）", bool(got)
      and got[0][1] == got[0][3] and got[0][2] == got[0][4], str(got[0][1:5] if got else None))

_box, _cov = ink_bbox(got[0][5]) if got else (None, 0.0)
check("渲染结果里有内容（判据不是恒真）", _box is not None and _cov > 0.001, f"{_box} cov={_cov:.4f}")
if _box:
    x0, y0, x1, y1 = _box
    # 页面上那股黑带原本在**顶边**；转 90°（PIL ROTATE_90 = 逆时针）之后顶边到**左侧**
    # ⇒ 变成一条贴左边缘、纵向贯通的黑带（读者把纸顺时针转 90° 就看到正常横向版面）。
    check("★ 原来在顶边的黑带落在**左边缘**（内容顶部朝左 ⇒ 读者顺时针转纸 90° 看）",
          x0 <= 0.02 and x1 <= 0.10 and (y1 - y0) >= 0.9,
          f"x=[{x0:.3f},{x1:.3f}] y=[{y0:.3f},{y1:.3f}]")
    check("★ 黑带不再横贯纸面顶端（那是「没转、只是缩小」的旧口径特征）",
          (y1 - y0) >= 0.9, f"纵向覆盖={y1 - y0:.3f}")

# ── 转 180° 补偿叠在 90° 上：仍然是竖版画布，黑带换到右边缘 ──
ok_r, msg_r, got_r = run_pipeline(pdf_ls, rotate_180=True)
check("横向页 + 180° 补偿（手动双面第 2 趟）：打印成功", ok_r, msg_r)
check("★ 90° 与 180° 叠加后仍是竖版画布（顺序无关：90+180 == 180+90 == 270）",
      bool(got_r) and got_r[0][2] > got_r[0][1] and abs(got_r[0][2] - 6698) <= 6,
      str(got_r[0][1:5] if got_r else None))
_box_r, _ = ink_bbox(got_r[0][5]) if got_r else (None, 0.0)
if _box_r and _box:
    check("★ 再转 180° ⇒ 黑带从左边缘换到右边缘（补偿真的生效了）",
          _box_r[2] >= 0.98 and _box_r[0] >= 0.90 and _box[0] <= 0.02,
          f"补偿后 x=[{_box_r[0]:.3f},{_box_r[2]:.3f}]，不补偿 x0={_box[0]:.3f}")

# ── 竖版 PDF 不受影响（零回归） ──
pdf_pt = os.path.join(tmp, "portrait.pdf")
_d = fitz.open()
_p = _d.new_page(width=A4W, height=A4H)
_p.insert_text((60, 90), "PORTRAIT PAGE", fontsize=14)
_d.save(pdf_pt)
_d.close()
ok_p, msg_p, got_p = run_pipeline(pdf_pt)
check("★ 竖版 PDF 保持不变（位图仍是竖版 A4 尺寸，不是转过的）",
      ok_p and bool(got_p) and abs(got_p[0][1] - CAP_W) <= 3 and abs(got_p[0][2] - 6698) <= 6,
      str(got_p[0][1:3] if got_p else None))

ok_o, _, got_o = run_pipeline(pdf_ls, landscape_fill=False)
check("★ landscape_fill=False（纸真的横着进纸的机器）：横向页不转，回到旧口径（宽 > 高）",
      ok_o and bool(got_o) and got_o[0][1] > got_o[0][2],
      str(got_o[0][1:3] if got_o else None))


# ============================================================
# C. 逐面 180° 补偿：同批长/短边混排
# ============================================================
print("\n=== C. manual_duplex：每个文件按自己的装订方向补偿 ===")

# L = 3 页双面（2 张纸）、S = 2 页单面（每页一张纸 ⇒ 2 张）；整批 4 张
files = [md.FilePlan("L", [1, 2, 3], True, True),      # 长边：第 2 面转
         md.FilePlan("S", [1, 2], False, False)]       # 短边 + 单面：第 2 面不转
plan = md.plan_passes(files, copies=1, first="odd", reverse_second=True)
check("第 1 趟永远不转（补偿只针对翻纸后的那一面）",
      all(x is False for x in plan.pass_rotate(1)), f"{plan.pass_rotate(1)}")
_lab = [(s.label, s.rotate) for s in plan.second_pass]
check("★ 第 2 趟逐面表：L 的两张纸都转、S 的两张都不转（同一批混排）",
      len([1 for lb, r in _lab if lb == "L"]) == 2
      and all(r for lb, r in _lab if lb == "L")
      and len([1 for lb, r in _lab if lb == "S"]) == 2
      and not any(r for lb, r in _lab if lb == "S")
      and len(_lab) == 4, f"{_lab}")
check("逐面表长度 == 该趟槽位数（可直接喂 rotate_slots）",
      len(plan.pass_rotate(2)) == len(plan.pass_page_indices(2)) == 4)

_p_def = md.plan_passes([md.FilePlan("X", [1, 2, 3])], second_rotate_180=True)
check("★ 文件没带 second_rotate 时退回整批默认（旧调用方零回归）",
      all(_p_def.pass_rotate(2)), f"{_p_def.pass_rotate(2)}")
_p_off = md.plan_passes([md.FilePlan("X", [1, 2, 3])], second_rotate_180=False)
check("整批默认 False（短边机）⇒ 逐面表全 False", not any(_p_off.pass_rotate(2)))

# print_plan_pass 真的把逐面表交给渲染层
print("\n=== C2. print_plan_pass 把逐面表交给 print_pdf ===")
seen = {}
_orig_print = pp.print_pdf


def _spy(*a, **kw):
    seen.update(kw)
    return True, "桩"


pp.print_pdf = _spy
try:
    pp.print_plan_pass(plan, os.path.join(tmp, "x.pdf"), "P", 2)
finally:
    pp.print_pdf = _orig_print
check("★ 第 2 趟：rotate_slots 逐面表原样传下去", seen.get("rotate_slots") == plan.pass_rotate(2),
      str(seen.get("rotate_slots")))
check("第 2 趟：页序仍是计划里的逆序（旋转不影响叠序）",
      seen.get("pages_override") == plan.pass_page_indices(2), str(seen.get("pages_override")))

seen.clear()
pp.print_pdf = _spy
try:
    pp.print_plan_pass(plan, os.path.join(tmp, "x.pdf"), "P", 2, second_rotate_180=False)
finally:
    pp.print_pdf = _orig_print
check("★ 调用方传 second_rotate_180=False（整趟不转）时，逐面表被清成全 False",
      seen.get("rotate_slots") == [False] * 4, str(seen.get("rotate_slots")))

seen.clear()
pp.print_pdf = _spy
try:
    pp.print_plan_pass(plan, os.path.join(tmp, "x.pdf"), "P", 1)
finally:
    pp.print_pdf = _orig_print
check("第 1 趟：逐面表全 False（不拿第 2 趟的补偿去打第 1 趟）",
      seen.get("rotate_slots") == [False] * 4, str(seen.get("rotate_slots")))


# ============================================================
# D. 静态接线 + 变异自证
# ============================================================
print("\n=== D. 静态接线 ===")
src_pp = open(os.path.join(LOCAL_TOOL, "pdf_printer.py"), encoding="utf-8").read()
src_gui = open(os.path.join(LOCAL_TOOL, "gui.py"), encoding="utf-8").read()
src_md = open(os.path.join(LOCAL_TOOL, "manual_duplex.py"), encoding="utf-8").read()
_flat_gui = " ".join(src_gui.split())

check("★ 横向页转 90° 用无损 transpose（不是会重采样的 rotate）",
      "img.transpose(_rot90)" in src_pp and "Transpose" in src_pp)
check("★ 手动双面批次把档案里的 landscape_fill 传下去",
      _flat_gui.count("landscape_fill=bool(getattr(p, \"landscape_fill\", True))") >= 2,
      f"count={_flat_gui.count('landscape_fill=bool(getattr(p,')}")
check("★ 批次不再按装订方向分组（旧的 groups.setdefault(self._rotate_for_job 已删）",
      "groups.setdefault(self._rotate_for_job" not in src_gui
      and "两种装订方向，将分" not in src_gui)
check("★ 每个文件的 FilePlan 带上自己的补偿标志",
      "self._rotate_for_job(job)" in src_gui and "dup, file_rot)" in src_gui)
check("pass_rotate 是唯一出表的地方（print_plan_pass 用它）",
      "def pass_rotate" in src_md and 'getattr(plan, "pass_rotate"' in src_pp)

print("\n=== D2. 变异自证（改坏 → 必须转红）===")

# 变异 1：angle 恒 0（回到旧口径）
_real_draw = pp.plan_page_draw
pp.plan_page_draw = lambda w, h, *a, **kw: (0,) + _real_draw(w, h, *a, landscape_fill=False)[1:]
try:
    _m = pp.plan_page_draw(A4LW, A4LH, 600, 600, CAP_W, CAP_H)
finally:
    pp.plan_page_draw = _real_draw
check("变异「angle 恒 0」→ A 段那条『转 90°』判据会转红（这里直接核变异后的值）",
      _m[0] == 0 and _m[1] > _m[2], f"{_m[0]} {_m[1]}x{_m[2]}")

# 变异 2：build_sheets 忽略逐文件标志（回到"整批一个开关"）
_real_bs = md.build_sheets
_old_bs = lambda files, copies=1, default_rotate=False: _real_bs(
    [md.FilePlan(f.label, f.pages, f.duplex, None) for f in files], copies, default_rotate)
_mut_plan = md.plan_passes([md.FilePlan("L", [1, 2, 3], True, True),
                            md.FilePlan("S", [1, 2], True, False)], second_rotate_180=True)
_old_plan_files = [md.FilePlan(f.label, f.pages, f.duplex, None) for f in
                   [md.FilePlan("L", [1, 2, 3], True, True), md.FilePlan("S", [1, 2], True, False)]]
_mut = md.plan_passes(_old_plan_files, second_rotate_180=True)
check("变异「忽略逐文件标志」→ 混排逐面表变成全 True（C 段那条就会红）",
      all(_mut.pass_rotate(2)) and not all(_mut_plan.pass_rotate(2)),
      f"变异后={_mut.pass_rotate(2)} 正常={_mut_plan.pass_rotate(2)}")

# 变异 3：print_plan_pass 不回传 rotate_slots
seen.clear()
pp.print_pdf = lambda *a, **kw: (seen.update(kw), (True, "桩"))[1]
_orig_ppp = pp.print_plan_pass


def _ppp_no_slots(plan_, pdf_path, printer_name="", which=1, *, second_rotate_180=True,
                  progress_callback=None, dpi=0, landscape_fill=True):
    return pp.print_pdf(pdf_path, printer_name, copies=1, duplex="off",
                        pages_override=plan_.pass_page_indices(2 if int(which) == 2 else 1),
                        rotate_180=(which == 2 and second_rotate_180),
                        progress_callback=progress_callback, dpi=dpi,
                        landscape_fill=landscape_fill)


try:
    _ppp_no_slots(plan, os.path.join(tmp, "x.pdf"), "P", 2)
finally:
    pp.print_pdf = _orig_print
check("变异「不回传 rotate_slots」→ C2 段那条判据会红（rotate_slots 为 None）",
      seen.get("rotate_slots") is None, str(seen.get("rotate_slots")))

print("\n" + ("=" * 60))
if FAILS:
    print(f"横向页铺满 / 逐面补偿验收: 存在失败 ❌  （{len(FAILS)} 项）")
    for f in FAILS:
        print("  - " + f)
    sys.exit(1)
print(f"横向页铺满 / 逐面补偿验收: 全部通过 ✅（{OKS} 项）")
