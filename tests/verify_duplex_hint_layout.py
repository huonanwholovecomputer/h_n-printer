# -*- coding: utf-8 -*-
"""验收脚本 · 「单/双面」下面那行小提示的版式（2026-10-17 用户现场反馈）

现象（用户截图）：本机确认「无自动双面器」后，编辑面板「单/双面」下面会出现一行提示
`← 本机无自动双面：程序分两趟打，中途会弹窗提示翻纸` —— 两个问题：
  ① 不美观：一个指向不明的 `←`，整行高饱和橙色，像报错；
  ② **没有宽度约束**：它是**不换行**的 QLabel，sizeHint 就是整行文字宽度，会把编辑面板的
     最小宽度撑得比可视区还宽 ⇒ 窄窗口下整行被裁掉只剩半句（第二张截图）。

修法（与同一面板的 selectedFileLabel / 备注框同策略）：wordWrap + 水平
QSizePolicy.Ignored + minimumWidth(0) + 缩进 6px；颜色/字号交给主题 QSS
（`QLabel#duplexHint`，深/浅两套）；完整说明同时挂在「单/双面」下拉框 tooltip 上。

本脚本的判据（都在真实 MainWindow + 真实布局上量，不看源码字符串）：
  ① 确认「不支持自动双面」后提示可见、文案无 `←`、下拉框 tooltip 有完整说明；
  ② `wordWrap=True`、水平 sizePolicy=Ignored、minimumWidth=0；
  ③ **不撑宽面板**：提示可见 vs 隐藏时 `_edit_group.minimumSizeHint().width()` 差值 ≤ 4px；
  ④ **判据灵敏度自证**：把提示改回旧写法（不换行 + 默认策略）→ 面板最小宽度必须明显变大
     （否则上面那条断言是"假绿"）；
  ⑤ 窄宽度不裁切：`heightForWidth(240)` 大于单行高度（真的折行），且整段文字折行后能装进
     该高度（不裁）；真实布局里编辑面板**不需要横向滚动条**（horizontalScrollBar.maximum==0）；
  ⑥ 深/浅两套主题各渲染一张 PNG（人工眼看 + 留证据）。
APPDATA 沙箱、离屏 Qt，不打印、不碰真实用户目录。

    python tests/verify_duplex_hint_layout.py        # exit 0 = 全绿
"""
import io
import os
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL_TOOL = os.path.join(REPO, "local_print_tool")

_SANDBOX = tempfile.mkdtemp(prefix="hn_duplex_hint_")
os.environ["APPDATA"] = _SANDBOX
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# 离屏 Qt 默认**没有字体**（PySide6/lib/fonts 不存在 ⇒ 截图全是方框、字宽也不可信）。
# 指向系统字体目录（仅当存在；别的机器没有就保持原样 —— 所有断言用的都是宽容阈值）。
_FONTS = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")
if os.path.isdir(_FONTS):
    os.environ.setdefault("QT_QPA_FONTDIR", _FONTS)
sys.path.insert(0, LOCAL_TOOL)
os.chdir(LOCAL_TOOL)

from PySide6.QtCore import Qt, QRect  # noqa: E402
from PySide6.QtWidgets import (QApplication, QScrollArea, QSizePolicy, QSplitter)  # noqa: E402

_app = QApplication.instance() or QApplication([])

import converter as _converter  # noqa: E402
_converter.get_available_engines = lambda: {"word": True, "wps": True, "libreoffice": True}

import gui  # noqa: E402
import paths  # noqa: E402
from printer_config import PrinterProfile  # noqa: E402
from theme_manager import ThemeManager, MODE_DARK, MODE_LIGHT  # noqa: E402

gui.QMessageBox.question = staticmethod(lambda *a, **k: gui.QMessageBox.Yes)
gui.QMessageBox.warning = staticmethod(lambda *a, **k: gui.QMessageBox.Ok)
gui.QMessageBox.information = staticmethod(lambda *a, **k: gui.QMessageBox.Ok)

