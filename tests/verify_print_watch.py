# -*- coding: utf-8 -*-
"""外部打印监视验收（A 档最小版）：判定 / 页数回落 / 记账 / 原子写 / 接线。

不需要真打印机，不会向任何队列提交作业（唯一碰真机的地方是 ①b：只订阅 + EnumJobs，只读）。

必须钉住的十件事：
  ① ctypes 订阅接线：flags 含 ADD_JOB、按队列订阅、stop 后句柄全关、线程干净退出；
  ② 自家作业（HN| 前缀）**不许记账** —— 否则自家每一单被记第二遍账；
  ③ 虚拟打印机不记账（但要留日志），真实打印机记账；
  ④ 页数三级回落：TotalPages → PagesPrinted → unknown（1020 主机型驱动常全 0）；
     ④b DEVMODE 的**对象形状**（pywin32 给 PyDEVMODEW，属性访问，不是 dict）也要读得出
        单双面/彩色/份数 —— 只认 dict 的老写法在实机上让这三列永远是「待确认」；
  ⑤ 金额与 printer_config.calc_cost 同口径，且单价取配置对象（改价金额跟着变）；
  ⑥ 同一作业被多个事件扫到只记 1 条；
  ⑦ 账本原子写、损坏文件容错（.corrupt 副本）、2000 条上限丢最旧；
  ⑧ CSV 导出列正确；
  ⑨ pdf_printer 的 tag_self_title 接线（静态 + 导入失败回退不抛）；
  ⑩ gui.py 静态接线（菜单可勾选项 / 对话框 / Signal / 非模态补录提示）；
  ⑪ 自动记账（2026-10-29）：页数与单双面都读到 ⇒ 直接「已确认」，缺一个才弹补录；
     人工点过的「已忽略」不许被自动记账翻回来。

用法: python tests/verify_print_watch.py   （exit 0 = 全部通过）
"""
import ast
import csv
import io
import json
import os
import re
import sys
import tempfile
import textwrap
import threading
import time
from datetime import datetime, timedelta

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL_TOOL = os.path.join(REPO, "local_print_tool")
TESTS = os.path.join(REPO, "tests")
sys.path.insert(0, LOCAL_TOOL)

import print_watch as pw            # noqa: E402
import printer_config as pconf      # noqa: E402

ok_all = True
TMP = tempfile.mkdtemp(prefix="hn_watch_verify_")


def check(name, cond, detail=""):
    global ok_all
    if not cond:
        ok_all = False
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))
    return cond


def section(title):
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


def new_store(tag):
    return pw.ExternalPrintStore(os.path.join(TMP, f"{tag}.json"))


def job(job_id=1, doc="报告.docx", user="顾客", total=0, printed=0, duplex=None,
        color=None, copies=None, printer="HP LaserJet 1020", submitted=None, size=1024):
    """造一条 JOB_INFO_2（pywin32 的 dict 形状）。"""
    devmode = {}
    if duplex is not None:
        devmode["Duplex"] = duplex
    if color is not None:
        devmode["Color"] = color
    if copies is not None:
        devmode["Copies"] = copies
    return {
        "JobId": job_id, "pDocument": doc, "pUserName": user, "TotalPages": total,
        "PagesPrinted": printed, "Size": size, "pDevMode": devmode or None,
        "pPrinterName": printer, "Submitted": submitted if submitted is not None else 0,
    }


def not_virtual(_name):
    return False


def only_pdf_virtual(name):
    return "PDF" in str(name) or "XPS" in str(name)


# ══════════════════════════════════════════════════════════════════
section("① ctypes 订阅接线（伪造 spooler：flags / 按队列订阅 / stop 关句柄 / 无线程残留）")
# ══════════════════════════════════════════════════════════════════

check("SELF_TAG 前缀是 HN|", pw.SELF_TAG == "HN|", f"实际 {pw.SELF_TAG!r}")
check("默认订阅 flags 含 ADD_JOB（新增作业）",
      bool(pw.WATCH_FLAGS & pw.PRINTER_CHANGE_ADD_JOB),
      f"WATCH_FLAGS=0x{pw.WATCH_FLAGS:04X}")
check("默认订阅 flags 含 SET_JOB / DELETE_JOB（页数在 SET 里过来、删除时要收尾）",
      bool(pw.WATCH_FLAGS & pw.PRINTER_CHANGE_SET_JOB)
      and bool(pw.WATCH_FLAGS & pw.PRINTER_CHANGE_DELETE_JOB))
check("导入 print_watch 不启动线程（无副作用）",
      not any(t.name == "hn-external-print-watch" for t in threading.enumerate()))


class FakeSpooler:
    """假 winspool：只记录调用，验证接线而不是验证 Windows。"""

    def __init__(self):
        self.opened, self.subs, self.closed, self.unsubs, self.waits = [], [], [], [], []

    def open(self, name):
        self.opened.append(name)
        return f"hpr::{name}"

    def close(self, h):
        self.closed.append(h)

    def subscribe(self, h, flags=pw.WATCH_FLAGS):
        self.subs.append((h, flags))
        return f"notif::{h}"

    def unsubscribe(self, n):
        self.unsubs.append(n)

    def wait(self, n, ms):
        self.waits.append((n, ms))
        time.sleep(min(max(ms, 0), 10) / 1000.0)   # 真等一会儿：让线程活在 wait 里而不是空转
        return 258


fake = FakeSpooler()
scanned = []
w_store = new_store("watch_a")
w_events = []
w = pw.PrintJobWatcher(w_store, enum_jobs=lambda p: scanned[-1] if scanned else [],
                       printers=["打印机A", "打印机B"], on_external=w_events.append,
                       on_log=lambda m: None, virtual_check=not_virtual,
                       spooler=fake, wait_ms=10)
scanned.append([job(job_id=11, doc="外部.docx", total=2)])
started = w.start()
check("start() 返回 True", started is True)
check("按队列名逐个订阅（整机订阅在实测里返回 INVALID_HANDLE_VALUE）",
      len(fake.subs) == 2 and sorted(fake.opened) == ["打印机A", "打印机B"],
      f"opened={fake.opened}")
check("订阅时把 WATCH_FLAGS 原样交给 FindFirstPrinterChangeNotification",
      all(flags == pw.WATCH_FLAGS and flags & pw.PRINTER_CHANGE_ADD_JOB
          for _h, flags in fake.subs),
      f"flags={[hex(f) for _h, f in fake.subs]}")
job_ids = []
for _ in range(150):                      # 最多等 3 秒（监视线程在真线程里扫）
    if w_store.known_job_keys():
        break
    time.sleep(0.02)
job_ids = sorted(w_store.known_job_keys())
check("监视线程在后台线程里扫到了作业（回调拿到 ExternalPrint）", len(w_events) == 1,
      f"记下 {job_ids}，回调 {len(w_events)} 次")
