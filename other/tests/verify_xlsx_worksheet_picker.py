# -*- coding: utf-8 -*-
"""验收脚本 · xlsx「工作表」选择器（2026-11-03，offscreen）

需求（用户 2026-11）：「表格版式（仅表格文件）」面板里要能选这张表格打**哪个工作表**。
1. 面板「打印范围」下面多一行「工作表」下拉，首项「跟随文件的活动工作表」= 老行为
   （在 Excel 里直接 Ctrl+P 打的就是活动表）⇒ 没选过的任务与改动前逐字节一致。
2. 列表来自**文件本身**：解 zip 读 xl/workbook.xml 的 <sheets>，不启动 Excel（COM 起
   一个实例要几秒）、不引入 openpyxl（不是本项目依赖）；隐藏表不列（Excel 打印下拉也不列，
   且隐藏表无法 Activate）。
3. 选了某张表 ⇒ 导出前先 Activate 它，PageSetup 也只写它（版式与导出同一张表）；
   print_range=workbook（整簿）时忽略本项。
4. 表被改名/删掉 ⇒ 引擎回退活动工作表 + 日志 warning；面板上把那个旧名字标成
   「（文件里已没有这张表）」——不能让用户以为选择还在，那是「选了 A 打 B」的静默错版。
5. 下拉必须复用面板里的 _fit_width（QSizePolicy.Ignored + minimumWidth(0)），否则最长
   31 字符的表名会把模块最小宽度顶回 166+，面板一窄就出横向滚动条。
6. worksheet 为空时从 options_signature 里剔掉 ⇒ 存量 PDF 缓存不因升级全部失效。

    python other/tests/verify_xlsx_worksheet_picker.py     # exit 0 = 全绿
"""
import hashlib
import io
import json
import logging
import os
import re
import sys
import tempfile
import zipfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LOCAL_TOOL = os.path.join(REPO, "local_print_tool")

_SANDBOX = tempfile.mkdtemp(prefix="hn_xlsxsheet_")
os.environ["APPDATA"] = _SANDBOX                 # 用户数据目录 → 沙箱（别碰真实配置）
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, LOCAL_TOOL)
os.chdir(LOCAL_TOOL)

import paths  # noqa: E402
from PySide6.QtWidgets import QApplication, QSizePolicy  # noqa: E402

app = QApplication.instance() or QApplication([])

import converter as _converter  # noqa: E402
_converter.get_available_engines = lambda: {"word": True, "wps": True, "libreoffice": True}
import gui as _gui  # noqa: E402

_gui.QMessageBox.question = staticmethod(lambda *a, **k: _gui.QMessageBox.Yes)
_gui.QMessageBox.warning = staticmethod(lambda *a, **k: _gui.QMessageBox.Ok)
_gui.QMessageBox.information = staticmethod(lambda *a, **k: _gui.QMessageBox.Ok)

import converter as C  # noqa: E402
import printer_config as pc  # noqa: E402
import xlsx_printer as X  # noqa: E402
from gui import MainWindow  # noqa: E402
from printer_config import (PrintJob, XLSX_OPTION_KEYS,  # noqa: E402
                            normalize_task_options, resolve_xlsx_print_options,
                            task_options_to_print_options)

GUI_SRC = io.open(os.path.join(LOCAL_TOOL, "gui.py"), encoding="utf-8").read()
ENGINE_SRC = io.open(os.path.join(LOCAL_TOOL, "xlsx_printer.py"), encoding="utf-8").read()
CFG_SRC = io.open(os.path.join(LOCAL_TOOL, "printer_config.py"), encoding="utf-8").read()

ok_all = True
_total = 0


def check(name, cond, extra=""):
    global ok_all, _total
    _total += 1
    if not cond:
        ok_all = False
        print(f"❌ {name}" + (f"  [{extra}]" if extra else ""))
    else:
        print(f"✅ {name}")


def eq(name, got, want):
    check(name, got == want, f"得到 {got!r} / 期望 {want!r}")


_TMP = tempfile.mkdtemp(prefix="hn_xlsxsheet_files_")
_XLSX_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"


