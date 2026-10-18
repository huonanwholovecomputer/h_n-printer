# -*- coding: utf-8 -*-
"""生成 README 用的「本地打印工具」界面截图（真实 MainWindow + 真实 QSS，全部为模拟数据）。

为什么单独做一个生成器：README 里的界面图必须**随界面演进而更新**，而以前那张是手工截的
空列表窗口（既过时、又看不出功能）。这里用真实 MainWindow 渲染，灌一批**模拟任务**
（不碰任何真实配置：APPDATA 指向临时沙箱），随时可重放。

两个渲染要点：
  ① 用默认的 `windows` 平台 + `WA_DontShowOnScreen`（窗口不真正上屏），这样**中文字体与彩色
     emoji（☁ 📂 💰 ✅ ⏸）都能正常出字**；纯 `offscreen` 平台走的是 Qt 基础字体库、找不到
     字体目录时整屏都是豆腐块 □。
  ② 价格/成员名单/任务参数全部来自沙箱里的模拟配置（成员 张伟/李娜/王芳，单面 0.30、双面 0.40），
     与 README 别处的截图用同一套虚构数据。

用法：PYTHONPATH= PYTHONUTF8=1 <Python311> tools/readme_screenshots/gen_gui_shot.py [输出路径]
默认输出：docs/screenshots/local-print-tool.png
"""
import io
import json
import os
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True)

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LOCAL_TOOL = os.path.join(REPO, "local_print_tool")
OUT = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
    REPO, "docs", "screenshots", "local-print-tool.png")

# 用户数据目录 → 临时沙箱：绝不读写真实的 print_config.json / print_data.json
_SANDBOX = tempfile.mkdtemp(prefix="hn_shot_")
os.environ["APPDATA"] = _SANDBOX
# windows 平台才能拿到系统字体（含彩色 emoji）；窗口用 WA_DontShowOnScreen 不上屏。
# 若显式指定 offscreen，则至少要给出字体目录，否则整屏豆腐块。
os.environ.setdefault("QT_QPA_PLATFORM", "windows")
_FONT_DIR = os.environ.get("HN_SHOT_FONTDIR", r"C:\Windows\Fonts")
if os.environ.get("QT_QPA_PLATFORM") == "offscreen" and os.path.isdir(_FONT_DIR):
    os.environ.setdefault("QT_QPA_FONTDIR", _FONT_DIR)
sys.path.insert(0, LOCAL_TOOL)
os.chdir(LOCAL_TOOL)

import paths  # noqa: E402
from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

app = QApplication.instance() or QApplication([])

import converter as _converter  # noqa: E402
import gui as _gui  # noqa: E402
import theme_manager as _theme  # noqa: E402

# 引擎探测会真的 Dispatch Word/WPS COM（慢 + 会拉起 wps.exe 残留进程）→ 桩成"三引擎都可用"
_converter.get_available_engines = lambda: {"word": True, "wps": True, "libreoffice": True}
# 启动 4 秒后的自更新检查会联网 → 出图用不上，直接停掉
_gui.MainWindow._start_update_check = lambda self, manual=False: None
# 无人应答的模态框（离屏/不上屏都一样）
_gui.QMessageBox.question = staticmethod(lambda *a, **k: _gui.QMessageBox.Yes)
_gui.QMessageBox.warning = staticmethod(lambda *a, **k: _gui.QMessageBox.Ok)
_gui.QMessageBox.information = staticmethod(lambda *a, **k: _gui.QMessageBox.Ok)

from gui import MainWindow  # noqa: E402
from printer_config import PrintJob, TabSettings  # noqa: E402

import fitz  # noqa: E402  PyMuPDF：造真 PDF，页数由 app 自己读

# ── 模拟收支数据（成员名单）：归属下拉/「归属」标签都从这里取 ─────────────────────
_MEMBERS = [("张伟", "#4472C4"), ("李娜", "#ED7D31"), ("王芳", "#70AD47")]
os.makedirs(os.path.dirname(paths.finance_data_path()), exist_ok=True)
with open(paths.finance_data_path(), "w", encoding="utf-8") as f:
    json.dump({
        "config": {
            "members": [{"id": "m%d" % i, "name": n, "color": c}
                        for i, (n, c) in enumerate(_MEMBERS)],
            "passwords": {}, "investRate": 0.05, "scoreCap": 15,
        },
        "records": {"expenses": [], "income": [], "scores": [], "transfers": []},
        "notes": {}, "meta": {"version": 1},
    }, f, ensure_ascii=False)


def make_pdf(name: str, pages: int) -> str:
    p = os.path.join(_SANDBOX, name)
    doc = fitz.open()
    for _ in range(max(1, pages)):
        doc.new_page()
    doc.save(p)
    doc.close()
    return p


print("… 正在创建主窗口（不上屏）", flush=True)


def _qss(name: str) -> str:
    with open(os.path.join(LOCAL_TOOL, name), "r", encoding="utf-8") as f:
        return f.read()


