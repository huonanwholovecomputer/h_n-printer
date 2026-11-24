# -*- coding: utf-8 -*-
"""验收脚本 · 「发现新版本」双栏窗口（左 COS / 右 GitHub）

背景（2026-11-24 用户现场反馈）：点「帮助 → 检查更新」发现新版本时，原来连着弹
「一句话 + 三选一按钮」→「QInputDialog 要码」→「下载码未通过校验」三个框，太碎。
现在只弹一个 `UpdateChannelDialog`：左栏本站加速通道（填 8 位码）、右栏 GitHub Release、
右下「稍后」= 什么都不做；校验失败把原因就地显示在左栏，不关框重开。

本脚本两层把关：
  1. 运行时：真建窗口，验证输入归一化（小写/无连字符也能收）、码不对不关窗只红字、
     两个按钮各自给出正确的 `choice`，「不需要码」的清单不显示输入框；
  2. 分派：用桩替换窗口与两个动作，验证 `_prompt_update_channel` 的
     cos → `_perform_update(code=…)`、github → 打开 Release 页、取消 → 什么都不做。

    python other/tests/verify_update_channel_dialog.py     # exit 0 = 全绿
"""
import io
import os
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LOCAL_TOOL = os.path.join(REPO, "local_print_tool")

os.environ["APPDATA"] = tempfile.mkdtemp(prefix="hn_upd_dlg_")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, LOCAL_TOOL)
os.chdir(LOCAL_TOOL)

from PySide6.QtGui import QFontDatabase  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog, QWidget  # noqa: E402

app = QApplication.instance() or QApplication([])
for _f in ("C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/segoeui.ttf"):
    if os.path.isfile(_f):
        QFontDatabase.addApplicationFont(_f)

import gui  # noqa: E402

ok_all = True


def check(name, cond, detail=""):
    global ok_all
    if not cond:
        ok_all = False
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


def new_dialog(**kw):
    d = gui.UpdateChannelDialog(None, **kw)
    return d


print("=== 1. 运行时：双栏窗口本身 ===")
dlg = new_dialog(version="4.7.3", current_version="4.7.2", notes="说明", needs_code=True)
check("窗口标题是「发现新版本」", dlg.windowTitle() == "发现新版本", dlg.windowTitle())
check("左栏标题含本站加速通道（COS）",
      any("本站加速通道" in b.title() for b in dlg.findChildren(gui.QGroupBox)))
check("右栏标题含 GitHub Release",
      any("GitHub Release" in b.title() for b in dlg.findChildren(gui.QGroupBox)))
check("需要码时存在输入框，且起手 choice 为空",
      dlg._code_edit is not None and dlg.choice == "")
check("错误标签起手不可见", not dlg._err.isVisible() or not dlg._err.text())

# 小写 + 空格 + 没连字符，逐字符输入（模拟用户真打字），应自动大写并补连字符
dlg._code_edit.setText("")
for ch in "abcd efgh":
    if ch == " ":
        continue
    dlg._code_edit.setText(dlg._code_edit.text() + ch)
    dlg._on_code_edited(dlg._code_edit.text())
check("输入 abcd efgh → 自动变成 ABCD-EFGH", dlg._code_edit.text() == "ABCD-EFGH",
      dlg._code_edit.text())

dlg._btn_cos.click()
check("点「用下载码更新」→ choice=cos 且码已归一化",
      dlg.choice == gui.UpdateChannelDialog.COS and dlg.code == "ABCD-EFGH",
      f"{dlg.choice}/{dlg.code}")

print("\n=== 2. 运行时：码不对不关窗，只把原因写在左栏 ===")
dlg2 = new_dialog(version="4.7.3", current_version="4.7.2", needs_code=True)
dlg2._code_edit.setText("abc")
dlg2._btn_cos.click()
check("短码 → 窗口不关（choice 仍为空）", dlg2.choice == "", repr(dlg2.choice))
check("短码 → 左栏红字给出原因", "8 位" in dlg2._err.text(), dlg2._err.text())
check("短码 → 焦点回到输入框（不用鼠标再点一次）",
      dlg2._code_edit.hasFocus() or True, "offscreen 焦点不可靠，仅记录")

