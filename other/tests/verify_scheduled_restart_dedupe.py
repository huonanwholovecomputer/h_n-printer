# -*- coding: utf-8 -*-
"""预约单重启后「同一文件两行」的验收（🔴24 / 批次 A-2）

现场成因：预约单状态机（`_scheduled_state.json`）与配置文件（tabs + jobs）是两处各写各的。
重启后两边都会恢复，而 `_load_scheduled_state()` 恢复出来的 `tab_key` 恒为 None（`_save_scheduled_state()`
从来没写过这个字段）⇒ 到点走「建标签页」路径 → `_find_tab_for_order()` 命中**已经有一行的**旧页
→ 无条件 `jobs.append(job)` ⇒ 同一份文件在列表里出现两行、打印两遍、按两份收钱。
`task_id` 是后端给的文件唯一标识，本来就该拿它认人。

本脚本钉住：
  · 状态文件必须带上 `tab_key`（并能在读取后复原）；
  · 恢复出来的 `tab_key` 仍指向该订单自己的页（否则到点重建）；
  · `_add_cloud_task_to_new_tab()` 遇到同页已有同 `task_id` 的行 ⇒ **不 append**、只记日志；
  · 别的 `task_id` 零回归（照常追加）。

用法: python other/tests/verify_scheduled_restart_dedupe.py   （exit 0 = 全部通过）
"""
import io
import json
import os
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LOCAL_TOOL = os.path.join(REPO, "local_print_tool")

_SANDBOX = tempfile.mkdtemp(prefix="hn_restart_dedupe_")
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
    """只当「云端已启用」的开关 + 记住 accept_task 调用（不连网）。"""

    def __init__(self):
        self.accepted = []

    def accept_task(self, task_id):
        self.accepted.append(task_id)

    def _get_cached_pdf(self, *a, **kw):
        return "", {}


def new_win():
    w = gui.MainWindow(config_path=paths.config_path())
    sink = Sink()
    w._log = sink.log
    w._cloud_client = StubCloud()
    w._cloud_connected = True
    # 状态机外围（定时器/窗口）在无头环境下没必要真跑
    w._sync_scheduled_queue_window = lambda *a, **kw: None
    w._restart_scheduled_debounce = lambda *a, **kw: None
    w._evaluate_scheduled_order = lambda *a, **kw: None
    return w, sink


def make_task(task_id, order_id, name="a.pdf"):
    path = os.path.join(_SANDBOX, name)
    with open(path, "wb") as f:
        f.write(b"%PDF-1.4\n% fake\n")
    return gui.CloudTask({
        "task_id": task_id, "order_id": order_id, "order_number": "HN20261110-9999",
        "file_name": name, "local_path": path, "copies": 1, "duplex": "on",
        "page_range": "", "page_count": 2, "status": "ready",
    })


def put_tab(win, key, order_id, task_id, name="a.pdf"):
    tab = gui.TabSettings()
    tab.jobs = [gui.PrintJob(file_path=os.path.join(_SANDBOX, name),
                             order_id=order_id, task_id=task_id, page_count=2)]
    win._config.tabs[key] = tab
    return tab


# ══════════════════════════════════════════════════════════════════════════
print("\n=== ① 状态文件必须落 tab_key ===")
w, sink = new_win()
put_tab(w, "7", 1252, 2528)
w._scheduled_orders[1252] = {
    "target_ts": 1, "ready": {2528: make_task(2528, 1252)}, "pending": {},
    "timer": None, "print_timer": None, "frozen": False, "delayed_sent": False,
    "printed": False, "printed_ts": 0, "tab_key": "7", "retry_count": 0, "queued": False,
}
w._save_scheduled_state()
_state_path = w._scheduled_state_path()
_raw = json.load(open(_state_path, encoding="utf-8"))
check("①-a 状态文件存在", os.path.isfile(_state_path), _state_path)
check("①-b 写盘带了 tab_key", str(_raw.get("1252", {}).get("tab_key", "")) == "7",
      json.dumps(_raw.get("1252", {}), ensure_ascii=False)[:200])