clean = w.stop()
check("stop() 返回 True（线程在超时内退出）", clean is True)
check("stop() 后线程已结束、无僵尸线程",
      not w.is_running()
      and not any(t.name == "hn-external-print-watch" for t in threading.enumerate()))
check("stop() 关闭全部通知句柄与打印机句柄（句柄不泄漏）",
      len(fake.unsubs) == 2 and len(fake.closed) == 2,
      f"unsubscribe={len(fake.unsubs)} close={len(fake.closed)}")
check("遍历队列时用的等待是有限时长的（到点也照扫）",
      fake.waits and all(ms is not None and ms >= 0 for _n, ms in fake.waits))


# ══════════════════════════════════════════════════════════════════
section("② 自家作业被排除（HN| 前缀不记账），外部作业照记")
# ══════════════════════════════════════════════════════════════════

now = datetime(2026, 11, 2, 12, 0, 0)
own = job(job_id=21, doc=pw.tag_self_title("作业单.pdf"), total=3)
outsider = job(job_id=22, doc="作业单.pdf", total=3)
st_b = new_store("self_b")
ok_own, why_own = pw.should_record(own, (), now, known_keys=st_b.known_job_keys(),
                                   virtual_check=not_virtual)
ok_out, why_out = pw.should_record(outsider, (), now, known_keys=st_b.known_job_keys(),
                                   virtual_check=not_virtual)
check("自家作业（HN|作业单.pdf）判定为不记账", ok_own is False and why_own == "self",
      f"should_record=({ok_own}, {why_own!r})")
check("同名但没前缀的文档判定为要记账（证明上一条不是恒 False）",
      ok_out is True and why_out == "ok", f"should_record=({ok_out}, {why_out!r})")
check("tag_self_title 幂等（已带前缀不叠第二层）",
      pw.tag_self_title("HN|a.pdf") == "HN|a.pdf" and pw.tag_self_title("a.pdf") == "HN|a.pdf")

wb = pw.PrintJobWatcher(st_b, enum_jobs=lambda p: [], printers=["HP LaserJet 1020"],
                        virtual_check=not_virtual, on_log=lambda m: None)
before = len(st_b)
got_own = wb.consider("HP LaserJet 1020", own, now)
got_out = wb.consider("HP LaserJet 1020", outsider, now)
check("自家作业走完整 consider() 也不落账（账本一增一不增）",
      got_own is None and got_out is not None and len(st_b) == before + 1,
      f"自家={got_own} 外部={got_out.job_id if got_out else None} 条数 {before}→{len(st_b)}")
check("落账的那条是外部作业（JobId 对得上）",
      [k[1] for k in sorted(st_b.known_job_keys())] == [22],
      f"{sorted(st_b.known_job_keys())}")


# ══════════════════════════════════════════════════════════════════
section("③ 虚拟打印机不记账（留一行日志）、真实打印机记账")
# ══════════════════════════════════════════════════════════════════

st_c = new_store("virtual_c")
logs_c = []
wc = pw.PrintJobWatcher(st_c, enum_jobs=lambda p: [], printers=[],
                        virtual_check=only_pdf_virtual, on_log=logs_c.append)
v_job = job(job_id=31, doc="a.pdf", total=1, printer="Microsoft Print to PDF")
r_job = job(job_id=32, doc="b.pdf", total=1, printer="HP LaserJet 1020")
ok_v, why_v = pw.should_record(v_job, (), now, virtual_check=only_pdf_virtual)
ok_r, why_r = pw.should_record(r_job, (), now, virtual_check=only_pdf_virtual)
check("虚拟打印机（Microsoft Print to PDF）不记账", ok_v is False and why_v == "virtual",
      f"should_record=({ok_v}, {why_v!r})")
check("真实打印机（HP LaserJet 1020）要记账", ok_r is True and why_r == "ok",
      f"should_record=({ok_r}, {why_r!r})")
wc.consider("Microsoft Print to PDF", v_job, now)
wc.consider("HP LaserJet 1020", r_job, now)
check("虚拟打印机队列一行都没进账本，真实队列进了 1 条",
      [k[1] for k in sorted(st_c.known_job_keys())] == [32],
      f"{sorted(st_c.known_job_keys())}")
check("虚拟打印机被跳过时**留了日志**（不是静默丢弃）",
      any("虚拟打印机" in m and "跳过" in m for m in logs_c), f"{logs_c}")
wc.consider("Microsoft Print to PDF", job(job_id=33, doc="c.pdf", total=1,
                                          printer="Microsoft Print to PDF"), now)
check("同一虚拟队列只记一次日志（不刷屏）",
      sum(1 for m in logs_c if "虚拟打印机" in m) == 1, f"{logs_c}")


# ══════════════════════════════════════════════════════════════════
section("④ 页数三级回落：TotalPages → PagesPrinted → unknown")
# ══════════════════════════════════════════════════════════════════

p1 = pw.decide_pages(job(total=5, printed=0))
p2 = pw.decide_pages(job(total=0, printed=4))
p3 = pw.decide_pages(job(total=0, printed=0))
check("① TotalPages>0 → 用 TotalPages（source=auto）", p1 == (5, pw.SOURCE_AUTO), f"{p1}")
check("② TotalPages=0 且 PagesPrinted>0 → 用 PagesPrinted（source=auto）",
      p2 == (4, pw.SOURCE_AUTO), f"{p2}")
check("③ 全为 0 → pages=0 / source=unknown（绝不拿 1 页糊过去）",
      p3 == (0, pw.SOURCE_UNKNOWN), f"{p3}")
p4 = pw.decide_pages({"JobId": 1, "TotalPages": "7", "PagesPrinted": None})
check("字段是字符串/None 也不崩（spooler 偶发给字符串）", p4 == (7, pw.SOURCE_AUTO), f"{p4}")
item_unknown = pw.build_external(job(job_id=41, total=0, printed=0, duplex=1), now)
check("页数未知时 build_external 的 pages=0、source=unknown",
      item_unknown.pages == 0 and item_unknown.pages_source == pw.SOURCE_UNKNOWN,
      f"pages={item_unknown.pages} source={item_unknown.pages_source}")
item_no_dm = pw.build_external({"JobId": 42, "pDocument": "x.pdf", "pDevMode": None}, now)
check("pDevMode=None 时不崩，单双面/彩色/份数都落到未确认默认值",
      item_no_dm.duplex == pw.DUPLEX_UNKNOWN and item_no_dm.color == 0
      and item_no_dm.copies == 1, f"{item_no_dm.duplex!r}/{item_no_dm.color}/{item_no_dm.copies}")


# ── ④b 真机形状：pywin32 的 DEVMODE 是对象（属性访问），不是 dict ──
# 2026-10-28 现场：只认 dict 的 devmode_of 让 1020 上四条外部作业全部「待确认」，
# 而账本里的 双面/单面 全是人工补录 —— 假作业写 dict，所以这个缺陷从没红过。

