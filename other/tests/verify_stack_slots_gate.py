# -*- coding: utf-8 -*-
"""L2-5：攒批第 2 趟启动前，「出纸槽占用数 present」必须等于「第 2 趟实际要送的槽位 slots」。

现状 bug：present 把第 1 趟失败的单也算进去（present_sheet_count 不排除 STATE_FAILED），
而 build_plan() 只送 active 单的背面 ⇒ 有失败单时 present > slots，程序照样开打：
进纸盒里到底几张、哪几张属于这叠无法确定，第 2 趟会整叠错位（打到别人的纸 / 漏页）。

判据（实现改坏就红）：
  · 删掉一致性检查 → ② 红（worker 真的被启动）
  · 反过来「一律拒绝」→ ③ 红（正常纸叠也打不了）
  · 不把证据写进日志/弹窗 → ④ 红

跑法：
    PYTHONPATH= PYTHONIOENCODING=utf-8 PYTHONUTF8=1 \
        C:/Users/Administrator/AppData/Local/Programs/Python/Python312/python.exe \
        other/tests/verify_stack_slots_gate.py
"""
import os
import sys
import tempfile

_SANDBOX = tempfile.mkdtemp(prefix="hn_stack_slots_gate_")
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


flush_created = []


class FlushStub:
    def __init__(self, stack, batch_dir, printer_name, dpi=0, parent=None):
        self.stack, self.batch_dir = stack, batch_dir
        self.progress, self.log_message, self.done = _Sig(), _Sig(), _Sig()
        self.started = False
        flush_created.append(self)

    def start(self):
        self.started = True

    def isRunning(self):
        return bool(self.started)

    def wait(self, ms=0):
        return True


_orig_flush = gui.DuplexFlushWorker
gui.DuplexFlushWorker = FlushStub
gui.QMessageBox.question = staticmethod(lambda *a, **k: gui.QMessageBox.Yes)
warns = []
gui.QMessageBox.warning = staticmethod(
    lambda *a, **k: warns.append(tuple(str(x) for x in a if isinstance(x, str))))

win = gui.MainWindow(config_path=paths.config_path())
win._cloud_client = None
win._config.printer_name = "Fake Print"
logs = []
win._log = lambda *a, **k: logs.append(" ".join(str(x) for x in a))
win._refresh_stack_queue = lambda *a, **k: None
win._stack_busy = lambda: False


def make_stack(name, entries):
    st = duplex_stack.DuplexStack(capacity=90, printer_name="Fake Print")
    st.new_batch(name)
    for e in entries:
        st.add(e)
    return st


print("── ① 静态：一致性检查在 reconcile gate 之前、且写明了不一致就拒绝 ──")
_gui_src = read(os.path.join(LOCAL_TOOL, "gui.py"))
_flush_src = _gui_src.split("def _on_stack_flush(self)", 1)[1].split("\n    def ", 1)[0]
check("①-a 有 present 与 slots 的比较", "present != slots" in _flush_src,
      _flush_src[:200])
check("①-b 不一致时拒绝启动（return，不是继续）", "拒绝启动" in _flush_src)
check("①-c 证据里有清单路径（人工核对用）",
      "manifest_path" in _flush_src.split("present != slots", 1)[-1][:900])

print("── ② 运行期：有第 1 趟失败单（present=2 / slots=1）→ 必须拒绝启动 ──")
# 1 单正常（2 页 = 1 张纸）+ 1 单第 1 趟失败（2 页 = 1 张纸）
# present = 1 + 1 = 2，但第 2 趟页序只送正常那单 ⇒ slots = 1。
st_bad = make_stack("gate_bad", [
    mk_entry(1, "OK", 2, duplex_stack.STATE_PRINTING),
    mk_entry(2, "FAILED", 2, duplex_stack.STATE_FAILED),
])
win._stack = st_bad
flush_created.clear()
warns.clear()
logs.clear()
_present = win._stack_present_sheets()
_slots = win._stack_second_pass_slots()
try:
    win._on_stack_flush()
    crashed = None
except Exception as e:  # noqa: BLE001
    crashed = f"{type(e).__name__}: {e}"
check("②-a 前提成立：present=2 但 slots=1（失败单不进第 2 趟页序）",
      _present == 2 and _slots == 1, f"present={_present} slots={_slots}")
check("②-b 收尾不抛异常", crashed is None, crashed)
check("②-c 没有创建/启动打印 worker", flush_created == [], flush_created)
_wtxt = " ".join(" ".join(w) if isinstance(w, tuple) else str(w) for w in warns)
check("②-d 弹了「页数对不上」并拒绝（不是静默返回）",
      "对不上" in _wtxt and "拒绝" in _wtxt, _wtxt[:240])
check("②-e 日志留了证据（present / slots / 清单路径）",
      any("拒绝启动" in s and "2 张" in s and "1 张" in s for s in logs), logs[-3:])

print("── ③ 正向对照：口径一致（present=slots=2）→ 正常启动（闸门不许过宽）──")
st_ok = make_stack("gate_ok", [
    mk_entry(1, "A", 2, duplex_stack.STATE_PRINTING),
    mk_entry(2, "B", 2, duplex_stack.STATE_PRINTING),
])
win._stack = st_ok
win._stack_flush_worker = None      # ② 那次真启动了桩 worker：不清掉会被第一行 isRunning 挡住
flush_created.clear()
warns.clear()
_present2 = win._stack_present_sheets()
_slots2 = win._stack_second_pass_slots()
try:
    win._on_stack_flush()
    crashed2 = None
except Exception as e:  # noqa: BLE001
    crashed2 = f"{type(e).__name__}: {e}"
check("③-a 前提成立：present=slots=2", _present2 == 2 and _slots2 == 2,
      f"present={_present2} slots={_slots2}")
check("③-b 收尾不抛异常", crashed2 is None, crashed2)
check("③-c 正常启动了打印 worker（闸门没有一律拒绝）",
      len(flush_created) == 1 and flush_created[0].started is True, flush_created)
check("③-d 没有弹「对不上」", not any("对不上" in (w if isinstance(w, str) else " ".join(w))
                                    for w in warns), warns)

gui.DuplexFlushWorker = _orig_flush
print()
print(f"PASS {len(_ok)}  FAIL {len(_fail)}")
if _fail:
    print("存在失败 ❌")
    sys.exit(1)
print("全部通过 ✅")