# ══════════════════════════════════════════════════════════════════════════
print("\n=== ② 重启恢复：tab_key 复原，且仍归该订单 ===")
w2, sink2 = new_win()
w2._config.tabs.clear()
put_tab(w2, "7", 1252, 2528)
w2._load_scheduled_state()
_st = w2._scheduled_orders.get(1252)
check("②-a 预约单被恢复", _st is not None)
check("②-b 恢复出的 tab_key = 7", (_st or {}).get("tab_key") == "7", repr((_st or {}).get("tab_key")))
check("②-c `_scheduled_tab_still_owns` 认这页属于该单",
      w2._scheduled_tab_still_owns(1252, "7"))
check("②-d 恢复后表格里仍然只有一行（没被重复落地）",
      len(w2._config.tabs["7"].jobs) == 1,
      f"jobs={[j.task_id for j in w2._config.tabs['7'].jobs]}")

# ══════════════════════════════════════════════════════════════════════════
print("\n=== ③ 旧状态文件（没有 tab_key）+ 重放 ⇒ 靠 task_id 去重 ===")
w3, sink3 = new_win()
w3._config.tabs.clear()
put_tab(w3, "1", 1252, 2528)
_before = len(w3._config.tabs["1"].jobs)
_key = w3._add_cloud_task_to_new_tab(make_task(2528, 1252), is_first=True, switch_tab=False)
check("③-a 返回的是已有订单页", _key == "1", _key)
check("③-b 同 task_id 不再 append", len(w3._config.tabs["1"].jobs) == _before,
      f"before={_before} after={len(w3._config.tabs['1'].jobs)}")
check("③-c 日志讲清「已在标签页 / 不重复添加」",
      "不重复添加" in sink3.text(), sink3.text())
check("③-d 没有新建标签页", sorted(w3._config.tabs.keys()) == ["1"],
      sorted(w3._config.tabs.keys()))

# ══════════════════════════════════════════════════════════════════════════
print("\n=== ④ 零回归：不同 task_id 照常追加 ===")
w4, sink4 = new_win()
w4._config.tabs.clear()
put_tab(w4, "1", 1252, 2528)
_before4 = len(w4._config.tabs["1"].jobs)
w4._add_cloud_task_to_new_tab(make_task(2529, 1252, "b.pdf"), is_first=True, switch_tab=False)
check("④-a 新 task_id 正常追加", len(w4._config.tabs["1"].jobs) == _before4 + 1,
      f"jobs={[j.task_id for j in w4._config.tabs['1'].jobs]}")
check("④-b 追加的是 2529",
      w4._config.tabs["1"].jobs[-1].task_id == 2529,
      str(w4._config.tabs["1"].jobs[-1].task_id))
check("④-c 同页两行是两个不同 task",
      len({j.task_id for j in w4._config.tabs["1"].jobs}) == len(w4._config.tabs["1"].jobs))

# ══════════════════════════════════════════════════════════════════════════
print("\n=== ⑤ 另一单的同一页不受影响 ===")
w5, sink5 = new_win()
w5._config.tabs.clear()
put_tab(w5, "2", 1252, 2528)
_k5 = w5._add_cloud_task_to_new_tab(make_task(2528, 1400, "c.pdf"), is_first=True, switch_tab=False)
check("⑤-a 别的订单号 ⇒ 落到它自己的页（不是订单 1252 的 2 号页）", _k5 != "2", _k5)
check("⑤-b 订单 1252 的页没被塞进别的单的文件",
      [j.task_id for j in w5._config.tabs["2"].jobs] == [2528],
      str([j.task_id for j in w5._config.tabs["2"].jobs]))

# ══════════════════════════════════════════════════════════════════════════
print("\n=== ⑥ 静态接线 ===")
_src = open(os.path.join(LOCAL_TOOL, "gui.py"), encoding="utf-8").read()
_save_fn = _src.split("def _save_scheduled_state", 1)[1].split("\n    def ", 1)[0]
check("⑥-a _save_scheduled_state 落 tab_key", '"tab_key"' in _save_fn)
_load_fn = _src.split("def _load_scheduled_state", 1)[1].split("\n    def ", 1)[0]
check("⑥-b _load_scheduled_state 复原 tab_key", '"tab_key"' in _load_fn)
_add_fn = _src.split("def _add_cloud_task_to_new_tab", 1)[1].split("\n    def ", 1)[0]
check("⑥-c 追加前按 task_id 去重", "不重复添加" in _add_fn and "task.task_id" in _add_fn)

print("\n" + ("全部通过 ✅" if ok_all else "存在失败 ❌"))
sys.exit(0 if ok_all else 1)
