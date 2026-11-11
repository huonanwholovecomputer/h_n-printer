# -*- coding: utf-8 -*-
"""攒批第 2 趟打完 ⇒ 每一份都必须「标成已完成」或「留下看得见的痕迹」。

2026-11-10 现场（HN20261109-0008 / -0009）：
    整叠第 2 趟 17:59:08 就报「完成（2 单 / 14 张纸 / 14 面）」，纸全部出槽；
    18:27 退出时却弹「有 2 个标签页还有没打印完的文件（共 6 个文件）」，
    并把这几单按「放弃打印」上报 —— 因为收尾时按入叠记下的 tab_key 去那一页找 job，
    号一错（标签页重编号 / ctx 丢号 / 该页被清空重建）就一个都找不到；本地单没有
    task_id 可兜底，于是 sent 永远是假，纸出来了而账没结。

本脚本钉住：
    ① 正常 tab_key → sent=True（基线）
    ② tab_key 丢了/被重编号 → 靠全标签页 file_path 兜底，仍 sent=True（本次修复）
    ③ 路径也变了（暂存副本重建）但显示名对得上 → 仍 sent=True（最后一级兜底）
    ④ 实在找不回 → sent=False **但必须有一行「找不回任务记录」的日志**（绝不静默）
    ⑤ 云端单（有 task_id）找不到 job 时仍按快照 report_success（既有行为，零回归）
    ⑥ 源码层：两级兜底与留痕分支都在
"""
import io
import os
import re
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LOCAL_TOOL = os.path.join(REPO, "local_print_tool")
_SANDBOX = tempfile.mkdtemp(prefix="hn_stack_sent_")
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


class FakeCloud:
    def __init__(self):
        self.success = []
        self.fail = []

    def report_success(self, tid, cfg=None):
        self.success.append((tid, dict(cfg or {})))

    def report_fail(self, tid, msg=""):
        self.fail.append((tid, msg))


def new_win():
    win = gui.MainWindow(config_path=paths.config_path())
    win._cloud_client = None
    win._logs = []
    win._log = win._logs.append
    return win


def make_job(name, task_id=0):
    path = os.path.join(_SANDBOX, name + ".pdf")
    with open(path, "wb") as f:
        f.write(b"%PDF-1.4\n%%EOF\n")
    return PrintJob(file_path=path, copies=1, duplex="on", duplex_mode="long-edge",
                    page_range="", page_count=2, display_name=name + ".pdf",
                    task_id=task_id)


def make_entry(tab_key, seq, jobs, paths=None, task_ids=None, order_number=""):
    return ds.StackEntry(
        seq=seq, title="t", tab_key=tab_key, order_number=order_number,
        page_count=2, sheet_count=1, pdf="x.pdf",
        file_plans=[{"label": "x", "pages": [1], "duplex": True}],
        tasks=[{"task_id": (task_ids or {}).get(i, 0), "copies": 1, "duplex": "on",
                "page_range": "", "page_count": 2, "display_name": j.display_name}
               for i, j in enumerate(jobs)],
        job_keys=[((task_ids or {}).get(i, 0), (paths or {}).get(i, j.file_path))
                  for i, j in enumerate(jobs)])


print("== ① 正常 tab_key ==")
w1 = new_win()
j1 = make_job("a")
w1._config.tabs["1"].jobs = [j1]
w1._finish_stack_entries([make_entry("1", 1, [j1])], True, "ok")
check("①-a 第 2 趟成功 → 这一单标成已完成", j1.sent is True, f"sent={j1.sent}")
check("①-b 正常路径不留「找不回」告警",
      not any("找不回任务记录" in s for s in w1._logs), f"{w1._logs}")

print("== ② tab_key 丢了 / 被重编号 ==")
w2 = new_win()
j2 = make_job("b")
w2._config.tabs["1"].jobs = [j2]
_e2 = make_entry("", 2, [j2])
_e2.tasks[0]["display_name"] = "改名了.pdf"   # 显示名对不上 ⇒ 只能靠路径兜底
w2._finish_stack_entries([_e2], True, "ok")
check("②-a tab_key 为空也要能把 job 找回来（全标签页按路径兜底）",
      j2.sent is True, f"sent={j2.sent}")
w2b = new_win()
j2b = make_job("b2")
w2b._config.tabs["5"] = gui.TabSettings(jobs=[j2b])
_e2b = make_entry("9", 3, [j2b])
_e2b.tasks[0]["display_name"] = "也改名了.pdf"
w2b._finish_stack_entries([_e2b], True, "ok")
check("②-b 标签页被重新编号（9 → 5）也要找回来", j2b.sent is True, f"sent={j2b.sent}")

print("== ③ 路径变了但显示名对得上 ==")
w3 = new_win()
j3 = make_job("c")
w3._config.tabs["2"] = gui.TabSettings(jobs=[j3])
e3 = make_entry("2", 4, [j3], paths={0: "C:/不存在的目录/c.pdf"})
w3._finish_stack_entries([e3], True, "ok")
check("③-a 暂存副本重建（路径全变）也要靠显示名找回来",
      j3.sent is True, f"sent={j3.sent}")

print("== ④ 实在找不回：必须留痕 ==")
w4 = new_win()
j4 = make_job("d")
e4 = make_entry("7", 5, [j4], paths={0: "C:/不存在/d-other.pdf"})
e4.tasks[0]["display_name"] = "找不到的名字.docx"
w4._finish_stack_entries([e4], True, "ok")
check("④-a 找不回时不能假装完成（sent 仍为假）", j4.sent is False, f"sent={j4.sent}")
check("④-b 找不回时必须打出「找不回任务记录」的告警（绝不静默）",
      any("找不回任务记录" in s for s in w4._logs), f"{w4._logs}")
