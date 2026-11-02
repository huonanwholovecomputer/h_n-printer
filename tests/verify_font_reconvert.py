# -*- coding: utf-8 -*-
"""验收脚本 · 右键「🔄 重新转换（重新检测字体）」（GUI 集成，offscreen）

需求（2026-11-02 现场，HN20261102 那台机上装了华文楷体/MingLiU/华文行楷）：
用户把缺的字体**装上**、又把文件**重新添加**一遍，⚠ 提示照旧。三处缓存叠着谁都拦得住重查：
① `font_check.installed_fonts()` 是进程内快照，中途装的字体（HKCU 那类）不重新枚举；
② 字体指纹因此没变 ⇒ `_queue_font_checks` 的 `font_fp == fp` 判据直接跳过；
③ 转换产物还在 `pdf_cache` ⇒ 重新添加走"缓存命中"分支，PDF 根本不会重转。

所以右键菜单要有一个**手动强制**入口，一次干完三件事：重新枚举字体 → 删这份文件的缓存 PDF
→ 重转 + 按新指纹重查字体。本脚本验的就是这三件事，以及"不许误删别人的缓存/原件"。

    python tests/verify_font_reconvert.py     # exit 0 = 全绿
"""
import io
import os
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL_TOOL = os.path.join(REPO, "local_print_tool")

_SANDBOX = tempfile.mkdtemp(prefix="hn_reconv_")
os.environ["APPDATA"] = _SANDBOX                 # 用户数据目录 → 沙箱（别碰真实配置）
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, LOCAL_TOOL)
os.chdir(LOCAL_TOOL)

import paths  # noqa: E402
from PySide6.QtWidgets import QApplication, QTableWidgetSelectionRange  # noqa: E402

app = QApplication.instance() or QApplication([])

import font_check  # noqa: E402
import gui as _gui  # noqa: E402

# 引擎可用性探测会真的 Dispatch Word/WPS COM（离屏测试里既慢又会留 wps.exe）→ 统一桩成可用
import converter as _converter  # noqa: E402
_converter.get_available_engines = lambda: {"word": True, "wps": True, "libreoffice": True}
_gui.QMessageBox.question = staticmethod(lambda *a, **k: _gui.QMessageBox.Yes)
_gui.QMessageBox.warning = staticmethod(lambda *a, **k: _gui.QMessageBox.Ok)
_gui.QMessageBox.information = staticmethod(lambda *a, **k: _gui.QMessageBox.Ok)

from gui import MainWindow  # noqa: E402
from printer_config import PrintJob, TabSettings  # noqa: E402

ok_all = True


def check(name, cond, detail=""):
    global ok_all
    if not cond:
        ok_all = False
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


# ═══════════════ ① font_check.refresh_installed 必须真的作废两级缓存 ═══════════════
# 用替身枚举函数，模拟"用户中途装了一个字体"（不碰真实注册表/GDI）
_orig_gdi = font_check._enum_gdi_fonts
_orig_reg = font_check._enum_registry_fonts
_orig_extra = set(font_check._installed_extra)
try:
    font_check._installed_extra.clear()
    font_check._enum_gdi_fonts = lambda: {"SimSun"}
    font_check._enum_registry_fonts = lambda: set()
    fp_before = font_check.refresh_installed()
    check("①a 首次枚举只有 SimSun", font_check.is_installed("SimSun"), fp_before)
    check("①b STKaiti 此刻算缺", not font_check.is_installed("STKaiti"))

    # 装上 STKaiti：不刷新时旧快照/指纹都不动（这正是现场报"装了也没用"的机制）
    font_check._enum_gdi_fonts = lambda: {"SimSun", "STKaiti"}
    check("①c 不刷新 ⇒ 指纹纹丝不动（现场 bug 的机制）",
          font_check.fonts_fingerprint() == fp_before and not font_check.is_installed("STKaiti"))

    fp_after = font_check.refresh_installed()
    check("①d refresh_installed 之后指纹变了", fp_after != fp_before, f"{fp_before} → {fp_after}")
    check("①e refresh_installed 之后新字体可查", font_check.is_installed("STKaiti"))
    check("①f 归一缓存也一起作废（不是只清 _installed）",
          font_check._norm("STKaiti") in font_check._installed_norm(),
          sorted(font_check._installed_norm())[:6])

    # Qt 看得见、GDI 枚举不到的家族名要能补种进来
    fp3 = font_check.refresh_installed(["QtOnly Family"])
    check("①g qt_families 补种进已装集合",
          font_check.is_installed("QtOnly Family") and fp3 != fp_after)
