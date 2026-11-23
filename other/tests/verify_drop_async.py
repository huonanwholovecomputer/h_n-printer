# -*- coding: utf-8 -*-
"""验收脚本 · 添加文件（拖放/对话框/粘贴）不阻塞主线程

2026-11-17：_add_files_to_table 由「同步复制 + 就地解析」改为「StageFilesWorker 后台线程 + 排队」。
本脚本守住三件事：

  A 静态：入口链上不得再出现 staging.stage_file / get_pdf_info / detect_trailing_blank_pages /
          get_image_info / _file_md5，这些必须只出现在 StageFilesWorker.run 里；
          且 worker 的 done/finished 必须连**绑定方法**（连 lambda 会在发射线程直接执行，
          等于把活儿又搬回主线程）。
  B 运行时：真 MainWindow（offscreen），把 staging.stage_file 打桩成 sleep(1.0)，
          断言入口 <0.25s 返回、_wait_staging_idle 之后任务才出现，且
          file_path 是暂存副本 / source_path 是原件 / 非 PDF 的 MD5 已算好；
          后台忙时再调入口进队列（不叠加线程）。
  C 对照：直接同步调用同一个打桩复制 ≥0.9s —— 证明 B 的 0.25s 阈值是真的能把
          「退回同步实现」判红，而不是阈值太松。

    python other/tests/verify_drop_async.py     # exit 0 = 全绿
"""
import hashlib
import io
import os
import shutil
import sys
import tempfile
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LOCAL_TOOL = os.path.join(REPO, "local_print_tool")
GUI_PY = os.path.join(LOCAL_TOOL, "gui.py")

_SANDBOX = tempfile.mkdtemp(prefix="hn_drop_async_")
os.environ["APPDATA"] = _SANDBOX
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, LOCAL_TOOL)
os.chdir(LOCAL_TOOL)

ok_all = True


def check(name, cond, detail=""):
    global ok_all
    if not cond:
        ok_all = False
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


def _segment(src, start_anchor, end_anchor):
    i = src.find(start_anchor)
    if i < 0:
        return ""
    j = src.find(end_anchor, i + len(start_anchor))
    return src[i:] if j < 0 else src[i:j]


# ════════════════════ A. 静态：活儿不在主线程路径上 ════════════════════
print("=== A. 静态检查：入口链与 worker 的职责边界 ===")
with open(GUI_PY, "r", encoding="utf-8") as f:
    _src = f.read()

_entry = _segment(_src, "def _add_files_to_table(", "def _drain_staging_queue(")
check("找得到新的入口实现 _add_files_to_table", bool(_entry), f"len={len(_entry)}")

_SYNC_TOKENS = ["staging.stage_file", "get_pdf_info", "detect_trailing_blank_pages",
                "get_image_info", "_file_md5"]
_hit = [t for t in _SYNC_TOKENS if t in _entry]
check("入口链不再同步复制/解析（这些必须留在 worker 里）", not _hit, f"残留={_hit}")

_worker = _segment(_src, "class StageFilesWorker(QThread):", "class DropTableWidget")
check("StageFilesWorker 存在", bool(_worker), f"len={len(_worker)}")
check("worker 里真的做了暂存复制", "staging.stage_file" in _worker)
check("worker 里算 MD5（不在主线程读整文件）", "_file_md5" in _worker)
check("worker 用信号把结果交回主线程（不在 worker 里碰 Qt 控件）", "self.done.emit" in _worker)

_batch = _segment(_src, "def _start_staging_batch(", "def _on_stage_batch_done(")
# 注意：注释里就写着 "lambda" 一词，判据只看 connect 调用本身
check("done 连的是绑定方法（连 lambda 会在发射线程直接跑）",
      "worker.done.connect(self." in _batch and "connect(lambda" not in _batch)
check("finished 连的是绑定方法",
      "worker.finished.connect(self." in _batch and "connect(lambda" not in _batch)
