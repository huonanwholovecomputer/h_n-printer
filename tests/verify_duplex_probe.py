# -*- coding: utf-8 -*-
"""打印侧双面适配验收：能力探测 / 渲染序列 / 不再"静默单面却报成功"

（不需要真打印机）

三件必须钉住的事：

  ① **渲染序列是唯一真相**：常规路径（份数 × 页码 + 奇数页双面多份的空白分隔页）与
     手动双面路径（pages_override 精确序列，含 None 空白占位页）都要按计划逐面出纸。
     做法：注入**假 win32ui DC + 假 fitz 页 + 假 PIL 绘图**，记录每次 StartPage/EndPage
     之间到底画了哪一页 —— 不依赖任何真实设备，也能断言"纸上的顺序"。
  ② **驱动拒绝双面 → 必须失败**：无自动双面的驱动会在 DocumentProperties 合并时把
     Duplex 悄悄改回 1(SIMPLEX)。此时继续打只会单面出纸，而客户端仍按双面计费
     （实测：Print to PDF / WPS PDF / Adobe PDF 三个队列都会被改回 1）。
     所以要求：① `_get_printer_devmode` 回读实际值；② 请求双面但驱动改回 → 返回失败；
     ③ `print_pdf` 预检发现"high 置信度不支持"直接拒打；④ 这个失败**不得降级**到
     Sumatra/循环兜底（那两条同样依赖驱动，降级过去只会换个地方静默单面）。
  ③ 日志必须报驱动校验后的**实际值**，不能拿请求值冒充已生效。

用法: python tests/verify_duplex_probe.py   （exit 0 = 全部通过）
"""
import ast
import io
import os
import sys
import types

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL_TOOL = os.path.join(REPO, "local_print_tool")
sys.path.insert(0, LOCAL_TOOL)

import win32con  # noqa: E402  （真实常量模块：DM_DUPLEX / LOGPIXELSX 等）
import fitz as real_fitz  # noqa: E402  （真实 fitz：造真 PDF / 数页；假 fitz 只在假 DC 测试里临时顶上）
import pdf_printer as pp  # noqa: E402
import PIL  # noqa: E402
import PIL.Image  # noqa: E402  （显式导入，PIL.Image 才是 PIL 的属性）

try:
    import PIL.ImageWin  # noqa: E402
except ImportError:  # 非 Windows
    pass

ok_all = True


def check(name, cond, detail=""):
    global ok_all
    if not cond:
        ok_all = False
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


# ============================================================
# 假打印机环境：假 DC + 假页 + 假绘图，记录"每一面画了什么"
# ============================================================
class Rec:
    def __init__(self):
        self.pages = []          # 按出纸顺序记录每一面的内容："p<页号>" / "blank"
        self.current = None
        self.startdoc = 0
        self.enddoc = 0
        self.abort = 0
        self.in_page = False


class FakePixmap:
    def __init__(self, w, h):
        self.width, self.height = w, h
        self.samples = b"\x00" * (w * h * 3)
        self.stride = w * 3


class FakeRect:
    def __init__(self, w, h):
        self.width, self.height = w, h


class FakePage:
    def __init__(self, idx, rec):
        self.idx, self.rec = idx, rec
        self.rect = FakeRect(595.0, 842.0)     # A4 pt

    def get_pixmap(self, matrix=None, colorspace=3, alpha=False):
        self.rec.current = f"p{self.idx}"
        return FakePixmap(100, 140)


class FakeDoc:
    def __init__(self, n, rec):
        self.pages = [FakePage(i, rec) for i in range(n)]
        self.rec = rec

    def __len__(self):
        return len(self.pages)

    def __getitem__(self, i):
        return self.pages[i]

    def close(self):
        pass


class FakeDC:
    def __init__(self, rec):
        self.rec = rec
        self.printer = ""

    def CreatePrinterDC(self, name):
        self.printer = name

    def GetHandleOutput(self):
        return 12345

    def GetDeviceCaps(self, which):
        return {win32con.LOGPIXELSX: 300, win32con.LOGPIXELSY: 300,
                win32con.HORZRES: 2480, win32con.VERTRES: 3508}[which]

    def StartDoc(self, title):
        self.rec.startdoc += 1

    def StartPage(self):
        self.rec.in_page = True

    def EndPage(self):
        self.rec.in_page = False

    def EndDoc(self):
        self.rec.enddoc += 1

    def AbortDoc(self):
        self.rec.abort += 1

    def DeleteDC(self):
        pass


class FakeDevMode:
    def __init__(self):
        self.Duplex = 1
        self.Fields = 0
        self.Orientation = 1
        self.Color = 2          # DMCOLOR_COLOR：本测试只验证页序/旋转，强制走 RGB 位图路径