_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def make_book(path, sheets, use_ns=True, tag_prefix=""):
    """造一个最小可用的 xlsx：只写 xl/workbook.xml（表名读取只需要它）。

    真文件的 <sheet> 上带 r:id，根节点声明 xmlns:r —— 不声明的话 expat 会报 unbound
    prefix，ElementTree 直接抛 ParseError（这条在下面用"真实形态"钉住）。
    """
    ns = f' xmlns="{_XLSX_NS}" xmlns:r="{_REL_NS}"' if use_ns else ""
    if tag_prefix:
        ns = f' xmlns:{tag_prefix}="{_XLSX_NS}" xmlns:r="{_REL_NS}"'
    body = "".join(
        f'<{tag_prefix}{"sheet" if not tag_prefix else ":sheet"}'
        f' name="{name}"{state}{rid}/>'
        for name, state, rid in sheets)
    root = (f'<?xml version="1.0" encoding="UTF-8"?>'
            f'<{tag_prefix}workbook{ns}><{tag_prefix}sheets>{body}'
            f'</{tag_prefix}sheets></{tag_prefix}workbook>')
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("xl/workbook.xml", root)
    return path


# ============================================================
# ① 选项层：15 个键，selection 与 worksheet 贯穿 task_options / print_options / XLSX_OPTION_KEYS
# ============================================================
print("\n=== ① 选项层：selection / worksheet 是后加的两个键（默认空 = 跟随文件） ===")

eq("DEFAULT_TASK_OPTIONS 有 worksheet 且默认空（跟随）",
   pc.DEFAULT_TASK_OPTIONS.get("worksheet"), "")
check("★ XLSX_OPTION_KEYS 含 worksheet，且与 task_options_to_print_options 的键集一致",
      "worksheet" in XLSX_OPTION_KEYS
      and set(XLSX_OPTION_KEYS) == set(task_options_to_print_options({})),
      f"keys={sorted(XLSX_OPTION_KEYS)}")
eq("task_options_to_print_options 默认带 worksheet=''",
   task_options_to_print_options({}).get("worksheet"), "")
check("worksheet 保持文件名/表名的原样（中文、空格不被吃）",
      task_options_to_print_options({"worksheet": "  数据 1 "}).get("worksheet") == "数据 1")
eq("worksheet 非字符串 ⇒ 退回 ''（配置被手改成 123 也不炸）",
   normalize_task_options({"worksheet": 123}).get("worksheet"), "")
eq("worksheet 名字截断到 120 字符（配置文件别被灌爆）",
   len(normalize_task_options({"worksheet": "表" * 200}).get("worksheet")), 120)
# 故意不做白名单：表被改名/删表只有引擎打开文件才判得准；在选项层抹成 "" 会变成
# "选了 A 打 B"的静默错版（设计注释在 printer_config._coerce_sheet_name）。
eq("不认识的表名原样保留（不在这里抹成 ''，交给引擎回退 + warning）",
   normalize_task_options({"worksheet": "早就删掉的老表"}).get("worksheet"), "早就删掉的老表")

_own = resolve_xlsx_print_options({}, {"worksheet": "报表"}) or {}
eq("resolve_xlsx_print_options 把任务级 worksheet 带出来", _own.get("worksheet"), "报表")
check("任务级只选工作表（其它都默认）⇒ 输出仍是完整全套键",
      set(_own) == set(XLSX_OPTION_KEYS), f"{sorted(_own)}")
check("两处都没配过 ⇒ None（引擎完全不碰版式，老用户行为不变）",
      resolve_xlsx_print_options({}, {}) is None)

eq("引擎 DEFAULT_PRINT_OPTIONS 有 worksheet=''",
   X.DEFAULT_PRINT_OPTIONS.get("worksheet"), "")
check("引擎 normalize_print_options({}) == DEFAULT_PRINT_OPTIONS（新键自动跟上）",
      X.normalize_print_options({}) == X.DEFAULT_PRINT_OPTIONS,
      str(X.normalize_print_options({})))
eq("引擎侧 worksheet=非字符串 ⇒ ''",
   X.normalize_print_options({"worksheet": None}).get("worksheet"), "")
eq("引擎侧 worksheet 去空格", X.normalize_print_options({"worksheet": " 数据 "}).get("worksheet"), "数据")
eq("引擎侧 worksheet 超长截断 120",
   len(X.normalize_print_options({"worksheet": "x" * 300}).get("worksheet")), 120)
check("引擎收到 XLSX_OPTION_KEYS 全套 ⇒ 一个键都不丢（全套键）",
      set(X.normalize_print_options({k: 1 for k in XLSX_OPTION_KEYS})) == set(XLSX_OPTION_KEYS))

