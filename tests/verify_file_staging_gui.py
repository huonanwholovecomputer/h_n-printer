# -*- coding: utf-8 -*-
"""验收脚本 · 压缩包拖拽兼容（GUI 集成冒烟：真 MainWindow + 真表格）

在 offscreen 平台下实例化真实 MainWindow，走「添加文件 → 列表 → 移除 → 清理」全流程，
验证列表任务确实使用暂存副本、原件删除后仍可打印、副本在任务移除后被回收。

    python tests/verify_file_staging_gui.py     # exit 0 = 全绿
"""
import io
import os
import shutil
import sys
import tempfile
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL_TOOL = os.path.join(REPO, "local_print_tool")

_SANDBOX = tempfile.mkdtemp(prefix="hn_staging_gui_")
os.environ["APPDATA"] = _SANDBOX                 # 用户数据目录 → 沙箱
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, LOCAL_TOOL)
os.chdir(LOCAL_TOOL)

import paths          # noqa: E402
import staging        # noqa: E402
from printer_config import PrintJob  # noqa: E402

ok_all = True


def check(name, cond, detail=""):
    global ok_all
    if not cond:
        ok_all = False
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


# ── 造一个「解压软件临时目录」里的真 PDF ──
from reportlab.pdfgen import canvas  # noqa: E402
from reportlab.lib.pagesizes import A4  # noqa: E402

FAKE_TEMP = os.path.join(_SANDBOX, "Temp", "7zO4A2B1C")
os.makedirs(FAKE_TEMP, exist_ok=True)
SRC = os.path.join(FAKE_TEMP, "拖出来的报告.pdf")
c = canvas.Canvas(SRC, pagesize=A4)
c.drawString(72, 720, "hello staging")
c.showPage()
c.save()

from PySide6.QtWidgets import QApplication  # noqa: E402
app = QApplication.instance() or QApplication([])

import gui as _gui  # noqa: E402

# 关闭窗口时若有未打印任务会弹确认框（offscreen 下无人可答会死等）→ 自动回「是」
_orig_question = _gui.QMessageBox.question
_gui.QMessageBox.question = staticmethod(lambda *a, **k: _gui.QMessageBox.Yes)
_gui.QMessageBox.warning = staticmethod(lambda *a, **k: _gui.QMessageBox.Ok)
_gui.QMessageBox.information = staticmethod(lambda *a, **k: _gui.QMessageBox.Ok)

from gui import MainWindow  # noqa: E402

cfg_path = paths.config_path()
print("… 正在创建主窗口", flush=True)
win = MainWindow(config_path=cfg_path)
win.show()
print("… 主窗口就绪", flush=True)

print("=== 1. 拖拽添加（走真实 _add_files_to_table）===")
win._add_files_to_table([SRC])
jobs = win._get_current_jobs()
check("任务已加入列表", len(jobs) == 1, f"n={len(jobs)}")
if jobs:
    job = jobs[0]
    check("列表任务使用暂存副本", staging.is_staged(job.file_path), job.file_path)
    check("原件路径记录在 source_path", job.source_path == os.path.abspath(SRC), job.source_path)
    check("表格显示原始文件名（副本名不露脸）",
          win._table.item(0, win.COL_FILE).text() == os.path.basename(SRC),
          win._table.item(0, win.COL_FILE).text())
    check("双击/打开位置用的路径指向原件",
          os.path.normcase(win._job_user_path(0)) == os.path.normcase(os.path.abspath(SRC)))
    check("工具提示展示原件路径", os.path.abspath(SRC) in win._table.item(0, win.COL_FILE).toolTip())

    # 同一文件重复添加 → 跳过（沿用原逻辑）
    win._add_files_to_table([SRC])
    check("同一文件重复添加被跳过", len(win._get_current_jobs()) == 1)

print("=== 2. 原件（解压临时目录）被清理后仍可打印 ===")
shutil.rmtree(FAKE_TEMP)
check("原件已消失", not os.path.isfile(SRC))
check("任务文件仍存在（打印不会报文件不存在）",
      bool(jobs) and os.path.isfile(jobs[0].file_path))

print("=== 3. 打印前兜底：副本被外部删除 → 从原件重建 ===")
# 模拟「副本被杀软/清理工具删掉，但原件还在」：恢复原件 + 删掉副本
os.makedirs(FAKE_TEMP, exist_ok=True)
shutil.copy2(jobs[0].file_path, SRC)
os.remove(jobs[0].file_path)
check("副本已被人为删除", not os.path.isfile(jobs[0].file_path))
# 只验证兜底逻辑本身（不真正启动打印线程）
_src = jobs[0].source_path
_new = staging.stage_file(_src)
check("可从原件重建副本", os.path.isfile(_new))

