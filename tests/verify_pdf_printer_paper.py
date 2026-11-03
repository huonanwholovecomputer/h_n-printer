"""验收：pdf_printer 纸张大小（需求 #11「纸张选项跨所有文件类型」）。

不碰真打印机：用假的 win32print / win32con 模块注入 sys.modules，只验证
 ① normalize_paper_name 的归一化规则；
 ② _get_printer_devmode 把 dmPaperSize / DM_PAPERSIZE 写进 DEVMODE 并交给
    DocumentProperties 的 dmFields（不置位的话驱动会忽略纸张设置）；
 ③ DocumentProperties 校验后回读 applied_paper 时能发现"驱动改回默认纸"；
 ④ print_pdf / print_plan_pass / _print_pdf_native / _print_via_loop 都带 paper 参数，
    且 print_plan_pass 会把它透传下去。

跑法：PYTHONPATH= PYTHONIOENCODING=utf-8 <python> tests/verify_pdf_printer_paper.py
"""
import inspect
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "local_print_tool"))

import pdf_printer  # noqa: E402


FAILS = []


def check(cond, label):
    if cond:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}")
        FAILS.append(label)


# ── ① 纸张名归一化 ────────────────────────────────────────────────
print("[1] normalize_paper_name")
cases = [
    ("A4", "A4"), ("a4", "A4"), (" A4 ", "A4"), ("A4(210x297)", "A4"),
    ("A4纸", "A4"), ("A3", "A3"), ("A5", "A5"), ("Letter", "LETTER"),
    ("legal", "LEGAL"), ("B5", "B5"), ("", ""), (None, ""), ("A2", ""), ("随便", ""),
]
for raw, want in cases:
    got = pdf_printer.normalize_paper_name(raw)
    check(got == want, f"normalize_paper_name({raw!r}) == {want!r} (got {got!r})")

check(pdf_printer.DMPAPER_BY_NAME == {
    "A4": 9, "A3": 8, "A5": 11, "LETTER": 1, "LEGAL": 5, "B5": 13,
}, "DMPAPER 映射表 = 标准值")

# ── ② / ③ 用假 win32 模块验证 DEVMODE 写入 ──────────────────────────
class FakeDevMode:
    def __init__(self):
        self.Duplex = 1
        self.Orientation = 1
        self.PaperSize = 9
        self.Fields = 0


def install_fake_win32(doc_props_result=1, force_paper=None):
    """注入假模块；返回记录 DocumentProperties 调用的列表。"""
    calls = []

    win32con = types.SimpleNamespace(
        DM_DUPLEX=0x00040000, DM_ORIENTATION=0x00000001 << 0,  # 值不重要，只看位
        DM_PAPERSIZE=0x00000001,
        DM_IN_BUFFER=8, DM_OUT_BUFFER=2,
        DMORIENT_PORTRAIT=1, DMORIENT_LANDSCAPE=2,
    )
    # 让 DM_ORIENTATION 与 DM_PAPERSIZE 不撞位，便于断言
    win32con.DM_ORIENTATION = 0x1000

    win32print = types.SimpleNamespace(
        OpenPrinter=lambda name: object(),
        ClosePrinter=lambda h: None,
        GetPrinter=lambda h, lvl: {"pDevMode": FakeDevMode()},
    )

    def DocumentProperties(hwnd, handle, name, out_dev, in_dev, flags):
        calls.append({"devmode": in_dev, "flags": flags})
        if force_paper is not None:
            in_dev.PaperSize = force_paper
        return doc_props_result

    win32print.DocumentProperties = DocumentProperties
    sys.modules["win32print"] = win32print
    sys.modules["win32con"] = win32con
    return calls


print("[2] _get_printer_devmode 写入 dmPaperSize/DM_PAPERSIZE")
calls = install_fake_win32()
dev, applied_duplex = pdf_printer._get_printer_devmode("Fake", "off", "long-edge", "portrait", "A3")
check(dev is not None, "返回 devmode 非空")
check(dev.PaperSize == 8, f"PaperSize 写成 A3(8)，实际 {getattr(dev, 'PaperSize', None)}")
check(bool(dev.Fields & sys.modules["win32con"].DM_PAPERSIZE), "dmFields 置了 DM_PAPERSIZE")
check(bool(calls) and bool(calls[-1]["flags"] & sys.modules["win32con"].DM_PAPERSIZE),
      "DocumentProperties 的 dmFields 带 DM_PAPERSIZE（否则驱动忽略纸张设置）")

print("[2b] paper 为空时不改纸张")
calls = install_fake_win32()
dev, _ = pdf_printer._get_printer_devmode("Fake", "off", "long-edge", "", "")
check(dev.PaperSize == 9, "空 paper 保持驱动默认（假 devmode 初始 9）")
check(not (calls[-1]["flags"] & sys.modules["win32con"].DM_PAPERSIZE),
      "空 paper 不改 dmFields 的 DM_PAPERSIZE 位")

print("[2c] 不认识的 paper 不写、不崩")
calls = install_fake_win32()
dev, _ = pdf_printer._get_printer_devmode("Fake", "off", "long-edge", "", "A2")
check(dev.PaperSize == 9, "未知纸型沿用默认值")
check(not (calls[-1]["flags"] & sys.modules["win32con"].DM_PAPERSIZE),
      "未知纸型不置 DM_PAPERSIZE 位")

print("[3] 驱动改回默认纸时能被发现（只影响日志，不阻断打印）")
calls = install_fake_win32(force_paper=9)
dev, _ = pdf_printer._get_printer_devmode("Fake", "off", "long-edge", "", "A3")
check(dev.PaperSize == 9, "被驱动改回 9 后回读到的就是 9（调用方据此报警）")

# ── ④ 参数链 ─────────────────────────────────────────────────────
print("[4] paper 参数链")
for fn, name in [
    (pdf_printer.print_pdf, "print_pdf"),
    (pdf_printer.print_plan_pass, "print_plan_pass"),
    (pdf_printer._print_pdf_native, "_print_pdf_native"),
    (pdf_printer._print_via_loop, "_print_via_loop"),
]:
    params = inspect.signature(fn).parameters
    check("paper" in params, f"{name} 有 paper 参数")
    check(params["paper"].default == "", f"{name}.paper 默认 ''（向后兼容：沿用驱动默认纸）")

src = inspect.getsource(pdf_printer.print_plan_pass)
check("paper=paper" in src, "print_plan_pass 把 paper 透传给 print_pdf")

# 清理假模块，避免污染同进程后续测试
for mod in ("win32print", "win32con"):
    sys.modules.pop(mod, None)

print()
if FAILS:
    print(f"FAILED: {len(FAILS)} 项")
    for f in FAILS:
        print(f"  - {f}")
    sys.exit(1)
print("ALL PASS: pdf_printer 纸张大小")
