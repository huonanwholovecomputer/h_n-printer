# -*- coding: utf-8 -*-
"""验收 · xlsx/xls 打印：必须完全复现 Excel 自己的打印结果

依据（2026-11-03 修订，取代 2026-11-02 的「绝不改写版式」口径）：用户的工作流是在 Excel 里
设好版式再打印，本地打印工具**默认必须完全复现 Excel 自己的打印结果** —— 唯一可靠路径 =
Excel / WPS 的 `Worksheet.ExportAsFixedFormat`（让 Office 自己排版）。
只有 GUI 显式传 `print_options`（打印选项面板：纸张/方向/页边距/页眉页脚/居中/缩放/打印区域）
时才写 `PageSetup`，而且**只写在 export_to_pdf 复制出来的临时副本上**（ReadOnly 打开、
Close(SaveChanges=False) ⇒ 用户源文件 md5 不变）；`print_options=None`（老调用方 / 旧缓存
路径）行为与旧版逐字节一致，旧缓存照常命中。选项进缓存键，改页边距不会命中旧版式的 PDF。
逐项降级要记账：引擎不支持时必须明说「设置未生效」（见 tests/verify_xlsx_print_options.py）。

本脚本判据
  A. 静态接线：converter 把 .xls/.xlsx 交给 xlsx_printer 并透传 print_options；云端来源仍
     强制 LibreOffice（且提示选项未生效）；PageSetup 写入只封在 _apply_print_options* 里、
     且仅 print_options 非 None 时调用；选项进缓存键；导出参数 Type=0 / IgnorePrintAreas=False /
     OpenAfterPublish=False；引擎优先级 Excel→WPS→LibreOffice；LibreOffice 兜底必须落
     日志「LibreOffice 兜底，版式可能与 Excel 不一致」；缓存 key 含引擎身份；
     GUI（文件类型白名单 / 引擎标签 / 转换 worker / 缓存读写）与文件卡片接线。
  B. 行为（全部用假 COM，不启动真 Office）：只读打开、导出参数、wb.Close(SaveChanges=False)、
     复用用户实例时绝不 Quit、别的工作表不误伤；引擎降级链；全都没装时的报错；
     密码保护 / 损坏 / 空文件 / 非表格 的明确失败信息；云端来源只走 LibreOffice。
  C. 真机 parity（本机须装 Excel 或 WPS；HN_NO_ENGINE_PROBE=1 或引擎缺失 ⇒ SKIP）：
     两份参考工作簿导出后与 Excel 打印出来的参考 PDF 比页数 / 纸张尺寸 / 逐页文字 bbox /
     文字内容；源文件 MD5 前后不变；不残留 EXCEL.EXE；两次导出结果一致；页数统计一致。
  D. 变异自证（判据必须真红）：把副本的页边距改成 Excel 默认 1.8cm 并去掉水平居中后，
     C 的几何判据必须失败；把 <pageSetup scale="10"> 改成 100 版式不变（证明 Excel 在
     「调整为 1 页宽」模式下忽略 scale，也证明我们没有靠改缩放来造假）。

为什么「成绩汇总表」不逐页对比文字：那份参考 PDF 的 title 是
`…成绩汇总表(4).xlsx`（打印自另一份带 (4) 的副本），而磁盘这份的 used range 是
A1:UY184（545 列格式垃圾）⇒ 「调整所有列到一页」被 Excel 压到约 10%，它自己打印出来
就是左上角一张小表。工具必须如实复现这个结果（不得替用户改缩放），所以对这份只验
页数 / 纸张 / 边距起点 / 「没有被我们放大或重排」的行为证据，外加 PageSetup 直读核对。

用法：
    python tests/verify_xlsx_excel_parity.py        # exit 0 = 全部通过（SKIP 不算失败）
"""
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL_TOOL = os.path.join(REPO, "local_print_tool")
sys.path.insert(0, LOCAL_TOOL)

import logging  # noqa: E402

logging.basicConfig(level=logging.ERROR)

CM = 72.0 / 2.54          # 1cm = 28.3465pt
A4_LANDSCAPE = (841.92, 595.32)

REF_DIR = r"C:\Users\Administrator\Desktop\打印列表\2026年11月1日\张钰果\皇甫的表格"
XLSX_SCORE = os.path.join(
    REF_DIR, "[处理好的]2025—2026总学年数科院计算机科学与技术专业成绩汇总表.xlsx")
PDF_SCORE = os.path.join(
    REF_DIR, "[处理好的]2025—2026总学年数科院计算机科学与技术专业成绩汇总.pdf")
XLSX_RANK = os.path.join(REF_DIR, "[处理好的]25级计科大一日综测排名表.xlsx")
PDF_RANK = os.path.join(REF_DIR, "[处理好的]25级计科大一日综测排名表.pdf")

_TMPS = []
_BAD = [0]


# ============================================================
# 断言 / 收尾
# ============================================================
def chk(name, cond, detail=""):
    if not cond:
        _BAD[0] += 1
    print("[%s] %s%s" % ("PASS" if cond else "FAIL", name,
                         ("  -- " + str(detail)) if (not cond and detail) else ""))
    return cond


def skip(name, why):
    print("[SKIP] %s  -- %s" % (name, why))


def info(msg):
    print("       %s" % msg)


def newdir(prefix="hn_xlsx_"):
    d = tempfile.mkdtemp(prefix=prefix)
    _TMPS.append(d)
    return d


def md5_of(path):
    import hashlib
    h = hashlib.md5()
    with open(path, "rb") as f:
        for blk in iter(lambda: f.read(1 << 20), b""):
            h.update(blk)
    return h.hexdigest()


def read_src(name):
    with open(os.path.join(LOCAL_TOOL, name), "r", encoding="utf-8", newline="") as f:
        return f.read()


