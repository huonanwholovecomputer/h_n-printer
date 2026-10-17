"""verify_native_render.py — 「按设备最终像素 1:1 渲染 +(单色)1bit」验收。

背景（2026-10）：旧路径按设定 DPI（默认 400）渲染 RGB 位图，再 StretchDIBits 放大到
打印机 600dpi 设备像素（1.43× 最近邻）并被单色驱动半色调 —— 小字锯齿的直接来源。
新路径：plan_page_raster() 先算目标设备像素，再让 MuPDF 直接渲染到该尺寸（1:1）；
单色队列输出 1bit（阈值/抖动），内存从 99.6 MiB/页降到 ~4 MiB/页。

判定（退出码 0 = 通过）：
  ① plan_page_raster 纯逻辑：自动/上限/像素上限/小页面 的几何都正确；
  ② _to_bilevel 阈值正确（t 以下黑、t 以上白），_page_needs_dither 只看图片覆盖；
  ③ _is_monochrome_printer 三路证据（DEVMODE.Color / 能力位 / DC 位深）；
  ④ **真实管线**（__print_pdf_native 逐页循环，只换 win32 设备层 + 内存 DC）：
       · 单色 + dpi=0 → 送去 Dib 的是 mode "1"，贴图矩形 == 位图尺寸（1:1、零缩放）；
       · 单色 + dpi=400 → 位图变小（3307 宽）但贴图矩形仍是设备矩形（物理尺寸不变）；
       · 彩色 + 小页面 → mode "RGB"。

用法：python tests/verify_native_render.py
"""

from __future__ import annotations

import contextlib
import ctypes
import os
import sys
import tempfile
import types

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "local_print_tool"))

import fitz
from PIL import Image, ImageWin

import pdf_printer as pp

FAILS = []


def check(name: str, cond: bool, detail: str = ""):
    if cond:
        print(f"  ✓ {name}" + (f"  [{detail}]" if detail else ""))
    else:
        print(f"  ✗ {name}" + (f"  [{detail}]" if detail else ""))
        FAILS.append(name)


# ============================================================
# ① 纯几何
# ============================================================
print("[1] plan_page_raster 几何")
A4W, A4H = 595.276, 841.89
# HP LaserJet 1020 实测：设备 600dpi、可打印区 4736×6824、物理页 4961×7016
d, dr, rw, rh, cap = pp.plan_page_raster(A4W, A4H, 600, 600, 4736, 6824, dpi_cap=0)
check("自动模式：位图 == 目标矩形（1:1，无重采样）", (rw, rh) == (d, dr) and not cap, f"{d}x{dr}")
check("自动模式：A4 适配可打印区后 ≈4736 宽", abs(d - 4736) <= 2 and abs(dr - 6698) <= 3, f"{d}x{dr}")

d, dr, rw, rh, cap = pp.plan_page_raster(A4W, A4H, 600, 600, 4736, 6824, dpi_cap=400)
check("400 上限：位图 ≈400dpi（3307 宽）且标记 capped", abs(rw - 3307) <= 2 and cap, f"{rw}x{rh}")
check("400 上限：贴图目标仍按设备尺寸（物理尺寸不变）", (d, dr) == (4736, 6698), f"{d}x{dr}")

d, dr, rw, rh, cap = pp.plan_page_raster(A4W, A4H, 600, 600, 4736, 6824, dpi_cap=600)
check("600 上限（≥设备原生）不触发降采样", (rw, rh) == (d, dr) and not cap)

d, dr, rw, rh, cap = pp.plan_page_raster(A4W, A4H, 600, 600, 4736, 6824, pixel_limit=10_000_000)
check("像素上限：超限自动降尺寸并标记 capped", rw * rh <= 10_000_000 and cap, f"{rw}x{rh}")

d, dr, rw, rh, cap = pp.plan_page_raster(210, 297, 600, 600, 4736, 6824, dpi_cap=0)
check("小页面（A5）：不放大，按自身尺寸 1:1", (rw, rh) == (d, dr) and d < 2000, f"{d}x{dr}")

# ============================================================
# ② 1bit 转换
# ============================================================
print("[2] 1bit 转换")
im = Image.new("L", (4, 1))
im.putdata([0, 135, 136, 255])
b = pp._to_bilevel(im, 136)
check("阈值 136：135 黑 / 136 白", b.mode == "1" and list(b.getdata()) == [0, 0, 255, 255])
b2 = pp._to_bilevel(im, 200)
check("阈值可调（200）", list(b2.getdata()) == [0, 0, 0, 255])
check("灰度图也可转（非 L 输入）", pp._to_bilevel(Image.new("RGB", (4, 4), "gray")).mode == "1")

# ============================================================
# ③ 单色判定
# ============================================================
print("[3] 单色判定")


class _DM:
    def __init__(self, color):
        self.Color = color


