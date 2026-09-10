# -*- coding: utf-8 -*-
"""验收脚本 · 压缩包拖拽兼容（文件暂存副本 staging.py）

背景：从压缩包（zip/rar/7z）里直接拖出来的文件位于解压软件的临时目录
（%TEMP%\\Rar$DIa0.123\\...），解压软件退出/临时目录被清理后原文件就没了，
旧逻辑要等到打印时才报「文件不存在」。

修复：入列表前复制一份「工作副本」到 %APPDATA%\\HN打印工具\\file_cache，
列表任务只用副本（PrintJob.file_path），原件路径记录在 PrintJob.source_path；
副本按引用清扫（启动/移除/清空撤回结束/退出）。

本脚本用真实模块 + 真实 PrintJob/PrinterConfig 跑行为，纯 Python、无需 Qt：
    python tests/verify_file_staging.py      # exit 0 = 全绿
"""
import io
import json
import os
import shutil
import sys
import tempfile
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL_TOOL = os.path.join(REPO, "local_print_tool")

# 用户数据目录改到临时沙箱（staging/paths 均按 APPDATA 环境变量解析，必须在导入前设置）
_SANDBOX_APPDATA = tempfile.mkdtemp(prefix="hn_staging_test_")
os.environ["APPDATA"] = _SANDBOX_APPDATA
sys.path.insert(0, LOCAL_TOOL)

import paths            # noqa: E402
import staging          # noqa: E402
from printer_config import PrintJob, PrinterConfig, TabSettings  # noqa: E402

ok_all = True


def check(name, cond, detail=""):
    global ok_all
    if not cond:
        ok_all = False
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


def src_of(rel):
    with open(os.path.join(LOCAL_TOOL, rel), encoding="utf-8") as f:
        return f.read()


# 模拟「解压软件临时目录里的文件」
FAKE_TEMP = os.path.join(_SANDBOX_APPDATA, "Temp", "Rar$DIa0.123")
os.makedirs(FAKE_TEMP, exist_ok=True)
SRC = os.path.join(FAKE_TEMP, "报告 2026.docx")
with open(SRC, "wb") as f:
    f.write(b"PK\x03\x04" + b"fake docx content" * 100)

print("=== 1. 加入列表即复制工作副本 ===")
work = staging.stage_file(SRC)
check("返回副本路径（不是原件）", work != SRC and os.path.isfile(work), work)
check("副本位于 file_cache 暂存目录", staging.is_staged(work)
      and os.path.normcase(os.path.dirname(os.path.dirname(work)))
      == os.path.normcase(paths.file_cache_dir()), work)
check("副本保留原文件名（显示名/报表名零改动）",
      os.path.basename(work) == os.path.basename(SRC), os.path.basename(work))
with open(SRC, "rb") as f:
    _a = f.read()
with open(work, "rb") as f:
    _b = f.read()
check("副本内容与原件一致", _a == _b)
check("原件路径被识别为非暂存", not staging.is_staged(SRC))
check("副本再次 stage 不再复制（幂等复用）", staging.stage_file(work) == work)

print("=== 2. 原件消失后仍可打印（核心场景）===")
job = PrintJob(file_path=work, source_path=SRC, copies=1, duplex="on")
shutil.rmtree(FAKE_TEMP)   # 解压软件退出/临时目录被系统清理
check("原件已不存在", not os.path.isfile(SRC))
check("任务使用的副本仍在（打印不再报文件不存在）", os.path.isfile(job.file_path))
check("任务保留原件路径（显示/双击打开用）", job.source_path == SRC)

print("=== 3. 原文件被改动 → 重新添加拿到新内容 ===")
os.makedirs(FAKE_TEMP, exist_ok=True)
with open(SRC, "wb") as f:
    f.write(b"PK\x03\x04" + b"v2 content" * 200)
time.sleep(0.02)
work2 = staging.stage_file(SRC)
check("同一源路径映射到同一副本路径（不产生重复副本）", work2 == work)
with open(work2, "rb") as f:
    check("副本已刷新为最新内容", b"v2 content" in f.read())

