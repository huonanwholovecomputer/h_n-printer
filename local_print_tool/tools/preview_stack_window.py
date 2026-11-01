# -*- coding: utf-8 -*-
"""渲染「攒批列表 / 打印列表」常驻窗的各场景示例截图（Qt offscreen，不碰真打印机）。

为什么要这个脚本：现场问「打印列表和攒批列表到底是不是合并的」时，光看代码说不清，
直接把 `gui.StackQueueDialog` 用**真实控件 + 真实 render()** 渲染成图，比手工画示意图可靠
（列宽、列名、状态色、显隐逻辑全部来自产线代码）。2026-11 又加了「逐个模拟」：
三种机型/模式（自动双面 / 手工双面+攒批开 / 手工双面+攒批关）× 单任务 / 多任务，
每个场景出一张图 + 一行README式的读数，改造前后各跑一次就能对着看。

跑法（不需要打印机）：
    python local_print_tool/tools/preview_stack_window.py --tag 改造前
    python local_print_tool/tools/preview_stack_window.py --tag 改造后
    python local_print_tool/tools/preview_stack_window.py --tag 改造后 --only stack_multi

产物（截图展示/）：
    列表展示_<tag>_<case>.png   每个场景一张，尺寸 = 该场景**自然最小尺寸**（空格子越少图越小）
    另外保留两张老示例：攒批列表示例_三块并存.png / 攒批列表示例_只有本机队列.png

注意：本脚本只读真实代码、只往临时目录写纸叠清单（APPDATA 已指到沙箱），
不调用任何 pdf_printer / print_* 入口。
"""
import inspect
import io
import os
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LOCAL_TOOL = os.path.join(REPO, "local_print_tool")

_SANDBOX = tempfile.mkdtemp(prefix="hn_preview_stack_")
os.environ["APPDATA"] = _SANDBOX                    # 纸叠清单/配置 → 沙箱，不碰真实数据
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, LOCAL_TOOL)
os.chdir(LOCAL_TOOL)

from PySide6.QtGui import QFont, QFontDatabase  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

_app = QApplication.instance() or QApplication([])

# offscreen 平台不会自己找系统字体（Qt 已不自带字体 ⇒ 不注册的话整屏都是豆腐块），
# 显式把 Windows 的中文字体挂进来，截图才看得懂。
for _f in ("C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/simhei.ttf",
           "C:/Windows/Fonts/segoeui.ttf", "C:/Windows/Fonts/seguisym.ttf",
           "C:/Windows/Fonts/seguiemj.ttf"):
    if os.path.isfile(_f):
        QFontDatabase.addApplicationFont(_f)
_ui_font = QFont("Microsoft YaHei UI", 9)
# 列里带 🔒/🔓/▶ 这类图标，YaHei 没有这些字形 ⇒ 交回退链给 emoji/符号字体
_ui_font.setFamilies(["Microsoft YaHei UI", "Segoe UI Emoji", "Segoe UI Symbol", "SimHei"])
_app.setFont(_ui_font)
import duplex_stack as ds          # noqa: E402
import gui                         # noqa: E402
from printer_config import PrintJob  # noqa: E402

OUT_DIR = os.path.join(REPO, "截图展示")

TAG = "示例"
ONLY = ""


def make_job(name, pages=4, duplex="on", task_id=0, order_id=0):
    return PrintJob(file_path=os.path.join(_SANDBOX, name), copies=1, duplex=duplex,
                    page_range="", page_count=pages, orientation="portrait",
                    engine="pdf", display_name=name, task_id=task_id, order_id=order_id)


# ── 纸叠 / 右栏排队 / 打印列表 三份数据（照抄产线口径，不自己编状态名）──

def build_stack_two():
    """两个已攒单：单 1 = 双面 4 页 + 单面 3 页（单面那份已登记取走），单 2 = 双面 6 页。"""
    stack = ds.DuplexStack(capacity=90, printer_name="HP LaserJet 1020")
    stack.new_batch("batch_preview")
    stack.first = "odd"
    stack.rotate = True

    e1 = ds.StackEntry(
        seq=1, title="HN20261031-0003 · 2 个文件", tab_key="1",
        order_id=3, order_number="HN20261031-0003",
        page_count=7, sheet_count=5, original_sheet_count=5,
        state=ds.STATE_DONE, rotate=True, first="odd",
        pdf="entry_0001.pdf",
        created_at="2026-10-31 21:02:11",
        file_plans=[
            {"label": "合同-2026复审.pdf", "pages": [1, 2, 3, 4], "duplex": True,
             "file_pages": [1, 2, 3, 4]},
            {"label": "报价单.xlsx", "pages": [5, 6, 7], "duplex": False,
             "file_pages": [1, 2, 3]},
        ],
        taken_plans=[1],            # 单面那份：第 2 趟全是空白占位 ⇒ 纸已被取走
    )
    e2 = ds.StackEntry(
        seq=2, title="HN20261031-0004 · 1 个文件", tab_key="2",
        order_id=4, order_number="HN20261031-0004",
        page_count=6, sheet_count=3, original_sheet_count=3,
        state=ds.STATE_DONE, rotate=True, first="odd",
        pdf="entry_0002.pdf",
        created_at="2026-10-31 21:18:40",
        file_plans=[
            {"label": "说明书（共 6 页）.docx", "pages": [1, 2, 3, 4, 5, 6], "duplex": True,
             "file_pages": [1, 2, 3, 4, 5, 6]},
        ],
    )
    stack.entries = [e1, e2]
    return stack