def install_fakes(rec, *, n_pages=6, driver_reverts=True, dm_readable=True,
                  openprinter_log=None, dm_log=None, dump=""):
    """注入假模块；返回 restore 回调。

    driver_reverts=True 模拟"没有双面器"的驱动：DocumentProperties 合并时把 Duplex 改回 1。
    openprinter_log / dm_log 分别记录 OpenPrinter / DocumentProperties 调用次数（查缓存用）。
    """
    saved = {k: sys.modules.get(k) for k in ("win32print", "win32ui", "fitz", "PIL.Image", "PIL.ImageWin")}
    saved_pil_image, saved_pil_imagewin = PIL.Image, getattr(PIL, "ImageWin", None)

    # ---- 假 win32print ----
    wpr = types.ModuleType("win32print")

    def _open(name):
        if openprinter_log is not None:
            openprinter_log.append(name)
        return f"h:{name}"

    def _getprinter(handle, level):
        dm = FakeDevMode() if dm_readable else None
        return {"pDevMode": dm, "pDriverName": "Fake Driver", "pPortName": "FAKE001"}

    def _document_properties(hwnd, handle, name, out, inp, flags):
        if dm_log is not None:
            dm_log.append(name)
        # 模拟驱动的合并校验：无自动双面能力的驱动会把 Duplex 归一化回 SIMPLEX
        if driver_reverts and getattr(inp, "Duplex", 1) in (2, 3):
            inp.Duplex = 1
        return 1

    wpr.OpenPrinter = _open
    wpr.GetPrinter = _getprinter
    wpr.DocumentProperties = _document_properties
    wpr.ClosePrinter = lambda h: None
    wpr.GetDefaultPrinter = lambda: "Fake Printer"
    wpr.DeviceCapabilities = lambda dev, port, cap: (0 if driver_reverts else 1)

    # ---- 假 win32ui ----
    wui = types.ModuleType("win32ui")
    wui.CreateDC = lambda: FakeDC(rec)

    # ---- 假 fitz ----
    fm = types.ModuleType("fitz")

    class _Matrix:
        def __init__(self, x, y):
            self.x, self.y = x, y

    fm.Matrix = _Matrix
    fm.csGRAY = 1               # 新增渲染策略用到这两个常量（真实 fitz 里分别是 1/3）
    fm.csRGB = 3
    fm.open = lambda path: FakeDoc(n_pages, rec)

    # ---- 假 PIL.Image / PIL.ImageWin ----
    class _FakeImg:
        """假位图：只记录类别与是否被 transpose 旋转过（用于验证 180° 补偿真的发生）。"""

        def __init__(self, kind, size=None, rotated=0):
            self.kind, self.size, self.rotated = kind, size, rotated

        def transpose(self, code):
            if code != "ROT180":
                raise AssertionError(f"pdf_printer 传了非 180° 的 transpose code: {code!r}")
            return _FakeImg(self.kind, self.size, (self.rotated + 180) % 360)

    pim = types.ModuleType("PIL.Image")
    pim.Transpose = types.SimpleNamespace(ROTATE_180="ROT180")   # 模拟 Pillow ≥10 的常量位置
    pim.frombuffer = lambda mode, size, samples, raw, rgb, stride, x: _FakeImg("IMAGE", size)
    pim.new = lambda mode, size, color: _FakeImg("BLANK", size)
    piw = types.ModuleType("PIL.ImageWin")

    class _Dib:
        def __init__(self, img):
            self.img = img

        def draw(self, hdc, rect):
            if not rec.in_page:
                raise AssertionError("draw 发生在 StartPage 之外")
            if getattr(self.img, "kind", "?") == "BLANK":
                rec.pages.append("blank")
            else:
                rec.pages.append(rec.current + ("(rot180)" if self.img.rotated else ""))

    piw.Dib = _Dib

    sys.modules["win32print"] = wpr
    sys.modules["win32ui"] = wui
    sys.modules["fitz"] = fm
    sys.modules["PIL.Image"] = pim
    sys.modules["PIL.ImageWin"] = piw
    PIL.Image, PIL.ImageWin = pim, piw

    def restore():
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v
        PIL.Image, PIL.ImageWin = saved_pil_image, saved_pil_imagewin

    return restore


# ============================================================
# ① 常规渲染序列（行为与历史一致：份数 × 页码 + 奇数页双面多份的空白分隔页）
# ============================================================
print("\n=== ① 常规路径渲染序列（假 DC，无打印机）===")
rec = Rec()
restore = install_fakes(rec, n_pages=6, driver_reverts=False)
try:
    ok, msg = pp._print_pdf_native("dummy.pdf", "Fake Printer", "off", "long-edge", "", 1, "portrait")
finally:
    restore()
check("单面 6 页 1 份：按顺序逐面出纸",
      ok and rec.pages == ["p0", "p1", "p2", "p3", "p4", "p5"], f"ok={ok} pages={rec.pages}")
check("StartDoc/EndDoc 成对", rec.startdoc == 1 and rec.enddoc == 1 and rec.abort == 0)

rec = Rec()
restore = install_fakes(rec, n_pages=6, driver_reverts=False)
try:
    ok, msg = pp._print_pdf_native("dummy.pdf", "Fake Printer", "on", "long-edge", "", 2, "portrait")
finally:
    restore()
check("双面 6 页 2 份（偶数页、无分隔页）：两份顺序拼接",
      ok and rec.pages == ["p0", "p1", "p2", "p3", "p4", "p5"] * 2, f"{rec.pages}")

rec = Rec()
restore = install_fakes(rec, n_pages=3, driver_reverts=False)
try:
    ok, msg = pp._print_pdf_native("dummy.pdf", "Fake Printer", "on", "long-edge", "", 2, "portrait")
finally:
    restore()
check("★ 奇数页(3) + 双面 + 2 份 → 副本间插一张空白分隔页（历史行为不变）",
      ok and rec.pages == ["p0", "p1", "p2", "blank", "p0", "p1", "p2"], f"{rec.pages}")

