# -*- coding: utf-8 -*-
"""验收脚本 · 右侧「任务参数」面板里的「缩放」行（2026-11-18 用户需求，offscreen）

用户原话：「这个设置我的打印工具貌似并没有涉及到？做一下这个设置。」「我说的是缩放调节功能」
「不应该在右侧的任务参数中显示吗？」

需求：
1. 面板要**看得见**：任务参数面板里有一行「缩放：」，下拉三档（调整为可打印区域大小 /
   实际大小（100%）/ 自定义百分比）+ 百分比输入框（10–400）。
2. 面板要**能调**：改下拉/百分比 → 写 PrinterConfig 并落盘；重启工具后仍是新值。
3. 与「选项 → 打印缩放…」是同一份配置：菜单对话框保存后面板这一行跟着变。
4. 它是**全局设置**（不是逐文件参数，也不写回 job）：合并打印一批只有一个缩放值。
5. **可用性跟着选区走**（2026-11-18 用户第二次反馈：「没有选中文件的时候应该是灰色不可用；
   以及需要选中 PDF、Word 类型才可用，其余是灰色不可用状态才对」）—— 没选中 / 选中图片 /
   选中表格 / 混选 ⇒ 整行灰掉，提示行写明"选中 PDF / Word 文件后可调"。
6. 文字与数字的比例：下拉占大头的宽度、百分比框固定窄框，否则「适合可打印区」显示不全。
7. 灰着的百分比框不回写（三档里只有「自定义百分比」才可用百分比）。

    python other/tests/verify_print_scale_panel.py     # exit 0 = 全绿
"""
import io
import json
import os
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LOCAL_TOOL = os.path.join(REPO, "local_print_tool")

_SANDBOX = tempfile.mkdtemp(prefix="hn_scalepanel_")
os.environ["APPDATA"] = _SANDBOX                 # 用户数据目录 → 沙箱（别碰真实配置）
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, LOCAL_TOOL)
os.chdir(LOCAL_TOOL)

from PySide6.QtWidgets import QApplication, QDialog  # noqa: E402

app = QApplication.instance() or QApplication([])

import converter  # noqa: E402
import gui as _gui  # noqa: E402
import pdf_printer as _pp  # noqa: E402

converter.get_available_engines = lambda: {"word": True, "wps": True, "libreoffice": True}
_gui.QMessageBox.question = staticmethod(lambda *a, **k: _gui.QMessageBox.Yes)
_gui.QMessageBox.warning = staticmethod(lambda *a, **k: _gui.QMessageBox.Ok)
_gui.QMessageBox.information = staticmethod(lambda *a, **k: _gui.QMessageBox.Ok)

from gui import MainWindow  # noqa: E402

SRC = io.open(os.path.join(LOCAL_TOOL, "gui.py"), encoding="utf-8").read()
CFG = os.path.join(_SANDBOX, "panel_config.json")
ok_all = True


def check(name, cond, extra=""):
    global ok_all
    if not cond:
        ok_all = False
        print(f"❌ {name}" + (f"  [{extra}]" if extra else ""))
    else:
        print(f"✅ {name}")


def build():
    w = MainWindow(config_path=CFG)
    w._cloud_client = None
    w._queue_font_checks = lambda *a, **k: None
    w.resize(1400, 900)
    w.show()
    app.processEvents()
    return w


print("── ① 面板结构（源码）──")
check("面板用短文案建下拉（SCALE_PANEL_LABELS）", "SCALE_PANEL_LABELS" in SRC)
check("下拉控件 _edit_scale_mode 存在", "self._edit_scale_mode = QComboBox()" in SRC)
check("百分比控件 _edit_scale_percent 存在", "self._edit_scale_percent = QSpinBox()" in SRC)
check("下拉变更接到 _on_scale_edit_changed",
      "self._edit_scale_mode.currentIndexChanged.connect(self._on_scale_edit_changed)" in SRC)
