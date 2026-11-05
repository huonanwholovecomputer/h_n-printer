# -*- coding: utf-8 -*-
"""验收 · 批量添加多个文件时的 PDF 转换健壮性

事故形状（2026-11-03 现场）：晚上一次拖入 8 + 12 + 6 个文件，转换看着都完成了，
但**有一行的页数一直没补上**，日志里一个字都没有 —— 既不知道是转换失败还是显示
没刷新。查完日志才确认 28 个文件全部转换成功，所以问题出在"结果回写"这一侧。

本文件钉死四条（前三条都是改动前**确实会红**的形状）：

① 转换途中新建标签页 ⇒ 结果仍回写到发起转换的那个标签页。
   改动前：`_start_convert_worker` 不传 tab_key，回调时才去取"当前前台标签页"，
   于是晚到的结果按**新标签页**找行 ⇒ 找不到 ⇒ 静默丢弃 + 删掉刚转好的 PDF。
   实测同一场景：改动前 13/13 行停在 "?"、pdf_cache 剩 0 个 PDF；改动后 13/13 补齐。
② 结果找不到对应行 ⇒ 不删 pdf_cache 里的产物，并留一条日志（不再静默）。
③ 转换失败 ⇒ 该格 "?" + 日志留痕 + **自动重试一次**（且只重试一次，防损坏文件无限重转）。
④ 并发闸门 ⇒ 同时 8 个转换线程，任意时刻真正在跑的 ≤ HN_CONVERT_CONCURRENCY
   （改动前 28 个文件 = 28 个 WINWORD.EXE 一起跑）。
⑤ 取消 ≠ 失败 ⇒ 清空标签页/退出时批量取消，不许刷「转换失败」日志、不许排重试。

跑法：python other/tests/verify_convert_robustness.py（退出码 0 = 全绿）
"""
import os
import sys
import tempfile
import threading
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LOCAL_TOOL = os.path.join(REPO, "local_print_tool")

_SANDBOX = tempfile.mkdtemp(prefix="hn_convert_robust_")
os.environ["APPDATA"] = _SANDBOX                     # 配置/暂存/缓存全落沙箱
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["HN_CONVERT_CONCURRENCY"] = "2"           # 并发闸门调小，验证"确实限住了"
# run_all.py 会给所有子进程设 HN_NO_ENGINE_PROBE=1（防探测把用户正在编辑的 Word 关掉）。
# 那个变量会让「本机有没有 Word」直接判否 ⇒ 新加的 job.engine 变成 libreoffice ⇒
# ConvertWorker 走 get_converter().convert() 分支，**绕开本文件对
# converter._convert_via_word_com 的桩**，真的去叫 LibreOffice 转桩文件：单份卡 10 秒后
# 抛 `LibreOffice 转换失败 (rc=1)`，整份验收从 ~10 秒变成 95 秒并全红（实测）。
# 这里显式恢复探测：converter 的探测只看 exe 路径、不创建 COM 对象（见 _detect_word），
# 与本文件"绝不启动真的 Word"的约束不冲突；下面还会把 get_converter 一并桩掉兜底。
os.environ["HN_NO_ENGINE_PROBE"] = "0"
sys.path.insert(0, LOCAL_TOOL)
os.chdir(LOCAL_TOOL)

from PySide6.QtWidgets import QApplication     # noqa: E402
_app = QApplication.instance() or QApplication([])

import converter                                # noqa: E402
import gui                                      # noqa: E402
import paths                                    # noqa: E402
import pdf_printer                              # noqa: E402

ok_all = True


def check(name, cond, detail=""):
    global ok_all
    if not cond:
        ok_all = False
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


def pump(seconds=0.0):
    """转一会儿事件循环（等信号/定时器落地）。"""
    end = time.time() + seconds
    while True:
        _app.processEvents()
        if time.time() >= end:
            break
        time.sleep(0.02)