class FakeDevmode:
    """PyDEVMODEW 的仿件：只有属性，没有 dict 接口。"""

    def __init__(self, duplex=1, color=1, copies=1):
        self.Duplex = duplex
        self.Color = color
        self.Copies = copies


def job_obj(job_id=43, doc="外部Word.docx", total=2, duplex=2, color=1, copies=1):
    return {"JobId": job_id, "pDocument": doc, "pUserName": "顾客", "TotalPages": total,
            "PagesPrinted": 0, "Size": 2048, "pDevMode": FakeDevmode(duplex, color, copies),
            "pPrinterName": "HP LaserJet 1020", "Submitted": 0}


it_obj = pw.build_external(job_obj(), now)
check("属性对象形状的 DEVMODE：单双面/彩色/份数读得出（不是永远待确认）",
      it_obj.duplex == pw.DUPLEX_ON and it_obj.color == pw.COLOR_MONO
      and it_obj.copies == 1, f"{it_obj.duplex!r}/{it_obj.color}/{it_obj.copies}")
check("属性对象 commons：Duplex=1 → off、Copies=2 生效、字典形状行为不变",
      pw.duplex_mode_of(job_obj(duplex=1)) == pw.DUPLEX_OFF
      and pw.copies_of(job_obj(copies=2)) == 2
      and pw.duplex_mode_of(job(duplex=3)) == pw.DUPLEX_ON)
check("属性对象缺字段时仍不猜（Duplex=None → unknown）",
      pw.duplex_mode_of(job_obj(duplex=None)) == pw.DUPLEX_UNKNOWN)
check("不像 DEVMODE 的对象（没有这三个属性）当没有 DEVMODE，不崩",
      pw.duplex_mode_of({"JobId": 44, "pDevMode": object()}) == pw.DUPLEX_UNKNOWN
      and pw.copies_of({"JobId": 44, "pDevMode": object()}) == 1)
check("devmode_debug 能区分形状与取值（现场排查「驱动没写」vs「我们没读」的唯一依据）",
      "FakeDevmode" in pw.devmode_debug(job_obj(duplex=2))
      and "Duplex=2" in pw.devmode_debug(job_obj(duplex=2))
      and pw.devmode_debug({"JobId": 45, "pDevMode": None}) == "pDevMode=None")

# 真机对象（只读 GetPrinter，不提交作业）：拿得到就验证，拿不到就跳过
try:
    import win32print as _wp
    _name = None
    for _row in _wp.EnumPrinters(_wp.PRINTER_ENUM_LOCAL):
        _name = _row[2]
        if "LaserJet" in str(_name):
            break
    if not _name:
        raise RuntimeError("本机没有本地打印队列")
    _h = _wp.OpenPrinter(_name)
    try:
        _dm = _wp.GetPrinter(_h, 2)["pDevMode"]
    finally:
        _wp.ClosePrinter(_h)
    _real = {"JobId": 46, "TotalPages": 2, "pDevMode": _dm}
    check(f"真机 {_name} 的 PyDEVMODEW 能读出 Duplex（修复前恒为 unknown）",
          pw.duplex_mode_of(_real) in (pw.DUPLEX_ON, pw.DUPLEX_OFF),
          pw.devmode_debug(_real))
except Exception as _e:
    print(f"[SKIP] 真机 PyDEVMODE 检查跳过：{_e}")


# ── ④c 单页归一：1 页物理上只有一个面 ⇒ 一律单面（与打印管线的单页硬闸门同口径）──
# 2026-10-28 现场提问：单页文件在 Word 里选了双面，账本记成「双面」，和工具/云端口径不一致。
check("1 页 + DEVMODE 双面 ⇒ 记成单面（账本里不该出现打不出来的双面）",
      pw.duplex_mode_of(job(duplex=2)) == pw.DUPLEX_ON          # 原始 DEVMODE 仍如实读出
      and pw.normalize_duplex(1, pw.DUPLEX_ON) == pw.DUPLEX_OFF
      and pw.build_external(job(total=1, duplex=2), now).duplex == pw.DUPLEX_OFF)
check("页数未知(0)时不动：那是真不知道，不是 1 页，绝不猜",
      pw.normalize_duplex(0, pw.DUPLEX_ON) == pw.DUPLEX_ON
      and pw.build_external(job(total=0, duplex=2), now).duplex == pw.DUPLEX_ON)
check("多页双面照旧是双面；单面输入不会被翻成双面",
      pw.build_external(job(total=3, duplex=2), now).duplex == pw.DUPLEX_ON
      and pw.build_external(job(total=1, duplex=1), now).duplex == pw.DUPLEX_OFF)
_norm_store = pw.ExternalPrintStore(os.path.join(TMP, "normalize.json"))
_norm_store.add(pw.ExternalPrint(job_id=71, printer="HP LaserJet 1020", document="单页.pdf",
                                 pages=0, duplex=pw.DUPLEX_ON, copies=1))
_norm_item = _norm_store.set_pages(0, 1)
check("人工补录成 1 页后单双面跟着归一（set_pages 走同一条规则）",
      _norm_item is not None and _norm_item.duplex == pw.DUPLEX_OFF,
      f"{getattr(_norm_item, 'duplex', None)!r}")
_norm_store2 = pw.ExternalPrintStore(os.path.join(TMP, "normalize2.json"))
_norm_store2.add(pw.ExternalPrint(job_id=72, printer="HP LaserJet 1020", document="单页2.pdf",
                                  pages=0, duplex=pw.DUPLEX_UNKNOWN, copies=1))
_wholesale = pw.ExternalPrint(job_id=72, printer="HP LaserJet 1020", document="单页2.pdf",
                              pages=1, duplex=pw.DUPLEX_ON, copies=1,
                              detected_at=_norm_store2.load()[0].detected_at,
                              status=pw.STATUS_CONFIRMED)
_norm_store2.update(_wholesale)
check("整条替换（update）也归一：补录弹窗那条写回路径同样不许留「1 页双面」",
      _norm_store2.load()[0].duplex == pw.DUPLEX_OFF, f"{_norm_store2.load()[0].duplex!r}")
_hist = pw.ExternalPrint.from_dict({"job_id": 75, "printer": "HP LaserJet 1020", "pages": 1,
                                    "duplex": "on", "copies": 1, "status": "confirmed",
                                    "detected_at": "2026-10-28 18:19:58"})
check("旧账本里「1 页 + 双面」的历史记录**读出来**也归成单面（不用等下次写入）",
      _hist is not None and _hist.duplex == pw.DUPLEX_OFF, f"{getattr(_hist, 'duplex', None)!r}")
