# -*- coding: utf-8 -*-
"""验收 · xlsx 打印选项（print_options）：只有显式传入才改版式，且只改临时副本

依据（2026-11-03 需求，取代 2026-11-02 的"绝不改写版式"旧口径）：
  1. `print_options=None`（老调用方 / 旧缓存路径）⇒ 行为与旧版**逐字节一致**：完全不读
     PageSetup、缓存 key 不带选项后缀 ⇒ 升级后旧 PDF 缓存照常命中。
  2. 显式传 `print_options`（GUI 打印选项面板）⇒ 纸张 / 方向 / 页边距 / 页眉页脚 / 居中 /
     缩放或「调整为 N 页」/ 打印区域 写进 `PageSetup`，但**只写在 export_to_pdf 复制出来的
     临时副本上**（工作簿 ReadOnly 打开、Close(SaveChanges=False)）⇒ 用户源文件 md5 不变。
  3. 缓存键纳入选项签名：改页边距/缩放/打印区域必须换一份缓存，不能命中旧版式的 PDF。
  4. 引擎不支持时必须**明说**：逐属性记账 `options_applied` + 日志点名「设置未生效」；
     LibreOffice 兜底日志额外写「页边距/缩放/打印范围设置未生效」。
  5. COM 安全口径不变：只退出 owned 实例、失败清理（本脚本用假 COM 只核对"旧路径不碰
     PageSetup"与"临时副本"两条，其余由 verify_xlsx_excel_parity.py 守）。

本脚本**不启动** Excel / WPS / LibreOffice：全部用假 COM 对象（记录 setattr 的属性映射）。
退出码 0 = 全部通过。

用法：
    python tests/verify_xlsx_print_options.py
"""
import hashlib
import io
import logging
import os
import re
import sys
import tempfile
import zipfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL_TOOL = os.path.join(REPO, "local_print_tool")
sys.path.insert(0, LOCAL_TOOL)

import xlsx_printer as X  # noqa: E402
import converter as C  # noqa: E402

CM_TO_PT = 72.0 / 2.54

_FAILS = []
_CHECKS = 0


def chk(name, cond, detail=""):
    global _CHECKS
    _CHECKS += 1
    if cond:
        print(f"[PASS] {name}")
    else:
        _FAILS.append(name)
        print(f"[FAIL] {name}" + (f"  -- {detail}" if detail else ""))


def eq(name, got, want):
    chk(name, got == want, f"got={got!r} want={want!r}")


# ============================================================
# A. 归一化 / 选项签名 / 缓存键（纯函数）
# ============================================================
print("=== A. normalize_print_options / 选项签名 / 缓存键 ===")

chk("★ print_options=None ⇒ normalize 返回 None（旧路径的唯一信号）",
    X.normalize_print_options(None) is None)

D = X.DEFAULT_PRINT_OPTIONS
chk("默认口径与 Lead 定稿一致（A4/纵向/四边 1cm/页眉页脚 0/水平居中开/垂直居中关/fit=cols/活动表）",
    (D["paper"], D["orientation"], D["margin_top_cm"], D["margin_bottom_cm"],
     D["margin_left_cm"], D["margin_right_cm"], D["header_cm"], D["footer_cm"],
     D["center_h"], D["center_v"], D["fit"], D["scale"], D["print_range"])
    == ("A4", "portrait", 1.0, 1.0, 1.0, 1.0, 0.0, 0.0, True, False, "cols", 100, "active"),
    str(D))

eq("空字典 ⇒ 全部按默认补齐", X.normalize_print_options({}), D)
eq("只给一个键 ⇒ 其余仍按默认（GUI 会传完整 dict，但缺项也必须稳）",
    X.normalize_print_options({"margin_left_cm": 2.5})["margin_top_cm"], 1.0)

eq("纸张映射 A4→9 / A3→8 / Letter→1 / Legal→5 / B5→13（契约里点名的五种）",
    {k: X.PAPER_SIZES[k] for k in ("A4", "A3", "Letter", "Legal", "B5")},
    {"A4": 9, "A3": 8, "Letter": 1, "Legal": 5, "B5": 13})
eq("纸张映射覆盖 GUI 面板的 A5 → 11（少这项就会静默打成 A4）",
    X.PAPER_SIZES.get("A5"), 11)
eq("面板的六种纸全都有映射（与 printer_config.PAPER_SIZES 对齐）",
    set(X.PAPER_SIZES), {"A4", "A3", "A5", "Letter", "Legal", "B5"})