print("=== 4. 副本按引用清扫 ===")
N, total = staging.cache_size()
check("cache_size 统计到副本", N >= 1 and total > 0, f"n={N} bytes={total}")

kept = staging.sweep({work})
check("仍被引用的副本不会被删", os.path.isfile(work) and kept == 0, f"removed={kept}")

# 再造一个「已无人引用」的副本（模拟任务被移除）
ORPHAN = os.path.join(FAKE_TEMP, "孤儿.pdf")
with open(ORPHAN, "wb") as f:
    f.write(b"%PDF-1.4 orphan")
orphan_work = staging.stage_file(ORPHAN)
check("孤儿副本已建立", os.path.isfile(orphan_work))

removed = staging.sweep({work})
check("未被引用的副本被清理", removed == 1 and not os.path.isfile(orphan_work), f"removed={removed}")
check("引用中的副本仍在", os.path.isfile(work))
# 空目录留宽限期（防「刚建好目录、马上写入」时目录被清扫删掉）→ 目录时间调旧后再扫
_od = os.path.dirname(orphan_work)
check("刚清空的子目录暂留（写入宽限）", os.path.isdir(_od))
os.utime(_od, (time.time() - 3600, time.time() - 3600))
staging.sweep({work})
check("宽限期过后空子目录被回收", not os.path.isdir(_od))

# 宽限期：新副本即使未被引用也不动（启动兜底留给崩溃前刚复制的副本）
ORPHAN2 = os.path.join(FAKE_TEMP, "新孤儿.pdf")
with open(ORPHAN2, "wb") as f:
    f.write(b"%PDF-1.4 fresh")
orphan2_work = staging.stage_file(ORPHAN2)
removed2 = staging.sweep({work}, min_age_seconds=600)
check("宽限期内不动未引用副本", removed2 == 0 and os.path.isfile(orphan2_work))
removed3 = staging.sweep({work})
check("宽限期过后仍会清理", removed3 == 1 and not os.path.isfile(orphan2_work))

print("=== 5. 复制失败的降级（功能不因暂存而不可用）===")
missing = os.path.join(FAKE_TEMP, "不存在.pdf")
check("源文件不存在 → 原样返回", staging.stage_file(missing) == missing)
check("空路径 → 原样返回", staging.stage_file("") == "")

# 暂存目录不可用（无权限/盘满/路径非法）→ 仍然返回原文件，添加文件流程不受影响
_orig_cache_dir = paths.file_cache_dir
def _boom():
    raise OSError("模拟暂存目录不可用")
paths.file_cache_dir = _boom
try:
    check("暂存目录不可用 → stage_file 回退原路径", staging.stage_file(SRC) == SRC)
    check("暂存目录不可用 → sweep 安全返回 0", staging.sweep(set()) == 0)
    check("暂存目录不可用 → cache_size 安全返回 (0,0)", staging.cache_size() == (0, 0))
finally:
    paths.file_cache_dir = _orig_cache_dir

print("=== 6. 云端任务下载文件也走暂存目录（同类隐患）===")
cloud_dst = staging.cloud_download_path(4242, "云端的报告.pdf")
check("下载目标位于暂存目录", staging.is_staged(cloud_dst), cloud_dst)
check("下载目标按 task_id 分目录", os.path.basename(os.path.dirname(cloud_dst)) == "cloud_4242")
check("下载目标保留后端原始文件名", os.path.basename(cloud_dst) == "云端的报告.pdf")
check("不同任务互不覆盖",
      staging.cloud_download_path(4243, "云端的报告.pdf") != cloud_dst)
check("非法文件名被净化",
      os.sep not in os.path.basename(staging.cloud_download_path(1, 'a/b:c*d?.pdf')))
check("task_id 非法也不炸", bool(staging.cloud_download_path("x", "a.pdf")))

os.makedirs(os.path.dirname(cloud_dst), exist_ok=True)
with open(cloud_dst, "wb") as f:
    f.write(b"%PDF-1.4 cloud download")