check("入口走排队（worker 忙时不叠加线程）", "self._staging_queue.append" in _entry)
check("★ 压缩包拖出的临时文件在入口就搬走（staging.snatch，不能留给后台线程）",
      "staging.snatch" in _entry)

# 打桩前先拿住原函数
import staging  # noqa: E402

_orig_stage_file = staging.stage_file
copied = []


def _slow_stage_file(src):
    """模拟「复制一个 1 秒才完成的大文件」——真实现里就是 shutil.copy2 的耗时。"""
    time.sleep(1.0)
    out = _orig_stage_file(src)
    copied.append(src)
    return out


# ════════════════════ 造文件 ════════════════════
from reportlab.pdfgen import canvas  # noqa: E402
from reportlab.lib.pagesizes import A4  # noqa: E402

os.makedirs(os.path.join(_SANDBOX, "inbox"), exist_ok=True)


def make_pdf(name, pages=1):
    p = os.path.join(_SANDBOX, "inbox", name)
    c = canvas.Canvas(p, pagesize=A4)
    for i in range(pages):
        c.drawString(72, 720, f"drop async {i}")
        c.showPage()
    c.save()
    return p


SRC = make_pdf("拖进来的报告.pdf", pages=2)
TXT = os.path.join(_SANDBOX, "inbox", "备注.txt")
with open(TXT, "w", encoding="utf-8") as f:
    f.write("hello drop async\n")

from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

app = QApplication.instance() or QApplication([])

import gui as _gui  # noqa: E402

_gui.QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.Yes)
_gui.QMessageBox.warning = staticmethod(lambda *a, **k: QMessageBox.Ok)
_gui.QMessageBox.information = staticmethod(lambda *a, **k: QMessageBox.Ok)

cfg_path = os.path.join(_SANDBOX, "cfg", "drop_async.json")
os.makedirs(os.path.dirname(cfg_path), exist_ok=True)
win = _gui.MainWindow(config_path=cfg_path)
win._cloud_client = None
win._queue_font_checks = lambda *a, **k: None
win._start_convert_worker = lambda *a, **k: None   # 别真去起预转换（要开 Word/字体服务）
win.show()

staging.stage_file = _slow_stage_file   # 从这里开始「复制」要 1 秒

# ════════════════════ B. 运行时：入口立即返回，活儿在后台 ════════════════════
print("\n=== B. 运行时：加文件不再卡住主线程 ===")
_t0 = time.perf_counter()
win._add_files_to_table([SRC])
_elapsed = time.perf_counter() - _t0
check("入口立即返回（<0.25s；同步复制要 1s）", _elapsed < 0.25, f"{_elapsed * 1000:.0f} ms")
check("返回那一刻还没复制完（活儿确实在后台）", not copied, f"copied={copied}")
check("返回那一刻列表里还没有这一行", len(win._get_current_jobs()) == 0,
      f"n={len(win._get_current_jobs())}")

# 后台正忙时再塞一批：必须进队列，不能叠加第二个 worker
_t0 = time.perf_counter()
win._add_files_to_table([TXT])
_elapsed_busy = time.perf_counter() - _t0
check("忙时再调入口同样立即返回", _elapsed_busy < 0.25, f"{_elapsed_busy * 1000:.0f} ms")
check("忙时那批进了队列（没有叠加线程）", len(win._staging_queue) == 1,
      f"queue={len(win._staging_queue)}")
check("忙时只有一个 worker 在跑", win._staging_worker is not None and len(copied) == 0)

_idle_ok = win._wait_staging_idle(30.0)
check("_wait_staging_idle 等到全部落地", _idle_ok)
check("后台确实对两批都做了暂存复制", len(copied) == 2, f"copied={copied}")

_jobs = win._get_current_jobs()
check("等待后两行都在列表里", len(_jobs) == 2, f"n={len(_jobs)}")
_by_base = {os.path.basename(j.source_path or j.file_path): j for j in _jobs}

