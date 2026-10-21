# -*- coding: utf-8 -*-
"""「帮助 → 打印速度…」验收（2026-10-20 用户要求）。

两件事必须钉住：
  ① 放行口径从「预估 − 5 秒」改成「预估 × 比例（默认 50%） + 人工翻页准备时间」；
  ② 「启动/预热」「每张纸」「人工翻页准备时间」都能在**同一个窗口**里手动调，
     原来的两点测速收进该窗口当按钮（入口名「打印速度…」）。

跑法：PYTHONPATH= PYTHONUTF8=1 python tests/verify_print_speed_ui.py   （exit 0 即全绿）
"""
import os
import sys
import tempfile

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL_TOOL = os.path.join(REPO, "local_print_tool")
_TMP = tempfile.mkdtemp(prefix="hn_speedui_")
os.environ["APPDATA"] = _TMP
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, LOCAL_TOOL)
os.chdir(LOCAL_TOOL)

from PySide6.QtWidgets import QApplication, QDialog  # noqa: E402

_app = QApplication.instance() or QApplication([])

import gui  # noqa: E402
import printer_config as pc  # noqa: E402

ok_all = True


def check(name, cond, detail=""):
    global ok_all
    if not cond:
        ok_all = False
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


SRC = open(os.path.join(LOCAL_TOOL, "gui.py"), encoding="utf-8").read()

# ══════════════════════════════════════════════════════════════════════
print("=== ① 窗口把四类设置装在一起 ===")
prof = pc.PrinterProfile(speed_manual_a=13.0, speed_manual_b=6.9,
                         gate_ratio=0.75, flip_ready_sec=20.0)
dlg = gui.PrintSpeedDialog(None, prof, "HP LaserJet 1020")
vals = dlg.values()
check("启动/预热、每张纸、放行比例、人工翻页准备时间四个值都从档案读进控件",
      vals == {"speed_manual_a": 13.0, "speed_manual_b": 6.9,
               "gate_ratio": 0.75, "flip_ready_sec": 20.0}, str(vals))
check("公式文案随比例/准备时间实时更新（把「放行时刻 = …」写清）",
      "75%" in dlg._formula.text() and "20 秒" in dlg._formula.text(),
      dlg._formula.text())
dlg._ratio.setValue(50)
check("改比例后公式跟着变", "50%" in dlg._formula.text(), dlg._formula.text())
dlg._on_reset()
check("「恢复自动学习」把两个分量清零（比例/翻页时间不动）",
      dlg.values()["speed_manual_a"] == 0.0 and dlg.values()["speed_manual_b"] == 0.0
      and dlg.values()["gate_ratio"] == 0.5 and dlg.values()["flip_ready_sec"] == 20.0,
      str(dlg.values()))
check("窗口里有一键测速按钮（原入口收进来了）",
      hasattr(dlg, "_btn_cal") and "测速" in dlg._btn_cal.text())
check("测速按钮通过信号交给主窗口（窗口自己不打纸）",
      dlg.__class__.calibrate_requested is not None
      and "dlg.calibrate_requested.connect(self._start_speed_calibration)" in SRC)
dlg.close()

# ══════════════════════════════════════════════════════════════════════
print("\n=== ② 菜单入口与保存链路 ===")
check("帮助菜单改名「打印速度…」并仍走 _on_calibrate_speed",
      'QAction("打印速度…"' in SRC and "speed_action.triggered.connect(self._on_calibrate_speed)" in SRC
      and 'QAction("打印速度校准…"' not in SRC)
check("保存写进本机档案（update_profile + _save_config，下一批生效）",
      "self._config.update_profile(self._config.printer_name, **vals)" in SRC
      and "（下一批打印生效）" in SRC)
check("原来的测速逻辑抽成 _start_speed_calibration（窗口按钮调它）",
      "def _start_speed_calibration(self):" in SRC
      and "SpeedCalibrationWorker(self._config.printer_name)" in SRC)
check("打印机的忙 / 攒批纸叠未完成两道闸门都在（窗口与测速共用）",
      "def _speed_settings_block_reason(self)" in SRC
      and '"打印机正忙"' in SRC and '"攒批未完成"' in SRC)
check("攒批未完成 ⇒ 点入口就被拒（测速会打 11 张纸，插进纸叠会让整叠错位）",
      "🕐 测速被拒：出纸槽里有未完成的手动双面纸叠" in SRC)

# ══════════════════════════════════════════════════════════════════════
print("\n=== ③ 放行闸门按档案比例算（不是写死 5 秒） ===")
p50 = pc.PrinterProfile(gate_ratio=0.5, flip_ready_sec=0.0)
p100 = pc.PrinterProfile(gate_ratio=1.0, flip_ready_sec=30.0)
check("默认 50%：预估 100 秒 ⇒ 50 秒放行",
      abs(p50.gate_unlock_seconds(100.0) - 50.0) < 1e-6)
check("可调成 100% + 30 秒（= 打完再给 30 秒翻页）：100 ⇒ 130 秒",
      abs(p100.gate_unlock_seconds(100.0) - 130.0) < 1e-6)
check("窗口对话返回的 dict 能直接喂给 update_profile（键名与档案字段一致）",
      set(dlg.values().keys()) == {"speed_manual_a", "speed_manual_b",
                                   "gate_ratio", "flip_ready_sec"}
      and set(dlg.values().keys()) <= set(pc.PrinterProfile._FIELDS))

# ══════════════════════════════════════════════════════════════════════
print("\n=== ④ 主窗口把档案参数传进翻纸窗 ===")
check("announce 带上 gate_ratio / gate_extra（窗口据此算闸门）",
      "gate_ratio=prof.gate_ratio, gate_extra=prof.flip_ready_sec" in SRC)
check("闸门文案说明按百分比放行 + 可在「帮助 → 打印速度…」里调",
      '按「预估 {_ratio}%' in SRC and "放行比例与人工翻页时间可在「帮助 → 打印速度…」里调" in SRC)

print("\n" + "=" * 56)
print("打印速度窗口验收:", "全部通过 ✅" if ok_all else "存在失败 ❌")
import shutil  # noqa: E402
shutil.rmtree(_TMP, ignore_errors=True)
sys.exit(0 if ok_all else 1)