# ============================================================
# ② 缓存签名：没选表 ⇒ 与加键前逐字符相同（存量 PDF 不重转）
# ============================================================
print("\n=== ② 缓存签名：没选工作表 ⇒ 签名与加键前逐字符相同 ===")

_old_payload = {k: v for k, v in X.DEFAULT_PRINT_OPTIONS.items()
                if k not in ("worksheet", "selection")}
_old_sig = hashlib.md5(
    json.dumps(_old_payload, sort_keys=True, ensure_ascii=False,
               separators=(",", ":")).encode("utf-8")).hexdigest()[:8]
eq("★ 默认选项（worksheet=''）的签名 == 加键前算法算出的签名",
   X.options_signature(X.normalize_print_options({})), _old_sig)
_sig_sheet = X.options_signature(X.normalize_print_options({"worksheet": "数据"}))
check("选了工作表 ⇒ 签名变化（换了表必须重出 PDF，不能命中旧表那份）",
      _sig_sheet != _old_sig and bool(re.fullmatch(r"[0-9a-f]{8}", _sig_sheet)), _sig_sheet)
eq("不同工作表 ⇒ 不同签名",
   X.options_signature({"worksheet": "数据"}) == X.options_signature({"worksheet": "报表"}), False)
check("选项签名仍然与字典顺序无关",
      X.options_signature({"worksheet": "数据", "paper": "A3"})
      == X.options_signature({"paper": "A3", "worksheet": "数据"}))
check("缓存 key 带上工作表签名（改表 = 换 key）",
      X.cache_key("md5abc", "", "excel", {"worksheet": "数据"})
      != X.cache_key("md5abc", "", "excel", {"worksheet": "报表"}))
check("描述串里带工作表名（日志能看出打的是哪张）",
      "工作表「数据」" in X.describe_print_options(X.normalize_print_options({"worksheet": "数据"})))
check("没选工作表时描述串不出现「工作表」（老日志形态）",
      "工作表" not in X.describe_print_options(X.normalize_print_options({})))

# ============================================================
# ③ 表名清单：解 zip 读 xl/workbook.xml（不启动 Office、不靠 openpyxl）
# ============================================================
print("\n=== ③ 表名清单：list_worksheet_names（解 zip，只读几 KB） ===")

_book = make_book(os.path.join(_TMP, "book.xlsx"), [
    ("Sheet1", "", ' r:id="rId1"'),
    ("数据", "", ' r:id="rId2"'),
    ("隐藏甲", ' state="hidden"', ' r:id="rId3"'),
    ("报表", "", ' r:id="rId4"'),
])
eq("默认只列可见表、保持工作簿里的标签顺序",
   X.list_worksheet_names(_book), ["Sheet1", "数据", "报表"])
eq("include_hidden=True 连隐藏表一起列（诊断用）",
   X.list_worksheet_names(_book, include_hidden=True), ["Sheet1", "数据", "隐藏甲", "报表"])
eq("veryHidden 也算隐藏（Excel 里那档连右键都看不到）",
   X.list_worksheet_names(make_book(os.path.join(_TMP, "vh.xlsx"),
                                    [("能打", "", ""), ("暗", ' state="veryHidden"', "")])),
   ["能打"])

_prefixed = make_book(os.path.join(_TMP, "prefixed.xlsx"),
                      [("A", "", ""), ("B", "", "")], use_ns=False, tag_prefix="x")
eq("命名空间前缀写成 x:sheet 的引擎（部分 WPS 导出）也能读",
   X.list_worksheet_names(_prefixed), ["A", "B"])

_no_wb = os.path.join(_TMP, "no_workbook.xlsx")
with zipfile.ZipFile(_no_wb, "w") as z:
    z.writestr("xl/worksheets/sheet1.xml", "<worksheet/>")
eq("缺 xl/workbook.xml（加密/残缺包）⇒ None，面板只留「跟随」",
   X.list_worksheet_names(_no_wb), None)

_old = os.path.join(_TMP, "old.xls")
with open(_old, "wb") as f:
    f.write(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 64)   # OLE 头：.xls 不是 zip
eq(".xls（BIFF 老二进制）⇒ None：老格式照样能打，只是不能选表",
   X.list_worksheet_names(_old), None)
eq("文件不存在 ⇒ None", X.list_worksheet_names(os.path.join(_TMP, "nope.xlsx")), None)
eq("非表格扩展名 ⇒ None", X.list_worksheet_names(os.path.join(_TMP, "a.docx")), None)

