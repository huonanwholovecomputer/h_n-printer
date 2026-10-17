# -*- coding: utf-8 -*-
"""虚拟打印机（Microsoft Print to PDF 等，无纸输出）× 双面 验收

背景（2026-10 现场）：本地工具默认打印机设成 **Microsoft Print to PDF**、打含双面文件的
订单时，每个文件都被拒：

    拒绝双面打印: 该打印机不支持自动双面（无自动双面（能力位=0 且驱动把 Duplex 改回单面））
                 ：Microsoft Print to PDF。请改用单面打印…

根因：拦截规则是「能力位=0 且驱动把 Duplex 改回 1 ⇒ 硬拒」。那条规则是为**真实机型**设计的
（防"按双面计费却单面出纸"）。虚拟打印机**不出纸**，Duplex 对它没有物理意义、也不浪费纸，
一律按真机型拒掉 ⇒ 开发/测试根本没法用它跑通含双面文件的流程。

本脚本钉住四件事（全部不出纸，不需要真打印机）：

  ① **识别**：`is_virtual_printer` 认得出 Print to PDF / XPS / OneNote / Adobe PDF /
     「导出为 WPS PDF」（队列名、驱动名、端口名三种证据），且**不误伤**
     HP LaserJet 1020 / M232 这类真机型；
  ② **能力字段照实报**：虚拟打印机的探测结果仍是 `auto_duplex=False / confidence=high`，
     只多一个 `virtual=True` —— 不得谎报"支持双面"（verify_duplex_probe.py ⑥ 按真实队列
     逐台核对 auto_duplex，谎报会当场转红）；
  ③ **A/B 对拍**（假 GDI + 假驱动"把 Duplex 改回 1"）：虚拟打印机 → 照常逐面出纸、报成功并
     在消息里注明"双面为空操作"；真机型 → 仍然硬失败 + `DUPLEX_UNSUPPORTED_MARKER`，
     且失败发生在 StartDoc 之前（零回归）；
  ④ **预检放行**：`print_pdf` 里虚拟打印机不再被拒（GDI 会被调用），真机型 high 置信度不支持
     仍然拒打（GDI 不被调用，不浪费纸）。

用法: python tests/verify_virtual_printer_duplex.py   （exit 0 = 全部通过）
"""
import io
import os
import sys
import types

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL_TOOL = os.path.join(REPO, "local_print_tool")
sys.path.insert(0, LOCAL_TOOL)

import win32con  # noqa: E402  （真实常量模块：LOGPIXELSX / HORZRES / DM_* 等）
import pdf_printer as pp  # noqa: E402
import PIL  # noqa: E402
import PIL.Image  # noqa: E402
import fitz as real_fitz  # noqa: E402

ok_all = True


def check(name, cond, detail=""):
    global ok_all
    if not cond:
        ok_all = False
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


def reset_caches():
    """清掉探测/身份缓存 —— 假驱动与真实队列交替出现时必须清，否则缓存键会串味。"""
    pp._DUPLEX_CAP_CACHE.clear()
    pp._IDENTITY_CACHE.clear()


# ============================================================
# ① 虚拟打印机识别（纯函数；三种证据：队列名 / 驱动名 / 端口名）
# ============================================================
print("\n=== ① 虚拟打印机识别（本机实测的队列都列进来）===")
CASES = [
    (("Microsoft Print to PDF", "Microsoft Print To PDF", "PORTPROMPT:"), True, "队列名+驱动+端口"),
    (("Microsoft Print to PDF", "", ""), True, "只给队列名（不查驱动也能判）"),
    (("microsoft print TO pdf", "", ""), True, "大小写不敏感"),
    (("Microsoft XPS Document Writer", "Microsoft XPS Document Writer", "PORTPROMPT:"), True, ""),
    (("OneNote (Desktop)", "Send to Microsoft OneNote 16 Driver", "nul:"), True, "端口 nul: 也算证据"),
    (("导出为WPS PDF", "Kingsoft Virtual Printer Driver", "Kingsoft Virtual Printer Port"), True,
     "队列名不含关键字 → 靠驱动/端口的 virtual printer"),
    (("Adobe PDF", "Adobe PDF Converter", "Documents\\*.pdf"), True, "端口就是 .pdf 路径"),
    (("Some Queue", "", "FILE:"), True, "端口 FILE:（打印到文件）"),
    # ↓↓↓ 真机型：一个都不许误判（误判 = 真表单面出纸却报成功，正是要防的假成功）
    (("HP LaserJet 1020", "HP LaserJet 1020", "USB005"), False, "真机型（手动双面）"),
    (("HP LaserJet MFP M232-M237", "HP LaserJet MFP M232-M237 PCLm-S", "USB001"), False,
     "真机型（自动双面）"),
    (("HP LaserJet MFP M232-M237", "HP LaserJet MFP M232-M237 PCLm-S", "192.168.1.20"), False, "网络端口"),
    (("", "", ""), False, "空队列名"),
]
for (nm, drv, port), want, why in CASES:
    got = pp.is_virtual_printer(nm, drv, port)
    check(f"is_virtual_printer({nm or '“”'!r}) → {want}" + (f"  [{why}]" if why else ""),
          got is want, f"got={got} driver={drv!r} port={port!r}")


