# -*- coding: utf-8 -*-
"""验收脚本 · 主界面「🖨 打印列表自动弹出」开关（offscreen，不真打印）

用户场景（2026-11）：那个常驻窗（StackQueueDialog：攒批列表 + 本机队列）在
"有任务在打 / 有活排队 / 出纸槽里有纸叠等第 2 趟"时会自己出现，用户觉得每次打印
都弹一下很烦，要求主界面加开关控制这个"自动弹出"。

钉住的口径：

  ① 配置字段 `print_list_auto_show` 默认 True（老配置没有这个键 ⇒ 开，零回归），
     坏值（字符串 / None / 缺键）一律回落 True，to_dict / from_dict 往返一致；
  ② 复选框是 ThemedCheckBox、文案含「打印列表自动弹出」、不含 markdown 星号，
     放在「🖐 攒批模式」**左边**（不破坏底栏分隔线的布局顺序验收）；
  ③ 关掉后：有本机队列 / 攒批模式开着 / 有纸叠 ⇒ `_sync_stack_list_visible()`
     **都不 show**；空闲了仍然自动收起；
  ④ 关掉后手动入口照旧可用：`_on_stack_button()` 能把窗口叫出来（关的是"自动"）；
  ⑤ 重新打开开关 ⇒ 立刻恢复自动弹出（不必等下一次状态变化）；
  ⑥ 落盘：勾选 / 取消都写进 print_config.json，重新 load 后保持；
  ⑦ 静态接线：`_sync_stack_list_visible` 里开关闸门在**任何 show 之前**早退；
     攒批续打恢复路径也按开关分流（不能让"恢复遗留纸叠"绕过开关弹窗）。

    python other/tests/verify_print_list_auto_show.py     # exit 0 = 全绿
"""
import io
import json
import os
import re
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LOCAL_TOOL = os.path.join(REPO, "local_print_tool")

_SANDBOX = tempfile.mkdtemp(prefix="hn_autoshow_")
os.environ["APPDATA"] = _SANDBOX                  # 用户数据目录 → 沙箱（不碰真实配置）
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, LOCAL_TOOL)
os.chdir(LOCAL_TOOL)

from PySide6.QtWidgets import QApplication  # noqa: E402

_app = QApplication.instance() or QApplication([])

import gui  # noqa: E402
import paths  # noqa: E402
from printer_config import PrintJob, PrinterConfig, PrinterProfile  # noqa: E402

# 真打印一律不碰：MainWindow 用的对话框/警告一律给安全答案
gui.QMessageBox.question = staticmethod(lambda *a, **k: gui.QMessageBox.No)
gui.QMessageBox.warning = staticmethod(lambda *a, **k: gui.QMessageBox.Ok)
gui.QMessageBox.information = staticmethod(lambda *a, **k: gui.QMessageBox.Ok)
gui.QMessageBox.critical = staticmethod(lambda *a, **k: gui.QMessageBox.Ok)

ok_all = True


def check(name, cond, detail=""):
    global ok_all
    if not cond:
        ok_all = False
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


SRC = open(os.path.join(LOCAL_TOOL, "gui.py"), encoding="utf-8").read()


def _body(name, src=None):
    """取一个方法的源码正文（到下一个 `def ` 或类尾），静态接线断言用。"""
    src = SRC if src is None else src
    m = re.search(rf"\n    def {re.escape(name)}\(", src)
    if not m:
        return ""
    rest = src[m.end():]
    nxt = re.search(r"\n    def |\n\nclass ", rest)
    return rest[:nxt.start()] if nxt else rest


MANUAL = PrinterProfile(auto_duplex=False, capability_source="manual", manual_duplex=True,
                        first="odd", reverse_second=True, second_rotate_180=True,
                        verified_at="2026-11-09 10:00:00", verified_by="测试成员",
                        stack_capacity=90, stack_warn_after_sec=1800)
AUTO = PrinterProfile(auto_duplex=True, capability_source="probe", manual_duplex=False,
                      stack_capacity=90, stack_warn_after_sec=1800)