rec = Rec()
restore = install_fakes(rec, n_pages=6, driver_reverts=False)
try:
    ok, msg = pp._print_pdf_native("dummy.pdf", "Fake Printer", "off", "long-edge", "2-4", 1, "portrait")
finally:
    restore()
check("页码范围只打选中页", ok and rec.pages == ["p1", "p2", "p3"], f"{rec.pages}")

# ============================================================
# ② pages_override（手动双面两趟的精确序列，含空白占位页）
# ============================================================
print("\n=== ② 手动双面：pages_override 精确序列 ===")
rec = Rec()
restore = install_fakes(rec, n_pages=5, driver_reverts=False)
try:
    # 5 页手动双面第 1 趟（偶数面约定）= 2 4 〔空白〕（1-based → 0-based: 1,3,None）
    ok, msg = pp._print_pdf_native("dummy.pdf", "Fake Printer", "off", "long-edge", "",
                                   1, "portrait", None, 0, [1, 3, None])
finally:
    restore()
check("★ 第 1 趟按给定序列出纸（含空白占位页，不是跳过）",
      ok and rec.pages == ["p1", "p3", "blank"], f"{rec.pages}")
check("成功消息报实际面数", ok and "3 面" in msg, f"{msg}")

rec = Rec()
restore = install_fakes(rec, n_pages=5, driver_reverts=False)
try:
    ok, msg = pp._print_pdf_native("dummy.pdf", "Fake Printer", "off", "long-edge", "",
                                   1, "portrait", None, 0, [4, 2, 0])
finally:
    restore()
check("★ 第 2 趟逆序序列（5 3 1 → 0-based 4,2,0）", ok and rec.pages == ["p4", "p2", "p0"], f"{rec.pages}")

check("build_render_sequence 常规路径与 _print_pdf_native 一致",
      pp.build_render_sequence([0, 1, 2], 2, "on") == [0, 1, 2, None, 0, 1, 2],
      f"{pp.build_render_sequence([0, 1, 2], 2, 'on')}")
check("build_render_sequence 手动路径原样返回 override",
      pp.build_render_sequence([0, 1, 2], 3, "off", [1, None, 0]) == [1, None, 0])

# ============================================================
# ②b 手动双面第 2 趟的 180° 补偿
#     （HP LaserJet 1020 实机实测：不补偿时第 2 趟那一面整体"上下颠倒"）
# ============================================================
print("\n=== ②b 第 2 趟 180° 旋转补偿（实机发现）===")
import manual_duplex as md  # noqa: E402

plan5 = md.plan_single([1, 2, 3, 4, 5])
tmp5 = os.path.join(os.environ.get("TEMP", "."), "hn_duplex_probe_rot.pdf")
_d5 = real_fitz.open()
for _ in range(5):
    _d5.new_page()
_d5.save(tmp5)
_d5.close()

rec = Rec()
restore = install_fakes(rec, n_pages=5, driver_reverts=False)
try:
    ok, msg = pp._print_pdf_native("dummy.pdf", "Fake Printer", "off", "long-edge", "",
                                   1, "portrait", None, 0, [1, 3, None], True)
finally:
    restore()
check("★ rotate_180=True：内容页被旋转 180°，空白占位页不受影响",
      ok and rec.pages == ["p1(rot180)", "p3(rot180)", "blank"], f"{rec.pages}")

rec = Rec()
restore = install_fakes(rec, n_pages=5, driver_reverts=False)
try:
    ok, msg = pp._print_pdf_native("dummy.pdf", "Fake Printer", "off", "long-edge", "",
                                   1, "portrait", None, 0, [1, 3, None])
finally:
    restore()
check("rotate_180 默认关闭（常规打印路径行为不变）",
      ok and rec.pages == ["p1", "p3", "blank"], f"{rec.pages}")

# print_plan_pass：第 1 趟不转、第 2 趟转 —— 这条规则只允许存在一个地方
# （默认约定 = odd：第 1 趟 = 1 3 5，第 2 趟 = 〔空白〕 4 2）
plan5 = md.plan_single([1, 2, 3, 4, 5])
rec = Rec()
restore = install_fakes(rec, n_pages=5, driver_reverts=False)
try:
    ok, msg = pp.print_plan_pass(plan5, tmp5, "Fake Printer", 1)
finally:
    restore()
check("★ print_plan_pass(第 1 趟)：不旋转，序列 = 1 3 5（odd 默认）",
      ok and rec.pages == ["p0", "p2", "p4"], f"ok={ok} {rec.pages}")

rec = Rec()
restore = install_fakes(rec, n_pages=5, driver_reverts=False)
try:
    ok, msg = pp.print_plan_pass(plan5, tmp5, "Fake Printer", 2)
finally:
    restore()
check("★ print_plan_pass(第 2 趟)：逆序 + 全部旋转 180°，且开头的空白占位页仍要送纸",
      ok and rec.pages == ["blank", "p3(rot180)", "p1(rot180)"], f"ok={ok} {rec.pages}")

rec = Rec()
restore = install_fakes(rec, n_pages=5, driver_reverts=False)
try:
    ok, msg = pp.print_plan_pass(plan5, tmp5, "Fake Printer", 2, second_rotate_180=False)
finally:
    restore()
