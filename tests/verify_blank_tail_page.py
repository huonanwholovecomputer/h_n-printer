# -*- coding: utf-8 -*-
"""验收脚本 · 末尾空白页：检测 + ⚠️ 标识 + 一键排除（2026-10-28 用户需求）

现场：学校系统导出的申请表常在最后多带一张完全空白页（表格后那个空段落/分页符撑的），
打出来就是白纸。用户口径（2026-10 决策）：
  ① 只标 ⚠️ + 提供一键排除（**不**自动删）；② 只查末尾连续空白页；
  ③ 云端单只标不排（价格后端已定）；④ 排除掉的空白页不收钱（按排除后页数计价）。

本脚本钉住：
1. 检测口径：真空白页判空、中间空白页不误报、只有页脚页码的页**不算**空白、
   扫描件整页白图判空、阈值附近不越界；
2. 排除逻辑落到 page_range（唯一口径）：有效页数/计费/单页硬闸门随之变化，能恢复；
3. GUI：⚠️ 出现在「页数」列、勾选框只在检测到时可点、勾/取消直接改 page_range 与费用；
4. 变异自证：把 split_tail_blank 换成空实现 ⇒ GUI 那几条必须变红（证明测的是真路径）；
5. 真实现场文件（存在且能转换才算，否则 SKIP）：转换后末尾必须判出 1 页空白。

    python tests/verify_blank_tail_page.py     # exit 0 = 全绿
"""
import io
import os
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL_TOOL = os.path.join(REPO, "local_print_tool")
_SANDBOX = tempfile.mkdtemp(prefix="hn_blanktail_")
os.environ["APPDATA"] = _SANDBOX
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, LOCAL_TOOL)
os.chdir(LOCAL_TOOL)

import fitz  # noqa: E402
from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import paths  # noqa: E402
from pdf_printer import detect_trailing_blank_pages, page_is_blank  # noqa: E402
from printer_config import (PrintJob, TabSettings, _count_pages_in_range,  # noqa: E402
                            calc_cost, format_page_selection, split_tail_blank)

app = QApplication.instance() or QApplication([])

ok_all = True


def check(name, cond, detail=""):
    global ok_all
    if not cond:
        ok_all = False
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


# ══════════════ 夹具：用 PyMuPDF 造「指定第几页空白」的 PDF ══════════════
def build_pdf(name, spec):
    """spec: [(kind, ...)]，kind ∈ text / blank / footer / white_image / dot"""
    doc = fitz.open()
    for item in spec:
        page = doc.new_page(width=595, height=842)
        kind = item[0]
        if kind == "text":
            page.insert_text((72, 120), item[1], fontsize=14)
        elif kind == "footer":
            page.insert_text((280, 800), "1", fontsize=9)   # 只有页码 = 有内容
        elif kind == "white_image":
            pix = fitz.Pixmap(fitz.csGRAY, fitz.IRect(0, 0, 400, 560), False)
            pix.set_rect(pix.irect, (255,))
            page.insert_image(fitz.Rect(72, 72, 520, 770), pixmap=pix)
        elif kind == "dot":
            page.draw_rect(fitz.Rect(100, 100, 104, 104), color=(0, 0, 0), fill=(0, 0, 0))
        elif kind == "box":
            page.draw_rect(fitz.Rect(100, 100, 140, 140), color=(0, 0, 0), fill=(0, 0, 0))
        # blank: 什么都不画
    path = os.path.join(_SANDBOX, name)
    doc.save(path)
    doc.close()
    return path


print("\n① 检测口径（纯夹具，离线可跑）")
p_1blank = build_pdf("a_1blank.pdf", [("text", "page1"), ("text", "page2"), ("blank",)])
check("3 页、末页空白 ⇒ 1", detect_trailing_blank_pages(p_1blank) == 1,
      str(detect_trailing_blank_pages(p_1blank)))

