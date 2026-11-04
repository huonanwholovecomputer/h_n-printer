# -*- coding: utf-8 -*-
"""验收脚本 · 系统托盘图标 + 右键菜单 + 「关窗口常驻托盘」（4.6.0 后台常驻）

背景（2026-11 用户要求）：云打印要能后台常驻 —— 操作员关掉主窗口后，程序仍得收
云端任务、跑外部打印监视、按预约出纸。旧版本「关窗口 = 退出进程」把这条链路一起关了。

本脚本逐条把关（每条都能红）：
  ① 静态接线：gui.py 建托盘（_setup_tray，图标 = HN_printer.png，安装包里就有）、
     关窗口分支早退（hide + ignore，**不碰**收尾逻辑）、真退出只有 _quit_app 一条路，
     文件菜单「退出」/ 更新安装前自动退出/ 重复启动置前 三处都接对了。
  ② 菜单语义（桩窗口，不碰真监视线程）：五项文案与顺序、本地监视 checkable、
     勾选态**只认文件菜单那一条 QAction**（双向一致且不发射回环信号）、
     五个动作分别落到主窗口已有入口（不复制业务逻辑）。
  ③ 真 MainWindow（offscreen）：托盘可用 → close() 只藏不关（_closing 仍 False、
     进程没退）；_quit_app() 才真退（_force_quit 置位 + 图标收起 + quit 被调用）。
     托盘**不可用** → 退回旧行为「关窗即退出」，绝不把程序藏进不存在的托盘。

    python tests/verify_tray_icon.py     # exit 0 = 全绿
"""
import io
import os
import re
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LOCAL_TOOL = os.path.join(REPO, "local_print_tool")

os.environ["APPDATA"] = tempfile.mkdtemp(prefix="hn_tray_")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, LOCAL_TOOL)
os.chdir(LOCAL_TOOL)

from PySide6.QtCore import QObject  # noqa: E402
from PySide6.QtGui import QAction, QIcon  # noqa: E402
from PySide6.QtWidgets import QApplication, QSystemTrayIcon, QWidget  # noqa: E402

app = QApplication.instance() or QApplication([])

import paths  # noqa: E402
from theme_manager import ThemeManager  # noqa: E402
import gui as _gui  # noqa: E402
import tray_icon  # noqa: E402
from printer_config import PrintJob  # noqa: E402

_gui.QMessageBox.question = staticmethod(lambda *a, **k: _gui.QMessageBox.Yes)
_gui.QMessageBox.warning = staticmethod(lambda *a, **k: _gui.QMessageBox.Ok)
_gui.QMessageBox.information = staticmethod(lambda *a, **k: _gui.QMessageBox.Ok)

ok_all = True

# 用户 2026-11 指定的五项 + 顺序（托盘右键菜单）
_MENU_WANT = ["显示应用", "云端…", "本地监视", "收支清算", "退出"]


def check(name, cond, detail=""):
    global ok_all
    if not cond:
        ok_all = False
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


def ctrl_watch_state_matches(window, ctrl):
    """托盘的「本地监视」勾选态是否与文件菜单那条 QAction 一致（同一份真相）。"""
    action = getattr(window, "_ext_print_action", None)
    return (ctrl is not None and action is not None
            and bool(ctrl._act_watch.isChecked()) == bool(action.isChecked()))


_gui_src = open(os.path.join(LOCAL_TOOL, "gui.py"), encoding="utf-8").read()
_tray_src = open(os.path.join(LOCAL_TOOL, "tray_icon.py"), encoding="utf-8").read()
_spec_src = open(os.path.join(LOCAL_TOOL, "HN打印工具.spec"), encoding="utf-8").read()
_ver_src = open(os.path.join(LOCAL_TOOL, "version_info.txt"), encoding="utf-8").read()

# ════════════════════════════════════════════════════════════
print("=== ① 静态：托盘接线 / 关窗口分支 / 真退出入口 ===")

check("gui.py 里有 _setup_tray（独立方法，不塞进 _setup_ui 里搅成一团）",
      "def _setup_tray(self):" in _gui_src)
check("__init__ 在 _setup_ui 之后调用 _setup_tray（右键菜单要镜像文件菜单的 QAction）",
      re.search(r"self\._setup_ui\(\)\s*\n(?:\s*#[^\n]*\n)*\s*self\._setup_tray\(\)", _gui_src)
      is not None)
check("托盘图标用程序 LOGO：HN_printer.png",
      'os.path.join(os.path.dirname(os.path.abspath(__file__)), "HN_printer.png")' in _gui_src)
