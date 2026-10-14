# -*- coding: utf-8 -*-
"""审计修复验收 · 🔴-A8：_add_files 每次添加都遍历全列表预转换（O(n²)）

事故形状：添加文件后预转换的循环写成了 `for i, job in enumerate(jobs)` ——
遍历**整个列表**而不是只有本次新增的行。于是添加第 N 个文件时，前 N-1 个文件
会被重新渲染一遍：逐个添加 4 个文件累计触发 1+2+3+4 = 10 次预转换（现场实测
"7 个图片渲染 18+ 次、首个文件 50 秒内被渲染 4 次"）。大文档/图片多时白烧 CPU，
且每次重渲染都会重写 PDF 缓存。

修法：`for i in range(rows_before, len(jobs))` —— 只处理新增行。

验收：真实 MainWindow + 桩掉 _start_convert_worker（记录被要求预转换的行号），
逐个添加 4 个非 PDF 文件，断言累计只触发 4 次、且没有任何一行被重复预转换。
"""
import io
import os
import sys
import tempfile

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL_TOOL = os.path.join(REPO, "local_print_tool")

_SANDBOX = tempfile.mkdtemp(prefix="hn_add_files_")
os.environ["APPDATA"] = _SANDBOX                  # 用户数据（配置/暂存副本）→ 沙箱
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, LOCAL_TOOL)
os.chdir(LOCAL_TOOL)

from PySide6.QtWidgets import QApplication     # noqa: E402
_app = QApplication.instance() or QApplication([])

import gui      # noqa: E402
import paths    # noqa: E402

ok_all = True


def check(name, cond, detail=""):
    global ok_all
    if not cond:
        ok_all = False
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


SRC = os.path.join(_SANDBOX, "src")
os.makedirs(SRC, exist_ok=True)


def mk(name, ext):
    p = os.path.join(SRC, name + ext)
    with open(p, "wb") as f:
        f.write(b"hello\n" * 3)
    return p


win = gui.MainWindow(config_path=paths.config_path())

# 桩：记录每次被要求预转换的行号（不真正转换）
calls = []          # 当前小节用（会被 clear）
total_calls = []    # 全流程累计，用于"没有任何行被处理两次"的整表判据


def _stub(row, file_path, engine="word"):
    calls.append(row)
    total_calls.append(row)


win._start_convert_worker = _stub

print("=== 1. 逐个添加 4 个非 PDF 文件：累计只应触发 4 次预转换 ===")
files = [mk(f"f{i}", ".txt") for i in range(4)]
for f in files:
    win._add_files_to_table([f])

print(f"    实际触发序列: {calls}")
check("累计触发次数 == 新增文件数（4）", len(calls) == 4,
      f"calls={calls}（改前为 1+2+3+4=10 次）")
check("没有任何一行被重复预转换（O(n²) 的直接判据）",
      len(calls) == len(set(calls)), f"重复行={[r for r in calls if calls.count(r) > 1]}")
check("被转换的正是 4 个新增行 0,1,2,3", sorted(calls) == [0, 1, 2, 3], f"{sorted(calls)}")

print("\n=== 2. 只增不重：再加 1 个文件后，旧行不再被碰 ===")
calls.clear()
win._add_files_to_table([mk("f4", ".txt")])
check("只触发 1 次预转换（第 5 行）", calls == [4], f"calls={calls}（改前为 0,1,2,3,4 共 5 次）")

print("\n=== 3. PDF 不参与预转换（原本就只对非 PDF）===")
calls.clear()
win._add_files_to_table([mk("f5", ".pdf")])
check("添加 PDF 不触发预转换", calls == [], f"calls={calls}")

print("\n=== 4. 同一批一次添加 3 个：只处理这 3 个新行 ===")
calls.clear()
win._add_files_to_table([mk("f6", ".txt"), mk("f7", ".png"), mk("f8", ".md")])
check("一次添加 3 个 → 恰好 3 次（行号连续且不重复）",
      sorted(calls) == [6, 7, 8], f"calls={sorted(calls)}")

print("\n=== 5. 整表统计：不存在任何行被处理两次 ===")
# 全流程应触发：4（逐个）+ 1（再加一个）+ 0（PDF）+ 3（一批三个）= 8 次，行号互不重复
check("累计 8 次、行号无重复（改前为 4+5+5+8=22 次）",
      len(total_calls) == 8 and len(total_calls) == len(set(total_calls)),
      f"total={total_calls}")

print("\n" + ("全部通过" if ok_all else "存在失败项"))
sys.exit(0 if ok_all else 1)