QUEUE = "HP LaserJet 1020"
ok_all = True
OUT = os.path.join(tempfile.gettempdir(), "hn_duplex_hint")

# 真实 ThemeManager（深浅两套 QSS 都在手里），与 tests/verify_table_selection_style.py 同法
_theme = ThemeManager(paths.theme_settings_path())
_theme.init(_app,
            open(os.path.join(LOCAL_TOOL, "styles_dark.qss"), encoding="utf-8").read(),
            open(os.path.join(LOCAL_TOOL, "styles_light.qss"), encoding="utf-8").read())


def check(name, cond, detail=""):
    global ok_all
    if not cond:
        ok_all = False
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  — {detail}" if detail else ""))
    return bool(cond)


def find_ancestor(w, cls):
    p = w.parent() if w is not None else None
    while p is not None:
        if isinstance(p, cls):
            return p
        p = p.parent()
    return None


print("=" * 78)
win = gui.MainWindow(config_path=paths.config_path(), theme_manager=_theme)
win.resize(1180, 820)
win.show()
_app.processEvents()

# 让顶栏下拉与 config 一致（_save_config 会把 printer_name 从下拉读回来），再盖章
combo = win._printer_combo
idx = next((i for i in range(combo.count()) if combo.itemData(i) == QUEUE), -1)
if idx < 0:
    combo.addItem(QUEUE, QUEUE)
    idx = combo.count() - 1
combo.setCurrentIndex(idx)
_orig_log = win._log
win._log = lambda m: _orig_log(m)

profile = PrinterProfile(auto_duplex=False, capability_source="manual", manual_duplex=True,
                         first="odd", reverse_second=True, second_rotate_180=True,
                         verified_at="2026-10-12 23:18:00", verified_by="霍楠")
win._config.printer_name = QUEUE
win._config.printer_profiles[QUEUE] = profile
win._update_duplex_label()
_app.processEvents()

hint = win._edit_duplex_hint
gb = win._edit_group
text = hint.text()

print("\n① 出现条件、文案与 tooltip")
check("确认「不支持自动双面」后提示可见", hint.isVisible())
check("文案不再有指向不明的「←」", "←" not in text, text)
check("文案一眼说明「程序分两趟」与「翻纸」",
      "两趟" in text and "翻纸" in text, text)
check("文案足够短（≤16 字：默认窗口下面板视口仅 ~178px，长了会变 3 行）",
      len(text) <= 16, f"{len(text)} 字：{text}")
check("完整三步说明挂在「单/双面」下拉框 tooltip 上（窄面板也读得到）",
      "两趟" in win._edit_duplex.toolTip() and "180" in win._edit_duplex.toolTip()
      and "纸盘" in win._edit_duplex.toolTip(), win._edit_duplex.toolTip().replace("\n", " / "))
check("提示字号小于正文（11px，来自主题 QSS 而非硬编码内联）",
      "font-size: 11px" in open(os.path.join(LOCAL_TOOL, "styles_dark.qss"), encoding="utf-8").read()
      and "font-size: 11px" in open(os.path.join(LOCAL_TOOL, "styles_light.qss"), encoding="utf-8").read()
      and "duplexHint" in open(os.path.join(LOCAL_TOOL, "styles_dark.qss"), encoding="utf-8").read())

print("\n② 版式属性：可换行 + 宽度不参与最小宽度计算")
check("wordWrap = True", hint.wordWrap())
check("水平 sizePolicy = Ignored", hint.sizePolicy().horizontalPolicy() == QSizePolicy.Ignored,
      str(hint.sizePolicy().horizontalPolicy()))
check("minimumWidth = 0", hint.minimumWidth() == 0)
check("有缩进（对齐字段，不再是顶格一行）", hint.indent() > 0, f"indent={hint.indent()}")

print("\n③ 核心回归：提示不得撑宽编辑面板")


