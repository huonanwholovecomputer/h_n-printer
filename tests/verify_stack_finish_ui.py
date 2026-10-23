# -*- coding: utf-8 -*-
"""验收 · 攒批收尾的两处 UI（2026-10-23 用户截图）

① 「关闭攒批列表 / 放弃整叠」那个三选确认框，底部三个按钮的**文字被裁掉**（现场：
   "留（纸叠与订单都留着）"、"弃整叠（1 单全部按失败收尾）"、"消（保持原样）"）——
   成因：Qt 的 QMessageBox 把整行按钮压进对话框宽度里、按比例把每个按钮压窄（实测
   sizeHint 190px 的文字被压成 114px，而它的 minimumSizeHint 仍是 190px）。
   修法：`_fit_message_box_buttons()` 把每个按钮的最小宽度设成自己的 sizeHint。

② 点「放弃整叠」之后，进度条停在 **50%**（攒批单的进度分母是「纸张数 × 2」，只打完第 1 趟
   就正好一半），看起来像还在打印。修法：`_finish_stack_progress()` 在整叠收尾（放弃 /
   第 2 趟失败）时把进度条收成 100%，状态栏写明结局。

判据（全部在真实 MainWindow + 真实 QSS 上量，不看源码字符串）：
  ① 三个按钮 width ≥ minimumSizeHint，且 ≥ 文字宽度 + 内边距（文字真的放得下）；
  ①b **变异自证**：把 `_fit_message_box_buttons` 换成空实现，同一次测量必须变红
     （否则上面那条是"假绿"）；
  ② 放弃整叠 → 进度条 maximum=value=100 且状态栏写「放弃」；第 2 趟失败同理写「失败」；
  ②b 放弃整叠仍然照旧调用 `_finish_stack_entries(entries, False, …)`（不会因为改进度条漏掉
     失败收尾）。

    python tests/verify_stack_finish_ui.py     # exit 0 = 全绿
"""
import io
import os
import sys
import tempfile
import types

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL_TOOL = os.path.join(REPO, "local_print_tool")

_SANDBOX = tempfile.mkdtemp(prefix="hn_stack_finish_ui_")
os.environ["APPDATA"] = _SANDBOX
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_FONTS = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")
if os.path.isdir(_FONTS):
    os.environ.setdefault("QT_QPA_FONTDIR", _FONTS)
sys.path.insert(0, LOCAL_TOOL)
os.chdir(LOCAL_TOOL)

from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

_app = QApplication.instance() or QApplication([])

import converter as _converter  # noqa: E402
_converter.get_available_engines = lambda: {"word": True, "wps": True, "libreoffice": True}

import gui  # noqa: E402
import paths  # noqa: E402
from theme_manager import ThemeManager, MODE_DARK  # noqa: E402
from PySide6.QtWidgets import QPushButton  # noqa: E402

gui.QMessageBox.question = staticmethod(lambda *a, **k: gui.QMessageBox.Yes)
gui.QMessageBox.warning = staticmethod(lambda *a, **k: gui.QMessageBox.Ok)
gui.QMessageBox.information = staticmethod(lambda *a, **k: gui.QMessageBox.Ok)

ok_all = True


def check(name, cond, detail=""):
    global ok_all
    if not cond:
        ok_all = False
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  — {detail}" if detail else ""))
    return bool(cond)


_theme = ThemeManager(paths.theme_settings_path())
_theme.init(_app,
            open("styles_dark.qss", encoding="utf-8").read(),
            open("styles_light.qss", encoding="utf-8").read())
_theme.set_mode(MODE_DARK)

print("… 正在创建主窗口", flush=True)
win = gui.MainWindow(config_path=paths.config_path(), theme_manager=_theme)
win.resize(1400, 900)
win.show()
_app.processEvents()
win._log = lambda m: None
win._cloud_client = None

# ── 通用：截获 QMessageBox.exec()（离屏不能真弹模态框），拿到对话框量按钮 ──
captured = {}
_orig_exec = QMessageBox.exec


def _fake_exec(self):
    captured["box"] = self
    self.adjustSize()
    self.show()
    _app.processEvents()
    _app.processEvents()
    return 0


def _stub_stack(count=1, present=3, original=3):
    win._stack = types.SimpleNamespace(entries=[object()] * count, count=count,
                                       clear=lambda: None, batch_id="")
    win._stack_present_sheets = lambda: present
    win._stack_original_sheets = lambda: original
    win._stack_second_pass_slots = lambda: 3