def finish():
    for d in _TMPS:
        shutil.rmtree(d, ignore_errors=True)
    print("\n%s（%d 条失败）" % ("❌ 有失败" if _BAD[0] else "✅ 全部通过", _BAD[0]))
    sys.exit(1 if _BAD[0] else 0)


# ============================================================
# PDF 几何
# ============================================================
def page_geom(pdf_path):
    """逐页：(bbox, 宽, 高, 去空白文字)。bbox = 文字块并集；空页为 None。"""
    import fitz
    out = []
    doc = fitz.open(pdf_path)
    try:
        for page in doc:
            blocks = page.get_text("blocks")
            box = None
            if blocks:
                box = (min(b[0] for b in blocks), min(b[1] for b in blocks),
                       max(b[2] for b in blocks), max(b[3] for b in blocks))
            text = "".join(c for c in page.get_text() if not c.isspace())
            out.append({"box": box, "w": page.rect.width, "h": page.rect.height,
                        "text": text})
    finally:
        doc.close()
    return out


def as_cm(v):
    return v / CM


# ============================================================
# xlsx 变异（只在临时副本上改 XML，绝不碰源文件）
# ============================================================
DEFAULT_MARGINS = ('<pageMargins left="0.708661" right="0.708661" top="0.748031" '
                   'bottom="0.748031" header="0.314961" footer="0.314961"/>')


def mutate_xlsx(src, dst, subs, only_sheets_with=None):
    """重写临时副本的 worksheet XML。返回被改动的 sheet 份数（0 ⇒ 变异没生效）。"""
    changed = 0
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename.startswith("xl/worksheets/sheet") and item.filename.endswith(".xml"):
                text = data.decode("utf-8")
                if only_sheets_with is None or only_sheets_with in text:
                    new = text
                    for pat, rep in subs:
                        new = re.sub(pat, rep, new)
                    if new != text:
                        changed += 1
                        data = new.encode("utf-8")
            zout.writestr(item, data)
    return changed


# ============================================================
# 假 COM（行为判据，不启动真 Office）
# ============================================================
class FakePdfWriter:
    def __init__(self):
        self.kwargs = None
        self.args = None

    def __call__(self, *args, **kwargs):
        self.args, self.kwargs = args, kwargs
        name = kwargs.get("Filename") or (args[1] if len(args) > 1 else "")
        with open(name, "wb") as f:
            f.write(b"%PDF-1.4\n%fake\n")


class FakeSheet:
    def __init__(self, name="Sheet1"):
        self.Name = name
        self.ExportAsFixedFormat = FakePdfWriter()


class FakeWorkbook:
    def __init__(self, sheets=("Sheet1",), active="Sheet1"):
        self._sheets = [FakeSheet(n) for n in sheets]
        self.ActiveSheet = next((s for s in self._sheets if s.Name == active), self._sheets[0])
        self.Sheets = _FakeCollection(self._sheets)
        self.open_kwargs = None
        self.close_kwargs = None

    def Close(self, *args, **kwargs):
        self.close_kwargs = (args, kwargs)


class _FakeCollection:
    def __init__(self, items):
        self._items = items

    @property
    def Count(self):
        return len(self._items)

    def __call__(self, i):
        return self._items[i - 1]

    def Item(self, i):
        return self._items[i - 1]


class FakeApp:
    def __init__(self, wb):
        self._wb = wb
        self.quit_calls = 0
        self.Workbooks = self
        self.Visible = True
        self.DisplayAlerts = True
        self.EnableEvents = True
        self.Interactive = True
        self.AskToUpdateLinks = True
        self.AutomationSecurity = 0

    def Open(self, path, *args, **kwargs):
        self._wb.open_kwargs = (path, args, kwargs)
        return self._wb

    def Quit(self):
        self.quit_calls += 1


# ============================================================
# A. 静态接线
# ============================================================
print("=== A. 静态接线 ===")
conv_src = read_src("converter.py")
xsl_src = read_src("xlsx_printer.py")
gui_src = read_src("gui.py")
card_src = read_src("order_tabs.py")

chk("converter 定义了 _convert_xlsx_to_pdf（表格走 Excel/WPS 自己的排版）",
    "def _convert_xlsx_to_pdf(" in conv_src)
chk("converter._convert_xlsx_to_pdf 调 xlsx_printer.export_to_pdf",
    re.search(r"def _convert_xlsx_to_pdf\(.*?\n(?:.*\n){0,40}?.*xlsx_printer\.export_to_pdf",
              conv_src) is not None)
chk("_convert_office_to_pdf 把 .xls/.xlsx 分流（本地才走 COM）",
    re.search(r'if ext in \("\.xls", "\.xlsx"\) and not \(from_cloud and not allow_cloud_com\)',
              conv_src) is not None)
chk("云端来源的表格不被 COM 分流（仍落到 LibreOffice，S1-04 口径）",
    re.search(r'if ext in \("\.xls", "\.xlsx"\) and not \(from_cloud and not allow_cloud_com\)',
              conv_src) is not None)

# —— 改写打印设置的唯一入口：_apply_print_options*，且只在显式传 print_options 时调用 ——
# 这里守的是「新契约」（2026-11-03），不是「允许随便改版式」：三条铁律落成可红断言 ——
#   ① 没有散落的 `ws.PageSetup.X = v`，写入只经 setattr(ps, name, value)（失败能记账/降级）
#   ② 只有 print_options 非 None 才走到写入（None ⇒ 旧路径零改动，旧缓存照常命中）
#   ③ 版式只落在 export_to_pdf 复制出来的临时副本上（源文件 ReadOnly 打开、不保存）
WRITE_PAGE_RE = re.compile(
    r"\.(?:LeftMargin|RightMargin|TopMargin|BottomMargin|HeaderMargin|FooterMargin|"
    r"Zoom|FitToPagesWide|FitToPagesTall|PrintArea|PrintTitleRows|PrintTitleColumns|"
    r"CenterHorizontally|CenterVertically|Orientation|PaperSize|PrintHeadings|"
    r"PrintGridlines)\s*=[^=]")