check("百分比变更接到 _on_scale_edit_changed",
      "self._edit_scale_percent.valueChanged.connect(self._on_scale_edit_changed)" in SRC)
check("百分比不做逐字 keyboardTracking", "setKeyboardTracking(False)" in SRC)
# 全局设置 ⇒ 不能混进「无选中任务就整片灰掉」的列表，否则又变成"看不见的功能"
_blk = SRC.find("self._edit_widgets = [")
_end = SRC.find("]", _blk)
_widgets_block = SRC[_blk:_end] if 0 <= _blk < _end else ""
check("缩放控件不在 _edit_widgets（始终可用）",
      "_edit_scale_mode" not in _widgets_block and "_edit_scale_percent" not in _widgets_block)
_menu_slot = SRC.find("def _on_print_scale_options")
_menu_next = SRC.find("\n    def ", _menu_slot + 10) if _menu_slot >= 0 else -1
_menu_body = SRC[_menu_slot:_menu_next] if _menu_slot >= 0 and _menu_next > _menu_slot else ""
check("菜单对话框保存后同步面板（_sync_scale_edit 在槽里）",
      "self._sync_scale_edit()" in _menu_body)
# 面板改缩放只写 config、不写 job（合并打印一批一个值）
_slot = SRC.find("def _on_scale_edit_changed")
_nxt_def = SRC.find("\n    def ", _slot + 10) if _slot >= 0 else -1
_slot_body = SRC[_slot:_nxt_def] if _slot >= 0 and _nxt_def > _slot else ""
check("面板改缩放不写回选中任务（不调 _auto_apply_edit / 不碰逐文件字段）",
      "_auto_apply_edit()" not in _slot_body and "_job_" not in _slot_body
      and "set_ranges" not in _slot_body)
# 可用性跟着选区走（2026-11-18 第二次反馈）：没选中/图片/表格 ⇒ 灰，PDF/Word ⇒ 可用
check("缩放可用性按文件类型判定（_job_scale_applies）",
      "def _job_scale_applies(self, job) -> bool:" in SRC
      and "_SCALE_EXTS = (" in SRC and '".pdf"' in SRC)
check("选区刷新时同步缩放可用性（_on_table_selection 里调用）",
      "self._set_scale_edit_enabled(\n            (not frozen) and bool(jobs)" in SRC)
check("没有选中任务时整行灰掉（_clear_edit_panel）",
      "self._set_scale_edit_enabled(False)\n\n    def _sync_edit_enabled" in SRC)
check("整片面板禁用时缩放行也灰（_sync_edit_enabled）",
      "self._set_scale_edit_enabled(False)" in SRC[SRC.find("def _sync_edit_enabled"):][:400])

print("── ② 运行态：默认 fit/100；没选中任务 ⇒ 整行灰 ──")
win = build()
check("下拉三档", win._edit_scale_mode.count() == len(_gui.SCALE_PANEL_LABELS),
      f"{win._edit_scale_mode.count()}")
check("默认停在「调整为可打印区域大小」",
      win._edit_scale_mode.currentText() == _gui.SCALE_PANEL_LABELS[0],
      win._edit_scale_mode.currentText())
check("默认百分比 100", win._edit_scale_percent.value() == _pp.SCALE_PERCENT_DEFAULT,
      f"{win._edit_scale_percent.value()}")
check("没选中任务时下拉灰着（用户第二次口径）", not win._edit_scale_mode.isEnabled())
check("没选中任务时百分比框也灰着", not win._edit_scale_percent.isEnabled())
check("没选中任务时提示行说清为什么不能点",
      "选中 PDF / Word 文件后可调" in win._label_scale_hint.text(),
      win._label_scale_hint.text())
check("fit 档百分比框灰着", not win._edit_scale_percent.isEnabled())
check("默认配置 fit/100", win._config.print_scale() == (_pp.SCALE_FIT, _pp.SCALE_PERCENT_DEFAULT),
      f"{win._config.print_scale()}")


