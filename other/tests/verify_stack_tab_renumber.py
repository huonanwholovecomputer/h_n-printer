# -*- coding: utf-8 -*-
"""标签页被删/重编号时，纸叠清单里的 tab_key 必须跟着搬（否则第 2 趟打完仍判「没打印完」）。

2026-11-10 现场（HN20261109-0008 / -0009）：攒批第 2 趟 17:59 就报「完成（2 单 / 14 张纸）」，
纸全出槽；18:27 退出却弹「有 2 个标签页还有没打印完的文件」并按「放弃打印」上报。
根因之一：收尾要按入叠时记下的 `entry.tab_key` 去那一页找回 `PrintJob`，而标签页被删/重编号后
清单里还留着旧 key —— 一个都对不上，本地单没有 task_id 可兜底，`sent` 永远不置真。

本脚本钉住（重编号是 `_renumber_tabs()` 的既有行为，这里只验「纸叠跟着搬」）：
    ① 存活标签页 2 → 1：清单里 tab_key "2" 的单一并改成 "1"，且**收尾能把 job 找回来标 sent**；
    ② 被删标签页：清单里 tab_key "1" 的项置空（**绝不保留旧 key 去撞重新编号后的另一页**），
       这一单的 job 已随页删除 ⇒ 收尾必须留痕（不许静默）；
    ③ 攒批排队（`_stack_wait` / `_stack_entry_wait`）与进行中上下文（`_stack_pending_ctx` /
       `_printing_tab_key`）一起重映射；
    ④ 清单写盘同步（重启续打读到的是新 key）；
    ⑤ 源码层：`_renumber_tabs()` 里确实调了 `_remap_stack_tab_keys(key_map)`。
"""
import io
import os
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LOCAL_TOOL = os.path.join(REPO, "local_print_tool")
_SANDBOX = tempfile.mkdtemp(prefix="hn_stack_renum_")
os.environ["APPDATA"] = _SANDBOX
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, LOCAL_TOOL)
os.chdir(LOCAL_TOOL)

from PySide6.QtWidgets import QApplication  # noqa: E402

_app = QApplication.instance() or QApplication([])

import duplex_stack as ds  # noqa: E402
import gui  # noqa: E402
import paths  # noqa: E402
from printer_config import PrintJob  # noqa: E402

_fails = []


def check(name, cond, detail=""):
    if cond:
        print(f"[PASS] {name}")
    else:
        _fails.append(name)
        print(f"[FAIL] {name}" + (f"  ←  {detail}" if detail else ""))


def make_job(name):
    path = os.path.join(_SANDBOX, name + ".pdf")
    with open(path, "wb") as f:
        f.write(b"%PDF-1.4\n%%EOF\n")
    return PrintJob(file_path=path, copies=1, duplex="on", duplex_mode="long-edge",
                    page_range="", page_count=2, display_name=name + ".pdf")


def entry(tab_key, seq, job):
    return ds.StackEntry(
        seq=seq, title="t", tab_key=tab_key, page_count=2, sheet_count=1, pdf="x.pdf",
        file_plans=[{"label": "x", "pages": [1], "duplex": True}],
        tasks=[{"task_id": 0, "copies": 1, "duplex": "on", "page_range": "",
                "page_count": 2, "display_name": job.display_name}],
        job_keys=[(0, job.file_path)])


win = gui.MainWindow(config_path=paths.config_path())
win._cloud_client = None
_logs = []
win._log = (lambda msg: _logs.append(str(msg)))

jA, jB = make_job("被删标签页的单"), make_job("存活标签页的单")
win._config.tabs["1"] = gui.TabSettings(jobs=[jA])
win._config.tabs["2"] = gui.TabSettings(jobs=[jB])
win._current_tab = "2"

# 纸叠：两单都已在出纸槽里（第 1 趟打完、等着打第 2 趟）
win._stack.batch_id = "RENUMTEST"
os.makedirs(win._stack_batch_dir(), exist_ok=True)
e1 = entry("1", 1, jA)
e2 = entry("2", 2, jB)
win._stack.entries = [e1, e2]
ds.save_manifest(win._stack_batch_dir(), win._stack)
win._stack_pending_ctx = {"tab_key": "2", "owner_name": "", "is_admin_print": False,
                          "order_id": 0, "order_number": ""}
win._printing_tab_key = "2"
win._stack_wait = [{"jobs": [jB], "tab_key": "2", "reason": "等纸叠"}]
win._stack_entry_wait = [{"jobs": [jB], "tab_key": "2", "reason": "排队"}]

print("== 基线 ==")
check("①-a 重编号前清单归属正确", [e1.tab_key, e2.tab_key] == ["1", "2"],
      f"{[e1.tab_key, e2.tab_key]}")

print("== 删掉标签页 1 → 2 被重编号成 1 ==")
win._config.tabs.pop("1")
win._renumber_tabs()
check("①-b 存活页的清单项搬到新 key", e2.tab_key == "1", f"tab_key={e2.tab_key!r}")
check("①-c 被删页的清单项置空（不许留着旧 key 撞新页）", e1.tab_key == "",
      f"tab_key={e1.tab_key!r}")
check("①-d 攒批排队项一起搬", [it["tab_key"] for it in win._stack_wait] == ["1"],
      f"{[it['tab_key'] for it in win._stack_wait]}")
check("①-e 攒批入口排队项一起搬", [it["tab_key"] for it in win._stack_entry_wait] == ["1"],
      f"{[it['tab_key'] for it in win._stack_entry_wait]}")
check("①-f 进行中上下文与在打页号一起搬",
      win._stack_pending_ctx.get("tab_key") == "1" and win._printing_tab_key == "1",
      f"{win._stack_pending_ctx.get('tab_key')}, {win._printing_tab_key}")

print("== 清单写盘也要是新 key（崩溃续打读到的是它） ==")
_m = ds.load_manifest(win._stack_batch_dir())
_keys = {it.tab_key for it in (_m.entries if _m else [])}
check("② 落盘清单里的 tab_key 已重映射", _keys == {"1", ""}, f"{_keys}")

print("== 收尾：重编号那一单必须标完成；被删页那一单必须留痕 ==")
_logs.clear()
win._finish_stack_entries([e1, e2], True, "整叠第 2 趟完成")
check("③-a 重编号那一单标完成（这才是现场那笔账）", jB.sent is True, f"sent={jB.sent}")
check("③-b 被删页那一单真没 job 了 → 不许静默，必须留痕并说清后果",
      any("找不回任务记录" in m and "放弃打印" in m for m in _logs),
      f"logs={_logs}")

print("== 源码层 ==")
_src = io.open(os.path.join(LOCAL_TOOL, "gui.py"), encoding="utf-8").read()
_ren = _src.split("def _renumber_tabs", 1)[1].split("\n    def ", 1)[0]
check("④-a _renumber_tabs 里调了 _remap_stack_tab_keys", "_remap_stack_tab_keys(key_map)" in _ren)
_remap = _src.split("def _remap_stack_tab_keys", 1)[1].split("\n    def ", 1)[0]
check("④-b 重映射覆盖纸叠清单/排队/上下文/在打页号",
      all(k in _remap for k in ("self._stack", "_stack_wait", "_stack_entry_wait",
                                "_stack_pending_ctx", "_printing_tab_key")))
check("④-c 映射不到时置空而不是保留旧 key", 'key_map.get(old_key, "")' in _remap)
check("④-d 改完要写盘", "save_manifest(" in _remap)

print()
if _fails:
    print(f"❌ 存在失败（{len(_fails)} 条）：" + " / ".join(_fails))
    sys.exit(1)
print("✅ 全部通过")