# ============================================================
# 假 win32print：只为能力探测/身份查询服务（不打印任何东西）
# ============================================================
def install_fake_win32print(*, driver, port, dc_duplex, reverts, log=None):
    saved = sys.modules.get("win32print")
    wpr = types.ModuleType("win32print")

    class _DM:
        Duplex = 1
        Fields = 0
        Orientation = 1
        Color = 2          # DMCOLOR_COLOR：本测试验证双面闸门，强制走 RGB 位图路径

    def _open(name):
        if log is not None:
            log.append(name)
        return f"h:{name}"

    def _getprinter(handle, level):
        return {"pDevMode": _DM(), "pDriverName": driver, "pPortName": port}

    def _document_properties(hwnd, handle, name, out, inp, flags):
        # 模拟"无自动双面"的驱动：合并时把 Duplex 归一化回 SIMPLEX
        if reverts and getattr(inp, "Duplex", 1) in (2, 3):
            inp.Duplex = 1
        return 1

    wpr.OpenPrinter = _open
    wpr.GetPrinter = _getprinter
    wpr.DocumentProperties = _document_properties
    wpr.ClosePrinter = lambda h: None
    wpr.GetDefaultPrinter = lambda: "Fake Printer"
    wpr.DeviceCapabilities = lambda dev, port_, cap: (0 if dc_duplex == 0 else 1)

    sys.modules["win32print"] = wpr

    def restore():
        if saved is None:
            sys.modules.pop("win32print", None)
        else:
            sys.modules["win32print"] = saved

    return restore


# ============================================================
# ② 能力探测：照实报 auto_duplex，另带 virtual 标记（不谎报"支持双面"）
# ============================================================
print("\n=== ② 能力探测：auto_duplex 照实报 + virtual 单独标记 ===")
reset_caches()
restore = install_fake_win32print(driver="Microsoft Print To PDF", port="PORTPROMPT:",
                                  dc_duplex=0, reverts=True)
try:
    v = pp.get_printer_duplex_support("Microsoft Print to PDF", refresh=True)
finally:
    restore()
check("★ 虚拟打印机：能力字段与真机型一致地报 False/high（不谎报支持双面）",
      v["auto_duplex"] is False and v["confidence"] == "high" and v["dm_accepted"] is False,
      f"auto={v['auto_duplex']}/{v['confidence']} acc={v['dm_accepted']}")
check("★ 同一次探测带 virtual=True（拦截点据此区分「无意义」与「做不到」）",
      v["virtual"] is True, f"{v}")
check("detail 里写明「双面为空操作、按请求放行」（日志/诊断可读）",
      "虚拟打印机" in v["detail"] and "空操作" in v["detail"], v["detail"])

reset_caches()
restore = install_fake_win32print(driver="HP LaserJet 1020", port="USB005",
                                  dc_duplex=0, reverts=True)
try:
    v = pp.get_printer_duplex_support("HP LaserJet 1020", refresh=True)
finally:
    restore()
check("★ 真机型（HP 1020）同样条件：virtual=False，结论不变（零回归）",
      v["virtual"] is False and v["auto_duplex"] is False and v["confidence"] == "high",
      f"virtual={v['virtual']} auto={v['auto_duplex']}/{v['confidence']}")

# 驱动/端口才能判的队列（队列名里没有关键字）
reset_caches()
restore = install_fake_win32print(driver="Kingsoft Virtual Printer Driver",
                                  port="Kingsoft Virtual Printer Port",
                                  dc_duplex=0, reverts=True)
try:
    v = pp.get_printer_duplex_support("导出为WPS PDF", refresh=True)
finally:
    restore()
