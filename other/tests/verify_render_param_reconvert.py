#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""验收：改「进 PDF 的渲染参数」后，文件列表显示 + PDF 重渲染一起更新（2026-11-06 用户第 2 条需求）。

用户现场：图片在右侧面板把方向从「横向」改成「竖向」，文件列表的「方向」列还是「横」，
但打印出来是对的 —— 因为打印时按 `job.image_orientation` 重新生成了 PDF，而界面
只作废了 `cached_pdf`、既没重转也没重画行，方向列读的还是旧的 `job.orientation`。

本脚本钉住：
  ① `get_image_info(路径, 方向)` 的返回值与「真的转出来的 PDF」方向一致（界面与打印同口径）；
  ② 真 MainWindow：图片面板改方向 → `job.image_orientation` 立刻写回、旧 PDF 引用作废、
     方向列**当场**变「竖」（不必等重转）、图片页数不被归零；
  ③ 事件循环下一拍自动发起一次重转，且带上正确的标签页；
  ④ 负例：只改份数 → 不重转、方向与缓存原样；
  ⑤ 表格版式（xlsx）「跟随默认」同样触发重转；
  ⑥ 「默认任务参数」改了版面且影响表格文件时，排队重转（静态）；
  ⑦ 变异自证：抽掉 `_auto_apply_edit` 尾部那行排队，判据必须变红。

