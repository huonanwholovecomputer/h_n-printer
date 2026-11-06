# -*- coding: utf-8 -*-
"""验收脚本 · xlsx「表格版式」右侧面板 + 转换透传（task-4 #10，offscreen）

需求（用户 2026-11）：
1. 选中表格（.xls/.xlsx）时，右侧「双面模式」下方出现新模块，上下各一条分隔线。
2. 模块含：页边距（上/下/左/右 cm）、页眉/页脚 cm、纸张方向、纸张大小、水平/垂直居中、
   缩放（无缩放 / 调整为一页 / 所有列一页 / 所有行一页 + 自定义百分比）、打印范围四档
   （2026-11-06 按用户要求加回「选定区域」：Excel 的选区是会话内状态、无人值守 COM 拿不到，
   所以地址由调用方显式给出 —— 面板输入框或小印接口 xlsx_options.selection）。
3. 值存 `PrintJob.xlsx_options`（持久化、可复制）；面板只收集参数，转换时才传给引擎。
4. 多选按**整片选区**规则启停：混合选区里只写表格任务；灰掉（冻结）时一律不写回。
5. 传引擎的键名必须与 task-5 契约逐字一致（print_options）。

    python tests/verify_xlsx_panel.py     # exit 0 = 全绿
"""
import io
import os
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LOCAL_TOOL = os.path.join(REPO, "local_print_tool")

_SANDBOX = tempfile.mkdtemp(prefix="hn_xlsxpanel_")
os.environ["APPDATA"] = _SANDBOX                 # 用户数据目录 → 沙箱（别碰真实配置）
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, LOCAL_TOOL)
os.chdir(LOCAL_TOOL)

import paths  # noqa: E402
from PySide6.QtCore import QPoint, QPointF, Qt  # noqa: E402
from PySide6.QtGui import QWheelEvent  # noqa: E402
from PySide6.QtWidgets import (QApplication, QFrame, QFormLayout,  # noqa: E402
                               QScrollArea, QAbstractSpinBox)

app = QApplication.instance() or QApplication([])

import converter as _converter  # noqa: E402
_converter.get_available_engines = lambda: {"word": True, "wps": True, "libreoffice": True}
import gui as _gui  # noqa: E402

_gui.QMessageBox.question = staticmethod(lambda *a, **k: _gui.QMessageBox.Yes)
_gui.QMessageBox.warning = staticmethod(lambda *a, **k: _gui.QMessageBox.Ok)
_gui.QMessageBox.information = staticmethod(lambda *a, **k: _gui.QMessageBox.Ok)

import xlsx_printer as xp  # noqa: E402
from gui import MainWindow  # noqa: E402
from printer_config import PrintJob, XLSX_OPTION_KEYS, resolve_xlsx_print_options  # noqa: E402

SRC = io.open(os.path.join(LOCAL_TOOL, "gui.py"), encoding="utf-8").read()
ok_all = True


def check(name, cond, extra=""):
    global ok_all
    if not cond:
        ok_all = False
        print(f"❌ {name}" + (f"  [{extra}]" if extra else ""))
    else:
        print(f"✅ {name}")


print("\n=== ① 面板结构：两条分隔线 + 指定控件 ===")
win = MainWindow(config_path=paths.config_path())
tab_key = win._current_tab
tab = win._config.tabs[tab_key]
tab.jobs = [
    PrintJob(file_path=r"C:\tmp\a.xlsx", page_count=3),
    PrintJob(file_path=r"C:\tmp\b.docx", page_count=2),
    PrintJob(file_path=r"C:\tmp\c.pdf", page_count=2),
]
win._set_current_jobs(tab.jobs)
win._rebuild_table()

check("有上下两条 QFrame HLine 分隔线",
      isinstance(win._xlsx_sep_top, QFrame) and isinstance(win._xlsx_sep_bottom, QFrame)
      and win._xlsx_sep_top.frameShape() == QFrame.HLine
      and win._xlsx_sep_bottom.frameShape() == QFrame.HLine)
_gl = win._xlsx_box.parentWidget().layout()
_idx_top = _gl.indexOf(win._xlsx_sep_top)
_idx_box = _gl.indexOf(win._xlsx_box)
_idx_bot = _gl.indexOf(win._xlsx_sep_bottom)
check("顺序是 上分隔线 → 面板 → 下分隔线", _idx_top < _idx_box < _idx_bot,
      f"{_idx_top}/{_idx_box}/{_idx_bot}")