def build_wait():
    """右栏「等待打印」：两种真实排队原因（等整叠第 2 趟 / 容量不足）。

    数字必须自洽 —— 旧示例写「本叠 5 张 + 该单 12 张 > 上限 90 张」，5+12=17 远小于 90，
    结论和数字对不上（现场看一眼就不知道该信哪个）。
    """
    return [
        {"reason": "等这一叠打完第 2 趟才放行", "tab_key": "1",
         "jobs": [make_job("扫描件-第 3 批.pdf", pages=8, duplex="on")]},
        {"reason": "容量不足（本叠 84 张 + 该单 12 张 > 上限 90 张）", "tab_key": "1",
         "jobs": [make_job("教辅-单元测试.docx", pages=12, duplex="on"),
                  make_job("答案页.docx", pages=6, duplex="off")]},
    ]


def rows_auto_single():
    return [{"state": "进行中", "source": "云端自动", "content": "教辅-单元测试.docx",
             "sheets": "6", "eta": "00:41 · 21:33", "active": True}]


def rows_auto_multi():
    return [{"state": "进行中", "source": "云端自动", "content": "教辅-单元测试.docx",
             "sheets": "6", "eta": "00:41 · 21:33", "active": True},
            {"state": "等待中", "source": "本机点击", "content": "扫描件-第 3 批.pdf",
             "sheets": "4", "eta": "01:12 · 21:44", "active": False},
            {"state": "等待中（预约）", "source": "预约", "content": "预约单 #12 · 2 个文件",
             "sheets": "—", "eta": "预约 21:50", "active": False}]


def rows_stack_single():
    return [{"state": "第 1 趟进行中（攒批）", "source": "本机点击",
             "content": "合同-2026复审.pdf", "sheets": "2", "eta": "00:36 · 21:29",
             "active": True}]


def rows_stack_multi():
    """进行中 1 + 等待中 2（文案照抄 `_local_queue_rows()` 的真实口径）。"""
    return [{"state": "第 2 趟进行中", "source": "本机点击",
             "content": "整叠 2 单 / 5 张纸（翻完纸打第 2 趟）", "sheets": "5",
             "eta": "01:12 · 21:44", "active": True},
            {"state": "等待中", "source": "云端自动",
             "content": "扫描件-第 3 批.pdf", "sheets": "4", "eta": "00:31 · 21:46",
             "active": False},
            {"state": "等待中（预约）", "source": "预约",
             "content": "预约单 #12 · 2 个文件", "sheets": "—", "eta": "预约 21:50",
             "active": False}]


def rows_manual_single():
    return [{"state": "第 1 趟进行中", "source": "本机点击", "content": "合同-2026复审.pdf",
             "sheets": "2", "eta": "00:36 · 21:29", "active": True}]


def rows_manual_multi():
    return [{"state": "第 1 趟进行中", "source": "本机点击", "content": "合同-2026复审.pdf",
             "sheets": "2", "eta": "00:36 · 21:29", "active": True},
            {"state": "等待中", "source": "云端自动", "content": "扫描件-第 3 批.pdf",
             "sheets": "4", "eta": "00:31 · 21:46", "active": False},
            {"state": "等待中", "source": "本机点击", "content": "说明书（共 6 页）.docx",
             "sheets": "3", "eta": "01:04 · 21:47", "active": False}]


EMPTY_STACK = lambda: ds.DuplexStack(capacity=90, printer_name="HP LaserJet 1020")  # noqa: E731