# 「使用中」标记：下载完成到挂到任务之间的窗口内，清扫不得删掉
staging.reserve(cloud_dst)
check("下载中的文件被清扫保留", staging.sweep({work}) == 0 and os.path.isfile(cloud_dst))
staging.unreserve(cloud_dst)
check("解除标记后无人引用 → 被回收", staging.sweep({work}) == 1 and not os.path.isfile(cloud_dst))

print("=== 7. 配置持久化（崩溃重启后仍指向副本 + 记得原件）===")
cfg = PrinterConfig()
cfg.tabs = {"1": TabSettings(jobs=[job])}
cfg_path = os.path.join(_SANDBOX_APPDATA, "print_config.json")
cfg.save(cfg_path)
with open(cfg_path, encoding="utf-8") as f:
    raw = json.load(f)
saved = raw["tabs"]["1"]["jobs"][0] if isinstance(raw["tabs"]["1"], dict) else raw["tabs"]["1"][0]
check("配置里写入了 source_path", saved.get("source_path") == SRC, str(saved.get("source_path")))
reloaded = PrinterConfig.load(cfg_path)
rj = reloaded.tabs["1"].jobs[0]
check("重新加载后 file_path 指向副本", rj.file_path == work)
check("重新加载后 source_path 指向原件", rj.source_path == SRC)
check("重新加载后副本仍可用于打印", os.path.isfile(rj.file_path))

print("=== 8. GUI / 云客户端接线检查（字符串级防回归）===")
gui_src = src_of("gui.py")
cloud_src = src_of("cloud_client.py")
check("添加文件核心逻辑调用 staging.stage_file",
      "staging.stage_file(src)" in gui_src)
check("PrintJob 记录了 source_path=src",
      "source_path=src" in gui_src)
check("启动时清理暂存副本", '_sweep_staged_files("启动清理"' in gui_src)
check("退出时清理暂存副本", '_sweep_staged_files("退出清理"' in gui_src)
check("移除任务后清理暂存副本", '_sweep_staged_files("移除任务"' in gui_src)
check("清空撤回窗口结束/取消时清理",
      '_sweep_staged_files("清空已确认"' in gui_src)
check("删除标签页/已完成订单时清理",
      '_sweep_staged_files("删除标签页"' in gui_src
      and '_sweep_staged_files("删除已完成订单"' in gui_src)
check("打印前会用原件重建丢失的副本",
      "暂存副本缺失，已从原文件重建" in gui_src)
check("双击打开/打开文件位置优先原件",
      "def _job_user_path" in gui_src)
check("引用集合覆盖撤回备份与打印批次",
      "_staged_reference_paths" in gui_src
      and "_cleared_jobs_backup.values()" in gui_src)
check("引用集合覆盖未落地的云端任务",
      "_cloud_kept_paths" in gui_src and "_cloud_tasks" in gui_src)
check("启动时接管旧版 %TEMP% 云端文件",
      "_adopt_legacy_cloud_files" in gui_src)

check("云端下载落到暂存目录（%TEMP% 仅作降级兜底）",
      "staging.cloud_download_path" in cloud_src
      and "回退下载到系统临时目录" in cloud_src
      and cloud_src.count("hn_cloud_{task_id}") == 1)
check("下载期间标记「使用中」", "staging.reserve(dest)" in cloud_src
      and "staging.unreserve(dest)" in cloud_src)
check("旧 %TEMP% 清理已改为旧版遗留兜底",
      "旧版临时文件" in cloud_src or "旧版本遗留在 %TEMP%" in cloud_src)

print("=== 9. 收尾：退出时清空全部副本 ===")
final_removed = staging.sweep(set())
check("全部副本可被清空（退出清理路径）",
      final_removed >= 0 and not os.path.isfile(work))

shutil.rmtree(_SANDBOX_APPDATA, ignore_errors=True)
print("\n" + ("全部通过 ✅" if ok_all else "存在失败项 ❌"))
sys.exit(0 if ok_all else 1)