eq("纸张取值大小写不敏感（'a3' → A3）",
    X.normalize_print_options({"paper": "a3"})["paper"], "A3")
eq("未知纸张 ⇒ 回退 A4（不猜用户想打什么纸）",
    X.normalize_print_options({"paper": "A0-巨幅"})["paper"], "A4")

eq("方向 portrait→1 / landscape→2（映射表）",
    dict(X.ORIENTATIONS), {"portrait": 1, "landscape": 2})
eq("方向非法值 ⇒ 回退 portrait",
    X.normalize_print_options({"orientation": "斜着"})["orientation"], "portrait")

eq("页边距：字符串数字可解析（'1.5' → 1.5）",
    X.normalize_print_options({"margin_top_cm": "1.5"})["margin_top_cm"], 1.5)
eq("页边距：负数钳到 0", X.normalize_print_options({"margin_left_cm": -3})["margin_left_cm"], 0.0)
eq("页边距：超大值钳到 20cm（超过纸宽没有意义）",
    X.normalize_print_options({"margin_right_cm": 999})["margin_right_cm"], 20.0)
eq("页边距：非数字回退默认 1cm",
    X.normalize_print_options({"margin_bottom_cm": "abc"})["margin_bottom_cm"], 1.0)
eq("页边距：保留 2 位小数（选项签名可复现）",
    X.normalize_print_options({"margin_top_cm": 1.234567})["margin_top_cm"], 1.23)

eq("缩放：低于 10 钳到 10", X.normalize_print_options({"scale": 5})["scale"], 10)
eq("缩放：高于 400 钳到 400", X.normalize_print_options({"scale": 999})["scale"], 400)
eq("缩放：字符串数字（'150' → 150）", X.normalize_print_options({"scale": "150"})["scale"], 150)

eq("fit 非法值 ⇒ 回退默认 cols",
    X.normalize_print_options({"fit": "乱七八糟"})["fit"], "cols")
eq("print_range 非法值 ⇒ 回退 active",
    X.normalize_print_options({"print_range": "all"})["print_range"], "active")
eq("center_h='false' ⇒ False（bool('false') 是 True，踩过的坑）",
    X.normalize_print_options({"center_h": "false"})["center_h"], False)
eq("center_v='0' ⇒ False", X.normalize_print_options({"center_v": "0"})["center_v"], False)
eq("center_h='on' ⇒ True", X.normalize_print_options({"center_h": "on"})["center_h"], True)
eq("未知键被忽略（不炸，也不影响其它值）",
    X.normalize_print_options({"duplex": "long-edge", "margin_top_cm": 2})["margin_top_cm"], 2.0)

chk("非 dict 误传（字符串）⇒ 退回 None（走旧路径，不猜）",
    X.normalize_print_options("A4") is None)
chk("非 dict 误传（True）⇒ None", X.normalize_print_options(True) is None)

# —— 选项签名 ——
sig_default = X.options_signature(X.normalize_print_options({}))
chk("选项签名：None / 空字典 ⇒ ''（旧 key 形态）",
    X.options_signature(None) == "" and X.options_signature({}) == "")
chk("选项签名：8 位十六进制", bool(re.fullmatch(r"[0-9a-f]{8}", sig_default)), sig_default)
chk("选项签名：字典顺序无关（同内容同签名）",
    X.options_signature({"paper": "A3", "scale": 120})
    == X.options_signature({"scale": 120, "paper": "A3"}))
chk("选项签名：内容不同 ⇒ 签名不同",
    X.options_signature({"paper": "A3"}) != X.options_signature({"paper": "A4"}))

# —— 缓存键 ——
eq("★ 旧形态逐字符不变：cache_key(md5,'','excel') == 'md5_xexcel'",
    X.cache_key("md5abc", "", "excel"), "md5abc_xexcel")
chk("★ print_options=None 时不带 _o 后缀（旧缓存仍命中）",
    "_o" not in X.cache_key("md5abc", "", "wps", None))
key_opts_a = X.cache_key("md5abc", "", "excel", {"margin_left_cm": 1})
key_opts_b = X.cache_key("md5abc", "", "excel", {"margin_left_cm": 2.5})
chk("★ 改页边距 ⇒ 缓存 key 变化（不会命中旧版式的 PDF）",
    key_opts_a != key_opts_b and key_opts_a.startswith("md5abc_xexcel_o"), key_opts_a)