chk("没有散落的 PageSetup 直接赋值（改写版式只能走 _apply_print_options_on_sheet 的 setattr）",
    WRITE_PAGE_RE.search(xsl_src) is None,
    WRITE_PAGE_RE.search(xsl_src).group(0) if WRITE_PAGE_RE.search(xsl_src) else "")
chk("PageSetup 写入统一用 setattr(ps, name, value)（逐项记账，不支持时能点名降级）",
    "setattr(ps, name, value)" in xsl_src)
chk("print_options=None ⇒ 不调用 _apply_print_options（旧路径逐字节不变）",
    re.search(r"if options is not None:\s*\n(?:.*\n){0,4}?\s+\w+\s*=\s*_apply_print_options\(",
              xsl_src) is not None)
chk("选项进缓存键（cache_key / find_cached_pdf 都接 print_options；改页边距换缓存）",
    'def cache_key(source_md5: str, image_orientation: str = "", engine: str = "",' in xsl_src
    and re.search(r"def find_cached_pdf\(.*?print_options", xsl_src, re.S) is not None)
chk("版式只写在临时副本上（ReadOnly 打开 + Close(SaveChanges=False)）",
    re.search(r"ReadOnly\s*=\s*True", xsl_src) is not None
    and re.search(r"Close\(\s*SaveChanges\s*=\s*False", xsl_src) is not None)
chk("LibreOffice 不认这些参数时必须明说「设置未生效」",
    "页边距/缩放/打印范围设置" in xsl_src and "未生效" in xsl_src)
chk("converter 把 print_options 透传给 xlsx_printer（GUI 面板 → 转换链路）",
    "print_options=print_options" in conv_src)
chk("xlsx_printer 不调用 PrintOut（走 ExportAsFixedFormat 而不是真打印机）",
    "PrintOut" not in xsl_src)
chk("xlsx_printer 从不把 Excel 设成可见",
    re.search(r"Visible\s*=\s*True", xsl_src) is None)
chk("导出参数含 Type=0（PDF）", re.search(r"Type\s*=\s*0", xsl_src) is not None)
chk("导出参数含 IgnorePrintAreas=False（尊重用户的打印区域）",
    re.search(r"IgnorePrintAreas\s*=\s*False", xsl_src) is not None)
chk("导出参数含 OpenAfterPublish=False（不弹阅读器）",
    re.search(r"OpenAfterPublish\s*=\s*False", xsl_src) is not None)
chk("只读打开：ReadOnly=True",
    re.search(r"ReadOnly\s*=\s*True", xsl_src) is not None)
chk("关工作簿不保存：Close(SaveChanges=False)",
    re.search(r"Close\(\s*SaveChanges\s*=\s*False", xsl_src) is not None
    or re.search(r"Close\(False\)", xsl_src) is not None)
chk("禁止宏：AutomationSecurity=3（MSO_AUTOMATION_SECURITY_FORCE_DISABLE）",
    re.search(r"AutomationSecurity\s*=\s*3", xsl_src) is not None)
chk("引擎优先级 Excel → WPS → LibreOffice",
    re.search(r'ENGINE_ORDER\s*=\s*\(\s*"excel"\s*,\s*"wps"\s*,\s*"libreoffice"\s*\)',
              xsl_src) is not None)
chk("WPS 走 KET.Application（本机实测可用的表格 ProgID）",
    re.search(r'"wps"\s*:\s*"KET\.Application"', xsl_src) is not None)
chk("LibreOffice 兜底必须明写「版式可能与 Excel 不一致」",
    "LibreOffice 兜底，版式可能与 Excel 不一致" in xsl_src)
chk("导出粒度=活动工作表（wb.ActiveSheet，与 Excel「打印活动工作表」一致）",
    "ActiveSheet" in xsl_src)
chk("多工作表时打 WARNING（避免用户以为整本都印了）",
    re.search(r"logger\.warning\(\s*\n\s*f\"[^\"]*活动工作表", xsl_src) is not None)

# —— GUI 接线 ——
chk("gui 引入 xlsx_printer", re.search(r"^\s*import xlsx_printer", gui_src, re.M) is not None)
chk("拖入白名单含 .xls / .xlsx",
    re.search(r'allowed_types\s*=\s*\{[^}]*"\.xlsx"', gui_src, re.S) is not None
    and re.search(r'allowed_types\s*=\s*\{[^}]*"\.xls"', gui_src, re.S) is not None)
_label_body = gui_src.split("def _format_engine_label(", 1)[-1].split("\ndef ", 1)[0]
chk("引擎标签：表格显示 Excel（不再写 LibreOffice）",
    'return "Excel"' in _label_body and 'return "LibreOffice"' not in _label_body,
    _label_body[:200])
chk("转换 worker 有表格分支（按引擎身份落盘缓存）",
    re.search(r"(?:xlsx_printer|_xp)\.(?:export_to_pdf|export_and_cache_tag)", gui_src) is not None)
chk("缓存读侧用 xlsx_printer.find_cached_pdf（含引擎身份）",
    re.search(r"(?:xlsx_printer|_xp)\.find_cached_pdf", gui_src) is not None)
chk("缓存写侧补索引条目（否则被 retention 当孤儿删）",
    re.search(r"(?:xlsx_printer|_xp)\.upsert_cache_index", gui_src) is not None)