p_2blank = build_pdf("a_2blank.pdf", [("text", "page1"), ("blank",), ("blank",)])
check("末尾连续 2 页空白 ⇒ 2", detect_trailing_blank_pages(p_2blank) == 2,
      str(detect_trailing_blank_pages(p_2blank)))

p_mid = build_pdf("a_mid.pdf", [("text", "page1"), ("blank",), ("text", "page3")])
check("★ 中间的空白页不报（只查末尾连续）⇒ 0", detect_trailing_blank_pages(p_mid) == 0,
      str(detect_trailing_blank_pages(p_mid)))

p_footer = build_pdf("a_footer.pdf", [("text", "page1"), ("footer",)])
check("★ 只印了页码的末页**不算**空白（有字就会出纸）⇒ 0",
      detect_trailing_blank_pages(p_footer) == 0, str(detect_trailing_blank_pages(p_footer)))

p_scan = build_pdf("a_scan.pdf", [("text", "page1"), ("white_image",)])
check("扫描件式「整页白图」末页判空白（栅格判据，不看有没有图）⇒ 1",
      detect_trailing_blank_pages(p_scan) == 1, str(detect_trailing_blank_pages(p_scan)))

p_dot = build_pdf("a_dot.pdf", [("text", "page1"), ("dot",)])
check("末页只有 4pt 微小墨点 ⇒ 按噪声算空白 ⇒ 1（容差 ≈1mm²，挡扫描脏点）",
      detect_trailing_blank_pages(p_dot) == 1, str(detect_trailing_blank_pages(p_dot)))

p_mark = build_pdf("a_mark.pdf", [("text", "page1"), ("box",)])
check("★ 末页有 40pt 方块（≈1600 暗像素）⇒ 有内容，不判空白 ⇒ 0",
      detect_trailing_blank_pages(p_mark) == 0, str(detect_trailing_blank_pages(p_mark)))

p_none = build_pdf("a_none.pdf", [("text", "page1"), ("text", "page2")])
check("全是内容 ⇒ 0", detect_trailing_blank_pages(p_none) == 0)
check("读不了的文件 ⇒ 0（fail-open，绝不影响打印）",
      detect_trailing_blank_pages(os.path.join(_SANDBOX, "not_exists.pdf")) == 0)

_doc = fitz.open(p_1blank)
check("page_is_blank: 内容页 False / 空白页 True",
      page_is_blank(_doc[0]) is False and page_is_blank(_doc[2]) is True)
_doc.close()

print("\n② 排除逻辑：结果只落到 page_range 上（唯一口径）")
check("format_page_selection: {1,2,3,5} → \"1-3,5\"",
      format_page_selection({1, 2, 3, 5}) == "1-3,5", format_page_selection({1, 2, 3, 5}))
check("format_page_selection: {1} → \"1\"", format_page_selection({1}) == "1")

cases = [
    # (范围, 总页, 空白尾, 期望范围, 期望排掉几页)
    ("", 3, 1, "1-2", 1),
    ("", 2, 1, "1", 1),
    ("1-2", 3, 1, "1-2", 0),        # 末尾空白页本来就不在范围里
    ("1,3", 3, 1, "1", 1),          # 只排范围里、且在文件末尾的那页
    ("", 3, 0, "", 0),              # 没检测到空白
    ("", 1, 0, "", 0),              # 单页文件
    ("", 2, 2, "1", 1),             # 全空也至少留 1 页（0 页没法打印）
]
for rng, total, tail, want_rng, want_drop in cases:
    got_rng, got_drop = split_tail_blank(rng, total, tail)
    check(f"split_tail_blank({rng!r}, {total}, {tail}) ⇒ ({want_rng!r}, {want_drop})",
          got_rng == want_rng and got_drop == want_drop, f"实际 ({got_rng!r}, {got_drop})")

