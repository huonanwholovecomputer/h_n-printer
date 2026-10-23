# -*- coding: utf-8 -*-
"""验收脚本 · 「双面模式：长边/短边翻转」的可用性（2026-10-23 用户现场截图反馈）

现象（用户截图）：编辑面板「单/双面」选着**单面打印**，下面「双面模式：长边翻转」却是亮的，
可以点开选长短边；同一块还挂着「分两趟打印 · 中途需翻纸」的橙色提示。单面打印只有一面纸，
翻转方式没有物理意义 ⇒ 这一格应该灰着，提示也不该还挂着。

修法：
  · `_sync_duplex_mode_availability()` 是唯一条算式：可用 = 选区能双面 **且** 面板真的选着
    「双面打印」（`_edit_duplex.currentIndex() == 0`）；
  · `_on_table_selection`（选中行变化）与 `_auto_apply_edit`（用户刚切了「单/双面」）都调它 ——
    少一处就会停在「单面打印 + 长边翻转亮着」的矛盾状态；
  · 灰着时**不写回** `job.duplex_mode`（与"选区含图片/单页文件"同一口径：灰着的控件不碰）；
  · 提示行（`_update_duplex_label`）跟着「单/双面」收起/恢复。

本脚本全在**真实 MainWindow + 真实控件状态**上断言（不看源码字符串），判据：
  ① 双面文件的单选：双面可用、双面模式可用、提示可见；
  ② 切到「单面打印」：双面模式下拉**与标签一起立刻灰掉**、提示**立刻收起**，job.duplex 变 off；
  ③ 切回「双面打印」：双双恢复可用、提示回来（可逆，不是一次性开关）；
  ④ 单面时面板灰着的双面模式**不被写回**：绕过禁用硬改索引、以及改份数，都不能动
     各文件自己的 duplex_mode；
  ⑤ 单页文件 / 图片 / 多选里含单页文件：双面与双面模式仍整体禁用（旧守卫零回归）；
  ⑥ 多选两个双面文件：双面模式可用；首行是单面时灰着，切回双面后两行一起改、模式写回。

    python tests/verify_duplex_mode_availability.py     # exit 0 = 全绿
    反向验证：把 local_print_tool/gui.py 换回改前版本（HEAD）再跑，②③④⑥ 必须红在断言上。
"""
import io
import os
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL_TOOL = os.path.join(REPO, "local_print_tool")

_SANDBOX = tempfile.mkdtemp(prefix="hn_duplex_mode_")
os.environ["APPDATA"] = _SANDBOX                 # 用户数据目录 → 沙箱（别碰真实配置）
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_FONTS = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")
if os.path.isdir(_FONTS):
    os.environ.setdefault("QT_QPA_FONTDIR", _FONTS)
sys.path.insert(0, LOCAL_TOOL)
os.chdir(LOCAL_TOOL)

import fitz  # noqa: E402  （PyMuPDF：造真 PDF，页数判定走真实文件）
from PySide6.QtCore import Qt, QItemSelectionModel  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

_app = QApplication.instance() or QApplication([])

import converter as _converter  # noqa: E402
# 引擎可用性探测会真的 Dispatch Word/WPS COM（离屏测试里既慢又会拉起残留进程）→ 桩掉
_converter.get_available_engines = lambda: {"word": True, "wps": True, "libreoffice": True}

import gui  # noqa: E402
import paths  # noqa: E402
from printer_config import PrintJob, PrinterProfile, TabSettings  # noqa: E402

gui.QMessageBox.question = staticmethod(lambda *a, **k: gui.QMessageBox.Yes)
gui.QMessageBox.warning = staticmethod(lambda *a, **k: gui.QMessageBox.Ok)
gui.QMessageBox.information = staticmethod(lambda *a, **k: gui.QMessageBox.Ok)

QUEUE = "HP LaserJet 1020"      # 无自动双面器：双面靠程序两趟 + 人工翻纸
ok_all = True


def check(name, cond, detail=""):
    global ok_all
    if not cond:
        ok_all = False
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  — {detail}" if detail else ""))
    return bool(cond)


def make_pdf(name: str, pages: int = 1) -> str:
    p = os.path.join(_SANDBOX, name)
    doc = fitz.open()
    for _ in range(max(1, pages)):
        doc.new_page()
    doc.save(p)
    doc.close()
    return p


PNG = os.path.join(_SANDBOX, "图片测试.png")
with open(PNG, "wb") as f:
    f.write(b"\x89PNG\r\n\x1a\n")