def make_job(name, pages=4, duplex="on"):
    return PrintJob(file_path=os.path.join(_SANDBOX, name), copies=1, duplex=duplex,
                    page_range="", page_count=pages, orientation="portrait",
                    engine="pdf", display_name=name)


class FakeWorker:
    """只提供「本机队列」读取所需的那几个属性。"""

    def __init__(self, jobs, running=True):
        self._jobs = list(jobs)
        self._stack_mode = False
        self._pass_state = ""
        self._running = bool(running)

    def isRunning(self):
        return self._running


# ============================================================
print("\n=== ① 配置字段：默认开、坏值回落、往返一致 ===")

check("字段默认 True（PrinterConfig() 新建即开）",
      PrinterConfig().print_list_auto_show is True)
check("老配置缺这个键 ⇒ True（零回归）",
      PrinterConfig.from_dict({}).print_list_auto_show is True)
check("坏值 'yes' ⇒ 回落 True（_bool_or_default 只认 bool / 0-1）",
      PrinterConfig.from_dict({"print_list_auto_show": "yes"}).print_list_auto_show is True)
check("坏值 None ⇒ 回落 True",
      PrinterConfig.from_dict({"print_list_auto_show": None}).print_list_auto_show is True)
check("显式 False ⇒ False",
      PrinterConfig.from_dict({"print_list_auto_show": False}).print_list_auto_show is False)
check("显式 0 ⇒ False（数字兼容）",
      PrinterConfig.from_dict({"print_list_auto_show": 0}).print_list_auto_show is False)
_cfg = PrinterConfig()
_cfg.print_list_auto_show = False
check("to_dict 带上这个键", _cfg.to_dict().get("print_list_auto_show") is False)
check("to_dict → from_dict 往返一致",
      PrinterConfig.from_dict(_cfg.to_dict()).print_list_auto_show is False)

# ============================================================
print("\n=== ② 主界面复选框：类型 / 文案 / 位置 ===")

win = gui.MainWindow(config_path=paths.config_path())
try:
    win._stack_state_timer.stop()          # 心跳由测试自己按需调，避免异步插一脚
except Exception:
    pass

chk = getattr(win, "_chk_print_list_auto", None)
check("主界面构造了 _chk_print_list_auto", chk is not None)
check("是 ThemedCheckBox（硬规矩：新增复选框不许直接造 QCheckBox）",
      isinstance(chk, gui.ThemedCheckBox), type(chk).__name__)
check("文案含「打印列表自动弹出」", "打印列表自动弹出" in chk.text(), chk.text())
check("文案不带 markdown 星号（QLabel 会原样画出来）",
      "**" not in chk.text() and "**" not in chk.toolTip())
check("tooltip 说清两种状态 + 手动入口",
      "手动打开" in chk.toolTip() and "自动收起" in chk.toolTip(), chk.toolTip()[:40])
check("默认勾上（与配置默认一致）", chk.isChecked() is True)
check("toggled 接到 _on_print_list_auto_toggled",
      _body("_on_print_list_auto_toggled") != ""
      and "_on_print_list_auto_toggled" in _body("_setup_button_bar")
      or "self._chk_print_list_auto.toggled.connect(" in SRC)

_bar_start = SRC.index("def _setup_button_bar")
_bar = SRC[_bar_start:SRC.index("    # ---- 数据 → UI ----", _bar_start)]
_i_auto = _bar.find("self._chk_print_list_auto = ThemedCheckBox")
_i_chk = _bar.find("self._chk_stack_mode = ThemedCheckBox")
check("布局顺序：自动弹出开关在「🖐 攒批模式」左边（底栏分隔线验收不受影响）",
      -1 not in (_i_auto, _i_chk) and _i_auto < _i_chk, f"auto={_i_auto} chk={_i_chk}")

load_body = _body("_load_config_to_ui")
check("_load_config_to_ui 把配置同步到复选框",
      "self._chk_print_list_auto.setChecked(" in load_body)