check("四边页边距 + 页眉页脚 六个 cm 输入都在",
      set(win._xlsx_margins) == {"margin_top_cm", "margin_bottom_cm",
                                 "margin_left_cm", "margin_right_cm"}
      and win._xlsx_header is not None and win._xlsx_footer is not None)
check("纸张方向 / 纸张大小 两个下拉在",
      win._xlsx_orientation.count() == 2 and win._xlsx_paper.count() == 6)
check("水平/垂直居中都是 ThemedCheckBox",
      isinstance(win._xlsx_center_h, _gui.ThemedCheckBox)
      and isinstance(win._xlsx_center_v, _gui.ThemedCheckBox))
check("缩放是四档下拉 + 自定义百分比输入",
      win._xlsx_fit.count() == 4 and win._xlsx_scale.minimum() == 10
      and win._xlsx_scale.maximum() == 400)
check("打印范围四档（活动工作表/整个工作簿/忽略打印区域/选定区域）",
      win._xlsx_range.count() == 4
      and win._xlsx_range.itemText(0) == "活动工作表"
      and win._xlsx_range.itemText(2) == "忽略打印区域"
      and win._xlsx_range.itemText(3) == "选定区域")
check("面板文案没有 markdown 星号",
      not any("**" in w.text() for w in win._xlsx_box.findChildren(_gui.QLabel))
      and not any("**" in w.toolTip() for w in win._xlsx_box.findChildren(_gui.QLabel)))

print("\n=== ② 显隐：只有选中表格才出现 ===")
win._table.selectRow(1)                     # docx
win._on_table_selection()
check("选中 docx ⇒ 面板不出现", win._xlsx_panel_shown is False and win._xlsx_box.isHidden())
win._table.selectRow(2)                     # pdf
win._on_table_selection()
check("选中 pdf ⇒ 面板不出现", win._xlsx_panel_shown is False and win._xlsx_box.isHidden())
win._table.selectRow(0)                     # xlsx
win._on_table_selection()
check("选中 xlsx ⇒ 面板出现", win._xlsx_panel_shown is True and not win._xlsx_box.isHidden())
check("打印范围默认「活动工作表」⇒ 选定区域地址框置灰",
      isinstance(win._xlsx_selection, _gui.QLineEdit)
      and not win._xlsx_selection.isEnabled())
win._xlsx_range.setCurrentText("选定区域")
check("切到「选定区域」⇒ 地址框可填", win._xlsx_selection.isEnabled())
check("切档立即写回任务（print_range=selection）",
      tab.jobs[0].xlsx_options.get("print_range") == "selection")
win._xlsx_selection.setText("A1:B2")
win._xlsx_selection.editingFinished.emit()
check("填地址（失焦提交）⇒ 写进 xlsx_options.selection",
      (tab.jobs[0].xlsx_options or {}).get("selection") == "A1:B2")
win._xlsx_range.setCurrentText("活动工作表")
check("切回其它档 ⇒ selection 清空（别让残留地址进缓存 key）",
      not (tab.jobs[0].xlsx_options or {}).get("selection"))
win._clear_edit_panel()
check("无选区 ⇒ 面板收起（不残留上一个任务的版式）",
      win._xlsx_panel_shown is False and win._xlsx_box.isHidden())

print("\n=== ③ 回写：只写表格任务，值进 PrintJob.xlsx_options ===")
win._table.selectRow(0)
win._on_table_selection()
check("面板初值 = 全局默认（A4/1cm/所有列一页/水平居中）",
      win._xlsx_paper.currentText() == "A4"
      and abs(win._xlsx_margins["margin_top_cm"].value() - 1.0) < 1e-6
      and win._xlsx_fit.currentText() == "将所有列调整为一页"
      and win._xlsx_center_h.isChecked() is True)
check("没改任何控件 ⇒ 任务不落个性化版式（仍是跟随默认）",
      tab.jobs[0].xlsx_options == {}, str(tab.jobs[0].xlsx_options))

tab.jobs[0].cached_pdf = r"C:\cache\old.pdf"
win._xlsx_margins["margin_top_cm"].setValue(2.5)
opts = tab.jobs[0].xlsx_options
check("改页边距 ⇒ 写进 xlsx_options", abs(opts.get("margin_top_cm", 0) - 2.5) < 1e-6, str(opts))
check("写进去的正好是引擎认的那套键", set(opts) == set(XLSX_OPTION_KEYS), str(sorted(opts)))
check("版式变了 ⇒ 旧缓存 PDF 作废（下次打印重转）", tab.jobs[0].cached_pdf == "",
      tab.jobs[0].cached_pdf)
