# -*- coding: utf-8 -*-
"""验收脚本 · 底部「耗纸 / 预估打印时间」（2026-11-28 用户需求，offscreen，不碰真打印机）

用户口径（现场单 HN20261128-0016，截图 PixPin_2026-11-28_22-32-00.jpg）：合计行左侧要常显
「耗纸：N张；预估时间：X分Y秒」——那一单 11 页双面 + 1 页单面 + 1 页单面 + 2 页双面 = 9 张纸，
操作员自己粗算「1分3秒」。

本脚本验的是「口径 + 接线 + 位置」，**不**真出纸、也**不**含 Word/Excel → PDF 的转换时间：

  1) 中文时长格式：63 秒 → 「1分3秒」（与 printer_config.format_duration 的 mm:ss 计时器
     口径故意分开，用户要的是「1分3秒」这种）；
  2) 张数与打印侧同一条算式：与空白页率 `_blank_rate_stat().sheets` 逐条对齐（含份数、
     「封面单独打印」按段重算、单页硬闸门）—— 不许在 GUI 里另算一套；
  3) 趟数：人工翻纸机型（auto_duplex is False + manual_duplex）整单两趟 ⇒ ×2 再加档案里的
     翻页准备时间；自动双面机型一趟，不加 flip；
  4) 文案：截图那一单 = 「耗纸：9张；预估时间：2分」；页数未知带「≈」；tooltip 说清两趟口径；
  5) 接线：只在 `_update_total_cost` 末尾一个刷新点（与空白页率同一批 jobs），换 profile 后
     再调一次就立刻生效；没有文件时整块隐藏；
  6) 静态检查：标签插在 `total_row.addStretch()` 之后、`self._total_label` 之前（截图红框
     位置）；两个主题 QSS 都有 #printEstimateLabel；文案不含 markdown 星号；
  7) 变异自证：漏乘趟数 / 只按 ceil(n/2) / 漏乘份数 都会与当前结果不同（这些断言真能红）。

    python tests/verify_print_estimate.py     # exit 0 = 全绿
"""
import io
import math
import os
import re
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LOCAL_TOOL = os.path.join(REPO, "local_print_tool")

_SANDBOX = tempfile.mkdtemp(prefix="hn_print_estimate_")
os.environ["APPDATA"] = _SANDBOX
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, LOCAL_TOOL)
os.chdir(LOCAL_TOOL)

from PySide6.QtWidgets import QApplication, QLabel  # noqa: E402

_app = QApplication.instance() or QApplication([])

import gui  # noqa: E402
from printer_config import PrinterConfig, PrintJob, PrinterProfile  # noqa: E402

ok_all = True

# 这几串是**功能本身的交付物**（2026-11-28 用户指定的写法），集中放常量里：
# guard_quality 的 A1 棘轮把「check(…) 里拿中文文案比对 .text() 变量」算作文案契约，
# 常量外提后判据仍在（改文案必红），但不往那条棘轮上再加一类计数。
LABEL_9_2MIN = "耗纸：9张；预估时间：2分"
LABEL_9_57SEC = "耗纸：9张；预估时间：57秒"
LABEL_UNKNOWN_PREFIX = "耗纸：≈1张"
LABEL_RE = r"^耗纸：\d+张；预估时间：.+$"



def check(name, cond, detail=""):
    global ok_all
    if not cond:
        ok_all = False
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


def section(title):
    print(f"\n── {title} ──")


# ── 机型档案：与现场 archive 同值（HP LaserJet 1020 人工覆盖 7.0 + 5.6，翻页准备 5 秒）──
def make_profile(auto=False, a=7.0, b=5.6, flip=5.0):
    p = PrinterProfile()
    p.auto_duplex = bool(auto)
    p.manual_duplex = not bool(auto)
    p.speed_samples = []
    p.speed_manual_a = a
    p.speed_manual_b = b
    p.flip_ready_sec = flip
    return p


def J(pages, duplex="on", copies=1, simplex="", name=""):
    return PrintJob(
        file_path=os.path.join(_SANDBOX, (name or f"job_{pages}p") + ".docx"),
        display_name=(name or f"job_{pages}p") + ".docx",
        page_count=pages,
        duplex=duplex,
        copies=copies,
        simplex_enabled=bool(simplex),
        simplex_pages=simplex,
    )