check("★ 靠驱动/端口判定的队列也会被标 virtual（导出为WPS PDF）", v["virtual"] is True, f"{v}")

# 身份缓存：同名重复查询只 OpenPrinter 一次
reset_caches()
log = []
restore = install_fake_win32print(driver="HP LaserJet 1020", port="USB005",
                                  dc_duplex=0, reverts=True, log=log)
try:
    pp.is_virtual_printer("HP LaserJet 1020")
    n1 = len(log)
    pp.is_virtual_printer("HP LaserJet 1020")
    n2 = len(log)
finally:
    restore()
check("驱动/端口查询按队列名缓存（第二次不再 OpenPrinter）",
      n1 == 1 and n2 == 1, f"OpenPrinter 次数 {n1} → {n2}")
reset_caches()


# ============================================================
# 假 GDI 环境：假 DC + 假页 + 假绘图，记录"到底出没出纸"
# ============================================================
class Rec:
    def __init__(self):
        self.startdoc = 0
        self.pages = 0
        self.abort = 0


class FakePixmap:
    def __init__(self, w, h):
        self.width, self.height = w, h
        self.samples = b"\x00" * (w * h * 3)
        self.stride = w * 3


class FakeRect:
    def __init__(self, w, h):
        self.width, self.height = w, h


class FakePage:
    def __init__(self, idx):
        self.idx = idx
        self.rect = FakeRect(595.0, 842.0)      # A4 pt

    def get_pixmap(self, matrix=None, colorspace=3, alpha=False):
        return FakePixmap(100, 140)


class FakeDoc:
    def __init__(self, n):
        self.pages = [FakePage(i) for i in range(n)]

    def __len__(self):
        return len(self.pages)

    def __getitem__(self, i):
        return self.pages[i]

    def close(self):
        pass


class FakeDC:
    def __init__(self, rec):
        self.rec = rec

    def CreatePrinterDC(self, name):
        self.printer = name

    def GetHandleOutput(self):
        return 1

    def GetDeviceCaps(self, which):
        return {win32con.LOGPIXELSX: 300, win32con.LOGPIXELSY: 300,
                win32con.HORZRES: 2480, win32con.VERTRES: 3508}[which]

    def StartDoc(self, title):
        self.rec.startdoc += 1

    def StartPage(self):
        pass

    def EndPage(self):
        self.rec.pages += 1

    def EndDoc(self):
        pass

    def AbortDoc(self):
        self.rec.abort += 1

    def DeleteDC(self):
        pass


class _FakeImg:
    def __init__(self, kind, size=None, rotated=0):
        self.kind, self.size, self.rotated = kind, size, rotated

    def transpose(self, code):
        return _FakeImg(self.kind, self.size, (self.rotated + 180) % 360)


def install_fake_gdi(rec, n_pages, *, printer, driver, port, reverts=True):
    """注入整套假模块（win32print/win32ui/win32gui/fitz/PIL），返回 restore 回调。"""
    saved = {k: sys.modules.get(k) for k in
             ("win32print", "win32ui", "win32gui", "fitz", "PIL.Image", "PIL.ImageWin")}
    saved_pil_image, saved_pil_imagewin = PIL.Image, getattr(PIL, "ImageWin", None)

    wpr = types.ModuleType("win32print")

    class _DM:
        Duplex = 1
        Fields = 0
        Orientation = 1
        Color = 2          # DMCOLOR_COLOR：本测试验证双面闸门，强制走 RGB 位图路径

    def _document_properties(hwnd, handle, name, out, inp, flags):
        if reverts and getattr(inp, "Duplex", 1) in (2, 3):
            inp.Duplex = 1
        return 1

    wpr.OpenPrinter = lambda name: f"h:{name}"
    wpr.GetPrinter = lambda handle, level: {"pDevMode": _DM(),
                                            "pDriverName": driver, "pPortName": port}
    wpr.DocumentProperties = _document_properties
    wpr.ClosePrinter = lambda h: None
    wpr.GetDefaultPrinter = lambda: printer
    wpr.DeviceCapabilities = lambda dev, port_, cap: (0 if reverts else 1)

    wui = types.ModuleType("win32ui")
    wui.CreateDC = lambda: FakeDC(rec)

    wgui = types.ModuleType("win32gui")
    wgui.ResetDC = lambda hdc, dm: 1

    fm = types.ModuleType("fitz")

    class _Matrix:
        def __init__(self, x, y):
            self.x, self.y = x, y

    fm.Matrix = _Matrix
    fm.csGRAY = 1               # 渲染策略用到的常量（真实 fitz 里分别是 1/3）
    fm.csRGB = 3
    fm.open = lambda path: FakeDoc(n_pages)

    pim = types.ModuleType("PIL.Image")
    pim.Transpose = types.SimpleNamespace(ROTATE_180="ROT180")
    pim.frombuffer = lambda mode, size, samples, raw, rgb, stride, x: _FakeImg("IMAGE", size)
    pim.new = lambda mode, size, color: _FakeImg("BLANK", size)

    piw = types.ModuleType("PIL.ImageWin")

    class _Dib:
        def __init__(self, img):
            self.img = img

        def draw(self, hdc, rect):
            pass

    piw.Dib = _Dib

    sys.modules["win32print"] = wpr
    sys.modules["win32ui"] = wui
    sys.modules["win32gui"] = wgui
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
# ③ A/B 对拍：同一台"把 Duplex 改回 1"的假驱动
# ============================================================
print("\n=== ③ A/B：虚拟打印机放行 / 真机型仍硬失败（同一假驱动）===")
reset_caches()
rec = Rec()
restore = install_fake_gdi(rec, 4, printer="Microsoft Print to PDF",
                           driver="Microsoft Print To PDF", port="PORTPROMPT:")