print("\n③ 排除后的下游都跟着变（计费 / 有效页数 / 单页闸门）")
_job = PrintJob(file_path=p_1blank, page_count=3, duplex="on", page_range="")
_before = calc_cost(3, 1, "on", 0.2, 0.3, "")[0]
_j2 = PrintJob(file_path=p_1blank, page_count=3, duplex="on")
_j2.page_range, _drop = split_tail_blank("", 3, 1)
_after = calc_cost(3, 1, "on", 0.2, 0.3, _j2.page_range)[0]
check("3 页双面 ¥0.50 → 排除末页后 2 页双面 ¥0.30（不收那一页的钱）",
      abs(_before - 0.5) < 1e-9 and abs(_after - 0.3) < 1e-9 and _drop == 1,
      f"{_before} → {_after}")

_j3 = PrintJob(file_path=p_1blank, page_count=2, duplex="on", page_range="")
_j3.page_range, _ = split_tail_blank("", 2, 1)
check("★ 2 页（1 页内容 + 1 页空白）排除后有效页数 == 1 ⇒ 单页硬闸门按单面打",
      _count_pages_in_range(_j3.page_range, _j3.page_count) == 1, _j3.page_range)
check("单页硬闸门/手动双面判定读的就是 page_range（不是 page_count）",
      "_count_pages_in_range(getattr(job, \"page_range\", \"\") or \"\"," in
      open(os.path.join(LOCAL_TOOL, "gui.py"), encoding="utf-8").read())

print("\n④ PrintJob 持久化往返")
_rt = PrintJob.from_dict(PrintJob(file_path="x.pdf", blank_tail=2,
                                  blank_undo_range="1-3").to_dict())
check("blank_tail / blank_undo_range 存得下也读得回",
      _rt.blank_tail == 2 and _rt.blank_undo_range == "1-3",
      f"tail={_rt.blank_tail} undo={_rt.blank_undo_range!r}")
_rt2 = PrintJob.from_dict({"file_path": "y.pdf", "blank_undo_range": 5})
check("旧配置 / 脏值（undo 不是字符串）⇒ None，不炸",
      _rt2.blank_tail == 0 and _rt2.blank_undo_range is None)

print("\n⑤ GUI（offscreen MainWindow）")
import converter as _converter  # noqa: E402
_converter.get_available_engines = lambda: {"word": True, "wps": True, "libreoffice": True}
import gui as _gui  # noqa: E402
_gui.QMessageBox.question = staticmethod(lambda *a, **k: _gui.QMessageBox.Yes)
_gui.QMessageBox.warning = staticmethod(lambda *a, **k: _gui.QMessageBox.Ok)
_gui.QMessageBox.information = staticmethod(lambda *a, **k: _gui.QMessageBox.Ok)

win = _gui.MainWindow(config_path=paths.config_path())
win.resize(1400, 900)
win.show()
QTest.qWaitForWindowExposed(win)
app.processEvents()


def seed(job):
    tab = TabSettings()
    tab.jobs = [job]
    win._config.tabs = {"1": tab}
    win._current_tab = "1"
    win._config.active_tab = "1"
    win._rebuild_table()
    win._refresh_tab_display()
    win._table.selectRow(0)
    app.processEvents()


job = PrintJob(file_path=p_1blank, page_count=3, duplex="on", display_name="a_1blank.pdf",
               blank_tail=1)
seed(job)
check("「页数」列带 ⚠（未排除）", win._pages_cell_text(job) == "3 ⚠",
      win._pages_cell_text(job))
check("悬停说明写明末尾有几页空白", "末尾有 1 页空白" in win._pages_cell_tip(job),
      win._pages_cell_tip(job))
check("勾选框可用且文案带页数", win._edit_blank_tail.isEnabled()
      and "1 页" in win._edit_blank_tail.text(), win._edit_blank_tail.text())
check("默认未勾选（不自动排除）", win._edit_blank_tail.isChecked() is False)