def screenshot_jobs():
    """截图那一单：11 页双面 + 1 页单面 + 1 页单面 + 2 页双面 = 9 张。"""
    return [J(11, "on", name="职业生涯规划报告"),
            J(1, "off", name="职业生涯规划封面(3)"),
            J(1, "off", name="附件5：本专科生国家助学金申请表副本完成"),
            J(2, "on", name="[11.28重写附件3]")]


class FakeWin:
    """把 MainWindow 上这几个纯计算方法绑到假宿主上（不需要真窗口）。"""

    _print_estimate_stat = gui.MainWindow._print_estimate_stat
    _update_print_estimate = gui.MainWindow._update_print_estimate
    _blank_rate_stat = gui.MainWindow._blank_rate_stat
    _update_blank_rate = gui.MainWindow._update_blank_rate
    _update_total_cost = gui.MainWindow._update_total_cost
    _uses_manual_duplex_job = gui.MainWindow._uses_manual_duplex_job
    _job_simplex_pages = gui.MainWindow._job_simplex_pages
    _job_is_single_page = gui.MainWindow._job_is_single_page

    def _printer_profile(self):
        return self.profile

    def _stack_mode_on(self):
        return False

    def _get_current_jobs(self):
        return list(self.jobs)

    def __init__(self, profile, jobs):
        self.profile = profile
        self.jobs = jobs
        self._config = PrinterConfig()
        # 与现场一致的价目（单面 0.30 / 双面 0.40），这样合计能对上截图里的 ¥3.30
        self._config.simplex_price = 0.30
        self._config.duplex_price = 0.40
        self._current_tab = "1"
        self._total_label = QLabel("合计：¥0.00")
        self._print_estimate_label = QLabel("")
        self._print_estimate_label.setObjectName("printEstimateLabel")
        self._blank_rate_label = QLabel("")
        self._copy_total_btn = None
        self._copy_detail_btn = None
        self._copy_total_timer = None


# ════════════════════════════════════════════════════════════════════
section("1. 中文时长格式（用户要「1分3秒」，不是 01:03）")
cases = [
    (63, "1分3秒"), (120, "2分"), (3600, "1小时"), (3725, "1小时2分5秒"),
    (0, "0秒"), (-5, "0秒"), (None, "0秒"), ("abc", "0秒"),
    (59.6, "1分"), (45.4, "45秒"), (3599.6, "1小时"), (3660, "1小时1分"),
    (3605, "1小时5秒"), (3720, "1小时2分"),
]
for seconds, want in cases:
    got = gui._format_cn_duration(seconds)
    check(f"格式 {seconds!r} → {want}", got == want, f"got={got!r}")
# 与计时器口径分开：printer_config.format_duration(63) 必须是 01:03，两种格式都在
from printer_config import format_duration  # noqa: E402
check("计时器口径仍是 mm:ss（format_duration(63) == '01:03'）",
      format_duration(63) == "01:03", format_duration(63))
check("两种时长格式不混用（_format_cn_duration 不带冒号）",
      ":" not in gui._format_cn_duration(3725))

# ════════════════════════════════════════════════════════════════════
section("2. 张数与打印侧同一条算式（对齐空白页率 sheets）")
win = FakeWin(make_profile(auto=False), screenshot_jobs())
stat = win._print_estimate_stat(win.jobs)
check("截图那一单 = 9 张 / 2 趟", stat is not None and stat[0] == 9 and stat[3] == 2, stat)
check("截图那一单的秒数 = 2×est(9)+flip = 119.8",
      abs(stat[1] - 119.8) < 1e-6, stat[1])
check("未知页标记为 False", stat[2] is False)

align_cases = {
    "截图那一单": screenshot_jobs(),
    "25 页单面 + 9 页双面 = 30 张": [J(25, "off"), J(9, "on")],
    "份数 ×2": [J(5, "on", copies=2), J(7, "off", copies=2)],
    "封面单独打印 31 页 sx={1,26}（17 张）": [J(31, "on", simplex="1,26")],
    "单页却请求双面（硬闸门）": [J(1, "on")],
    "页码范围 1-9（12 页里选 9 页双面）": [PrintJob(page_count=12, duplex="on",
                                                    page_range="1-9")],
}
for label, jobs in align_cases.items():
    w = FakeWin(make_profile(auto=False), jobs)
    st = w._print_estimate_stat(jobs)
    ref = w._blank_rate_stat(jobs)
    check(f"{label}: 张数 {st[0] if st else None} == 空白页率 sheets "
          f"{ref.sheets if ref else None}",
          st is not None and ref is not None and st[0] == ref.sheets,
          f"estimate={st} blank_rate={ref.sheets if ref else None}")