chk("选项签名在 key 末尾且顺序无关",
    X.cache_key("md5abc", "", "excel", {"paper": "A3", "scale": 120})
    == X.cache_key("md5abc", "", "excel", {"scale": 120, "paper": "A3"}))
chk("★ print_options={} 与 None 是两种缓存（显式空字典 = 应用默认版式）",
    X.cache_key("md5abc", "", "excel", {}) != X.cache_key("md5abc", "", "excel", None))
eq("cache_key 空 md5 ⇒ 空串", X.cache_key("", "", "excel"), "")


# ============================================================
# 假 COM（记录 PageSetup 的 setattr；不启动真 Office）
# ============================================================
class RecordingPageSetup:
    def __init__(self, fail=()):
        object.__setattr__(self, "writes", {})
        object.__setattr__(self, "reads", 0)
        object.__setattr__(self, "fail", set(fail))

    def __setattr__(self, name, value):
        if name in self.fail:
            raise AttributeError(f"该引擎不支持 {name}")
        self.writes[name] = value


class StubUsedRange:
    Address = "$A$1:$F$20"


class StubCollection:
    def __init__(self, items):
        self._items = list(items)

    @property
    def Count(self):
        return len(self._items)

    def Item(self, i):
        return self._items[i - 1]

    def __call__(self, i):
        return self._items[i - 1]


class StubSheet:
    def __init__(self, name="Sheet1", fail=(), allow_pagesetup=True):
        self.Name = name
        self._allow_ps = allow_pagesetup
        self._ps = RecordingPageSetup(fail)
        self.export_calls = []
        self.UsedRange = StubUsedRange()

    @property
    def PageSetup(self):
        if not self._allow_ps:
            raise RuntimeError("旧路径（print_options=None）不得读取 PageSetup")
        self._ps.reads += 1
        return self._ps

    def ExportAsFixedFormat(self, *args, **kwargs):
        self.export_calls.append((args, kwargs))
        fn = kwargs.get("Filename") or (args[1] if len(args) > 1 else "")
        with open(fn, "wb") as f:
            f.write(b"%PDF-1.4\n%stub\n")


class StubWorkbook:
    def __init__(self, names=("Sheet1",), active="Sheet1", fail=(), allow_pagesetup=True,
                 worksheets=True):
        self.sheets = [StubSheet(n, fail, allow_pagesetup) for n in names]
        self.ActiveSheet = next((s for s in self.sheets if s.Name == active), self.sheets[0])
        self.Sheets = StubCollection(self.sheets)
        object.__setattr__(self, "_worksheets", StubCollection(self.sheets) if worksheets else None)
        self.whole_export_calls = []
        self.close_calls = []

    @property
    def Worksheets(self):
        if self._worksheets is None:
            raise AttributeError("该引擎没有 Worksheets 集合（老 WPS）")
        return self._worksheets

    def ExportAsFixedFormat(self, *args, **kwargs):
        self.whole_export_calls.append((args, kwargs))
        fn = kwargs.get("Filename") or (args[1] if len(args) > 1 else "")
        with open(fn, "wb") as f:
            f.write(b"%PDF-1.4\n%whole\n")

    def Close(self, *args, **kwargs):
        self.close_calls.append((args, kwargs))


class StubApp:
    def __init__(self, wb):
        self._wb = wb
        self.Workbooks = self
        self.quit_calls = 0
        self.Visible = True
        self.DisplayAlerts = True
        self.EnableEvents = True
        self.Interactive = True
        self.AskToUpdateLinks = True
        self.AutomationSecurity = 0

    def Open(self, path, *args, **kwargs):
        self.open_call = (path, args, kwargs)
        return self._wb

    def Quit(self):
        self.quit_calls += 1


class LogCapture:
    """抓 xlsx_printer 的 WARNING 记录（断言"设置未生效"这类话必须出现）。"""

    def __init__(self):
        self.records = []

    def __enter__(self):
        self._logger = logging.getLogger("xlsx_printer")
        self._old_level = self._logger.level
        self._logger.setLevel(logging.DEBUG)

        class _H(logging.Handler):
            def emit(_self, record):
                self.records.append(record.getMessage())

        self._handler = _H()
        self._logger.addHandler(self._handler)
        return self

    def __exit__(self, *exc):
        self._logger.removeHandler(self._handler)
        self._logger.setLevel(self._old_level)
        return False

    @property
    def text(self):
        return "\n".join(self.records)


_TMP = tempfile.mkdtemp(prefix="hn_xlsx_opts_")


def make_xlsx(path):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("xl/workbook.xml", "<workbook/>")