def install_jobs(*specs):
    """把若干假任务装进当前标签页并选中第一行（specs = [(名字, 扩展名), ...]）。"""
    return _install(win, *specs)


def _install(target, *specs):
    jobs = [_gui.PrintJob(file_path=os.path.join(_SANDBOX, f"{name}{ext}"),
                          display_name=f"{name}{ext}", page_count=2)
            for name, ext in specs]
    target._set_current_jobs(jobs)
    target._rebuild_table()
    app.processEvents()
    if jobs:
        target._table.selectRow(0)
    target._on_table_selection()
    app.processEvents()
    return jobs


install_jobs(("doc1", ".pdf"))
check("选中 PDF ⇒ 下拉可用", win._edit_scale_mode.isEnabled())
check("选中 PDF ⇒ 提示行回到常规口径",
      "全局设置，下一批生效" in win._label_scale_hint.text(),
      win._label_scale_hint.text())
install_jobs(("doc2", ".docx"))
check("选中 Word(.docx) ⇒ 下拉可用", win._edit_scale_mode.isEnabled())
install_jobs(("pic", ".png"))
check("选中图片 ⇒ 下拉灰着", not win._edit_scale_mode.isEnabled())
check("选中图片 ⇒ 百分比框灰着", not win._edit_scale_percent.isEnabled())
install_jobs(("sheet", ".xlsx"))
check("选中表格 ⇒ 下拉灰着（表格有 Excel 自己的缩放）", not win._edit_scale_mode.isEnabled())
install_jobs(("doc3", ".pdf"), ("pic2", ".png"))
win._table.selectAll()
win._on_table_selection()
app.processEvents()
check("混选 PDF + 图片 ⇒ 灰着（一批只能一个口径）", not win._edit_scale_mode.isEnabled())
install_jobs(("doc4", ".pdf"))
_cur = win._config.tabs.get(win._current_tab)
_cur.frozen = True
win._on_table_selection()
app.processEvents()
check("锁定标签页 ⇒ 缩放行灰着", not win._edit_scale_mode.isEnabled())
_cur.frozen = False
win._on_table_selection()
app.processEvents()
check("解锁后又可用", win._edit_scale_mode.isEnabled())

print("── ②b 文字/数字的比例：下拉给大头、百分比固定窄框 ──")
_w_narrow = win._edit_scale_mode.width()
win._edit_group.setFixedWidth(340)      # 现场面板常被拖到 ~340px 宽
app.processEvents()
check("百分比框固定窄框 64px（不随面板伸缩、也不和文字抢宽度）",
      win._edit_scale_percent.width() == 64, f"{win._edit_scale_percent.width()}px")
check("下拉弹层 ≥170px（点开能看全整句，合上可以省略）",
      win._edit_scale_mode.view().minimumWidth() >= 170,
      f"{win._edit_scale_mode.view().minimumWidth()}px")
check("数字框装得下最长的 400%（文本宽 + 边框仍有余量）",
      win._edit_scale_percent.fontMetrics().horizontalAdvance("400%") <= win._edit_scale_percent.width() - 12,
      f"text={win._edit_scale_percent.fontMetrics().horizontalAdvance('400%')}px")
check("面板变宽后多出来的宽度全给下拉",
      win._edit_scale_mode.width() >= _w_narrow + 80,
      f"narrow={_w_narrow} wide={win._edit_scale_mode.width()}")
check("340px 面板下下拉 ≥200px（三档短文案完整显示）",
      win._edit_scale_mode.width() >= 200, f"{win._edit_scale_mode.width()}px")
check("下拉至少是百分比框的 2.5 倍宽（比例对了）",
      win._edit_scale_mode.width() >= win._edit_scale_percent.width() * 2.5,
      f"combo={win._edit_scale_mode.width()} spin={win._edit_scale_percent.width()}")
win._edit_group.setMinimumWidth(0)
win._edit_group.setMaximumWidth(16777215)
app.processEvents()