check("print_plan_pass(第 2 趟, second_rotate_180=False)：不旋转（短边/其它机型）",
      ok and rec.pages == ["blank", "p3", "p1"], f"{rec.pages}")

if os.path.isfile(tmp5):
    os.remove(tmp5)

# ============================================================
# ③ 驱动拒绝双面 → 必须失败，不得静默单面出纸
# ============================================================
print("\n=== ③ 驱动拒绝双面（无自动双面的机器）===")
rec = Rec()
restore = install_fakes(rec, n_pages=4, driver_reverts=True)
try:
    ok, msg = pp._print_pdf_native("dummy.pdf", "Fake Printer", "on", "long-edge", "", 1, "portrait")
finally:
    restore()
check("★ 请求双面但驱动改回单面 → 返回失败（不是成功）", ok is False, f"ok={ok} msg={msg}")
check("★ 失败带终止标记（禁止降级到 Sumatra/循环）",
      msg.startswith(pp.DUPLEX_UNSUPPORTED_MARKER), f"{msg}")
check("★ 一个面都没出纸（在 StartDoc 之前就拦住）",
      rec.pages == [] and rec.startdoc == 0, f"pages={rec.pages} startdoc={rec.startdoc}")

rec = Rec()
restore = install_fakes(rec, n_pages=4, driver_reverts=True)
try:
    ok, msg = pp._print_pdf_native("dummy.pdf", "Fake Printer", "off", "long-edge", "", 1, "portrait")
finally:
    restore()
check("同一台机器打**单面**照常工作（失败只针对双面请求）",
      ok and rec.pages == ["p0", "p1", "p2", "p3"], f"ok={ok} pages={rec.pages}")

# print_pdf 预检：high 置信度"不支持" → 直接拒打，连 GDI 都不试
print("\n=== ④ print_pdf 预检拒绝（high 置信度不支持）===")
import fitz as real_fitz  # noqa: E402  真实 fitz：造一个真 PDF 给 count_pdf_pages 读
tmp_pdf = os.path.join(os.environ.get("TEMP", "."), "hn_duplex_probe_test.pdf")
_d = real_fitz.open()
for _ in range(3):
    _d.new_page()
_d.save(tmp_pdf)
_d.close()

calls = []
saved_native, saved_probe = pp._print_pdf_native, pp.get_printer_duplex_support
pp._print_pdf_native = lambda *a, **kw: (calls.append("native"), (True, "stub"))[1]
try:
    pp.get_printer_duplex_support = lambda *a, **kw: {
        "auto_duplex": False, "confidence": "high", "printer": "HP LaserJet 1020",
        "detail": "无自动双面（能力位=0 且驱动把 Duplex 改回单面）"}
    ok, msg = pp.print_pdf(tmp_pdf, "HP LaserJet 1020", duplex="on")
    check("★ 预检发现不支持自动双面 → 拒绝双面打印",
          ok is False and "不支持自动双面" in msg, f"ok={ok} msg={msg}")
    check("★ 拒绝发生在打印之前（GDI 未被调用，没浪费纸）", calls == [], f"{calls}")
    check("报错文案给出出路（改单面 / 走手动双面）",
          "单面" in msg and "手动双面" in msg, f"{msg}")

    # 判不准（None）→ 不拦，照常打（避免误伤探测失败的机型）
    calls.clear()
    pp.get_printer_duplex_support = lambda *a, **kw: {
        "auto_duplex": None, "confidence": "low", "printer": "X", "detail": "判不出来"}
    ok, msg = pp.print_pdf(tmp_pdf, "X", duplex="on")
    check("能力判不准时不拦（照常提交，只记日志）", ok is True and calls == ["native"], f"ok={ok} {calls}")

    # 支持双面 → 正常走 GDI
    calls.clear()
    pp.get_printer_duplex_support = lambda *a, **kw: {
        "auto_duplex": True, "confidence": "medium", "printer": "HP M232", "detail": "ok"}
    ok, msg = pp.print_pdf(tmp_pdf, "HP M232", duplex="on")
    check("探测为支持 → 正常打印", ok is True and calls == ["native"], f"ok={ok} {calls}")
finally:
    pp._print_pdf_native, pp.get_printer_duplex_support = saved_native, saved_probe
    if os.path.isfile(tmp_pdf):
        os.remove(tmp_pdf)

# ============================================================
# ⑤ 手动双面每一趟**禁止**降级到 Sumatra（它只认整份 PDF，会把整份文件再打一遍）
# ============================================================
print("\n=== ⑤ pages_override 的降级策略 ===")
import fitz as real_fitz2  # noqa: E402
tmp_pdf2 = os.path.join(os.environ.get("TEMP", "."), "hn_duplex_probe_loop.pdf")
_d2 = real_fitz2.open()
for _ in range(3):
    _d2.new_page()
_d2.save(tmp_pdf2)
_d2.close()