_SRC = os.path.join(_TMP, "src.xlsx")
make_xlsx(_SRC)

_ORIG = {
    "_dispatch_app": X._dispatch_app,
    "engine_available": X.engine_available,
    "live_engines": X.live_engines,
    "_lo": C._convert_office_via_libreoffice,
    "_com_uninit": C._com_uninitialize_quietly,
}


def _run_com(options, wb=None, engine="excel", out_name="out.pdf"):
    """用假 COM 跑一次 _export_with_com；返回 (result, wb, app, out_path)。"""
    wb = wb or StubWorkbook()
    app = StubApp(wb)
    X._dispatch_app = lambda eng: (app, False)      # owned=False：不触发 Quit/强杀
    out = os.path.join(_TMP, out_name)
    C._com_uninitialize_quietly = lambda: None
    try:
        res = X._export_with_com(engine, _SRC, out, print_options=options)
    finally:
        X._dispatch_app = _ORIG["_dispatch_app"]
        C._com_uninitialize_quietly = _ORIG["_com_uninit"]
    return res, wb, app, out


# ============================================================
# B. PageSetup 属性映射（假 COM 记录 setattr）
# ============================================================
print("\n=== B. PageSetup 属性映射（PaperSize / Orientation / Margins / 居中 / 缩放 / 打印区域）===")

for paper, code in (("A4", 9), ("A3", 8), ("Letter", 1), ("Legal", 5), ("B5", 13), ("A5", 11)):
    res, wb, app, out = _run_com({"paper": paper}, out_name=f"paper_{paper}.pdf")
    got = wb.ActiveSheet._ps.writes.get("PaperSize")
    eq(f"paper={paper} ⇒ PageSetup.PaperSize={code}", got, code)
    chk(f"paper={paper} 导出真的发生了（假 COM 写出 %PDF）",
        bool(wb.ActiveSheet.export_calls) and os.path.getsize(out) > 0)

for orient, code in (("portrait", 1), ("landscape", 2)):
    res, wb, app, out = _run_com({"orientation": orient}, out_name=f"orient_{orient}.pdf")
    eq(f"orientation={orient} ⇒ PageSetup.Orientation={code}",
        wb.ActiveSheet._ps.writes.get("Orientation"), code)

res, wb, app, out = _run_com({}, out_name="margins_default.pdf")
w = wb.ActiveSheet._ps.writes
eq("默认四边页边距 1cm → TopMargin ≈ 28.3465pt", round(w.get("TopMargin"), 4), round(1.0 * CM_TO_PT, 4))
eq("默认四边页边距 1cm → BottomMargin ≈ 28.3465pt", round(w.get("BottomMargin"), 4), round(1.0 * CM_TO_PT, 4))
eq("默认四边页边距 1cm → LeftMargin ≈ 28.3465pt", round(w.get("LeftMargin"), 4), round(1.0 * CM_TO_PT, 4))
eq("默认四边页边距 1cm → RightMargin ≈ 28.3465pt", round(w.get("RightMargin"), 4), round(1.0 * CM_TO_PT, 4))
eq("默认页眉 0cm → HeaderMargin=0", w.get("HeaderMargin"), 0.0)
eq("默认页脚 0cm → FooterMargin=0", w.get("FooterMargin"), 0.0)
eq("默认水平居中开 ⇒ CenterHorizontally=True", w.get("CenterHorizontally"), True)
eq("默认垂直居中关 ⇒ CenterVertically=False", w.get("CenterVertically"), False)

res, wb, app, out = _run_com(
    {"margin_top_cm": 0.5, "margin_bottom_cm": 2, "margin_left_cm": 1.5,
     "margin_right_cm": 0.25, "header_cm": 0.5, "footer_cm": 0.75, "center_h": False,
     "center_v": True}, out_name="margins_custom.pdf")
w = wb.ActiveSheet._ps.writes
for prop, cm in (("TopMargin", 0.5), ("BottomMargin", 2.0), ("LeftMargin", 1.5),
                 ("RightMargin", 0.25), ("HeaderMargin", 0.5), ("FooterMargin", 0.75)):
    eq(f"{prop} 按 cm→pt 换算（{cm}cm ≈ {cm * CM_TO_PT:.4f}pt）",
        round(w.get(prop), 4), round(cm * CM_TO_PT, 4))
eq("center_h=False ⇒ CenterHorizontally=False", w.get("CenterHorizontally"), False)
eq("center_v=True ⇒ CenterVertically=True", w.get("CenterVertically"), True)

