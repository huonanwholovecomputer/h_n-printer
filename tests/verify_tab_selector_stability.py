# -*- coding: utf-8 -*-
"""验收脚本 · 标签页选择器「状态图标不挪动加减按钮」（GUI 集成，offscreen）

需求（2026-10 用户反馈）：`[-] [1] [+]` 那一排，**有没有 🔒（已固定）/ ⏸（待人工翻纸）
状态图标，加减按钮的位置都必须一模一样**。

旧机制为什么违约：🔒/⏸ 是数字标签里的**文字前缀**，且随状态把标签宽度在 36/44/52
之间来回改 ⇒ 图标一出现，右侧的 ＋ 就被推着挪 8~16px（用户要按的位置会跑）。
新机制：数字标签只放数字、宽度恒定；状态走**独立固定宽度徽标**（永远占位，无状态时只
清空文字）。

本脚本用真实 MainWindow 逐个状态取控件几何做对拍，钉住这条不变量：
    1. 四种状态（正常/🔒/⏸/回到正常）下 −、数字、＋、🗑 空的 geometry **完全一致**；
    2. 徽标宽度恒定且**始终可见**（不 setVisible(False) —— 隐藏控件会被布局跳过，
       同样会挪位）；
    3. 徽标文字正确（"" / "🔒" / "⏸"）；
    4. 两位数标签页（"12"）也不改变 −/＋ 的几何（宽度由固定标签吸收）；
    5. 静态：数字标签不再承载图标，旧写法（`🔒{n}` + setFixedWidth(52)）已消失。

    python tests/verify_tab_selector_stability.py     # exit 0 = 全绿
"""
import io
import os
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL_TOOL = os.path.join(REPO, "local_print_tool")

_SANDBOX = tempfile.mkdtemp(prefix="hn_tabsel_")
os.environ["APPDATA"] = _SANDBOX                 # 用户数据目录 → 沙箱（别碰真实配置）
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, LOCAL_TOOL)
os.chdir(LOCAL_TOOL)

import paths  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

app = QApplication.instance() or QApplication([])

import gui as _gui  # noqa: E402

# 关窗确认框在 offscreen 下无人可答会死等 → 一律自动回「是」
_gui.QMessageBox.question = staticmethod(lambda *a, **k: _gui.QMessageBox.Yes)
_gui.QMessageBox.warning = staticmethod(lambda *a, **k: _gui.QMessageBox.Ok)
_gui.QMessageBox.information = staticmethod(lambda *a, **k: _gui.QMessageBox.Ok)

from gui import MainWindow  # noqa: E402
from printer_config import TabSettings  # noqa: E402

ok_all = True


def check(name, cond, detail=""):
    global ok_all
    if not cond:
        ok_all = False
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


print("… 正在创建主窗口", flush=True)
win = MainWindow(config_path=paths.config_path())
win.resize(1400, 900)
win.show()
app.processEvents()


def geo(w):
    g = w.geometry()
    return (g.x(), g.y(), g.width(), g.height())


def snap():
    """当前状态下的选择器几何 + 徽标文字/可见性。"""
    return {
        "minus": geo(win._tab_btn_minus),
        "label": geo(win._tab_label),
        "plus": geo(win._tab_btn_plus),
        "badge": geo(win._tab_state_badge),
        "cleanup": geo(win._tab_btn_cleanup_empty),
        "badge_text": win._tab_state_badge.text(),
        "badge_visible": win._tab_state_badge.isVisible(),
    }


# 附加服务行（row2）：徽标 + 「标签页 N」+ 右侧全部下拉/只读标签。
# 其中任何一项随状态挪位，都会让操作员"瞄准好了却按到别处"。
ROW2_WIDGETS = (
    "_tab_scope_badge", "_tab_scope_label",
    "_delivery_onoff_combo", "_delivery_location_combo", "_delivery_percent_label",
    "_urgency_combo", "_urgency_price_label",
    "_cover_page_onoff_combo", "_cover_page_price_label",
)


def snap2():
    """附加服务行几何 + 两个文字控件的文字/可见性。"""
    out = {name: geo(getattr(win, name)) + (getattr(win, name).isVisible(),)
           for name in ROW2_WIDGETS}
    out["scope_badge_text"] = win._tab_scope_badge.text()
    out["scope_label_text"] = win._tab_scope_label.text()
    out["scope_badge_visible"] = win._tab_scope_badge.isVisible()
    return out


def apply_state(frozen: bool, needs_flip: bool) -> tuple:
    tab = win._config.tabs.setdefault(win._current_tab, TabSettings())
    tab.frozen = frozen
    tab.manual_duplex_pending = needs_flip
    win._refresh_tab_display()
    app.processEvents()
    return snap(), snap2()