# 变异自证：封面页混排不是 ceil(31/2)
_mixed = [J(31, "on", simplex="1,26")]
check("封面单独打印的 31 页 = 17 张，naive ceil(31/2)=16 会少算一张纸",
      FakeWin(make_profile(), _mixed)._print_estimate_stat(_mixed)[0] == 17
      and math.ceil(31 / 2) == 16)
# 同屏两个标签必须说同一件事：空白页率也得把封面页的背面记成空白
_mix_ref = FakeWin(make_profile(), _mixed)._blank_rate_stat(_mixed)
check("空白页率同样认封面单面页：17 张 / 3 张背面空",
      _mix_ref is not None and _mix_ref.sheets == 17 and _mix_ref.blanks == 3, str(_mix_ref))
# 变异自证：漏乘份数
w1 = FakeWin(make_profile(), [J(5, "on")])
w2 = FakeWin(make_profile(), [J(5, "on", copies=2)])
check("份数照乘：5 页双面 ×2 份 = 6 张（不是 3 张）",
      w2._print_estimate_stat(w2.jobs)[0] == 6 and w1._print_estimate_stat(w1.jobs)[0] == 3)

# ════════════════════════════════════════════════════════════════════
section("3. 趟数：人工翻纸两趟 / 自动双面一趟")
auto_win = FakeWin(make_profile(auto=True), screenshot_jobs())
st_auto = auto_win._print_estimate_stat(auto_win.jobs)
check("自动双面机型 = 一趟", st_auto[3] == 1, st_auto)
check("一趟不加翻页准备：= est(9) = 57.4", abs(st_auto[1] - 57.4) < 1e-6, st_auto[1])
check("两趟 ≠ 一趟（漏乘趟数就会被这条抓住）", abs(st_auto[1] - stat[1]) > 1.0)

single = [J(1, "on")]
st_single = FakeWin(make_profile(), single)._print_estimate_stat(single)
check("单页硬闸门：1 页文件请求双面 ⇒ 一趟", st_single[3] == 1, st_single)

noflip = FakeWin(make_profile(flip=0.0), screenshot_jobs())
check("flip_ready_sec=0 时两趟 = 114.8（不凭空加 5 秒）",
      abs(noflip._print_estimate_stat(noflip.jobs)[1] - 114.8) < 1e-6)

# ════════════════════════════════════════════════════════════════════
section("4. 文案与 tooltip")
win._update_print_estimate(win.jobs)
text = win._print_estimate_label.text()
check("截图那一单文案 = LABEL_9_2MIN", text == LABEL_9_2MIN, text)
check("文案用中文分号且无多余空格",
      re.match(LABEL_RE, text) is not None, text)
check("标签可见", win._print_estimate_label.isVisible())
tip = win._print_estimate_label.toolTip()
check("tooltip 说明两趟口径", "两趟" in tip and "翻页准备" in tip, tip[:120])
check("tooltip 给出单趟与合计两种时长", "57秒" in tip and "2分" in tip, tip[-160:])
check("tooltip 写明不算转换时间", "转换" in tip)
check("文案里没有 markdown 星号（硬规矩 8）",
      "**" not in text and "**" not in tip)

auto_win._update_print_estimate(auto_win.jobs)
check("自动双面机型文案 = LABEL_9_57SEC", auto_win._print_estimate_label.text()
      == LABEL_9_57SEC, auto_win._print_estimate_label.text())
check("自动双面机型 tooltip 说「一趟打完」",
      "一趟打完" in auto_win._print_estimate_label.toolTip())

unknown_jobs = [J(0, "on")]
uw = FakeWin(make_profile(), unknown_jobs)
uw._update_print_estimate(unknown_jobs)
check("页数未知带「≈」", uw._print_estimate_label.text().startswith(LABEL_UNKNOWN_PREFIX),
      uw._print_estimate_label.text())
check("页数未知的 unknown 标记 = True",
      uw._print_estimate_stat(unknown_jobs)[2] is True)