try:
    ok, msg = pp._print_pdf_native("dummy.pdf", "Microsoft Print to PDF", "on",
                                   "long-edge", "", 1, "portrait")
finally:
    restore()
check("★ 虚拟打印机请求双面 → 照常出纸并报成功（不再拒打）",
      ok is True and rec.startdoc == 1 and rec.pages == 4 and rec.abort == 0,
      f"ok={ok} startdoc={rec.startdoc} pages={rec.pages} msg={msg}")
check("成功消息注明「虚拟打印机：双面为空操作」（不让人误以为真双面了）",
      "虚拟打印机" in msg, msg)

reset_caches()
rec = Rec()
restore = install_fake_gdi(rec, 4, printer="HP LaserJet 1020",
                           driver="HP LaserJet 1020", port="USB005")
try:
    ok, msg = pp._print_pdf_native("dummy.pdf", "HP LaserJet 1020", "on",
                                   "long-edge", "", 1, "portrait")
finally:
    restore()
check("★ 真机型（HP 1020）同样条件仍然硬失败（零回归）",
      ok is False and msg.startswith(pp.DUPLEX_UNSUPPORTED_MARKER), f"ok={ok} msg={msg}")
check("★ 真机型仍是在 StartDoc 之前就拦住（一个面都没出纸）",
      rec.startdoc == 0 and rec.pages == 0, f"startdoc={rec.startdoc} pages={rec.pages}")

reset_caches()
rec = Rec()
restore = install_fake_gdi(rec, 3, printer="Microsoft Print to PDF",
                           driver="Microsoft Print To PDF", port="PORTPROMPT:")
try:
    ok, msg = pp._print_pdf_native("dummy.pdf", "Microsoft Print to PDF", "off",
                                   "long-edge", "", 1, "portrait")
finally:
    restore()
check("虚拟打印机打单面照常（消息里不出现「空操作」字样）",
      ok is True and rec.pages == 3 and "虚拟打印机" not in msg, f"ok={ok} pages={rec.pages} msg={msg}")
reset_caches()


# ============================================================
# ④ print_pdf 预检：虚拟打印机放行 / 真机型仍拒
# ============================================================
print("\n=== ④ print_pdf 预检（真 fitz 造 PDF，原生打印被桩替换）===")
tmp_pdf = os.path.join(os.environ.get("TEMP", "."), "hn_virtual_printer_test.pdf")
_d = real_fitz.open()
for _ in range(3):
    _d.new_page()
_d.save(tmp_pdf)
_d.close()

VIRTUAL_VERDICT = {
    "auto_duplex": False, "confidence": "high", "printer": "Microsoft Print to PDF",
    "driver": "Microsoft Print To PDF", "port": "PORTPROMPT:", "dc_duplex": 0,
    "dm_accepted": False, "virtual": True,
    "detail": "无自动双面（能力位=0 且驱动把 Duplex 改回单面）；虚拟打印机（无纸输出）⇒ 双面为空操作，按请求放行",
}
REAL_VERDICT = {
    "auto_duplex": False, "confidence": "high", "printer": "HP LaserJet 1020",
    "driver": "HP LaserJet 1020", "port": "USB005", "dc_duplex": 0, "dm_accepted": False,
    "virtual": False, "detail": "无自动双面（能力位=0 且驱动把 Duplex 改回单面）",
}