check("同步期间屏蔽信号（加载不触发落盘）",
      "self._chk_print_list_auto.blockSignals(True)" in load_body
      and "self._chk_print_list_auto.blockSignals(False)" in load_body)

# ============================================================
print("\n=== ③ 关掉后：任何自动路径都不 show，空闲仍自动收起 ===")

win._config.printer_name = "Fake Print"
win._config.printer_profiles["Fake Print"] = AUTO
# 组合框里补一个条目：_sync_ui_to_config() 会从 currentData 回写 printer_name，
# 没有条目时每次落盘都会把它清成 ""（然后就查不到档案、攒批判定成不适用）。
win._printer_combo.addItem("Fake Print", "Fake Print")
win._printer_combo.setCurrentIndex(win._printer_combo.count() - 1)
win._config.duplex_stack_mode = False
win._stack_list_user_closed = False
win._stack_flush_worker = None
win._stack_wait = []
win._stack_entry_wait = []
win._auto_print_retry = []
win._scheduled_orders = {}
win._worker = None
_cfg_path = paths.config_path()

chk.setChecked(False)                      # 走真实信号 → 配置 + 落盘 + 当场生效
_app.processEvents()
check("取消勾选 ⇒ 配置 False", win._config.print_list_auto_show is False)
check("_print_list_auto_show() 跟着 False", win._print_list_auto_show() is False)
_saved = json.load(open(_cfg_path, encoding="utf-8"))
check("取消勾选已落盘（print_config.json 里是 false）",
      _saved.get("print_list_auto_show") is False, str(_saved.get("print_list_auto_show")))

# 本机队列有活（自动双面机型：攒批不适用，旧代码会为它弹窗）
win._worker = FakeWorker([make_job("在打.pdf")])
dlg0 = getattr(win, "_stack_queue_dialog", None)
if dlg0 is not None:
    dlg0.hide()
win._sync_stack_list_visible()
dlg0 = getattr(win, "_stack_queue_dialog", None)
check("关掉后：只有本机队列有活 ⇒ 不自动弹（旧行为是会弹的）",
      dlg0 is None or not dlg0.isVisible(),
      f"visible={getattr(dlg0, 'isVisible', lambda: None)()}")
win._sync_stack_button()
check("关掉后：底部入口照旧给读数（手动入口没丢）",
      not win._btn_stack.isHidden() and "🖨 打印中 1" in win._btn_stack.text(),
      win._btn_stack.text())

# 攒批模式开 + 手动机型（旧代码：常驻）
win._config.printer_name = "Fake Print"
win._config.printer_profiles["Fake Print"] = MANUAL
win._config.duplex_stack_mode = True
dlg1 = getattr(win, "_stack_queue_dialog", None)
if dlg1 is not None:
    dlg1.hide()
win._sync_stack_list_visible()
dlg1 = getattr(win, "_stack_queue_dialog", None)
check("关掉后：攒批模式开着也不常驻弹出",
      dlg1 is None or not dlg1.isVisible(),
      f"visible={getattr(dlg1, 'isVisible', lambda: None)()}")

# ============================================================
print("\n=== ④ 关掉后：手动入口照旧能打开 ===")

win._on_stack_button()
_app.processEvents()
dlg2 = getattr(win, "_stack_queue_dialog", None)
check("点底部入口 ⇒ 窗口打开（关的只是「自动」）",
      dlg2 is not None and dlg2.isVisible(),
      f"visible={getattr(dlg2, 'isVisible', lambda: None)()}")

# 已开着的窗口在关掉自动弹出时仍要实时刷新，不能停在旧内容
win._worker = FakeWorker([make_job("第二单.pdf")])
win._sync_stack_list_visible()
check("手动开着的窗口：内容仍跟着刷新（本机队列可见）",
      dlg2 is not None and dlg2.local_queue_visible())

# 空闲 ⇒ 照旧自动收起
win._worker = None
win._config.duplex_stack_mode = False
win._stack.clear() if hasattr(win._stack, "clear") else None
win._sync_stack_list_visible()
check("关掉后：空闲仍然自动收起（不留空窗）",
      dlg2 is not None and not dlg2.isVisible(),
      f"visible={getattr(dlg2, 'isVisible', lambda: None)()}")

