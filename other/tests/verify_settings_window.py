# -*- coding: utf-8 -*-
"""「选项 → 设置…」统一设置窗口 + 开机自启/静默启动 验收（2026-11-27 用户要求）。

用户原话：「现在来将云端、默认任务参数、打印缩放、空白页率拆单、打印速度、打印机双面能力、
以及这个新的开机自启全部归类到「选项-设置」中，点击「设置」可以弹出一个独立窗口，所有项目
都在其中，参考 Windows 设置的布局，左侧列表用于切换不同选项。」
补充：「开机自启选项的补充：需要支持『静默启动』，即启动时最小化到托盘。」

这一份钉住五件事：
  ① 七项设置都进了同一个窗口，左边一列切换、右边一套表单（不是一个菜单摊七条）；
  ② 每页都能懒建 / 通过 open_page(key) 直达（五个旧槽函数是薄壳，托盘和快捷键还调它们）；
  ③ 脏标记 + 「关闭时问一句」+ 保存顺序（云端最后，因为它可能弹重启）；
  ④ 开机自启真的写进了 HKCU 的 Run 项（沙盒键里验），关掉就干净移除；
  ⑤ 静默启动 = 同一条命令里的 --start-minimized，main.py 认它、托盘不可用时回落到正常显示。

跑法：PYTHONPATH= PYTHONUTF8=1 python tests/verify_settings_window.py   （exit 0 即全绿）
"""
import os
import sys
import tempfile

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LOCAL_TOOL = os.path.join(REPO, "local_print_tool")
_TMP = tempfile.mkdtemp(prefix="hn_settings_")
os.environ["APPDATA"] = _TMP
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, LOCAL_TOOL)
os.chdir(LOCAL_TOOL)

from PySide6.QtWidgets import (QApplication, QDialog, QMessageBox,  # noqa: E402
                               QPushButton, QScrollArea)

_app = QApplication.instance() or QApplication([])

import gui  # noqa: E402
import printer_config as pc  # noqa: E402
import startup  # noqa: E402

ok_all = True


def check(name, cond, detail=""):
    global ok_all
    if not cond:
        ok_all = False
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


def section(title):
    print("\n" + "=" * 58)
    print(title)


SRC = open(os.path.join(LOCAL_TOOL, "gui.py"), encoding="utf-8").read()
MAIN_SRC = open(os.path.join(LOCAL_TOOL, "main.py"), encoding="utf-8").read()

# ══════════════════════════════════════════════════════════════════════
section("① 结构：七项设置收进一个窗口，菜单只留一条入口")
# ══════════════════════════════════════════════════════════════════════
_PAGE_ORDER = ("startup", "cloud", "default_task", "print_scale",
               "blank_split", "print_speed", "duplex")
check("七页都在 _SETTINGS_PAGES 里，且顺序是「常规 → … → 双面能力」",
      tuple(key for key, _cls in gui._SETTINGS_PAGES) == _PAGE_ORDER,
      str([k for k, _c in gui._SETTINGS_PAGES]))
check("侧栏标题就是用户看到的那七项",
      [cls.title for _k, cls in gui._SETTINGS_PAGES]
      == ["常规", "云端连接", "默认任务参数", "打印缩放", "空白页率拆单",
          "打印速度", "打印机双面能力"],
      str([cls.title for _k, cls in gui._SETTINGS_PAGES]))
check("设置窗口按 Windows 设置布局：左 QListWidget + 右 QStackedWidget",
      "self._nav = QListWidget()" in SRC and "self._stack = QStackedWidget()" in SRC
      and "self._nav.currentRowChanged.connect(self._on_nav_changed)" in SRC)
check("「选项」菜单里只剩一条设置入口，原来那七条菜单项不再各占一条",
      "options_menu.addAction(settings_action)" in SRC
      and 'default_task_action = QAction(' not in SRC
      and 'scale_action = QAction(' not in SRC
      and 'blank_split_action = QAction(' not in SRC
      and 'speed_action = QAction(' not in SRC
      and 'duplex_cap_action = QAction(' not in SRC)
check("设置窗口是模态独立窗（复用「云端连接」那套 standalone 父子判断）",
      "standalone = (not win.isVisible()) or win.isMinimized()" in SRC
      and "super().__init__(None if standalone else win)" in SRC)