calls = []
saved_native, saved_probe = pp._print_pdf_native, pp.get_printer_duplex_support
pp._print_pdf_native = lambda *a, **kw: (calls.append(a[1] if len(a) > 1 else kw.get("printer_name")),
                                         (True, "stub"))[1]
try:
    pp.get_printer_duplex_support = lambda *a, **kw: dict(VIRTUAL_VERDICT)
    ok, msg = pp.print_pdf(tmp_pdf, "Microsoft Print to PDF", duplex="on")
    check("★ 预检放行虚拟打印机（GDI 被调用、整单成功）",
          ok is True and calls == ["Microsoft Print to PDF"], f"ok={ok} calls={calls}")

    # 旧版/外部 verdict 没有 virtual 键时，靠队列名兜底也要放行
    pp.get_printer_duplex_support = lambda *a, **kw: {
        "auto_duplex": False, "confidence": "high", "printer": "Microsoft Print to PDF",
        "detail": "无自动双面"}
    calls.clear()
    ok, msg = pp.print_pdf(tmp_pdf, "Microsoft Print to PDF", duplex="on")
    check("verdict 没带 virtual 键时，仍按队列名认出来并放行",
          ok is True and calls == ["Microsoft Print to PDF"], f"ok={ok} calls={calls}")

    pp.get_printer_duplex_support = lambda *a, **kw: dict(REAL_VERDICT)
    calls.clear()
    ok, msg = pp.print_pdf(tmp_pdf, "HP LaserJet 1020", duplex="on")
    check("★ 真机型 high 置信度不支持 → 仍然拒打，且 GDI 未被调用（没浪费纸）",
          ok is False and "不支持自动双面" in msg and calls == [], f"ok={ok} calls={calls}")
    check("拒打文案仍给出出路（改单面 / 走手动双面）",
          "单面" in msg and "手动双面" in msg, msg)

    calls.clear()
    ok, msg = pp.print_pdf(tmp_pdf, "Microsoft Print to PDF", duplex="off")
    check("虚拟打印机打单面也不受影响（预检不介入）",
          ok is True and calls == ["Microsoft Print to PDF"], f"ok={ok} calls={calls}")
finally:
    pp._print_pdf_native, pp.get_printer_duplex_support = saved_native, saved_probe
    if os.path.isfile(tmp_pdf):
        os.remove(tmp_pdf)


# ============================================================
# ⑤ 本机真实队列抽查（有 Microsoft Print to PDF 就顺手验一次）
# ============================================================
print("\n=== ⑤ 本机真实队列抽查（只探测，不打印）===")
try:
    import win32print as real_wpr
    flags = real_wpr.PRINTER_ENUM_LOCAL | real_wpr.PRINTER_ENUM_CONNECTIONS
    rows = real_wpr.EnumPrinters(flags, None, 2)
    names = [i["pPrinterName"] for i in rows]
    virtual_names = [n for n in names if pp.is_virtual_printer(n)]
    real_names = [n for n in names if not pp.is_virtual_printer(n)]
    print(f"  本机队列：虚拟 {virtual_names}")
    print(f"          真机 {real_names}")
    check("本机所有队列都能被明确分类（虚拟/真机，无中间态）",
          len(virtual_names) + len(real_names) == len(names), f"{len(names)} 台")
    check("★ 真机型队列一个都没被误判为虚拟打印机",
          all(not pp.is_virtual_printer(n) for n in real_names),
          f"{real_names}")
    if "Microsoft Print to PDF" in names:
        reset_caches()
        v = pp.get_printer_duplex_support("Microsoft Print to PDF", refresh=True)
        check("★ 本机 Microsoft Print to PDF 实测：virtual=True 且不谎报 auto_duplex",
              v["virtual"] is True and v["auto_duplex"] in (False, None),
              f"virtual={v['virtual']} auto={v['auto_duplex']}/{v['confidence']} detail={v['detail']}")
    else:
        print("  （本机没有 Microsoft Print to PDF，跳过实测）")
except ImportError:
    print("  （无 pywin32，跳过真实队列抽查）")

print("\n" + "=" * 56)
print("虚拟打印机 × 双面 验收:", "全部通过 ✅" if ok_all else "存在失败 ❌")
sys.exit(0 if ok_all else 1)