# 缓存：(路径, mtime_ns, size) 没变就复用；文件改了就重读。
_cached = X.list_worksheet_names(_book)
check("第二次读走缓存，结果一致",
      _cached == X.list_worksheet_names(_book) and len(X._SHEET_NAME_CACHE) > 0)
make_book(_book, [("新表", "", ""), ("数据", "", "")])
eq("文件被改（mtime/size 变）⇒ 缓存自然失效、读到新表名",
   X.list_worksheet_names(_book), ["新表", "数据"])
check("引擎源码里没有 openpyxl（不是本项目依赖，不能偷偷引进来）",
      "openpyxl" not in ENGINE_SRC.replace("openpyxl **不是**", "")
      .replace("不用 openpyxl", "").replace("openpyxl（非项目依赖）", ""),
      "源码里出现 openpyxl 代码引用")
check("读表名不会启动 Office（只用 zipfile + ElementTree）",
      "zipfile.ZipFile" in ENGINE_SRC and "ElementTree" in ENGINE_SRC
      and "Dispatch(" not in ENGINE_SRC.split("def list_worksheet_names")[1][:2000])

# ============================================================
# ④ 引擎：假 COM（不启动 Office）
# ============================================================
print("\n=== ④ 引擎行为：选定 ⇒ 只导那张表、版式也只写它 ===")


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
    """COM 集合：Count / Item(i) / 可按名索引（老 WPS 不支持按名 ⇒ by_name=False）。"""

    def __init__(self, items, by_name=True):
        self._items = list(items)
        self._by_name = by_name

    @property
    def Count(self):
        return len(self._items)

    def Item(self, i):
        return self._items[i - 1]

    def __call__(self, arg):
        if isinstance(arg, str):
            if not self._by_name:
                raise TypeError("按名索引需要字符串键 —— 该引擎不支持")
            for sheet in self._items:
                if str(sheet.Name).strip().lower() == arg.strip().lower():
                    return sheet           # Excel 表名不区分大小写
            raise KeyError(arg)
        return self._items[int(arg) - 1]


class StubSheet:
    def __init__(self, name, fail=(), activate_error=None):
        self.Name = name
        self._ps = RecordingPageSetup(fail)
        self.export_calls = []
        self.ps_reads = 0
        self.activate_calls = 0
        self._activate_error = activate_error
        self.UsedRange = StubUsedRange()

    @property
    def PageSetup(self):
        self.ps_reads += 1
        return self._ps

    def Activate(self):
        self.activate_calls += 1
        if self._activate_error is not None:
            raise self._activate_error

    def ExportAsFixedFormat(self, *args, **kwargs):
        self.export_calls.append((args, kwargs))
        fn = kwargs.get("Filename") or (args[1] if len(args) > 1 else "")
        with open(fn, "wb") as f:
            f.write(b"%PDF-1.4\n%stub\n")


class StubWorkbook:
    def __init__(self, names=("Sheet1",), active="Sheet1", by_name=True,
                 activate_errors=None):
        _errs = dict(activate_errors or {})
        self.sheets = [StubSheet(n, activate_error=_errs.get(n)) for n in names]
        self.ActiveSheet = next((s for s in self.sheets if s.Name == active), self.sheets[0])
        self.Sheets = StubCollection(self.sheets, by_name)
        self._worksheets = StubCollection(self.sheets, by_name)
        self.whole_export_calls = []
        self.close_calls = []

    @property
    def Worksheets(self):
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
        self.Visible = True
        self.DisplayAlerts = True
        self.EnableEvents = True
        self.Interactive = True
        self.AskToUpdateLinks = True
        self.AutomationSecurity = 0

    def Open(self, path, *args, **kwargs):
        return self._wb

    def Quit(self):
        pass


class LogCapture:
    def __init__(self):
        self.records = []

    def __enter__(self):
        self._logger = logging.getLogger("xlsx_printer")
        self._old_level = self._logger.level
        self._logger.setLevel(logging.DEBUG)
        records = self.records

        class _H(logging.Handler):
            def emit(_self, record):
                records.append(record.getMessage())

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


_SRC = make_book(os.path.join(_TMP, "src.xlsx"), [("Sheet1", "", ""), ("数据", "", "")])
_ORIG_DISPATCH = X._dispatch_app
_ORIG_UNINIT = C._com_uninitialize_quietly
_out_seq = [0]


