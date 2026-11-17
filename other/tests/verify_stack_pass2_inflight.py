# -*- coding: utf-8 -*-
"""L2-2：攒批「整叠第 2 趟」必须留下 inflight 痕迹、可被取消、崩溃重启能认出来。

缺口：第 2 趟开打后进程崩溃/退出，清单里没有任何「这一叠正在打第 2 趟」的记录
⇒ 重启后按「还没打第 2 趟」整叠重打 = 重复出纸。退出路径现在也只是 wait(8000)
直接放弃，不给 worker 取消机会。

判据（实现改坏就红）：
  · 开打前不写 pass2_inflight → ② 红
  · 结束（成功/失败）后不清记录 → ③ 红
  · DuplexFlushWorker 没有 cancel / run 不检查 → ④ 红
  · closeEvent 的取消路径不清 inflight → ⑤ 红
  · 续打判据认不出 inflight → ⑥ 红

跑法：
    PYTHONPATH= PYTHONIOENCODING=utf-8 PYTHONUTF8=1 \
        C:/Users/Administrator/AppData/Local/Programs/Python/Python312/python.exe \
        other/tests/verify_stack_pass2_inflight.py
"""
import os
import sys
import tempfile

_SANDBOX = tempfile.mkdtemp(prefix="hn_stack_pass2_inflight_")
os.environ["APPDATA"] = _SANDBOX
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LOCAL_TOOL = os.path.join(REPO, "local_print_tool")
sys.path.insert(0, LOCAL_TOOL)
os.chdir(LOCAL_TOOL)

_ok = []
_fail = []


def check(name, cond, detail=""):
    if cond:
        _ok.append(name)
        print(f"  [PASS] {name}")
    else:
        _fail.append(name)
        print(f"  [FAIL] {name}" + (f"  ← {detail}" if detail else ""))


def read(p):
    with open(p, "r", encoding="utf-8") as f:
        return f.read()


from PySide6.QtWidgets import QApplication  # noqa: E402

_app = QApplication.instance() or QApplication([])

import gui  # noqa: E402
import paths  # noqa: E402
import duplex_stack  # noqa: E402
import pdf_printer  # noqa: E402


def mk_entry(seq, title, n_pages, state):
    return duplex_stack.StackEntry(
        seq=seq, title=title, tab_key="1", order_id=0, order_number=f"HN{seq:04d}",
        page_count=n_pages, sheet_count=(n_pages + 1) // 2, state=state, rotate=True,
        created_at="2026-11-17 10:00:00", pdf=f"entry_{seq:04d}.pdf",
        file_plans=[{"label": title, "pages": list(range(1, n_pages + 1)),
                     "duplex": True, "second_rotate": True}],
        tasks=[{"task_id": seq, "display_name": title, "file_path": "",
                "copies": 1, "duplex": "on", "page_range": "",
                "page_count": n_pages, "tab_key": "1"}],
    )


class _Sig:
    def __init__(self):
        self.slots = []

    def connect(self, fn):
        self.slots.append(fn)


startmanifests = []


class FlushStub:
    """启动时把磁盘上的清单读回来（=崩溃前那一刻的持久化事实）。"""

    def __init__(self, stack, batch_dir, printer_name, dpi=0, parent=None):
        self.stack, self.batch_dir = stack, batch_dir
        self.progress, self.log_message, self.done = _Sig(), _Sig(), _Sig()
        self.started = False

    def start(self):
        self.started = True
        startmanifests.append(duplex_stack.load_manifest(self.batch_dir))

    def isRunning(self):
        return bool(self.started)

    def wait(self, ms=0):
        return True


_orig_flush = gui.DuplexFlushWorker
gui.DuplexFlushWorker = FlushStub
gui.QMessageBox.question = staticmethod(lambda *a, **k: gui.QMessageBox.Yes)
gui.QMessageBox.warning = staticmethod(lambda *a, **k: None)

win = gui.MainWindow(config_path=paths.config_path())
win._cloud_client = None
win._config.printer_name = "Fake Print"
logs = []
win._log = lambda *a, **k: logs.append(" ".join(str(x) for x in a))
win._refresh_stack_queue = lambda *a, **k: None
win._stack_busy = lambda: False
win._stack_reconcile_gate = lambda: True
win._finish_stack_entries = lambda *a, **k: None
win._finish_stack_entries_split = lambda *a, **k: None
win._finish_stack_progress = lambda *a, **k: None
win._sync_stack_button = lambda *a, **k: None
win._drain_stack_wait_queue = lambda *a, **k: None
win._save_config = lambda *a, **k: None
win._report_job_result = lambda *a, **k: None


def inflight_active(st):
    fn = getattr(st, "pass2_inflight_active", None)
    try:
        return bool(fn()) if fn else False
    except Exception:
        return False


def make_stack(name, entries):
    st = duplex_stack.DuplexStack(capacity=90, printer_name="Fake Print")
    st.new_batch(name)
    for e in entries:
        st.add(e)
    return st