check("非表格任务没被牵连", tab.jobs[1].xlsx_options == {} and tab.jobs[2].xlsx_options == {})

print("\n=== ④ 多选：整片选区一起生效，但只作用于表格 ===")
win._table.clearSelection()
_sm = win._table.selectionModel()
from PySide6.QtCore import QItemSelectionModel  # noqa: E402
_model = win._table.model()
for _r in (0, 1):
    _sm.select(_model.index(_r, 0),
               QItemSelectionModel.Select | QItemSelectionModel.Rows)
win._on_table_selection()
check("混选（xlsx + docx）⇒ 面板仍出现", win._xlsx_panel_shown is True)
win._xlsx_paper.setCurrentText("A3")
check("改纸张 ⇒ 表格任务跟着变", tab.jobs[0].xlsx_options.get("paper") == "A3",
      str(tab.jobs[0].xlsx_options.get("paper")))
check("docx 任务不被写版式", tab.jobs[1].xlsx_options == {}, str(tab.jobs[1].xlsx_options))

print("\n=== ⑤ 冻结标签页：面板灰掉且不回写（灰控件不回写是硬规矩） ===")
tab.frozen = True
_before = dict(tab.jobs[0].xlsx_options)
win._on_table_selection()
check("冻结后面板控件是灰的（整块禁用）",
      all(not w.isEnabled() for w in win._xlsx_widgets if w is not win._label_xlsx),
      str([type(w).__name__ for w in win._xlsx_widgets if w.isEnabled()]))
win._xlsx_margins["margin_left_cm"].setValue(5.0)
check("冻结后改控件不写回任务", dict(tab.jobs[0].xlsx_options) == _before,
      str(tab.jobs[0].xlsx_options))
tab.frozen = False

print("\n=== ⑥ 跟随默认 / 持久化 ===")
win._table.selectRow(0)
win._on_table_selection()
win._on_xlsx_follow_default()
check("「跟随默认」清空任务版式", tab.jobs[0].xlsx_options == {}, str(tab.jobs[0].xlsx_options))
win._xlsx_fit.setCurrentText("将所有行调整为一页")
check("切到「所有行一页」⇒ 自定义缩放被灰掉", not win._xlsx_scale.isEnabled())
win._xlsx_fit.setCurrentText("无缩放")
check("切到「无缩放」⇒ 自定义缩放可用", win._xlsx_scale.isEnabled())
win._xlsx_scale.setValue(80)
win._save_config()

_win2 = MainWindow(config_path=paths.config_path())
_j2 = _win2._config.tabs[win._current_tab].jobs[0]
check("xlsx_options 落盘并读回（重启不丢）",
      _j2.xlsx_options.get("fit") == "none" and _j2.xlsx_options.get("scale") == 80,
      str(_j2.xlsx_options))
_win2._cloud_client = None
_win2.close()

print("\n=== ⑦ 转换透传：面板值 → ConvertWorker → xlsx_printer.export_to_pdf ===")
check("ConvertWorker 收版式并转交引擎",
      "def set_print_options(self, print_options)" in SRC
      and "worker.set_print_options(_print_options)" in SRC
      and "print_options=self._print_options" in SRC)
# 源码里跨了两行：`_xp.cache_key(self._source_md5, "", _res.engine,` + 换行 + `self._print_options)`
_norm_src = " ".join(SRC.split())
check("缓存 key 带上版式签名（改版式不会命中旧 PDF）",
      "_xp.cache_key(self._source_md5, \"\", _res.engine, self._print_options)" in _norm_src)
check("读缓存也带版式（否则改完页边距拿到旧版式的那份）",
      "_xp.find_cached_pdf(source_md5, image_orientation," in SRC
      and "print_options=_print_options" in SRC.split("_xp.find_cached_pdf")[1][:200])
check("只对表格文件解析版式（docx/pdf 不受影响）",
      'if _ext_of_file in (".xls", ".xlsx"):' in SRC
      and "resolve_xlsx_print_options(" in SRC)
# 「PageSetup」只允许出现在注释里（解释分工），代码里不许有真调用：判据看属性访问写法
check("面板只收集参数：代码里没有任何 PageSetup 属性访问（改版式交给引擎）",
      ".PageSetup" not in SRC and "PageSetup =" not in SRC)

print("\n=== ⑧ 与 xlsx 引擎接口对齐（键名逐个对齐 task-5 契约） ===")
_keys = set(resolve_xlsx_print_options({}, {"margin_top_cm": 2.0}) or {})
check("print_options 键名与 XLSX_OPTION_KEYS 完全一致", _keys == set(XLSX_OPTION_KEYS),
      str(sorted(_keys)))