print("… 正在创建主窗口", flush=True)
win = gui.MainWindow(config_path=paths.config_path())
win.resize(1400, 900)
win.show()
_app.processEvents()

# 顶栏下拉与 config 对齐（_save_config 会从下拉读回 printer_name），再给这个队列盖「手动翻纸」章
combo = win._printer_combo
idx = next((i for i in range(combo.count()) if combo.itemData(i) == QUEUE), -1)
if idx < 0:
    combo.addItem(QUEUE, QUEUE)
    idx = combo.count() - 1
combo.setCurrentIndex(idx)
_orig_log = win._log
win._log = lambda m: _orig_log(m)
win._config.printer_name = QUEUE
win._config.printer_profiles[QUEUE] = PrinterProfile(
    auto_duplex=False, capability_source="manual", manual_duplex=True,
    first="odd", reverse_second=True, second_rotate_180=True,
    verified_at="2026-10-23 13:05:00", verified_by="测试")
win._update_duplex_label()
_app.processEvents()

PDF4 = make_pdf("四页.pdf", 4)
PDF3 = make_pdf("三页.pdf", 3)
PDF1 = make_pdf("单页.pdf", 1)
PDF2 = make_pdf("两页.pdf", 2)


def seed(jobs):
    tab = TabSettings()
    tab.jobs = jobs
    win._config.tabs = {"1": tab}
    win._current_tab = "1"
    win._config.active_tab = "1"
    win._rebuild_table()
    win._refresh_tab_display()
    _app.processEvents()


def select_rows(rows):
    win._table.clearSelection()
    sm = win._table.selectionModel()
    for r in rows:
        sm.select(win._table.model().index(r, 0),
                  QItemSelectionModel.Select | QItemSelectionModel.Rows)
    _app.processEvents()


def job_at(i):
    return win._config.tabs["1"].jobs[i]


def mode_state():
    return (win._edit_duplex_mode.isEnabled(), win._label_duplex_mode.isEnabled())


def hint_visible():
    return win._edit_duplex_hint.isVisible()


print("\n① 双面文件单选：双面与双面模式都可用、两趟提示在场")
seed([PrintJob(file_path=PDF4, copies=1, duplex="on", duplex_mode="long-edge",
               page_count=4, orientation="portrait", engine="word")])
select_rows([0])
check("双面文件：双面下拉可用", win._edit_duplex.isEnabled())
check("双面文件：双面模式下拉与标签都可用", mode_state() == (True, True), str(mode_state()))
check("双面文件：面板停在「双面打印」", win._edit_duplex.currentIndex() == 0)
check("双面文件：两趟提示可见（本机无自动双面器）", hint_visible(),
      win._edit_duplex_hint.text())

print("\n② 切到「单面打印」：双面模式必须立刻灰掉、提示收起")
win._edit_duplex.setCurrentIndex(1)          # 等价用户在下拉里点「单面打印」
_app.processEvents()
check("单面：双面模式下拉**禁用**", not win._edit_duplex_mode.isEnabled(),
      f"enabled={win._edit_duplex_mode.isEnabled()}")
check("单面：双面模式标签也灰着", not win._label_duplex_mode.isEnabled())
check("单面：两趟提示**收起**", not hint_visible(), win._edit_duplex_hint.text())
check("单面：job.duplex 真的变成 off", job_at(0).duplex == "off", job_at(0).duplex)
check("单面：表格双面列显示「单面」",
      win._table.item(0, gui.MainWindow.COL_DUPLEX).text() == "单面",
      win._table.item(0, gui.MainWindow.COL_DUPLEX).text())
check("单面：灰着的双面模式**不被重置**（仍显示文件自己的长边，只是不可点）",
      win._edit_duplex_mode.currentIndex() == 0 and job_at(0).duplex_mode == "long-edge",
      f"combo={win._edit_duplex_mode.currentIndex()} job={job_at(0).duplex_mode}")

print("\n③ 切回「双面打印」：两者都要回来（可逆）")
win._edit_duplex.setCurrentIndex(0)
_app.processEvents()
check("切回双面：双面模式恢复可用", mode_state() == (True, True), str(mode_state()))
check("切回双面：两趟提示回来", hint_visible(), win._edit_duplex_hint.text())
check("切回双面：job.duplex 变回 on", job_at(0).duplex == "on", job_at(0).duplex)

print("\n④ 单面时灰着的双面模式不写回（改份数 / 硬改索引都拦）")
seed([PrintJob(file_path=PDF3, copies=1, duplex="off", duplex_mode="short-edge",
               page_count=3, orientation="portrait", engine="word")])