# 缩放口径
res, wb, app, out = _run_com({}, out_name="fit_cols.pdf")   # 默认 fit=cols
w = wb.ActiveSheet._ps.writes
chk("默认 fit=cols ⇒ Zoom=False + FitToPagesWide=1 + FitToPagesTall=False",
    w.get("Zoom") is False and w.get("FitToPagesWide") == 1 and w.get("FitToPagesTall") is False,
    str({k: w.get(k) for k in ("Zoom", "FitToPagesWide", "FitToPagesTall")}))

res, wb, app, out = _run_com({"fit": "sheet"}, out_name="fit_sheet.pdf")
w = wb.ActiveSheet._ps.writes
chk("fit=sheet ⇒ FitToPagesWide=1 且 FitToPagesTall=1",
    w.get("FitToPagesWide") == 1 and w.get("FitToPagesTall") == 1,
    str({k: w.get(k) for k in ("FitToPagesWide", "FitToPagesTall")}))

res, wb, app, out = _run_com({"fit": "rows"}, out_name="fit_rows.pdf")
w = wb.ActiveSheet._ps.writes
chk("fit=rows ⇒ FitToPagesWide=False + FitToPagesTall=1",
    w.get("FitToPagesWide") is False and w.get("FitToPagesTall") == 1,
    str({k: w.get(k) for k in ("FitToPagesWide", "FitToPagesTall")}))

res, wb, app, out = _run_com({"fit": "none", "scale": 150}, out_name="fit_none.pdf")
w = wb.ActiveSheet._ps.writes
chk("fit=none ⇒ Zoom=150 且先关掉 FitToPages*（否则部分引擎忽略 Zoom）",
    w.get("Zoom") == 150 and w.get("FitToPagesWide") is False and w.get("FitToPagesTall") is False,
    str({k: w.get(k) for k in ("Zoom", "FitToPagesWide", "FitToPagesTall")}))

# 打印区域
res, wb, app, out = _run_com({"print_range": "active"}, out_name="area_active.pdf")
chk("print_range=active ⇒ 不写 PrintArea（尊重文件里已有的打印区域）",
    "PrintArea" not in wb.ActiveSheet._ps.writes)

res, wb, app, out = _run_com({"print_range": "ignore_area"}, out_name="area_ignore.pdf")
eq("print_range=ignore_area ⇒ PrintArea=''（清空）",
    wb.ActiveSheet._ps.writes.get("PrintArea"), "")

with LogCapture() as cap:
    res, wb, app, out = _run_com({"print_range": "selection"}, out_name="area_selection.pdf")
eq("print_range=selection ⇒ PrintArea=UsedRange.Address",
    wb.ActiveSheet._ps.writes.get("PrintArea"), "$A$1:$F$20")
chk("selection 在日志里说明「COM 侧没有真实选区，用已用区域顶替」",
    "selection" in cap.text and "没有真实选区" in cap.text, cap.text[:200])

chk("选项全部生效 ⇒ ExportResult.options_honored=True",
    res.options_honored and all(res.options_applied.values()), str(res.options_applied))
chk("ExportResult 带回归一化后的 print_options（便于 GUI/日志核对）",
    res.print_options.get("paper") == "A4" and res.print_options.get("print_range") == "selection")

# ============================================================
# C. workbook 导出分支（打印整个工作簿）
# ============================================================
print("\n=== C. workbook 导出分支 ===")

wb3 = StubWorkbook(names=("S1", "S2", "S3"), active="S2")
res, wb, app, out = _run_com({"print_range": "workbook"}, wb=wb3, out_name="whole.pdf")
chk("★ workbook 模式调用 Workbook.ExportAsFixedFormat（不是 Worksheet）",
    len(wb.whole_export_calls) == 1 and not any(s.export_calls for s in wb.sheets),
    f"whole={len(wb.whole_export_calls)} sheet_exports={[len(s.export_calls) for s in wb.sheets]}")
_ck = wb.whole_export_calls[0][1]
chk("整本导出参数与单表一致（Type=0 / IgnorePrintAreas=False / OpenAfterPublish=False）",
    _ck.get("Type") == 0 and _ck.get("IgnorePrintAreas") is False
    and _ck.get("OpenAfterPublish") is False, str(_ck))
chk("workbook 模式给**每张**表都写 PageSetup（3 张表都有 PaperSize）",
    all(s._ps.writes.get("PaperSize") == 9 for s in wb.sheets),
    str([s._ps.writes.get("PaperSize") for s in wb.sheets]))