# ── 桩：不真的叫 Word。写一个占位 PDF（内容不重要，get_pdf_info 也被换成桩）──
_CONV_GATE = threading.Event()
_CONV_GATE.set()
_CONCUR_LOCK = threading.Lock()
_CONCUR_NOW = 0
_CONCUR_PEAK = 0
_FAKE_CALLS: list[str] = []      # 桩被真正调用的次数（防"验收空转"：一次没走桩也算绿）


def _fake_convert(src, out, **_kw):
    """模拟一次 Word 转换：可被 _CONV_GATE 卡住（用于稳定复现"转换途中切页"）。"""
    global _CONCUR_NOW, _CONCUR_PEAK
    with _CONCUR_LOCK:
        _CONCUR_NOW += 1
        _CONCUR_PEAK = max(_CONCUR_PEAK, _CONCUR_NOW)
    try:
        _CONV_GATE.wait(30)
        with open(out, "wb") as f:
            f.write(b"%PDF-1.4\n%stub\n")
        _FAKE_CALLS.append(src)
    finally:
        with _CONCUR_LOCK:
            _CONCUR_NOW -= 1


class _FakeConverter:
    """get_converter() 的替身：不管走到哪条分支，转换都是假的、都计入并发峰值。

    2026-11：run_all 里 HN_NO_ENGINE_PROBE=1 会让 docx 落到 `get_converter().convert`
    这条分支（原以为只有云端文件才会走），没有这个替身就会真叫 LibreOffice。
    """

    def convert(self, src, **kw):
        out = os.path.join(_SANDBOX, "fake_%d.pdf" % len(_FAKE_CALLS))
        _fake_convert(src, out, **kw)
        return out


converter._convert_via_word_com = _fake_convert                 # noqa: SLF001
converter._convert_via_wps_com = _fake_convert                  # noqa: SLF001
converter.get_converter = lambda *a, **k: _FakeConverter()       # noqa: SLF001
pdf_printer.get_pdf_info = lambda p: {"page_count": 2, "orientation": "portrait"}
gui.get_pdf_info = pdf_printer.get_pdf_info

SRC = os.path.join(_SANDBOX, "src")
os.makedirs(SRC, exist_ok=True)


def mk_docx(name):
    """占位「docx」：**内容必须各不相同**，否则 MD5 相同 ⇒ 13 个文件共用同一份缓存 PDF
    （第一版就踩了这个坑：断言"缓存里有 13 个 PDF"直接红，其实是内容一样）。"""
    p = os.path.join(SRC, name)
    with open(p, "wb") as f:
        f.write(b"stub docx " + name.encode("utf-8"))
    return p


win = gui.MainWindow(config_path=paths.config_path())
logs: list[str] = []
win._log = lambda m: logs.append(str(m))


def rows_of(tab_key):
    return win._config.tabs[tab_key].jobs