select_rows([0])
check("前置：面板是单面 ⇒ 双面模式灰着且显示短边",
      (not win._edit_duplex_mode.isEnabled()) and win._edit_duplex_mode.currentIndex() == 1,
      str(mode_state()))
win._edit_copies._increase()                  # 改份数（会触发 _auto_apply_edit 的写回）
_app.processEvents()
check("改份数：duplex_mode 原样保留（灰着不写回）", job_at(0).duplex_mode == "short-edge",
      job_at(0).duplex_mode)
check("改份数本身照常生效", job_at(0).copies == 2, str(job_at(0).copies))
win._edit_duplex_mode.setCurrentIndex(0)      # 绕过禁用硬改索引（模拟任何异常触发）
_app.processEvents()
check("硬改灰着的双面模式：仍不写回 job.duplex_mode", job_at(0).duplex_mode == "short-edge",
      job_at(0).duplex_mode)

print("\n⑤ 旧守卫零回归：单页文件 / 图片 / 多选含单页 → 双面与双面模式都禁用")
seed([PrintJob(file_path=PDF1, copies=1, duplex="on", duplex_mode="long-edge",
               page_count=1, orientation="portrait", engine="word")])
select_rows([0])
check("单页文件：双面禁用", not win._edit_duplex.isEnabled())
check("单页文件：双面模式禁用", mode_state() == (False, False), str(mode_state()))
check("单页文件：两趟提示也收起", not hint_visible(), win._edit_duplex_hint.text())
seed([PrintJob(file_path=PNG, copies=1, duplex="on", duplex_mode="long-edge",
               page_count=0, orientation="portrait", engine="word")])
select_rows([0])
check("图片：双面与双面模式都禁用",
      (not win._edit_duplex.isEnabled()) and mode_state() == (False, False), str(mode_state()))

print("\n⑥ 多选：两个双面文件都可用；首行是单面时灰着，切回双面后整片一起改")
seed([PrintJob(file_path=PDF4, copies=1, duplex="on", duplex_mode="long-edge",
               page_count=4, orientation="portrait", engine="word"),
      PrintJob(file_path=PDF3, copies=1, duplex="on", duplex_mode="short-edge",
               page_count=3, orientation="portrait", engine="word")])
select_rows([0, 1])
check("多选两个双面文件：双面与双面模式都可用",
      win._edit_duplex.isEnabled() and mode_state() == (True, True), str(mode_state()))
win._edit_duplex_mode.setCurrentIndex(1)      # 面板改成「短边」→ 两行都写成 short-edge
_app.processEvents()
check("双面模式下改动写回整片选区（首行 long→short）",
      job_at(0).duplex_mode == "short-edge" and job_at(1).duplex_mode == "short-edge",
      f"{job_at(0).duplex_mode} / {job_at(1).duplex_mode}")
win._edit_duplex_mode.setCurrentIndex(0)      # 再改回「长边」
_app.processEvents()
check("双面模式下再改回长边，整片同步",
      job_at(0).duplex_mode == "long-edge" and job_at(1).duplex_mode == "long-edge",
      f"{job_at(0).duplex_mode} / {job_at(1).duplex_mode}")

seed([PrintJob(file_path=PDF4, copies=1, duplex="off", duplex_mode="short-edge",
               page_count=4, orientation="portrait", engine="word"),
      PrintJob(file_path=PDF3, copies=1, duplex="on", duplex_mode="long-edge",
               page_count=3, orientation="portrait", engine="word")])
select_rows([0, 1])
check("多选且首行是单面：面板显示单面 ⇒ 双面模式灰着",
      win._edit_duplex.currentIndex() == 1 and not win._edit_duplex_mode.isEnabled(),
      f"duplex_index={win._edit_duplex.currentIndex()} {mode_state()}")
win._edit_duplex.setCurrentIndex(0)           # 整片切双面
_app.processEvents()
check("整片切双面后：双面模式立刻可用",
      win._edit_duplex_mode.isEnabled() and win._label_duplex_mode.isEnabled(),
      str(mode_state()))
check("整片切双面后：两行 job.duplex 都变成 on",
      job_at(0).duplex == "on" and job_at(1).duplex == "on",
      f"{job_at(0).duplex} / {job_at(1).duplex}")

print("\n" + "=" * 70)
print("全绿 ✅" if ok_all else "有 FAIL ❌（见上）")
sys.exit(0 if ok_all else 1)