check("安装包真的带这张 png（否则打包后托盘图标是空白）",
      "('HN_printer.png', '.')" in _spec_src,
      "HN打印工具.spec datas")
check("托盘模块只做界面：动作全部回调主窗口已有入口，不自己写业务",
      'getattr(self._window, "_on_cloud_settings", None)' in _tray_src
      and 'getattr(self._window, "_on_open_finance", None)' in _tray_src
      and 'getattr(self._window, "_quit_app", None)' in _tray_src)

# 子弹窗：主窗口在托盘里时不许弹主窗口，也**不许**把对话框挂在不可见的父窗口上
_tray_cloud_body = (_tray_src.split("def _on_cloud_settings(self) -> None:", 1)[1]
                    .split("def _on_watch_toggled", 1)[0]
                    if "def _on_cloud_settings(self) -> None:" in _tray_src else "")
check("托盘「云端…」不再 show_window（改个 token 不必把整个主窗口弹起来）",
      bool(_tray_cloud_body.strip()) and "show_window()" not in _tray_cloud_body)
check("主窗口不可见/最小化 ⇒ 对话框做成独立窗口（parent=None）",
      "standalone = (not self.isVisible()) or self.isMinimized()" in _gui_src
      and "dlg = QDialog(None if standalone else self)" in _gui_src,
      "父=隐藏窗口的模态框没有任务栏按钮，抢不到前台就是看不见的死锁")
check("独立对话框补回了窗口图标（任务栏/Alt+Tab 里还认得出是本程序）",
      "dlg.setWindowIcon(self.windowIcon())" in _gui_src)
check("主窗口正常显示时仍用 QDialog(self)（模态把主窗口一起锁住，关掉就回到主窗口）",
      "dlg = QDialog(None if standalone else self)" in _gui_src)
check("_on_cloud_settings 的签名没变（verify_offline_sync_target.py 按它切函数体）",
      "def _on_cloud_settings(self):" in _gui_src)

# closeEvent：托盘可用时**先**早退（hide + ignore），再谈收尾
_ce_start = _gui_src.index("    def closeEvent(self, event):\n        \"\"\"关闭窗口时检查未完成任务，确认后保存并退出。\"\"\"")
_ce_end = _gui_src.index("super().closeEvent(event)", _ce_start)
_ce = _gui_src[_ce_start:_ce_end]
_i_guard = _ce.find("if not self._force_quit and getattr(self, \"_tray\", None) is not None:")
_i_closing = _ce.find("self._closing = True")
check("关窗口分支出现在 closeEvent 最前面（在 _closing = True 之前）",
      0 <= _i_guard < _i_closing, f"guard={_i_guard} closing={_i_closing}")
check("分支里 event.ignore() + 收进托盘，不往下走收尾逻辑",
      "event.ignore()" in _ce[_i_guard:_i_closing]
      and "self._minimize_to_tray()" in _ce[_i_guard:_i_closing]
      and "return" in _ce[_i_guard:_i_closing])
check("三处「用户改主意」的 event.ignore() 都复位了 _force_quit（否则 _quit_app 误判已退出）",
      _ce.count("self._force_quit = False") == 3,
      f"count={_ce.count('self._force_quit = False')}")

check("文件菜单「退出」走 _quit_app（不再裸 self.close()）",
      "exit_action.triggered.connect(self._quit_app)" in _gui_src)
check("更新安装前自动退出走 _quit_app（裸 close() 会收进托盘 ⇒ update.cmd 等不到进程退出）",
      "self._quit_app()  # update.cmd 轮询到进程退出后执行静默安装并启动新版本" in _gui_src
      and "self.close()  # update.cmd" not in _gui_src)
check("重复启动置前也走 _restore_from_tray（hide 进托盘的窗口只 showNormal() 叫不回来）",
      re.search(r"def _on_bring_to_front_request\(self\):(?:.|\n)*?self\._restore_from_tray\(\)",
                _gui_src) is not None)

check("自动重启（_restart_app，直接 quit 不走 closeEvent）也收了托盘图标，不留通知区残影",
      re.search(r"def _restart_app\(self\):(?:.|\n)*?self\._tray\.stop\(\)(?:.|\n)*?QApplication\.quit\(\)",
                _gui_src) is not None)

check("_quit_app 先置 _force_quit 再 close()，close 后仍置位才收图标 + quit",
      "self._force_quit = True" in _gui_src
      and "if self._force_quit:" in _gui_src
      and "_tray.stop()" in _gui_src and "QApplication.quit()" in _gui_src)