# 主题管理器与 main.py 同款接法（styles_dark.qss / styles_light.qss），并先钉成深色：
# README 里这几张图统一深色，widgets 在构造时就按深色取色。
tm = _theme.ThemeManager(paths.theme_settings_path())
tm.init(app, _qss("styles_dark.qss"), _qss("styles_light.qss"))
tm.set_mode(_theme.MODE_DARK)

win = MainWindow(config_path=paths.config_path(), theme_manager=tm)
win.setAttribute(Qt.WA_DontShowOnScreen, True)     # 布局照常算，但不真的弹窗
win.resize(1500, 1010)
win.show()
app.processEvents()

# ── 模拟任务：8 个文件、混合单双面（费用由 app 自己按 0.30/0.40 算，不写死） ────────
SPECS = [
    ("高等数学作业1.pdf",   30, 5, "on",  "word"),
    ("实验报告模板.docx",   12, 2, "off", "word"),
    ("课件图表.png",         1, 3, "off", ""),
    ("报名统计表.xlsx",      7, 1, "on",  ""),
    ("离散数学作业2.pdf",   10, 5, "on",  "word"),
    ("社团招新海报.png",     1, 6, "off", ""),
    ("毕业论文初稿.docx",   48, 1, "on",  "wps"),
    ("会议纪要.txt",         3, 2, "off", ""),
    ("英语听力原文.pdf",    16, 2, "on",  "word"),
    ("课程设计说明书.docx", 22, 1, "on",  "word"),
    ("期末复习提纲.pdf",    36, 3, "on",  "word"),
    ("实验数据表.xlsx",      5, 4, "off", ""),
]
jobs = []
for i, (name, pages, copies, duplex, engine) in enumerate(SPECS):
    jobs.append(PrintJob(
        file_path=make_pdf(f"src{i}.pdf", pages),
        # source_path 只用于界面显示（“路径: …”那一行）：给一个像真订单的路径，
        # 免得 README 图上出现 %TEMP% 沙箱路径
        source_path=os.path.join("D:\\打印订单\\2026-10-18", name),
        copies=copies, duplex=duplex, page_count=pages, orientation="portrait",
        engine=engine, display_name=name, order_number="HN20261018-0007",
    ))

tab = TabSettings(
    jobs=jobs,
    delivery_enabled=True, delivery_location="1号楼北楼",
    cover_page=False, urgency="低",
    owner_name="张伟", is_admin_print=False,
    remark="宿舍 5 人合并打印，打完放前台",
)
win._config.tabs = {
    "1": tab,
    "2": TabSettings(jobs=[PrintJob(file_path=make_pdf("src8.pdf", 3), copies=1, duplex="off",
                                    page_count=3, orientation="portrait",
                                    display_name="会议议程.pdf")],
                     owner_name="李娜"),
}
win._current_tab = "1"
win._config.active_tab = "1"
win._config.admin_names = [n for n, _ in _MEMBERS]
win._config.simplex_price = 0.30          # 与云端 pricing.json 同口径（0.30 / 0.40）
win._config.duplex_price = 0.40

# 打印机：现场那台（无自动双面器 → 双面靠程序分两趟）——不触发能力探测（blockSignals）
combo = win._printer_combo
combo.blockSignals(True)
combo.clear()
combo.addItem("HP LaserJet 1020")
combo.setCurrentIndex(0)
combo.blockSignals(False)
win._config.printer_name = "HP LaserJet 1020"

win._update_price_labels()
win._rebuild_table()
win._refresh_tab_display()
win._update_total_cost()

# 云端连接状态（状态栏「☁ 接单中 / 断开云端」）——只假连上，不真连
class _FakeCloud:
    take_orders = True
    last_printer_state = {"owner_name": "张伟"}

    def is_connected(self):
        return True


win._cloud_client = _FakeCloud()
win._update_cloud_status()

# 选中第一个文件 → 右侧「任务参数」显示它的实际参数（也让表格看起来"在用"）
win._table.selectRow(0)
app.processEvents()

# 日志区：写一段有代表性的模拟日志（真实时间戳会让图上看不出逻辑，这里用连续时刻）。
# 只写 3 行：日志框默认高度正好放得下，多写会被裁在半行上。
LOG_LINES = [
    "[15:39:58] 🔌 已连接到云端服务器 · ☁ 本机正在接单",
    "[15:40:07] 📂 已添加 8 个文件（工作副本已暂存），合计 ¥62.80",
    "[15:40:15] 🖨 本批含双面文件，HP LaserJet 1020 需分两趟并提示翻纸",
]
win._log_text.setPlainText("\n".join(LOG_LINES))

app.processEvents()
os.makedirs(os.path.dirname(OUT), exist_ok=True)
pix = win.grab()
pix.save(OUT)
print(f"已生成 {OUT}  ({pix.width()}x{pix.height()})", flush=True)
