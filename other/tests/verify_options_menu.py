# -*- coding: utf-8 -*-
"""验收脚本 · 「选项」菜单 + 「默认任务参数…」对话框（task-4 #11，offscreen）

需求（用户 2026-11）：
1. 菜单栏新增「选项(O)」，把原来散在「文件」/「帮助」里的四项收进来：
   外部打印监视、外部打印账本、打印速度、打印机双面能力；原位置移除，菜单结构与快捷键不能坏。
2. 「选项 → 默认任务参数…」弹出对话框：单/双面、双面模式、页边距上下左右、页眉页脚、
   纸张方向、纸张大小、缩放、打印范围、DPI、图片方向、排除末尾空白页、封面单独打印区间。
3. 保存进 print_config.json（paper_size + default_task_options），旧配置缺字段回落：
   A4 / 边距 1cm / 页眉页脚 0 / 水平居中 / 所有列一页 / 双面 / 纵向；**没改过 = 旧行为**。
4. 新复选框必须是 ThemedCheckBox；QLabel/tooltip 文案不许出现 markdown 星号。

    python tests/verify_options_menu.py     # exit 0 = 全绿
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

_SANDBOX = tempfile.mkdtemp(prefix="hn_optsmenu_")
os.environ["APPDATA"] = _SANDBOX                 # 用户数据目录 → 沙箱（别碰真实配置）
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, LOCAL_TOOL)
os.chdir(LOCAL_TOOL)

import paths  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog  # noqa: E402

app = QApplication.instance() or QApplication([])

import gui as _gui  # noqa: E402
import converter as _converter  # noqa: E402
import printer_config as pc  # noqa: E402

_converter.get_available_engines = lambda: {"word": True, "wps": True, "libreoffice": True}
_gui.QMessageBox.question = staticmethod(lambda *a, **k: _gui.QMessageBox.Yes)
_gui.QMessageBox.warning = staticmethod(lambda *a, **k: _gui.QMessageBox.Ok)
_gui.QMessageBox.information = staticmethod(lambda *a, **k: _gui.QMessageBox.Ok)

from gui import MainWindow  # noqa: E402

SRC = io.open(os.path.join(LOCAL_TOOL, "gui.py"), encoding="utf-8").read()
ok_all = True


def check(name, cond, extra=""):
    global ok_all
    if not cond:
        ok_all = False
        print(f"❌ {name}" + (f"  [{extra}]" if extra else ""))
    else:
        print(f"✅ {name}")


def menu_titles(win):
    """菜单栏 → {标题: [动作文案...]}。"""
    out = {}
    for act in win.menuBar().actions():
        m = act.menu()
        if m is not None:
            out[act.text()] = [a.text() for a in m.actions()]
    return out


# ══════════════════════════════════════════════════════════════════════════
print("\n=== ① 菜单结构：新增「选项」，四项已迁入、原位置已移除 ===")
win = MainWindow(config_path=paths.config_path())
menus = menu_titles(win)
titles = list(menus.keys())
check("菜单栏有「选项(O)」", any("选项" in t for t in titles), str(titles))
opt_title = next((t for t in titles if "选项" in t), "")
opts = menus.get(opt_title, [])
check("选项菜单含四项（打印速度/双面能力/外部打印监视/外部打印账本）",
      all(any(k in item for item in opts) for k in
          ("打印速度", "打印机双面能力", "外部打印监视", "外部打印账本")), str(opts))
check("选项菜单第一项是「默认任务参数…」",
      bool(opts) and "默认任务参数" in opts[0], str(opts[:2]))
file_title = next((t for t in titles if "文件" in t), "")
help_title = next((t for t in titles if "帮助" in t), "")
check("文件菜单里不再有外部打印监视/账本",
      not any("外部打印" in i for i in menus.get(file_title, [])),
      str(menus.get(file_title, [])))
check("帮助菜单里不再有打印速度/双面能力（迁走了而不是复制一份）",
      not any(("打印速度" in i) or ("打印机双面能力" in i) for i in menus.get(help_title, [])),
      str(menus.get(help_title, [])))
check("帮助菜单仍保留 快捷键/自检/日志/检查更新/关于",
      all(any(k in i for i in menus.get(help_title, [])) for k in
          ("快捷键", "自检", "日志", "检查更新", "关于")), str(menus.get(help_title, [])))
check("选项菜单排在主题菜单之前",
      titles.index(opt_title) < next((i for i, t in enumerate(titles) if "主题" in t), 999),
      str(titles))

print("\n=== ② 快捷键与既有入口没被菜单搬迁带坏 ===")
_sc_dq = SRC.count("QShortcut(QKeySequence(_seq), self)")
check("tests/verify_tab_arrow_shortcut.py 钉住的字面量仍是 2 处", _sc_dq == 2, str(_sc_dq))
check("Ctrl+T / Ctrl+N 循环、Ctrl+W、Ctrl+Return 仍在",
      '("Ctrl+T", "Ctrl+N")' in SRC or 'for _seq in ("Ctrl+T", "Ctrl+N")' in SRC)
check("外部打印监视仍是 checkable 且接线到 _on_external_watch_toggled",
      "self._ext_print_action.setCheckable(True)" in SRC
      and "self._ext_print_action.toggled.connect(self._on_external_watch_toggled)" in SRC
      and "self._ext_print_action.setChecked(bool(" in SRC)
check("setChecked 在 connect 之前（启动恢复不会重复起线程）",
      SRC.index("self._ext_print_action.setChecked(bool(")
      < SRC.index("self._ext_print_action.toggled.connect("))
check("打印速度/双面能力仍走原槽函数",
      "speed_action.triggered.connect(self._on_calibrate_speed)" in SRC
      and "duplex_cap_action.triggered.connect(self._on_printer_duplex_capability)" in SRC)
check("外部打印监视线程仍按配置延迟恢复（不被搬走而丢）",
      "QTimer.singleShot(1200, self._restore_external_watch)" in SRC)

print("\n=== ③ 默认任务参数对话框：默认值就是「旧行为」 ===")
dlg = _gui.DefaultTaskOptionsDialog(win, options={}, paper_size="", theme_manager=None)
v = dlg.values()
check("默认 A4", v["paper"] == "A4", v["paper"])
check("默认双面 + 长边翻转", v["duplex"] == "on" and v["duplex_mode"] == "long-edge")
check("默认四边页边距 1cm", [v["margin_top_cm"], v["margin_bottom_cm"],
                             v["margin_left_cm"], v["margin_right_cm"]] == [1.0] * 4, str(v))
check("默认页眉页脚 0", v["header_cm"] == 0.0 and v["footer_cm"] == 0.0)
check("默认纵向", v["orientation"] == "portrait", v["orientation"])
check("默认水平居中开、垂直居中关", v["center_h"] is True and v["center_v"] is False)
check("默认「将所有列调整为一页」", v["fit"] == "cols", v["fit"])
check("默认打印范围 = 活动工作表", v["print_range"] == "active", v["print_range"])
check("默认跟随全局 DPI / 自动图片方向 / 默认排除末尾空白页 / 不启用封面单面",
      v["dpi"] == 0 and v["image_orientation"] == "auto"
      and v["blank_tail"] is True and v["simplex_enabled"] is False)
check("纸张下拉是 A4/A3/A5/Letter/Legal/B5 六档", list(pc.PAPER_SIZES) ==
      ["A4", "A3", "A5", "Letter", "Legal", "B5"], str(pc.PAPER_SIZES))
check("对话框里所有复选框都是 ThemedCheckBox",
      all(isinstance(c, _gui.ThemedCheckBox) for c in
          (dlg._center_h, dlg._center_v, dlg._blank_tail, dlg._simplex)))
check("对话框文案没有 markdown 星号",
      not any("**" in w.text() for w in dlg.findChildren(_gui.QLabel)) and
      not any("**" in w.toolTip() for w in dlg.findChildren(_gui.QLabel)))
check("自定义缩放只在「无缩放」时可用（其余档位百分比无意义）",
      (dlg._fit.setCurrentIndex(0) or True) and dlg._scale.isEnabled())
dlg._fit.setCurrentIndex(1)      # 将工作表调整为一页
check("切到「调整为一页」⇒ 自定义缩放被灰掉", not dlg._scale.isEnabled())
dlg._fit.setCurrentIndex(0)
check("切回「无缩放」⇒ 自定义缩放恢复可用", dlg._scale.isEnabled())
dlg._paper.setCurrentText("A3")
dlg._margin_spins["margin_top_cm"].setValue(2.5)
v2 = dlg.values()
check("改纸张与页边距能读出来（保存的是控件值，不是初始值）",
      v2["paper"] == "A3" and abs(v2["margin_top_cm"] - 2.5) < 1e-6, str(v2["paper"]))
dlg._on_reset()
check("「恢复默认」把控件退回默认值", dlg.values()["paper"] == "A4"
      and abs(dlg.values()["margin_top_cm"] - 1.0) < 1e-6)

print("\n=== ③b 页边距/页眉/页脚在主题 QSS 下真的能看见（2026-11-06 用户现场回归） ===")
# 用户照片：「默认任务参数」窗口的页边距/页眉/页脚六格整片空白，问「这里貌似卡了？」。
# 根因不是卡死，是 Qt 的 QStyleSheetStyle：应用了主题 QSS + buttonSymbols=NoButtons 时，
# CC_SpinBox/SC_SpinBoxEditField 只返回 1px，lineEdit 跟着 1px —— 值都在控件里，就是画不出。
# 修法：保留 UpDownArrows，用控件级 QSS 把按钮压成 0 宽（gui._hide_spin_buttons）。
from PySide6.QtWidgets import QStyle, QStyleOptionSpinBox  # noqa: E402


def _edit_field_width(sp):
    opt = QStyleOptionSpinBox()
    opt.initFrom(sp)
    opt.buttonSymbols = sp.buttonSymbols()
    return sp.style().subControlRect(QStyle.CC_SpinBox, opt,
                                     QStyle.SC_SpinBoxEditField, sp).width()


_prev_qss = app.styleSheet()
app.setStyleSheet(io.open(os.path.join(LOCAL_TOOL, "styles_dark.qss"),
                          encoding="utf-8").read())
dlg.show()
QApplication.processEvents()
_fields = [dlg._margin_spins[k] for k in ("margin_top_cm", "margin_bottom_cm",
                                          "margin_left_cm", "margin_right_cm")]
_fields += [dlg._header, dlg._footer]
check("六个 cm 输入框在主题 QSS 下编辑区真有宽度（≥40px；NoButtons 时只有 1px）",
      all(_edit_field_width(sp) >= 40 and sp.lineEdit().width() >= 40 for sp in _fields),
      str([(_edit_field_width(sp), sp.lineEdit().width()) for sp in _fields]))
# 变异自证：把同一只控件切回 NoButtons，编辑区必须塌成 1px —— 判据不是恒真。
_fields[0].setButtonSymbols(_gui.QAbstractSpinBox.NoButtons)
QApplication.processEvents()
_bad_edit_w = _edit_field_width(_fields[0])
_fields[0].setButtonSymbols(_gui.QAbstractSpinBox.UpDownArrows)
app.setStyleSheet(_prev_qss)
QApplication.processEvents()
check("变异自证：切回 NoButtons 时编辑区塌成 1px（主题 QSS 下数值完全看不见）",
      _bad_edit_w <= 1, f"edit={_bad_edit_w}")
dlg.close()

print("\n=== ④ 保存链路：写进 print_config.json 并能读回 ===")
_orig_dlg = _gui.DefaultTaskOptionsDialog


class _FakeDlg:
    """模态对话框不能真弹（offscreen 下 exec 会挂住）→ 用桩直接给返回值。"""

    def __init__(self, *a, **k):
        self.values_d = dict(pc.DEFAULT_TASK_OPTIONS, paper="A3", orientation="landscape",
                             margin_left_cm=2.0, fit="sheet")

    def exec(self):
        return QDialog.Accepted

    def values(self):
        return self.values_d


_gui.DefaultTaskOptionsDialog = _FakeDlg
try:
    win._on_default_task_options()
finally:
    _gui.DefaultTaskOptionsDialog = _orig_dlg

cfg = win._config
check("配置里 paper_size 跟着对话框选了 A3", cfg.paper_size == "A3", cfg.paper_size)
check("default_task_options 落盘为归一后的完整 dict",
      cfg.task_options()["orientation"] == "landscape"
      and abs(cfg.task_options()["margin_left_cm"] - 2.0) < 1e-6, str(cfg.task_options()))
check("task_options_customized() 现在为 True（配过了）", cfg.task_options_customized() is True)

_win2 = MainWindow(config_path=paths.config_path())
check("重启后仍是 A3 + 横向（真的写进了 print_config.json）",
      _win2._config.paper_size == "A3"
      and _win2._config.task_options()["orientation"] == "landscape",
      _win2._config.paper_size)
_win2._cloud_client = None
_win2.close()

print("\n=== ⑤ 旧配置兼容：缺字段 ⇒ 默认值，且行为与改动前一致 ===")
fresh = pc.PrinterConfig.from_dict({})
check("旧配置（空 dict）⇒ paper_size A4", fresh.paper_size == "A4", fresh.paper_size)
check("旧配置 ⇒ 没配过默认任务参数（xlsx 不接管版式 = 旧行为）",
      fresh.task_options_customized() is False and fresh.xlsx_print_options() is None)
_opts = fresh.task_options()
check("旧配置 ⇒ 边距 1cm / 页眉页脚 0 / 水平居中 / 所有列一页 / 纵向 / 双面",
      _opts["duplex"] == "on" and _opts["duplex_mode"] == "long-edge"
      and [_opts["margin_top_cm"], _opts["margin_bottom_cm"],
           _opts["margin_left_cm"], _opts["margin_right_cm"]] == [1.0] * 4
      and _opts["header_cm"] == 0.0 and _opts["footer_cm"] == 0.0
      and _opts["center_h"] is True and _opts["center_v"] is False
      and _opts["fit"] == "cols" and _opts["orientation"] == "portrait")
check("纸张大小坏值/缺失都回落到 A4",
      pc.normalize_paper_size("???") == "A4" and pc.normalize_paper_size(None) == "A4"
      and pc.normalize_paper_size("a3") == "A3")
check("to_dict → from_dict 往返不掉字段",
      pc.PrinterConfig.from_dict(fresh.to_dict()).to_dict() == fresh.to_dict())

print("\n=== ⑥ 纸张贯通：只对表格与图片生效，Word/PDF 用文档自带页面设置 ===")
check("PrintWorker 收到 paper 参数，出纸前按整批文件类型门控",
      "paper: str = \"\"" in SRC
      and "def _paper_for(self, jobs)" in SRC
      and "effective_paper(self._paper, paths)" in SRC
      and SRC.count("paper=self._paper_for(") == 3,
      f"paper=self._paper_for( ×{SRC.count('paper=self._paper_for(')}")
check("构造 PrintWorker / DuplexFlushWorker 时传 config.paper_size",
      "paper=self._config.paper_size" in SRC)
check("表格引擎的 print_options 也带 paper（同一默认值）",
      '"paper": o["paper"]' in io.open(os.path.join(LOCAL_TOOL, "printer_config.py"),
                                       encoding="utf-8").read())
check("页面设置只认表格与图片（Word/PDF 不吃全局纸张）",
      pc.page_setup_applies("a.xlsx") and pc.page_setup_applies("b.XLS")
      and pc.page_setup_applies("c.JPG") and pc.page_setup_applies("d.png")
      and not pc.page_setup_applies("e.docx") and not pc.page_setup_applies("f.pdf")
      and not pc.page_setup_applies(""))
check("整批判：混合批次走驱动默认纸",
      pc.effective_paper("A3", ["a.xlsx", "b.jpg"]) == "A3"
      and pc.effective_paper("A3", ["a.xlsx", "b.docx"]) == ""
      and pc.effective_paper("A3", ["a.pdf"]) == ""
      and pc.effective_paper("", ["a.xlsx"]) == ""
      and pc.effective_paper("A3", []) == "",
      f"{pc.effective_paper('A3', ['a.xlsx', 'b.jpg'])!r}/"
      f"{pc.effective_paper('A3', ['a.xlsx', 'b.docx'])!r}")
check("默认任务参数窗口写明页面设置只对表格与图片生效",
      "仅对表格与图片生效" in SRC)

win._cloud_client = None
win.close()

print("\n=== ⑦ 默认任务参数作用到新增任务（差量存储：没配过的键仍按原逻辑推导）===")
import fitz  # noqa: E402  （与 gui 读页数/末尾空白页同一条路径）


def _make_pdf(name, pages=2, blank_last=False):
    _p = os.path.join(_SANDBOX, name)
    _doc = fitz.open()
    for _i in range(pages):
        _pg = _doc.new_page()
        if blank_last and _i == pages - 1:
            continue                    # 末页留空 ⇒ 检测出 1 页末尾空白
        _pg.insert_text((72, 72), f"page {_i + 1}", fontsize=12)  # 有字 ⇒ 不是空白页
    _doc.save(_p)
    _doc.close()
    return _p


_pdf1 = _make_pdf("opts_plain.pdf")
_pdf2 = _make_pdf("opts_custom.pdf")

# 配置路径必须显式指向沙箱：MainWindow 默认参数是相对路径 "print_config.json"，
# 不传就会读写 cwd（local_print_tool/）里的真实开发配置 —— 之前正是这里被写脏的。
_win3 = MainWindow(config_path=os.path.join(_SANDBOX, "opts_menu_win3.json"))
_win3._cloud_client = None
_win3._queue_font_checks = lambda *a, **k: None       # 别真去数字体（要开 Word/字体服务）
# 显式回到"从未配置"状态（开发机 cwd 下可能留着上次的 print_config.json，不能靠环境干净）
_win3._config.default_task_options = {}
_win3._config.paper_size = "A4"
_add_impl = getattr(_win3, "_MainWindow__add_files_to_table_impl")

# —— 没配过默认参数：新增任务逐项等于「改动前」的行为 ——
_detected_tail = _win3._detect_blank_tail(_pdf1)
_add_impl([_pdf1], None)
_j1 = _win3._get_current_jobs()[-1]
check("没配过 ⇒ 双面仍是 on（文本文件默认双面）", _j1.duplex == "on", _j1.duplex)
check("没配过 ⇒ 双面模式仍是按页面方向推导的值",
      _j1.duplex_mode == _win3._duplex_mode_for_orientation(_j1.orientation), _j1.duplex_mode)
check("没配过 ⇒ DPI 仍是 0（跟随全局）", _j1.dpi == 0, str(_j1.dpi))
check("没配过 ⇒ 图片方向仍是 auto", _j1.image_orientation == "auto", _j1.image_orientation)
check("没配过 ⇒ 排除末尾空白页仍是自动检测结果", _j1.blank_tail == _detected_tail,
      f"{_j1.blank_tail} vs {_detected_tail}")
check("没配过 ⇒ 封面单独打印仍是关", bool(_j1.simplex_enabled) is False, str(_j1.simplex_enabled))

# 2026-11-13：本地与移动端一致，默认开启「排除末尾空白页」——不是只把 blank_tail 写成 1
# （那会让它显示「排除末尾空白页（1 页）」却没真排），而是加文件当场把 page_range 裁掉。
_pdf3 = _make_pdf("opts_blank_tail_on.pdf", pages=3, blank_last=True)
_add_impl([_pdf3], None)
_j1b = _win3._get_current_jobs()[-1]
check("没配过 ⇒ 默认开启：末尾空白页已自动排掉（落到 page_range）",
      _j1b.blank_tail == 1 and _j1b.page_range == "1-2"
      and _j1b.blank_undo_range == "",
      f"tail={_j1b.blank_tail} range={_j1b.page_range!r} undo={_j1b.blank_undo_range!r}")

# —— 配过默认参数：只覆盖配过的键 ——
_win3._config.default_task_options = {"dpi": 300, "duplex": "off", "duplex_mode": "short-edge",
                                      "blank_tail": False, "image_orientation": "landscape",
                                      "simplex_enabled": True, "simplex_pages": "1"}
_add_impl([_pdf2], None)
_j2 = _win3._get_current_jobs()[-1]
check("配过 DPI 300 ⇒ 新任务带 300", _j2.dpi == 300, str(_j2.dpi))
check("配过单面 ⇒ 新任务 duplex=off", _j2.duplex == "off", _j2.duplex)
check("配过双面模式 short-edge ⇒ 新任务跟着走", _j2.duplex_mode == "short-edge", _j2.duplex_mode)
# 默认值改成「开启」后，这条只剩「取消勾选」有信息量：不自动排，只按检测结果标 ⚠。
_pdf4 = _make_pdf("opts_blank_tail_off.pdf", pages=3, blank_last=True)
_add_impl([_pdf4], None)
_j2b = _win3._get_current_jobs()[-1]
check("取消勾选默认排除 ⇒ 新任务只标 ⚠、不改 page_range",
      _j2b.blank_tail == 1 and _j2b.page_range == "" and _j2b.blank_undo_range is None,
      f"tail={_j2b.blank_tail} range={_j2b.page_range!r} undo={_j2b.blank_undo_range!r}")
check("配过图片方向 landscape ⇒ 新任务跟着走", _j2.image_orientation == "landscape")
check("配过封面单独打印 + 页 1 ⇒ 新任务 simplex 开且带页范围",
      bool(_j2.simplex_enabled) is True and _j2.simplex_pages == "1",
      f"{_j2.simplex_enabled}/{_j2.simplex_pages}")

# —— 图片仍然强制单面（既有硬规矩，默认参数不得改写）——
_png = os.path.join(_SANDBOX, "opts_img.png")
from PySide6.QtGui import QImage  # noqa: E402
_qi = QImage(40, 40, QImage.Format_RGB32)
_qi.fill(0xFFFFFF)
_qi.save(_png)
_win3._config.default_task_options = {"duplex": "on"}   # 就算默认写成双面
_add_impl([_png], None)
_j3 = _win3._get_current_jobs()[-1]
check("图片一律单面：默认参数写「双面」也不改图片", _j3.duplex == "off", _j3.duplex)

# —— 落盘稀疏：只存与出厂默认不同的键 ——
_win3._config.default_task_options = {}
_win3._config.paper_size = "A4"


class _FakeDlg2:
    def __init__(self, *a, **k):
        pass

    def exec(self):
        return QDialog.Accepted

    def values(self):
        _v = dict(pc.DEFAULT_TASK_OPTIONS)
        _v["dpi"] = 400                      # 只改了一项
        return pc.normalize_task_options(_v)


_orig_dlg2 = _gui.DefaultTaskOptionsDialog
_gui.DefaultTaskOptionsDialog = _FakeDlg2
try:
    _win3._on_default_task_options()
finally:
    _gui.DefaultTaskOptionsDialog = _orig_dlg2
_stored = dict(_win3._config.default_task_options)
check("只改一项 ⇒ 落盘只存这一项（差量存储）", _stored == {"dpi": 400}, str(_stored))
check("差量存储后 resolve 仍得到完整默认值",
      _win3._config.task_options()["dpi"] == 400
      and _win3._config.task_options()["fit"] == "cols", str(_win3._config.task_options()))
check("整份都是默认值 ⇒ 存成空 dict（= 从未配置 = 旧行为）",
      "self._config.default_task_options = {\n"
      "                k: v for k, v in vals.items() if DEFAULT_TASK_OPTIONS.get(k) != v"
      in io.open(os.path.join(LOCAL_TOOL, "gui.py"), encoding="utf-8").read())

_win3._cloud_client = None
_win3.close()

print("\n" + "=" * 78)
print("选项菜单与默认任务参数验收:", "全部通过 ✅" if ok_all else "存在失败 ❌")
sys.exit(0 if ok_all else 1)