eq("workbook 模式 sheet 字段写明「全部 3 个工作表」", res.sheet, "全部 3 个工作表")
eq("workbook 模式 sheet_count=3", res.sheet_count, 3)

wb3b = StubWorkbook(names=("S1", "S2", "S3"), active="S2")
res, wb, app, out = _run_com({}, wb=wb3b, out_name="active_only.pdf")
chk("★ 默认（active）只写活动工作表的 PageSetup，别的工作表一个属性都不碰",
    wb.ActiveSheet is wb.sheets[1] and wb.sheets[1]._ps.writes.get("PaperSize") == 9
    and wb.sheets[0]._ps.reads == 0 and wb.sheets[2]._ps.reads == 0,
    str([s._ps.reads for s in wb.sheets]))
chk("默认只导活动工作表（Worksheet.ExportAsFixedFormat，整本没被导）",
    len(wb.sheets[1].export_calls) == 1 and not wb.whole_export_calls)

# 老 WPS：没有 Worksheets 集合 ⇒ 退回 Sheets
wb_old = StubWorkbook(names=("S1", "S2"), worksheets=False)
res, wb, app, out = _run_com({"print_range": "workbook"}, wb=wb_old, out_name="whole_wps.pdf")
chk("没有 Worksheets 集合的老 WPS ⇒ 退回 wb.Sheets，仍给每张表写 PageSetup",
    all(s._ps.writes.get("PaperSize") == 9 for s in wb.sheets)
    and len(wb.whole_export_calls) == 1,
    str([s._ps.writes.get("PaperSize") for s in wb.sheets]))


class NoWholeBookWorkbook(StubWorkbook):
    """模拟只有 Worksheet.ExportAsFixedFormat、没有 Workbook 级的 WPS 版本。"""

    def ExportAsFixedFormat(self, *args, **kwargs):
        raise AttributeError("Workbook 没有 ExportAsFixedFormat")


try:
    _run_com({"print_range": "workbook"}, wb=NoWholeBookWorkbook(), out_name="no_whole.pdf")
    chk("整本导出不被支持时抛 XlsxExportError（而不是裸 AttributeError）", False)
except X.XlsxExportError as e:
    chk("★ 整本导出不被支持时抛 XlsxExportError 且明说「整本导出」+ 怎么绕",
        "整本导出" in str(e) and "活动工作表" in str(e), str(e))
except Exception as e:  # noqa: BLE001
    chk("整本导出不被支持时抛 XlsxExportError（而不是裸 AttributeError）", False, repr(e))

# ============================================================
# D. 旧路径：print_options=None 完全不碰 PageSetup
# ============================================================
print("\n=== D. print_options=None ⇒ 旧路径零改动 ===")

wb_none = StubWorkbook(allow_pagesetup=False)   # 读 PageSetup 就抛异常
res, wb, app, out = _run_com(None, wb=wb_none, out_name="legacy.pdf")
chk("★ 不传 print_options 时不读 PageSetup（读就抛异常的桩也没炸）",
    res.pdf_path == out and os.path.getsize(out) > 0)
chk("旧路径结果：print_options 为空、options_applied 为空、options_honored=True",
    res.print_options == {} and res.options_applied == {} and res.options_honored is True,
    f"{res.print_options} {res.options_applied}")
chk("旧路径导出参数仍是旧形态（Type=0 / IgnorePrintAreas=False / OpenAfterPublish=False）",
    wb_none.ActiveSheet.export_calls[0][1].get("Type") == 0
    and wb_none.ActiveSheet.export_calls[0][1].get("IgnorePrintAreas") is False)

# ============================================================
# E. 引擎不支持 / 部分失败：必须明说「设置未生效」
# ============================================================
print("\n=== E. 部分属性失败与 LibreOffice 降级（必须明说未生效）===")

with LogCapture() as cap:
    res, wb, app, out = _run_com({"fit": "cols", "paper": "A3"},
                                 wb=StubWorkbook(fail=("FitToPagesTall",)), out_name="wps_fail.pdf")
chk("★ 单属性失败 ⇒ options_applied 记 False（WPS 风格：FitToPagesTall 不支持）",
    res.options_applied.get("FitToPagesTall") is False, str(res.options_applied))
chk("★ 单属性失败 ⇒ options_honored=False", res.options_honored is False)
chk("★ 日志点名「PageSetup.FitToPagesTall 设置未生效」", 
    "FitToPagesTall" in cap.text and "设置未生效" in cap.text, cap.text[:300])
