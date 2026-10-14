# -*- coding: utf-8 -*-
"""审计修复验收 · 🔴-6 .processed_tasks.json 放在程序目录（写失败静默 ⇒ 重复打印）

事故形状：_processed_tasks_path() 返回 `os.path.dirname(__file__)/.processed_tasks.json`，
即**程序安装目录**。装到 C:\\Program Files 后该目录不可写，而 _mark_processed_task
的 `except Exception: pass` 把失败静静吞掉 ⇒「已处理任务」集合无法持久化 ⇒ 重启后
同一批云端任务被**重复打印**（顾客白付一次钱），且全程无任何提示。
同时该文件属于用户数据，本就不该跟程序走（重装/升级会丢）。

修法：路径改到 %APPDATA%\\HN打印工具（paths.get_app_data_dir()），旧文件由既有的
paths.migrate_legacy_data 一次性幂等迁移；读/写失败改为留痕（logger.warning/error）。
"""
import io
import json
import logging
import os
import shutil
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL_TOOL = os.path.join(REPO, "local_print_tool")

_SANDBOX = tempfile.mkdtemp(prefix="hn_proc_tasks_")
os.environ["APPDATA"] = _SANDBOX                  # 用户数据目录 → 沙箱（不碰真实配置）
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, LOCAL_TOOL)
os.chdir(LOCAL_TOOL)

from PySide6.QtWidgets import QApplication  # noqa: E402

_app = QApplication.instance() or QApplication([])

import gui    # noqa: E402
import paths  # noqa: E402

# ── 自我保洁 ──
# 本脚本用于反向验证时会临时换回"改前代码"，那时 _processed_tasks_path() 指向
# 程序目录，测试用 ID 会被写进真实的 local_print_tool/.processed_tasks.json
# （污染本机运行数据）。这里在退出时把该文件还原（仅内容不一致才写回）。
_LEGACY_REAL = os.path.join(LOCAL_TOOL, ".processed_tasks.json")
_LEGACY_BACKUP = None
if os.path.isfile(_LEGACY_REAL):
    with open(_LEGACY_REAL, "r", encoding="utf-8") as _f:
        _LEGACY_BACKUP = _f.read()


def _restore_legacy():
    if _LEGACY_BACKUP is None:
        return
    try:
        with open(_LEGACY_REAL, "r", encoding="utf-8") as _f:
            now = _f.read()
        if now != _LEGACY_BACKUP:
            with open(_LEGACY_REAL, "w", encoding="utf-8") as _f:
                _f.write(_LEGACY_BACKUP)
            print("[保洁] 已将程序目录 .processed_tasks.json 还原（反向验证造成的污染）")
    except OSError:
        pass


import atexit  # noqa: E402

atexit.register(_restore_legacy)

ok_all = True


def check(name, cond, detail=""):
    global ok_all
    if not cond:
        ok_all = False
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


APP_DATA = paths.get_app_data_dir()
TARGET = os.path.join(APP_DATA, ".processed_tasks.json")
SCRIPT_DIR = os.path.join(_SANDBOX, "fake_install_dir")   # 模拟"C:\\Program Files\\..."程序目录
os.makedirs(SCRIPT_DIR, exist_ok=True)

print("=== 1. 路径必须落在用户数据目录，且不在程序目录 ===")
win = gui.MainWindow(config_path=paths.config_path())
p = win._processed_tasks_path()
check("路径位于 %APPDATA%\\HN打印工具 下", os.path.dirname(p) == APP_DATA,
      f"dir={os.path.dirname(p)!r} 期望 {APP_DATA!r}")
check("文件名不变（仍为 .processed_tasks.json，兼容旧文件）",
      os.path.basename(p) == ".processed_tasks.json", os.path.basename(p))
check("不再指向 local_print_tool 程序目录",
      os.path.dirname(os.path.abspath(p)) != os.path.abspath(LOCAL_TOOL),
      f"{os.path.dirname(os.path.abspath(p))!r} != {os.path.abspath(LOCAL_TOOL)!r}")
check("源码中已无『程序目录 + .processed_tasks.json』的写法",
      'os.path.dirname(os.path.abspath(__file__)), ".processed_tasks.json"' not in
      open(os.path.join(LOCAL_TOOL, "gui.py"), encoding="utf-8").read())