_one_on = pw.amount_for(pw.ExternalPrint(job_id=73, pages=1, duplex=pw.DUPLEX_ON), 0.3, 0.4)
_one_off = pw.amount_for(pw.ExternalPrint(job_id=74, pages=1, duplex=pw.DUPLEX_OFF), 0.3, 0.4)
check("1 页双面与 1 页单面金额等价（归一不改钱，只是标签对齐口径）",
      _one_on == _one_off == 0.3, f"双面={_one_on} 单面={_one_off}")


# ══════════════════════════════════════════════════════════════════
section("⑤ 金额与 calc_cost 同口径（单价取配置对象，不硬编码）")
# ══════════════════════════════════════════════════════════════════

cfg = pconf.PrinterConfig()
simp, dup = cfg.simplex_price, cfg.duplex_price
print(f"     配置对象单价：simplex={simp} duplex={dup}（默认值不算数，一律从配置取）")
it_simplex = pw.ExternalPrint(job_id=51, pages=2, duplex=pw.DUPLEX_OFF, copies=1)
it_duplex = pw.ExternalPrint(job_id=52, pages=2, duplex=pw.DUPLEX_ON, copies=1)
exp_s, _ = pconf.calc_cost(2, 1, "off", simplex_price=simp, duplex_price=dup)
exp_d, _ = pconf.calc_cost(2, 1, "on", simplex_price=simp, duplex_price=dup)
got_s = pw.amount_for(it_simplex, simp, dup)
got_d = pw.amount_for(it_duplex, simp, dup)
check("单面 2 页金额 == calc_cost(pages=2, copies=1, 'off')", got_s == round(exp_s, 2),
      f"amount_for={got_s} calc_cost={round(exp_s, 2)}")
check("双面 2 页金额 == calc_cost(pages=2, copies=1, 'on')", got_d == round(exp_d, 2),
      f"amount_for={got_d} calc_cost={round(exp_d, 2)}")
check("双面判错会算错钱：2 页双面（1 张）比 2 页单面（2 张）便宜",
      got_d < got_s, f"双面={got_d} < 单面={got_s}")
cfg2 = pconf.PrinterConfig()
cfg2.simplex_price = simp + 0.5
cfg2.duplex_price = dup + 0.7
got_s2 = pw.amount_for(it_simplex, cfg2.simplex_price, cfg2.duplex_price)
got_d2 = pw.amount_for(it_duplex, cfg2.simplex_price, cfg2.duplex_price)
check("改配置单价 → 金额跟着变（不是写死的 0.2/0.3）",
      got_s2 != got_s and got_d2 != got_d
      and got_s2 == round(pconf.calc_cost(2, 1, "off", simplex_price=cfg2.simplex_price,
                                         duplex_price=cfg2.duplex_price)[0], 2),
      f"单面 {got_s}→{got_s2}，双面 {got_d}→{got_d2}")
check("份数进金额（3 份双面 4 页）",
      pw.amount_for(pw.ExternalPrint(job_id=53, pages=4, duplex=pw.DUPLEX_ON, copies=3), simp, dup)
      == round(pconf.calc_cost(4, 3, "on", simplex_price=simp, duplex_price=dup)[0], 2))
check("页数或单双面未知 → 金额返回 None（不是 0，不许拿 0 冒充）",
      pw.amount_for(pw.ExternalPrint(job_id=54, pages=0, duplex=pw.DUPLEX_OFF), simp, dup) is None
      and pw.amount_for(pw.ExternalPrint(job_id=55, pages=3, duplex=pw.DUPLEX_UNKNOWN),
                        simp, dup) is None)
check("DEVMODE.Duplex 映射：1→off / 2,3→on / 缺键不猜",
      pw.duplex_mode_of(job(duplex=1)) == pw.DUPLEX_OFF
      and pw.duplex_mode_of(job(duplex=2)) == pw.DUPLEX_ON
      and pw.duplex_mode_of(job(duplex=3)) == pw.DUPLEX_ON
      and pw.duplex_mode_of({"JobId": 1, "pDevMode": None}) == pw.DUPLEX_UNKNOWN)


# ══════════════════════════════════════════════════════════════════
section("⑥ 去重（同一作业被 ADD/SET/DELETE 多次事件扫到只记 1 条）")
# ══════════════════════════════════════════════════════════════════

st_d = new_store("dedupe_d")
wd = pw.PrintJobWatcher(st_d, enum_jobs=lambda p: [], printers=["HP LaserJet 1020"],
                        virtual_check=not_virtual, on_log=lambda m: None)
same = job(job_id=61, doc="重复.pdf", total=2)
r1 = wd.consider("HP LaserJet 1020", same, now)
r2 = wd.consider("HP LaserJet 1020", same, now)
r3 = wd.consider("HP LaserJet 1020", same, now + timedelta(seconds=30))
check("同一 (printer, job_id) 扫三次只有 1 条", len(st_d) == 1,
      f"第 1 次={r1 is not None} 第 2 次={r2} 第 3 次={r3} 条数={len(st_d)}")
ok_dup, why_dup = pw.should_record(same, (), now, known_keys=st_d.known_job_keys(),
                                   virtual_check=not_virtual)
check("第二次判定返回 (False, 'duplicate')", (ok_dup, why_dup) == (False, "duplicate"),
      f"{ok_dup}/{why_dup}")
check("不同队列的同名 JobId 不算重复（两台打印机各自的 JobId 空间独立）",
      pw.should_record(job(job_id=61, doc="重复.pdf", total=2,
                           printer="HP LaserJet MFP M232-M237"), (), now,
                       known_keys=st_d.known_job_keys(), virtual_check=not_virtual)[0] is True)

st_d3 = new_store("dedupe_d3")
wd3 = pw.PrintJobWatcher(st_d3, enum_jobs=lambda p: [], printers=["HP LaserJet MFP M232-M237"],
                         virtual_check=not_virtual, on_log=lambda m: None)
early = job(job_id=63, doc="后报页数.pdf", total=0, printed=0,
            printer="HP LaserJet MFP M232-M237")
wd3.consider("HP LaserJet MFP M232-M237", early, now)
later = job(job_id=63, doc="后报页数.pdf", total=3, printed=0,
            printer="HP LaserJet MFP M232-M237")
wd3.consider("HP LaserJet MFP M232-M237", later, now + timedelta(seconds=5))
after = st_d3.load()
check("同一作业后来报出页数 ⇒ 补进原记录（仍只有 1 条，来源 auto）",
      len(after) == 1 and after[0].pages == 3 and after[0].pages_source == pw.SOURCE_AUTO,
      f"条数={len(after)} pages={after[0].pages} source={after[0].pages_source}")
st_d4 = new_store("dedupe_d4")
wd4 = pw.PrintJobWatcher(st_d4, enum_jobs=lambda p: [], printers=["P"], virtual_check=not_virtual,
                         on_log=lambda m: None)