# 版本基线（用户 2026-11：从 4.6.x 起）—— 不写死具体版本号：
# 每次发版都要同步改 gui.py 的 APP_VERSION 和 version_info.txt，硬编码在这里只会让脚本
# 在发版当天"无辜变红"（2026-11-02 发 4.6.1 时就是这么红的）。这里只钉"两处必须同步"。
_ver_m = re.search(r'^APP_VERSION\s*=\s*"([0-9]+(?:\.[0-9]+)*)"', _gui_src, re.M)
_APP_VER = _ver_m.group(1) if _ver_m else ""
check(f"gui.py 里有 APP_VERSION（现为 {_APP_VER or '未找到'}）", bool(_APP_VER))
_ver_parts = [int(x) for x in _APP_VER.split(".")] if _APP_VER else []
_ver_tuple = tuple((_ver_parts + [0, 0, 0, 0])[:4])
_ver_tuple_src = "(" + ", ".join(str(x) for x in _ver_tuple) + ")"
check(f"version_info.txt 的 filevers/prodvers 与 APP_VERSION 同步 {_ver_tuple_src}（打包版文件属性靠它）",
      f"filevers={_ver_tuple_src}" in _ver_src and f"prodvers={_ver_tuple_src}" in _ver_src)
check(f"version_info.txt 的 FileVersion/ProductVersion 字符串也是 {_APP_VER}",
      f"u'FileVersion', u'{_APP_VER}'" in _ver_src and f"u'ProductVersion', u'{_APP_VER}'" in _ver_src)

# UI 硬规矩：文案里不许出现 markdown 星号（只看真正显示给用户的字，注释/docstring 不管）
_ui_texts = [tray_icon.TRAY_TOOLTIP, tray_icon.BALLOON_TITLE, tray_icon.BALLOON_BODY,
             tray_icon.WATCH_ON_BODY, tray_icon.WATCH_OFF_BODY] + _MENU_WANT
_ui_stars = [t for t in _ui_texts if "*" in t]
check("托盘文案没有 markdown 星号（UI 硬规矩）", _ui_stars == [], f"{_ui_stars}")

# ════════════════════════════════════════════════════════════
print("\n=== ② 菜单语义（桩窗口：不碰真监视/打印线程）===")


class _StubWindow(QWidget):
    """只记录回调，验证托盘没有自己另写一套业务。"""

    def __init__(self):
        super().__init__()
        self.calls = []
        self._ext_print_action = QAction("外部打印监视(&W)", self)
        self._ext_print_action.setCheckable(True)
        self._ext_print_action.setChecked(False)

    def _restore_from_tray(self):
        self.calls.append("restore")

    def _on_cloud_settings(self):
        self.calls.append("cloud")

    def _on_open_finance(self):
        self.calls.append("finance")

    def _quit_app(self):
        self.calls.append("quit")


_stub = _StubWindow()
ctrl = tray_icon.TrayController(_stub, QIcon(), _APP_VER)
actions = ctrl._menu.actions()
_texts = [a.text() for a in actions if not a.isSeparator()]
check(f"右键菜单五项且顺序为 {_MENU_WANT}", _texts == _MENU_WANT, f"{_texts}")
check("「退出」前有一条分隔线（与上面四项快捷功能分开）",
      len(actions) == 6 and actions[4].isSeparator(), f"{[a.isSeparator() for a in actions]}")
check(f"悬停提示带版本号（v{_APP_VER}）", ("v" + _APP_VER) in ctrl._tray.toolTip(),
      ctrl._tray.toolTip())
check("本地监视是可勾选项（√ 由勾选态给，不写死在文案里）",
      ctrl._act_watch.isCheckable() and not ctrl._act_watch.isChecked())

# 托盘勾选 → 写回文件菜单那一条 QAction（唯一真相）
ctrl._act_watch.setChecked(True)
check("托盘勾上 → 文件菜单的监视 QAction 也勾上（同一份真相）",
      _stub._ext_print_action.isChecked())
ctrl._act_watch.setChecked(False)
check("托盘取消 → 文件菜单的监视 QAction 也取消", not _stub._ext_print_action.isChecked())

# 文件菜单勾选 → 单向同步到托盘，且**不**发射 toggled（否则会再触发一次 start/stop）
_loops = []
ctrl._act_watch.toggled.connect(lambda v: _loops.append(v))
_stub._ext_print_action.setChecked(True)
ctrl.sync_watch_state()
check("文件菜单勾上 → 托盘同步勾上", ctrl._act_watch.isChecked())
check("同步过程不发 toggled（没有信号回环）", _loops == [], f"{_loops}")