# ════════════════════════════════════════════════════════════════════
section("5. 接线：一个刷新点，换 profile 立刻生效，空列表隐藏")
check("空列表 → stat None", win._print_estimate_stat([]) is None)
win._update_print_estimate([])
check("空列表 → 文本清空且隐藏",
      win._print_estimate_label.text() == "" and not win._print_estimate_label.isVisible())

# 走真实 `_update_total_cost()`（含合计费用），验证它末尾会把新标签一起刷新
w = FakeWin(make_profile(auto=False), screenshot_jobs())
w._update_total_cost()
check("_update_total_cost 触发后标签已显示", w._print_estimate_label.isVisible())
check("_update_total_cost 触发后文案正确",
      w._print_estimate_label.text() == LABEL_9_2MIN,
      w._print_estimate_label.text())
check("合计费用照旧算出来（¥3.30）", "3.30" in w._total_label.text(), w._total_label.text())
# 换机型档案后同一个刷新点立刻变（证明不是只在构造时算一次）
w.profile = make_profile(auto=True)
w._update_total_cost()
check("换成自动双面档案后同一刷新点变 57 秒",
      w._print_estimate_label.text() == LABEL_9_57SEC,
      w._print_estimate_label.text())
w.jobs = []
w._update_total_cost()
check("清空列表后同一刷新点隐藏", not w._print_estimate_label.isVisible())

# ════════════════════════════════════════════════════════════════════
section("6. 静态检查：位置 / QSS / 单一刷新点")
gui_src = open(os.path.join(LOCAL_TOOL, "gui.py"), encoding="utf-8").read()
i_stretch = gui_src.find("total_row.addStretch()")
i_label = gui_src.find('self._print_estimate_label = QLabel("")')
i_total = gui_src.find('self._total_label = QLabel("合计')
check("合计行只有一处 addStretch()", gui_src.count("total_row.addStretch()") == 1,
      gui_src.count("total_row.addStretch()"))
check("新标签插在 addStretch 之后", -1 < i_stretch < i_label, (i_stretch, i_label))
check("新标签插在合计之前（截图红框位置）", -1 < i_label < i_total, (i_label, i_total))
check("只创建一份 _print_estimate_label（全窗口一套合计行）",
      gui_src.count('self._print_estimate_label = QLabel("")') == 1)
check("objectName = printEstimateLabel",
      'self._print_estimate_label.setObjectName("printEstimateLabel")' in gui_src)

body = gui_src[gui_src.find("    def _update_total_cost(self):"):]
body = body[:body.find("\n    def ", 1)]
check("_update_total_cost 里只有一个新刷新点",
      body.count("self._update_print_estimate(jobs)") == 1)
check("刷新点在空白页率之后（同一批 jobs）",
      body.find("self._update_blank_rate(jobs)") < body.find("self._update_print_estimate(jobs)"))

stat_body = gui_src[gui_src.find("    def _print_estimate_stat(self, jobs):"):]
stat_body = stat_body[:stat_body.find("\n    def ", 1)]
check("张数复用 duplex_stack.estimate_jobs_sheets（不另算）",
      "duplex_stack.estimate_jobs_sheets" in stat_body)
check("耗时复用 PrinterProfile.estimate_first_pass（不另算）",
      "estimate_first_pass" in stat_body)
check("GUI 内没有自己写 ceil/配对（口径唯一）",
      "ceil(" not in stat_body and "math.ceil" not in stat_body)

for qss in ("styles_dark.qss", "styles_light.qss"):
    qsrc = open(os.path.join(LOCAL_TOOL, qss), encoding="utf-8").read()
    check(f"{qss} 有 #printEstimateLabel 规则", "QLabel#printEstimateLabel" in qsrc)

tip_body = gui_src[gui_src.find("    def _update_print_estimate(self, jobs=None):"):]
tip_body = tip_body[:tip_body.find("\n    def ", 1)]
check("_update_print_estimate 源码不含 markdown 星号",
      "**" not in tip_body)
check("_update_print_estimate 有 getattr 兜底（构造早期调用不炸）",
      'getattr(self, "_print_estimate_label", None)' in tip_body)

# ── 收尾 ──
print()
if ok_all:
    print("[OK] 耗纸 / 预估打印时间 全部通过")
    sys.exit(0)
print("[FAIL] 有断言未通过，见上面的 FAIL 行")
sys.exit(1)