wd4.consider("P", job(job_id=64, doc="人工已补.pdf", total=0, printer="P"), now)
st_d4.set_pages(0, 9)
wd4.consider("P", job(job_id=64, doc="人工已补.pdf", total=4, printer="P"), now + timedelta(seconds=5))
check("人工补过的页数不被后来的 auto 值覆盖（人填的优先）",
      st_d4.load()[0].pages == 9 and st_d4.load()[0].pages_source == pw.SOURCE_CONFIRMED,
      f"pages={st_d4.load()[0].pages} source={st_d4.load()[0].pages_source}")

st_d2 = new_store("dedupe_d2")
a = pw.ExternalPrint(job_id=62, printer="P", detected_at="2026-11-02 12:00:00")
check("账本本身按 (printer, job_id, detected_at) 去重：同 key 第二次 add 返回 False",
      st_d2.add(a) is True and st_d2.add(pw.ExternalPrint(**a.to_dict())) is False
      and len(st_d2) == 1)


# ══════════════════════════════════════════════════════════════════
section("⑦ 账本原子写 / 损坏容错 / 2000 条上限")
# ══════════════════════════════════════════════════════════════════

st_e = new_store("atomic_e")
st_e.add(pw.ExternalPrint(job_id=71, printer="P", detected_at="2026-11-02 12:00:01"))
with open(st_e.path, "r", encoding="utf-8") as f:
    parsed = json.load(f)
check("落盘是合法 JSON（不会被写成半截）", isinstance(parsed, list) and parsed[0]["job_id"] == 71)
leftovers = [n for n in os.listdir(os.path.dirname(st_e.path)) if n.endswith(".tmp")
             and n.startswith(os.path.basename(st_e.path))]
check("原子写不留 .tmp 残file（tmp + os.replace）", not leftovers, f"{leftovers}")

st_f = new_store("corrupt_f")
with open(st_f.path, "w", encoding="utf-8") as f:
    f.write('[{"job_id": 1, "printer": "P", "pages"')      # 半截 JSON（断电/并发写现场形态）
items_f = st_f.load()
check("喂半截 JSON：读不崩，回落空账本", items_f == [], f"{items_f}")
check("坏文件被留成 .corrupt 副本（证据不丢）", os.path.exists(st_f.path + ".corrupt"))
check("坏账本之后还能继续记账", st_f.add(pw.ExternalPrint(job_id=72, printer="P")) is True
      and len(st_f) == 1)
st_g = new_store("corrupt_g")
with open(st_g.path, "w", encoding="utf-8") as f:
    f.write('{"items": [{"job_id": 9, "printer": "Q", "pages": 2, "duplex": "off",'
            ' "detected_at": "2026-11-02 09:00:00"}]}')
check("根节点是 {\"items\": [...]} 也认（手工整理过的账本不丢）",
      len(st_g.load()) == 1 and st_g.load()[0].job_id == 9)
st_h = new_store("cap_h")
for i in range(1, 2006):
    st_h.add(pw.ExternalPrint(job_id=i, printer="P", detected_at=f"2026-11-02 12:00:{i % 60:02d}",
                              pages=1, duplex=pw.DUPLEX_OFF))
cap_items = st_h.load()
check(f"上限 2000 条：塞 {2005} 条后只剩 {len(cap_items)} 条", len(cap_items) == 2000,
      f"{len(cap_items)}")
check("超出时丢的是**最旧**的（第 1~5 条被丢，第 6 条成为首条）",
      cap_items[0].job_id == 6 and cap_items[-1].job_id == 2005,
      f"首={cap_items[0].job_id} 末={cap_items[-1].job_id}")


# ══════════════════════════════════════════════════════════════════
section("⑧ CSV 导出列正确")
# ══════════════════════════════════════════════════════════════════

st_i = new_store("csv_i")
st_i.add(pw.ExternalPrint(job_id=81, printer="HP LaserJet 1020", document="菜单.docx",
                          user="顾客A", pages=2, pages_source=pw.SOURCE_AUTO,
                          duplex=pw.DUPLEX_OFF, color=pw.COLOR_MONO, copies=1,
                          detected_at="2026-11-02 12:30:00"))
st_i.add(pw.ExternalPrint(job_id=82, printer="HP LaserJet 1020", document="简历.pdf",
                          user="顾客B", pages=0, pages_source=pw.SOURCE_UNKNOWN,
                          duplex=pw.DUPLEX_UNKNOWN, color=pw.COLOR_UNKNOWN, copies=2,
                          detected_at="2026-11-02 12:31:00"))
csv_path = os.path.join(TMP, "export.csv")
n = st_i.to_csv(csv_path, simplex_price=simp, duplex_price=dup)
with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
    rows = list(csv.reader(f))
check("导出行数 == 账本条数", n == 2 and len(rows) == 3, f"n={n} rows={len(rows)}")
check("表头与约定一致（时间/打印机/文档/用户/页数/页数来源/单双面/彩色/份数/金额/状态）",
      rows[0] == pw.CSV_HEADERS == ["时间", "打印机", "文档", "用户", "页数", "页数来源",
                                    "单双面", "彩色", "份数", "金额", "状态"], f"{rows[0]}")
row1, row2 = rows[1], rows[2]
check("第 1 行内容正确（含金额=calc_cost 值）",
      row1[0] == "2026-11-02 12:30:00" and row1[2] == "菜单.docx" and row1[4] == "2"
      and row1[5] == "自动" and row1[6] == "单面" and row1[7] == "黑白"
      and row1[9] == f"{pw.amount_for(st_i.load()[0], simp, dup):.2f}"
      and row1[10] == "待处理", f"{row1}")
check("未知页数的那行页数/金额留空、来源写「未知」、单双面写「待确认」",
      row2[4] == "" and row2[9] == "" and row2[5] == "未知" and row2[6] == "待确认",
      f"{row2}")
st_j = new_store("csv_j")
st_j.add(pw.ExternalPrint(job_id=83, printer="P", pages=1, duplex=pw.DUPLEX_OFF))
st_j.to_csv(os.path.join(TMP, "no_price.csv"))
with open(os.path.join(TMP, "no_price.csv"), "r", encoding="utf-8-sig", newline="") as f:
    no_price = list(csv.reader(f))
check("没给单价时金额列留空（不拿硬编码默认价糊一个金额）", no_price[1][9] == "", f"{no_price[1]}")


# ══════════════════════════════════════════════════════════════════
section("⑨ pdf_printer 的 tag_self_title 接线（静态 + 导入失败回退）")
# ══════════════════════════════════════════════════════════════════

pp_path = os.path.join(LOCAL_TOOL, "pdf_printer.py")
with open(pp_path, "r", encoding="utf-8") as f:
    pp_src = f.read()
block_re = re.compile(
    r"( +)title = os\.path\.basename\(pdf_path\)\n(.*?)hdc\.StartDoc\(title\)", re.S)
