# -*- coding: utf-8 -*-
"""验收脚本 · 攒批模式：装订方向混排不再把订单停死（2026-10-28 现场事故）

现场（13:34-13:35，日志 local_tool.log 可查）：攒批模式开着，点「开始打印」一个
「成绩表横向 + 申请表竖向」的订单 ⇒ 日志写「⏸ 已排队（等当前纸叠打完第 2 趟）：同一单里
混排了长边/短边装订，攒批时只能有一种旋转补偿」，**一行纸都没出**；再点一次又排一条；
想关攒批时被「有 N 个打印请求在排队等这一叠打完」挡住 —— 只能强关程序。

根因三环相扣：
  · 第 2 趟的 180° 补偿自 2026-10-27 起已经是**逐张纸**的（FilePlan.second_rotate →
    print_plan_pass 的 rotate_slots），同一叠里长边/短边可以混排；
  · 但准入层（duplex_stack.admit 的 REASON_ROTATE、gui._request_rotate）还按「一叠一种
    方向」拒绝入叠 ⇒ 这种订单被判「不可入叠」，丢进 _stack_wait；
  · 而 _stack_wait 只在「纸叠收尾」时 drain —— 这里**纸叠是空的**，永远不会有收尾事件
    ⇒ 请求永久卡在队列里：不出纸、也关不掉模式（忙闲判定把它算成「忙」）。

本脚本钉住（每条对应上面一环）：
1. 账本：混合方向的单能入叠（只剩容量门槛），逐文件 rotate 一路带到第 2 趟计划；
2. GUI：_stack_gate 对混排单返回 entry（不是 parked）、_stack_wait 保持空；
3. 防呆：队列非空 + 纸叠空 + 没在打 ⇒ 定时兜底能放行；关模式不再被「排队」挡住；
4. 去重：同一批 job 连点两次只排一条（旧写法会打两遍）；
5. 变异自证：把 admit 换回「方向不一致就拒绝」⇒ 第 2 节必须变红（证明测的是真路径）。

    python tests/verify_stack_mixed_binding.py     # exit 0 = 全绿
"""
import io
import os
import re
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL_TOOL = os.path.join(REPO, "local_print_tool")
_SANDBOX = tempfile.mkdtemp(prefix="hn_stackmix_")
os.environ["APPDATA"] = _SANDBOX
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, LOCAL_TOOL)
os.chdir(LOCAL_TOOL)

import duplex_stack as ds  # noqa: E402
import manual_duplex  # noqa: E402
from printer_config import PrintJob, PrinterProfile, TabSettings  # noqa: E402

ok_all = True


def check(name, cond, detail=""):
    global ok_all
    if not cond:
        ok_all = False
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


print("\n① 账本：混合装订方向不再被拒（只剩容量门槛）")
st = ds.DuplexStack(capacity=90)
e_mixed = ds.StackEntry(
    seq=1, title="混合单", page_count=4, sheet_count=3, rotate=True,
    file_plans=[
        {"label": "成绩表(横向)", "pages": [1, 2], "duplex": True, "second_rotate": False},
        {"label": "申请表(竖向)", "pages": [3, 4], "duplex": True, "second_rotate": True},
    ])
check("★ 混排单能收进空叠", st.admit(3, False).ok)
st.add(e_mixed)
check("空叠已收 1 单 / 3 张", st.count == 1 and st.total_sheets == 3,
      f"count={st.count} sheets={st.total_sheets}")
e2 = ds.StackEntry(seq=2, title="第二单", page_count=2, sheet_count=2, rotate=True,
                   file_plans=[{"label": "第二单", "pages": [1, 2], "duplex": True,
                                "second_rotate": True}])
check("★ 第二单（另一标签页）照样能入叠（旧实现被 REASON_ROTATE 拒）", st.admit(2, True).ok)
st.add(e2)
check("同一叠里两单都在", st.count == 2 and st.total_sheets == 5,
      f"count={st.count} sheets={st.total_sheets}")

plan = st.build_plan()
flags = [bool(x) for x in plan.pass_rotate(2)]
check("★ 第 2 趟逐张纸补偿：同一叠里既有转 180° 也有不转", len(set(flags)) == 2,
      f"flags={flags}")
check("第 2 趟页序仍是整叠逆序（配对只在单内）", plan.pass_pages(2) == [6, 4, 2],
      str(plan.pass_pages(2)))
check("逐文件 rotate 一路带到 FilePlan",
      [fp.second_rotate for fp in st.file_plans()] == [False, True, True],
      str([fp.second_rotate for fp in st.file_plans()]))
check("旧批次清单（没有 second_rotate 字段）⇒ None，回落整叠默认值",
      ds._plan_rotate({"label": "旧清单"}) is None)