print("=== 1. 尺寸口径（圆角正方形 + 固定槽位）===")
check("− 按钮 = 方形且边长 == TAB_SWITCH_BTN",
      win._tab_btn_minus.width() == win._tab_btn_minus.height() == _gui.TAB_SWITCH_BTN,
      f"{win._tab_btn_minus.width()}x{win._tab_btn_minus.height()}")
check("＋ 按钮 = 方形且边长 == TAB_SWITCH_BTN",
      win._tab_btn_plus.width() == win._tab_btn_plus.height() == _gui.TAB_SWITCH_BTN,
      f"{win._tab_btn_plus.width()}x{win._tab_btn_plus.height()}")
check("数字标签宽度 == TAB_NUM_WIDTH（与是否有图标无关）",
      win._tab_label.width() == _gui.TAB_NUM_WIDTH, f"{win._tab_label.width()}")
check("徽标宽度 == TAB_BADGE_WIDTH",
      win._tab_state_badge.width() == _gui.TAB_BADGE_WIDTH,
      f"{win._tab_state_badge.width()}")
check("同排控件等高（−/数字/＋/徽标/🗑 空）",
      len({win._tab_btn_minus.height(), win._tab_label.height(),
           win._tab_btn_plus.height(), win._tab_state_badge.height(),
           win._tab_btn_cleanup_empty.height()}) == 1,
      f"{[w.height() for w in (win._tab_btn_minus, win._tab_label, win._tab_btn_plus, win._tab_state_badge, win._tab_btn_cleanup_empty)]}")

print("\n=== 2. 四种状态逐个对拍：按钮几何必须一字不差 ===")
s_normal, s2_normal = apply_state(False, False)
s_frozen, s2_frozen = apply_state(True, False)
s_flip, s2_flip = apply_state(False, True)
s_back, s2_back = apply_state(False, False)

check("徽标文字：正常 = 空", s_normal["badge_text"] == "", repr(s_normal["badge_text"]))
check("徽标文字：已固定 = 🔒", s_frozen["badge_text"] == "🔒", repr(s_frozen["badge_text"]))
check("徽标文字：待翻纸 = ⏸", s_flip["badge_text"] == "⏸", repr(s_flip["badge_text"]))
check("徽标文字：回到正常 = 空", s_back["badge_text"] == "", repr(s_back["badge_text"]))

for state in ("minus", "label", "plus", "badge", "cleanup"):
    vals = {s_normal[state], s_frozen[state], s_flip[state], s_back[state]}
    check(f"{state}: 四种状态下 geometry 完全一致", len(vals) == 1,
          f"{sorted(vals)}")

check("徽标在四种状态下**始终可见**（不用 setVisible(False)，否则布局会跳过它）",
      all(s["badge_visible"] for s in (s_normal, s_frozen, s_flip, s_back)),
      f"{[s['badge_visible'] for s in (s_normal, s_frozen, s_flip, s_back)]}")
check("徽标高度/宽度不随文字变（固定槽位）",
      s_normal["badge"][2:] == s_frozen["badge"][2:] == s_flip["badge"][2:],
      f"{s_normal['badge']} / {s_frozen['badge']} / {s_flip['badge']}")
check("🔒 与 ⏸ 的 ＋ 按钮坐标与正常态完全相同（用户要按的位置不跑）",
      s_frozen["plus"][:2] == s_normal["plus"][:2] == s_flip["plus"][:2],
      f"normal={s_normal['plus'][:2]} frozen={s_frozen['plus'][:2]} flip={s_flip['plus'][:2]}")

print("\n=== 2b. 附加服务行：状态一变，整排下拉框/只读标签都不许动 ===")
check("附加服务行徽标文字：正常 = 📑",
      s2_normal["scope_badge_text"] == "📑", repr(s2_normal["scope_badge_text"]))
check("附加服务行徽标文字：已固定 = 🔒",
      s2_frozen["scope_badge_text"] == "🔒", repr(s2_frozen["scope_badge_text"]))
check("附加服务行徽标文字：待翻纸 = ⏸",
      s2_flip["scope_badge_text"] == "⏸", repr(s2_flip["scope_badge_text"]))
check("可见文字只剩「标签页 N」（⏸ 的长说明不再拼进文字）",
      all(s["scope_label_text"] == "标签页 1"
          for s in (s2_normal, s2_frozen, s2_flip, s2_back)),
      f"{[s['scope_label_text'] for s in (s2_normal, s2_frozen, s2_flip, s2_back)]}")

for name in ROW2_WIDGETS:
    vals = {s2_normal[name], s2_frozen[name], s2_flip[name], s2_back[name]}
    check(f"{name}: 四种状态下 geometry 完全一致", len(vals) == 1, f"{sorted(vals)}")