finally:
    font_check._enum_gdi_fonts = _orig_gdi
    font_check._enum_registry_fonts = _orig_reg
    font_check._installed_extra.clear()
    font_check._installed_extra.update(_orig_extra)
    font_check.refresh_installed()

# ═══════════════ ② 右键菜单项：文案 / 启用态 ═══════════════
print("… 正在创建主窗口", flush=True)
win = MainWindow(config_path=paths.config_path())
win.resize(1400, 900)
win.show()
app.processEvents()

CACHE_DIR = paths.pdf_cache_dir()
os.makedirs(CACHE_DIR, exist_ok=True)


def fake_cached_pdf(name="cached_x.pdf"):
    """在**真实的** pdf_cache 目录里造一个假缓存产物。"""
    p = os.path.join(CACHE_DIR, name)
    with open(p, "wb") as f:
        f.write(b"%PDF-1.4\n% fake cache\n")
    return p


def job_of(path, cached=""):
    return PrintJob(file_path=path, copies=1, page_count=2, engine="word",
                    display_name=os.path.basename(path), cached_pdf=cached)


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


DOC = os.path.join(_SANDBOX, "(2026) 霍尔效应及应用实验报告.docx")
with open(DOC, "wb") as f:
    f.write(b"not a real docx, only used as a path")   # 只当路径：本脚本不真转

C1 = fake_cached_pdf("cached_one.pdf")
seed([job_of(DOC, C1)])
win._table.selectRow(0)
app.processEvents()

menu = win._build_table_context_menu(win._table.visualRect(
    win._table.model().index(0, 0)).center())
texts = [a.text() for a in menu.actions() if a.text()]
reconv = [a for a in menu.actions() if "重新转换" in a.text()]
check("②a 未冻结标签页：菜单里有「重新转换」", len(reconv) == 1, " | ".join(texts))
check("②b 文案说明会重新检测字体",
      bool(reconv) and "重新检测字体" in reconv[0].text() and reconv[0].isEnabled(),
      reconv[0].text() if reconv else "")

# 冻结标签页（已上报订单的账目快照）⇒ 禁用，并给出替代路径
seed([job_of(DOC, C1)], frozen=True)
win._table.selectRow(0)
app.processEvents()
menu_f = win._build_table_context_menu(win._table.visualRect(
    win._table.model().index(0, 0)).center())
reconv_f = [a for a in menu_f.actions() if "重新转换" in a.text()]
check("②c 冻结标签页：菜单项仍在但灰掉",
      len(reconv_f) == 1 and not reconv_f[0].isEnabled())
check("②d 冻结时的 tooltip 指向「添加到新标签页」",
      bool(reconv_f) and "添加到新标签页" in (reconv_f[0].toolTip() or ""))

# 多选时文案报个数
seed([job_of(DOC, C1), job_of(os.path.join(_SANDBOX, "b.docx"))])
win._table.selectRow(0)
win._table.setRangeSelected(
    QTableWidgetSelectionRange(1, 0, 1, win._table.columnCount() - 1), True)
app.processEvents()
menu_m = win._build_table_context_menu(win._table.visualRect(
    win._table.model().index(0, 0)).center())
reconv_m = [a for a in menu_m.actions() if "重新转换" in a.text()]
check("②e 多选时文案带文件数", bool(reconv_m) and "2 个文件" in reconv_m[0].text(),
      reconv_m[0].text() if reconv_m else "")

# ═══════════════ ③ 动作本身：清缓存 + 重转 + 重查字体 ═══════════════
calls = {"convert": [], "queue": []}
_orig_start = win._start_convert_worker
_orig_queue = win._queue_font_checks
win._start_convert_worker = lambda row, fp, engine, tab_key="", **kw: calls["convert"].append(
    (row, os.path.basename(fp), engine, tab_key))
win._queue_font_checks = lambda jobs, tab_key="": calls["queue"].append(
    ([os.path.basename(j.file_path) for j in (jobs or [])], tab_key,
     [(j.font_checked, j.font_fp, list(j.missing_fonts)) for j in (jobs or [])]))