# 场景 = 三种机型/模式 × 单任务/多任务。ok=攒批适用（不支持自动双面）, mode=攒批模式开
CASES = [
    ("auto_single", dict(ok=False, mode=False, stack=EMPTY_STACK, waits=lambda: [],
                         rows=rows_auto_single, printing="")),
    ("auto_multi", dict(ok=False, mode=False, stack=EMPTY_STACK, waits=lambda: [],
                        rows=rows_auto_multi, printing="")),
    ("stack_single", dict(ok=True, mode=True, stack=EMPTY_STACK, waits=lambda: [],
                          rows=rows_stack_single, printing="正在打某单的第 1 趟")),
    ("stack_multi", dict(ok=True, mode=True, stack=build_stack_two, waits=build_wait,
                         rows=rows_stack_multi,
                         printing="第 1 趟已打完 · 等人工把整叠搬回进纸盒")),
    ("off_single", dict(ok=True, mode=False, stack=EMPTY_STACK, waits=lambda: [],
                        rows=rows_manual_single, printing="")),
    ("off_multi", dict(ok=True, mode=False, stack=EMPTY_STACK, waits=lambda: [],
                       rows=rows_manual_multi, printing="")),
]

_RENDER_ARGS = set(inspect.signature(gui.StackQueueDialog.render).parameters)


def _render(dlg, case: dict):
    """按当前代码支持的形参调用 render（改造前没有 stack_mode/stack_applicable 两个形参）。"""
    kw = {}
    if "stack_applicable" in _RENDER_ARGS:
        kw["stack_applicable"] = case["ok"]
        kw["stack_mode"] = case["mode"]
    dlg.render(case["stack"](), busy=False, wait_requests=case["waits"](),
               printing_text=case["printing"], **kw)
    dlg.set_local_queue(case["rows"]())


def _visible(w) -> bool:
    return (not w.isHidden()) if w is not None else False


def _reading(dlg) -> str:
    """一行"现场读数"：窗口标题 / 汇总句 / 在场区块 / 一行提示 / 在场按钮。"""
    groups = []
    for name, w in (("已攒", getattr(dlg, "_left_group", None)),
                    ("等待打印", getattr(dlg, "_right_group", None)),
                    ("打印列表", getattr(dlg, "_local_group", None))):
        if _visible(w):
            groups.append(name)
    empty = getattr(dlg, "_empty_line", None)
    if _visible(empty) and empty.text():
        groups.append(f"灰字说明「{empty.text()}」")
    hint = getattr(dlg, "_hint", None)
    hint_text = hint.text() if (_visible(hint) and hint is not None) else "（无）"
    btns = [b.text().replace("\u200b", "")[:10] for b in
            (getattr(dlg, "_btn_take", None), getattr(dlg, "_btn_start", None),
             getattr(dlg, "_btn_abandon", None), getattr(dlg, "_btn_refresh", None),
             getattr(dlg, "_btn_close", None)) if _visible(b)]
    summary = getattr(dlg, "_summary", None)
    return (f"标题「{dlg.windowTitle()}」\n"
            f"    汇总：{summary.text() if summary else ''}\n"
            f"    区块：{' + '.join(groups) or '（无）'}\n"
            f"    提示：{hint_text}\n"
            f"    按钮：{' / '.join(btns) or '（无）'}")


def _shot(dlg, filename):
    """按**该场景的自然最小尺寸**出图（不硬塞 1320 宽，否则看不出空格子少没少）。"""
    dlg.show()
    for _ in range(4):
        _app.processEvents()
    hint = dlg.minimumSizeHint()
    w = max(820, min(1400, int(hint.width())))
    h = max(240, min(920, int(hint.height())))
    dlg.resize(w, h)
    for _ in range(6):          # 让布局/换行/列宽全部结算完再抓图
        _app.processEvents()
    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, filename)
    if not dlg.grab().save(path):
        raise SystemExit(f"保存失败：{path}")
    real = (dlg.width(), dlg.height())
    dlg.close()
    print(f"已生成：{os.path.relpath(path, REPO)}  （{real[0]}x{real[1]}）")
    return path, real


def main(argv=None):
    global TAG, ONLY
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--tag" in argv:
        TAG = argv[argv.index("--tag") + 1]
    if "--only" in argv:
        ONLY = argv[argv.index("--only") + 1]

    print(f"=== 场景矩阵（tag={TAG}）===")
    for name, case in CASES:
        if ONLY and ONLY != name:
            continue
        dlg = gui.StackQueueDialog()
        _render(dlg, case)
        print(f"\n[{name}] {_reading(dlg)}")
        _shot(dlg, f"列表展示_{TAG}_{name}.png")

    # 老示例（保持不变，文档/对话里引用过）：
    if not ONLY:
        dlg = gui.StackQueueDialog()
        _render(dlg, [c for n, c in CASES if n == "stack_multi"][0])
        _shot(dlg, "攒批列表示例_三块并存.png")
        dlg2 = gui.StackQueueDialog()
        _render(dlg2, [c for n, c in CASES if n == "auto_multi"][0])
        _shot(dlg2, "攒批列表示例_只有本机队列.png")


if __name__ == "__main__":
    main()