chk("文件卡片不再把表格标成「不支持类型」",
    re.search(r"excel_warning\s*=\s*is_excel", card_src) is None)
chk("文件卡片给表格一个表格图标（📊）", "📊" in card_src)

# ============================================================
# B. 行为（假 COM）
# ============================================================
print("\n=== B. 行为（假 COM，不启动真 Office）===")
import xlsx_printer as X  # noqa: E402

tmp = newdir("hn_xlsx_behavior_")
sample_src = os.path.join(tmp, "sample.xlsx")
if os.path.isfile(XLSX_RANK):
    shutil.copy2(XLSX_RANK, sample_src)          # 只作为「输入路径」用，不会被真打开
else:
    with zipfile.ZipFile(sample_src, "w") as z:
        z.writestr("xl/workbook.xml", "<workbook/>")

_ORIG_DISPATCH = X._dispatch_app


def _fake_dispatch(app_, owned):
    X._dispatch_app = lambda engine, _a=app_, _o=owned: (_a, _o)


def _restore_dispatch():
    X._dispatch_app = _ORIG_DISPATCH


# 1) 复用用户已开的实例（owned=False）：只读打开 + 导出参数 + Close + 不 Quit
wb = FakeWorkbook(sheets=("附件4", "Sheet1"), active="Sheet1")
out1 = os.path.join(tmp, "b1.pdf")
app = FakeApp(wb)
_fake_dispatch(app, False)
try:
    res1 = X._export_with_com("excel", sample_src, out1)
finally:
    _restore_dispatch()

chk("复用用户已开的 Excel 时绝不 Quit", app.quit_calls == 0, "quit_calls=%d" % app.quit_calls)
chk("导出后 Close(SaveChanges=False)（不改用户文件）",
    wb.close_kwargs is not None and (
        wb.close_kwargs[1].get("SaveChanges") is False or wb.close_kwargs[0] == (False,)),
    wb.close_kwargs)
kw = wb.ActiveSheet.ExportAsFixedFormat.kwargs or {}
chk("只导出活动工作表（ActiveSheet），不是整本工作簿",
    bool(kw or wb.ActiveSheet.ExportAsFixedFormat.args), "没调用 ExportAsFixedFormat")
chk("非活动工作表一张都没被导出（整本导出会变成 12+5=17 页）",
    all(s.ExportAsFixedFormat.kwargs is None and s.ExportAsFixedFormat.args is None
        for s in wb._sheets if s is not wb.ActiveSheet))
chk("ExportAsFixedFormat: Type=0", kw.get("Type") == 0, kw)
chk("ExportAsFixedFormat: IgnorePrintAreas=False", kw.get("IgnorePrintAreas") is False, kw)
chk("ExportAsFixedFormat: OpenAfterPublish=False", kw.get("OpenAfterPublish") is False, kw)
chk("Open: ReadOnly=True / UpdateLinks=0",
    (wb.open_kwargs is not None)
    and (wb.open_kwargs[2].get("ReadOnly") is True or wb.open_kwargs[1][2] is True)
    and (wb.open_kwargs[2].get("UpdateLinks") == 0 or wb.open_kwargs[1][0] == 0),
    wb.open_kwargs)
chk("结果里带 sheet 名与工作表数量（日志可追溯）",
    res1.sheet == "Sheet1" and res1.sheet_count == 2, (res1.sheet, res1.sheet_count))
chk("hardened 六项全 True",
    all(res1.hardened.get(k) for k in (
        "Visible=False", "DisplayAlerts=False", "EnableEvents=False",
        "Interactive=False", "AskToUpdateLinks=False", "AutomationSecurity=3")),
    res1.hardened)

# 2) 自己启动的实例（owned=True）才 Quit
wb2 = FakeWorkbook()
app2 = FakeApp(wb2)
out2 = os.path.join(tmp, "b2.pdf")
_fake_dispatch(app2, True)
try:
    X._export_with_com("excel", sample_src, out2)
finally:
    _restore_dispatch()
chk("自己启动的 Excel 实例退出时 Quit（不留残留进程）", app2.quit_calls == 1,
    "quit_calls=%d" % app2.quit_calls)

# 3) 引擎降级链（桩掉真 COM）
print("\n--- 引擎降级链 ---")


def _stub_pdf(out):
    with open(out, "wb") as f:
        f.write(b"%PDF-1.4\n%stub\n")


def run_export(**kw):
    d = newdir("hn_xlsx_dg_")
    fake_src = os.path.join(d, "a.xlsx")
    if os.path.isfile(XLSX_RANK):
        shutil.copy2(XLSX_RANK, fake_src)
    else:                                    # 别的机器没有参考文件时也能跑行为判据
        with zipfile.ZipFile(fake_src, "w") as z:
            z.writestr("xl/workbook.xml", "<workbook/>")
    out = os.path.join(d, "a.pdf")
    seen = []
    orig_com, orig_lo, orig_avail = X._export_with_com, X._export_with_libreoffice, X.engine_available

    def com(engine, s, o):
        seen.append(engine)
        _stub_pdf(o)
        return X.ExportResult(engine=engine, pdf_path=o, sheet="S", sheet_count=1)

    def lo(s, o):
        seen.append("libreoffice")
        _stub_pdf(o)
        return X.ExportResult(engine="libreoffice", pdf_path=o, fallback=True)

    X._export_with_com, X._export_with_libreoffice = com, lo
    # 引擎可用性由本桩决定（默认「三引擎都装了」），不受 HN_NO_ENGINE_PROBE 影响
    X.engine_available = kw.pop("avail", lambda e: True)
    try:
        res = X.export_to_pdf(fake_src, out, **kw)
        return res, seen, None
    except Exception as e:  # noqa: BLE001
        return None, seen, e
    finally:
        X._export_with_com, X._export_with_libreoffice = orig_com, orig_lo
        X.engine_available = orig_avail