print("=== 4. 移除任务 → 暂存副本回收 ===")
win._table.selectRow(0)
win._on_remove_selected()
check("任务已从列表移除", len(win._get_current_jobs()) == 0)
check("暂存副本已回收（不残留占盘）", not os.path.isfile(_new))
# 副本目录：刚清空时留宽限期（防「建目录→马上写入」被清扫打断），过期后回收
_copy_dir = os.path.dirname(_new)
if os.path.isdir(_copy_dir):
    os.utime(_copy_dir, (time.time() - 3600, time.time() - 3600))
    win._sweep_staged_files("测试：空目录宽限过期")
check("宽限过期后副本目录被回收", not os.path.isdir(_copy_dir))
check("无任务时暂存目录无残留文件", staging.cache_size()[0] == 0, str(staging.cache_size()))

print("=== 5. 清空列表（撤回窗口）不影响副本，撤回后仍可打印 ===")
win._add_files_to_table([SRC])
jobs = win._get_current_jobs()
_copy = jobs[0].file_path if jobs else ""
win._on_clear_list()
check("清空后副本仍在（5 秒内可撤回）", os.path.isfile(_copy))
win._on_undo_clear()
check("撤回后任务恢复", len(win._get_current_jobs()) == 1)
check("撤回后副本仍可打印", os.path.isfile(win._get_current_jobs()[0].file_path))

print("=== 6. 云端任务：待确认期间文件不被清掉，落地后按任务存活 ===")
from dataclasses import dataclass  # noqa: E402


@dataclass
class _FakeCloudTask:
    task_id: int = 777
    local_path: str = ""
    order_id: int = 0


# 模拟 CloudClient 落在暂存目录的下载文件（cloud_<task_id>\<原文件名>）
cloud_dst = staging.cloud_download_path(777, "云端订单.pdf")
os.makedirs(os.path.dirname(cloud_dst), exist_ok=True)
with open(cloud_dst, "wb") as f:
    f.write(b"%PDF-1.4 pretend cloud download")
win._cloud_tasks[777] = _FakeCloudTask(task_id=777, local_path=cloud_dst)
win._sweep_staged_files("测试：待确认的云端任务")
check("待确认的云端文件不被误删（接受后仍可打印）", os.path.isfile(cloud_dst))

# 用户打回/任务取消 → 不再被引用 → 下一次清扫回收
win._cloud_tasks.pop(777, None)
win._sweep_staged_files("测试：云端任务已打回")
check("打回后的云端文件被回收", not os.path.isfile(cloud_dst))

print("=== 7. 旧版遗留 %TEMP% 云端文件在启动时被接管 ===")
import tempfile  # noqa: E402

_legacy = os.path.join(tempfile.gettempdir(), "hn_cloud_999_旧版云端文件.pdf")
with open(_legacy, "wb") as f:
    f.write(b"%PDF-1.4 legacy temp download")
_legacy_job = PrintJob(file_path=_legacy, copies=1, display_name="旧版云端文件.pdf",
                       task_id=999, order_id=999)
win._get_current_jobs().append(_legacy_job)
_adopted = win._adopt_legacy_cloud_files()
check("旧版 %TEMP% 云端文件被识别并复制进暂存目录",
      _adopted == 1 and staging.is_staged(_legacy_job.file_path)
      and os.path.isfile(_legacy_job.file_path),
      f"adopted={_adopted} path={_legacy_job.file_path}")
check("接管后文件内容一致", open(_legacy_job.file_path, "rb").read() == b"%PDF-1.4 legacy temp download")
check("原件缺失时副本仍可打印（模拟旧版 24h 清理删掉 %TEMP% 文件）",
      os.path.isfile(_legacy_job.file_path))
check("已在暂存目录的任务不会重复接管", win._adopt_legacy_cloud_files() == 0)
# 收尾：清掉这个任务（模拟用户移除），并删除造的旧版临时文件
win._get_current_jobs().remove(_legacy_job)
try:
    os.remove(_legacy)
except OSError:
    pass

print("=== 8. 关闭窗口 → 退出清理 ===")
win.close()
check("退出后暂存副本全部清理", staging.cache_size()[0] == 0, str(staging.cache_size()))
check("退出后任务列表已清空", win._get_current_jobs() == [])

app.processEvents()
shutil.rmtree(_SANDBOX, ignore_errors=True)
print("\n" + ("全部通过 ✅" if ok_all else "存在失败项 ❌"))
sys.exit(0 if ok_all else 1)
