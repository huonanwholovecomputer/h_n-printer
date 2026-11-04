# -*- coding: utf-8 -*-
"""验收脚本 · 双面短边（short-edge）链路复核（2026-11 用户需求 4）

用户问：「移动端下单的需短边翻转的文件，在已知 PDF 和未转换 PDF 时双面配置是否正确？
比如短边翻转的双面 Word 文档，未转 PDF 就提交订单，传到本地打印工具之后，
双面的配置会进一步确认或纠正吗？」

链路事实（本脚本逐条钉住，改坏了就红）：

  ① 移动端/后端**没有**短边选项：`CloudTask` 只有 `duplex: "on"|"off"`，
     `__slots__` 里没有 duplex_mode ⇒ 短边/长边只可能由**本地按页面方向推导**。
  ② 推导只有一个入口：`MainWindow._duplex_mode_for_orientation()`
     （landscape → short-edge，其余 → long-edge），云端落单与本地加文件都调它。
  ③ 已知 PDF：落地时 `get_pdf_info()` 已有真实方向 ⇒ 一落地就推导正确。
  ④ 未转 Word（`.doc` 方向完全未知 / `.docx` 只有 XML 粗判）：先按 guess 落地
     （`.doc` → long-edge），**转换完成后必须按真实方向复核纠正**
     （`_on_convert_finished` 的「翻转轴复核」块）。
  ⑤ 操作员在编辑面板**显式**选过的长/短边不许被复核覆盖。
  ⑥ 1 页硬闸门优先：单页双面请求 → duplex=off（`_effective_duplex` / 单页归一化）。
  ⑦ 多文件同单方向不一致：逐文件各自推导，打印侧逐面生效（`rotate_second_for`
     长边补偿第 2 趟 180°、短边不补偿）。

    python tests/verify_duplex_short_edge.py     # exit 0 = 全绿
"""
import io
import os
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LOCAL_TOOL = os.path.join(REPO, "local_print_tool")

_SANDBOX = tempfile.mkdtemp(prefix="hn_short_edge_")
os.environ["APPDATA"] = _SANDBOX
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, LOCAL_TOOL)
os.chdir(LOCAL_TOOL)

from PySide6.QtWidgets import QApplication  # noqa: E402

_app = QApplication.instance() or QApplication([])

import cloud_client  # noqa: E402
import gui  # noqa: E402
import manual_duplex  # noqa: E402
import paths  # noqa: E402
from printer_config import PrintJob, PrinterProfile  # noqa: E402

ok_all = True


def check(name, cond, detail=""):
    global ok_all
    if not cond:
        ok_all = False
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


MANUAL = PrinterProfile(auto_duplex=False, capability_source="manual", manual_duplex=True,
                        first="odd", reverse_second=True, second_rotate_180=True,
                        stack_capacity=90, stack_warn_after_sec=1800)

print("=" * 60)
print("① 协议事实：短边/长边不是移动端传下来的，只能由本地按方向推导")
t = cloud_client.CloudTask({"task_id": 1, "file_name": "a.docx", "options": {"duplex": "on"}})
check("CloudTask 只有 duplex=on/off（没有短边/长边）", t.duplex == "on")
check("★ CloudTask.__slots__ 里没有 duplex_mode（移动端没这个字段）",
      "duplex_mode" not in cloud_client.CloudTask.__slots__,
      list(cloud_client.CloudTask.__slots__))
slots = cloud_client.CloudTask.__slots__
check("★ 也没有任何 short/long-edge 字段",
      not any("edge" in s for s in slots), [s for s in slots if "edge" in s])

print()
print("② 唯一推导入口：_duplex_mode_for_orientation")
m = gui.MainWindow._duplex_mode_for_orientation
check("landscape → short-edge（短边翻）", m("landscape") == "short-edge")
check("portrait → long-edge（长边翻）", m("portrait") == "long-edge")
check("方向未知 \"\" → long-edge（保守，与历史一致）", m("") == "long-edge")
check("mixed → long-edge（不猜）", m("mixed") == "long-edge")
check("None / 大小写 / 空格都归一", m(None) == "long-edge" and m(" LANDSCAPE ") == "short-edge")

gui_src = open(os.path.join(LOCAL_TOOL, "gui.py"), encoding="utf-8").read()
check("★ 云端落单 + 本地加文件都走同一个入口（调用点恰好 2 处）",
      gui_src.count("duplex_mode = self._duplex_mode_for_orientation(orientation)") == 2,
      gui_src.count("duplex_mode = self._duplex_mode_for_orientation(orientation)"))