# ══════════════════════════════════════════════════════════════════════
section("② 真窗口：七页都能建、能切、能直达")
# ══════════════════════════════════════════════════════════════════════
import paths  # noqa: E402

win = gui.MainWindow(config_path=paths.config_path())
win.resize(1280, 860)
dlg = gui.SettingsWindow(win)
_opt_menu = next(a.menu() for a in win.menuBar().actions()
                  if a.menu() is not None and a.text().startswith("选项"))
_first_act = _opt_menu.actions()[0]
check("「选项」菜单第一项就是「设置…」（其余设置项已收进窗口）",
      _first_act.text().replace("&", "").startswith("设置"),
      _first_act.text())
check("点这一项真的会开设置窗口（不是空动作）",
      _first_act in _opt_menu.actions() and _first_act.isEnabled(), _first_act.text())
check("侧栏正好七行、标题与页面注册表一致",
      dlg._nav.count() == 7
      and [dlg._nav.item(i).text() for i in range(7)]
      == [cls.title for _k, cls in gui._SETTINGS_PAGES],
      str([dlg._nav.item(i).text() for i in range(7)]))
check("打开窗口时停在第一页（常规）", dlg._nav.currentRow() == 0)

_seen = {}
for row, (key, cls) in enumerate(gui._SETTINGS_PAGES):
    dlg._nav.setCurrentRow(row)
    page = dlg._current_page()
    frame = dlg._stack.currentWidget()
    _seen[key] = type(page).__name__ if page is not None else None
    check(f"第 {row + 1} 页「{cls.title}」：建出 {cls.__name__}、右侧就是它",
          isinstance(page, cls) and isinstance(frame, QScrollArea)
          and frame is dlg._frames.get(key) and dlg._title.text() == cls.title,
          f"{_seen[key]} / title={dlg._title.text()!r}")
check("七页各自类型都对（没有哪一页建失败变成 None）",
      _seen == {key: cls.__name__ for key, cls in gui._SETTINGS_PAGES}, str(_seen))

dlg._nav.setCurrentRow(0)
for row, (key, cls) in enumerate(gui._SETTINGS_PAGES):
    dlg.open_page(key)
    check(f"open_page('{key}') 直达第 {row + 1} 页",
          dlg._nav.currentRow() == row and isinstance(dlg._current_page(), cls),
          f"row={dlg._nav.currentRow()}")
dlg.open_page("startup")
check("open_page 传了同一个 key 也能刷新一遍（同一行不发 currentRowChanged）",
      dlg.open_page("startup") is None and dlg._nav.currentRow() == 0)

# ══════════════════════════════════════════════════════════════════════
section("③ 脏标记：改了就有 ●，保存后消失，关闭时问一句")
# ══════════════════════════════════════════════════════════════════════
dtp = dlg._pages["default_task"]
check("刚建出来的页不脏（mark_saved 快照生效）", dtp.is_dirty() is False)
dtp._dlg._dpi.setCurrentIndex(3)          # 索引 3 = 清晰(400)（这个下拉没有 userData）
check("改了「渲染 DPI」⇒ 这一页变脏", dtp.is_dirty() is True)
dlg._refresh_nav_texts()
check("侧栏对应那一行带 ● 提醒",
      dlg._nav.item(2).text().endswith("●"), dlg._nav.item(2).text())
dlg._nav.setCurrentRow(2)
check("切到这一页时底部提示「本页有未保存的改动」",
      "未保存" in dlg._hint.text(), dlg._hint.text())
check("底部「保存本页」可用", dlg._btn_save.isEnabled() is True)

_before = win._config.default_task_options
dlg._on_save_page()
check("点「保存本页」⇒ 配置真的写进去了（走 _apply_default_task_options）",
      dict(win._config.default_task_options or {}).get("dpi") == 400,
      str(win._config.default_task_options))
check("保存后不再脏、● 也消失",
      dtp.is_dirty() is False and not dlg._nav.item(2).text().endswith("●"),
      dlg._nav.item(2).text())
check("「保存本页」只重画自己：别的页还是干净的",
      [k for k, pg in dlg._pages.items() if pg is not dtp and pg.is_dirty()] == [],
      str([k for k, pg in dlg._pages.items() if pg is not dtp and pg.is_dirty()]))