# 旧清单的兜底方向必须跟着本叠走（短边机型上续打旧清单不能整趟转 180°）
for _nominal, _want in ((True, True), (False, False)):
    _st = ds.DuplexStack(capacity=90)
    _st.add(ds.StackEntry(seq=1, page_count=2, sheet_count=1, rotate=_nominal,
                          file_plans=[{"label": "旧", "pages": [1, 2], "duplex": True}]))
    _flags = list(_st.build_plan().pass_rotate(2))
    check(f"旧清单（无逐文件标志）⇒ 按本叠名义方向 rotate={_nominal} 兜底",
          _flags and set(_flags) == {_want}, str(_flags))

_src_stack = open(os.path.join(LOCAL_TOOL, "duplex_stack.py"), encoding="utf-8").read()
check("admit 里不再有 REASON_ROTATE 判定", "adm.reason = REASON_ROTATE" not in _src_stack)

print("\n② GUI：混排单必须走 entry（现场就是被停成 parked）")
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

app = QApplication.instance() or QApplication([])
import converter as _converter  # noqa: E402
_converter.get_available_engines = lambda: {"word": True, "wps": True, "libreoffice": True}
import gui as _gui  # noqa: E402
import paths  # noqa: E402
_gui.QMessageBox.question = staticmethod(lambda *a, **k: _gui.QMessageBox.Yes)
_gui.QMessageBox.warning = staticmethod(lambda *a, **k: _gui.QMessageBox.Ok)
_gui.QMessageBox.information = staticmethod(lambda *a, **k: _gui.QMessageBox.Ok)

MANUAL = PrinterProfile(auto_duplex=False, capability_source="manual", manual_duplex=True,
                        first="odd", reverse_second=True, second_rotate_180=True,
                        stack_capacity=90, stack_warn_after_sec=1800)

win = _gui.MainWindow(config_path=paths.config_path())
win.resize(1400, 900)
win.show()
QTest.qWaitForWindowExposed(win)
app.processEvents()
def ensure_env(batch: str = "test_batch"):
    """把"这台队列需要人工翻纸 + 攒批开着"摆好。

    为什么每次都要重摆：`_save_config()` 会先 `_sync_ui_to_config()`，那一步按**打印机下拉框**
    回写 `config.printer_name`（offscreen 里下拉框没有这个测试队列）⇒ 档案会跟着换成默认值、
    `_stack_applicable()` 变 False。这是测试环境的口径问题，与本次修复无关，重摆一次即可。
    """
    win._config.printer_name = "测试队列"
    win._config.printer_profiles["测试队列"] = MANUAL
    win._config.duplex_stack_mode = True
    win._stack.new_batch(batch)
    win._stack.capacity = 90
    win._stack_wait.clear()
    app.processEvents()


ensure_env()


def mk(name, orientation, duplex_mode, pages=2):
    return PrintJob(file_path=os.path.join(_SANDBOX, name), copies=1, duplex="on",
                    duplex_mode=duplex_mode, page_count=pages, orientation=orientation,
                    display_name=name)


# 现场那一单：3 个横向（短边，不转）+ 2 个竖向（长边，转 180°）
field_jobs = [mk("排名表.pdf", "landscape", "short-edge"),
              mk("成绩汇总.pdf", "landscape", "short-edge"),
              mk("奖学金申请表.doc", "portrait", "long-edge"),
              mk("认定表.docx", "portrait", "long-edge"),
              mk("申请书.docx", "portrait", "long-edge")]
tab = TabSettings()
tab.jobs = list(field_jobs)
win._config.tabs = {"1": tab}
win._current_tab = "1"
win._config.active_tab = "1"
win._rebuild_table()
app.processEvents()

check("前置：攒批开着 + 本队列要人工翻纸",
      win._stack_mode_on() and win._uses_manual_duplex_job(field_jobs[0]),
      f"mode_on={win._stack_mode_on()} profile={win._printer_profile().auto_duplex}")
check("前置：这一单确实混排两种装订方向",
      {win._rotate_for_job(j) for j in field_jobs} == {True, False},
      str({win._rotate_for_job(j) for j in field_jobs}))
_ok, _rot, _note = win._request_rotate(field_jobs)
check("_request_rotate 不再判「不可用」，只给说明", _ok is True and "混排" in _note,
      f"ok={_ok} note={_note!r}")
_adm_ok, _reason, _sheets, _unknown = win._admit_stack_entry(field_jobs)
check("★ _admit_stack_entry 放行混排单", _adm_ok, _reason)
gate = win._stack_gate(field_jobs, "1")
check("★ _stack_gate 返回 entry（不是 parked）", gate == "entry", gate)
check("★ 队列里没有被停住的请求（现场就是这里攒了 2 条）", len(win._stack_wait) == 0,
      str(len(win._stack_wait)))

print("\n③ 防呆：队列里的请求一定有出路")
_calls = []
_orig_start = win._start_print_worker
win._start_print_worker = lambda jobs, tab_key=None, stack_entry=None: (
    _calls.append((len(jobs), tab_key)) or True)