res, seen, _ = run_export()
chk("装齐时首选 Excel", seen == ["excel"], seen)
res, seen, _ = run_export(avail=lambda e: e != "excel")
chk("Excel 不可用 ⇒ 降级 WPS", seen == ["wps"], seen)
res, seen, _ = run_export(avail=lambda e: e == "libreoffice")
chk("Excel/WPS 都不可用 ⇒ LibreOffice 兜底", seen == ["libreoffice"], seen)
chk("兜底结果标记 fallback=True（便于上层提示版式差异）",
    res is not None and res.fallback is True, res and res.fallback)
res, seen, err = run_export(from_cloud=True, avail=lambda e: True)
chk("云端顾客上传：只走 LibreOffice（不给 COM 执行机会）", seen == ["libreoffice"], seen)
res, seen, err = run_export(from_cloud=True, allow_cloud_com=True)
chk("管理员放行后云端表格可以走 Excel", seen == ["excel"], seen)
res, seen, err = run_export(avail=lambda e: False)
chk("一个引擎都没装 ⇒ XlsxExportError 且提示装 Office/LibreOffice",
    isinstance(err, X.XlsxExportError) and "Excel" in str(err), err)

# 4) 失败面（预检）
print("\n--- 预检失败面 ---")
d = newdir("hn_xlsx_bad_")
empty = os.path.join(d, "empty.xlsx")
open(empty, "wb").close()
try:
    X.validate_xlsx(empty)
    chk("0 字节文件 ⇒ 明确报错", False)
except X.XlsxExportError as e:
    chk("0 字节文件 ⇒ 明确报错", "空的" in str(e), e)

bad = os.path.join(d, "bad.xlsx")
with open(bad, "wb") as f:
    f.write(b"PK\x03\x04this is not a real zip")
try:
    X.validate_xlsx(bad)
    chk("损坏的 zip ⇒ 明确报错", False)
except X.XlsxExportError as e:
    chk("损坏的 zip ⇒ 明确报错", "损坏" in str(e), e)

notxlsx = os.path.join(d, "renamed.docx")
with zipfile.ZipFile(notxlsx, "w") as z:
    z.writestr("word/document.xml", "<w/>")
try:
    X.validate_xlsx(notxlsx)
    chk("改了后缀的非表格 ⇒ 明确报错", False)
except X.XlsxExportError as e:
    chk("改了后缀的非表格 ⇒ 明确报错", "xl/workbook.xml" in str(e), e)

enc = os.path.join(d, "locked.xlsx")
with open(enc, "wb") as f:
    f.write(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 32
            + "EncryptedPackage".encode("utf-16-le") + b"\x00" * 64)
try:
    X.validate_xlsx(enc)
    chk("密码保护（EncryptedPackage）⇒ 明确报错并给出解法", False)
except X.XlsxExportError as e:
    chk("密码保护（EncryptedPackage）⇒ 明确报错并给出解法",
        "加密" in str(e) and "密码" in str(e), e)

# 5) 缓存 key 含引擎身份
k_excel = X.cache_key("abc123", "", "excel")
k_wps = X.cache_key("abc123", "", "wps")
k_lo = X.cache_key("abc123", "", "libreoffice")
chk("缓存 key 含引擎身份（不同引擎结果不同，不能混用）",
    len({k_excel, k_wps, k_lo}) == 3, (k_excel, k_wps, k_lo))
chk("表格的引擎后缀与 docx 的 _eword/_ewps 不同（不会被 _purge_stale_engine_entries 误删）",
    all("_x" in k for k in (k_excel, k_wps, k_lo)) and "_eword" not in k_excel,
    (k_excel, k_wps))
