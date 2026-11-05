#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""验收：右侧任务面板切换「转换引擎」后，自动按新引擎重新转换（2026-11-04 用户需求）。

用户现场：文件默认用 WPS 转成 PDF 了，他在右侧任务面板把引擎从 WPS 改成 Word ——
旧版只把 `job.engine` 改掉，已经转好的那份 PDF 原样留着，于是「面板写着 Word、
打印用的还是 WPS 的产物」；`.doc` 更硬，它的缓存 key 不带引擎后缀，就算重查缓存
也还是命中同一份 WPS 产物（见 cloud_client.cache_engine_for）。

本脚本钉住：
  ① 静态：`_auto_apply_edit` 只在引擎**真的变了**时记账，并在存配置之后触发重转；
  ② 真 MainWindow（.docx 分引擎缓存）：WPS→Word 只丢引用，**不删** `_ewps.pdf`
     （切回来可以免重转）；页数归零、按新引擎、带当前标签页发起重转；
  ③ 再切回 WPS：命中 `_ewps.pdf` 立刻回填页数，**根本不起转换线程**（不拉 COM）；
  ④ 真 MainWindow（.doc 单 key 缓存）：切引擎**必须删**旧 PDF，否则重转又命中；
  ⑤ 负例：只改份数不碰引擎 → 一个都不重转、缓存与页数原样保留；
  ⑥ 转换回调回来后，页数/方向/PDF 都换成新引擎的。

变异自证：把 `_auto_apply_edit` 尾部那行 `self._restart_convert_for_engine_change(
engine_switched)` 注释掉，②④⑥ 的检查会全红（不记账、旧 PDF 还在、页数不归零）。