_pdf_job = _by_base.get(os.path.basename(SRC))
check("PDF 行已加入", _pdf_job is not None, str(list(_by_base)))
if _pdf_job is not None:
    check("列表任务用暂存副本（原件删了也能打）", staging.is_staged(_pdf_job.file_path),
          _pdf_job.file_path)
    check("原型路径（双击/打开位置）指向原件",
          os.path.normcase(_pdf_job.source_path) == os.path.normcase(os.path.abspath(SRC)),
          _pdf_job.source_path)
    check("页数在后台解析好了（2 页）", _pdf_job.page_count == 2, str(_pdf_job.page_count))

_txt_job = _by_base.get(os.path.basename(TXT))
check("排队的那批（txt）也落地了", _txt_job is not None, str(list(_by_base)))
if _txt_job is not None:
    check("txt 行的副本同样在暂存目录", staging.is_staged(_txt_job.file_path), _txt_job.file_path)
    _want_md5 = hashlib.md5(open(TXT, "rb").read()).hexdigest()
    check("MD5 已在后台算好（预转换查缓存 / 云端去重都用它）",
          _txt_job.source_md5 == _want_md5, f"{_txt_job.source_md5} vs {_want_md5}")

check("两批都落地后队列与 worker 都已清空",
      not win._staging_queue and win._staging_worker is None)

print("\n=== B2. 压缩包拖出：解压软件在拖放返回后立刻清场（2026-11-23 现场）===")
# 解压软件的拖出临时目录：IDropTarget::Drop 一返回就被删。这里用一个 7-Zip 命名的
# 临时目录复现：入口调用返回后立刻整目录删掉，任务仍必须可用。
_VOL_DIR = os.path.join(tempfile.gettempdir(), "7zE00A1B2C3")
os.makedirs(_VOL_DIR, exist_ok=True)
VOL = os.path.join(_VOL_DIR, "压缩包里的报告.pdf")
_c = canvas.Canvas(VOL, pagesize=A4)
_c.drawString(72, 720, "snatched from archive")
_c.showPage()
_c.save()

_t0 = time.perf_counter()
win._add_files_to_table([VOL])
_vol_elapsed = time.perf_counter() - _t0
check("压缩包拖出的文件：入口依旧立即返回（rename 是瞬时的，不因文件大而卡）",
      _vol_elapsed < 0.25, f"{_vol_elapsed * 1000:.0f} ms")
check("★ 入口返回那一刻源文件已搬进暂存目录（不是留给后台线程去复制）",
      not os.path.isfile(VOL))
shutil.rmtree(_VOL_DIR, ignore_errors=True)      # 模拟解压软件清场
_vol_idle = win._wait_staging_idle(30.0)
check("压缩包拖出的文件：后台批次正常落地", _vol_idle)
_vol_job = next((j for j in (win._get_current_jobs() or [])
                 if os.path.basename(j.file_path) == "压缩包里的报告.pdf"), None)
check("★ 临时目录被清掉后，任务仍指向可用的暂存副本",
      _vol_job is not None and staging.is_staged(_vol_job.file_path)
      and os.path.isfile(_vol_job.file_path),
      str(_vol_job.file_path if _vol_job else None))
check("★ 副本的「使用中」预占已交还（_apply_staged_files 记账没漏）",
      staging._reserved_snapshot() == set(), str(staging._reserved_snapshot()))

# ════════════════════ C. 对照：阈值能把「退回同步」判红 ════════════════════
print("\n=== C. 对照实验：同步调用的耗时 ===")
SRC3 = make_pdf("对照组.pdf", pages=1)
_t0 = time.perf_counter()
staging.stage_file(SRC3)
_sync_elapsed = time.perf_counter() - _t0
check("同一个打桩复制同步跑 ≥0.9s ⇒ 入口若退回同步必然超 0.25s 阈值",
      _sync_elapsed >= 0.9, f"{_sync_elapsed * 1000:.0f} ms")

staging.stage_file = _orig_stage_file
try:
    win.close()
except Exception:
    pass
for _ in range(50):
    app.processEvents()

print("\n" + ("全部通过" if ok_all else "存在失败项"))
sys.exit(0 if ok_all else 1)