cdir = newdir("hn_xlsx_cache_")
# 缓存探测只认「当前可用引擎」：这里显式给出引擎池，判据不受 HN_NO_ENGINE_PROBE 影响
_orig_live = X.live_engines
X.live_engines = lambda: ["excel", "wps", "libreoffice"]
try:
    with open(os.path.join(cdir, k_excel + ".pdf"), "wb") as f:
        f.write(b"%PDF-1.4\n%cache\n")
    found = X.find_cached_pdf("abc123", "", cache_dir=cdir)
    chk("缓存命中：找到 Excel 产出的那份", found == os.path.join(cdir, k_excel + ".pdf"), found)
    os.remove(found)
    with open(os.path.join(cdir, k_wps + ".pdf"), "wb") as f:
        f.write(b"%PDF-1.4\n%cache\n")
    X.live_engines = lambda: ["excel"]          # Excel 可用、WPS 不可用
    chk("只跳过不可用引擎的旧产物（WPS 卸了就不再复用它的 _xwps 缓存）",
        X.find_cached_pdf("abc123", "", cache_dir=cdir) is None)
    X.live_engines = lambda: ["wps", "libreoffice"]
    chk("可用引擎切换到 WPS 后命中 _xwps 缓存",
        X.find_cached_pdf("abc123", "", cache_dir=cdir)
        == os.path.join(cdir, k_wps + ".pdf"))
    os.remove(os.path.join(cdir, k_wps + ".pdf"))
    with open(os.path.join(cdir, k_lo + ".pdf"), "wb") as f:
        f.write(b"not a pdf at all")
    X.live_engines = lambda: ["libreoffice"]
    chk("坏缓存（非 %PDF 头）不算命中",
        X.find_cached_pdf("abc123", "", cache_dir=cdir) is None)

    # LibreOffice 兜底必须自己"喊出来"：运维/用户要能从日志看出这份 PDF 不能当 Excel 用
    import logging as _logging

    class _Capture(_logging.Handler):
        def __init__(self):
            super().__init__()
            self.msgs = []

        def emit(self, record):
            self.msgs.append(record.getMessage())

    _cap = _Capture()
    _orig_level = X.logger.level
    X.logger.addHandler(_cap)
    X.logger.setLevel(_logging.DEBUG)   # 本脚本把 root 设成 ERROR 静音，这里单独放开
    try:
        import converter as _conv
        _orig_lo = _conv._convert_office_via_libreoffice

        def _fake_lo(src, out):
            with open(out, "wb") as f:
                f.write(b"%PDF-1.4\n%lo\n")

        _conv._convert_office_via_libreoffice = _fake_lo
        try:
            _lo_src = os.path.join(cdir, "lo_src.xlsx")
            with open(_lo_src, "wb") as f:
                f.write(b"PK\x03\x04")
            _lo_res = X._export_with_libreoffice(_lo_src, os.path.join(cdir, "lo_out.pdf"))
        finally:
            _conv._convert_office_via_libreoffice = _orig_lo
    finally:
        X.logger.removeHandler(_cap)
        X.logger.setLevel(_orig_level)
    chk("B-LO 兜底在日志里明写「LibreOffice 兜底，版式可能与 Excel 不一致」",
        any("LibreOffice 兜底，版式可能与 Excel 不一致" in m for m in _cap.msgs),
        str(_cap.msgs)[:120])
    chk("B-LO 兜底结果带 fallback=True 且 engine=libreoffice",
        _lo_res.fallback is True and _lo_res.engine == "libreoffice", _lo_res.engine)

    # 收尾保险：只确认/强制结束**我们自己启动**的那个实例（PID 会被复用 ⇒ 还要认进程名）
    _me = os.getpid()
    _me_img = X._pid_image_name(_me)
    chk("B-收尾 进程探针可用（存活判定 + 映像名）",
        X._pid_alive(_me) and _me_img.endswith(".exe"), _me_img)
    chk("B-收尾 活进程不会被误判成已退出（等满超时后返回 False）",
        X._wait_pid_exit(_me, timeout=0.6) is False, "")
    chk("B-收尾 强制结束前核对进程名（只认 excel.exe / wps.exe 这类映像）",
        X._APP_IMAGES.get("excel") == ("excel.exe",)
        and "python.exe" not in X._APP_IMAGES.get("excel", ())
        and "python.exe" not in X._APP_IMAGES.get("wps", ()), "")
    _src_xsl = open(X.__file__, encoding="utf-8").read()
    chk("B-收尾 导出收尾会等实例退出，必要时只对自己的 PID 兜底强制结束",
        "_wait_pid_exit(pid)" in _src_xsl and "_pid_image_name(pid)" in _src_xsl
        and "_terminate_pid(pid)" in _src_xsl, "")
finally:
    X.live_engines = _orig_live

# ============================================================
# C. 真机 parity
# ============================================================
print("\n=== C. 真机 parity（Excel/WPS 自己排版）===")
probe_off = (os.environ.get("HN_NO_ENGINE_PROBE") or "").strip() == "1"
engines = [] if probe_off else X.live_engines()
have_ref = os.path.isfile(XLSX_SCORE) and os.path.isfile(PDF_SCORE) \
    and os.path.isfile(XLSX_RANK) and os.path.isfile(PDF_RANK)

if probe_off:
    skip("C/D 真机 parity + 变异自证",
         "HN_NO_ENGINE_PROBE=1（run_all 会设它；直接跑本脚本即真跑）")
elif not engines:
    skip("C/D 真机 parity + 变异自证", "本机没探测到 Excel / WPS / LibreOffice")
elif not have_ref:
    skip("C/D 真机 parity + 变异自证", "参考文件不在本机：" + REF_DIR)