print("── ① 静态：开打前落盘 inflight，且不用 UI 文案当判据 ──")
_gui_src = read(os.path.join(LOCAL_TOOL, "gui.py"))
_flush_src = _gui_src.split("def _on_stack_flush(self)", 1)[1].split("\n    def ", 1)[0]
check("①-a _on_stack_flush 里调用了 mark_pass2_inflight",
      "mark_pass2_inflight" in _flush_src, _flush_src[:200])
if "mark_pass2_inflight" in _flush_src and "w.start()" in _flush_src:
    _mk = _flush_src.index("mark_pass2_inflight")
    _sp = _flush_src.index("w.start()")
    check("①-b 标记在 w.start() 之前（开打前持久化）", _mk < _sp, f"mark={_mk} start={_sp}")
    check("①-c 标记之后立刻 save_manifest 落盘",
          "save_manifest" in _flush_src[_mk:_sp], _flush_src[_mk:_sp][:300])
else:
    check("①-b 标记在 w.start() 之前（开打前持久化）", False, "缺 mark 或 w.start()")
    check("①-c 标记之后立刻 save_manifest 落盘", False, _flush_src[:200])

print("── ② 运行期：整叠第 2 趟开打前，磁盘清单里有 pass2_inflight ──")
st = make_stack("inflight1", [
    mk_entry(1, "A", 2, duplex_stack.STATE_PRINTING),
    mk_entry(2, "B", 2, duplex_stack.STATE_PRINTING),
])
win._stack = st
win._stack_flush_worker = None
startmanifests.clear()
try:
    win._on_stack_flush()
    crashed = None
except Exception as e:  # noqa: BLE001
    crashed = f"{type(e).__name__}: {e}"
check("②-a 启动不抛异常", crashed is None, crashed)
check("②-b 确实启动了 worker（前提）",
      len(startmanifests) == 1, startmanifests)
_doc = (startmanifests[0].to_dict() if startmanifests and startmanifests[0] else {}).get("pass2_inflight")
check("②-c 开打那一刻清单里已有 pass2_inflight",
      isinstance(_doc, dict) and _doc.get("stage") == "pass2_inflight", _doc)
check("②-d 记录带时间戳（at 文本 + ts 数值）",
      isinstance(_doc, dict) and bool(str(_doc.get("at") or ""))
      and isinstance(_doc.get("ts"), (int, float)), _doc)
check("②-e 内存里的 stack 也认得 inflight（pass2_inflight_active）",
      inflight_active(st), getattr(st, "pass2_inflight", None))

print("── ③ 成功后记录消失（清单不再声称正在打第 2 趟）──")
_saved_discard = duplex_stack.discard_batch
duplex_stack.discard_batch = lambda *a, **k: None   # 保住目录，检查清记录本身
try:
    win._on_stack_flush_done(True, "桩：整叠第 2 趟完成", 2)
finally:
    duplex_stack.discard_batch = _saved_discard
check("③-a done 后内存里 inflight 已清", not inflight_active(st))
_fresh = duplex_stack.load_manifest(win._stack_batch_dir()) if os.path.isdir(win._stack_batch_dir()) else None
check("③-b 磁盘清单里的 pass2_inflight 已清（不是靠删目录掩盖）",
      _fresh is None or not inflight_active(_fresh),
      (getattr(_fresh, "pass2_inflight", None)))

# 失败路径（第 2 趟卡纸）同样必须清痕迹 —— 否则下次启动误报"只打了一半"
st_f = make_stack("inflight_fail", [mk_entry(7, "F", 2, duplex_stack.STATE_PRINTING)])
win._stack = st_f
st_f.mark_pass2_inflight()
duplex_stack.save_manifest(win._stack_batch_dir(), st_f)
win._stack_flush_snapshot = list(st_f.entries)
win._keep_stack_stall = lambda *a, **k: None   # 本守卫只查 inflight 清理，台账另有用例
_saved_discard2 = duplex_stack.discard_batch
duplex_stack.discard_batch = lambda *a, **k: None
try:
    win._on_stack_flush_done(False, "整叠第 2 趟失败：卡纸", 0)
    crashed3 = None
except Exception as e:  # noqa: BLE001
    crashed3 = f"{type(e).__name__}: {e}"
finally:
    duplex_stack.discard_batch = _saved_discard2
check("③-c 失败收尾不抛异常", crashed3 is None, crashed3)
check("③-d 失败收尾也清掉内存里的 inflight", not inflight_active(st_f))
_f3 = duplex_stack.load_manifest(win._stack_batch_dir())
check("③-e 失败收尾的磁盘清单也不再有 inflight",
      _f3 is None or not inflight_active(_f3), getattr(_f3, "pass2_inflight", None))