saved = (pp._print_pdf_native, pp._find_sumatra_pdf, pp._print_via_loop)
seen = {"sumatra": 0, "loop": 0, "native": 0}
try:
    pp._print_pdf_native = lambda *a, **kw: (seen.__setitem__("native", seen["native"] + 1),
                                             (False, "boom"))[1]

    def _fake_sumatra():
        seen["sumatra"] += 1
        return "C:/fake/SumatraPDF.exe"

    pp._find_sumatra_pdf = _fake_sumatra

    def _fake_loop(*a, **kw):
        seen["loop"] += 1
        return True, "loop-ok"

    pp._print_via_loop = _fake_loop

    # 常规路径：GDI 失败 → 允许降级（Sumatra 会被尝试）
    seen.update({"sumatra": 0, "loop": 0, "native": 0})
    ok, msg = pp.print_pdf(tmp_pdf2, "Fake Printer", duplex="off")
    check("常规路径 GDI 失败仍会尝试 Sumatra 降级（历史行为不变）",
          seen["sumatra"] == 1, f"{seen}")

    # 手动双面一趟：绝不降级到 Sumatra，只走应用层循环（序列一致）
    seen.update({"sumatra": 0, "loop": 0, "native": 0})
    ok, msg = pp.print_pdf(tmp_pdf2, "Fake Printer", duplex="off", pages_override=[1, None, 0])
    check("★ 手动双面一趟（pages_override）失败时**不**降级 Sumatra（否则整份重打）",
          seen["sumatra"] == 0, f"{seen}")
    check("★ 且会走应用层循环兜底（序列原样透传）", seen["loop"] == 1 and ok is True, f"{seen}")

    # 循环兜底必须把 override 传给 GDI（否则"每次都打全部页"）
    seen.update({"sumatra": 0, "loop": 0, "native": 0})
    pp._print_via_loop = saved[2]          # 恢复真函数，让真正的 _print_via_loop 去调 native
    caught = {}

    def _capture(*a, **kw):
        caught["override"] = a[9] if len(a) > 9 else kw.get("pages_override")
        return True, "ok"

    pp._print_pdf_native = _capture
    pp._print_via_loop("x.pdf", "P", "off", "long-edge", "", 1, "", None, 0, [2, None])
    check("★ 循环兜底把 pages_override 透传给 GDI", caught.get("override") == [2, None],
          f"{caught}")
finally:
    pp._print_pdf_native, pp._find_sumatra_pdf, pp._print_via_loop = saved
    if os.path.isfile(tmp_pdf2):
        os.remove(tmp_pdf2)

# ============================================================
# ⑥ 能力探测：本机真实打印机（有就用，没有就跳过）
# ============================================================
print("\n=== ⑥ 能力探测（本机真实队列，交叉核对）===")
try:
    import win32print as real_wpr
    pp._DUPLEX_CAP_CACHE.clear()
    flags = real_wpr.PRINTER_ENUM_LOCAL | real_wpr.PRINTER_ENUM_CONNECTIONS
    rows = real_wpr.EnumPrinters(flags, None, 2)
    checked = no_duplex = dup = 0
    for info in rows:
        name, port = info["pPrinterName"], info.get("pPortName", "")
        try:
            raw = real_wpr.DeviceCapabilities(name, port, pp.DC_DUPLEX)
            dc = raw[0] if isinstance(raw, (tuple, list)) else raw
        except Exception:
            continue
        v = pp.get_printer_duplex_support(name, refresh=True)
        checked += 1
        # 能力位=0 的队列必须被判为"不支持"，且置信度不能低于 medium
        if dc == 0:
            no_duplex += 1
            check(f"能力位=0 的队列判为不支持: {name[:34]}",
                  v["auto_duplex"] is False and v["confidence"] in ("high", "medium"),
                  f"auto={v['auto_duplex']}/{v['confidence']} dc={v['dc_duplex']} acc={v['dm_accepted']}")
        elif dc == 1:
            dup += 1
            check(f"能力位=1 且驱动接受 → 判为支持但只给 medium（须人工确认）: {name[:34]}",
                  v["auto_duplex"] is True and v["confidence"] == "medium",
                  f"auto={v['auto_duplex']}/{v['confidence']}")
    print(f"  本机 {checked} 个队列（无自动双面 {no_duplex} 个 / 声明支持 {dup} 个）")
    if checked == 0:
        print("  （本机没有可探测的队列，跳过）")

    # 缓存：第二次直接命中，不再跑 DocumentProperties 探测（OpenPrinter 仍要调用 —— 它的
    # 作用就是读 driver/port 以确认缓存键仍然有效，比重复探测便宜得多）
    pp._DUPLEX_CAP_CACHE.clear()
    log, dmlog = [], []
    restore = install_fakes(Rec(), n_pages=2, driver_reverts=False,
                            openprinter_log=log, dm_log=dmlog)
    try:
        pp.get_printer_duplex_support("Cache Probe", refresh=True)
        n1, d1 = len(log), len(dmlog)
        pp.get_printer_duplex_support("Cache Probe")
        n2, d2 = len(log), len(dmlog)
    finally:
        restore()
    check("能力结果缓存：第二次不再重复探测（DocumentProperties 只跑一次）",
          d1 == 1 and d2 == 1, f"探测次数 {d1} → {d2}（OpenPrinter {n1} → {n2}）")

    # 假驱动"拒绝双面"时的判定：能力位=0 且被改回 → high 且不支持
    pp._DUPLEX_CAP_CACHE.clear()
    restore = install_fakes(Rec(), n_pages=2, driver_reverts=True)
    try:
        v = pp.get_printer_duplex_support("Reverting Printer", refresh=True)
    finally:
        restore()
    check("★ 能力位=0 且驱动改回单面 → auto_duplex=False / confidence=high",
          v["auto_duplex"] is False and v["confidence"] == "high" and v["dm_accepted"] is False,
          f"{v['auto_duplex']}/{v['confidence']}/{v['dm_accepted']}")

    # 假驱动"声明支持且接受" → True 但只给 medium（HP 1018/1020/1022 的虚报风险）
    pp._DUPLEX_CAP_CACHE.clear()
    restore = install_fakes(Rec(), n_pages=2, driver_reverts=False)
    try:
        v = pp.get_printer_duplex_support("Claiming Printer", refresh=True)
    finally:
        restore()
    check("★ 能力位=1 且驱动接受 → True 但 confidence=medium（提示需实机确认）",
          v["auto_duplex"] is True and v["confidence"] == "medium", f"{v['auto_duplex']}/{v['confidence']}")

    # 拿不到 DEVMODE → 不能谎报高置信度（能力位说支持，但没验证过）
    pp._DUPLEX_CAP_CACHE.clear()
    restore = install_fakes(Rec(), n_pages=2, driver_reverts=False, dm_readable=False)
    try:
        v = pp.get_printer_duplex_support("NoDevmode Printer", refresh=True)
    finally:
        restore()
    check("DEVMODE 取不到时不谎报高置信度（支持但低置信，交由人工确认）",
          v["auto_duplex"] is True and v["confidence"] in ("medium", "low")
          and v["dm_accepted"] is None, f"{v['auto_duplex']}/{v['confidence']}/{v['dm_accepted']}")