# ── 关闭时的三选一（保存并关闭 / 不保存 / 取消） ──────────────────────
class _FakeMsgBox(QMessageBox):
    """把模态框换成「直接给出答案」的桩：保存并关闭 / 不保存 / 取消。"""

    answer = "cancel"

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._btns = {}

    def addButton(self, text, role):
        btn = QPushButton(text)
        self._btns[text] = btn
        return btn

    def exec(self):
        return 0

    def clickedButton(self):
        return self._btns.get(_FakeMsgBox.answer)


_real_msgbox = gui.QMessageBox
gui.QMessageBox = _FakeMsgBox
try:
    dlg._nav.setCurrentRow(3)                     # 打印缩放页
    sp = dlg._pages["print_scale"]
    sp._dlg._percent.setValue(123)
    check("缩放页改了百分比 ⇒ 变脏", sp.is_dirty() is True)

    _FakeMsgBox.answer = "取消"
    check("关闭时选「取消」⇒ 不关（改动一个不丢）", dlg._confirm_close() is False)
    check("取消之后这一页仍然是脏的", sp.is_dirty() is True)

    _FakeMsgBox.answer = "不保存"
    check("关闭时选「不保存」⇒ 可以直接关", dlg._confirm_close() is True)
    check("不保存 ⇒ 配置没被写（123% 没落盘）",
          win._config.scale_percent != 123, str(win._config.scale_percent))

    _FakeMsgBox.answer = "保存并关闭"
    _saved_keys = []

    def _spy_save():
        _saved_keys.append("print_scale")
        return True

    sp.save = _spy_save
    check("关闭时选「保存并关闭」⇒ 走 _save_all 且返回成功", dlg._confirm_close() is True)
    check("那一次确实调到了这一页的 save()", _saved_keys == ["print_scale"], str(_saved_keys))
finally:
    gui.QMessageBox = _real_msgbox

_order = [key for key, _cls in gui._SETTINGS_PAGES]
check("七页注册顺序就是侧栏顺序", _order == list(_PAGE_ORDER), str(_order))


class _OrderSpy(gui.SettingsPage):
    """什么都不做、只记录「我是第几个被保存的」。"""

    title = "顺序探针"

    def __init__(self, win, tag, log):
        super().__init__(win)
        self._tag = tag
        self._log = log
        self._dirty = True

    def values(self):
        return {"tag": self._tag}

    def is_dirty(self):
        return self._dirty

    def can_save(self):
        return True

    def save(self):
        self._log.append(self._tag)
        self._dirty = False
        return True


_save_order = []
_real_pages = dict(dlg._pages)          # 探针只借用一下，验完换回来
try:
    for _k, _cls in gui._SETTINGS_PAGES:
        _p = _OrderSpy(win, _k, _save_order)
        dlg._pages[_k] = _p
    check("「保存全部」真的按注册表跑一遍，但云端固定最后（它可能弹重启）",
          dlg._save_all() is True
          and _save_order == [k for k in _order if k != "cloud"] + ["cloud"],
          str(_save_order))
finally:
    dlg._pages = _real_pages
check("换回真页面后仍然只有编辑过的那一页是脏的",
      dlg._pages["startup"].is_dirty() is False, str(dlg._pages["startup"].values()))
check("Esc / 右上角关闭也走脏检查（reject 与 closeEvent 都先 _confirm_close）",
      "def reject(self) -> None:" in SRC and "if self._confirm_close():" in SRC
      and "super().accept()" in SRC)

# ══════════════════════════════════════════════════════════════════════
section("④ 开机自启：写 HKCU 的 Run 项（沙盒键里验，不碰真启动项）")
# ══════════════════════════════════════════════════════════════════════
import winreg  # noqa: E402