check("附加服务行徽标在四种状态下始终可见（固定槽位，不靠隐藏腾空间）",
      all(s["scope_badge_visible"] for s in (s2_normal, s2_frozen, s2_flip, s2_back)),
      f"{[s['scope_badge_visible'] for s in (s2_normal, s2_frozen, s2_flip, s2_back)]}")

print("\n=== 3. 两位数标签页（宽度由固定标签吸收，不挤按钮/下拉框）===")
_orig_tab = win._current_tab
win._config.tabs.setdefault("12", TabSettings())
win._current_tab = "12"
win._refresh_tab_display()
app.processEvents()
s_two = snap()
s2_two = snap2()
check("标签页 12：数字标签宽度仍是 TAB_NUM_WIDTH",
      s_two["label"][2] == _gui.TAB_NUM_WIDTH, f"{s_two['label']}")
check("标签页 12：− / ＋ / 徽标几何与标签页 1 完全一致",
      s_two["minus"] == s_normal["minus"] and s_two["plus"] == s_normal["plus"]
      and s_two["badge"] == s_normal["badge"],
      f"minus={s_two['minus']} plus={s_two['plus']}")
check("标签页 12：附加服务行徽标/文字槽位几何不变（位数由固定宽度吸收）",
      s2_two["_tab_scope_badge"] == s2_normal["_tab_scope_badge"]
      and s2_two["_tab_scope_label"] == s2_normal["_tab_scope_label"],
      f"{s2_two['_tab_scope_badge']} / {s2_two['_tab_scope_label']}")
check("标签页 12：右侧 派送/地点/优先级/首页 全部不动",
      all(s2_two[name] == s2_normal[name]
          for name in ROW2_WIDGETS if name not in ("_tab_scope_badge", "_tab_scope_label")),
      f"{ {n: s2_two[n][:2] for n in ROW2_WIDGETS if n not in ('_tab_scope_badge', '_tab_scope_label')} }")
check("「标签页 12」在固定宽度里放得下（留 ≥4px 余量）",
      win._tab_scope_label.fontMetrics().horizontalAdvance("标签页 12")
      <= _gui.TAB_SCOPE_WIDTH - 4,
      f"text={win._tab_scope_label.fontMetrics().horizontalAdvance('标签页 12')} "
      f"slot={_gui.TAB_SCOPE_WIDTH}")
win._current_tab = _orig_tab
win._config.tabs.pop("12", None)
win._refresh_tab_display()
app.processEvents()

print("\n=== 4. 静态：旧机制（状态/说明塞进文字 + 改宽度）已彻底移除 ===")
_src = open(os.path.join(LOCAL_TOOL, "gui.py"), encoding="utf-8").read()
check("数字标签不再带状态前缀", "🔒{self._current_tab}" not in _src
      and 'f"⏸{self._current_tab}"' not in _src)
check("不再有随状态切换的标签宽度（数字标签只设一次固定尺寸）",
      "self._tab_label.setFixedWidth" not in _src
      and "self._tab_label.setFixedSize(" in _src)
check("数字标签宽度只在 _setup_file_table 里按常量设定一次",
      _src.count("self._tab_label.setFixedSize(") == 1
      and "setFixedWidth(TAB_NUM_WIDTH" not in _src.split("def _refresh_tab_display")[1][:400])
check("附加服务行的状态说明不再拼进可见文字，改由常量文案走 tooltip",
      'suffix = "（含双面：需人工翻纸）"' not in _src
      and 'self._tab_scope_label.setText(f"标签页 {self._current_tab}")' in _src
      and "TAB_FLIP_HINT" in _src)
check("「标签页 N」固定宽度按常量设定一次",
      "self._tab_scope_label.setFixedWidth(TAB_SCOPE_WIDTH)" in _src
      and _src.count("self._tab_scope_label.setFixedWidth(") == 1)
check("徽标只改文字、不改宽度/可见性（函数体里没有 setFixed*/setVisible 调用）",
      not any(k in _src.split("def _set_tab_state_badge")[1].split("def _refresh_tab_display")[0]
              for k in (".setFixed", ".setVisible(")))
check("两个徽标都是布局成员（选择器那排 + 附加服务行）",
      "tab_row.addWidget(self._tab_state_badge)" in _src
      and "scope_row.addWidget(self._tab_scope_badge)" in _src)

# 还原（沙箱内，不影响真实配置）
apply_state(False, False)
win.close()
app.processEvents()

print("\n" + "=" * 56)
print("标签页状态徽标（不挪动加减按钮/附加服务行）验收:", "全部通过 ✅" if ok_all else "存在失败 ❌")
sys.exit(0 if ok_all else 1)
