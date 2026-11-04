# -*- coding: utf-8 -*-
"""临时截图脚本：把「打印速度」窗口在三种双面能力下的样子存成 PNG（不入 git 长期使用）。"""
import os
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LOCAL_TOOL = os.path.join(REPO, "local_print_tool")
os.environ["APPDATA"] = tempfile.mkdtemp(prefix="hn_shot_")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, LOCAL_TOOL)
os.chdir(LOCAL_TOOL)

from PySide6.QtWidgets import QApplication  # noqa: E402

app = QApplication.instance() or QApplication([])

# Qt 6 不自带字体，offscreen 下全是豆腐块 ⇒ 手动挂系统中文字体
from PySide6.QtGui import QFont, QFontDatabase  # noqa: E402

for cand in (r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\msyhbd.ttc",
             r"C:\Windows\Fonts\simhei.ttf", r"C:\Windows\Fonts\Deng.ttf"):
    if os.path.exists(cand) and QFontDatabase.addApplicationFont(cand) >= 0:
        print("font:", cand, flush=True)
        break
_fam = (QFontDatabase.families() or ["Microsoft YaHei"])[0]
app.setFont(QFont("Microsoft YaHei", 9) if "YaHei" in " ".join(QFontDatabase.families())
            else QFont(_fam, 9))
print("families:", QFontDatabase.families()[:5], flush=True)
import gui  # noqa: E402
import printer_config as pc  # noqa: E402

OUT = os.path.join(REPO, "截图展示", "打印速度窗口_自动双面适配")
os.makedirs(OUT, exist_ok=True)

cases = [
    ("auto", pc.PrinterProfile(auto_duplex=True, manual_duplex=False,
                               capability_source="manual", verified_at="2026-10-30 19:55:00"),
     "HP LaserJet MFP M232-M237"),
    ("manual", pc.PrinterProfile(auto_duplex=False, manual_duplex=True,
                                 capability_source="manual",
                                 speed_manual_a=7.0, speed_manual_b=5.6,
                                 gate_ratio=0.6, flip_ready_sec=5.0),
     "HP LaserJet 1020"),
    ("unknown", pc.PrinterProfile(auto_duplex=None), "HP LaserJet 1020"),
]

for slug, prof, queue in cases:
    d = gui.PrintSpeedDialog(None, prof, queue)
    d.setStyleSheet("")  # 不套主题，保证截图清晰
    d.show()
    app.processEvents()
    d.adjustSize()
    app.processEvents()
    path = os.path.join(OUT, f"{slug}.png")
    d.grab().save(path)
    print(slug, d.size().width(), d.size().height(), path, flush=True)
    d.close()