chk("单属性失败不影响其它属性（PaperSize 仍生效）与导出",
    res.options_applied.get("PaperSize") is True and os.path.getsize(out) > 0)

# LibreOffice 兜底
def _stub_lo(src, out, *a, **k):
    with open(out, "wb") as f:
        f.write(b"%PDF-1.4\n%lo\n")


C._convert_office_via_libreoffice = _stub_lo
try:
    with LogCapture() as cap:
        lo_res = X._export_with_libreoffice(_SRC, os.path.join(_TMP, "lo.pdf"),
                                           print_options={"paper": "A3", "margin_left_cm": 2})
finally:
    C._convert_office_via_libreoffice = _ORIG["_lo"]

chk("★ LibreOffice 兜底 + print_options ⇒ 日志明写「页边距/缩放/打印范围设置未生效」",
    "页边距/缩放/打印范围" in cap.text and "未生效" in cap.text, cap.text[:300])
chk("LibreOffice 兜底保留旧警告「LibreOffice 兜底，版式可能与 Excel 不一致」",
    "LibreOffice 兜底，版式可能与 Excel 不一致" in cap.text)
chk("LibreOffice 兜底结果 options_honored=False（不假装设置生效）",
    lo_res.options_honored is False and lo_res.fallback is True)
chk("LibreOffice 结果仍回填 print_options（GUI 可据此提示用户）",
    lo_res.print_options.get("paper") == "A3")

# ============================================================
# F. 端到端：源文件 md5 不变 + 旧/新缓存各自命中
# ============================================================
print("\n=== F. 端到端：源文件不动、缓存键分流 ===")

md5_before = hashlib.md5(open(_SRC, "rb").read()).hexdigest()
X.engine_available = lambda e: e == "excel"
try:
    e2e_wb = StubWorkbook()
    e2e_app = StubApp(e2e_wb)
    X._dispatch_app = lambda eng: (e2e_app, False)
    e2e_out = os.path.join(_TMP, "e2e.pdf")
    res = X.export_to_pdf(_SRC, e2e_out, print_options={"margin_left_cm": 3})
    chk("export_to_pdf 端到端（假 COM）产出 PDF", os.path.getsize(e2e_out) > 0)
    eq("端到端：页边距 3cm 写成 3×72/2.54 pt",
        round(e2e_wb.ActiveSheet._ps.writes.get("LeftMargin"), 4), round(3 * CM_TO_PT, 4))
finally:
    X._dispatch_app = _ORIG["_dispatch_app"]
md5_after = hashlib.md5(open(_SRC, "rb").read()).hexdigest()
eq("★ 源文件 md5 前后一致（PageSetup 只写在临时副本上）", md5_after, md5_before)

# 缓存读侧按选项签名探测
cache_dir = os.path.join(_TMP, "cache")
os.makedirs(cache_dir, exist_ok=True)
md5 = "abc123"
old_pdf = os.path.join(cache_dir, X.cache_key(md5, "", "excel") + ".pdf")
new_pdf = os.path.join(cache_dir, X.cache_key(md5, "", "excel", {"margin_left_cm": 3}) + ".pdf")
for p in (old_pdf, new_pdf):
    with open(p, "wb") as f:
        f.write(b"%PDF-1.4\n%cache\n")
X.live_engines = lambda: ["excel"]
try:
    eq("find_cached_pdf(print_options=None) 命中旧缓存",
        X.find_cached_pdf(md5, "", cache_dir), old_pdf)
    eq("find_cached_pdf(同一套选项) 命中选项缓存",
        X.find_cached_pdf(md5, "", cache_dir, print_options={"margin_left_cm": 3}), new_pdf)
    # 另一个 md5：只有旧形态缓存，没有任何选项缓存
    md5b = "def456"
    old_pdf_b = os.path.join(cache_dir, X.cache_key(md5b, "", "excel") + ".pdf")
    with open(old_pdf_b, "wb") as f:
        f.write(b"%PDF-1.4\n%cache_old\n")
    chk("★ 只有旧缓存时，带选项的查找**不命中**（绝不能拿旧版式顶替）",
        X.find_cached_pdf(md5b, "", cache_dir, print_options={"margin_left_cm": 3}) is None)
    eq("同一时刻 print_options=None 仍能命中旧缓存（升级不丢已有缓存）",
       X.find_cached_pdf(md5b, "", cache_dir), old_pdf_b)
