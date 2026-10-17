# -*- coding: utf-8 -*-
"""验收脚本 · 打印机双面能力：未确认要说出来 + 用户能自己盖章（2026-10-17 事故闭环）

背景（现场事故链条，见 printer_config.py / gui.py 相关注释）：
  1) 档案被旧版客户端的整体落盘清空 ⇒ 本机「HP LaserJet 1020」的能力变成"未确认"；
  2) 能力未确认 ⇒ 程序按旧行为把双面请求交给驱动，而该机型**驱动虚报支持自动双面**
     （DC_DUPLEX=1 且接受 Duplex=2）⇒ 实际走驱动程序自带的「双面打印说明」向导；
  3) 单页文件被驱动多吐一张空白页（订单 HN20261017-0002），而且全程没走程序自己的
     两趟控制器（manual_duplex）—— 用户根本不知道要去哪里盖章。

本脚本验收两个补丁：
  ① `MainWindow._warn_if_duplex_capability_unknown`：未确认（且非虚拟打印机）就在日志里
     明说后果（会走驱动自带的向导、单页可能多吐空白页），同一队列同一状态只说一次，
     切到另一个未盖章队列会重判并再说一次；
  ② `帮助 → 打印机双面能力…`：三态盖章（未确认 / 不支持自动双面 / 支持自动双面）+
     手动双面参数 + 校准人，写入当前队列档案（capability_source="manual"，
     人工结论不被探测覆盖），并立刻联动攒批入口显隐与「双面（手动翻纸）」文案。

全程离屏 + APPDATA 沙箱，**不打印、不碰真实 `%APPDATA%\\HN打印工具`**。

    python tests/verify_duplex_capability_ui.py       # exit 0 = 全绿
"""
import io
import os
import sys
import json
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL_TOOL = os.path.join(REPO, "local_print_tool")

_SANDBOX = tempfile.mkdtemp(prefix="hn_duplex_cap_")
os.environ["APPDATA"] = _SANDBOX                  # 用户数据目录 → 沙箱（不碰真实配置）
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, LOCAL_TOOL)
os.chdir(LOCAL_TOOL)

from PySide6.QtWidgets import QApplication, QDialog  # noqa: E402

_app = QApplication.instance() or QApplication([])

import converter as _converter  # noqa: E402
_converter.get_available_engines = lambda: {"word": True, "wps": True, "libreoffice": True}

import gui  # noqa: E402
import paths  # noqa: E402
from printer_config import PrintJob, PrinterProfile, PrinterConfig  # noqa: E402

# offscreen 下没人能回答模态框 → 一律自动回「好/是」
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


def make_job(pages, duplex="on", name="doc.pdf"):
    return PrintJob(file_path=os.path.join(_SANDBOX, name), copies=1, duplex=duplex,
                    page_count=pages, engine="word")


def select_printer(name):
    """把顶栏下拉切到 name（真实使用路径：_sync_ui_to_config 会把 printer_name 从下拉读回来，
    所以测试也必须让下拉与 config 一致 —— 直接赋值 _config.printer_name 会在下一次
    _save_config 被下拉值覆盖，那是测试造出来的假状态，不是产品的真实路径）。"""
    combo = win._printer_combo
    for i in range(combo.count()):
        if combo.itemData(i) == name:
            combo.setCurrentIndex(i)
            break
    else:
        combo.addItem(name, name)
        combo.setCurrentIndex(combo.count() - 1)
    win._config.printer_name = name
    win._duplex_warned = ""
    return win._config.printer_name


print("=" * 78)
print("① 菜单入口：帮助 → 打印机双面能力…")
win = gui.MainWindow(config_path=paths.config_path())
logs = []
_orig_log = win._log          # 捕获主窗口日志（与 tests/verify_stack_gate_exclusive.py 同法）
win._log = lambda m: (logs.append(m), _orig_log(m))[1]

texts = []
for menu_action in win.menuBar().actions():
    m = menu_action.menu()
    if m is not None:
        texts.extend(a.text() for a in m.actions())
check("帮助菜单里有「打印机双面能力…」", any("打印机双面能力" in t for t in texts),
      f"菜单动作: {texts}")
check("动作已接线到 _on_printer_duplex_capability（方法存在且可调用）",
      callable(getattr(win, "_on_printer_duplex_capability", None)))