_warn = next((s for s in w4._logs if "找不回任务记录" in s), "")
check("④-c 告警要说清后果（别直接退程序 / 会当放弃上报）",
      "放弃" in _warn or "已完成" in _warn, _warn)

print("== ⑤ 云端单找不到 job：仍按快照回报（零回归） ==")
w5 = new_win()
w5._cloud_client = FakeCloud()
e5 = ds.StackEntry(
    seq=6, title="已删标签页", tab_key="(已删除)", page_count=2, sheet_count=1,
    pdf="y.pdf", file_plans=[{"label": "y", "pages": [1], "duplex": True}],
    tasks=[{"task_id": 401, "copies": 1, "duplex": "on", "page_range": "",
            "page_count": 2, "owner_name": "测试成员", "is_admin_print": False,
            "display_name": "gone.pdf"}],
    job_keys=[(401, "C:/不存在/gone.pdf")])
w5._finish_stack_entries([e5], True, "ok")
check("⑤-a 云端单找不到 job → 仍 report_success(401)",
      [t for t, _ in w5._cloud_client.success] == [401], f"{w5._cloud_client.success}")
check("⑤-b 云端单不走「本地留痕」分支（它有 task_id 兜底）",
      not any("找不回任务记录" in s for s in w5._logs), f"{w5._logs}")

print("== ⑦ 一单两个文件：两份都要标成已完成（2026-11-11 现场） ==")
# 现场：tab1/2/3 都是「6 页那份 sent=True / 1 页那份 sent=False」成对出现。
# 原因是收尾按「整单全部路径」比，两个任务都命中第一个 job。
w7 = new_win()
ja = make_job("e1")          # 6 页那份
jb = make_job("e2")          # 1 页那份
w7._config.tabs["1"].jobs = [ja, jb]
e7 = make_entry("1", 7, [ja, jb])
for _t in e7.tasks:          # 模拟旧清单：tasks 里还没有 file_path
    _t.pop("file_path", None)
w7._finish_stack_entries([e7], True, "ok")
check("⑦-a 两份都标成已完成（不许只有第一份）",
      ja.sent is True and jb.sent is True, f"a={ja.sent} b={jb.sent}")
check("⑦-b 两份都标成已完成时不留「找不回」告警（一单两份都在本页）",
      not any("找不回任务记录" in s for s in w7._logs), f"{w7._logs}")

# 新清单形态：tasks 自带 file_path ⇒ 逐份精确命中（哪怕两个 job 的显示名一样）
w7b = new_win()
jc = make_job("e3")
jd = make_job("e4")
jd.display_name = jc.display_name            # 显示名撞车，只能靠路径区分
w7b._config.tabs["1"].jobs = [jc, jd]
e7b = make_entry("1", 8, [jc, jd])
for _t, _j in zip(e7b.tasks, (jc, jd)):
    _t["file_path"] = _j.file_path
w7b._finish_stack_entries([e7b], True, "ok")
check("⑦-c 显示名撞车也能靠逐份 file_path 各归各",
      jc.sent is True and jd.sent is True, f"c={jc.sent} d={jd.sent}")

print("== ⑥ 源码层 ==")
_src = io.open(os.path.join(LOCAL_TOOL, "gui.py"), encoding="utf-8").read()
_find = _src.split("def _find_job_for_stack_task", 1)[1].split("\n    def ", 1)[0]
check("⑥-a 查找顺序第一级是 task_id", "getattr(job, \"task_id\"" in _find)
check("⑥-b 有「先本单那一页、后全部标签页」的遍历（_iter_jobs）",
      "def _iter_jobs()" in _find and "own = self._config.tabs.get(getattr(entry, \"tab_key\"" in _find,
      "缺 _iter_jobs / own 页优先")
check("⑥-b2 先按逐份 file_path、再按逐份显示名，整单 keys 只做兜底",
      'tpath = str(task.get("file_path")' in _find
      and 'in keys' in _find and "_iter_jobs()" in _find,
      "缺逐份路径一级或整单 keys 兜底")
check("⑥-b3 同一单里领过的 job 不许再领一次（used 集合）",
      "_free(job)" in _find and "id(job) not in used" in _find,
      "缺 used 去重")
check("⑥-b4 整单 keys 兜底时优先还没置 sent 的那一个",
      "if not getattr(job, \"sent\", False):" in _find,
      "兜底层没优先挑未完成的")
check("⑥-c 有「按显示名」的最后一级",
      "display_name" in _find and "shown == name" in _find)
_fin = _src.split("def _finish_stack_entries(self", 1)[1].split("\n    def ", 1)[0]
check("⑥-d 收尾对「本地单找不到 job」有留痕分支", "elif not tid:" in _fin)
check("⑥-e 留痕文案含「找不回任务记录」", "找不回任务记录" in _fin)
check("⑥-f 收尾按「逐份、不重复」找回（传 used 并占位）",
      "used=used_ids" in _fin and "used_ids.add(id(job))" in _fin, "缺 used 传递/占位")
check("⑥-g 入叠清单逐份记下 file_path（旧清单只有 entry.job_keys）",
      '"file_path": it.get("file_path") or ""' in _src, "tasks 里没有 file_path")

print()
if _fails:
    print(f"❌ 存在失败（{len(_fails)} 条）：" + " / ".join(_fails))
    sys.exit(1)
print("✅ 全部通过")