m = block_re.search(pp_src)
check("pdf_printer 在 StartDoc 之前插入了一段 title 处理", m is not None)
pre = textwrap.dedent(m.group(2)) if m else ""
check("这段里 import 了 print_watch.tag_self_title 并在 StartDoc 前调用它",
      "from print_watch import tag_self_title" in pre and "title = tag_self_title(title)" in pre,
      pre.strip().splitlines()[-3:][0] if pre.strip() else "(空)")
check("导入被 try/except 兜住（缺 print_watch.py 也不能影响打印）",
      "except Exception:" in pre or "except ImportError" in pre)
added = sum(1 for line in pre.splitlines() if line.strip()) + 1
check("只加了 ≤6 行（一行级改动，不动打印管线）", added <= 6, f"插入 {added} 行")

# 回退：把 print_watch 从 sys.modules 里变成 None ⇒ import 必然抛 ImportError
class _FakeDC:
    def __init__(self):
        self.titles = []

    def StartDoc(self, t):
        self.titles.append(t)


real_module = sys.modules.get("print_watch")
sys.modules["print_watch"] = None
try:
    ns = {"os": os, "pdf_path": r"C:\tmp\顾客文件.pdf"}
    exec("title = os.path.basename(pdf_path)\n" + pre + "hdc.StartDoc(title)",
         {"os": os, "pdf_path": ns["pdf_path"], "hdc": _FakeDC()}, ns)
except Exception as e:                       # noqa: BLE001
    ns["_err"] = e
finally:
    sys.modules["print_watch"] = real_module
check("print_watch 导入失败时回退不抛异常（打印照走）", "_err" not in ns,
      repr(ns.get("_err")))
dc1 = _FakeDC()
ns2 = {}
exec("title = os.path.basename(pdf_path)\n" + pre + "hdc.StartDoc(title)",
     {"os": os, "pdf_path": r"C:\tmp\顾客文件.pdf", "hdc": dc1}, ns2)
check("正常路径下 StartDoc 收到的是带 HN| 前缀的标题", dc1.titles == ["HN|顾客文件.pdf"],
      f"{dc1.titles}")
dc2 = _FakeDC()
sys.modules["print_watch"] = None
try:
    exec("title = os.path.basename(pdf_path)\n" + pre + "hdc.StartDoc(title)",
         {"os": os, "pdf_path": r"C:\tmp\顾客文件.pdf", "hdc": dc2}, {})
finally:
    sys.modules["print_watch"] = real_module
check("导入失败时 StartDoc 收到原标题（不出现空标题/半截标题）", dc2.titles == ["顾客文件.pdf"],
      f"{dc2.titles}")


# ══════════════════════════════════════════════════════════════════
section("⑩ gui.py 静态接线（菜单勾选项 / 对话框 / Signal / 非模态补录）")
# ══════════════════════════════════════════════════════════════════

gui_path = os.path.join(LOCAL_TOOL, "gui.py")
with open(gui_path, "r", encoding="utf-8") as f:
    gui_src = f.read()
tree = ast.parse(gui_src)


def has_class(name):
    return any(isinstance(n, ast.ClassDef) and n.name == name for n in tree.body)


def class_src(name):
    for n in tree.body:
        if isinstance(n, ast.ClassDef) and n.name == name:
            return ast.get_source_segment(gui_src, n) or ""
    return ""


check("存在 ExternalPrintDialog（外部打印账本窗口）", has_class("ExternalPrintDialog"))
dlg_src = class_src("ExternalPrintDialog")
check("对话框表格十列齐全（时间/打印机/文档/用户/页数/单双面/彩色/金额/来源/状态）",
      all(h in dlg_src for h in ("时间", "打印机", "文档", "用户", "页数",
                                 "单双面", "彩色", "金额", "来源", "状态")))
check("对话框有补录页数 / 标记已处理 / 导出 CSV / 清空 四个按钮",
      "补录页数" in dlg_src and "标记已处理" in dlg_src
      and "导出" in dlg_src and "清空" in dlg_src)
check("空表有占位文案（不出现一片空白的表格）",
      "还没有" in dlg_src or "占位" in dlg_src or "暂无" in dlg_src)
check("存在监视线程 → 主线程的 Signal 桥（QObject + Signal(object)）",
      has_class("ExternalPrintBridge") and "Signal(object)" in class_src("ExternalPrintBridge"))
check("菜单「外部打印监视」是**可勾选**项（QAction + setCheckable(True)）",
      "外部打印监视" in gui_src
      and re.search(r"QAction\(\"外部打印监视[^\"]*\"", gui_src) is not None
      and re.search(r"_ext_print_action\.setCheckable\(True\)", gui_src) is not None)
check("勾选/取消分别调用 start / stop（toggled → _on_external_watch_toggled）",
      "_on_external_watch_toggled" in gui_src
      and ".start()" in class_src("MainWindow") and ".stop()" in class_src("MainWindow"))
check("开关状态写回配置 external_watch_enabled 并落盘",
      "external_watch_enabled" in gui_src and "_save_config(" in gui_src)
check("启动时按配置自动恢复（_restore_external_watch）",
      "_restore_external_watch" in gui_src)
check("补录提示是**非模态**（Qt.NonModal / setWindowModality），不阻塞打印现场",
      re.search(r"setWindowModality\(\s*(Qt\.)?NonModal", gui_src) is not None)
check("监视线程绝不直接碰 Qt：回调落在 _on_external_print_detected（桥的 Signal 接收者）",
      re.search(r"\.detected\.connect\(self\._on_external_print_detected\)", gui_src) is not None
      and re.search(r"on_external=\w+\.detected\.emit", gui_src) is not None)
check("GUI 金额用配置单价现算（config.simplex_price / duplex_price），不硬编码",
      "self._config.simplex_price" in gui_src and "self._config.duplex_price" in gui_src
      and "amount_for(" in gui_src)
check("GUI 不向队列提交/暂停/删除作业（红线：无 Pause/SetJob/CancelJob 调用）",
      not re.search(r"\b(SetJob|PausePrinter|PurgePrinter)\b", gui_src))
# 2026-10-28 现场：拔掉 USB 后发起的作业从没出纸、被取消了，却全部算进了「已确认金额合计」。
# 口径必须是：待处理 = 还没人确认纸出来了 ⇒ 不计入合计；只有已处理才计钱。
check("合计只算「已处理」：待处理/已忽略都不进金额（没出纸的作业不许算钱）",
      "item.status == print_watch.STATUS_CONFIRMED" in gui_src
      and "pending_total" in gui_src
      and re.search(r"item\.status != print_watch\.STATUS_IGNORED:\s*\n\s*total \+=", gui_src) is None)
check("账本有「标记已忽略」（取消/失败/试打的作业可以留档、不再计钱，也不必清空整个账本）",
      '标记已忽略' in gui_src and "_on_mark_ignored" in gui_src
      and re.search(r"_on_mark_ignored[\s\S]{0,400}STATUS_IGNORED", gui_src) is not None)