_cost_cell_before = win._table.item(0, win.COL_COST).text()
QTest.mouseClick(win._edit_blank_tail, Qt.LeftButton)
app.processEvents()
check("★ 勾上后 page_range 变成 1-2（排除落到唯一口径上）", job.page_range == "1-2",
      repr(job.page_range))
check("勾上后 undo 范围记下原值（可恢复）", job.blank_undo_range == "",
      repr(job.blank_undo_range))
check("页数列变为「2 (共 3 页) ⚠已排」", win._pages_cell_text(job) == "2 (共 3 页) ⚠已排",
      win._pages_cell_text(job))
_cost_cell_after = win._table.item(0, win.COL_COST).text()
check("费用列同步降下来（¥0.50 → ¥0.30）",
      _cost_cell_before != _cost_cell_after and "0.30" in _cost_cell_after,
      f"{_cost_cell_before} → {_cost_cell_after}")
check("面板的页码范围控件也跟着变成 1-2（否则下次改别的参数会写回旧值）",
      [w.text() for w in win._edit_page_range._inputs if w.text().strip()] == ["1-2"],
      str([w.text() for w in win._edit_page_range._inputs]))

QTest.mouseClick(win._edit_blank_tail, Qt.LeftButton)
app.processEvents()
check("★ 取消勾选 → page_range 还原为空（恢复) ", job.page_range == "", repr(job.page_range))
check("恢复后 undo 清空", job.blank_undo_range is None)
check("恢复后页数列回到 3 ⚠", win._pages_cell_text(job) == "3 ⚠",
      win._pages_cell_text(job))

print("\n⑤b 缓存命中路径（2026-10-28 现场 bug：⚠ 只对「真正转换」那一次生效）")
# 现场：三个文件的日志都是「📦 缓存命中 → N 页」，页数列却一个 ⚠ 都没有。
# 原因 = _start_convert_worker 的缓存命中分支只回写 md5/pdf/页数/方向，漏了空白页检测
# （检测原本只写在 _on_convert_finished，也就是「真正转换这一次」）。
import hashlib as _hashlib  # noqa: E402
import shutil as _shutil  # noqa: E402
from cloud_client import cache_engine_for as _cache_eng, pdf_cache_key as _cache_key  # noqa: E402

_src_docx = os.path.join(_SANDBOX, "cached_source.docx")
with open(_src_docx, "wb") as _f:
    _f.write(b"PK\x03\x04 fake docx body: cache-hit path fixture")
_src_md5 = _hashlib.md5(open(_src_docx, "rb").read()).hexdigest()
_cache_dir = paths.pdf_cache_dir()
os.makedirs(_cache_dir, exist_ok=True)
_cached_pdf = os.path.join(
    _cache_dir, _cache_key(_src_md5, "auto", _cache_eng(_src_docx, "word")) + ".pdf")
_shutil.copy2(p_1blank, _cached_pdf)      # 3 页、末页空白，冒充「上一次转换留下的缓存」


def _cache_hit_run():
    seed(PrintJob(file_path=_src_docx, page_count=0, duplex="on",
                  display_name="cached_source.docx"))
    _j = win._get_current_jobs()[0]
    _workers_before = len(win._convert_workers)
    win._start_convert_worker(0, _src_docx, "word")
    app.processEvents()
    return _j, len(win._convert_workers) - _workers_before


_cj, _new_workers = _cache_hit_run()
check("缓存确实命中（拿到缓存 PDF，且没新起转换线程）",
      _cj.cached_pdf == _cached_pdf and _new_workers == 0,
      f"cached={_cj.cached_pdf!r} 新线程={_new_workers}")
check("★ 缓存命中的文件 blank_tail 也要有值（3 页末页空白 ⇒ 1）", _cj.blank_tail == 1,
      f"blank_tail={_cj.blank_tail}")
check("★ 缓存命中的行「页数」列带 ⚠", win._pages_cell_text(_cj) == "3 ⚠",
      win._pages_cell_text(_cj))