# 五个动作的落点
_stub.calls.clear()
ctrl._act_show.trigger()
check("显示应用 → _restore_from_tray", _stub.calls == ["restore"], f"{_stub.calls}")

_stub.calls.clear()
ctrl._act_cloud.trigger()
check("云端… → 直接开设置，**不**把主窗口整块弹起来（弹窗自己解决父窗口问题）",
      _stub.calls == ["cloud"], f"{_stub.calls}")

_stub.calls.clear()
ctrl._act_finance.trigger()
check("收支清算 → _on_open_finance", _stub.calls == ["finance"], f"{_stub.calls}")

_stub.calls.clear()
ctrl._act_quit.trigger()
check("退出 → _quit_app", _stub.calls == ["quit"], f"{_stub.calls}")

# 单击/双击图标都要能叫回窗口（Windows 习惯双击，两种都接）
_stub.calls.clear()
ctrl._on_activated(QSystemTrayIcon.ActivationReason.DoubleClick)
check("双击托盘图标 → 显示应用", _stub.calls == ["restore"], f"{_stub.calls}")

# 气泡提示：首次收进托盘才有；没有真托盘时不得崩
_records = []
_orig_show_msg = QSystemTrayIcon.showMessage
QSystemTrayIcon.showMessage = lambda self, title, body, *a, **k: _records.append((title, body))
try:
    ctrl.notify_hidden()
    check("未 start() 时不发气泡（_started 是闸门）", _records == [], f"{_records}")
    ctrl._started = True
    ctrl.notify_hidden()
    ctrl.notify_hidden()
    check("首次收进托盘弹一次「仍在后台运行」，不重复骚扰",
          len(_records) == 1 and "后台" in _records[0][0], f"{_records}")
finally:
    QSystemTrayIcon.showMessage = _orig_show_msg

# ════════════════════════════════════════════════════════════
print("\n=== ③ 真 MainWindow（offscreen）：关窗口只藏、_quit_app 才退 ===")

_tm = ThemeManager(paths.theme_settings_path())
_tm.init(app, open(os.path.join(LOCAL_TOOL, "styles_dark.qss"), encoding="utf-8").read(),
         open(os.path.join(LOCAL_TOOL, "styles_light.qss"), encoding="utf-8").read())

_real_available = QSystemTrayIcon.isSystemTrayAvailable
check("offscreen 环境里系统托盘不可用（本脚本的判据前提）",
      QSystemTrayIcon.isSystemTrayAvailable() is False)

# ③-a 托盘不可用 → 关窗口必须真关（旧行为保住，绝不变成"进程还在但找不到"）
_win_a = _gui.MainWindow(config_path=paths.config_path(), theme_manager=_tm)
_win_a.show()
app.processEvents()
check("[不可用] _tray 为 None（不会硬塞一个假托盘）", getattr(_win_a, "_tray", "missing") is None)
_win_a.close()
app.processEvents()
check("[不可用] close() 真的关掉窗口（退回旧行为）",
      (not _win_a.isVisible()) and _win_a._closing is True)