check("★ 旧的三元表达式已绝迹（不许再各处手写一套）",
      'duplex_mode = "short-edge" if orientation == "landscape" else "long-edge"' not in gui_src)
check("★ 转换完成回调里有「翻转轴复核」块", "翻转轴复核" in gui_src
      and "_auto_before = self._duplex_mode_for_orientation(_old_orientation)" in gui_src)

print()
print("③ 已知 PDF / 已转好的文件：落地时方向已知 ⇒ 一落地就正确")
win = gui.MainWindow(config_path=paths.config_path())
win._config.printer_name = "Fake Print"
win._config.printer_profiles["Fake Print"] = MANUAL
win._config.keep_temp_pdf = False
logs = []
win._log = lambda msg, *a, **k: logs.append(str(msg))
win._detect_blank_tail = lambda pdf: 0        # 不读真 PDF（本脚本只验双面字段）


def _pdf(name, pages=2):
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.pdfgen import canvas
    path = os.path.join(_SANDBOX, name)
    c = canvas.Canvas(path, pagesize=A4)
    for i in range(pages):
        c.drawString(72, 760, f"{name} p{i + 1}")
        c.showPage()
    c.save()
    path_ls = os.path.join(_SANDBOX, "ls_" + name)
    c = canvas.Canvas(path_ls, pagesize=landscape(A4))
    for i in range(pages):
        c.drawString(72, 560, f"{name} p{i + 1}")
        c.showPage()
    c.save()
    return path, path_ls


PDF_PORTRAIT, PDF_LANDSCAPE = _pdf("word_doc.pdf", 4)


def _add_job(path, **kw):
    job = PrintJob(file_path=path, copies=1, **kw)
    win._tab_jobs("").append(job)
    return job


def _row(job):
    return win._tab_jobs("").index(job)


# 已知 PDF：方向已由 get_pdf_info 得出 → 直接写进 job
job = _add_job(PDF_LANDSCAPE, duplex="on",
               duplex_mode=gui.MainWindow._duplex_mode_for_orientation("landscape"),
               page_count=4, orientation="landscape")
check("已知 PDF（横向）：落地就是 short-edge", job.duplex_mode == "short-edge", job.duplex_mode)

print()
print("④ 未转 Word 落地（.doc 方向未知、page_count=0）→ 转换完成后必须纠正")
# 场景 A：竖版 Word 未转 PDF 就下单，落地时方向未知 → 先猜 long-edge
jobA = _add_job(os.path.join(_SANDBOX, "vertical.doc"), duplex="on",
                duplex_mode=gui.MainWindow._duplex_mode_for_orientation(""),
                page_count=0, orientation="")
check("未转 .doc：落地时按未知方向先猜 long-edge", jobA.duplex_mode == "long-edge", jobA.duplex_mode)

logs.clear()
win._on_convert_finished(_row(jobA), jobA.file_path, PDF_PORTRAIT, 4, "portrait")
check("A 竖版：复核后保持 long-edge", jobA.duplex_mode == "long-edge", jobA.duplex_mode)
check("A 竖版：方向与页数已回写", jobA.orientation == "portrait" and jobA.page_count == 4)

# 场景 B：横版 Word 未转 PDF 就下单（这就是用户问的那单）→ 落地猜 long-edge，转换后必须改 short-edge
jobB = _add_job(os.path.join(_SANDBOX, "horizontal.doc"), duplex="on",
                duplex_mode=gui.MainWindow._duplex_mode_for_orientation(""),
                page_count=0, orientation="")
check("未转 .doc：横版落地时也只能先猜 long-edge", jobB.duplex_mode == "long-edge", jobB.duplex_mode)
logs.clear()
win._on_convert_finished(_row(jobB), jobB.file_path, PDF_LANDSCAPE, 4, "landscape")
check("★ B 横版：转换完成后纠正为 short-edge", jobB.duplex_mode == "short-edge", jobB.duplex_mode)
check("★ B 横版：日志说清「实际是横版 → 翻转轴改为短边」",
      any("实际是横版" in s and "短边" in s for s in logs), logs)
check("B 横版：duplex 仍是双面（没被误关）", jobB.duplex == "on", jobB.duplex)

# 场景 C：.docx 的 XML 粗判猜错（猜横、实际竖）→ 也要改回 long-edge
jobC = _add_job(os.path.join(_SANDBOX, "guess_landscape.docx"), duplex="on",
                duplex_mode=gui.MainWindow._duplex_mode_for_orientation("landscape"),
                page_count=0, orientation="landscape")
