# -*- coding: utf-8 -*-
"""验收 · 「📴 离线未同步订单」入口的显隐与窗口文案（2026-10-23 用户口径）

用户原话：「还有就是这个现在是常驻的吗」→ 确认后要求：「改成有未同步单才出现吧，
此外给界面里面这个 **星号加粗** 删掉」。

判据（全部真 Qt 离屏 + APPDATA 沙箱，不碰真实数据、不打印）：
  ① 文件菜单里的「📴 离线未同步订单…」**默认不出现**（0 条未同步时）；
  ② 本地库有 1 条未同步 → `_refresh_offline_badge()` 一跑，菜单项**出现**，
     状态栏角标同时出现且文案是「📴 离线未同步 1」；有拒收单时角标追加「（N 条拒收）」；
  ③ 未同步归零 → 菜单项与角标**一起消失**（两处同一判据 n>0）；
  ④ 窗口提示文案里**没有 markdown 星号**（`**只存在本机**` 这种在 QLabel 里会被原样显示）；
  ⑤ 窗口空态/非空态文案与按钮可用性正确（0 条：✅ 都已上报 + 两个重试按钮置灰）。

    python tests/verify_offline_orders_entry.py      # exit 0 = 全绿
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

_SANDBOX = tempfile.mkdtemp(prefix="hn_offline_entry_")
os.environ["APPDATA"] = _SANDBOX                  # 用户数据目录 → 沙箱
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, LOCAL_TOOL)
os.chdir(LOCAL_TOOL)

from PySide6.QtWidgets import QApplication  # noqa: E402

_app = QApplication.instance() or QApplication([])

import converter as _converter  # noqa: E402
_converter.get_available_engines = lambda: {"word": True, "wps": True, "libreoffice": True}

import gui  # noqa: E402
import paths  # noqa: E402

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


def section(t):
    print("\n" + "=" * 78)
    print(t)
    print("=" * 78)


win = gui.MainWindow(config_path=paths.config_path())

sync = win._offline_sync
_state = {"unsynced": 0, "rejected": 0, "rows": []}


def fake_summary():
    return {"unsynced": _state["unsynced"], "rejected": _state["rejected"],
            "synced": 0, "total": _state["unsynced"] + _state["rejected"]}


def fake_list():
    return list(_state["rows"])


sync.status_summary = fake_summary          # 只替换计数与列表，其余逻辑走真实实现
sync.list_unsynced_orders = fake_list

section("① 默认（0 条未同步）：菜单项与角标都不出现")
win._refresh_offline_badge()
act = getattr(win, "_offline_action", None)
check("文件菜单项对象存在（还是那个入口，只是默认隐藏）", act is not None)
check("0 条未同步 → 菜单项**不出现**", act is not None and not act.isVisible())
# 窗口本身没 show() ⇒ 用 isHidden()（显式隐藏标记）而不是 isVisible()（还受父窗口可见性影响）
check("0 条未同步 → 状态栏角标也不出现", win._offline_badge.isHidden())
# 同时在真实菜单树里确认这个 action 就在「文件(&F)」下（不是被放到别处/漏加）
# 注意：menuBar().actions() 返回的包装对象若不留住，取出的子菜单会被 GC 掉 → 先整体持有
_menu_actions = list(win.menuBar().actions())
file_menu = next((a.menu() for a in _menu_actions
                  if a.text().replace("&", "") == "文件(F)"), None)
check("「文件」菜单里确实挂着这个 action（隐藏不等于没加）",
      file_menu is not None and act in file_menu.actions())
check("隐藏时「文件」菜单的可见项数 = 全部项 - 1", file_menu is not None
      and sum(1 for a in file_menu.actions() if a.isVisible()) == len(file_menu.actions()) - 1,
      file_menu and [(a.text(), a.isVisible()) for a in file_menu.actions()])

section("② 有 1 条未同步：菜单项与角标一起出现")
_state.update(unsynced=1, rejected=0)
win._refresh_offline_badge()
check("1 条未同步 → 菜单项出现", act.isVisible())
check("1 条未同步 → 角标出现且文案 = 「📴 离线未同步 1」",
      not win._offline_badge.isHidden() and win._offline_badge.text() == "📴 离线未同步 1",
      win._offline_badge.text())

section("③ 有拒收单：角标追加「（N 条拒收）」")
_state.update(unsynced=3, rejected=2)
win._refresh_offline_badge()
check("角标文案含总条数与拒收条数",
      win._offline_badge.text() == "📴 离线未同步 3（2 条拒收）", win._offline_badge.text())

section("④ 未同步归零：菜单项与角标一起消失")
_state.update(unsynced=0, rejected=0)
win._refresh_offline_badge()
check("归零 → 菜单项消失", not act.isVisible())
check("归零 → 角标消失且文案清空",
      win._offline_badge.isHidden() and win._offline_badge.text() == "")

section("⑤ 窗口提示文案：没有 markdown 星号，空态/非空态都正确")
_state.update(unsynced=0, rejected=0, rows=[])
win._open_offline_orders_dialog()
dlg = win._offline_dialog
hint = dlg._hint.text()
check("提示文案里**没有** markdown 星号（`**…**` 在 QLabel 里会原样显示）",
      "*" not in hint, hint)
check("提示文案仍说清了三件事：只在本机 / 自动补报 / 可手动重试",
      "只存在本机" in hint and "自动补报" in hint and "手动重试" in hint, hint)
check("0 条 → 空态文案「没有离线未同步订单…」",
      "没有离线未同步订单" in dlg._summary.text(), dlg._summary.text())
check("0 条 → 两个重试按钮都置灰",
      not dlg._retry_all_btn.isEnabled() and not dlg._retry_selected_btn.isEnabled())

_rows = [
    {"order_number": "HN20261023-0001", "created_at": "2026-10-23 10:00:00",
     "total_price": 3.6, "status": "pending", "retry_count": 2, "last_error": "ConnectionError"},
    {"order_number": "HN20261023-0002", "created_at": "2026-10-23 10:05:00",
     "total_price": 1.2, "status": "rejected", "retry_count": 0, "last_error": "订单重复"},
]
_state.update(unsynced=2, rejected=1, rows=_rows)
dlg.reload()
check("2 条（含 1 条拒收）→ 汇总行写明条数与拒收数",
      "共 2 条" in dlg._summary.text() and "1 条被云端拒收" in dlg._summary.text(),
      dlg._summary.text())
check("表格真的填了 2 行", dlg._table.rowCount() == 2, dlg._table.rowCount())
check("状态列不把未同步单写成「已完成」",
      all("已完成" not in dlg.status_text(r) for r in _rows)
      and "离线未同步" in dlg.status_text(_rows[0])
      and "云端拒收" in dlg.status_text(_rows[1]))
check("非空 → 「全部重试」可用", dlg._retry_all_btn.isEnabled())
check("非空 → 未选中任何行时「重试选中」仍置灰", not dlg._retry_selected_btn.isEnabled())
dlg.close()

print("\n" + "=" * 78)
print(("全部通过 ✅" if ok_all else "存在失败 ❌"))
sys.exit(0 if ok_all else 1)