win._park_print_request(field_jobs, "1", "桩：模拟被停在队列里")
win._park_print_request(field_jobs, "1", "桩：模拟连点第二次")
check("同一批 job 连点两次只排一条（旧写法排两条 ⇒ 放行后打两遍）",
      len(win._stack_wait) == 1, str(len(win._stack_wait)))

win._stack_state_tick()
app.processEvents()
check("★ 定时兜底把排队请求放行（纸叠空 + 没在打）",
      len(win._stack_wait) == 0 and len(_calls) == 1,
      f"队列={len(win._stack_wait)} 启动={len(_calls)}")

ensure_env("test_batch2")
win._park_print_request(field_jobs, "1", "桩：模拟排队")
_calls.clear()
win._on_stack_mode_toggled(False)
app.processEvents()
check("★ 只剩排队时**可以**关攒批模式（现场被「打印机正忙」锁死）",
      win._stack_mode_on() is False, str(win._stack_mode_on()))
check("关掉后队列立刻按普通打印放行",
      len(win._stack_wait) == 0 and len(_calls) == 1,
      f"队列={len(win._stack_wait)} 启动={len(_calls)}")
win._start_print_worker = _orig_start

print("\n④ 静态接线：逐张纸补偿真的接通了")
_src_gui = open(os.path.join(LOCAL_TOOL, "gui.py"), encoding="utf-8").read()
check("攒批第 1 趟把每个文件的 second_rotate 记进账本",
      '"second_rotate": bool(_rot),' in _src_gui)
check("整叠第 2 趟不再用整叠单值压平（second_rotate_180=True 交给计划）",
      re.search(r"print_plan_pass\(\s*\n\s*plan, merged, self\._printer_name, 2,\s*\n"
                r"\s*second_rotate_180=True,", _src_gui) is not None)
check("旧写法（bool(self._stack.rotate)）已经不存在",
      "second_rotate_180=bool(self._stack.rotate)" not in _src_gui)

print("\n⑤ 变异自证：把两道旧闸门分别装回去，② 的断言必须变红")
# 现场那条日志（「混排了长边/短边装订…」）出自 **_request_rotate**，不是 admit ——
# admit 那道只在"纸叠已经有纸"时才轮到（空叠永远能收）。两道都还原，各测一次。
_orig_rr = win._request_rotate


def _old_request_rotate(jobs):
    duplex_jobs = [j for j in jobs if win._uses_manual_duplex_job(j)]
    if not duplex_jobs:
        return True, True, ""
    vals = {win._rotate_for_job(j) for j in duplex_jobs}
    if len(vals) > 1:
        return False, False, "同一单里混排了长边/短边装订，攒批时只能有一种旋转补偿"
    return True, bool(next(iter(vals))), ""


win._request_rotate = _old_request_rotate
ensure_env("mutant_rr")
_mut_gate_a = win._stack_gate(field_jobs, "1")      # 空叠 + 旧 _request_rotate
win._request_rotate = _orig_rr
check("★ 变异A（旧 _request_rotate）+ 空纸叠 ⇒ 停成 parked（现场就是这一支）",
      _mut_gate_a == "parked", _mut_gate_a)
check("变异A 下队列里真的多了一条（现场症状）", len(win._stack_wait) == 1,
      str(len(win._stack_wait)))

_orig_admit = ds.DuplexStack.admit


def _old_admit(self, sheets, rotate=True, unknown_pages=False):
    sheets = max(0, int(sheets or 0))
    adm = ds.Admission(ok=False, estimated_sheets=sheets, unknown_pages=bool(unknown_pages))
    if sheets <= 0:
        adm.reason = ds.REASON_EMPTY
        return adm
    if self.is_empty:
        adm.ok = True
        return adm
    if bool(rotate) != bool(self.rotate):
        adm.reason = ds.REASON_ROTATE
        return adm
    if self.total_sheets + sheets > self.capacity:
        adm.reason = ds.REASON_CAPACITY
        return adm
    adm.ok = True
    return adm


ds.DuplexStack.admit = _old_admit
ensure_env("mutant_admit")
win._stack.rotate = True          # 本叠名义方向 = 长边 ⇒ 混排单在旧 admit 下必被拒
win._stack.entries = [ds.StackEntry(seq=1, page_count=2, sheet_count=1, rotate=True,
                                   file_plans=[{"label": "占位", "pages": [1, 2],
                                                "duplex": True, "second_rotate": True}])]
_mut_gate_b = win._stack_gate(field_jobs, "1")      # 非空叠 + 旧 admit
ds.DuplexStack.admit = _orig_admit
check("★ 变异B（旧 admit）+ 非空纸叠 ⇒ 也停成 parked", _mut_gate_b == "parked", _mut_gate_b)

print("\n" + ("PASS 全部通过" if ok_all else "FAIL 有失败项"))
sys.exit(0 if ok_all else 1)