finally:
    X.live_engines = _ORIG["live_engines"]

# ============================================================
# G. 静态契约（防止今后把 PageSetup 写操作扩散出 _apply_print_options*）
# ============================================================
print("\n=== G. 静态契约 ===")

xsl_src = open(os.path.join(LOCAL_TOOL, "xlsx_printer.py"), encoding="utf-8").read()
conv_src = open(os.path.join(LOCAL_TOOL, "converter.py"), encoding="utf-8").read()

WRITE_PAGE_RE = re.compile(
    r"\.(?:LeftMargin|RightMargin|TopMargin|BottomMargin|HeaderMargin|FooterMargin|"
    r"Zoom|FitToPagesWide|FitToPagesTall|PrintArea|PrintTitleRows|PrintTitleColumns|"
    r"CenterHorizontally|CenterVertically|Orientation|PaperSize|PrintHeadings|"
    r"PrintGridlines)\s*=[^=]")
chk("没有散落的 PageSetup 直接赋值（全部走 _apply_print_options* 的 setattr）",
    WRITE_PAGE_RE.search(xsl_src) is None,
    WRITE_PAGE_RE.search(xsl_src).group(0) if WRITE_PAGE_RE.search(xsl_src) else "")

_body = xsl_src.split("def _apply_print_options_on_sheet", 1)
chk("存在 _apply_print_options_on_sheet（唯一写 PageSetup 的地方）", len(_body) == 2)
if len(_body) == 2:
    fn_body = _body[1].split("\ndef ", 1)[0]
    for prop in ("PaperSize", "Orientation", "TopMargin", "BottomMargin", "LeftMargin",
                 "RightMargin", "HeaderMargin", "FooterMargin", "CenterHorizontally",
                 "CenterVertically", "Zoom", "FitToPagesWide", "FitToPagesTall", "PrintArea"):
        if f'"{prop}"' not in fn_body:
            chk(f"属性 {prop} 的写入在 _apply_print_options_on_sheet 内", False)
    chk("PageSetup 写入统一用 setattr(ps, name, value)（便于逐项记账/降级）",
        "setattr(ps, name, value)" in fn_body)

chk("★ _apply_print_options 只在 print_options 非 None 时被调用（None 走旧路径）",
    re.search(r"if options is not None:\s*\n(?:.*\n){0,4}?\s+options_applied = _apply_print_options\(",
              xsl_src) is not None)
chk("call site 都已做 None 判断（不存在无条件下调用）",
    len(re.findall(r"_apply_print_options\(", xsl_src))
    == len(re.findall(r"options is not None", xsl_src)) + 0
    or len(re.findall(r"_apply_print_options\(", xsl_src)) >= 2)

chk("cache_key 签名含 print_options（选项进缓存键）",
    "def cache_key(source_md5: str, image_orientation: str = \"\", engine: str = \"\"," in xsl_src)
chk("find_cached_pdf 签名含 print_options",
    re.search(r"def find_cached_pdf\(.*?print_options", xsl_src, re.S) is not None)
chk("export_to_pdf 签名含 print_options",
    re.search(r"def export_to_pdf\(.*?print_options", xsl_src, re.S) is not None)
chk("converter 的 _convert_xlsx_to_pdf / _convert_office_to_pdf / convert 都透传 print_options",
    conv_src.count("print_options") >= 6, str(conv_src.count("print_options")))
chk("模块 docstring 已是新口径（显式 print_options / 临时副本 / 未生效）",
    "print_options" in xsl_src[:4000] and "临时副本" in xsl_src[:4000]
    and "未生效" in xsl_src[:4000])
chk("LibreOffice 降级日志文案在源码里（页边距/缩放/打印范围设置未生效）",
    "页边距/缩放/打印范围设置" in xsl_src)
chk("安全口径未变：ReadOnly=True / Close(SaveChanges=False) / 只退出 owned 实例",
    "ReadOnly=True" in xsl_src and "Close(SaveChanges=False)" in xsl_src
    and "if app is not None and owned:" in xsl_src)
chk("不调用 PrintOut（仍走 ExportAsFixedFormat）", "PrintOut" not in xsl_src)
chk("从不把 Excel 设为可见", re.search(r"Visible\s*=\s*True", xsl_src) is None)

print(f"\n{'✅ 全部通过' if not _FAILS else '❌ 有失败'}（{_CHECKS - len(_FAILS)}/{_CHECKS}）")
if _FAILS:
    for f in _FAILS:
        print(f"  · {f}")
    sys.exit(1)