else:
    info("可用引擎: %s" % " → ".join(X.engine_label(e) for e in engines))

    def excel_pids():
        try:
            p = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "Get-Process EXCEL -ErrorAction SilentlyContinue | "
                 "Select-Object -ExpandProperty Id"],
                capture_output=True, text=True, timeout=60)
            return {int(t) for t in p.stdout.split() if t.strip().isdigit()}
        except Exception:  # noqa: BLE001
            return set()

    pids_before = excel_pids()
    md5_before = {p: md5_of(p) for p in (XLSX_SCORE, XLSX_RANK)}
    mtime_before = {p: os.path.getmtime(p) for p in (XLSX_SCORE, XLSX_RANK)}
    work = newdir("hn_xlsx_parity_")

    # ---- C1. 综测排名表：与参考 PDF 逐页 parity ----
    print("\n--- C1. 综测排名表（参考 PDF 打自同一份文件状态）---")
    ours_rank = os.path.join(work, "rank_ours.pdf")
    res_rank = X.export_to_pdf(XLSX_RANK, ours_rank)
    g_ours = page_geom(ours_rank)
    g_ref = page_geom(PDF_RANK)
    info("引擎=%s 工作表=%s（共 %d 个） 页数=%d（参考 %d） 耗时 %.2fs"
         % (res_rank.engine_label, res_rank.sheet, res_rank.sheet_count,
            len(g_ours), len(g_ref), res_rank.elapsed))
    chk("C1 页数与 Excel 打印一致（5 页）", len(g_ours) == len(g_ref) == 5,
        "ours=%d ref=%d" % (len(g_ours), len(g_ref)))
    worst = {"dx": 0.0, "dy": 0.0, "dr": 0.0}
    text_ok = True
    for i, (a, b) in enumerate(zip(g_ours, g_ref)):
        chk("C1 p%d 纸张尺寸 A4 横向 29.70×21.00cm" % (i + 1),
            abs(a["w"] - 841.92) < 1.5 and abs(a["h"] - 595.32) < 1.5, (a["w"], a["h"]))
        if a["box"] is None or b["box"] is None:
            chk("C1 p%d 有文字" % (i + 1), False, "ours/ref 有空页")
            continue
        dx = abs(as_cm(a["box"][0] - b["box"][0]))
        dy = abs(as_cm(a["box"][1] - b["box"][1]))
        dr = abs(as_cm(a["box"][2] - b["box"][2]))
        worst["dx"] = max(worst["dx"], dx)
        worst["dy"] = max(worst["dy"], dy)
        worst["dr"] = max(worst["dr"], dr)
        text_ok = text_ok and (a["text"] == b["text"])
        chk("C1 p%d 文字 bbox 与参考 PDF 一致（左/上 ≤0.10cm，右 ≤0.15cm）" % (i + 1),
            dx <= 0.10 and dy <= 0.10 and dr <= 0.15,
            "dx=%.3f dy=%.3f dright=%.3f cm" % (dx, dy, dr))
    info("最大偏差: 左 %.3fcm / 上 %.3fcm / 右 %.3fcm" % (worst["dx"], worst["dy"], worst["dr"]))
    chk("C1 每页文字内容与参考 PDF 完全相同（去空白后逐字符相等）", text_ok)
    chk("C1 首页文字从 0.5cm 边距起（参考 PDF 实测 1.583cm）",
        abs(as_cm(g_ours[0]["box"][0]) - 1.583) <= 0.10,
        "minx=%.3fcm" % as_cm(g_ours[0]["box"][0]))

    # ---- C2. 成绩汇总表：文件自身状态（缩放≈10%）的忠实复现 ----
    print("\n--- C2. 成绩汇总表（参考 PDF 打自另一份 (4) 副本，见脚本头说明）---")
    ours_score = os.path.join(work, "score_ours.pdf")
    res_score = X.export_to_pdf(XLSX_SCORE, ours_score)
    g_s = page_geom(ours_score)
    info("引擎=%s 工作表=%s 页数=%d 耗时 %.2fs"
         % (res_score.engine_label, res_score.sheet, len(g_s), res_score.elapsed))
    chk("C2 页数与 Excel 对这份文件的打印一致（4 页）", len(g_s) == 4, len(g_s))
    chk("C2 纸张 A4 横向 29.70×21.00cm",
        abs(g_s[0]["w"] - 841.92) < 1.5 and abs(g_s[0]["h"] - 595.32) < 1.5,
        (g_s[0]["w"], g_s[0]["h"]))
    minx, miny = as_cm(g_s[0]["box"][0]), as_cm(g_s[0]["box"][1])
    chk("C2 首页文字从 0.5cm 边距起（左 ≈0.58cm，上 ≈0.57cm）",
        abs(minx - 0.58) <= 0.06 and abs(miny - 0.57) <= 0.06,
        "minx=%.3f miny=%.3f cm" % (minx, miny))
    chk("C2 如实复现文件的「调整为 1 页宽」结果：文字只占页宽不到 1/4（没被我们放大或重排）",
        g_s[0]["box"][2] < 0.25 * g_s[0]["w"],
        "maxx=%.1fpt / 页宽 %.1fpt" % (g_s[0]["box"][2], g_s[0]["w"]))

    # PageSetup 直读：Excel 自己怎么看这份文件（证明我们按文件排版，不是按自己的默认值）
    def read_pagesetup(path):
        """用 Excel 自己读 PageSetup（权威），并确认这次自查实例确实退出了。

        验收脚本自己开 COM 会话时同样要"只退自己启动的实例 + 确认进程退出"，
        否则会污染后面的 C3「无残留 Excel」判据（实测过一次：自查会话之后紧接的
        那次导出漏出一个 EXCEL.EXE /automation -Embedding）。
        """
        import pythoncom  # noqa: F401
        from converter import _dispatch_ex, _com_uninitialize_quietly
        pythoncom.CoInitialize()
        app = None
        pid = 0
        try:
            app = _dispatch_ex("Excel.Application")
            pid = X._pid_of_app(app)
            app.Visible = False
            app.DisplayAlerts = False
            wb = app.Workbooks.Open(path, 0, True)
            try:
                ws = wb.ActiveSheet
                ps = ws.PageSetup
                return {"sheet": ws.Name, "zoom": bool(ps.Zoom),
                        "fitw": ps.FitToPagesWide, "fith": ps.FitToPagesTall,
                        "left_cm": ps.LeftMargin / CM, "top_cm": ps.TopMargin / CM,
                        "hc": bool(ps.CenterHorizontally),
                        "used": ws.UsedRange.Address}
            finally:
                wb.Close(SaveChanges=False)
        finally:
            if app is not None:
                try:
                    app.Quit()
                except Exception:  # noqa: BLE001
                    pass
                if not X._wait_pid_exit(pid):
                    X._terminate_pid(pid)
                    X._wait_pid_exit(pid, timeout=3.0)
            _com_uninitialize_quietly()

    try:
        ps = read_pagesetup(XLSX_SCORE)
        info("Excel 直读: sheet=%s used=%s zoom=%s fitW=%s fitH=%s 左=%.2fcm 上=%.2fcm 水平居中=%s"
             % (ps["sheet"], ps["used"], ps["zoom"], ps["fitw"], ps["fith"],
                ps["left_cm"], ps["top_cm"], ps["hc"]))
        chk("C2 Excel 认定「调整为 1 页宽」（zoom=False, fitW=1, fitH=False）——即用户的设置",
            ps["zoom"] is False and ps["fitw"] == 1 and ps["fith"] is False, ps)
        chk("C2 Excel 认定的边距就是用户设的 0.5cm", abs(ps["left_cm"] - 0.5) <= 0.02
            and abs(ps["top_cm"] - 0.5) <= 0.02, (ps["left_cm"], ps["top_cm"]))
        chk("C2 Excel 认定水平居中已开", ps["hc"] is True, ps["hc"])
    except Exception as e:  # noqa: BLE001
        chk("C2 能直读 PageSetup（证明按文件排版）", False, e)

    # ---- C3. 不残留进程 / 源文件未变 / 确定性 / 页数统计 ----
    print("\n--- C3. 副作用 ---")
    deadline = time.time() + 15
    leaked = set()
    while True:
        leaked = excel_pids() - pids_before
        if not leaked or time.time() > deadline:
            break
        time.sleep(0.5)
    chk("C3 不残留 EXCEL.EXE（我们自己启动的实例已退出）", not leaked, leaked)

    same = all(md5_of(p) == md5_before[p] for p in md5_before)
    chk("C3 两个源文件 MD5 前后不变（只读打开，绝不改源文件）", same)
    chk("C3 源文件 mtime 未被刷新（Excel 没有回写）",
        all(abs(os.path.getmtime(p) - mtime_before[p]) < 1.0 for p in md5_before),
        [(os.path.getmtime(p) - mtime_before[p]) for p in md5_before])

    ours_rank2 = os.path.join(work, "rank_ours2.pdf")
    X.export_to_pdf(XLSX_RANK, ours_rank2)
    g2 = page_geom(ours_rank2)
    chk("C3 同一文件两次导出结果一致（页数 + 首页 bbox）",
        len(g2) == len(g_ours) and g2[0]["box"] == g_ours[0]["box"],
        (len(g2), g2[0]["box"], g_ours[0]["box"]))

    from pdf_printer import get_pdf_info  # noqa: E402
    info_rank = get_pdf_info(ours_rank)
    info_score = get_pdf_info(ours_score)
    chk("C3 页数统计与导出结果一致（5 / 4 页，横向）",
        info_rank["page_count"] == 5 and info_score["page_count"] == 4
        and info_rank["orientation"] == "landscape",
        (info_rank, info_score))

    # ============================================================
    # D. 变异自证
    # ============================================================
    print("\n=== D. 变异自证（判据必须真红）===")
    # 变异 A：成绩表边距 0.5cm → Excel 默认 1.8cm，并关掉水平居中
    mut_a = os.path.join(work, "score_mut_margins.xlsx")
    n = mutate_xlsx(XLSX_SCORE, mut_a, [
        (r"<pageMargins[^>]*/>", DEFAULT_MARGINS),
        (r'(<printOptions[^>]*?)horizontalCentered="1"', r'\1horizontalCentered="0"'),
    ], only_sheets_with='left="0.196850')
    chk("D 变异 A 生效（改到了工作表 XML）", n >= 1, n)
    out_a = os.path.join(work, "score_mut_margins.pdf")
    X.export_to_pdf(mut_a, out_a)
    ga = page_geom(out_a)
    a_minx, a_miny = as_cm(ga[0]["box"][0]), as_cm(ga[0]["box"][1])
    info("变异 A 首页: minx=%.3fcm miny=%.3fcm（原 %.3f/%.3f）"
         % (a_minx, a_miny, minx, miny))
    chk("D 变异 A 让「首页从 0.5cm 边距起」判据必然失败（说明该判据能红）",
        abs(a_minx - 0.58) > 0.06 or abs(a_miny - 0.57) > 0.06,
        "minx=%.3f miny=%.3f" % (a_minx, a_miny))
    chk("D 变异 A 边距被真的用上了（左上角整体右/下移 ≈1.2cm，而不是我们写死默认值）",
        a_minx > minx + 0.6 and a_miny > miny + 0.6,
        "dx=%.3f dy=%.3f cm" % (a_minx - minx, a_miny - miny))

    # 变异 B：综测 Sheet1 边距 0.5cm → 1.8cm，parity 必须失败
    mut_b = os.path.join(work, "rank_mut_margins.xlsx")
    n = mutate_xlsx(XLSX_RANK, mut_b, [
        (r"<pageMargins[^>]*/>", DEFAULT_MARGINS),
    ], only_sheets_with='left="0.196850')
    chk("D 变异 B 生效（改到了活动工作表的 XML）", n >= 1, n)
    out_b = os.path.join(work, "rank_mut_margins.pdf")
    X.export_to_pdf(mut_b, out_b)
    gb = page_geom(out_b)
    bx = abs(as_cm(gb[0]["box"][0] - g_ref[0]["box"][0]))
    info("变异 B 页数=%d（参考 %d）首页左偏差=%.3fcm" % (len(gb), len(g_ref), bx))
    chk("D 变异 B 让 C1 的 parity 判据必然失败（页数或几何超出容差）",
        len(gb) != len(g_ref) or bx > 0.10, (len(gb), bx))

    # 变异 C：scale 10 → 100 —— Excel 在「调整为 1 页宽」下忽略 scale（我们也没靠改它造假）
    mut_c = os.path.join(work, "score_scale100.xlsx")
    n = mutate_xlsx(XLSX_SCORE, mut_c, [
        (r'(<pageSetup[^>]*?)scale="10"', r'\1scale="100"'),
    ], only_sheets_with="<pageSetup")
    chk("D 变异 C 生效（改了 scale）", n >= 1, n)
    out_c = os.path.join(work, "score_scale100.pdf")
    X.export_to_pdf(mut_c, out_c)
    gc = page_geom(out_c)
    chk("D 变异 C 版式不变（Excel 忽略 scale ⇒ 我们的结果来自 Excel 的排版，不是改缩放改出来的）",
        len(gc) == len(g_s) and gc[0]["box"] == g_s[0]["box"],
        (len(gc), gc[0]["box"], g_s[0]["box"]))

finish()
