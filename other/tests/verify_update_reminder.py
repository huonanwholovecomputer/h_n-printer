# -*- coding: utf-8 -*-
"""验收 · 更新提醒不能「隐身」（2026-11-03 现场）

现场：`DESKTOP-EJGEB1V` 从 4.6.0 起数天没升级。它在 08:20:17 只拉过一次
`/updates/update.json`（拿到 4.6.1 清单 → 启动弹窗 → 点「稍后」），之后再没查过；
桌面工具是**常驻进程**，只查启动那一次 ⇒ 12:51 的换号补报仍在跑旧代码、丢接单设备
（HN20261103-0014/0035/0036 三单「接单设备」空白）。

本脚本钉住三条（都不依赖网络、不弹真窗）：
  1. 启动检查之后还有**周期静默复检**（2 小时一次），且静默复检不弹窗；
  2. 查到新版本后，「帮助 → 检查更新」菜单文案**常驻**显示 `⬆ 有新版本 vX`；
  3. 确认已是最新 / 没有清单时，该标记要被清掉（不能永远挂着一个假的新版本）。

    python other/tests/verify_update_reminder.py     # exit 0 = 全绿
"""
import io
import os
import re
import shutil
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LOCAL_TOOL = os.path.join(REPO, "local_print_tool")
GUI_PATH = os.path.join(LOCAL_TOOL, "gui.py")

ok_all = True


def check(name, cond, detail=""):
    global ok_all
    if not cond:
        ok_all = False
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


src = open(GUI_PATH, encoding="utf-8").read()

print("=== 1. 静态：周期静默复检接线 ===")
check("存在周期复检定时器 _updateRecheckTimer", "_updateRecheckTimer" in src)
m = re.search(r"_updateRecheckTimer\.setInterval\(\s*([0-9 *]+)\)", src)
check("周期是 2 小时", bool(m) and "2 * 60 * 60 * 1000" in m.group(1), m.group(1) if m else "未找到")
check("周期复检走 quiet=True（不弹窗）",
      bool(re.search(r"_updateRecheckTimer\.timeout\.connect\([\s\S]{0,200}?quiet=True", src)))
check("_start_update_check 接受 quiet 参数",
      bool(re.search(r"def _start_update_check\(self, manual: bool = False, quiet: bool = False\)", src)))
check("quiet 分支在弹窗之前 return（静默复检绝不弹窗）",
      bool(re.search(r"if quiet:[\s\S]{0,400}?return\s*\n\s*if manual:", src)))
check("启动检查仍是 manual=False 且不 quiet（照旧提示一次）",
      bool(re.search(r"QTimer\.singleShot\(4000, lambda: self\._start_update_check\(manual=False\)\)", src)))
check("菜单文案带 ⬆ 有新版本标记",
      "_update_pending_version" in src and "⬆ 有新版本" in src)

print("=== 2. 行为：菜单标记出现 / 清除（不弹窗、不联网）===")
_SANDBOX = tempfile.mkdtemp(prefix="hn_upd_")
os.environ["APPDATA"] = _SANDBOX
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, LOCAL_TOOL)
os.chdir(LOCAL_TOOL)

import paths  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

app = QApplication.instance() or QApplication([])

import gui as _gui  # noqa: E402


class _NoNet:
    def get(self, *a, **k):
        raise AssertionError("本测试不应发请求")

    post = get


_gui.http_requests = _NoNet()
_gui.QMessageBox.question = staticmethod(lambda *a, **k: _gui.QMessageBox.Yes)
_gui.QMessageBox.warning = staticmethod(lambda *a, **k: _gui.QMessageBox.Ok)
_gui.QMessageBox.information = staticmethod(lambda *a, **k: _gui.QMessageBox.Ok)

from gui import MainWindow  # noqa: E402

win = MainWindow(config_path=paths.config_path())
win.show()

print("—— 初始：没有待更新版本 ⇒ 菜单是朴素的「检查更新」——")
win._update_pending_version = ""
win._set_update_menu_state(False)
check("初始文案不含新版本标记",
      "有新版本" not in win._update_action.text(), win._update_action.text())

print("—— 静默复检拿到新版本：只标记、不弹窗 ——")
win._update_check_quiet = True
win._update_checking = True
win._on_update_check_result(("available", {"version": "9.9.9", "notes": ""}, False))
check("记下了待更新版本", win._update_pending_version == "9.9.9", win._update_pending_version)
check("★ 菜单常驻显示新版本", "9.9.9" in win._update_action.text(), win._update_action.text())
check("quiet 标记已复位（不会影响下一次手动检查）", win._update_check_quiet is False)

print("—— 确认已是最新：标记必须清掉 ——")
win._update_check_quiet = False
win._update_checking = True
win._on_update_check_result(("latest", None, False))
check("待更新版本已清空", win._update_pending_version == "")
check("菜单文案回到朴素态", "有新版本" not in win._update_action.text(), win._update_action.text())

print("=== 3. 收尾 ===")
win.close()
app.processEvents()
shutil.rmtree(_SANDBOX, ignore_errors=True)
print("\n" + ("全部通过 ✅" if ok_all else "存在失败项 ❌"))
sys.exit(0 if ok_all else 1)