print("── ④ 取消：DuplexFlushWorker.cancel() 后 run 不打印、不清账 ──")
check("④-a DuplexFlushWorker 有 cancel() 方法", hasattr(_orig_flush, "cancel"))
w = _orig_flush(st, win._stack_batch_dir(), "Fake Print", dpi=0)
dones = []
w.done.connect(lambda *a: dones.append(a))
printed = []
_orig_pp = gui.print_plan_pass
_orig_merge = pdf_printer.merge_pdfs
gui.print_plan_pass = lambda *a, **k: printed.append(a) or (True, "桩")
pdf_printer.merge_pdfs = lambda *a, **k: (True, "")
try:
    w.cancel()
    w.run()
except Exception as _e4:  # noqa: BLE001
    print(f"  （cancel 路径抛了：{type(_e4).__name__}: {_e4}）")
finally:
    gui.print_plan_pass = _orig_pp
    pdf_printer.merge_pdfs = _orig_merge
check("④-b 取消后 run 不再送打印", printed == [], printed)
check("④-c 取消后不 emit done（不误落台账/误收尾）", dones == [], dones)

print("── ⑤ closeEvent 的取消路径：cancel 后清掉 inflight ──")
st2 = make_stack("inflight2", [mk_entry(3, "C", 2, duplex_stack.STATE_PRINTING)])
win._stack = st2
try:
    st2.mark_pass2_inflight()
except Exception as _e2:
    print(f"  （mark_pass2_inflight 不可用：{type(_e2).__name__}: {_e2}）")
duplex_stack.save_manifest(win._stack_batch_dir(), st2)
w2 = _orig_flush(st2, win._stack_batch_dir(), "Fake Print", dpi=0)
win._stack_flush_worker = w2
_m5 = duplex_stack.load_manifest(win._stack_batch_dir())
check("⑤-a 前提：inflight 已落盘", inflight_active(_m5))
win._closing = False
try:
    _ended = win._cancel_stack_flush(200)
    crashed5 = None
except Exception as e:  # noqa: BLE001
    _ended, crashed5 = None, f"{type(e).__name__}: {e}"
check("⑤-b _cancel_stack_flush 不抛且报告线程已结束", crashed5 is None and _ended is True, (crashed5, _ended))
check("⑤-c 内存 inflight 已清", not inflight_active(st2))
_f2 = duplex_stack.load_manifest(win._stack_batch_dir())
check("⑤-d 磁盘清单 inflight 已清", _f2 is None or not inflight_active(_f2),
      getattr(_f2, "pass2_inflight", None))
# 全文件有 2 个 closeEvent（另一个是薄包装窗），必须限定在 MainWindow 里找
_mw_src = _gui_src.split("class MainWindow(QMainWindow):", 1)[1]
_cg = _mw_src.split("def closeEvent(self, event):", 1)[1].split("\n    def ", 1)[0]
check("⑤-e closeEvent 走 _cancel_stack_flush（不是裸 wait 后放弃）",
      "_cancel_stack_flush(8000)" in _cg
      and "self._stack_flush_worker.wait(8000)" not in _cg)

print("── ⑥ 模拟崩溃（不调结束路径）→ 重启判据认得出「上次第 2 趟可能只打了一半」──")
st3 = make_stack("inflight3", [mk_entry(4, "D", 2, duplex_stack.STATE_PRINTING)])
win._stack = st3
win._stack_flush_worker = None
# 崩溃现场：真的走一次开打入口（标记落盘 + worker 启动），然后"进程直接死"：
# 绝不调用 _on_stack_flush_done（也没有任何清理路径）
try:
    win._on_stack_flush()
    crashed6 = None
except Exception as e:  # noqa: BLE001
    crashed6 = f"{type(e).__name__}: {e}"
check("⑥-0 开打入口不抛异常（前提）", crashed6 is None, crashed6)
_crash = duplex_stack.load_manifest(win._stack_batch_dir())
check("⑥-a 重启后从清单读回 inflight",
      _crash is not None and inflight_active(_crash),
      getattr(_crash, "pass2_inflight", None))
_haz = gui._stack_resume_hazards(_crash) if hasattr(gui, "_stack_resume_hazards") else []
_htxt = " ".join(_haz)
check("⑥-b 判据里点明「上次第 2 趟可能只打了一半」",
      "上次第 2 趟可能只打了一半" in _htxt, _htxt[:200])
check("⑥-c 提示是中文文案且没有 markdown 星号", "*" not in _htxt, _htxt[:200])
_resume_src = _gui_src.split("def _check_pending_stack_on_startup(self)", 1)[1].split("\n    def ", 1)[0]
check("⑥-d 启动续打弹窗真的用了这个判据",
      "_stack_resume_hazards" in _resume_src)

gui.DuplexFlushWorker = _orig_flush
print()
print(f"PASS {len(_ok)}  FAIL {len(_fail)}")
if _fail:
    print("存在失败 ❌")
    sys.exit(1)
print("全部通过 ✅")