退出码 0 = 全绿。
"""
import io
import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(os.path.dirname(_HERE))
LOCAL_TOOL = os.path.join(_REPO, "local_print_tool")

_SANDBOX = tempfile.mkdtemp(prefix="hn_render_param_")
os.environ["APPDATA"] = _SANDBOX
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, LOCAL_TOOL)
os.chdir(LOCAL_TOOL)

from PySide6.QtCore import QItemSelectionModel  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

_app = QApplication.instance() or QApplication([])

import converter as _converter  # noqa: E402
_converter.get_available_engines = lambda: {"word": True, "wps": True, "libreoffice": True}

import gui  # noqa: E402
import paths  # noqa: E402
import pdf_printer  # noqa: E402
from printer_config import PrintJob, TabSettings  # noqa: E402

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
# ① 界面口径 == 打印口径（真转一份 PDF 对比）
# ══════════════════════════════════════════════════════════════════
section("① get_image_info 与真转出来的 PDF 方向一致")

from PIL import Image  # noqa: E402

def make_img(name: str, w: int, h: int) -> str:
    p = os.path.join(_SANDBOX, name)
    Image.new("RGB", (w, h), (200, 200, 200)).save(p)
    return p


PORTRAIT_IMG = make_img("竖图.png", 600, 900)
LANDSCAPE_IMG = make_img("横图.png", 900, 600)

for _p, _label in ((PORTRAIT_IMG, "竖图600x900"), (LANDSCAPE_IMG, "横图900x600")):
    for _ori in ("auto", "landscape", "portrait"):
        _out = os.path.join(_SANDBOX, f"conv_{_label}_{_ori}.pdf")
        _converter._convert_image_to_pdf(_p, _out, _ori)
        _pdf_ori = pdf_printer.get_pdf_info(_out).get("orientation", "")
        _info = pdf_printer.get_image_info(_p, _ori)
        check(f"① {_label} / 面板={_ori}：列表口径与 PDF 一致",
              _info.get("orientation") == _pdf_ori and _info.get("page_count") == 1,
              f"info={_info.get('orientation')} pdf={_pdf_ori}")

check("① 显式方向覆盖原图长宽（横图选「竖向」⇒ 竖）",
      pdf_printer.get_image_info(LANDSCAPE_IMG, "portrait").get("orientation") == "portrait")
check("① 默认参数仍是 auto（老调用方不传也不变）",
      pdf_printer.get_image_info(PORTRAIT_IMG).get("orientation") == "portrait")

# ══════════════════════════════════════════════════════════════════
# ②⑥ 静态：改参数 → 记账 → 延后一拍重转
# ══════════════════════════════════════════════════════════════════
section("② 静态：_auto_apply_edit 记账 + 排队重转")

_AUTO = _method_body(_src, "def _auto_apply_edit(")
_SCHED = _method_body(_src, "def _schedule_render_reconvert(")
_FLUSH = _method_body(_src, "def _flush_render_reconvert(")
_RENDER = _method_body(_src, "def _restart_convert_for_render_change(")
_FILL = _method_body(_src, "def _fill_table_row(")
_XLSX_DEFAULT = _method_body(_src, "def _on_xlsx_follow_default(")
_DEF_TASK = _method_body(_src, "def _on_default_task_options(")


def _render_ok(src: str) -> bool:
    auto = _method_body(src, "def _auto_apply_edit(")
    i_save = auto.find("self._set_current_jobs(jobs)")
    i_sched = auto.find("self._schedule_render_reconvert(render_changed)")
    return (auto.count("render_changed.append(row)") >= 2
            and i_sched > 0 and 0 <= i_save < i_sched)


check("★ ② 图片方向与表格版式都记进 render_changed（至少 2 处）",
      _AUTO.count("render_changed.append(row)") >= 2,
      f"count={_AUTO.count('render_changed.append(row)')}")
check("★ ② 循环结束后排队重转，且在 _set_current_jobs 之后",
      _render_ok(_src))
check("② 排队走 QTimer.singleShot(0, …)（本轮写回都落盘后再起线程）",
      "QTimer.singleShot(0, self._flush_render_reconvert)" in _SCHED)
check("② 多次改动合并（有 _render_scheduled 防重入 + 行号进 set）",
      "_render_scheduled" in _SCHED and "bucket.add(int(r))" in _SCHED)
check("★ ② 重转走既有入口 _start_convert_worker，带 tab_key，且不直接调 converter.convert",
      "self._start_convert_worker(" in _RENDER
      and "tab_key=tab_key" in _RENDER
      and "converter.convert(" not in _RENDER)
check("② 图片不把页数归零（方向改了也是 1 页，免得列表闪「?」）",
      "not in self.IMAGE_EXTS" in _RENDER or "not in self.IMAGE_EXTS:" in _RENDER)
_RLOG = [ln for ln in _RENDER.splitlines() if "渲染参数已改" in ln]
check("② 日志文案不含 markdown 星号", bool(_RLOG) and "**" not in _RLOG[0], f"{_RLOG[:1]}")
check("★ ② 方向列优先用 job.image_orientation（列头文字与打印同口径）",
      'getattr(job, "image_orientation", "auto")' in _FILL
      and 'ori_map = {"portrait": "竖", "landscape": "横", "mixed": "混"}' in _FILL)
check("★ ⑤ 表格「跟随默认」也排队重转", "self._schedule_render_reconvert(" in _XLSX_DEFAULT)
check("★ ⑥ 默认任务参数改了版面才排队重转（并有 _layout_before 快照）",
      "_layout_before" in _DEF_TASK and "self._schedule_render_reconvert(" in _DEF_TASK)
check("⑥ 只作废「跟着全局默认走」的表格任务（自己设过版式的不动）",
      'dict(getattr(_job, "xlsx_options", {}) or {})' in _DEF_TASK)
check("⑥ 冻结标签页不动", 'getattr(_tab, "frozen", False)' in _DEF_TASK)

# ══════════════════════════════════════════════════════════════════
# 真 MainWindow
# ══════════════════════════════════════════════════════════════════
section("③ 真 MainWindow：图片改方向 → 列表当场变、随后重转")

_CACHE = paths.pdf_cache_dir()
os.makedirs(_CACHE, exist_ok=True)


def make_pdf(path: str, pages: int = 1) -> str:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas
    c = canvas.Canvas(path, pagesize=A4)
    for i in range(pages):
        c.drawString(72, 760, f"page {i + 1}")
        c.showPage()
    c.save()
    return path


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


def select_row(row: int):
    win._table.clearSelection()
    win._table.selectionModel().select(
        win._table.model().index(row, 0),
        QItemSelectionModel.Select | QItemSelectionModel.Rows)
    _app.processEvents()
    win._on_table_selection()
    _app.processEvents()


def flush_timers(times: int = 3):
    for _ in range(times):
        _app.processEvents()


OLD_PDF = make_pdf(os.path.join(_CACHE, "hn_render_param_old.pdf"))
job = PrintJob(file_path=LANDSCAPE_IMG, copies=1, duplex="off", page_count=1,
               orientation="landscape", image_orientation="auto",
               cached_pdf=OLD_PDF, display_name="横图.png")
seed([job])
select_row(0)

check("③ 初始：列表方向 = 横", win._table.item(0, win.COL_ORIENT).text() == "横")
check("③ 初始：面板「自动方向」", win._edit_img_orientation.currentIndex() == 0)

stub_start()
# 走真实用户路径：下拉框的 currentIndexChanged 自己就会调 _auto_apply_edit（不用手动调）
win._edit_img_orientation.setCurrentIndex(2)      # 竖向

check("★ ③ 面板选「竖向」→ job.image_orientation 立刻写回",
      getattr(job, "image_orientation", "") == "portrait", str(job.image_orientation))
check("★ ③ 旧方向 PDF 引用作废", job.cached_pdf == "", repr(job.cached_pdf))
check("★ ③ 列表方向列**当场**变「竖」（不必等重转）",
      win._table.item(0, win.COL_ORIENT).text() == "竖",
      f"cell={win._table.item(0, win.COL_ORIENT).text()!r}")
check("③ 图片页数不被归零（仍 1 页）", int(getattr(job, "page_count", 0) or 0) == 1,
      str(job.page_count))
check("③ 还没到事件循环下一拍时不重转（延后一拍，避免回调里抢同一行）",
      _calls == [], f"calls={_calls}")

flush_timers()
check("★ ③ 事件循环下一拍自动重转一次", len(_calls) == 1, f"calls={_calls}")
if _calls:
    check("★ ③ 带正确行号与标签页", _calls[0]["row"] == 0 and _calls[0].get("tab_key") == "1",
          f"{_calls[0]}")
    check("③ 用文件真实路径", _calls[0]["path"] == LANDSCAPE_IMG)
check("③ 重转后页数仍是 1（图片不闪「?」）",
      int(getattr(job, "page_count", 0) or 0) == 1, str(job.page_count))

# ④ 负例：只改份数
section("④ 负例：只改份数不重转")

job2 = PrintJob(file_path=LANDSCAPE_IMG, copies=1, duplex="off", page_count=1,
                orientation="landscape", image_orientation="portrait",
                cached_pdf=OLD_PDF, display_name="横图2.png")
seed([job2])
select_row(0)
stub_start()
win._edit_copies._btn_plus.click()
win._edit_copies._btn_plus.click()
_app.processEvents()
flush_timers()
check("★ ④ 只点份数「+」→ 一次重转都不发", _calls == [], f"calls={_calls}")
check("④ 方向与缓存原样保留",
      job2.image_orientation == "portrait" and job2.cached_pdf == OLD_PDF)

# ⑤ 表格版式「跟随默认」
section("⑤ 真 MainWindow：xlsx「跟随默认」→ 重转")

XLSX = os.path.join(_SANDBOX, "版式.xlsx")
with io.open(XLSX, "wb") as f:
    f.write(b"fake xlsx for render param test\n")
job3 = PrintJob(file_path=XLSX, copies=1, duplex="on", page_count=2,
                orientation="portrait", engine="libreoffice",
                xlsx_options={"scale": 80}, cached_pdf=OLD_PDF,
                display_name="版式.xlsx")
seed([job3])
select_row(0)
stub_start()
win._on_xlsx_follow_default()
_app.processEvents()
flush_timers()
check("★ ⑤ 「跟随默认」清掉自己的版式", dict(job3.xlsx_options or {}) == {}, str(job3.xlsx_options))
check("★ ⑤ 旧版式 PDF 作废", job3.cached_pdf == "", repr(job3.cached_pdf))
check("★ ⑤ 并发起重转一次", len(_calls) == 1, f"calls={_calls}")
if _calls:
    check("⑤ 带当前标签页", _calls[0].get("tab_key") == "1", f"{_calls[0]}")
check("⑤ 非图片任务重转期间页数归零（打印闸门等新页数）",
      int(getattr(job3, "page_count", 0) or 0) == 0, str(job3.page_count))

unstub_start()

# ⑦ 变异自证
section("⑦ 变异自证")

_MUT = _src.replace(
    "        if render_changed:\n            self._schedule_render_reconvert(render_changed)", "")
check("★ ⑦ 抽掉排队那两行 ⇒ 判据变红", (not _render_ok(_MUT)) and _MUT != _src)
_MUT2 = _src.replace('getattr(job, "image_orientation", "auto")', '"auto"')
_FILL2 = _method_body(_MUT2, "def _fill_table_row(")
check("★ ⑦ 方向列不再看 image_orientation ⇒ 判据变红",
      'getattr(job, "image_orientation", "auto")' not in _FILL2 and _MUT2 != _src)

print("\n" + ("=" * 60))
print("渲染参数改动后 UI/PDF 一致性验收: " + ("全部通过 ✅" if ok_all else "存在失败 ❌"))
sys.exit(0 if ok_all else 1)