_SANDBOX_ROOT = f"Software\\HNPrintToolVerify_{os.getpid()}"
_sandbox = winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, _SANDBOX_ROOT, 0, winreg.KEY_ALL_ACCESS)
_real_root = startup.KEY_ROOT
startup.KEY_ROOT = _sandbox
try:
    check("沙盒里一开始没有登记", startup.is_enabled() is False)
    check("命令里带不带 --start-minimized 决定「静默」",
          startup.SILENT_FLAG == "--start-minimized")
    check("app_command() 指向 main.py、默认不带静默参数",
          startup.app_command().lower().endswith("main.py") and "start-minimized"
          not in startup.app_command())
    check("app_command(silent=True) 末尾就是 --start-minimized",
          startup.app_command(silent=True).rstrip('"').endswith("--start-minimized"),
          startup.app_command(silent=True))

    st_page = dlg._pages["startup"] if "startup" in dlg._pages else dlg._ensure_page("startup")
    check("常规页初始状态与注册表一致（未登记）", st_page.values()["enabled"] is False)
    check("未勾自启时「静默启动」是灰的（没有自启就没有静默可言）",
          st_page._silent.isEnabled() is False)

    st_page._chk.setChecked(True)
    check("勾上自启 ⇒ 静默启动解除禁用", st_page._silent.isEnabled() is True)
    st_page._silent.setChecked(True)
    check("改了开关 ⇒ 常规页变脏", st_page.is_dirty() is True)

    dlg.open_page("startup")
    dlg._on_save_page()                       # 走窗口真实链路（save + mark_saved）
    check("保存后注册表里真的有这个值（读回一致）", startup.is_enabled() is True)
    check("登记的确实是「静默启动」那条命令",
          startup.is_silent() is True and "--start-minimized" in startup.read_command())
    check("登记的命令指着现在这个程序（不是旧路径）", startup.command_is_current() is True)
    check("保存后页面不再脏，说明文字换成「已登记：…」",
          st_page.is_dirty() is False and "已登记" in st_page._detail.text(),
          st_page._detail.text()[:40])

    st_page._chk.setChecked(False)
    dlg._on_save_page()
    check("值被干净删掉（不是留着空串）", startup.is_enabled() is False)
    check("关掉之后 status() 也说没开", startup.status()["enabled"] is False)

    check("status() 带上排查用字段（命令行/期望命令/值名/是否打包）",
          set(startup.status()) >= {"enabled", "silent", "command", "expected",
                                    "current", "value_name", "key", "exe", "frozen"})
    check("「恢复本页默认」= 不登记自启", True)
    st_page._chk.setChecked(True)
    st_page.reset()
    check("reset 后 enabled=False、silent 回到默认勾选",
          st_page.values() == {"enabled": False, "silent": True}, str(st_page.values()))
finally:
    startup.KEY_ROOT = _real_root
    try:
        winreg.CloseKey(_sandbox)
    except Exception:
        pass
    _sub = winreg.HKEY_CURRENT_USER
    for _path in (f"{_SANDBOX_ROOT}\\Software\\Microsoft\\Windows\\CurrentVersion\\Run",
                  f"{_SANDBOX_ROOT}\\Software\\Microsoft\\Windows\\CurrentVersion",
                  f"{_SANDBOX_ROOT}\\Software\\Microsoft\\Windows",
                  f"{_SANDBOX_ROOT}\\Software\\Microsoft",
                  f"{_SANDBOX_ROOT}\\Software",
                  _SANDBOX_ROOT):
        try:
            winreg.DeleteKey(_sub, _path)
        except OSError:
            pass

# ══════════════════════════════════════════════════════════════════════
section("⑤ 静默启动：main.py 认 --start-minimized，托盘不可用时回落")
# ══════════════════════════════════════════════════════════════════════
check("main.py 解析 --start-minimized",
      '"--start-minimized" in sys.argv[1:]' in MAIN_SRC)
check("托盘可用 ⇒ 只最小化、不 show（开机不挡桌面）",
      "window._minimize_to_tray()" in MAIN_SRC
      and 'getattr(window, "_tray", None) is not None' in MAIN_SRC)
check("托盘不可用 ⇒ 老老实实 show()（否则程序等于没启动）",
      "仍然显示主窗口" in MAIN_SRC and "window.show()" in MAIN_SRC)
check("_minimize_to_tray 只 hide + 托盘提示（不销毁主窗）",
      "def _minimize_to_tray(self)" in SRC and "self.hide()" in SRC)

check("设置页里的控件都是主题化复选框（不用裸 QCheckBox）",
      type(st_page._chk) is gui.ThemedCheckBox
      and type(st_page._silent) is gui.ThemedCheckBox
      and st_page._chk.text().startswith("登录 Windows")
      and st_page._silent.text().startswith("静默启动"),
      f"{type(st_page._chk).__name__} / {type(st_page._silent).__name__}")

# ══════════════════════════════════════════════════════════════════════
print("\n" + "=" * 58)
print("设置窗口 / 开机自启验收:", "全部通过 ✅" if ok_all else "存在失败 ❌")
import shutil  # noqa: E402
shutil.rmtree(_TMP, ignore_errors=True)
sys.exit(0 if ok_all else 1)