try:
    C2 = fake_cached_pdf("cached_two.pdf")
    seeded = job_of(DOC, C2)
    seeded.font_checked = True
    seeded.font_fp = "STALE_FP_0000"
    seeded.missing_fonts = ["华文楷体"]
    seeded.nonembedded_fonts = ["STSong"]
    seed([seeded])
    n = win._force_reconvert_rows([0])
    app.processEvents()

    check("③a 返回重转的文件数", n == 1, str(n))
    check("③b 缓存 PDF 已被删掉", not os.path.isfile(C2), C2)
    check("③c job.cached_pdf 已清空", not seeded.cached_pdf, repr(seeded.cached_pdf))
    check("③d 字体状态清零（否则 _queue_font_checks 会被 font_fp 挡住）",
          not seeded.font_checked and not seeded.font_fp
          and not seeded.missing_fonts and not seeded.nonembedded_fonts,
          f"checked={seeded.font_checked} fp={seeded.font_fp!r}")
    check("③e 真的发起了重转（同一条路径、同一个引擎）",
          len(calls["convert"]) == 1 and calls["convert"][0][1] == os.path.basename(DOC),
          str(calls["convert"]))
    check("③f 重转后把字体重查也排上了",
          len(calls["queue"]) == 1 and calls["queue"][0][0] == [os.path.basename(DOC)],
          str(calls["queue"]))
    check("③g 重查用的是「已清零」的状态（font_checked=False）",
          bool(calls["queue"]) and calls["queue"][0][2][0][0] is False,
          str(calls["queue"]))

    # 冻结标签页：一行都不许动
    calls["convert"].clear()
    calls["queue"].clear()
    C3 = fake_cached_pdf("cached_three.pdf")
    frozen_job = job_of(DOC, C3)
    seed([frozen_job], frozen=True)
    n = win._force_reconvert_rows([0])
    check("③h 冻结标签页拒绝重转（返回 0、缓存不动、不起 worker）",
          n == 0 and os.path.isfile(C3) and not calls["convert"] and not calls["queue"],
          f"n={n} exists={os.path.isfile(C3)}")
finally:
    win._start_convert_worker = _orig_start
    win._queue_font_checks = _orig_queue

# ═══════════════ ④ 删除缓存的三条护栏 ═══════════════
# ④a 别的任务还在用同一份缓存 ⇒ 不删（删了那一单会莫名重转）
SHARED = fake_cached_pdf("cached_shared.pdf")
j1, j2 = job_of(DOC, SHARED), job_of(os.path.join(_SANDBOX, "c.docx"), SHARED)
seed([j1, j2])
deleted = win._purge_job_pdf_cache(j1)
check("④a 另一任务引用同一缓存 ⇒ 不删", not deleted and os.path.isfile(SHARED))

# ④b 不在 pdf_cache 里的路径（用户直接拖进来的原件）⇒ 绝不能删
OUTSIDE = os.path.join(_SANDBOX, "原件.pdf")
with open(OUTSIDE, "wb") as f:
    f.write(b"%PDF-1.4\n% user original\n")
j3 = job_of(OUTSIDE, OUTSIDE)
seed([j3])
deleted = win._purge_job_pdf_cache(j3)
check("④b 缓存目录外的文件绝不删", not deleted and os.path.isfile(OUTSIDE))

# ④c 没有别的引用 ⇒ 删掉并返回 True
LONE = fake_cached_pdf("cached_lone.pdf")
j4 = job_of(DOC, LONE)
seed([j4])
deleted = win._purge_job_pdf_cache(j4)
check("④c 独占缓存 ⇒ 删掉", deleted and not os.path.isfile(LONE))

# ═══════════════ ⑤ 行 tooltip 要指路这个新入口 ═══════════════
tip_job = PrintJob(file_path=DOC, copies=1, page_count=2, engine="word",
                   display_name="x.docx")
tip_job.missing_fonts = ["华文楷体", "MingLiU"]
blocks = win._font_tip_blocks(tip_job)
check("⑤a 缺字体提示仍在", any("本机没装这些字体" in b for b in blocks), str(len(blocks)))
check("⑤b 提示改为指路右键「重新转换」",
      any("重新转换" in b and "重新添加" not in b for b in blocks),
      (blocks[-1].splitlines()[-1] if blocks else ""))

# 未嵌入那条仍然只讲"让生成方重导"，不能被改乱
ne_job = PrintJob(file_path=DOC, copies=1, page_count=2, engine="word",
                  display_name="y.pdf")
ne_job.nonembedded_fonts = ["STSong"]
blocks_ne = win._font_tip_blocks(ne_job)
check("⑤c 未嵌入提示不受影响（仍说让生成方重导）",
      any("让生成方重新导出" in b for b in blocks_ne), str(len(blocks_ne)))

print()
print("=" * 60)
print("PASS" if ok_all else "FAIL")
sys.exit(0 if ok_all else 1)