# 文案里残留 markdown 星号（2026-10-28 用户截图：账本提示原样显示「**绕过本工具**」）。
# QLabel / setToolTip / 纯文本说明都不解析 markdown，星号会原样出现在界面上。
_doc_ids = set()
for _n in ast.walk(tree):
    if isinstance(_n, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) \
            and _n.body:
        _d = _n.body[0]
        if isinstance(_d, ast.Expr) and isinstance(_d.value, ast.Constant):
            _doc_ids.add(id(_d.value))
_star_lits = [(n.lineno, n.value[:40]) for n in ast.walk(tree)
              if isinstance(n, ast.Constant) and isinstance(n.value, str)
              and id(n) not in _doc_ids and "**" in n.value
              and re.search(r"[\u4e00-\u9fff]", n.value)]
check("GUI 文案不含残留 markdown 星号（**…** 在 QLabel/tooltip 里原样显示）",
      not _star_lits, f"{len(_star_lits)} 处：{_star_lits[:3]}")


# ══════════════════════════════════════════════════════════════════
section("①b 真实 spooler 冒烟（只读：订阅 + EnumJobs，不提交任何作业）")
# ══════════════════════════════════════════════════════════════════

if sys.platform == "win32":
    try:
        real = pw.SpoolerApi()
        names = pw.default_list_printers()
        check("能枚举本机打印队列", len(names) > 0, f"{len(names)} 个：{names[:6]}")
        hpr = real.open(names[0])
        notif = real.subscribe(hpr, pw.WATCH_FLAGS)
        rc = real.wait(notif, 300)
        check("非提权进程能订阅队列作业事件（WaitForSingleObject 返回 0/258 即句柄有效）",
              rc in (0, 258), f"rc={rc} (258=WAIT_TIMEOUT)")
        real.unsubscribe(notif)
        real.close(hpr)
        r_store = new_store("real_smoke")
        rw = pw.PrintJobWatcher(r_store, printers=names, on_log=lambda m: None)
        scanned_jobs = rw.scan_once()
        check("对真实队列跑一遍 scan_once() 不抛异常（只读 EnumJobs）",
              isinstance(scanned_jobs, int), f"本轮新记 {scanned_jobs} 条，账本 {len(r_store)} 条")
    except Exception as e:                    # noqa: BLE001
        check("真实 spooler 冒烟", False, f"{type(e).__name__}: {e}")
else:
    print("[SKIP] 非 Windows：跳过真实 spooler 冒烟")



# ══════════════════════════════════════════════════════════════════
section("⑪ SumatraPDF 兜底路径不许重复记账（近期自家作业名登记 + TTL）")
# ══════════════════════════════════════════════════════════════════
# 背景：GDI 路径靠 HN| 前缀排除自家作业，但 Sumatra 兜底是它自己起 StartDoc、作业名取自
# PDF 文件名，插不进前缀 ⇒ 不登记就会被当成外部打印再记一条（同一单收两遍钱）。
# 现场代价：TTL 内如果有外部程序打印**同名**文件会漏记 —— 所以只登记，且 TTL 短、跳过留日志。

pw.note_self_job("hn_sumatra_fallback_测试.pdf")
check("登记后 recent_self_titles() 带出该名字，should_record 据此判自家作业（不记账）",
      "hn_sumatra_fallback_测试.pdf" in pw.recent_self_titles()
      and pw.should_record({"JobId": 9101, "pDocument": "hn_sumatra_fallback_测试.pdf",
                            "Submitted": int(time.time())},
                           pw.recent_self_titles(),
                           printer="HP LaserJet 1020", virtual_check=lambda n: False) == (False, "self"))
check("登记同时覆盖「路径形式」（Sumatra 传路径，spooler 里只剩基名）",
      pw.should_record({"JobId": 9102, "pDocument": "hn_三页.pdf", "Submitted": int(time.time())},
                       pw.recent_self_titles(),
                       printer="HP LaserJet 1020", virtual_check=lambda n: False) == (True, "ok")
      and (pw.note_self_job(r"C:\Users\Administrator\AppData\Local\Temp\hn_三页.pdf") or True)
      and (pw.should_record({"JobId": 9103, "pDocument": "hn_三页.pdf", "Submitted": int(time.time())},
                            pw.recent_self_titles(),
                            printer="HP LaserJet 1020", virtual_check=lambda n: False) == (False, "self")))
check("未登记的同名外部文件照常记账（不能顺手把顾客文件也豁免）",
      pw.should_record({"JobId": 9104, "pDocument": "顾客合同.pdf", "Submitted": int(time.time())},
                       pw.recent_self_titles(),
                       printer="HP LaserJet 1020", virtual_check=lambda n: False) == (True, "ok"))
pw.note_self_job("hn_已过期.pdf", ttl=-1)
check("TTL 过期后不再是自家作业（登记不会永久生效）",
      "hn_已过期.pdf" not in pw.recent_self_titles()
      and pw.should_record({"JobId": 9105, "pDocument": "hn_已过期.pdf",
                            "Submitted": int(time.time())},
                           pw.recent_self_titles(),
                           printer="HP LaserJet 1020", virtual_check=lambda n: False) == (True, "ok"))

# 监视器必须**每轮重取**：Sumatra 是运行中才登记的，构造时取快照会漏
dyn_store = new_store("dynamic_self")
holder = {"jobs": []}
dyn = pw.PrintJobWatcher(dyn_store, enum_jobs=lambda p: holder["jobs"] if "1020" in p else [],
                         printers=["HP LaserJet 1020"], self_titles=pw.recent_self_titles,
                         virtual_check=lambda n: False)
pw.note_self_job("运行中才登记.pdf")
holder["jobs"] = [{"JobId": 9201, "pDocument": "运行中才登记.pdf", "Submitted": int(time.time()),
                   "TotalPages": 3, "pDevMode": {"Duplex": 1, "Color": 1, "Copies": 1}}]
dyn.scan_once()
check("构造之后才登记的作业名同样被排除（动态取，不是构造时快照）", len(dyn_store) == 0)

pdf_src = open(os.path.join(LOCAL_TOOL, "pdf_printer.py"), encoding="utf-8").read()
sumatra_src = pdf_src[pdf_src.find("def _print_via_sumatra"):]
sumatra_src = sumatra_src[:sumatra_src.find("def _print_via_loop")] if "def _print_via_loop" in sumatra_src else sumatra_src
check("pdf_printer 的 Sumatra 兜底在提交前登记作业名（note_self_job）",
      "note_self_job(" in sumatra_src and "os.path.basename(pdf_path)" in sumatra_src)
check("登记发生在真正调用 SumatraPDF 之前（否则登记晚于作业入队，等于没登记）",
      sumatra_src.find("note_self_job(") < sumatra_src.find("subprocess.run("))