def run_com(options, wb=None):
    wb = wb or StubWorkbook()
    app_stub = StubApp(wb)
    X._dispatch_app = lambda eng: (app_stub, False)      # owned=False：不触发 Quit/强杀
    C._com_uninitialize_quietly = lambda: None
    _out_seq[0] += 1
    out = os.path.join(_TMP, f"out_{_out_seq[0]}.pdf")
    try:
        res = X._export_with_com("excel", _SRC, out, print_options=options)
    finally:
        X._dispatch_app = _ORIG_DISPATCH
        C._com_uninitialize_quietly = _ORIG_UNINIT
    return res, wb, out


# —— 选定某张表 ——
_wb = StubWorkbook(["Sheet1", "数据", "报表"], active="Sheet1")
_res, _wb, _out = run_com({"worksheet": "数据"}, _wb)
_by_name = {s.Name: s for s in _wb.sheets}
check("选中「数据」⇒ 真的 Activate 了它（否则 PageSetup/导出还落在活动表上）",
      _by_name["数据"].activate_calls == 1, str(_by_name["数据"].activate_calls))
check("选中「数据」⇒ 只导出它，其它表没有被导出",
      len(_by_name["数据"].export_calls) == 1
      and not _by_name["Sheet1"].export_calls and not _by_name["报表"].export_calls)
check("选中「数据」⇒ PageSetup 只写到它身上（版式与导出同一张表）",
      _by_name["数据"]._ps.writes and _by_name["数据"].ps_reads > 0
      and _by_name["Sheet1"].ps_reads == 0 and _by_name["报表"].ps_reads == 0,
      f"reads={[s.Name + ':' + str(s.ps_reads) for s in _wb.sheets]}")
check("选中「数据」⇒ 版式确实写进去了（纸张/边距都在那张表的 PageSetup 上）",
      _by_name["数据"]._ps.writes.get("PaperSize") == 9
      and "TopMargin" in _by_name["数据"]._ps.writes
      and _res.options_applied.get("PaperSize") is True, str(_res.options_applied))
eq("结果里报告的表名就是选中的那张（日志/对账一致）", _res.sheet, "数据")
check("结果里带着 print_options（含 worksheet），缓存 key 才能对上",
      _res.print_options.get("worksheet") == "数据")

# —— 没选（默认跟随活动表）：一切照旧 ——
_wb = StubWorkbook(["Sheet1", "数据"], active="数据")
_res, _wb, _out = run_com({"worksheet": ""}, _wb)
check("没选工作表 ⇒ 不去 Activate 任何表（老路径一行不多做）",
      all(s.activate_calls == 0 for s in _wb.sheets),
      str([s.activate_calls for s in _wb.sheets]))
check("没选工作表 ⇒ 导出的是文件里的活动表", _res.sheet == "数据"
      and _wb.sheets[1].export_calls and not _wb.sheets[0].export_calls)
_wb_none = StubWorkbook(["Sheet1", "数据"], active="数据")
_res_none, _wb_none, _ = run_com(None, _wb_none)
check("print_options=None（老任务）⇒ 完全不碰 PageSetup、不 Activate",
      all(s.ps_reads == 0 and s.activate_calls == 0 for s in _wb_none.sheets))

# —— 表名不存在（改名/删表/配置被手改）——
_wb = StubWorkbook(["Sheet1", "数据"], active="Sheet1")
with LogCapture() as _cap:
    _res, _wb, _out = run_com({"worksheet": "已删除的表"}, _wb)
check("表名不存在 ⇒ 回退活动表导出（不抛错，文件照样能打）",
      _res.sheet == "Sheet1" and _wb.sheets[0].export_calls
      and not _wb.sheets[1].export_calls)
check("表名不存在 ⇒ 日志明确说没找到这张表（不能静默换表）",
      "没有名为「已删除的表」的工作表" in _cap.text, _cap.text[-300:])
check("表名不存在 ⇒ 不 Activate 任何表（没有可切的）",
      all(s.activate_calls == 0 for s in _wb.sheets))

# —— 整簿导出：忽略工作表选择 ——
_wb = StubWorkbook(["Sheet1", "数据", "报表"], active="Sheet1")
_res, _wb, _out = run_com({"print_range": "workbook", "worksheet": "数据"}, _wb)
check("print_range=workbook ⇒ 忽略 worksheet：不做切表、走整簿导出",
      all(s.activate_calls == 0 for s in _wb.sheets)
      and len(_wb.whole_export_calls) == 1
      and not _wb.sheets[1].export_calls)
