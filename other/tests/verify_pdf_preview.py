# -*- coding: utf-8 -*-
"""验收脚本 · 文件右键「👁 预览 PDF」（GUI 集成，offscreen）

需求（2026-11-09）：本地打印工具的文件列表右键菜单要能先把**转换好的 PDF**
用系统默认程序打开看一眼，再决定打不打。

为什么要这么验：
① 用户看到的必须是**真正要打印的那份**（排好版式的 pdf_cache 产物），不是原件；
② 重启后 `cached_pdf` 不持久化（printer_config.PrintJob.to_dict 里写明），
   如果只认 cached_pdf，重启后这个菜单永远是灰的 —— 必须能按缓存 key 找回产物；
③ 这份 PDF 可能还在转换 / 转换失败 / 缓存被清理：不能崩、不能弹一堆窗打断批量操作；
④ 锁定标签页（🔒 已上报订单）是只读快照，预览是只读操作，必须照常可用；
⑤ 表格文件的缓存 key 带版式（print_options），找错就会预览到"改了页边距之前的旧版式"。

    python tests/verify_pdf_preview.py     # exit 0 = 全绿
"""
import io
import os
import sys
import tempfile
import hashlib

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LOCAL_TOOL = os.path.join(REPO, "local_print_tool")

_SANDBOX = tempfile.mkdtemp(prefix="hn_pvpdf_")
os.environ["APPDATA"] = _SANDBOX                 # 用户数据目录 → 沙箱（别碰真实配置）
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, LOCAL_TOOL)
os.chdir(LOCAL_TOOL)

import paths  # noqa: E402
from PySide6.QtWidgets import QApplication, QTableWidgetSelectionRange  # noqa: E402

app = QApplication.instance() or QApplication([])

import converter as _converter  # noqa: E402
import gui as _gui  # noqa: E402

# 引擎可用性探测会真的 Dispatch Word/WPS COM（离屏测试里既慢又会留 wps.exe）→ 统一桩成可用
_converter.get_available_engines = lambda: {"word": True, "wps": True, "libreoffice": True}

_info_calls = []


def _fake_information(*a, **k):
    _info_calls.append((a[1] if len(a) > 1 else "", a[2] if len(a) > 2 else ""))
    return _gui.QMessageBox.Ok


_gui.QMessageBox.question = staticmethod(lambda *a, **k: _gui.QMessageBox.Yes)
_gui.QMessageBox.warning = staticmethod(lambda *a, **k: _gui.QMessageBox.Ok)
_gui.QMessageBox.information = staticmethod(_fake_information)

from gui import MainWindow  # noqa: E402
from printer_config import PrintJob, TabSettings  # noqa: E402

ok_all = True


def check(name, cond, detail=""):
    global ok_all
    if not cond:
        ok_all = False
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


# ── 用默认程序打开 = os.startfile；这里换成记录器（绝不真的拉起阅读器）──
_STARTFILE_CALLS = []


def _fake_startfile(path):
    _STARTFILE_CALLS.append(path)


_ORIG_STARTFILE = getattr(os, "startfile", None)
os.startfile = _fake_startfile

print("… 正在创建主窗口", flush=True)
win = MainWindow(config_path=paths.config_path())
win.resize(1400, 900)
win.show()
app.processEvents()

CACHE_DIR = paths.pdf_cache_dir()
os.makedirs(CACHE_DIR, exist_ok=True)


def fake_pdf(name, payload=b"%PDF-1.4\n% fake preview cache\n"):
    """在**真实的** pdf_cache 目录里造一个假缓存产物。"""
    p = os.path.join(CACHE_DIR, name)
    with open(p, "wb") as f:
        f.write(payload)
    return p


def job_of(path, cached="", page_count=2, engine="word", display_name=""):
    return PrintJob(file_path=path, copies=1, page_count=page_count, engine=engine,
                    display_name=display_name or os.path.basename(path),
                    cached_pdf=cached)


