# -*- coding: utf-8 -*-
"""验收脚本 · xlsx「表格版式」右侧面板 + 转换透传（task-4 #10，offscreen）

需求（用户 2026-11）：
1. 选中表格（.xls/.xlsx）时，右侧「双面模式」下方出现新模块，上下各一条分隔线。
2. 模块含：页边距（上/下/左/右 cm）、页眉/页脚 cm、纸张方向、纸张大小、水平/垂直居中、
   缩放（无缩放 / 调整为一页 / 所有列一页 / 所有行一页 + 自定义百分比）、打印范围四档。
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
from PySide6.QtWidgets import QApplication, QFrame  # noqa: E402

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
check("打印范围四档（活动工作表/整个工作簿/选定区域/忽略打印区域）",
      win._xlsx_range.count() == 4
      and win._xlsx_range.itemText(0) == "活动工作表"
      and win._xlsx_range.itemText(3) == "忽略打印区域")
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
check("写进去的正好是引擎认的那 13 个键", set(opts) == set(XLSX_OPTION_KEYS), str(sorted(opts)))
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

win._cloud_client = None
win.close()

print("\n" + "=" * 78)
print("表格版式面板验收:", "全部通过 ✅" if ok_all else "存在失败 ❌")
sys.exit(0 if ok_all else 1)