def _button_problems(box):
    """返回 [(button, width, need)]：宽度不足的按钮。need = minimumSizeHint（布局硬下限）。"""
    bad = []
    for b in box.buttons():
        need = max(int(b.minimumSizeHint().width()), int(b.sizeHint().width()))
        if int(b.width()) < need:
            bad.append((b.text(), int(b.width()), need))
    return bad


print("\n① 「关闭攒批列表」三选按钮：文字必须完整显示")
_stub_stack()
QMessageBox.exec = _fake_exec
try:
    choice = win._confirm_stack_close("关闭攒批列表")
finally:
    QMessageBox.exec = _orig_exec
box = captured.get("box")
bad = _button_problems(box)
check("三选框仍返回「取消」（桩 exec 没点任何按钮）", choice == "", repr(choice))
check("三个按钮都拿到了足够宽度（width ≥ 文字所需）", not bad, str(bad))
if box is not None:
    labels = [b.text() for b in box.buttons()]
    check("三个选项的文案都在（保留 / 放弃整叠 / 取消）",
          any("保留" in t for t in labels) and any("放弃整叠" in t for t in labels)
          and any("取消" in t for t in labels), str(labels))
    fm_ok = []
    for b in box.buttons():
        text_w = b.fontMetrics().horizontalAdvance(b.text())
        # QSS：padding 6px 16px + 1px border ⇒ 左右共约 34px
        fm_ok.append(int(b.width()) >= text_w + 34)
    check("按字体度量，文字 + 内边距真的装得下（不是只骗过 sizeHint）", all(fm_ok),
          f"{[(b.text(), b.width(), b.fontMetrics().horizontalAdvance(b.text())) for b in box.buttons()]}")
    box.close()

print("\n①b 变异自证：关掉 _fit_message_box_buttons，判据必须变红")
_orig_fit = getattr(gui, "_fit_message_box_buttons", None)
if _orig_fit is None:
    check("改前版本没有 _fit_message_box_buttons（① 的判据确实在测新行为）", False,
          "gui._fit_message_box_buttons 不存在")
else:
    gui._fit_message_box_buttons = lambda box: None
    captured.clear()
    QMessageBox.exec = _fake_exec
    try:
        win._confirm_stack_close("关闭攒批列表")
    finally:
        QMessageBox.exec = _orig_exec
        gui._fit_message_box_buttons = _orig_fit
    box2 = captured.get("box")
    bad2 = _button_problems(box2)
    check("没有撑宽时按钮确实被压扁（说明上面①的判据是真的）", bool(bad2),
          f"被压扁的按钮：{bad2}")
    if box2 is not None:
        box2.close()

print("\n② 放弃整叠：进度条收成 100% + 状态栏写结局")
_stub_stack(count=1, present=3, original=3)
win._progress_bar.setMaximum(6)      # 攒批单：3 张纸 × 2 趟 = 6 面
win._progress_bar.setValue(3)        # 第 1 趟打完 = 50%（现场截图的那一态）
win._status_label.setText("本单第 1 趟完成")
finished = {}
win._finish_stack_entries = lambda entries, ok, msg: finished.update(n=len(entries), ok=ok, msg=msg)
win._sync_stack_button = lambda: None
win._refresh_stack_queue = lambda: None
win._drain_stack_wait_queue = lambda: None
win._stack_batch_dir = lambda: ""
win._on_stack_abandon(interactive=False)
_app.processEvents()
check("放弃整叠后进度条 = 100%（满格）",
      win._progress_bar.maximum() == 100 and win._progress_bar.value() == 100,
      f"max={win._progress_bar.maximum()} value={win._progress_bar.value()}")
check("状态栏写明「已放弃整叠」", "放弃" in win._status_label.text(),
      win._status_label.text())
check("放弃整叠仍照旧按失败收尾（没漏掉 _finish_stack_entries）",
      finished.get("ok") is False and finished.get("n") == 1, str(finished))

print("\n③ 整叠第 2 趟失败：同样是终态，进度条也要收满格")
_stub_stack(count=2, present=5, original=6)
win._progress_bar.setMaximum(10)
win._progress_bar.setValue(5)
win._finish_stack_entries = lambda entries, ok, msg: finished.update(ok=ok)
win._on_stack_flush_done(False, "第 2 趟失败：打印机缺纸", 5)
_app.processEvents()
check("第 2 趟失败后进度条 = 100%",
      win._progress_bar.maximum() == 100 and win._progress_bar.value() == 100,
      f"max={win._progress_bar.maximum()} value={win._progress_bar.value()}")
check("状态栏写明「第 2 趟失败」", "失败" in win._status_label.text(),
      win._status_label.text())

print("\n" + "=" * 70)
print("全绿 ✅" if ok_all else "有 FAIL ❌（见上）")
sys.exit(0 if ok_all else 1)