def seed(jobs, frozen=False):
    tab = TabSettings()
    tab.jobs = jobs
    tab.frozen = frozen
    win._config.tabs = {"1": tab}
    win._current_tab = "1"
    win._config.active_tab = "1"
    win._rebuild_table()
    win._refresh_tab_display()
    app.processEvents()
    return "1"


def menu_for_selection():
    win._table.selectRow(0)
    app.processEvents()
    return win._build_table_context_menu(
        win._table.visualRect(win._table.model().index(0, 0)).center())


def preview_action(menu):
    acts = [a for a in menu.actions() if "预览 PDF" in a.text()]
    return acts[0] if acts else None


def reset_calls():
    _STARTFILE_CALLS.clear()
    _info_calls.clear()


def make_source(name, payload=b"not a real document, only used as a path"):
    p = os.path.join(_SANDBOX, name)
    with open(p, "wb") as f:
        f.write(payload)
    return p


# ═══════════════ ① 菜单项：存在 / 可点 / 排在「打开文件位置」之前 ═══════════════
DOC = make_source("(2026) 预览测试.docx")
C1 = fake_pdf("pv_cached_one.pdf")
seed([job_of(DOC, C1)])
menu = menu_for_selection()
texts = [a.text() for a in menu.actions() if a.text()]
pv = preview_action(menu)
check("①a 菜单里有「预览 PDF」", pv is not None, " | ".join(texts))
check("①b 有缓存产物 ⇒ 可点", pv is not None and pv.isEnabled())
check("①c 文案就是「👁 预览 PDF」", pv is not None and pv.text() == "👁 预览 PDF",
      pv.text() if pv else "")
_i_pv = next((i for i, a in enumerate(menu.actions()) if "预览 PDF" in a.text()), -1)
_i_open = next((i for i, a in enumerate(menu.actions()) if "打开文件位置" in a.text()), -1)
check("①d 排在「打开文件位置」之前（只读操作归在一组）",
      0 <= _i_pv < _i_open, f"预览={_i_pv} 打开位置={_i_open}")

# ═══════════════ ② 点下去真的用默认程序打开那份缓存 PDF ═══════════════
reset_calls()
pv.trigger()
app.processEvents()
check("②a 调用了一次 os.startfile", len(_STARTFILE_CALLS) == 1, str(_STARTFILE_CALLS))
check("②b 打开的就是转换好的缓存产物", bool(_STARTFILE_CALLS)
      and os.path.normcase(_STARTFILE_CALLS[0]) == os.path.normcase(C1),
      str(_STARTFILE_CALLS))
check("②c 日志里留了一行（用户可回溯）", "预览 PDF" in win._log_text.toPlainText())

# ═══════════════ ③ 还没有 PDF：能现转的照旧可点（2026-11-13 改），真转不了的才灰 ═══════════════
# 改口径的原因：md/txt/csv 的页数云端永远分析不出来（页数分析只覆盖 pdf/doc/docx），
# 旧判据「cached_pdf / 原件是 PDF / page_count>0」三条都不成立 ⇒ 这类文件第一次打印之前
# 永远预览不了，用户的理解就是「md 不支持预览 PDF」。现在加了第 ④ 条「源文件可转换」。
TODO = make_source("还在转换.docx")
seed([job_of(TODO, "", page_count=0)])
menu3 = menu_for_selection()
pv3 = preview_action(menu3)
check("③a 没有产物但源文件可转换（docx）⇒ 菜单可点，点了先现转",
      pv3 is not None and pv3.isEnabled())

# 反过来：源文件在、但转换器不认这个扩展名 ⇒ 仍然灰掉，且 tooltip 要指路「重新转换」
NOPE = make_source("奇怪格式.xyz", b"not convertible")
seed([job_of(NOPE, "", page_count=0)])
menu3b = menu_for_selection()
pv3b = preview_action(menu3b)
check("③b 不可转换的源文件 + 页数未知 ⇒ 菜单项仍在但灰掉",
      pv3b is not None and not pv3b.isEnabled())
