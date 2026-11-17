# -*- coding: utf-8 -*-
"""危险按钮 / 行动型确认框的默认按钮守卫（2026-11-16 全仓统一口径）

用户现场：翻纸窗「回车/空格会误触跳过」已修（ManualDuplexDialog）。之后全仓排查，
发现同类隐患还有 4 个对话框 + 7 处确认框。Qt 的真实规则（实测）：

  · 默认按钮在**对话框显示那一刻**按第一个可用的 autoDefault 按钮决定，
    之后 setEnabled(True) 也不会改；
  · 回车 = 点默认按钮（与焦点无关）；空格只点聚焦按钮。

判据：
  A. gui.py 里每个 QMessageBox.question(...) 都显式传默认按钮，且只能是 No / Cancel
     （不显式传 = 交给 Qt 自己挑，那正是误触的来源）；
  B. 四个不可逆动作对话框：危险按钮全部进 safe_confirm.defuse_dialog_buttons(...)，
     且都有 showEvent → safe_confirm.focus_dialog_not_button(self)；
  C. 运行时：safe_confirm.build_confirm_box 的默认按钮与 Esc 都是「取消」（offscreen，不 exec）；
  D. 变异自证：默认按钮改回 Yes、删掉一个危险按钮的 defuse 名，A / B 必须当场转红。

    python tests/verify_dangerous_default_buttons.py     # exit 0 = 全绿
"""
import ast
import io
import os
import re
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LOCAL_TOOL = os.path.join(REPO, "local_print_tool")
sys.path.insert(0, LOCAL_TOOL)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

GUI = os.path.join(LOCAL_TOOL, "gui.py")
SRC = io.open(GUI, encoding="utf-8").read()

ok_all = True


def check(name, cond, detail=""):
    global ok_all
    if not cond:
        ok_all = False
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))
    return cond


def section(title):
    print("\n" + "=" * 78)
    print(title)


# ── A. 每个确认框都显式指定「默认取消」──────────────────────────────────────
def confirm_default_fails(src):
    """返回失败清单；传变异后的源码文本即可复用做自证。"""
    fails = []
    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        return [f"gui.py 语法错误：{e}"]
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if not (isinstance(fn, ast.Attribute) and fn.attr == "question"):
            continue
        dflt = node.args[4] if len(node.args) >= 5 else None
        for kw in node.keywords:
            if kw.arg == "defaultButton":
                dflt = kw.value
        if dflt is None:
            fails.append(f"L{node.lineno}: 没显式传默认按钮（Qt 会自己挑第一个可用的）")
            continue
        txt = ast.unparse(dflt)
        if not re.search(r"\b(No|Cancel)$", txt):
            fails.append(f"L{node.lineno}: 默认按钮是 {txt}（必须 No / Cancel）")
    return fails


# ── B. 四个不可逆动作对话框：危险按钮 + 焦点 ────────────────────────────────
DIALOGS = {
    "StackQueueDialog": ["_btn_take", "_btn_start", "_btn_abandon"],
    "DuplexRemedyDialog": ["_btn_pass2", "_btn_reprint", "_btn_abandon"],
    "CloudTaskListWindow": ["_accept_all_btn", "_reject_all_btn"],
    "OfflineOrdersDialog": ["_retry_selected_btn", "_retry_all_btn"],
}


def dialog_fails(src):
    fails = []
    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        return [f"gui.py 语法错误：{e}"]
    classes = {n.name: n for n in tree.body if isinstance(n, ast.ClassDef)}
    lines = src.splitlines()
    for cname, btns in DIALOGS.items():
        cls = classes.get(cname)
        if cls is None:
            fails.append(f"{cname}: 找不到类")
            continue
        seg = "\n".join(lines[cls.lineno - 1: (cls.end_lineno or cls.lineno)])
        calls = re.findall(r"safe_confirm\.defuse_dialog_buttons\(([^)]*)\)", seg, re.S)
        blob = " ".join(calls)
        for b in btns:
            if b not in blob:
                fails.append(f"{cname}: {b} 没进 defuse_dialog_buttons（回车会点它）")
        if "def showEvent" not in seg:
            fails.append(f"{cname}: 没有 showEvent")
        elif "safe_confirm.focus_dialog_not_button(self)" not in seg:
            fails.append(f"{cname}: showEvent 里没把焦点收回窗口（空格可能点到按钮）")
    return fails


section("A. 行动型确认框一律显式「默认取消」")
_fails = confirm_default_fails(SRC)
check(f"gui.py 全部 QMessageBox.question 都显式传 No/Cancel 默认按钮（{SRC.count('QMessageBox.question(')} 处）",
      not _fails, "; ".join(_fails[:6]))

section("B. 不可逆动作对话框：危险按钮非默认 + 焦点在窗口")
_dfails = dialog_fails(SRC)
check(f"4 个对话框的危险按钮全部 defuse + showEvent 收回焦点（{sum(len(v) for v in DIALOGS.values())} 个按钮）",
      not _dfails, "; ".join(_dfails[:6]))

section("C. 运行时：确认框的默认按钮 / Esc 都是「取消」")
from PySide6.QtWidgets import QApplication  # noqa: E402

import safe_confirm  # noqa: E402

_app = QApplication.instance() or QApplication([])
_box, _ok_btn, _cancel_btn = safe_confirm.build_confirm_box(None, "删除标签页", "删除后无法恢复。",
                                                            ok_text="删除（不可恢复）")
check("C1 defaultButton 是「取消」（回车不会执行）", _box.defaultButton() is _cancel_btn)
check("C2 escapeButton 也是「取消」（Esc 不会执行）", _box.escapeButton() is _cancel_btn)
check("C3 「执行」按钮 autoDefault=False、direction 不是默认",
      (not _ok_btn.autoDefault()) and (not _ok_btn.isDefault()))
check("C4 「取消」按钮 autoDefault=False（靠 setDefaultButton 当默认，Enter 走它）",
      not _cancel_btn.autoDefault())
_box.deleteLater()

section("D. 变异自证（判据必须能红）")
_mut1 = SRC.replace("QMessageBox.Yes | QMessageBox.No, QMessageBox.No,",
                    "QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes,", 1)
check("D1 变异：把首处默认按钮改回 Yes → A 判据当场转红",
      _mut1 != SRC and bool(confirm_default_fails(_mut1)),
      str(confirm_default_fails(_mut1))[:120])
_mut2 = SRC.replace("self._btn_take, self._btn_start, self._btn_abandon, self._btn_close",
                    "self._btn_start, self._btn_abandon, self._btn_close", 1)
check("D2 变异：把「✓ 我已取走」从 defuse 名单删掉 → B 判据当场转红",
      _mut2 != SRC and bool(dialog_fails(_mut2)),
      str(dialog_fails(_mut2))[:120])

print("\n" + "=" * 78)
print("危险默认按钮统一口径验收：全部通过 ✅" if ok_all else "危险默认按钮统一口径验收：有失败项 ❌")
sys.exit(0 if ok_all else 1)