logs.clear()
win._on_convert_finished(_row(jobC), jobC.file_path, PDF_PORTRAIT, 3, "portrait")
check("★ C 粗判猜横、实际竖：改回 long-edge", jobC.duplex_mode == "long-edge", jobC.duplex_mode)
check("C：日志说清「实际是竖版 → 长边」",
      any("实际是竖版" in s and "长边" in s for s in logs), logs)

# 场景 D：操作员显式选过的短边不许被复核覆盖
jobD = _add_job(os.path.join(_SANDBOX, "explicit.doc"), duplex="on",
                duplex_mode="short-edge",      # 显式 = 与自动推导值（long-edge）不同
                page_count=0, orientation="")
logs.clear()
win._on_convert_finished(_row(jobD), jobD.file_path, PDF_PORTRAIT, 6, "portrait")
check("★ D 操作员显式选短边：竖版转换完成也不许改回长边",
      jobD.duplex_mode == "short-edge", jobD.duplex_mode)
check("D：没有任何「翻转轴改为」日志", not any("翻转轴改为" in s for s in logs), logs)

# 场景 E：1 页硬闸门优先（1 页双面 → 单面），且不因复核把 duplex 又打开
jobE = _add_job(os.path.join(_SANDBOX, "one_page.doc"), duplex="on",
                duplex_mode=gui.MainWindow._duplex_mode_for_orientation(""),
                page_count=0, orientation="")
logs.clear()
win._on_convert_finished(_row(jobE), jobE.file_path, PDF_LANDSCAPE, 1, "landscape")
check("★ E 单页：duplex 强制改 off（单页硬闸门）", jobE.duplex == "off", jobE.duplex)
check("E 单页：日志说明「实际只打 1 页 → 自动改为单面」",
      any("自动改为单面" in s for s in logs), logs)

print()
print("⑤ 打印侧闸门：页数未知(0) 不当作 1 页，1 页一定单面")
w = gui.PrintWorker([], "Fake Print", "long-edge", False)   # 只借方法，不启动线程
check("_effective_duplex：page_count=0（未分析）请求双面 → 仍是 on",
      w._effective_duplex(PrintJob(file_path="x.doc", duplex="on", page_count=0), 0) == "on")
check("_effective_duplex：1 页请求双面 → 强制 off",
      w._effective_duplex(PrintJob(file_path="x.doc", duplex="on", page_count=1), 1) == "off")
check("_effective_duplex：单面请求 → off",
      w._effective_duplex(PrintJob(file_path="x.doc", duplex="off", page_count=4), 4) == "off")

print()
print("⑥ 多文件同单、方向不一致：逐文件各自推导，打印侧逐面生效")
# 前面几次 _on_convert_finished 都调了 _save_config()，它会把 printer_name 从真实下拉框
# 读回去（变回「系统默认打印机」）→ 这里先复位，才验得到手动双面的准入闸门。
win._config.printer_name = "Fake Print"
win._config.printer_profiles["Fake Print"] = MANUAL
jobP = _add_job(PDF_PORTRAIT, duplex="on",
                duplex_mode=gui.MainWindow._duplex_mode_for_orientation("portrait"),
                page_count=4, orientation="portrait")
jobL = _add_job(PDF_LANDSCAPE, duplex="on",
                duplex_mode=gui.MainWindow._duplex_mode_for_orientation("landscape"),
                page_count=4, orientation="landscape")
check("同单两文件：竖版 long-edge / 横版 short-edge 并存",
      jobP.duplex_mode == "long-edge" and jobL.duplex_mode == "short-edge",
      (jobP.duplex_mode, jobL.duplex_mode))
check("两单都要走手动双面（方向不同不影响准入）",
      win._uses_manual_duplex_job(jobP) and win._uses_manual_duplex_job(jobL))
check("第 2 趟方向逐面生效：长边补偿 180°、短边不补偿",
      manual_duplex.rotate_second_for(True, jobP.duplex_mode) is True
      and manual_duplex.rotate_second_for(True, jobL.duplex_mode) is False,
      (manual_duplex.rotate_second_for(True, jobP.duplex_mode),
       manual_duplex.rotate_second_for(True, jobL.duplex_mode)))
check("_rotate_for_job 与 manual_duplex 同口径",
      win._rotate_for_job(jobP) is True and win._rotate_for_job(jobL) is False,
      (win._rotate_for_job(jobP), win._rotate_for_job(jobL)))

print()
print("=" * 60)
print("双面短边链路验收：" + ("全部通过 ✅" if ok_all else "存在失败 ❌"))
sys.exit(0 if ok_all else 1)