_norm = xp.normalize_print_options({k: 1 for k in XLSX_OPTION_KEYS})
check("引擎的 normalize_print_options 接受这套键（不丢弃）",
      isinstance(_norm, dict) and set(_norm) == set(XLSX_OPTION_KEYS), str(sorted(_norm or {})))
check("引擎侧签名确实收 print_options（export_to_pdf / find_cached_pdf / cache_key）",
      "print_options=None" in io.open(os.path.join(LOCAL_TOOL, "xlsx_printer.py"),
                                      encoding="utf-8").read())
check("两处都没配过 ⇒ None ⇒ 引擎完全不碰版式（老用户行为不变）",
      resolve_xlsx_print_options({}, {}) is None)
# 2026-11-06：按用户要求把「选定区域」加回（小印智能助手给地址）。语义与 11-03 删它时
# 不同：任务级 selection **不再被悄悄降级成 active** —— 地址缺失/写错时引擎会明确记
# 「PrintArea 未生效」并保留文件原有打印区域，不会替用户拿 UsedRange 顶替。
check("任务级 print_range=selection 原样保留（不再降级成 active）",
      (resolve_xlsx_print_options({}, {"print_range": "selection"}) or {})
      .get("print_range") == "selection",
      str(resolve_xlsx_print_options({}, {"print_range": "selection"})))
check("带地址时地址原样进 print_options",
      (resolve_xlsx_print_options({}, {"print_range": "selection",
                                       "selection": "A1:F20"}) or {})
      .get("selection") == "A1:F20")
check("「选定区域」只在表格面板（全局默认参数仍是三档），引擎白名单同步",
      "selection" in xp.PRINT_RANGES and "selection" not in _gui.RANGE_VALUES
      and "selection" in _gui.XLSX_RANGE_VALUES
      and "selection" in xp.DEFAULT_PRINT_OPTIONS)

print("\n=== ⑨ 纯输入框 + 不再撑宽面板（2026-11-03 用户要求） ===")
# 用户原话：「上下箭头删掉吧，纯输入框即可。以及注意表格版式（仅表格文件）这个模块的最小
# 宽度是不是没有和其他的模块保持一致，发生了板面剪切。」两条都要钉住：
#   ① 七个数字控件不许画上下箭头（数值校验/两位小数/单位后缀仍由 QAbstractSpinBox 保证）；
#   ② 模块的出现不许抬升右侧参数面板的最小宽度（旧的两列式 QFormLayout 让它独占 248px）。
_spins = ([win._xlsx_margins[k] for k in ("margin_top_cm", "margin_bottom_cm",
                                          "margin_left_cm", "margin_right_cm")]
          + [win._xlsx_header, win._xlsx_footer, win._xlsx_scale])
check("七个数控控件都去掉了上下箭头（NoButtons）",
      all(sp.buttonSymbols() == QAbstractSpinBox.NoButtons for sp in _spins),
      str([sp.buttonSymbols() for sp in _spins]))
check("仍然不是裸输入框：范围/两位小数/单位后缀都还在",
      win._xlsx_margins["margin_top_cm"].decimals() == 2
      and win._xlsx_margins["margin_top_cm"].suffix() == " cm"
      and win._xlsx_margins["margin_top_cm"].minimum() == 0.0
      and win._xlsx_margins["margin_top_cm"].maximum() == 20.0
      and win._xlsx_scale.suffix() == " %" and win._xlsx_scale.minimum() == 10
      and win._xlsx_scale.maximum() == 400)
check("标签在上、控件在下（WrapAllRows，与份数/单双面等模块同一排版）",
      any(fl.rowWrapPolicy() == QFormLayout.WrapAllRows
          for fl in win._xlsx_box.findChildren(QFormLayout)))
_sp_top = win._xlsx_margins["margin_top_cm"]
_v0 = _sp_top.value()
QApplication.sendEvent(
    _sp_top,
    QWheelEvent(QPointF(6, 6), QPointF(_sp_top.mapToGlobal(QPoint(6, 6))),
                QPoint(0, 0), QPoint(0, 120), Qt.NoButton, Qt.NoModifier,
                Qt.NoScrollPhase, False))
check("滚轮压在页边距输入框上不改数值（转发给面板滚动）", _sp_top.value() == _v0,
      f"{_v0} -> {_sp_top.value()}")
_sw = win._xlsx_box
while _sw is not None and not isinstance(_sw, QScrollArea):
    _sw = _sw.parentWidget()