class _DC:
    def __init__(self, colors=256, bpp=8):
        self._c, self._b = colors, bpp

    def GetDeviceCaps(self, which):
        # win32con: NUMCOLORS=24 / BITSPIXEL=12
        return {24: self._c, 12: self._b}[which]


check("DEVMODE.Color=1 → 单色", pp._is_monochrome_printer("x", _DM(1), _DC(), types.SimpleNamespace(NUMCOLORS=24, BITSPIXEL=12)))
check("DEVMODE.Color=2 → 彩色（不被 DC 位深误导）",
      not pp._is_monochrome_printer("x", _DM(2), _DC(2, 1), types.SimpleNamespace(NUMCOLORS=24, BITSPIXEL=12)))
check("无 DEVMODE：DC NUMCOLORS=2 → 单色",
      pp._is_monochrome_printer("x", None, _DC(2, 8), types.SimpleNamespace(NUMCOLORS=24, BITSPIXEL=12)))
check("无 DEVMODE：DC BITSPIXEL=1 → 单色",
      pp._is_monochrome_printer("x", None, _DC(256, 1), types.SimpleNamespace(NUMCOLORS=24, BITSPIXEL=12)))
check("无 DEVMODE：8bpp 有调色板 → 不武断判单色",
      not pp._is_monochrome_printer("x", None, _DC(256, 8), types.SimpleNamespace(NUMCOLORS=24, BITSPIXEL=12)))

# ============================================================
# ③b 抖动判定：非纯黑内容必须保灰阶（阈值会把浅灰抹成白）
# ============================================================
print("[3b] 抖动判定（保灰阶）")


def _probe_page(path, kind):
    d = fitz.open()
    p = d.new_page(width=595, height=842)
    if kind == "black_text":
        p.insert_text((60, 100), "black text 黑色文字", fontsize=12, color=(0, 0, 0))
    elif kind == "gray_text":
        p.insert_text((60, 100), "gray text 浅灰文字", fontsize=12, color=(0.55, 0.55, 0.55))
    elif kind == "black_fill":
        p.draw_rect(fitz.Rect(60, 120, 300, 160), color=None, fill=(0, 0, 0))
    elif kind == "gray_fill":
        p.draw_rect(fitz.Rect(60, 120, 300, 160), color=None, fill=(0.85, 0.85, 0.85))
    elif kind == "image":
        pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 40, 40), 0)
        pix.clear_with(200)
        p.insert_image(fitz.Rect(60, 200, 200, 340), pixmap=pix)
    d.save(path)
    d.close()
    return fitz.open(path)[0]


_probe_dir = tempfile.mkdtemp(prefix="hn_dither_")
_probe = {}
for _k in ("black_text", "gray_text", "black_fill", "gray_fill", "image"):
    _probe[_k] = _probe_page(os.path.join(_probe_dir, _k + ".pdf"), _k)

check("纯黑文字页 → 硬阈值（保锐度）", not pp._page_needs_dither(_probe["black_text"]))
check("浅灰文字 → 抖动（否则会变白丢字）", pp._page_needs_dither(_probe["gray_text"]))
check("纯黑填充 → 硬阈值", not pp._page_needs_dither(_probe["black_fill"]))
check("浅灰底纹 → 抖动（否则灰底纹消失）", pp._page_needs_dither(_probe["gray_fill"]))
check("含图片（哪怕很小）→ 抖动（保照片中间调）", pp._page_needs_dither(_probe["image"]))
for _p in _probe.values():
    _p.parent.close()

# ============================================================
# ④ 真实逐页循环（伪 win32 + 内存 DC，不出纸）
# ============================================================
print("[4] 真实管线（伪设备层 + 内存 DC）")


class _Rec:
    def __init__(self):
        self.pages = 0
        self.startdoc = 0
        self.enddoc = 0


class _FakeDC:
    def __init__(self, rec, hdc, native_dpi, printable):
        self._rec, self._hdc, self._dpi, self._pr = rec, hdc, native_dpi, printable

    def CreatePrinterDC(self, name):
        pass

    def GetHandleOutput(self):
        return self._hdc

    def GetDeviceCaps(self, which):
        # win32con：LOGPIXELSX=88 / LOGPIXELSY=90 / HORZRES=8 / VERTRES=10
        return {88: self._dpi, 90: self._dpi, 8: self._pr[0], 10: self._pr[1]}[which]

    def StartDoc(self, title):
        self._rec.startdoc += 1

    def StartPage(self):
        self._rec.pages += 1

    def EndPage(self):
        pass

    def EndDoc(self):
        self._rec.enddoc += 1

    def AbortDoc(self):
        pass

    def DeleteDC(self):
        pass


