# -*- coding: utf-8 -*-
"""验收脚本 · 日志/文案必须**准确反映当前状态**（2026-10 用户要求）

起因：现场日志里出现「🖐 攒批模式已开启：当前没有正在累积的纸叠」——把**当时的状态**
（模式已开启）写成了像**刚刚发生的事件**，而真正的重点（没有纸叠、点了不会有动作）
反而在后面。顺着这条线全量扫了一遍日志/提示文案，把同一类"说得不准"的地方一并修掉：

  A. 状态陈述写成事件   「已取消关闭云端」→ 云端其实还开着；「云端开关已保存，下次启动
     生效」→ 此刻客户端已经 stop 了，重启前根本不会连云端；「本机已启用接单」→ 这是每次
     连接都会打印的状态，不是"刚开启"。
  B. 把能分辨的差别糊成「（A 或 B）」  「支持自动双面（或双面能力未确认）」——两种原因的
     处置完全不同（前者本来就无需求，后者要去盖章）；「打印机正在打（第 1 趟或整叠第 2 趟）」；
     「整叠第 2 趟正在打（或待收尾）」；「当前没有在线设备（或全部设备超时未回报）」——
     最后那种在 logs 为空时根本不可能出现。
  C. 含糊的否定/宾语缺失  「标签页 X 无文件，跳过」→ 文件在，只是都打完了；
     「正在打印，已自动取消」→ 取消的是"本机这次打印作业"，不是订单。
  D. 少说一句关键状态  「已挂起等人工翻纸」→ 是本机挂起（不回报失败），订单不会被判失败。

    python tests/verify_log_wording_accuracy.py     # exit 0 = 全绿
"""
import os
import re
import shutil
import sys
import tempfile

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL_TOOL = os.path.join(REPO, "local_print_tool")

_SANDBOX = tempfile.mkdtemp(prefix="hn_logword_")
os.environ["APPDATA"] = _SANDBOX
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, LOCAL_TOOL)
os.chdir(LOCAL_TOOL)

from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

_app = QApplication.instance() or QApplication([])
QMessageBox.information = staticmethod(lambda *a, **kw: QMessageBox.Ok)
QMessageBox.warning = staticmethod(lambda *a, **kw: QMessageBox.Ok)
QMessageBox.question = staticmethod(lambda *a, **kw: QMessageBox.Yes)

import gui  # noqa: E402
import paths  # noqa: E402
from printer_config import PrinterProfile  # noqa: E402

ok_all = True


def check(name, cond, detail=""):
    global ok_all
    if not cond:
        ok_all = False
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


G = open(os.path.join(LOCAL_TOOL, "gui.py"), encoding="utf-8").read()
CC = open(os.path.join(LOCAL_TOOL, "cloud_client.py"), encoding="utf-8").read()

# ══════════════════════════════════════════════════════════════════════════
print("=== ① 旧的歧义文案必须已经消失（按**代码里的整句**核对，注释里引用不算）===")
GONE = [
    ('"☁ 订单 #{order_id} 正在打印，已自动取消"',
     "「正在打印，已自动取消」没说清取消的是什么"),
    ("：标签页 {tab_key} 无文件，跳过", "「无文件」与「标签页里其实有文件」不符"),
    ("（第 1 趟或整叠第 2 趟），打完再决定下一步", "两趟能分辨却糊成一句"),
    ("（或双面能力未确认）—— 不需要攒批模式", "两种不适用的原因处置完全不同"),
    ("（或能力未确认），不需要攒批模式", "同上（移动端回报给手机的那句）"),
    ("正在打（或待收尾）——出纸槽", "能分辨「正在打 / 刚打完收尾」"),
    ('"❌ 已取消关闭云端"', "读起来像「云端已经关了」"),
    ('"🔁 云端开关已保存，下次启动时生效（{state_txt}）"', "没交代此刻的进程状态"),
    ('"当前没有在线设备（或全部设备超时未回报）。"', "logs 为空时后者不可能发生"),
]
for frag, why in GONE:
    check(f"已消失：{frag[:34]}…（{why}）", frag not in G, "".join(
        l for l in G.splitlines(keepends=True) if frag in l)[:200])