退出码 0 = 全绿。
"""
import hashlib
import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(os.path.dirname(_HERE))
LOCAL_TOOL = os.path.join(_REPO, "local_print_tool")

_SANDBOX = tempfile.mkdtemp(prefix="hn_engine_switch_")
os.environ["APPDATA"] = _SANDBOX                  # 用户数据目录 → 沙箱（不碰真实配置）
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, LOCAL_TOOL)
os.chdir(LOCAL_TOOL)

from PySide6.QtCore import QItemSelectionModel  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

_app = QApplication.instance() or QApplication([])

import converter as _converter  # noqa: E402
# 引擎可用性探测会真的 Dispatch Word/WPS COM（离屏里慢且会拉起 wps.exe 残留）→ 桩掉
_converter.get_available_engines = lambda: {"word": True, "wps": True, "libreoffice": True}

import gui  # noqa: E402
import paths  # noqa: E402
from cloud_client import cache_engine_for, pdf_cache_key  # noqa: E402
from printer_config import PrintJob, TabSettings  # noqa: E402

# offscreen 下没人能回答模态框 → 一律自动回「是/好」
gui.QMessageBox.question = staticmethod(lambda *a, **k: gui.QMessageBox.Yes)
gui.QMessageBox.warning = staticmethod(lambda *a, **k: gui.QMessageBox.Ok)
gui.QMessageBox.information = staticmethod(lambda *a, **k: gui.QMessageBox.Ok)

ok_all = True


def check(name, cond, detail=""):
    global ok_all
    if not cond:
        ok_all = False
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


def section(title):
    print(f"\n=== {title} ===")


_src = open(os.path.join(LOCAL_TOOL, "gui.py"), encoding="utf-8").read()


def _method_body(src: str, header: str) -> str:
    """按缩进切出一个方法体（从 header 所在行到第一个缩进 <= 它的非空行为止）。"""
    i = src.find(header)
    if i < 0:
        return ""
    line_start = src.rfind("\n", 0, i) + 1
    base = len(src[line_start:i]) - len(src[line_start:i].lstrip())
    out = []
    pos = src.find("\n", i) + 1
    while 0 <= pos < len(src):
        end = src.find("\n", pos)
        line = src[pos:] if end < 0 else src[pos:end]
        if line.strip() and (len(line) - len(line.lstrip())) <= base:
            break
        out.append(line)
        if end < 0:
            break
        pos = end + 1
    return "\n".join(out)


# ══════════════════════════════════════════════════════════════════
# ① 静态：只有「引擎真的变了」才重转，且必须在存配置之后
# ══════════════════════════════════════════════════════════════════
section("① 静态：面板写回引擎 → 记账 → 重转")

_AUTO = _method_body(_src, "def _auto_apply_edit(")
check("① 找到 _auto_apply_edit", bool(_AUTO))
check("★ ① 引擎真变时才记账（改份数/双面不会触发重转）",
      'if (job.engine or "") != engine:' in _AUTO
      and "engine_switched.append((row, job.engine or \"\"))" in _AUTO)
check("★ ① 循环结束后触发重转（`_restart_convert_for_engine_change(engine_switched)`）",
      "self._restart_convert_for_engine_change(engine_switched)" in _AUTO)
_i_save = _AUTO.find("self._set_current_jobs(jobs)")
_i_restart = _AUTO.find("self._restart_convert_for_engine_change(engine_switched)")
check("① 重转在 _set_current_jobs 之后（重转回调按 file_path 找行，配置先落地）",
      0 <= _i_save < _i_restart, f"save@{_i_save} restart@{_i_restart}")
check("① 仍然只在「Word 文件 + 引擎控件可编辑」时写回（不串给图片/PDF）",
      "if is_word and engine_editable:" in _AUTO)

_RESTART = _method_body(_src, "def _restart_convert_for_engine_change(")
check("★ ① 新方法 _restart_convert_for_engine_change 存在", bool(_RESTART))
check("① 先作废旧产物再重转", "_invalidate_pdf_for_engine(job, old_engine, new_engine)" in _RESTART)
check("★ ① 重转期间页数归零（打印入口的页数闸门据此等新页数）",
      "job.page_count = 0" in _RESTART)
check("① 重转走既有入口 _start_convert_worker，且用**新**引擎（不直接调 converter.convert）",
      "self._start_convert_worker(" in _RESTART
      and "self._resolve_engine(job)" in _RESTART
      and "converter.convert(" not in _RESTART)
check("① 重转带上当前标签页（后台标签页/多标签时回写到正确的一页）",
      "tab_key=self._current_tab" in _RESTART)
check("① 云端任务按 S1-04 闸门传 from_cloud / allow_cloud_com",
      "from_cloud=_from_cloud" in _RESTART and "allow_cloud_com=" in _RESTART)

_INVAL = _method_body(_src, "def _invalidate_pdf_for_engine(")
check("★ ① 新方法 _invalidate_pdf_for_engine 存在", bool(_INVAL))
check("① 判「新引擎会不会命中同一文件名」必须走 cache_engine_for（与读/写/打印三侧同一口径）",
      "cache_engine_for(" in _INVAL and "pdf_cache_key(" in _INVAL)
check("① 命中同一文件名时真删文件（走 _purge_job_pdf_cache，别的任务还在用就不删）",
      "self._purge_job_pdf_cache(job)" in _INVAL)
_LOG_LINES = [ln for ln in _RESTART.splitlines() if "转换引擎已改为" in ln]
check("① 日志文案不用 markdown 星号（UI 不解析）",
      bool(_LOG_LINES) and "**" not in _LOG_LINES[0], f"{_LOG_LINES[:1]}")

# ══════════════════════════════════════════════════════════════════
# 真 MainWindow
# ══════════════════════════════════════════════════════════════════
from reportlab.lib.pagesizes import A4  # noqa: E402
from reportlab.pdfgen import canvas  # noqa: E402

_CACHE = paths.pdf_cache_dir()
os.makedirs(_CACHE, exist_ok=True)


def make_pdf(path: str, pages: int) -> str:
    c = canvas.Canvas(path, pagesize=A4)
    for i in range(pages):
        c.drawString(72, 760, f"page {i + 1}")
        c.showPage()
    c.save()
    return path


def make_source(name: str, payload: bytes) -> tuple[str, str]:
    """造一个假源文件，返回 (路径, MD5)。转换/缓存都只看 MD5 与后缀，不读内容。"""
    path = os.path.join(_SANDBOX, name)
    with open(path, "wb") as f:
        f.write(payload)
    return path, hashlib.md5(payload).hexdigest()


win = gui.MainWindow(config_path=paths.config_path())
win.resize(1400, 900)
win.show()
_app.processEvents()

_ORIG_START = gui.MainWindow._start_convert_worker
_calls: list[dict] = []


def _rec_start(self, row, file_path, engine, **kw):
    _calls.append({"row": row, "path": file_path, "engine": engine, **kw})


def stub_start():
    _calls.clear()
    gui.MainWindow._start_convert_worker = _rec_start


def unstub_start():
    gui.MainWindow._start_convert_worker = _ORIG_START


def seed(jobs):
    tab = TabSettings()
    tab.jobs = jobs
    win._config.tabs = {"1": tab}
    win._current_tab = "1"
    win._config.active_tab = "1"
    win._rebuild_table()
    win._refresh_tab_display()
    _app.processEvents()
    return jobs


def select_row(row: int):
    win._table.clearSelection()
    win._table.selectionModel().select(
        win._table.model().index(row, 0),
        QItemSelectionModel.Select | QItemSelectionModel.Rows)
    _app.processEvents()
    win._on_table_selection()
    _app.processEvents()


def engine_index(name: str) -> int:
    return gui.ENGINE_INDEX_OF[name]


# ── ② .docx：分引擎缓存（WPS→Word 只丢引用，不删 _ewps.pdf） ──────────
section("② 真 MainWindow（.docx）：WPS → Word 自动重转")

DOCX, MD5X = make_source("引擎切换.docx", b"fake docx for engine switch\n")
WPS_PDF = os.path.join(_CACHE, pdf_cache_key(MD5X, "auto", "wps") + ".pdf")
WORD_PDF = os.path.join(_CACHE, pdf_cache_key(MD5X, "auto", "word") + ".pdf")
make_pdf(WPS_PDF, 3)
check("② 素材：两个分引擎缓存名不同（.docx 才带引擎后缀）",
      os.path.basename(WPS_PDF) != os.path.basename(WORD_PDF)
      and os.path.basename(WPS_PDF).endswith("_ewps.pdf"),
      f"{os.path.basename(WPS_PDF)} / {os.path.basename(WORD_PDF)}")

job = PrintJob(file_path=DOCX, copies=1, duplex="off", page_count=3, orientation="portrait",
               engine="wps", source_md5=MD5X, cached_pdf=WPS_PDF,
               display_name="引擎切换.docx")
seed([job])
select_row(0)
check("② 前提：面板引擎停在 WPS", win._edit_engine.currentIndex() == engine_index("wps"),
      f"index={win._edit_engine.currentIndex()}")

stub_start()
win._edit_engine.setCurrentIndex(engine_index("word"))     # 等价于用户在下拉里选 Word
_app.processEvents()
unstub_start()

check("★ ② job.engine 已改成 word", job.engine == "word", f"{job.engine!r}")
check("★ ② 确实发起了重转，且用的是新引擎", len(_calls) == 1 and _calls[0]["engine"] == "word",
      f"{_calls}")
check("② 重转带对了行号与标签页",
      bool(_calls) and _calls[0]["row"] == 0 and _calls[0].get("tab_key") == "1", f"{_calls}")
check("② 本地任务不被当成云端任务（from_cloud=False）",
      bool(_calls) and _calls[0].get("from_cloud") is False)
check("★ ② 旧 PDF 的引用已清掉（不会拿 WPS 的产物去打）", job.cached_pdf == "",
      f"{job.cached_pdf!r}")
check("② .docx 的分引擎缓存**保留**（切回 WPS 可以免重转）", os.path.isfile(WPS_PDF))
check("★ ② 重转期间页数归零（旧页数是旧引擎排版出来的）", job.page_count == 0,
      f"page_count={job.page_count}")
check("② 表格「页数」列同步显示未知，而不是停在 3",
      win._table.item(0, gui.MainWindow.COL_PAGES).text() == win._pages_cell_text(job)
      and "3" not in win._table.item(0, gui.MainWindow.COL_PAGES).text(),
      f"{win._table.item(0, gui.MainWindow.COL_PAGES).text()!r}")

# ── ⑥ 转换回调：页数/方向/PDF 换成新引擎的 ──────────────────────────
section("⑥ 新引擎转换完成 → 页数/方向/PDF 跟着换")

make_pdf(WORD_PDF, 5)
win._on_convert_finished(0, DOCX, WORD_PDF, 5, "landscape", "")
_app.processEvents()
check("★ ⑥ 页数改成新引擎产物（3 → 5）", job.page_count == 5, f"page_count={job.page_count}")
check("⑥ cached_pdf 指向 Word 的产物", job.cached_pdf == WORD_PDF,
      f"{os.path.basename(job.cached_pdf or '(空)')}")
check("⑥ 方向跟着新产物走（横版）", job.orientation == "landscape", f"{job.orientation!r}")

# ── ③ 切回 WPS：命中 _ewps.pdf，不起转换线程 ─────────────────────────
section("③ 再切回 WPS：命中既有 _ewps.pdf，不重转")

_workers_before = len(getattr(win, "_convert_workers", []) or [])
win._edit_engine.setCurrentIndex(engine_index("wps"))
_app.processEvents()
check("★ ③ 切回 WPS 直接缓存命中（page_count=3，来自 _ewps.pdf）", job.page_count == 3,
      f"page_count={job.page_count}")
check("★ ③ 没有新起转换线程（不拉 Word/WPS COM）",
      len(getattr(win, "_convert_workers", []) or []) == _workers_before,
      f"{_workers_before} → {len(getattr(win, '_convert_workers', []) or [])}")
check("③ cached_pdf 回到 WPS 的那份", os.path.normcase(job.cached_pdf or "")
      == os.path.normcase(WPS_PDF), f"{os.path.basename(job.cached_pdf or '(空)')}")

# ── ④ .doc：单 key 缓存（切引擎必须删文件） ──────────────────────────
section("④ 真 MainWindow（.doc）：缓存 key 不带引擎 → 必须删旧 PDF")

DOC, MD5D = make_source("旧格式.doc", b"fake doc payload for engine switch\n")
DOC_PDF = os.path.join(_CACHE, pdf_cache_key(MD5D, "auto", cache_engine_for(DOC, "word")) + ".pdf")
make_pdf(DOC_PDF, 2)
check("④ 素材：.doc 的缓存名不带引擎后缀（word/wps 都会落到同一份）",
      cache_engine_for(DOC, "word") == "" and cache_engine_for(DOC, "wps") == "",
      f"{os.path.basename(DOC_PDF)}")

doc_job = PrintJob(file_path=DOC, copies=1, duplex="off", page_count=2, orientation="portrait",
                   engine="wps", source_md5=MD5D, cached_pdf=DOC_PDF,
                   display_name="旧格式.doc")
seed([doc_job])
select_row(0)
stub_start()
win._edit_engine.setCurrentIndex(engine_index("word"))
_app.processEvents()
unstub_start()

check("★ ④ 旧 PDF 文件被删掉（否则重转又会命中同一份 WPS 产物）",
      not os.path.isfile(DOC_PDF), f"{os.path.basename(DOC_PDF)}")
check("★ ④ 依然发起了重转，引擎=word",
      len(_calls) == 1 and _calls[0]["engine"] == "word", f"{_calls}")
check("④ 页数归零、引用清空", doc_job.page_count == 0 and doc_job.cached_pdf == "",
      f"{doc_job.page_count}/{doc_job.cached_pdf!r}")

# ── ⑤ 负例：只改份数 → 不重转 ───────────────────────────────────────
section("⑤ 负例：只改份数（引擎没变）→ 一个都不重转")

DOCX2, MD5Y = make_source("只改份数.docx", b"fake docx for copies change\n")
KEEP_PDF = os.path.join(_CACHE, pdf_cache_key(MD5Y, "auto", "word") + ".pdf")
make_pdf(KEEP_PDF, 4)
job2 = PrintJob(file_path=DOCX2, copies=1, duplex="off", page_count=4, orientation="portrait",
                engine="word", source_md5=MD5Y, cached_pdf=KEEP_PDF,
                display_name="只改份数.docx")
seed([job2])
select_row(0)
stub_start()
# 注意：CounterWidget.setValue() 是同步 API（**不发** valueChanged，防面板回灌误触发），
# 要模拟用户改份数必须点「+」按钮，才会走 valueChanged → _auto_apply_edit 这条真实路径。
win._edit_copies._btn_plus.click()
win._edit_copies._btn_plus.click()          # 1 → 2 → 3
_app.processEvents()
unstub_start()

check("⑤ 份数已写回", job2.copies == 3, f"copies={job2.copies}")
check("★ ⑤ 引擎没变 ⇒ 不重转（面板改参数不该反复重排 PDF）", not _calls, f"{_calls}")
check("⑤ 缓存与页数原样保留", job2.cached_pdf == KEEP_PDF and job2.page_count == 4
      and os.path.isfile(KEEP_PDF),
      f"{os.path.basename(job2.cached_pdf or '(空)')}/{job2.page_count}")

print("\n" + ("=" * 60))
print("结果：" + ("全部通过" if ok_all else "存在失败项"))
try:
    win.close()
except Exception:
    pass
sys.exit(0 if ok_all else 1)