except ImportError:
    print("  （无 pywin32，跳过真实队列探测）")

# ============================================================
# ⑦ 静态接线：日志说真话 + 终止标记不被绕过
# ============================================================
print("\n=== ⑦ 静态接线 ===")
src = open(os.path.join(LOCAL_TOOL, "pdf_printer.py"), encoding="utf-8").read()
check("★ 日志报驱动实际值（旧的『请求值冒充已生效』写法已消失）",
      "→ 驱动实际=" in src and '双面模式={desc}")' not in src)
check("_get_printer_devmode 返回 (devmode, applied) 且调用方解包",
      "return devmode, applied_duplex" in src and "devmode, applied_duplex = _get_printer_devmode(" in src)
check("_get_printer_devmode 仍在 GDI 主路径被调用（审计约定：出现次数 >= 2）",
      src.count("_get_printer_devmode(") >= 2, f"count={src.count('_get_printer_devmode(')}")
check("DUPLEX_UNSUPPORTED_MARKER 在 _print_via_loop 里原样上抛",
      "if msg.startswith(DUPLEX_UNSUPPORTED_MARKER):" in src)
check("Sumatra 路径仍不被用于终止型双面失败（print_pdf 提前返回）",
      src.count("msg.startswith(DUPLEX_UNSUPPORTED_MARKER)") >= 2,
      f"count={src.count('msg.startswith(DUPLEX_UNSUPPORTED_MARKER)')}")
check("★ pages_override 时禁用 Sumatra 降级（源码级）",
      "sumatra = None if pages_override is not None else _find_sumatra_pdf()" in src)
# 2026-10-27：两条透传调用多了 rotate_slots / landscape_fill（逐面 180° 补偿 + 横向页转 90°），
# 判据改成"压缩空白后两条调用都以同一串参数结尾"，比钉死某个换行更耐用。
_flat = " ".join(src.split())
_TAIL = "pages_override, rotate_180, rotate_slots, landscape_fill)"
check("★ pages_override 与旋转参数透传到 _print_pdf_native 和 _print_via_loop（两条调用都在）",
      _flat.count(_TAIL) >= 2 and src.count("pages_override") >= 8,
      f"tail={_flat.count(_TAIL)}, pages_override={src.count('pages_override')}")
check("★ 第 2 趟旋转规则只在 print_plan_pass 一处定义",
      "def print_plan_pass(" in src and "rotate_180=(which == 2 and second_rotate_180)" in src)
check("180° 旋转用无损 transpose（不是会被重采样的 rotate）",
      "img.transpose(_rot180)" in src and "Image.Transpose" in src)

# ── 空白占位页：禁止按打印机原生 DPI 的**设备像素**开整页位图 ──
# 旧写法按 printable_w × printable_h（设备像素，非 PDF 点数）开整页 RGB 图，600dpi 的 A4
# 上 ≈ 99.6 MiB/张，比内容页还贵；手动双面第 2 趟 / 校准序列里空白页可以很多。
# 现改为复用一个 8×8 白 DIB。
#
# ⚠ 负向检查（"旧写法已消失"）必须先把**整行注释**剔掉：实现文件里会复述旧写法做说明
#   （例如注释里写着"旧值 400_000_000…"），直接把注释当代码会让检查假失败。
code_src = "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))
check("★ 空白占位页不再按设备像素开整页位图（旧写法已消失）",
      "Image.new(\"RGB\", (printable_w, printable_h)" not in code_src
      and "Image.new(\"RGB\", (8, 8), \"white\")" in code_src)
check("★ 空白占位页复用一个 DIB（不是每张新建）",
      "if blank_dib is None:" in code_src and code_src.count('blank_dib.draw(') == 1)
check("★ 空白占位页仍走 Dib.draw（与内容页同一条 StretchDIBits 路径，不引入新 GDI 调用）",
      code_src.count("dib.draw(") >= 2)