# ============================================================
print("\n=== ⑤ 重新打开开关：立刻恢复自动弹出 ===")

win._worker = FakeWorker([make_job("在打.pdf")])
win._sync_stack_list_visible()
check("开关仍关着 ⇒ 还是不出来",
      getattr(win, "_stack_queue_dialog", None) is None
      or not win._stack_queue_dialog.isVisible())
chk.setChecked(True)
_app.processEvents()
check("勾回来 ⇒ 配置 True", win._config.print_list_auto_show is True)
_saved = json.load(open(_cfg_path, encoding="utf-8"))
check("勾回来已落盘", _saved.get("print_list_auto_show") is True)
check("勾回来 ⇒ 当场就弹出来（不必等下一次状态变化）",
      win._stack_queue_dialog is not None and win._stack_queue_dialog.isVisible(),
      f"visible={getattr(getattr(win, '_stack_queue_dialog', None), 'isVisible', lambda: None)()}")

_loaded = PrinterConfig.load(_cfg_path)
check("重新 load 后保持 True（重启不丢）", _loaded.print_list_auto_show is True)

# 默认行为回归：开关开着时老口径不变
win._worker = None
win._config.duplex_stack_mode = True
win._config.printer_profiles["Fake Print"] = MANUAL
win._stack_list_user_closed = False
win._stack_queue_dialog.hide()
win._sync_stack_list_visible()
check("开关开着 + 攒批模式开 + 手动机型 ⇒ 恢复常驻（旧口径不变）",
      win._stack_queue_dialog.isVisible())
win._stack_list_user_closed = True
win._stack_queue_dialog.hide()
win._sync_stack_list_visible()
check("开关开着时「操作员主动收起」仍受尊重（不抢着弹回）",
      not win._stack_queue_dialog.isVisible())

# ============================================================
print("\n=== ⑥ 静态接线：闸门在任何 show 之前；恢复续打也按开关分流 ===")

check("_print_list_auto_show 用 getattr 兜底（半初始化环境不炸）",
      'getattr(self._config, "print_list_auto_show", True)' in _body("_print_list_auto_show"))

visible = _body("_sync_stack_list_visible")
_i_gate = visible.find("if not self._print_list_auto_show():")
_i_show = visible.find("self._show_stack_queue_dialog(dlg, activate=False)")
check("_sync_stack_list_visible 里有开关闸门",
      _i_gate >= 0)
check("闸门在自动 show 之前（否则关了也白关）",
      _i_gate >= 0 and _i_show > _i_gate, f"gate={_i_gate} show={_i_show}")
check("闸门分支里绝不出现 _show_stack_queue_dialog",
      "self._show_stack_queue_dialog" not in visible[_i_gate:_i_show])

# 攒批续打恢复路径：不许绕过开关
_resume = ""
for _m in re.finditer(r"\n    def (\w+)\(", SRC):
    _b = _body(_m.group(1))
    if "已恢复上次的攒批纸叠" in _b:
        _resume = _b
        _rname = _m.group(1)
        break
check("找到「已恢复上次的攒批纸叠」所在方法",
      _resume != "", _rname if _resume else "未找到：恢复路径被改名/删除？")
check("恢复路径按开关分流（关掉时不弹窗）",
      "if self._print_list_auto_show():" in _resume)
check("恢复路径关掉分支给了手动入口提示",
      "已关 ⇒ 点底部" in _resume)

check("printer_config.py 里有字段与序列化",
      "print_list_auto_show: bool = True" in
      open(os.path.join(LOCAL_TOOL, "printer_config.py"), encoding="utf-8").read())
check("新增复选框没有直接构造 QCheckBox",
      "QCheckBox(\"🖨 打印列表自动弹出\")" not in SRC)

print("\n" + ("PASS 全部通过" if ok_all else "FAIL 有失败项"))
sys.exit(0 if ok_all else 1)