print("── ③ 运行态：改成自定义 150% → 落盘 ──")
win._edit_scale_mode.setCurrentIndex(_gui.SCALE_MODE_VALUES.index(_pp.SCALE_PERCENT))
app.processEvents()
check("切到自定义百分比后百分比框可用", win._edit_scale_percent.isEnabled())
win._edit_scale_percent.setValue(150)
app.processEvents()
check("config 跟随面板", win._config.print_scale() == (_pp.SCALE_PERCENT, 150),
      f"{win._config.print_scale()}")
_saved = json.load(io.open(CFG, encoding="utf-8"))
check("落盘 scale_mode=percent", _saved.get("scale_mode") == _pp.SCALE_PERCENT,
      f"{_saved.get('scale_mode')!r}")
check("落盘 scale_percent=150", _saved.get("scale_percent") == 150,
      f"{_saved.get('scale_percent')!r}")
win.close()

print("── ④ 运行态：重启后仍是 150%，且菜单对话框与面板同源 ──")
win2 = build()
check("重启后面板仍显示自定义档",
      win2._edit_scale_mode.currentText() == _gui.SCALE_PANEL_LABELS[2],
      win2._edit_scale_mode.currentText())
check("重启后百分比仍是 150", win2._edit_scale_percent.value() == 150,
      f"{win2._edit_scale_percent.value()}")


class _FakeDialog:
    """替身：模拟用户在「选项 → 打印缩放…」里选了「实际大小」并点保存。"""

    def __init__(self, *_a, **_k):
        pass

    def exec(self):
        return QDialog.Accepted

    def values(self):
        return (_pp.SCALE_ACTUAL, _pp.SCALE_PERCENT_DEFAULT)


_orig_dlg = _gui.PrintScaleDialog
_gui.PrintScaleDialog = _FakeDialog
try:
    win2._on_print_scale_options()
finally:
    _gui.PrintScaleDialog = _orig_dlg
app.processEvents()
check("菜单对话框保存后 config = actual",
      win2._config.print_scale() == (_pp.SCALE_ACTUAL, _pp.SCALE_PERCENT_DEFAULT),
      f"{win2._config.print_scale()}")
check("菜单对话框保存后面板那一行跟着变",
      win2._edit_scale_mode.currentText() == _gui.SCALE_PANEL_LABELS[1],
      win2._edit_scale_mode.currentText())
check("actual 档百分比框灰着（灰着的不回写）", not win2._edit_scale_percent.isEnabled())
check("菜单对话框给的是 100 ⇒ 面板同步成 100（配置是唯一真相）",
      win2._edit_scale_percent.value() == _pp.SCALE_PERCENT_DEFAULT,
      f"{win2._edit_scale_percent.value()}")

print("── ⑤ 面板内切档：自定义 150 → 实际大小，再切回来仍是 150 ──")
_install(win2, ("doc5", ".pdf"))     # 面板要选中 PDF 才可调（灰着的控件不回写配置）
win2._edit_scale_mode.setCurrentIndex(_gui.SCALE_MODE_VALUES.index(_pp.SCALE_PERCENT))
app.processEvents()
win2._edit_scale_percent.setValue(150)
app.processEvents()
win2._edit_scale_mode.setCurrentIndex(_gui.SCALE_MODE_VALUES.index(_pp.SCALE_ACTUAL))
app.processEvents()
_m, _p = win2._config.print_scale()
check("切到实际大小：档位生效但百分比被记住（150）", _m == _pp.SCALE_ACTUAL and _p == 150,
      f"{(_m, _p)}")
win2._edit_scale_mode.setCurrentIndex(_gui.SCALE_MODE_VALUES.index(_pp.SCALE_PERCENT))
app.processEvents()
check("切回自定义：百分比仍是 150", win2._edit_scale_percent.value() == 150,
      f"{win2._edit_scale_percent.value()}")
win2.close()

print()
print("全部通过" if ok_all else "有失败项")
sys.exit(0 if ok_all else 1)