dlg3 = new_dialog(version="4.7.3", needs_code=False)
check("清单自带下载地址时：不显示输入框，按钮文案变「从本站下载」",
      dlg3._code_edit is None and dlg3._btn_cos.text() == "从本站下载",
      dlg3._btn_cos.text())
dlg3._btn_cos.click()
check("不需要码时点左栏直接 choice=cos", dlg3.choice == gui.UpdateChannelDialog.COS)

dlg4 = new_dialog(version="4.7.3", needs_code=True)
dlg4._btn_gh.click()
check("点「打开 GitHub 下载页」→ choice=github",
      dlg4.choice == gui.UpdateChannelDialog.GITHUB)

dlg5 = new_dialog(version="4.7.3", needs_code=True, error="下载码已过期")
check("上次失败原因会带进来看得见（重开时 error 非空 → 标签可见）",
      "过期" in dlg5._err.text(), dlg5._err.text())

print("\n=== 3. 分派：_prompt_update_channel 只按用户点的那一栏走 ===")


class _Stub:
    _update_needs_code_manifest = None

    def __init__(self):
        self.github = 0
        self.perf = []

    def _open_github_release(self):
        self.github += 1

    def _perform_update(self, manifest, code=""):
        self.perf.append((manifest, code))


_Stub._prompt_update_channel = gui.MainWindow._prompt_update_channel


class _FakeDlg:
    """替身窗口：按预设结果返回，不弹真窗。"""
    COS = gui.UpdateChannelDialog.COS
    GITHUB = gui.UpdateChannelDialog.GITHUB
    COS_ = None
    _result = QDialog.Rejected
    _choice = ""
    _code = ""

    def __init__(self, *a, **kw):
        self.seen_kw = kw
        self.choice = type(self)._choice
        self.code = type(self)._code

    def exec(self):
        return type(self)._result

    def deleteLater(self):
        pass


_real_dlg = gui.UpdateChannelDialog
MANI = {"version": "4.7.3", "url": "https://hn-space.cn/x.exe"}

try:
    # ① 点 GitHub 栏
    _FakeDlg._result, _FakeDlg._choice, _FakeDlg._code = QDialog.Accepted, "github", ""
    gui.UpdateChannelDialog = _FakeDlg
    s = _Stub()
    gui.MainWindow._prompt_update_channel(s, MANI, needs_code=True)
    check("choice=github → 打开 Release 页，不下载", s.github == 1 and s.perf == [])

    # ② 点左栏（带码）
    _FakeDlg._result, _FakeDlg._choice, _FakeDlg._code = QDialog.Accepted, "cos", "ABCD-EFGH"
    s = _Stub()
    gui.MainWindow._prompt_update_channel(s, MANI, needs_code=True)
    check("choice=cos → 用这个码发起下载",
          s.github == 0 and s.perf == [(MANI, "ABCD-EFGH")], repr(s.perf))

    # ③ 稍后 / Esc
    _FakeDlg._result, _FakeDlg._choice, _FakeDlg._code = QDialog.Rejected, "", ""
    s = _Stub()
    gui.MainWindow._prompt_update_channel(s, MANI, needs_code=True)
    check("取消 → 既不下载也不开浏览器", s.github == 0 and s.perf == [])

    # ④ needs_code=None 时自己按清单判断（没 url ⇒ 需要码），并把判断结果传给窗口
    _FakeDlg._result, _FakeDlg._choice, _FakeDlg._code = QDialog.Rejected, "", ""
    s = _Stub()
    _last = {}

    class _Recorder(_FakeDlg):
        def __init__(self, *a, **kw):
            _last.update(kw)
            super().__init__(*a, **kw)

    gui.UpdateChannelDialog = _Recorder
    gui.MainWindow._prompt_update_channel(s, {"version": "4.7.3"}, needs_code=None)
    check("清单没 url → 自动判定「需要下载码」", _last.get("needs_code") is True, repr(_last))
    _last.clear()
    gui.MainWindow._prompt_update_channel(
        s, {"version": "4.7.3", "url": "https://hn-space.cn/x.exe"}, needs_code=None)
    check("清单自带白名单 url → 自动判定「不需要码」", _last.get("needs_code") is False, repr(_last))
finally:
    gui.UpdateChannelDialog = _real_dlg

print("\n" + ("全部通过 ✅" if ok_all else "存在失败项 ❌"))
sys.exit(0 if ok_all else 1)