# ── 单页渲染像素上限：必须按进程位宽分档（旧硬编码值对 32 位等于无保护）──
try:
    import pdf_printer as _pp_mod
    _mp = _pp_mod.max_render_pixels()
    check("★ MAX_RENDER_PIXELS 由 max_render_pixels() 按位宽推导（不再是硬编码值）",
          "MAX_RENDER_PIXELS = max_render_pixels()" in code_src
          and "MAX_RENDER_PIXELS = 400_000_000" not in code_src)
    check("★ 像素上限落在合理区间（10 Mpx ~ 200 Mpx）",
          10_000_000 <= _mp <= 200_000_000, f"{_mp:,}")
    # 换算回单页位图字节数：必须显著小于旧值（400 Mpx × 3B = 1.14 GiB 单缓冲）
    _bytes = _mp * 3
    check("★ 单页位图上限显著小于旧的 1.14 GiB（实测瞬时系数 5.7×）",
          _bytes < 400 * 1024 * 1024, f"{_bytes / 1024 / 1024:.0f} MiB")
    check("★ 32 位分档存在且更保守（源码按 sys.maxsize 分流）",
          "sys.maxsize > 2 ** 32" in code_src and "300 * 1024 * 1024" in code_src)
    check("★ 上限已提到模块级（不在逐页循环里重新赋值；单色另有一档）",
          code_src.count("MAX_RENDER_PIXELS") >= 2
          and "MONO_RENDER_PIXELS = max_render_pixels(_PIXEL_BYTES_GRAY)" in code_src)
except Exception as e:  # pragma: no cover
    check(f"读取 pdf_printer.max_render_pixels() 失败: {e}", False)

# ── M1：逐页渲染的中间物（pix/img/dib）绝不能被攒起来 ──
# 【2026-10-14 重写】旧版是**字面子串**检查（`.append(pix)` / `self._pix`）—— 审查员实测：
# 把真循环改成 `_keep_img.append(rendered_im)`（200 面 @300dpi ≈ 5.0 GiB 常驻）后，
# 它**照样 exit 0**：变量名一换就漏。现在改为 **AST 审计**，从**赋值形态**反推"渲染中间物"
# （谁是被 get_pixmap / frombuffer / transpose / ImageWin.Dib / Image.new 赋出来的），
# 再查它们有没有"逃逸"出循环（进容器 / 进 self / 进下标 / 被 return）。
#
# 动态侧由 verify_render_memory.py 驱动真实 print_plan_pass 度量；两边一静一动，互为补充。
_RENDER_FACTORIES = {"get_pixmap", "frombuffer", "transpose", "Dib", "new", "copy",
                     "convert", "crop", "resize", "rotate", "tobytes"}
_CONTAINER_METHODS = {"append", "extend", "insert", "add", "update", "appendleft",
                      "appendleft_all", "push", "put"}
_ASSIGN_NODES = (ast.Assign, ast.AnnAssign, ast.AugAssign)


def _root_name(node):
    """属性链的根名：self._a.b → 'self'；_keep.img → '_keep'；其它 → None。"""
    while isinstance(node, ast.Attribute):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


def _names_in(node):
    """表达式里出现的全部裸名字（不含属性名）。"""
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)} if node is not None else set()


def _targets_of(node):
    t = getattr(node, "targets", None)
    if t is None:
        t = [node.target]
    return [x for x in t if isinstance(x, (ast.Name, ast.Attribute, ast.Subscript))]


def _factory_name(call):
    f = call.func
    if isinstance(f, ast.Name):
        return f.id
    if isinstance(f, ast.Attribute):
        return f.attr
    return ""


