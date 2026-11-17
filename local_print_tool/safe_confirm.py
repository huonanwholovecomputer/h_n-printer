"""safe_confirm.py — 危险确认框的统一口径（2026-11-16 全仓统一）

为什么单独一个模块
------------------
2026-11-16 用户报「回车/空格会误触」之后全仓排查，发现同一个坑有 4 个对话框 + 7 处
确认框：Qt 是在**对话框显示那一刻**按第一个可用的 autoDefault 按钮决定默认按钮的，
之后再 setEnabled(True) 也不会改；回车 = 点默认按钮（与焦点无关），空格只点聚焦按钮。
于是只要「危险按钮」先于普通按钮可用，操作员随手一回车就真的执行了（跳过第 2 趟、
全部添加到新标签页、补打第 2 趟、清空账本…）。

统一口径（新代码只准用这里的函数）
----------------------------------
① 所有「行动型」确认框 → confirm_dangerous()：默认按钮**固定是「取消」**，
   Esc 也走「取消」；按钮文字写清后果（不用笼统的「是/否」）。
② 不可逆动作所在的对话框 → 按钮逐个 defuse_dialog_buttons()（setAutoDefault(False)
   + setDefault(False)），并在该窗的 showEvent 里 focus_dialog_not_button(self)，
   让焦点落在窗口本身（与 ManualDuplexDialog 同一套，见 gui.py:1044 的现场说明）。
③ 判断标准：这个动作做错了要不要人工重来 / 会不会白打纸 / 会不会丢数据。
   是 → 必须走 ①②；只是「打开一个窗口」这类可逆动作不算。

用法
----
    from safe_confirm import confirm_dangerous, defuse_dialog_buttons, focus_dialog_not_button

    if not confirm_dangerous(self, "删除标签页",
                             f"标签页 {key} 里有 {len(jobs)} 个文件，删除后无法恢复。",
                             ok_text="删除（不可恢复）"):
        return

    # 对话框里的危险按钮
    defuse_dialog_buttons(self._btn_take, self._btn_abandon)
    ...
    def showEvent(self, ev):
        super().showEvent(ev)
        focus_dialog_not_button(self)
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QMessageBox


def build_confirm_box(parent, title: str, text: str,
                      ok_text: str = "继续", cancel_text: str = "取消"):
    """构造确认框并返回 (box, ok_btn, cancel_btn)。

    默认按钮与 Esc 都是「取消」——回车/关闭键都不会替操作员做决定。
    拆出构造是为了让无头测试能断言 defaultButton()，不必真的 exec()。
    """
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Warning)
    box.setWindowTitle(title)
    box.setText(text)
    ok_btn = box.addButton(ok_text, QMessageBox.AcceptRole)
    cancel_btn = box.addButton(cancel_text, QMessageBox.RejectRole)
    for b in (ok_btn, cancel_btn):
        b.setAutoDefault(False)
        b.setDefault(False)
    box.setDefaultButton(cancel_btn)
    box.setEscapeButton(cancel_btn)
    return box, ok_btn, cancel_btn


def confirm_dangerous(parent, title: str, text: str,
                      ok_text: str = "继续", cancel_text: str = "取消") -> bool:
    """行动型确认框：只有真的点了「执行」按钮才返回 True。

    返回 True 当且仅当 clickedButton 是 ok_btn —— 回车、Esc、点 × 一律 False。
    """
    box, ok_btn, _cancel = build_confirm_box(parent, title, text, ok_text, cancel_text)
    box.exec()
    return box.clickedButton() is ok_btn


def defuse_dialog_buttons(*buttons) -> None:
    """把一组按钮变成「非默认按钮」：回车不会触发它们。

    与 gui.py 里 ManualDuplexDialog（:1050）同一口径；Tab 仍能到按钮，键盘可用性不丢。
    """
    for b in buttons:
        b.setAutoDefault(False)
        b.setDefault(False)


def focus_dialog_not_button(dialog) -> None:
    """窗口显示时焦点落在窗口本身、不落在任何按钮上（空格/回车都不替操作员做决定）。"""
    dialog.setFocus(Qt.OtherFocusReason)