def wait_all_done(tab_key, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        pump(0.05)
        jobs = rows_of(tab_key)
        if jobs and all(int(j.page_count or 0) > 0 for j in jobs):
            return True
    return False


# ════════════════════ ① 转换途中新建标签页 ════════════════════
print("\n=== ① 13 个文件转换途中新建标签页：结果必须回到原标签页 ===")
_CONV_GATE.clear()                                   # 卡住转换，先切页
files = [mk_docx(f"a{i:02d}.docx") for i in range(13)]
tab1 = win._current_tab
win._add_files_to_table(files)
pump(0.3)
tab2 = win._new_tab()
print(f"    转换途中新建标签页：{tab1} -> {tab2}")
_CONV_GATE.set()                                     # 放行
done = wait_all_done(tab1, timeout=90)
pump(1.0)
jobs1 = rows_of(tab1)
_counts = [int(j.page_count or 0) for j in jobs1]
check("13 行页数全部补齐（改动前 13 个全是 0）", done and _counts == [2] * 13,
      f"counts={_counts}")
win._current_tab = tab1
win._rebuild_table()
pump(0.2)
_cells = [win._table.item(i, win.COL_PAGES).text() for i in range(win._table.rowCount())]
check("表格页数格没有残留 '?'", "?" not in _cells, f"cells={_cells[:6]}…")
_pdfs = [f for f in os.listdir(paths.pdf_cache_dir()) if f.endswith(".pdf")]
check("pdf_cache 里的转换产物没被删（改动前删光 = 0 个）", len(_pdfs) >= 13,
      f"pdf 数={len(_pdfs)}")

# ════════════════════ ② 找不到对应行 ════════════════════
print("\n=== ② 结果找不到对应行：不删缓存产物 + 留日志 ===")
_orphan = os.path.join(paths.pdf_cache_dir(), "abcdef12_eword.pdf")
with open(_orphan, "wb") as f:
    f.write(b"%PDF-1.4\n%orphan\n")
logs.clear()
win._on_convert_finished(0, os.path.join(SRC, "已经被删掉的文件.docx"), _orphan,
                         2, "portrait", tab_key=tab1)
check("pdf_cache 里的产物保留（改动前会被 os.remove 掉）", os.path.isfile(_orphan),
      _orphan)
check("留了一条日志（改动前完全静默）", any("无对应行" in m for m in logs), f"logs={logs}")

# ════════════════════ ③ 转换失败 → 自动重试一次 ════════════════════
print("\n=== ③ 转换失败：'?' + 日志 + 自动重试一次 ===")
fail_file = mk_docx("b_fail.docx")
win._add_files_to_table([fail_file])
pump(0.3)
_fail_row = None
_fail_job_path = ""
for i, j in enumerate(rows_of(tab1)):
    # 表里的 file_path 是**暂存副本**路径，不是原路径（原件删了也能打，靠的就是这份副本）
    if os.path.basename(j.file_path) == os.path.basename(fail_file):
        _fail_row = i
        _fail_job_path = j.file_path
        break
check("失败样例已进表格", _fail_row is not None and bool(_fail_job_path),
      f"row={_fail_row} path={_fail_job_path}")

# 桩掉重试真正启动转换的动作，只记录
retries: list[tuple] = []
_orig_start = win._start_convert_worker
win._start_convert_worker = lambda row, fp, eng, *a, **k: retries.append((row, fp))

if _fail_row is not None:
    rows_of(tab1)[_fail_row].page_count = 0
    logs.clear()
    win._on_convert_finished(_fail_row, _fail_job_path, "", 0, "", tab_key=tab1)
    check("失败那一格显示 '?'",
          win._table.item(_fail_row, win.COL_PAGES).text() == "?",
          repr(win._table.item(_fail_row, win.COL_PAGES).text()))
    check("失败留了日志（改动前没有）", any("转换失败" in m for m in logs), f"logs={logs}")
    _key = os.path.normcase(os.path.abspath(_fail_job_path))
    check("重试计数 +1", win._convert_retries.get(_key) == 1,
          f"retries={win._convert_retries}")
    pump(2.0)                                  # 等 1500ms 的一次性定时器落地
    check("重试真的发起了一次", len(retries) == 1, f"retries={retries}")
    check("同一文件不再排第二次重试",
          win._schedule_convert_retry(_fail_job_path, tab1) is False)

# ════════════════════ ③b 打印中：重试让路，不许被静默丢掉 ════════════════════
print("\n=== ③b 打印占用引擎时，重试要让路而不是被丢掉 ===")


def _row_by_basename(tab_key, path):
    for i, j in enumerate(rows_of(tab_key)):
        if os.path.basename(j.file_path or "") == os.path.basename(path):
            return i, j.file_path
    return None, ""


_defer_file = mk_docx("b_defer.docx")
win._start_convert_worker = _orig_start        # 先恢复正常预转换，别把"添加"也记成重试
win._add_files_to_table([_defer_file])
pump(0.5)
_defer_row, _defer_path = _row_by_basename(tab1, _defer_file)
_defer_retries: list[tuple] = []               # 只记录这一小节的重试
win._start_convert_worker = lambda row, fp, eng, *a, **k: _defer_retries.append((row, fp))


class _BusyWorker:
    """假的「打印作业在跑」：isRunning() 恒为 True。"""
    def isRunning(self):
        return True


if _defer_row is not None:
    _orig_worker = win._worker
    win._worker = _BusyWorker()
    try:
        rows_of(tab1)[_defer_row].page_count = 0
        win._on_convert_finished(_defer_row, _defer_path, "", 0, "", tab_key=tab1)
        pump(1.0)                       # 1.5s 定时器还没到第二次尝试
        check("打印中不抢引擎：重试先让路", not _defer_retries, f"retries={_defer_retries}")
        win._worker = _orig_worker
        pump(4.5)                       # 让路后下一轮应当真的发起
        check("引擎空闲后重试补上（改动前在打印中会被直接丢掉）",
              len(_defer_retries) == 1, f"retries={_defer_retries}")
    finally:
        win._worker = _orig_worker
win._start_convert_worker = _orig_start

# ════════════════════ ④ 并发闸门 ════════════════════
print("\n=== ④ 并发闸门：同时 8 个转换线程，在跑的 ≤ 2 ===")


def _peak_reset():
    global _CONCUR_PEAK, _CONCUR_NOW
    with _CONCUR_LOCK:
        _CONCUR_PEAK = 0
        _CONCUR_NOW = 0


_peak_reset()
done_sig = []
workers = []
for i in range(8):
    p = mk_docx(f"c{i:02d}.docx")
    w = gui.ConvertWorker(i, p, "word", "")
    w.finished.connect(lambda *a: done_sig.append(a))
    workers.append(w)
    w.start()
end = time.time() + 60
while time.time() < end and len(done_sig) < 8:
    pump(0.05)
check("8 个转换全部拿到结果", len(done_sig) == 8, f"done={len(done_sig)}")
check("并发峰值 <= 2（环境变量 HN_CONVERT_CONCURRENCY=2）", _CONCUR_PEAK <= 2,
      f"peak={_CONCUR_PEAK}（改动前会到 8）")
# 防"验收空转"：桩一次没被调用说明转换走了别的分支（真 Word / 真 LibreOffice），
# 那前面那些"页数补齐"只是假象。2026-11 在 run_all（HN_NO_ENGINE_PROBE=1）下就栽过。
check("转换桩确实被调用过（不是真去叫 Word/LibreOffice）", len(_FAKE_CALLS) >= 13,
      f"calls={len(_FAKE_CALLS)}")

print("\n=== ⑤ 取消的转换：不刷「转换失败」日志、不排重试 ===")
# 清空标签页 / 退出时会一次性取消几十个 worker，它们都用 CONVERT_CANCELLED(-1) 报告。
# 如果把取消当失败，日志会被自己刷满，还会给已经不存在的行排一堆重试。
_retries_before = len(retries)
logs.clear()
_cancel_p = mk_docx("e_cancel.docx")
win._add_files_to_table([_cancel_p])
pump(0.3)
_cancel_row = None
_cancel_path = ""
for i, j in enumerate(rows_of(tab1)):
    if os.path.basename(j.file_path) == os.path.basename(_cancel_p):
        _cancel_row, _cancel_path = i, j.file_path
        break
if _cancel_row is not None:
    win._on_convert_finished(_cancel_row, _cancel_path, "", gui.CONVERT_CANCELLED,
                             "", tab_key=tab1)
    check("取消不写日志", not any("转换失败" in m for m in logs), f"logs={logs}")
    check("取消不排重试", len(retries) == _retries_before,
          f"retries {_retries_before} -> {len(retries)}")
    check("取消不动页数格（仍是转换中的样子）",
          win._table.item(_cancel_row, win.COL_PAGES).text() in ("2", "?"),
          repr(win._table.item(_cancel_row, win.COL_PAGES).text()))

print("\n" + ("✅ 全部通过" if ok_all else "❌ 有判据失败"))
sys.exit(0 if ok_all else 1)