check("③c 灰掉时 tooltip 解释原因并指路「重新转换」",
      pv3b is not None and "重新转换" in (pv3b.toolTip() or ""),
      pv3b.toolTip() if pv3b else "")

# ═══════════════ ④ 原件本身就是 PDF ⇒ 直接预览原件 ═══════════════
SRC_PDF = make_source("原件.pdf", b"%PDF-1.4\n% original\n")
seed([job_of(SRC_PDF, "", page_count=0, engine="libreoffice")])
menu4 = menu_for_selection()
pv4 = preview_action(menu4)
check("④a 原件是 PDF ⇒ 可点（无需转换）", pv4 is not None and pv4.isEnabled())
reset_calls()
pv4.trigger()
app.processEvents()
check("④b 打开的是原件 PDF 本身", bool(_STARTFILE_CALLS)
      and os.path.normcase(_STARTFILE_CALLS[0]) == os.path.normcase(SRC_PDF),
      str(_STARTFILE_CALLS))

# ═══════════════ ⑤ 重启场景：cached_pdf 为空，按缓存 key 找回产物 ═══════════════
RESTART_DOC = make_source("重启后.docx", b"restart-case-bytes")
with open(RESTART_DOC, "rb") as f:
    _md5 = hashlib.md5(f.read()).hexdigest()
_key = _gui.pdf_cache_key(_md5, "auto", _gui.cache_engine_for(RESTART_DOC, "word"))
RESTART_CACHE = fake_pdf(_key + ".pdf")
seed([job_of(RESTART_DOC, "", page_count=3)])      # cached_pdf 空 = 刚重启
menu5 = menu_for_selection()
pv5 = preview_action(menu5)
check("⑤a 重启后（cached_pdf 空、页数已知）菜单仍可点",
      pv5 is not None and pv5.isEnabled())
check("⑤b 按同一套缓存 key 找回了产物",
      os.path.isfile(win._preview_pdf_source(win._get_current_jobs()[0])))
reset_calls()
pv5.trigger()
app.processEvents()
check("⑤c 打开的是按 key 找回的那份 PDF", bool(_STARTFILE_CALLS)
      and os.path.normcase(_STARTFILE_CALLS[0]) == os.path.normcase(RESTART_CACHE),
      str(_STARTFILE_CALLS))

# ═══════════════ ⑥ 表格文件：查找要带版式（print_options），不能拿旧版式那份 ═══════════════
XLSX = make_source("表格.xlsx", b"xlsx-bytes")
XLSX_CACHE = fake_pdf("pv_xlsx.pdf")
import xlsx_printer as _xp  # noqa: E402
_ORIG_FIND = _xp.find_cached_pdf
_captured = {}


def _fake_find(md5, image_orientation="", cache_dir="", print_options=None):
    _captured.update(md5=md5, orientation=image_orientation,
                     cache_dir=cache_dir, print_options=print_options)
    return XLSX_CACHE


_xp.find_cached_pdf = _fake_find
try:
    seed([job_of(XLSX, "", page_count=2, engine="libreoffice")])
    src6 = win._preview_pdf_source(win._get_current_jobs()[0])
    check("⑥a 表格走 xlsx_printer.find_cached_pdf", src6 == XLSX_CACHE, src6)
    check("⑥b 查找目录是 pdf_cache", os.path.normcase(_captured.get("cache_dir", ""))
          == os.path.normcase(CACHE_DIR), str(_captured.get("cache_dir")))
    check("⑥c 查找带了版式参数（与转换侧同一口径）", _captured.get("print_options")
          == _gui.resolve_xlsx_print_options(win._config.default_task_options, {}),
          repr(_captured.get("print_options")))
finally:
    _xp.find_cached_pdf = _ORIG_FIND