# ══════════════════════════════════════════════════════════════════════════
print("\n=== ② 新文案必须把状态说清（正向钉住，防回退）===")
KEEP = [
    (G, "❌ 已取消「关闭云端」：云端保持开启", "取消关闭时明确「云端还开着」"),
    (G, "重启前本进程不会连接云端（云端订单不会进来）", "不重启时要交代当前进程状态"),
    (G, "已完成打印并回报成功！标签页已锁定", "「打印成功」限定为「已打印并回报」"),
    (G, "已无待打印文件", "无障碍路径：文件在，只是都打完了"),
    (G, "已中止本机打印作业", "取消订单时明确中止的是本机作业"),
    (G, "本机已挂起等人工翻纸", "挂起是本机状态（不回报失败）"),
    (G, "某一单的第 1 趟", "攒批入口日志分辨正在打哪一趟"),
    (G, "刚打完、正在收尾", "排队原因分辨「正在打 / 在收尾」"),
    (G, "_stack_inapplicable_reason", "不适用的原因单一出口"),
    (CC, "本机正在接单（可接收云端订单）", "接单状态用进行时（不是「刚开启」）"),
]
for src, frag, why in KEEP:
    check(f"已就位：{frag[:30]}…（{why}）", frag in src)

# ══════════════════════════════════════════════════════════════════════════
print("\n=== ③ `_stack_inapplicable_reason()` 按档案三态分流 ===")
win = gui.MainWindow(config_path=paths.config_path())
win._config.printer_name = "Fake Print"

win._printer_profile = lambda: PrinterProfile(auto_duplex=True, manual_duplex=False)
r_auto = win._stack_inapplicable_reason()
check("已确认支持自动双面 → 说「已确认支持自动双面」（本来就无需求）",
      "已确认支持自动双面" in r_auto and "不需要攒批" in r_auto, r_auto)
check("……不再出现「或」（不糊成二选一）", "或" not in r_auto, r_auto)

win._printer_profile = lambda: PrinterProfile()          # 空档案 = 能力未确认
r_un = win._stack_inapplicable_reason()
check("能力未确认 → 说「双面能力未确认」并指路去盖章",
      "双面能力未确认" in r_un and "帮助 → 打印机双面能力" in r_un, r_un)
check("……两句原因互不相同（没被糊成一句）", r_auto != r_un)

win._printer_profile = lambda: PrinterProfile(auto_duplex=False, manual_duplex=True)
check("需人工翻纸 → 适用（`_stack_applicable()` 为 True，不会被误判）",
      win._stack_applicable() is True)


class _Running:
    def isRunning(self):
        return True


class _Idle:
    def isRunning(self):
        return False


def _logs_of(w):
    return w._log_text.toPlainText()


print("\n=== ④ 点「🖐 攒批」时日志分辨正在打哪一趟 ===")
win._config.duplex_stack_mode = True
win._printer_profile = lambda: PrinterProfile(auto_duplex=False, manual_duplex=True)
win._stack.entries = [object()]        # 只要有纸叠就够（忙分支不读 entry 内容）

win._worker = _Running()
win._stack_flush_worker = None
before = _logs_of(win)
win._on_stack_button()
after = _logs_of(win)
check("第 1 趟在打 → 日志说「某一单的第 1 趟」",
      "某一单的第 1 趟" in after[len(before):], after[-120:])

win._worker = None
win._stack_flush_worker = _Running()
before = _logs_of(win)
win._on_stack_button()
after = _logs_of(win)
check("整叠第 2 趟在打 → 日志说「整叠第 2 趟」",
      "整叠第 2 趟" in after[len(before):], after[-120:])

win._worker = None
win._stack_flush_worker = None
win._stack.entries = []

for w in (win,):
    try:
        w.close()
    except Exception:
        pass

print("\n" + ("全部通过 ✅" if ok_all else "存在失败项 ❌"))
shutil.rmtree(_SANDBOX, ignore_errors=True)
sys.exit(0 if ok_all else 1)