def audit_render_intermediates(src_text):
    """审计真实逐页循环：渲染中间物不得被循环外的容器/属性持有。返回违规描述列表。"""
    viols = []
    try:
        tree = ast.parse(src_text)
    except SyntaxError as e:
        return [f"源码无法解析为 AST（{e}）—— 审计失效"]

    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == "_print_pdf_native"), None)
    if fn is None:
        return ["未找到 _print_pdf_native（函数被改名/删除 ⇒ 审计目标消失）"]
    loop = next((n for n in ast.walk(fn)
                 if isinstance(n, ast.For) and isinstance(n.iter, ast.Name)
                 and n.iter.id == "render_seq"), None)
    if loop is None:
        return ["未找到 `for … in render_seq:` 逐面循环（渲染序列来源变了 ⇒ 审计目标消失）"]

    # ① 从赋值形态反推"渲染中间物"（不依赖变量名；链式传播：rendered = img）
    inter = set()
    for _ in range(4):
        for node in ast.walk(fn):
            if not isinstance(node, _ASSIGN_NODES):
                continue
            v = node.value
            is_factory = isinstance(v, ast.Call) and _factory_name(v) in _RENDER_FACTORIES
            is_chain = isinstance(v, ast.Name) and v.id in inter
            if is_factory or is_chain:
                for t in _targets_of(node):
                    if isinstance(t, ast.Name):
                        inter.add(t.id)
    if not inter:
        return ["识别不出任何渲染中间物（get_pixmap/frombuffer/Dib 的赋值形态变了 ⇒ 审计失效）"]

    # "循环体内的变量" = 循环里被**普通赋值**绑定过的名字（+已在其中绑定过的 `+=` 目标）。
    # 注意不能把 `+=` 的目标直接算进来：`_imgs += [img]`（_imgs 在循环外建）本身就在循环里，
    # 若把它当"循环局部名"就会漏掉这一类累加逃逸 —— AST 对拍用例会当场抓到。
    _plain = {t.id for n in ast.walk(loop) if isinstance(n, (ast.Assign, ast.AnnAssign))
              for t in _targets_of(n) if isinstance(t, ast.Name)}
    _aug = {t.id for n in ast.walk(loop) if isinstance(n, ast.AugAssign)
            for t in _targets_of(n) if isinstance(t, ast.Name)}
    bound_in_loop = _plain | (_aug & _plain)

    # 注：**不**单独检查"中间物在循环外绑定" —— 把 Dib 建在循环外复用是合法优化（与
    # blank_dib 同源），真正要拦的是"把渲染结果攒起来"，那一定表现为下面三类**存储**逃逸。
    for node in ast.walk(fn):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr in _CONTAINER_METHODS:
            recv = _root_name(node.func.value)
            for a in node.args:
                for nm in sorted(_names_in(a) & inter):
                    if recv and recv in bound_in_loop:
                        continue          # 每次迭代自建的局部容器，不是泄漏
                    viols.append(f"`{recv or '?'}.{node.func.attr}({nm})` 把渲染中间物攒进容器"
                                 f"（第 {node.lineno} 行）")
        if isinstance(node, _ASSIGN_NODES):
            names_in_val = _names_in(node.value) & inter
            if names_in_val:
                for t in _targets_of(node):
                    if isinstance(t, (ast.Attribute, ast.Subscript)):
                        where = ast.unparse(t) if hasattr(ast, "unparse") else "<target>"
                        for nm in sorted(names_in_val):
                            viols.append(f"`{where} = …{nm}…` 把渲染中间物存进属性/下标"
                                         f"（第 {node.lineno} 行）")
                    elif isinstance(t, ast.Name) and t.id not in bound_in_loop:
                        # 目标名不在循环体内绑定 ⇒ 是循环外/模块级变量（如 `_imgs += [img]`）
                        for nm in sorted(names_in_val):
                            viols.append(f"`{t.id} = …{nm}…` 把渲染中间物累加进逐面循环外的变量"
                                         f"（第 {node.lineno} 行）")
        if isinstance(node, ast.Return) and node.value is not None:
            for nm in sorted(_names_in(node.value) & inter):
                viols.append(f"`return …{nm}…` 把渲染中间物带出函数（第 {node.lineno} 行）")

    return viols


_cur_viols = audit_render_intermediates(src)
check("★ AST 审计：真实逐页循环不持有渲染中间物（pix/img/dib/blank_dib 及其改名变体）",
      not _cur_viols, "；".join(_cur_viols[:3]))

# ── AST 审计器**自带对拍**：喂进 6 种"逃逸"变体，每一种都必须被抓到 ──
# 没有一个的话，扫描器就可能是"永远为绿"的假守卫（正是 D1-🔴12 的教训）。
_ANCHOR = "            dib = ImageWin.Dib(img)\n"
check("对拍夹具锚点唯一（能定位到真实循环里的 Dib 构造点）", src.count(_ANCHOR) == 1)
_MUTATIONS = {
    "审查员原变异（改名 + append 到模块级列表）":
        "_keep_img = []\n" + src.replace(_ANCHOR, _ANCHOR + "            _keep_img.append(img)\n"),
    "改名后 append（rendered_im）":
        src.replace(_ANCHOR, "            rendered_im = ImageWin.Dib(img)\n"
                             "            _hold.append(rendered_im)\n"),
    "攒进 self._cache":
        src.replace(_ANCHOR, _ANCHOR + "            self._cache.append(pix)\n"),
    "存进字典下标":
        src.replace(_ANCHOR, _ANCHOR + '            _holder["img"] = img\n'),
    "augassign 累加列表":
        src.replace(_ANCHOR, _ANCHOR + "            _imgs += [img]\n"),
    "extend 列表实参（绕过 append 名字）":
        src.replace(_ANCHOR, _ANCHOR + "            _cv.imgs.extend([pix])\n"),
}
for _label, _mut in _MUTATIONS.items():
    _v = audit_render_intermediates(_mut)
    check(f"★ AST 对拍：变异「{_label}」必须被判违规", bool(_v),
          "" if _v else "审计器漏过了这一种逃逸形态 —— 它是假守卫")
check("★ AST 对拍：合法写法不误报（真实源码 / 只加一行 print(img.size)）",
      not audit_render_intermediates(src)
      and not audit_render_intermediates(src.replace("        hdc.EndDoc()",
                                                     "        print(img.size)\n        hdc.EndDoc()")))
check("★ AST 审计对「函数被改名」也报违规（防审计目标消失后空转）",
      bool(audit_render_intermediates(src.replace("def _print_pdf_native(",
                                                  "def _print_pdf_nativeX("))))

check("★ 中间物在每次迭代内重新赋值（作用域出循环即被引用计数回收）",
      "pix = page.get_pixmap(" in code_src and "img = Image.frombuffer(" in code_src
      and "dib = ImageWin.Dib(img)" in code_src)

print("\n" + "=" * 56)
print("打印侧双面适配验收:", "全部通过 ✅" if ok_all else "存在失败 ❌")
sys.exit(0 if ok_all else 1)