# ③-b 托盘可用 → close() 只藏
_quits = []
_orig_quit = _gui.QApplication.quit
_gui.QApplication.quit = staticmethod(lambda: _quits.append(1))
_hidden = []
_orig_show = QSystemTrayIcon.show
_orig_hide = QSystemTrayIcon.hide
QSystemTrayIcon.isSystemTrayAvailable = staticmethod(lambda: True)
QSystemTrayIcon.show = lambda self: _hidden.append("show")
QSystemTrayIcon.hide = lambda self: _hidden.append("hide")
try:
    _win_b = _gui.MainWindow(config_path=paths.config_path(), theme_manager=_tm)
    _win_b.show()
    app.processEvents()
    _ctrl = getattr(_win_b, "_tray", None)
    check("[可用] 建出了 TrayController 且 start() 成功（图标已 show）",
          _ctrl is not None and _ctrl.is_started() and _hidden.count("show") == 1,
          f"ctrl={_ctrl} show={_hidden.count('show')}")
    check("[可用] 建立时就把文件菜单的监视勾选态同步过来了",
          ctrl_watch_state_matches(_win_b, _ctrl),
          f"tray={_ctrl._act_watch.isChecked() if _ctrl else None}")

    _win_b.close()
    app.processEvents()
    check("[可用] close() 只藏不关：窗口隐藏、进程没退",
          (not _win_b.isVisible()) and _win_b._closing is False and _win_b._force_quit is False,
          f"visible={_win_b.isVisible()} closing={_win_b._closing} force={_win_b._force_quit}")
    check("[可用] 藏起来时没调用 QApplication.quit", _quits == [], f"{_quits}")
    check("[可用] 首次收进托盘给了一次气泡提示", _ctrl._shown_balloon is True)

    _win_b._restore_from_tray()
    app.processEvents()
    check("[可用] 显示应用 / 双击图标能把窗口叫回来", _win_b.isVisible())

    _win_b._quit_app()
    app.processEvents()
    check("[可用] _quit_app 真的退出：_force_quit 置位 + 托盘图标收起 + quit 被调用",
          (not _win_b.isVisible()) and _quits == [1] and "hide" in _hidden
          and not _ctrl.is_started(),
          f"visible={_win_b.isVisible()} quits={_quits} hide={'hide' in _hidden}")

    # ③-c 2026-11-01 现场：从托盘退出时"突然置于前台 + 画面全白 + 退出"
    #   根因：旧 _quit_app **无条件**先 _restore_from_tray() 再 close()，而这次退出根本
    #   没有要确认的东西（订单全打完/标签页全空）⇒ show() 闪一帧还没重绘的白窗口就退进程。
    #   现改成"真要弹确认框之前才叫回前台"（_ensure_visible_for_close_dialog）。
    _win_c = _gui.MainWindow(config_path=paths.config_path(), theme_manager=_tm)
    _win_c.show()
    app.processEvents()
    _win_c.close()                      # 收进托盘：窗口隐藏，进程活着
    app.processEvents()
    check("[闪白] 前置：窗口确实藏在托盘里（隐藏、没退出）",
          (not _win_c.isVisible()) and _win_c._force_quit is False)

    _restores_c, _quits_c = [], []
    _real_restore_c = _win_c._restore_from_tray

    def _spy_restore_c():
        _restores_c.append(1)
        _real_restore_c()

    _win_c._restore_from_tray = _spy_restore_c
    _gui.QApplication.quit = staticmethod(lambda: _quits_c.append(1))
    _win_c._quit_app()
    app.processEvents()
    check("[闪白] ★ 没有要确认的东西 ⇒ 退出时一帧都不亮（不再无谓 show()）",
          _restores_c == [] and _quits_c == [1] and not _win_c.isVisible(),
          f"restore={_restores_c} quits={_quits_c} visible={_win_c.isVisible()}")

    # 反证：真有未完成的任务要确认时**必须**先把窗口叫回前台 —— 确认框是主窗口的子窗口，
    # 弹在隐藏窗口后面等于没弹（旧注释里那条理由依然成立，不能被上面的修复弄丢）。
    _win_d = _gui.MainWindow(config_path=paths.config_path(), theme_manager=_tm)
    _win_d.show()
    app.processEvents()
    _win_d._config.tabs[_win_d._current_tab].jobs.append(
        PrintJob(file_path=os.path.join(LOCAL_TOOL, "main.py"), sent=False))
    _restores_d, _quits_d, _asked_d = [], [], []
    _real_restore_d = _win_d._restore_from_tray

    def _spy_restore_d():
        _restores_d.append(1)
        _real_restore_d()

    _win_d._restore_from_tray = _spy_restore_d
    _orig_question_c = _gui.QMessageBox.question

    def _decline(*a, **k):
        _asked_d.append(1)
        return _gui.QMessageBox.No

    _win_d.hide()
    app.processEvents()
    _gui.QMessageBox.question = staticmethod(_decline)
    try:
        _win_d._quit_app()
        app.processEvents()
        check("[闪白] ★ 真要弹确认框 ⇒ 先把窗口叫回前台（对话框不会被藏住）",
              _asked_d == [1] and _restores_d == [1] and _win_d.isVisible(),
              f"asked={_asked_d} restore={_restores_d} visible={_win_d.isVisible()}")
        check("[闪白] 用户改主意「不退了」⇒ 进程没退、_force_quit 已复位",
              _quits_d == [] and _win_d._force_quit is False and _win_d._closing is False,
              f"quits={_quits_d} force={_win_d._force_quit} closing={_win_d._closing}")
    finally:
        _gui.QMessageBox.question = _orig_question_c
    # 收尾：清空任务后正常退出，别把沙箱配置留在"有未完成任务"的状态
    _win_d._config.tabs[_win_d._current_tab].jobs = []
    _win_d._quit_app()
    app.processEvents()
finally:
    QSystemTrayIcon.isSystemTrayAvailable = _real_available
    QSystemTrayIcon.show = _orig_show
    QSystemTrayIcon.hide = _orig_hide
    _gui.QApplication.quit = _orig_quit

print("\n" + ("全绿 ✅" if ok_all else "有失败 ❌"))
sys.exit(0 if ok_all else 1)