check("print_range=workbook ⇒ 结果里的表名是「全部 N 个工作表」",
      "全部" in str(_res.sheet), str(_res.sheet))
check("print_range=workbook ⇒ 版式写到每一张表（既有行为不变）",
      all(s.ps_reads > 0 for s in _wb.sheets),
      str([(s.Name, s.ps_reads) for s in _wb.sheets]))

# —— 找到但引擎拒绝 Activate（隐藏表 1004）——
_wb = StubWorkbook(["Sheet1", "隐藏甲"], active="Sheet1",
                   activate_errors={"隐藏甲": RuntimeError("1004 无法激活")})
with LogCapture() as _cap:
    _res, _wb, _out = run_com({"worksheet": "隐藏甲"}, _wb)
_hidden = {s.Name: s for s in _wb.sheets}["隐藏甲"]
check("引擎拒绝激活（隐藏表）⇒ 仍然直接导出那张表（Excel 允许对隐藏表 Export）",
      _res.sheet == "隐藏甲" and _hidden.export_calls
      and not {s.Name: s for s in _wb.sheets}["Sheet1"].export_calls)
check("引擎拒绝激活 ⇒ 版式也落到那张表上（不退回活动表）",
      _hidden._ps.writes.get("PaperSize") == 9
      and {s.Name: s for s in _wb.sheets}["Sheet1"].ps_reads == 0)
check("引擎拒绝激活 ⇒ 日志说明是「无法激活」而不是悄悄换表",
      "无法激活工作表「隐藏甲」" in _cap.text, _cap.text[-300:])

# —— 大小写 / 按名索引不可用（老 WPS）——
_wb = StubWorkbook(["Report", "Data"], active="Report", by_name=False)
_res, _wb, _out = run_com({"worksheet": "data"}, _wb)
check("引擎不支持按名索引 ⇒ 遍历 Name 匹配；大小写不敏感（data → Data）",
      _res.sheet == "Data" and {s.Name: s for s in _wb.sheets}["Data"].export_calls)

# —— 多表但没选：warning 里给出「去面板里选」的出路 ——
_wb = StubWorkbook(["Sheet1", "数据", "报表"], active="Sheet1")
with LogCapture() as _cap:
    run_com({"worksheet": ""}, _wb)
check("多表 + 没选 ⇒ 日志提示可以去表格版式里选工作表（而不是让人改代码传参）",
      "请在表格版式里选「工作表」" in _cap.text, _cap.text[-400:])
_wb = StubWorkbook(["Sheet1", "数据", "报表"], active="Sheet1")
with LogCapture() as _cap2:
    run_com({"worksheet": "数据"}, _wb)
check("多表 + 选了 ⇒ 不再打「导出的是活动工作表」那条 warning（用户已经指定了）",
      "导出的是活动工作表" not in _cap2.text, _cap2.text[-400:])

# ============================================================
# ⑤ GUI：下拉存在 / 列出文件里真实的表 / 写回任务 / 不撑宽
# ============================================================
print("\n=== ⑤ GUI：「工作表」下拉（offscreen，真实 xlsx 文件） ===")

_LONG = "这是一个相当长的工作表名称2026年度汇总"
_gui_book = make_book(os.path.join(_TMP, "门店月度汇总.xlsx"), [
    ("Sheet1", "", ' r:id="rId1"'),
    ("数据", "", ' r:id="rId2"'),
    ("隐藏甲", ' state="hidden"', ' r:id="rId3"'),
    (_LONG, "", ' r:id="rId4"'),
])

win = MainWindow(config_path=paths.config_path())
tab = win._config.tabs[win._current_tab]
tab.jobs = [
    PrintJob(file_path=_gui_book, page_count=3),
    PrintJob(file_path=os.path.join(_TMP, "b.docx"), page_count=2),
    PrintJob(file_path=os.path.join(_TMP, "c.pdf"), page_count=2),
]
win._set_current_jobs(tab.jobs)
win._rebuild_table()

check("面板里有 _xlsx_worksheet 下拉，且是 ThemedComboBox/QComboBox",
      hasattr(win, "_xlsx_worksheet") and isinstance(win._xlsx_worksheet, _gui.QComboBox))