print("\n=== 2. 旧文件（程序目录）被幂等迁移到新位置且内容保留 ===")
legacy = os.path.join(SCRIPT_DIR, ".processed_tasks.json")
with open(legacy, "w", encoding="utf-8") as f:
    json.dump([11, 22, 33], f)
paths.migrate_legacy_data(SCRIPT_DIR)
check("旧文件内容已复制到新位置", os.path.exists(TARGET), f"exists={os.path.exists(TARGET)}")
if os.path.exists(TARGET):
    with open(TARGET, "r", encoding="utf-8") as f:
        got = json.load(f)
    check("内容完整保留 [11,22,33]", got == [11, 22, 33], f"{got}")
check("旧文件保留兜底（复制而非删除）", os.path.exists(legacy), f"exists={os.path.exists(legacy)}")
check("迁移后 MainWindow 能读到旧集合",
      win._load_processed_tasks() == {11, 22, 33}, f"{win._load_processed_tasks()}")

print("\n=== 3. 迁移幂等：新位置已有数据时不被旧文件覆盖 ===")
with open(TARGET, "w", encoding="utf-8") as f:
    json.dump([99], f)                 # 模拟新数据已经写过
paths.migrate_legacy_data(SCRIPT_DIR)
with open(TARGET, "r", encoding="utf-8") as f:
    got = json.load(f)
check("新数据优先（仍是 [99]，未被旧文件 [11,22,33] 覆盖）", got == [99], f"{got}")

print("\n=== 4. 写失败必须留痕（原来是 except: pass 静默）===")


class _Catcher(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


catcher = _Catcher()
gui.logger.addHandler(catcher)
gui.logger.setLevel(logging.WARNING)
try:
    bad_dir = os.path.join(_SANDBOX, "not_a_file")
    os.makedirs(bad_dir, exist_ok=True)
    win._processed_tasks_path = lambda: bad_dir      # open(目录, "w") → IsADirectoryError
    win._mark_processed_task(4242)
    errs = [r for r in catcher.records if r.levelno >= logging.ERROR]
    check("写失败被记录为 error（不再静默）", len(errs) >= 1,
          f"records={[(r.levelname, r.getMessage()) for r in catcher.records]}")
    check("错误信息说明了后果（重复打印）",
          any("重复打印" in r.getMessage() for r in errs),
          "; ".join(r.getMessage() for r in errs))
finally:
    gui.logger.removeHandler(catcher)

print("\n=== 5. 文件缺失（首次运行）不应刷日志 ===")
for f in (TARGET,):
    if os.path.exists(f):
        os.remove(f)
catcher2 = _Catcher()
gui.logger.addHandler(catcher2)
gui.logger.setLevel(logging.WARNING)
try:
    fresh = gui.MainWindow(config_path=paths.config_path())
    got = fresh._load_processed_tasks()
    check("首次运行返回空集合", got == set(), f"{got}")
    check("首次运行不产生任何 warning/error（避免每次启动刷屏）",
          len(catcher2.records) == 0,
          f"{[(r.levelname, r.getMessage()) for r in catcher2.records]}")
finally:
    gui.logger.removeHandler(catcher2)

print("\n=== 6. 跨重启持久化往返（正常路径）===")
win2 = gui.MainWindow(config_path=paths.config_path())
win2._mark_processed_task(777)
win2._mark_processed_task(888)
check("写入后文件存在", os.path.exists(TARGET), f"exists={os.path.exists(TARGET)}")
fresh2 = gui.MainWindow(config_path=paths.config_path())
loaded = fresh2._load_processed_tasks()
check("重新加载（模拟重启）能读回 {777,888}", loaded == {777, 888}, f"{loaded}")
check("_is_processed_task 对已记录任务返回 True", fresh2._is_processed_task(777) is True)

for w in (win, fresh, win2, fresh2):
    try:
        w.close()
    except Exception:
        pass

print("\n" + ("全部通过" if ok_all else "存在失败项"))
shutil.rmtree(_SANDBOX, ignore_errors=True)
sys.exit(0 if ok_all else 1)