@contextlib.contextmanager
def fake_printer(native_dpi=600, printable=(4736, 6824)):
    saved = {k: sys.modules.get(k) for k in ("win32print", "win32ui")}
    rec = _Rec()
    gdi = ctypes.WinDLL("gdi32", use_last_error=True)
    gdi.CreateCompatibleDC.restype = ctypes.c_void_p
    gdi.CreateCompatibleDC.argtypes = [ctypes.c_void_p]
    gdi.DeleteDC.argtypes = [ctypes.c_void_p]
    hdc = gdi.CreateCompatibleDC(None)

    wpr = types.ModuleType("win32print")
    wpr.GetDefaultPrinter = lambda: "HN-NATIVE-PROBE(无设备)"
    wui = types.ModuleType("win32ui")
    wui.CreateDC = lambda: _FakeDC(rec, hdc, native_dpi, printable)
    sys.modules["win32print"] = wpr
    sys.modules["win32ui"] = wui
    try:
        yield rec
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v
        gdi.DeleteDC(hdc)


def make_pdf(path, pages=1, size=(A4W, A4H)):
    doc = fitz.open()
    for i in range(pages):
        page = doc.new_page(width=size[0], height=size[1])
        page.insert_text((50, 80), f"native render probe page {i + 1}", fontsize=10.4)
    doc.save(path)
    doc.close()


def run(pdf_path, dpi, monkey_mono):
    """跑一次真实管线，返回 [(img_mode, img_w, img_h, draw_w, draw_h), ...]（只记内容页）。"""
    got = []
    orig_init, orig_draw = ImageWin.Dib.__init__, ImageWin.Dib.draw
    cur = {}

    def _init(self, image, *a, **k):
        cur["mode"] = image.mode
        cur["size"] = image.size
        return orig_init(self, image, *a, **k)

    def _draw(self, dib_dc, dst_rect):
        if cur.get("size") and cur["size"] != (8, 8):     # 跳过 8×8 空白占位 DIB
            x0, y0, x1, y1 = dst_rect
            got.append((cur["mode"], *cur["size"], x1 - x0, y1 - y0))
        return orig_draw(self, dib_dc, dst_rect)

    ImageWin.Dib.__init__, ImageWin.Dib.draw = _init, _draw
    orig_mono = pp._is_monochrome_printer
    pp._is_monochrome_printer = lambda *a, **k: monkey_mono
    try:
        with fake_printer() as rec:
            ok, msg = pp.print_pdf(pdf_path, printer_name="HN-NATIVE-PROBE(无设备)",
                                   copies=1, duplex="off", dpi=dpi)
    finally:
        ImageWin.Dib.__init__, ImageWin.Dib.draw = orig_init, orig_draw
        pp._is_monochrome_printer = orig_mono
    return ok, msg, got, rec


tmp = tempfile.mkdtemp(prefix="hn_native_")
pdf1 = os.path.join(tmp, "a4_1p.pdf")
pdf_small = os.path.join(tmp, "a5_1p.pdf")
make_pdf(pdf1, 1)
make_pdf(pdf_small, 1, size=(210, 297))

ok, msg, got, rec = run(pdf1, dpi=0, monkey_mono=True)
check("单色 + dpi=0：打印成功", ok, msg)
check("单色 + dpi=0：送去 Dib 的是 1bit 位图", bool(got) and got[0][0] == "1", str(got[:1]))
check("单色 + dpi=0：位图尺寸 == 贴图矩形（1:1、零缩放）",
      bool(got) and got[0][1] == got[0][3] and got[0][2] == got[0][4], str(got[:1]))
check("单色 + dpi=0：位图 ≈4736×6698（600dpi 设备像素）",
      bool(got) and abs(got[0][1] - 4736) <= 2 and abs(got[0][2] - 6698) <= 3, str(got[:1]))

ok, msg, got, rec = run(pdf1, dpi=400, monkey_mono=True)
check("单色 + dpi=400：打印成功", ok, msg)
check("单色 + dpi=400：位图按 400dpi 生成（≈3307 宽）",
      bool(got) and abs(got[0][1] - 3307) <= 2, str(got[:1]))
check("单色 + dpi=400：贴图矩形仍为设备尺寸（物理尺寸不缩水）",
      bool(got) and got[0][3] == 4736 and abs(got[0][4] - 6698) <= 3, str(got[:1]))

ok, msg, got, rec = run(pdf_small, dpi=0, monkey_mono=False)
check("彩色 + dpi=0：打印成功", ok, msg)
check("彩色 + dpi=0：送去 Dib 的是 RGB", bool(got) and got[0][0] == "RGB", str(got[:1]))
check("彩色 + dpi=0：仍是 1:1（位图 == 贴图矩形）",
      bool(got) and got[0][1] == got[0][3] and got[0][2] == got[0][4], str(got[:1]))

print()
if FAILS:
    print(f"✗ 失败 {len(FAILS)} 项：" + "；".join(FAILS))
    sys.exit(1)
print("全部通过 ✅（几何 / 1bit / 单色判定 / 真实管线 1:1）")