gui_src2 = open(os.path.join(LOCAL_TOOL, "gui.py"), encoding="utf-8").read()
check("GUI 把 recent_self_titles **作为可调用对象**传进去（不是调用结果）",
      "self_titles=print_watch.recent_self_titles" in gui_src2)
check("登记失败不影响打印（try/except 兜住，退回 HN| 前缀那一层）",
      "from print_watch import note_self_job" in sumatra_src
      and re.search(r"except Exception:\s*\n\s*pass", sumatra_src) is not None)

# ── 账本合计口径（真渲染一次：待处理/已忽略都不许计钱）──
try:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    _app = QApplication.instance() or QApplication([])
    import gui as _gui
    _st = pw.ExternalPrintStore(os.path.join(TMP, "summary.json"))
    _st.add(pw.ExternalPrint(job_id=1, printer="HP LaserJet 1020", document="a.pdf", pages=3,
                             duplex=pw.DUPLEX_ON, copies=1, status=pw.STATUS_NEW))
    _st.add(pw.ExternalPrint(job_id=2, printer="HP LaserJet 1020", document="b.pdf", pages=2,
                             duplex=pw.DUPLEX_ON, copies=1, status=pw.STATUS_CONFIRMED))
    _st.add(pw.ExternalPrint(job_id=3, printer="HP LaserJet 1020", document="c.pdf", pages=1,
                             duplex=pw.DUPLEX_OFF, copies=1, status=pw.STATUS_IGNORED))
    _dlg = _gui.ExternalPrintDialog(_st, 0.3, 0.4)
    _summary = _dlg._summary.text()
    check("账本合计只算已处理：3 条里待处理(0.70)/已忽略(0.30)都不计入，只剩 ¥0.40",
          "¥0.40" in _summary and "¥0.70，未计入" in _summary, _summary)
except Exception as _e:                                     # 没装 PySide6/无显示环境时不阻塞
    print(f"[SKIP] 账本合计渲染检查跳过：{_e}")

# ══════════════════════════════════════════════════════════════════
section("⑫ 自动记账：页数与单双面都读到就不再等人工确认（2026-10-29 用户口径）")
# ══════════════════════════════════════════════════════════════════

check("auto_confirmable 判据：页数 > 0 且单双面已知才 True（缺一个照旧走人工）",
      pw.auto_confirmable(pw.ExternalPrint(pages=3, duplex=pw.DUPLEX_ON)) is True
      and pw.auto_confirmable(pw.ExternalPrint(pages=2, duplex=pw.DUPLEX_OFF)) is True
      and pw.auto_confirmable(pw.ExternalPrint(pages=0, duplex=pw.DUPLEX_ON)) is False
      and pw.auto_confirmable(pw.ExternalPrint(pages=3, duplex=pw.DUPLEX_UNKNOWN)) is False)

st_auto = new_store("auto_confirm")
logs_auto = []
w_auto = pw.PrintJobWatcher(st_auto, enum_jobs=lambda p: [], printers=["HP LaserJet 1020"],
                            virtual_check=not_virtual, on_log=lambda m: logs_auto.append(m))
w_auto.consider("HP LaserJet 1020", job(job_id=71, doc="自动记账.pdf", total=3, duplex=2), now)
items_auto = st_auto.load()
check("页数+单双面都读到的作业直接落成「已确认」（不必再等人工点）",
      len(items_auto) == 1 and items_auto[0].status == pw.STATUS_CONFIRMED,
      f"{[i.status for i in items_auto]}")
_amount_auto = pw.amount_for(items_auto[0], 0.3, 0.4) if items_auto else None
check("落账即可算钱（3 页双面 = 1 张双面 0.4 + 1 张单面 0.3 = 0.70）",
      _amount_auto == 0.7, f"{_amount_auto}")
check("自动记账在日志里明说「已自动记账」（现场可核，不静默）",
      any("已自动记账" in m for m in logs_auto), str(logs_auto[-1:]))

st_auto2 = new_store("auto_confirm_partial")
w_auto2 = pw.PrintJobWatcher(st_auto2, enum_jobs=lambda p: [], printers=["HP LaserJet 1020"],
                             virtual_check=not_virtual, on_log=lambda m: None)
w_auto2.consider("HP LaserJet 1020", job(job_id=72, doc="缺单双面.pdf", total=3), now)
w_auto2.consider("HP LaserJet 1020", job(job_id=73, doc="缺页数.pdf", total=0, duplex=2), now)
_pend = {i.document: i.status for i in st_auto2.load()}
check("缺页数或缺单双面的作业仍是「待处理」（不猜，等人工补录）",
      _pend.get("缺单双面.pdf") == pw.STATUS_NEW and _pend.get("缺页数.pdf") == pw.STATUS_NEW,
      str(_pend))

st_auto3 = new_store("auto_confirm_late")
w_auto3 = pw.PrintJobWatcher(st_auto3, enum_jobs=lambda p: [],
                             printers=["HP LaserJet MFP M232-M237"],
                             virtual_check=not_virtual, on_log=lambda m: None)
w_auto3.consider("HP LaserJet MFP M232-M237",
                 job(job_id=74, doc="后报页数.pdf", total=0, duplex=2,
                     printer="HP LaserJet MFP M232-M237"), now)
w_auto3.consider("HP LaserJet MFP M232-M237",
                 job(job_id=74, doc="后报页数.pdf", total=4, duplex=2,
                     printer="HP LaserJet MFP M232-M237"), now + timedelta(seconds=5))
_late = st_auto3.load()
check("页数作业跑起来才报的机型：补上后自动转「已确认」（不再要求人点一次）",
      len(_late) == 1 and _late[0].pages == 4 and _late[0].status == pw.STATUS_CONFIRMED,
      f"{[(i.pages, i.status) for i in _late]}")

st_auto4 = new_store("auto_confirm_ignored")
w_auto4 = pw.PrintJobWatcher(st_auto4, enum_jobs=lambda p: [], printers=["P"],
                             virtual_check=not_virtual, on_log=lambda m: None)
w_auto4.consider("P", job(job_id=75, doc="人工已忽略.pdf", total=0, duplex=2, printer="P"), now)
st_auto4.set_status(0, pw.STATUS_IGNORED)
w_auto4.consider("P", job(job_id=75, doc="人工已忽略.pdf", total=5, duplex=2, printer="P"),
                 now + timedelta(seconds=5))
_kept = st_auto4.load()[0]
check("人工点过「已忽略」的记录不会被自动记账翻回「已确认」（撤销必须留得住）",
      _kept.status == pw.STATUS_IGNORED and _kept.pages == 5, f"{_kept.status}/{_kept.pages}")

check("GUI 的补录弹窗判据换成 auto_confirmable（能自动的绝不弹框打扰现场）",
      "print_watch.auto_confirmable(item)" in gui_src)


print("\n" + "=" * 78)
print("全部通过 ✅" if ok_all else "有失败项 ❌")
print("=" * 78)
sys.exit(0 if ok_all else 1)