check("它挂在表格版式面板里（在 _xlsx_widgets 清单中 ⇒ 冻结时一起灰）",
      win._xlsx_worksheet in win._xlsx_widgets)
_combo = win._xlsx_worksheet
eq("首项文案是「跟随文件的活动工作表」", _combo.itemText(0), _gui.XLSX_WORKSHEET_FOLLOW_LABEL)
check("★ 下拉复用了面板的 _fit_width（Ignored + minimumWidth(0)，长表名不撑宽）",
      _combo.sizePolicy().horizontalPolicy() == QSizePolicy.Ignored
      and _combo.minimumWidth() == 0,
      f"policy={_combo.sizePolicy().horizontalPolicy()} minW={_combo.minimumWidth()}")
check("下拉装了滚轮守卫（滚轮压在它上面不偷偷改选择）",
      "_disable_combo_wheel(self._xlsx_worksheet)" in GUI_SRC)
check("「工作表」行的标签在面板里", "addRow(\"工作表：\"" in GUI_SRC)
check("工作表下拉的 tooltip 里没有 markdown 星号（硬规矩 8）",
      "**" not in _combo.toolTip(), _combo.toolTip())

win._table.selectRow(0)
win._on_table_selection()
check("选中表格 ⇒ 面板出现", win._xlsx_panel_shown is True)
_names = [_combo.itemText(i) for i in range(_combo.count())]
eq("列表 = 「跟随」+ 文件里可见的表名（隐藏表不列），顺序照工作簿",
   _names, [_gui.XLSX_WORKSHEET_FOLLOW_LABEL, "Sheet1", "数据", _LONG])
check("默认选中「跟随」（没选过表的任务不该被悄悄指定某张表）",
      _combo.currentIndex() == 0 and _combo.currentData() == "")
eq("「跟随」项的 itemData 是空串（文案给人看，数据才是判据）", _combo.itemData(0), "")
eq("没动下拉 ⇒ 任务不落 worksheet（仍是跟随默认）",
   tab.jobs[0].xlsx_options.get("worksheet"), None)

# —— 选一张表 ⇒ 写回任务 ——
_combo.setCurrentIndex(_combo.findData("数据"))
_opts = tab.jobs[0].xlsx_options
eq("选「数据」⇒ 写进 PrintJob.xlsx_options", _opts.get("worksheet"), "数据")
check("★ 写回的是完整全套键（引擎认的那套）", set(_opts) == set(XLSX_OPTION_KEYS),
      f"{sorted(_opts)}")
check("非表格任务没被牵连", tab.jobs[1].xlsx_options == {} and tab.jobs[2].xlsx_options == {})

# —— 版本跟随：选长表名也要能正确回填 ——
_combo.setCurrentIndex(_combo.findData(_LONG))
eq("长表名也写回（没有长度截断到 31 以外的问题）",
   tab.jobs[0].xlsx_options.get("worksheet"), _LONG)
win._on_table_selection()
eq("重新刷新选区 ⇒ 下拉回显的还是那张长表名（currentData 往返一致）",
   _combo.currentData(), _LONG)

# —— 改回「跟随」⇒ 任务回到全默认（键彻底消失，不是留个空串）——
_combo.setCurrentIndex(0)
eq("改回「跟随」⇒ xlsx_options 里不再有 worksheet",
   tab.jobs[0].xlsx_options.get("worksheet"), None)
eq("其余项也都是默认 ⇒ xlsx_options 整个回到 {}（= 跟随全局默认）",
   tab.jobs[0].xlsx_options, {})

# —— 存着的旧表名不在文件里：必须显示并标注，不能假装跟随 ——
tab.jobs[0].xlsx_options = {"worksheet": "已删除的表"}
win._on_table_selection()
check("旧表名不在文件里 ⇒ 仍然出现在下拉里并标注「文件里已没有这张表」",
      _combo.currentData() == "已删除的表"
      and _combo.currentText().endswith("（文件里已没有这张表）"),
      f"{_combo.currentData()!r} / {_combo.currentText()!r}")
check("刷新面板不会把旧表名悄悄改成「跟随」（否则用户以为选择还在）",
      tab.jobs[0].xlsx_options.get("worksheet") == "已删除的表",
      str(tab.jobs[0].xlsx_options))
tab.jobs[0].xlsx_options = {}