# 变异自证：把检测换掉 ⇒ 上面两条「★」必须变红（证明读的是真调用，不是别处顺手写的值）
_orig_detect = _gui.MainWindow._detect_blank_tail
_gui.MainWindow._detect_blank_tail = staticmethod(lambda p: 0)
_cj_mut, _ = _cache_hit_run()
_gui.MainWindow._detect_blank_tail = _orig_detect
check("变异自证：检测被换空后缓存命中的 blank_tail 必须是 0（断言确实走该调用）",
      _cj_mut.blank_tail == 0 and win._pages_cell_text(_cj_mut) == "3",
      f"blank_tail={_cj_mut.blank_tail} cell={win._pages_cell_text(_cj_mut)!r}")

print("\n⑥ 云端单：只标不排（自动路径不许调用排除）")
_src = open(os.path.join(LOCAL_TOOL, "gui.py"), encoding="utf-8").read()
_cloud_block = _src.split("blank_tail=blank_tail,        # 末尾空白页")[0]
check("云端建 job 的地方只写 blank_tail，没有 split_tail_blank",
      "blank_tail=blank_tail,        # 末尾空白页" in _src
      and "split_tail_blank" not in _cloud_block.split("def _build")[-1])
check("自动排除在 add/convert/cloud 三条路径上都不存在（只有操作员动作才排）",
      _src.count("split_tail_blank(") == 1,
      f"出现 {_src.count('split_tail_blank(')} 次（应只在 _set_blank_tail_excluded 里）")

print("\n⑦ 变异自证：把 split_tail_blank 换成空实现，⑤ 的断言必须变红")
_orig = _gui.split_tail_blank
_gui.split_tail_blank = lambda rng, total, tail: (rng, 0)
job2 = PrintJob(file_path=p_1blank, page_count=3, duplex="on", blank_tail=1)
seed(job2)
QTest.mouseClick(win._edit_blank_tail, Qt.LeftButton)
app.processEvents()
_mutant_noop = (job2.page_range == "" and win._pages_cell_text(job2) == "3 ⚠")
_gui.split_tail_blank = _orig
check("变异体（空实现）下勾选不改变任何东西 ⇒ 说明 ⑤ 的量测是真的", _mutant_noop,
      f"page_range={job2.page_range!r} cell={win._pages_cell_text(job2)!r}")

print("\n⑧ 真实现场文件（存在且能转换才算）")
REAL = [
    r"C:\Users\Administrator\Desktop\打印列表\2026年10月28日\张钰果\附件2：本专科生国家励志奖学金申请表(最终1)(1).doc",
    r"C:\Users\Administrator\Desktop\打印列表\2026年10月28日\王睿豪\附件2.本专科生国家励志奖学金申请表1_修改版.doc",
]
_ran = False
for _p in REAL:
    if not os.path.isfile(_p):
        print(f"[SKIP] 不在本机: {os.path.basename(_p)}")
        continue
    try:
        from converter import get_converter
        _out = os.path.join(_SANDBOX, "real_%d.pdf" % len(os.listdir(_SANDBOX)))
        if not get_converter().convert(_p, _out) or not os.path.isfile(_out):
            print(f"[SKIP] 转换失败: {os.path.basename(_p)}")
            continue
    except Exception as exc:
        print(f"[SKIP] 转换异常 {type(exc).__name__}: {os.path.basename(_p)}")
        continue
    _ran = True
    _tail = detect_trailing_blank_pages(_out)
    _doc = fitz.open(_out)
    _n = len(_doc)
    _doc.close()
    check(f"{os.path.basename(_p)[:22]}… 末尾判出 1 页空白（{_n} 页）", _tail == 1,
          f"tail={_tail}")
if not _ran:
    print("[SKIP] 本机没有可转换的现场文件（需要 Office/WPS）")

print("\n" + ("PASS 全部通过" if ok_all else "FAIL 有失败项"))
sys.exit(0 if ok_all else 1)