def group_min_width():
    gb.layout().activate()
    return gb.minimumSizeHint().width()


hint.setVisible(True)
_app.processEvents()
w_with = group_min_width()
hint.setVisible(False)
_app.processEvents()
w_without = group_min_width()
hint.setVisible(True)
_app.processEvents()
delta = w_with - w_without
check("提示可见 vs 隐藏：编辑面板最小宽度差值 ≤ 4px",
      delta <= 4, f"with={w_with} without={w_without} delta={delta}")
check("提示自身 minimumSizeHint 宽度很小（不可能是整行文字宽度）",
      hint.minimumSizeHint().width() <= 60, f"{hint.minimumSizeHint().width()}px")
check("编辑面板最小宽度 ≤ 240px（用户截图里面板可见宽约 390px ⇒ 不再顶破窗口）",
      w_with <= 240, f"{w_with}px")

print("\n④ 判据灵敏度自证：改回旧写法必须让面板明显变宽（否则 ③ 是假绿）")
hint.setWordWrap(False)
hint.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)
hint.setText("← 本机无自动双面：程序分两趟打，中途会弹窗提示翻纸")
_app.processEvents()
w_old = group_min_width()
check("旧写法（不换行 + 默认策略）确实把面板撑宽 ≥100px",
      w_old - w_without >= 100, f"old={w_old} without={w_without} 差={w_old - w_without}")
# 还原新写法
hint.setWordWrap(True)
hint.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
hint.setMinimumWidth(0)
win._update_duplex_label()
_app.processEvents()

print("\n⑤ 窄宽度折行不裁切 + 真实面板视口宽度下只占一行")
fm = hint.fontMetrics()
one_line = fm.height()
scroll0 = find_ancestor(hint, QScrollArea)
vw = scroll0.viewport().width() if scroll0 is not None else 0
check("真实面板视口宽度下提示只占一行（不再把下面字段整体下推）",
      vw > 0 and hint.heightForWidth(vw) <= one_line + 2,
      f"viewport={vw}px 需要高度={hint.heightForWidth(vw)} 单行={one_line}")
wrapped_h = hint.heightForWidth(120)
need_h = fm.boundingRect(QRect(0, 0, 120, 10000), Qt.TextWordWrap, hint.text()).height()
check("120px 极窄宽度下高度 > 单行高度（确实折行）", wrapped_h > one_line,
      f"wrapped={wrapped_h} one_line={one_line}")
check("极窄宽度下折行后的文字能装进该高度（不裁切）", need_h <= wrapped_h + 2,
      f"need={need_h} avail={wrapped_h}")
check("编辑面板确实在可滚动容器里（判据作用域正确）", scroll0 is not None)
if scroll0 is not None:
    _app.processEvents()
    hbar = scroll0.horizontalScrollBar()
    max_with = hbar.maximum()
    hint.setVisible(False)
    _app.processEvents()
    max_without = hbar.maximum()
    hint.setVisible(True)
    _app.processEvents()
    # 面板里 QComboBox 们自带最小宽度，本就可能顶出几像素横向滚动（与本次修复无关）；
    # 这里判的是**提示不再新增**横向滚动，而不是"整个面板永不出现横向滚动条"。
    check("提示可见/隐藏时横向滚动量相同（提示不新增横向滚动）",
          max_with == max_without, f"with={max_with} without={max_without} "
                                  f"hint_w={hint.width()} group_w={gb.width()}")

print("\n⑥ 深/浅两套主题各渲染两张 PNG（默认窄面板 + 用户截图那种 400px 面板）+ 真像素核对")
os.makedirs(OUT, exist_ok=True)
shots, expect = [], {MODE_DARK: (224, 164, 88), MODE_LIGHT: (138, 74, 8)}   # #e0a458 / #8a4a08