print("\n② 未确认队列 ⇒ 日志明说（含「驱动自带向导 / 空白页」后果）")
select_printer("HP LaserJet 1020")
win._config.printer_profiles.pop("HP LaserJet 1020", None)
win._duplex_warned = ""
logs.clear()
win._warn_if_duplex_capability_unknown()
warn = "\n".join(logs)
check("提示里点明能力未确认", "双面能力" in warn and "未确认" in warn, warn[:160])
check("提示里说明会走驱动程序自带的手动双面向导", "双面打印说明" in warn)
check("提示里说明单页文件可能先吐一张空白页", "空白页" in warn)
check("提示里给出盖章入口（帮助 → 打印机双面能力…）", "打印机双面能力" in warn)

print("\n②b 同一队列同一状态只说一次（防刷日志）；force 可强制再说")
logs.clear()
win._warn_if_duplex_capability_unknown()
check("重复调用不再输出", not [m for m in logs if "未确认" in m], f"{logs}")
logs.clear()
win._warn_if_duplex_capability_unknown(force=True)
check("force=True 可再提示", any("未确认" in m for m in logs))

print("\n③ 虚拟打印机（不出纸）不提示")
select_printer("Microsoft Print to PDF")
logs.clear()
win._warn_if_duplex_capability_unknown()
check("Print to PDF 不刷「未确认」提示", not [m for m in logs if "未确认" in m], f"{logs}")

print("\n④ 盖章「不支持自动双面」：写档案 + 立刻接上自制控制器 + 攒批入口出现")
select_printer("HP LaserJet 1020")
dlg = gui.PrinterDuplexCapabilityDialog("HP LaserJet 1020",
                                        win._printer_profile(),
                                        verified_by="霍楠", parent=win)
check("对话框初始为「未确认」", dlg._cap.currentIndex() == 0)
check("未确认时手动参数组灰着", not dlg._grp.isEnabled())
dlg._cap.setCurrentIndex(1)
check("选「不支持自动双面」后手动参数组可用", dlg._grp.isEnabled())
dlg._first.setCurrentIndex(0)          # 奇数页先打
dlg._reverse.setCurrentIndex(0)        # 逆序
dlg._rotate.setChecked(True)           # 180° 补偿
vals = dlg.values()
check("values() 三态与参数映射正确",
      vals["auto_duplex"] is False and vals["capability_source"] == "manual"
      and vals["manual_duplex"] is True and vals["first"] == "odd"
      and vals["reverse_second"] is True and vals["second_rotate_180"] is True
      and vals["verified_by"] == "霍楠" and bool(vals["verified_at"]),
      json.dumps({k: v for k, v in vals.items() if k != "note"}, ensure_ascii=False))

check("_apply_duplex_capability 返回成功", win._apply_duplex_capability("HP LaserJet 1020", vals))
prof = win._printer_profile()
check("内存档案：不支持自动双面 + 手动双面 + 校准人",
      prof.auto_duplex is False and prof.manual_duplex is True
      and prof.capability_source == "manual" and prof.verified_by == "霍楠",
      prof.summary())

disk = json.load(open(paths.config_path(), encoding="utf-8"))
check("print_config.json 已落盘该档案（镜像）",
      disk.get("printer_profiles", {}).get("HP LaserJet 1020", {}).get("auto_duplex") is False)
store_path = os.path.join(os.path.dirname(os.path.abspath(paths.config_path())),
                          "printer_profiles.json")
check("独立档案文件 printer_profiles.json 已生成（task-2 的权威存储）",
      os.path.isfile(store_path))
if os.path.isfile(store_path):
    st = json.load(open(store_path, encoding="utf-8"))
    check("独立文件里 capability_source=manual / first=odd / 180° 补偿",
          st.get("profiles", {}).get("HP LaserJet 1020", {}).get("capability_source") == "manual"
          and st["profiles"]["HP LaserJet 1020"]["first"] == "odd"
          and st["profiles"]["HP LaserJet 1020"]["second_rotate_180"] is True)

check("自制控制器接上：多页双面任务 _uses_manual_duplex_job == True",
      win._uses_manual_duplex_job(make_job(2)) is True)
check("攒批入口对「需人工翻纸」机型开放（_stack_applicable == True）",
      win._stack_applicable() is True)
check("双面下拉文案改成「双面（手动翻纸）」",
      win._edit_duplex.itemText(0) == "双面（手动翻纸）", win._edit_duplex.itemText(0))
logs.clear()
win._warn_if_duplex_capability_unknown(force=True)
check("盖章后不再提示未确认", not [m for m in logs if "未确认" in m])