# ═══════════════ ⑦ 多选：文案报个数，逐个打开 ═══════════════
C7A = fake_pdf("pv_multi_a.pdf")
C7B = fake_pdf("pv_multi_b.pdf")
seed([job_of(make_source("多选A.docx"), C7A), job_of(make_source("多选B.docx"), C7B)])
win._table.selectRow(0)
win._table.setRangeSelected(
    QTableWidgetSelectionRange(1, 0, 1, win._table.columnCount() - 1), True)
app.processEvents()
menu7 = win._build_table_context_menu(
    win._table.visualRect(win._table.model().index(0, 0)).center())
pv7 = preview_action(menu7)
check("⑦a 多选时文案带文件数", pv7 is not None and "2 个文件" in pv7.text(),
      pv7.text() if pv7 else "")
reset_calls()
pv7.trigger()
app.processEvents()
check("⑦b 两个文件都被打开", len(_STARTFILE_CALLS) == 2, str(_STARTFILE_CALLS))

# ═══════════════ ⑧ 锁定标签页（只读快照）也能预览 ═══════════════
C8 = fake_pdf("pv_frozen.pdf")
seed([job_of(DOC, C8)], frozen=True)
menu8 = menu_for_selection()
pv8 = preview_action(menu8)
check("⑧a 冻结标签页：预览仍可用（只读操作）", pv8 is not None and pv8.isEnabled())
reset_calls()
pv8.trigger()
app.processEvents()
check("⑧b 冻结标签页点下去照样打开", len(_STARTFILE_CALLS) == 1, str(_STARTFILE_CALLS))

# ═══════════════ ⑨ 一个都打不开：不崩、不刷屏，只弹一次说明 ═══════════════
GONE = os.path.join(CACHE_DIR, "pv_gone_never_exists.pdf")
seed([job_of(os.path.join(_SANDBOX, "已经没了.docx"), GONE, page_count=2)])
menu9 = menu_for_selection()
pv9 = preview_action(menu9)
check("⑨a 页数已知 ⇒ 菜单可点（点下去再解释为什么没有）",
      pv9 is not None and pv9.isEnabled())
reset_calls()
n9 = win._preview_pdf_rows([0])
app.processEvents()
check("⑨b 没打开任何文件、也没抛异常", n9 == 0)
check("⑨c 只弹一次提示", len(_info_calls) == 1, str(_info_calls))
check("⑨d 提示里说清「还没有可预览」和下一步",
      bool(_info_calls) and "还没有可预览" in _info_calls[0][1]
      and "重新转换" in _info_calls[0][1],
      _info_calls[0][1] if _info_calls else "")
check("⑨e 没有调用 os.startfile", not _STARTFILE_CALLS, str(_STARTFILE_CALLS))

# ═══════════════ ⑩ 同一份 PDF 被多行引用 ⇒ 只开一个窗口 ═══════════════
SHARED = fake_pdf("pv_shared.pdf")
seed([job_of(make_source("同一份A.docx"), SHARED),
      job_of(make_source("同一份B.docx"), SHARED)])
reset_calls()
n10 = win._preview_pdf_rows([0, 1])
app.processEvents()
check("⑩a 返回按文件数记 2", n10 == 2, str(n10))
check("⑩b 只真正打开一个窗口（同源去重）", len(_STARTFILE_CALLS) == 1,
      str(_STARTFILE_CALLS))

# ═══════════════ ⑪ 判据本身：没产物且页数未知 = 不可预览 ═══════════════
ghost = job_of(os.path.join(_SANDBOX, "不存在.docx"), "", page_count=0)
check("⑪a 无产物 + 页数未知 ⇒ _preview_pdf_available False",
      win._preview_pdf_available(ghost) is False)
check("⑪b 解析路径返回空串（不抛）", win._preview_pdf_source(ghost) == "")

if _ORIG_STARTFILE is not None:
    os.startfile = _ORIG_STARTFILE

print()
print("=" * 60)
print("PASS" if ok_all else "FAIL")
sys.exit(0 if ok_all else 1)