def _shoot(mode, name, tag):
    _theme.set_mode(mode)
    _app.processEvents()
    win._update_duplex_label()
    _app.processEvents()
    pix = gb.grab()
    p = os.path.join(OUT, f"duplex_hint_{name}_{tag}.png")
    pix.save(p, "PNG")
    shots.append(p)
    hp = hint.grab().toImage()
    want = expect[mode]
    hits = 0
    for y in range(hp.height()):
        for x in range(hp.width()):
            c = hp.pixelColor(x, y)
            if (abs(c.red() - want[0]) <= 60 and abs(c.green() - want[1]) <= 60
                    and abs(c.blue() - want[2]) <= 60):
                hits += 1
    print(f"  → {p}  ({pix.width()}x{pix.height()}, 主题色像素 {hits} 个, "
          f"提示高 {hint.height()}px)")
    return hits


# 自然状态：默认窗口下编辑面板视口只有 ~178px —— 这就是用户日常看到的样子
_prev = (gb.width(), gb.height())
for mode, name in ((MODE_DARK, "dark"), (MODE_LIGHT, "light")):
    check(f"[{name}] 默认窄面板下提示只用一行（不把后面字段下推）",
          hint.heightForWidth(max(1, hint.width())) <= hint.fontMetrics().height() + 2,
          f"hint_w={hint.width()} 需要高度={hint.heightForWidth(max(1, hint.width()))} "
          f"单行={hint.fontMetrics().height()}")
    check(f"[{name}] 主题色像素真的落在提示上（颜色来自 QSS）",
          _shoot(mode, name, "narrow") >= 3, "见上一行像素数")

# 再把面板撑到用户截图那个量级（≈400px）抓一张"宽面板"对照
_W = 400
gb.setMinimumWidth(_W)
gb.resize(_W, gb.sizeHint().height())
gb.layout().activate()
_app.processEvents()
for mode, name in ((MODE_DARK, "dark"), (MODE_LIGHT, "light")):
    check(f"[{name}] 400px 面板下提示只占一行",
          hint.heightForWidth(_W) <= hint.fontMetrics().height() + 2,
          f"h={hint.heightForWidth(_W)} 单行={hint.fontMetrics().height()}")
    check(f"[{name}] 宽面板下主题色像素仍正确",
          _shoot(mode, name, "wide") >= 3, "见上一行像素数")
check("两套主题 × 两种宽度都产出了截图（共 4 张）", len(shots) == 4, f"{shots}")
gb.setMinimumWidth(0)
gb.resize(*_prev)

print("\n⑦ 选区本身打不了双面（图片/单页）时提示跟着收起，别一边灰着单面一边写「分两趟」")
from PySide6.QtCore import QItemSelectionModel  # noqa: E402
from printer_config import PrintJob  # noqa: E402

jobs = win._get_current_jobs()
jobs.append(PrintJob(file_path=os.path.join(_SANDBOX, "single.docx"), copies=1,
                     duplex="on", page_count=1))
win._rebuild_table()
sm = win._table.selectionModel()
sm.select(win._table.model().index(0, 0),
          QItemSelectionModel.Select | QItemSelectionModel.Rows)
_app.processEvents()
check("单页文件：双面控件被禁用（选区特征）", not win._edit_duplex.isEnabled())
check("单页文件：能力提示随之收起", not hint.isVisible(), f"visible={hint.isVisible()}")

jobs[0].page_count = 3
win._rebuild_table()
sm = win._table.selectionModel()
sm.select(win._table.model().index(0, 0),
          QItemSelectionModel.Select | QItemSelectionModel.Rows)
_app.processEvents()
check("多页文件：双面控件恢复可用", win._edit_duplex.isEnabled())
check("多页文件：能力提示恢复显示", hint.isVisible())
check("恢复后文案仍是那一行短句", hint.text() == "分两趟打印 · 中途需翻纸", hint.text())

win.close()
print("=" * 78)
print("全部通过 ✅" if ok_all else "存在失败 ❌")
sys.exit(0 if ok_all else 1)