print("\n⑤ 盖章「支持自动双面」：不再走自制控制器、攒批入口收起")
dlg2 = gui.PrinterDuplexCapabilityDialog("HP LaserJet 1020", win._printer_profile(), parent=win)
check("再次打开时按已存档案预选「不支持自动双面」", dlg2._cap.currentIndex() == 1)
dlg2._cap.setCurrentIndex(2)
vals2 = dlg2.values()
check("支持态 values()：auto_duplex=True / manual_duplex=False",
      vals2["auto_duplex"] is True and vals2["manual_duplex"] is False
      and vals2["capability_source"] == "manual")
win._apply_duplex_capability("HP LaserJet 1020", vals2)
check("支持态下双面不走自制控制器", win._uses_manual_duplex_job(make_job(2)) is False)
check("支持态下攒批入口不适用", win._stack_applicable() is False)
check("双面下拉文案回到「双面打印」", win._edit_duplex.itemText(0) == "双面打印")

print("\n⑥ 改回「未确认」：参数留着，但不再声称双面能力")
dlg3 = gui.PrinterDuplexCapabilityDialog("HP LaserJet 1020", win._printer_profile(), parent=win)
dlg3._cap.setCurrentIndex(0)
vals3 = dlg3.values()
check("未确认 values()：auto_duplex=None / capability_source 清空 / manual False",
      vals3["auto_duplex"] is None and vals3["capability_source"] == ""
      and vals3["manual_duplex"] is False)
win._apply_duplex_capability("HP LaserJet 1020", vals3)
prof3 = win._printer_profile()
check("回到未确认（auto_duplex=None，参数仍在档里）",
      prof3.auto_duplex is None and prof3.first == "odd", prof3.summary())
check("未确认时双面不自制控制器", win._uses_manual_duplex_job(make_job(2)) is False)
logs.clear()
win._warn_if_duplex_capability_unknown(force=True)
check("回到未确认后提示恢复", any("未确认" in m for m in logs))

print("\n⑦ 端到端：_on_printer_duplex_capability 真的把对话框结果写进档案")
_saved = {}


class _StubDialog:
    def __init__(self, *a, **k):
        pass

    def exec(self):
        return QDialog.Accepted

    def values(self):
        return {"auto_duplex": False, "capability_source": "manual", "manual_duplex": True,
                "first": "odd", "reverse_second": True, "second_rotate_180": True,
                "verified_at": "2026-10-17 18:00:00", "verified_by": "霍楠", "note": "端到端桩"}


_orig_dlg = gui.PrinterDuplexCapabilityDialog
gui.PrinterDuplexCapabilityDialog = _StubDialog
try:
    win._on_printer_duplex_capability()
finally:
    gui.PrinterDuplexCapabilityDialog = _orig_dlg
_saved = win._printer_profile()
check("端到端后档案=不支持自动双面 + manual（内含 _save_config 落盘）",
      _saved.auto_duplex is False and _saved.manual_duplex is True
      and _saved.verified_by == "霍楠")
check("端到端后控制器生效", win._uses_manual_duplex_job(make_job(3)) is True)
check("端到端后独立档案文件也更新",
      json.load(open(store_path, encoding="utf-8"))["profiles"]["HP LaserJet 1020"]["verified_by"] == "霍楠")

print("\n⑧ 换队列重判：另一个未盖章队列会再提示一次")
select_printer("Fake Queue B")
check("切换处理器把 printer_name 同步进配置", win._config.printer_name == "Fake Queue B",
      win._config.printer_name)
logs_after = [m for m in logs if "Fake Queue B" in m and "未确认" in m]
check("新队列未盖章 ⇒ 重新提示（含队列名）", bool(logs_after), f"{logs[-3:]}")
select_printer("HP LaserJet 1020")
check("切队列不误伤已盖章队列的档案",
      win._config.get_profile("HP LaserJet 1020").auto_duplex is False)

print("\n⑨ 零回归：档案读取接口与既有语义未变")
cfg2 = PrinterConfig.load(paths.config_path())
check("重新 load 后档案仍在（含 manual/校准人）",
      cfg2.get_profile("HP LaserJet 1020").auto_duplex is False
      and cfg2.get_profile("HP LaserJet 1020").verified_by == "霍楠")
check("PrinterProfile 默认值未变（未确认 + first=odd）",
      PrinterProfile().auto_duplex is None and PrinterProfile().first == "odd")

win.close()
print("=" * 78)
print("全部通过 ✅" if ok_all else "存在失败 ❌")
sys.exit(0 if ok_all else 1)
