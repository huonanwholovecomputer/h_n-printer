# -*- coding: utf-8 -*-
"""验收脚本 · 退出时「还有未打印的文件」确认框（2026-11-04 用户需求，离屏 GUI）

需求（用户原话）：
1. 程序退出时，若有一个标签页存在未打印的文件，弹窗要**说是哪个标签页**未打印完成，
   并提供「到达指定标签页」的按钮 + 确定 + 取消；
2. 如果存在多个标签页未打印完成，则提示**有几个标签页**，提供「打开标签页管理器」的按钮
   + 确定 + 取消。

钉的东西（都会真的红）：
  ① `_unfinished_tabs_info()` 只列「还有未打印文件」的页，且未打印数 / 总数算对；
  ② 单页文案点名标签页与文件数，多页文案给出页数与页号列表；两者都不含 markdown 星号；
  ③ 单页框有「去到这个标签页/确定/取消」，多页框换成「打开标签页管理器」，四个按钮的
     点击真的落成分支（goto / manager / quit / cancel），关窗（Esc/✕）按取消；
  ④ 无头平台不弹模态（否则 `win.close()` 会把任何验收脚本卡死）；
  ⑤ 「去到这个标签页」真的切到那一页（`_goto_tab`），且页号已不存在时安全返回 False；
  ⑥ 隐藏 bug 回归：标签页右侧两个时间标签**在真窗口里宽度不为 0**（曾因 Ignored 尺寸
     策略被压成 width=0、text=''，用户从来没见过）。

    python other/tests/verify_quit_unfinished_tabs.py     # exit 0 = 全绿
"""
import io
import os
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LOCAL_TOOL = os.path.join(REPO, "local_print_tool")

_SANDBOX = tempfile.mkdtemp(prefix="hn_quitdlg_")
os.environ["APPDATA"] = _SANDBOX                 # 用户数据目录 → 沙箱（别碰真实配置）
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, LOCAL_TOOL)
os.chdir(LOCAL_TOOL)

import paths  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog, QDialogButtonBox  # noqa: E402

app = QApplication.instance() or QApplication([])

import gui as _gui  # noqa: E402
import converter as _converter  # noqa: E402

# 引擎探测会真的 Dispatch Word/WPS COM（离屏测试里既慢又有副作用）→ 一律桩成"都可用"
_converter.get_available_engines = lambda: {"word": True, "wps": True, "libreoffice": True}
# 退出路上的对话框（非本次验收对象）一律桩掉
_gui.QMessageBox.question = staticmethod(lambda *a, **k: _gui.QMessageBox.Yes)
_gui.QMessageBox.warning = staticmethod(lambda *a, **k: _gui.QMessageBox.Ok)
_gui.QMessageBox.information = staticmethod(lambda *a, **k: _gui.QMessageBox.Ok)
_gui.QMessageBox.critical = staticmethod(lambda *a, **k: _gui.QMessageBox.Ok)

from gui import MainWindow  # noqa: E402
from printer_config import PrintJob  # noqa: E402

ok_all = True


def check(name, cond, extra=""):
    global ok_all
    if not cond:
        ok_all = False
        print(f"❌ {name}" + (f"  [{extra}]" if extra else ""))
    else:
        print(f"✅ {name}")


def mkjob(name, sent=False, order_number=""):
    j = PrintJob(file_path=rf"C:\tmp\{name}.pdf", page_count=2)
    j.sent = sent
    j.order_number = order_number
    return j


win = MainWindow(config_path=paths.config_path())
win.show()
app.processEvents()

# ── ② 夹具：标签页 1 全打完 / 标签页 2 两个没打 / 标签页 3 一个没打 ──────────
win._config.tabs["1"].jobs = [mkjob("a", sent=True), mkjob("b", sent=True)]
win._config.tabs["2"] = win._make_new_tab()
win._config.tabs["2"].jobs = [mkjob("c"), mkjob("d"), mkjob("e", sent=True, order_number="HN2026-1")]
win._config.tabs["3"] = win._make_new_tab()
win._config.tabs["3"].jobs = [mkjob("f")]
win._current_tab = "1"
win._config.active_tab = "1"

print("\n=== ① 盘点：只列还有未打印文件的页，数对 ===")
info = win._unfinished_tabs_info()
keys = [it["key"] for it in info]
check("全部打完的标签页 1 不出现在提示里", "1" not in keys, str(keys))
check("两个未完成页都在（1、3 之外的 2 与 3）", keys == ["2", "3"], str(keys))
_by = {it["key"]: it for it in info}
check("标签页 2：未打印 2 个 / 共 3 个", (_by["2"]["files"], _by["2"]["total"]) == (2, 3), str(_by["2"]))
check("标签页 2 带上订单号（便于对账）", _by["2"]["order_number"] == "HN2026-1", _by["2"]["order_number"])
check("标签页 3：未打印 1 个 / 共 1 个", (_by["3"]["files"], _by["3"]["total"]) == (1, 1), str(_by["3"]))

print("\n=== ② 文案：单页点名，多页给页数与页号 ===")
one = win._unfinished_tabs_text([_by["3"]])
check("单页文案点名「标签页 3」", "标签页 3" in one, one)
check("单页文案给出未打印文件数「1 个文件」", "1 个文件" in one, one)
check("单页文案给出直达按钮的含义「去到这个标签页」", "去到这个标签页" in one, one)
check("单页文案给出确定/取消的含义",
      "确定 = 清空全部" in one and "退出" in one and "取消 = 不退出" in one, one)
check("单页文案说明云端订单会被标记为已放弃",
      "已放弃" in one, one)
