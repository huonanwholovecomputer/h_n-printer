# -*- coding: utf-8 -*-
"""渲染「🖐 攒批列表」常驻窗的示例截图（Qt offscreen，不碰真打印机、不写真实配置）。

为什么要这个脚本：现场问「打印列表和攒批列表到底是不是合并的」时，光看代码说不清，
直接把 `gui.StackQueueDialog` 用**真实控件 + 真实 render()** 渲染成图，比手工画示意图可靠
（列宽、列名、状态色、略显隐逻辑全部来自产线代码）。

跑法（不需要打印机）：
    python local_print_tool/tools/preview_stack_window.py

产物：
    截图展示/攒批列表示例_三块并存.png       手工双面机攒批中：已攒 + 等待打印 + 本机队列
    截图展示/攒批列表示例_只有本机队列.png   自动双面机（无攒批）：只剩第三块

注意：本脚本只读真实代码、只往临时目录写纸叠清单（APPDATA 已指到沙箱），
不调用任何 pdf_printer / print_* 入口。
"""
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


def make_job(name, pages=4, duplex="on", task_id=0, order_id=0):
    return PrintJob(file_path=os.path.join(_SANDBOX, name), copies=1, duplex=duplex,
                    page_range="", page_count=pages, orientation="portrait",
                    engine="pdf", display_name=name, task_id=task_id, order_id=order_id)


def build_stack():
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
    """右栏「等待打印」：两种真实排队原因（等整叠第 2 趟 / 容量不足）。"""
    return [
        {"reason": "等这一叠打完第 2 趟才放行", "tab_key": "1",
         "jobs": [make_job("扫描件-第 3 批.pdf", pages=8, duplex="on")]},
        {"reason": "容量不足（本叠 5 张 + 该单 12 张 > 上限 90 张）", "tab_key": "1",
         "jobs": [make_job("教辅-单元测试.docx", pages=12, duplex="on"),
                  make_job("答案页.docx", pages=6, duplex="off")]},
    ]


def build_local_rows():
    """第三块「本机队列」：进行中 1 + 等待中 2（文案照抄 `_local_queue_rows()` 的真实口径）。"""
    return [
        {"state": "第 2 趟进行中", "source": "本机点击",
         "content": "整叠 2 单 / 5 张纸（翻完纸打第 2 趟）", "sheets": "5",
         "eta": "01:12 · 21:44", "active": True},
        {"state": "等待中", "source": "云端自动",
         "content": "扫描件-第 3 批.pdf", "sheets": "4", "eta": "00:31 · 21:46",
         "active": False},
        {"state": "等待中（预约）", "source": "预约",
         "content": "预约单 #12 · 2 个文件", "sheets": "—", "eta": "预约 21:50",
         "active": False},
    ]


def _shot(dlg, filename, height=880):
    dlg.resize(1320, height)
    dlg.show()
    for _ in range(6):          # 让布局/换行/列宽全部结算完再抓图
        _app.processEvents()
    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, filename)
    if not dlg.grab().save(path):
        raise SystemExit(f"保存失败：{path}")
    dlg.close()
    print(f"已生成：{path}")
    return path


def main():
    # 场景 A：手工双面机攒批中 —— 三块同时在场（左栏已攒 / 右栏等待打印 / 下方本机队列）
    dlg = gui.StackQueueDialog()
    dlg.render(build_stack(), busy=False, wait_requests=build_wait(),
               printing_text="第 1 趟已打完 · 等人工把整叠搬回进纸盒")
    dlg.set_local_queue(build_local_rows())
    _shot(dlg, "攒批列表示例_三块并存.png")

    # 场景 B：自动双面机（没有攒批）—— 同一个常驻窗，只剩第三块「本机队列」
    dlg2 = gui.StackQueueDialog()
    dlg2.render(ds.DuplexStack(capacity=90), busy=False, wait_requests=[],
                printing_text="")
    dlg2.set_local_queue(build_local_rows()[:2])
    _shot(dlg2, "攒批列表示例_只有本机队列.png")


if __name__ == "__main__":
    main()