check("表格版式面板挂在 QScrollArea 里（下面的宽度判据才有意义）", _sw is not None)
win.show()
QApplication.processEvents()
_content = _sw.widget()
# 判据 1：模块自己的最小宽度要和面板里其它模块同量级（最宽的其它控件是 DPI 下拉 126）。
# 旧的两列式 + 带箭头 + 长文案下拉是 166/248，比其它模块都宽 ⇒ 面板一窄就裁它。
_other_w = max(win._edit_dpi.minimumSizeHint().width(),
               win._edit_engine.minimumSizeHint().width())
check("模块最小宽度与面板其它模块同量级（≤ 最宽者 +8）",
      win._xlsx_box.minimumSizeHint().width() <= _other_w + 8,
      f"模块 {win._xlsx_box.minimumSizeHint().width()} vs 其它最宽 {_other_w}")
# 判据 2：整块面板的最小宽度不再被这个模块顶到 248（旧口径）。真机（windows 平台 + 中文字体）
# 实测 148 = DPI 下拉 126 + 组框边距 22，比默认面板宽度 180 还窄 ⇒ 内容整块放得下、不出现
# 横向滚动条（用户现场「板面剪切」的直接症状）。offscreen 没有中文字体、度量偏大，所以这里
# 用 200 这个上限做回归闸门，真机具体值由下面的 extra 打出来。
check("面板最小宽度已降下来（≤ 200，旧口径 248；真机实测 148 < 默认面板宽 180）",
      _content.minimumSizeHint().width() <= 200,
      f"面板内容最小 {_content.minimumSizeHint().width()}（真机 148 / 旧口径 248）")

check("面板最小宽度不再由表格版式模块决定（= 其它模块的 148 量级）",
      _content.minimumSizeHint().width() <= _other_w + 22 + 8,
      f"面板内容最小 {_content.minimumSizeHint().width()}（旧口径 248）")

print("\\n=== ⑩ 面板拖到最窄也不出横向滚动条（2026-11-03 用户第二次现场） ===")
# 用户原话：「当任务参数面板缩小到最小时，底部会出现滚动条 … 让任务面板变为最小时，
# 不会出现滚动条。」旧代码把容器最小宽度砍到 minimumSizeHint 的一半，面板到最窄时
# 视口 134px < 内容 148px ⇒ 底部横向滚动条。现在最小宽度 = 内容最小宽度 + 边框/竖滚动条。
_panel = win._edit_panel
_edit_sw = win._edit_scroll
_check_sw = _panel.parentWidget()
check("面板的最小宽度给足了自身 chrome（内容最小宽度 + 边框/竖滚动条）",
      _panel.minimumWidth() >= _content.minimumSizeHint().width() + 12,
      f"面板最小 {_panel.minimumWidth()} vs 内容 {_content.minimumSizeHint().width()}")


def _shrink_panel_to_min():
    """把右侧面板拖到它自己的最小宽度（模拟用户拖到最窄）。"""
    _tot = sum(_check_sw.sizes())
    _check_sw.setSizes([max(1, _tot - _panel.minimumWidth()), _panel.minimumWidth()])
    QApplication.processEvents()


_shrink_panel_to_min()
check("面板拖到最窄后不出横向滚动条",
      not _edit_sw.horizontalScrollBar().isVisible(),
      f"视口 {_edit_sw.viewport().width()} / 内容 {_content.width()}")
check("面板拖到最窄后内容没被横向裁切（内容宽度 ≥ 其最小宽度）",
      _content.width() >= _content.minimumSizeHint().width(),
      f"内容 {_content.width()} vs 最小 {_content.minimumSizeHint().width()}")
# 变异自证：把最小宽度故意调小 30px，再看滚动条是否真的会冒出来（证明上面两条不是恒真）
_keep_min = _panel.minimumWidth()
_panel.setMinimumWidth(max(80, _keep_min - 30))
_shrink_panel_to_min()
_mutated_hbar = _edit_sw.horizontalScrollBar().isVisible()
_panel.setMinimumWidth(_keep_min)
_shrink_panel_to_min()
check("变异自证：最小宽度调小 30px 时横向滚动条确实会出现",
      _mutated_hbar and not _edit_sw.horizontalScrollBar().isVisible(),
      f"调小后 hbar={_mutated_hbar}，恢复后 hbar={_edit_sw.horizontalScrollBar().isVisible()}")


win._cloud_client = None
win.close()

print("\n" + "=" * 78)
print("表格版式面板验收:", "全部通过 ✅" if ok_all else "存在失败 ❌")
sys.exit(0 if ok_all else 1)