many = win._unfinished_tabs_text(info)
check("多页文案给出页数「2 个标签页」", "2 个标签页" in many, many)
check("多页文案列出页号「标签页 2、3」", "标签页 2、3" in many, many)
check("多页文案给出总未打印文件数「共 3 个文件」", "共 3 个文件" in many, many)
check("多页文案给出管理器入口「打开标签页管理器」", "打开标签页管理器" in many, many)
check("两个文案里都没有 markdown 星号", "**" not in one and "**" not in many)

print("\n=== ③ 单页框：三个按钮，点击真的落成分支 ===")
dlg, buttons, decision = win._build_unfinished_tabs_dialog([_by["3"]])
check("单页框按钮齐全：goto / ok / cancel", set(buttons) == {"goto", "ok", "cancel"}, str(set(buttons)))
check("「去到这个标签页」文案正确", buttons["goto"].text() == "去到这个标签页", buttons["goto"].text())
check("「确定 / 取消」文案正确",
      buttons["ok"].text() == "确定" and buttons["cancel"].text() == "取消")
check("「去到这个标签页」是 ActionRole（不关窗的附加入口）",
      dlg.findChildren(QDialogButtonBox)[0].buttonRole(buttons["goto"]) == QDialogButtonBox.ActionRole)
check("默认决策（Esc / 右上角 ✕）= 取消，不会误退出", decision["value"] == "cancel")
buttons["goto"].click()
check("点「去到这个标签页」→ 决策 goto 且窗口 Accepted",
      decision["value"] == "goto" and dlg.result() == QDialog.Accepted,
      f'{decision["value"]} / {dlg.result()}')
dlg.deleteLater()

dlg2, buttons2, dec2 = win._build_unfinished_tabs_dialog([_by["3"]])
buttons2["cancel"].click()
check("点「取消」→ 决策 cancel 且窗口 Rejected",
      dec2["value"] == "cancel" and dlg2.result() == QDialog.Rejected,
      f'{dec2["value"]} / {dlg2.result()}')
dlg2.deleteLater()

dlg3, buttons3, dec3 = win._build_unfinished_tabs_dialog([_by["3"]])
buttons3["ok"].click()
check("点「确定」→ 决策 quit（= 清空并退出）", dec3["value"] == "quit", dec3["value"])
dlg3.deleteLater()

print("\n=== ④ 多页框：换成「打开标签页管理器」 ===")
dlg4, buttons4, dec4 = win._build_unfinished_tabs_dialog(info)
check("多页框按钮是 manager / ok / cancel", set(buttons4) == {"manager", "ok", "cancel"},
      str(set(buttons4)))
check("「打开标签页管理器」文案正确",
      buttons4["manager"].text() == "打开标签页管理器", buttons4["manager"].text())
buttons4["manager"].click()
check("点「打开标签页管理器」→ 决策 manager", dec4["value"] == "manager", dec4["value"])
dlg4.deleteLater()

print("\n=== ⑤ 无头平台不弹模态（否则任何验收脚本的 close() 都会卡死）===")
check("离屏平台被判定为「没有可点按钮」", win._interactive_ui_available() is False)
check("离屏环境下 _ask_unfinished_tabs 直接返回 quit（旧行为），不阻塞",
      win._ask_unfinished_tabs(info) == "quit")

print("\n=== ⑥ 去到这个标签页：真的切过去 ===")
check("从标签页 1 切到标签页 3", win._goto_tab("3") is True and win._current_tab == "3",
      win._current_tab)
check("页号不存在时安全返回 False（不抛、不乱切）", win._goto_tab("99") is False,
      win._current_tab)
check("切过去后 _config.active_tab 同步落盘",
      win._config.active_tab == "3", win._config.active_tab)

print("\n=== ⑦ 回归：标签页右侧两个时间标签在真窗口里真的看得见 ===")
win._current_tab = "2"
win._config.active_tab = "2"
win._set_current_jobs(win._config.tabs["2"].jobs)
# 2026-11 用户改口径：左边那格显示「首次添加」（第一次添加文件的时刻），
# 不再是「订单创建」（created_at 照旧记录/上报，只是不占用工具条）。
first_added = win._mark_tab_first_added("2")
win._mark_order_created("2")
started = win._mark_print_started("2")
win._refresh_tab_display()
win.resize(1400, 900)
app.processEvents()
for _ in range(3):
    app.processEvents()

for label_name, stamp in (("_tab_created_label", first_added), ("_tab_started_label", started)):
    lbl = getattr(win, label_name)
    check(f"{label_name} 宽度 > 0（不是被压成 0 的隐形标签）", lbl.width() > 0, str(lbl.width()))
    check(f"{label_name} 画出来的文字非空", bool(lbl.text().strip()), repr(lbl.text()))
    check(f"{label_name} 显示完整时间（{stamp}）", bool(stamp) and stamp in lbl.text(), lbl.text())
    check(f"{label_name} sizeHint 有真实宽度", lbl.sizeHint().width() > 0, str(lbl.sizeHint().width()))

check("左边那格的文案是「首次添加」（不再是「订单创建」）",
      win._tab_created_label.text().startswith("首次添加"),
      win._tab_created_label.text())

win.resize(900, 700)
app.processEvents()
for _ in range(3):
    app.processEvents()
check("窄窗口下仍显示时间（退化也不能变空）",
      win._tab_created_label.text().strip() != "" and win._tab_started_label.text().strip() != "",
      f"{win._tab_created_label.text()!r} / {win._tab_started_label.text()!r}")

win._force_quit = True          # 收尾：跳过退出确认，直接退（无头环境本来也不会弹）
win.close()
app.processEvents()

print("\n" + ("全绿 ✅" if ok_all else "有失败 ❌"))
sys.exit(0 if ok_all else 1)