# —— .xls 老格式：读不出表名 ⇒ 只剩「跟随」，引擎路径不变 ——
_old_job = os.path.join(_TMP, "old_book.xls")
with open(_old_job, "wb") as f:
    f.write(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 64)
tab.jobs[0].file_path = _old_job
win._on_table_selection()
eq(".xls 读不出表名 ⇒ 下拉只有「跟随」一项（不让用户看见猜的名字）",
   _combo.count(), 1)
tab.jobs[0].file_path = _gui_book
win._on_table_selection()

# —— 冻结标签页：下拉变灰、不回写 ——
tab.frozen = True
# 必须再走一次选区刷新：frozen 只是数据，_sync_xlsx_panel(..., frozen=True)
# 才是真正把控件置灰的地方（_sync_edit_enabled 不管表格版式这几件）。
win._on_table_selection()
_was = _combo.currentIndex()
_combo.setCurrentIndex(_combo.findData("数据"))
check("冻结时下拉是灰的", not _combo.isEnabled(),
      f"enabled={_combo.isEnabled()} shown={win._xlsx_panel_shown} frozen={tab.frozen} "
      f"in_widgets={_combo in win._xlsx_widgets} parentEnabled={_combo.parentWidget().isEnabled()}")
check("冻结时改下拉不写回任务（灰控件不回写是硬规矩）",
      tab.jobs[0].xlsx_options.get("worksheet") is None, str(tab.jobs[0].xlsx_options))
tab.frozen = False
win._on_table_selection()

# —— 宽度：最长 31 字符的表名不许把模块最小宽度顶回去 ——
win.show()
QApplication.processEvents()
_other_w = max(win._edit_dpi.minimumSizeHint().width(),
               win._edit_engine.minimumSizeHint().width())
_box_w = win._xlsx_box.minimumSizeHint().width()
check("★ 面板里出现长表名后，模块最小宽度仍 ≤ 其它模块最宽者 + 8（不撑宽）",
      _box_w <= _other_w + 8, f"模块 {_box_w} vs 其它最宽 {_other_w}")
check("下拉的最小宽度没有被长表名顶成 100+（Ignored 生效）",
      _combo.minimumSizeHint().width() <= _other_w, f"下拉 {_combo.minimumSizeHint().width()}")

# ============================================================
# ⑥ 静态契约（源码级，防止有人把上面这些行为改回去）
# ============================================================
print("\n=== ⑥ 静态契约 ===")

check("GUI 用 _fit_width 包着工作表下拉（不是裸 _combo）",
      "self._xlsx_worksheet = _fit_width(_combo(" in GUI_SRC)
check("_xlsx_panel_values 用 currentData 读 worksheet（不是拿给人看的文案）",
      '"worksheet": str(self._xlsx_worksheet.currentData() or "")' in GUI_SRC)
check("_sync_xlsx_panel 里会重填工作表列表（切换选中文件时列表跟着换）",
      "self._sync_xlsx_worksheet_items(sheet_jobs[0]," in GUI_SRC)
check("面板的 options 值来自任务级解析（_effective_xlsx_options），不另起一套",
      "opts = self._effective_xlsx_options(sheet_jobs[0])" in GUI_SRC)

_com = ENGINE_SRC.split("def _export_with_com", 1)[1]
_idx_select = _com.find("_select_worksheet(")
_idx_apply = _com.find("_apply_print_options(wb, options, label, target=target_ws)")
check("★ 先切表、后写 PageSetup（顺序反了就是「按 A 的版式导 B」）",
      0 <= _idx_select < _idx_apply, f"select@{_idx_select} apply@{_idx_apply}")
check("切表发生在导出之前（_export_worksheet_pdf 在它后面）",
      _idx_select < _com.find("_export_worksheet_pdf(ws, abs_out)"))
check("整簿导出时忽略 worksheet（whole_book 分支不看 worksheet）",
      "if options is not None and not whole_book:" in ENGINE_SRC)
check("找不到表就打 warning（不允许静默回退）",
      "回退活动工作表" in ENGINE_SRC)
check("printer_config 不改白名单：选表名不在这里校验存在性",
      "def _coerce_sheet_name" in CFG_SRC and "工作表不存在" not in CFG_SRC)

win._cloud_client = None
win.close()

print("\n" + "=" * 78)
print(f"工作表选择器验收：{_total} 项检查，", "全部通过 ✅" if ok_all else "存在失败 ❌")
sys.exit(0 if ok_all else 1)
