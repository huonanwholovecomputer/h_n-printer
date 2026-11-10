# -*- coding: utf-8 -*-
"""预约单「一份文件下载失败」不许扔掉整单（🔴21 / 批次 A-3）

旧行为：`_update_scheduled_task()` 一看到 `task.status == "error"` 就
`_cleanup_scheduled_order(oid, reason="文件下载失败")` —— 这一单里**其它已经下载好、
或者还在下**的文件，连状态机带标签页一起消失。顾客付了钱、机器空转，日志里只有一行
「文件下载失败」，没有任何「其余几份照常打」的补救。

现在的口径：
  · 失败的那一份：`report_fail` + `reject_task`（打回给顾客重传）+ 记入已处理；
  · 其余文件留在状态机里照常打印，并落盘 + 重新防抖；
  · 只有「一份都不剩」时才收尾（`_cleanup_scheduled_order`），且日志写明原因。

用法: python other/tests/verify_scheduled_partial_download_fail.py   （exit 0 = 全部通过）
"""
import io
import os
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LOCAL_TOOL = os.path.join(REPO, "local_print_tool")

_SANDBOX = tempfile.mkdtemp(prefix="hn_partial_dl_fail_")
os.environ["APPDATA"] = _SANDBOX
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, LOCAL_TOOL)
os.chdir(LOCAL_TOOL)

from PySide6.QtWidgets import QApplication  # noqa: E402

_app = QApplication.instance() or QApplication([])

import gui      # noqa: E402
import paths    # noqa: E402

ok_all = True


def check(name, cond, detail=""):
    global ok_all
    if not cond:
        ok_all = False
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


class Sink:
    def __init__(self):
        self.lines = []

    def log(self, msg):
        self.lines.append(str(msg))

    def text(self):
        return "\n".join(self.lines)


class StubCloud:
    def __init__(self):
        self.fails = []
        self.rejects = []

    def report_fail(self, task_id, msg=""):
        self.fails.append((task_id, msg))

    def reject_task(self, task_id):
        self.rejects.append(task_id)

    def accept_task(self, task_id):
        pass

    def _get_cached_pdf(self, *a, **kw):
        return "", {}


def make_task(task_id, oid, name, status="pending"):
    t = gui.CloudTask({
        "task_id": task_id, "order_id": oid, "order_number": "HN20261110-8888",
        "file_name": name, "local_path": os.path.join(_SANDBOX, name),
        "copies": 1, "duplex": "on", "page_range": "", "page_count": 2, "status": "ready",
    })
    t.status = status
    return t


def new_win(tasks):
    w = gui.MainWindow(config_path=paths.config_path())
    sink = Sink()
    w._log = sink.log
    w._cloud_client = StubCloud()
    w._cloud_connected = True
    cleaned = []
    w._cleanup_scheduled_order = lambda oid, reason="": cleaned.append((oid, reason))
    w._sync_scheduled_queue_window = lambda *a, **kw: None
    w._restart_scheduled_debounce = lambda *a, **kw: None
    oid = tasks[0].order_id
    w._scheduled_orders[oid] = {
        "target_ts": 1, "ready": {}, "pending": {t.task_id: t for t in tasks},
        "timer": None, "print_timer": None, "frozen": False, "delayed_sent": False,
        "printed": False, "printed_ts": 0, "tab_key": None, "retry_count": 0, "queued": False,
    }
    return w, sink, cleaned, oid


# ══════════════════════════════════════════════════════════════════════════
print("\n=== ① 两份里失败一份：整单不许被扔掉 ===")
_t1 = make_task(3001, 1350, "ok1.pdf")
_t2 = make_task(3002, 1350, "bad.pdf")
w, sink, cleaned, OID = new_win([_t1, _t2])
_bad = make_task(3002, 1350, "bad.pdf", status="error")
_bad.error_message = "磁盘满"
w._update_scheduled_task(_bad)
_st = w._scheduled_orders.get(OID)
check("①-a 状态机还在", _st is not None)
check("①-b 没调用 _cleanup_scheduled_order", cleaned == [], str(cleaned))
check("①-c 失败的那份已从 pending 移除",
      _st is not None and 3002 not in _st["pending"])
check("①-d 另一份仍在 pending（等它下载完）",
      _st is not None and 3001 in _st["pending"],
      str(sorted(_st["pending"])) if _st else "-")
check("①-e 失败单独回报了 report_fail",
      w._cloud_client.fails and w._cloud_client.fails[0][0] == 3002,
      str(w._cloud_client.fails))
check("①-f 失败那份被打回 reject_task", w._cloud_client.rejects == [3002],
      str(w._cloud_client.rejects))
check("①-g 日志写明「其余照常打印」", "照常打印" in sink.text(), sink.text())
check("①-h 失败那份记为已处理（重启不再捡）", 3002 in w._processed_cloud_tasks)

# ══════════════════════════════════════════════════════════════════════════
print("\n=== ② 已经有一份就绪：照打就绪的那份 ===")
_t3 = make_task(3011, 1360, "ready.pdf", status="ready")
_t4 = make_task(3012, 1360, "bad2.pdf")
w2, sink2, cleaned2, OID2 = new_win([_t4])
w2._scheduled_orders[OID2]["ready"][3011] = _t3
_bad2 = make_task(3012, 1360, "bad2.pdf", status="error")
_bad2.error_message = "404"
w2._update_scheduled_task(_bad2)
_st2 = w2._scheduled_orders.get(OID2)
check("②-a 状态机还在", _st2 is not None)
check("②-b 没整单收尾", cleaned2 == [], str(cleaned2))
check("②-c 就绪那份还在", _st2 is not None and 3011 in _st2["ready"])
check("②-d 日志写明还剩几份照打",
      "其余 1 份照常打印" in sink2.text(), sink2.text())

# ══════════════════════════════════════════════════════════════════════════
print("\n=== ③ 一份都不剩：才收尾（并写明原因） ===")
_t5 = make_task(3021, 1370, "only.pdf")
w3, sink3, cleaned3, OID3 = new_win([_t5])
_bad3 = make_task(3021, 1370, "only.pdf", status="error")
_bad3.error_message = "无权限"
w3._update_scheduled_task(_bad3)
check("③-a 确实收尾了一次", len(cleaned3) == 1, str(cleaned3))
check("③-b 原因写清「无剩余可打文件」",
      cleaned3 and "无剩余" in str(cleaned3[0][1]), str(cleaned3))
check("③-c 仍然单独回报了失败", w3._cloud_client.fails and w3._cloud_client.fails[0][0] == 3021,
      str(w3._cloud_client.fails))

# ══════════════════════════════════════════════════════════════════════════
print("\n=== ④ 静态接线：error 分支里 cleanup 必须有前置条件 ===")
_src = open(os.path.join(LOCAL_TOOL, "gui.py"), encoding="utf-8").read()
_fn = _src.split("def _update_scheduled_task", 1)[1].split("\n    def ", 1)[0]
_err = _fn.split('if task.status == "error":', 1)[1].split('if task.status == "ready":', 1)[0]
check("④-a error 分支里仍有 _cleanup_scheduled_order", "_cleanup_scheduled_order" in _err)
check("④-b 但被「还有别的文件」挡住",
      'if not st["pending"] and not st["ready"]:' in _err, _err[-400:].replace("\n", " ⏎ "))
check("④-c 收尾原因写明「无剩余可打文件」", "无剩余可打文件" in _err)

print("\n" + ("全部通过 ✅" if ok_all else "存在失败 ❌"))
sys.exit(0 if ok_all else 1)
