# -*- coding: utf-8 -*-
"""验收脚本 · 末尾空白页：检测 + ⚠️ 标识 + 一键排除（2026-10-28 用户需求）

现场：学校系统导出的申请表常在最后多带一张完全空白页（表格后那个空段落/分页符撑的），
打出来就是白纸。用户口径（2026-10 决策）：
  ① 只标 ⚠️ + 提供一键排除（**不**自动删）；② 只查末尾连续空白页；
  ③ 云端单只标不排（价格后端已定）；④ 排除掉的空白页不收钱（按排除后页数计价）。

本脚本钉住：
1. 检测口径：真空白页判空、中间空白页不误报、正文区有字的页不算空白、
   **只有页码 / 只有页眉 / 页码+页眉条 的页算空白**（2026-11-01 用户需求）、
   扫描件整页白图判空、阈值附近不越界；
2. 排除逻辑落到 page_range（唯一口径）：有效页数/计费/单页硬闸门随之变化，能恢复；
3. GUI：⚠️ 出现在「页数」列、勾选框只在检测到时可点、勾/取消直接改 page_range 与费用；
4. 变异自证：把 split_tail_blank 换成空实现 ⇒ GUI 那几条必须变红（证明测的是真路径）；
5. 真实现场文件（存在且能转换才算，否则 SKIP）：转换后末尾必须判出 1 页空白。

    python tests/verify_blank_tail_page.py     # exit 0 = 全绿
"""
import io
import os
import re
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
from pdf_printer import (detect_trailing_blank_pages, page_is_blank,  # noqa: E402
                         page_text_is_furniture)
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
    """spec: [(kind, ...)]，kind ∈ text / blank / footer / header / header_rule /
    body_bottom / body_mid / white_image / dot / box / title_straddle /
    title_in_band_with_frame"""
    doc = fitz.open()
    for item in spec:
        page = doc.new_page(width=595, height=842)
        kind = item[0]
        if kind == "text":
            page.insert_text((72, 120), item[1], fontsize=14)
        elif kind == "footer":
            # 只有页脚页码（2026-11-01 起：算空白，见 page_text_is_furniture）
            page.insert_text((280, 800), "12", fontsize=9)
        elif kind == "header":
            # 只有页眉标题（不印正文）
            page.insert_text((72, 40), "陕西省家庭经济困难学生认定暨国家教育资助申请表",
                             fontsize=9)
        elif kind == "header_rule":
            # 页码 + 页眉标题 + 页眉下那条装饰横线（现场第 3 页就是这个长相）
            page.draw_line(fitz.Point(72, 52), fitz.Point(523, 52), width=1)
            page.insert_text((72, 40), "陕西省家庭经济困难学生认定暨国家教育资助申请表",
                             fontsize=9)
            page.insert_text((280, 800), "12", fontsize=9)
        elif kind == "title_straddle":
            # 2026-11-01 现场误判（示波器的使用.pdf 第 6 页）的 1:1 复刻：
            # 整页一个边框 + 页脚「第 页」+ 一个 12pt 真标题坐 y0≈58.1/y1≈70.1，
            # 而 A4 家具带 = 0.08×842 = 67.36 pt ⇒ 标题横跨带边界。
            # 旧判据要求文字"完全落在正文区"才算内容 ⇒ 标题被当页眉 ⇒ 误判空白。
            page.draw_rect(fitz.Rect(55.45, 28.75, 539.9, 780.6), width=1)
            page.insert_text((79.8, 68), "2.思考题（课后完成）", fontsize=12)
            page.insert_text((280, 832), "第 6 页", fontsize=9)
        elif kind == "title_in_band_with_frame":
            # 标题**完全落在家具带内**（y1≈48 < 67.36）+ 页脚，但页面有边框/竖线压在
            # 正文区 ⇒ 只有「正文区墨量」那条判据能兜住（钉住 BLANK_FURNITURE_BODY_MAX_INK）
            page.draw_rect(fitz.Rect(55.45, 28.75, 539.9, 780.6), width=1)
            page.insert_text((79.8, 45), "2.思考题（课后完成）", fontsize=10)
            page.insert_text((280, 832), "第 6 页", fontsize=9)
        elif kind == "body_bottom":
            # 正文区最后一行（Word 默认下边距 72pt ⇒ 正文落在页脚带以外）
            page.insert_text((72, 760), "最后一行正文内容", fontsize=10)
        elif kind == "body_mid":
            page.insert_text((240, 420), "此页无正文", fontsize=10)
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
check("★ 只印了页码的末页算空白（2026-11-01 用户口径：只剩页码 = 空白页）⇒ 1",
      detect_trailing_blank_pages(p_footer) == 1, str(detect_trailing_blank_pages(p_footer)))

p_header = build_pdf("a_header.pdf", [("text", "page1"), ("header",)])
check("★ 只印了页眉标题的末页算空白（同上口径：只剩页眉 = 空白页）⇒ 1",
      detect_trailing_blank_pages(p_header) == 1, str(detect_trailing_blank_pages(p_header)))

p_header_rule = build_pdf("a_header_rule.pdf", [("text", "page1"), ("header_rule",)])
check("★ 页码 + 页眉标题 + 页眉装饰线（现场第 3 页长相）⇒ 1",
      detect_trailing_blank_pages(p_header_rule) == 1,
      str(detect_trailing_blank_pages(p_header_rule)))

p_body_bottom = build_pdf("a_body_bottom.pdf", [("text", "page1"), ("body_bottom",)])
check("★ 末页正文区最后一行有字（Word 默认下边距，正文在页脚带外）⇒ 0（不能误判）",
      detect_trailing_blank_pages(p_body_bottom) == 0,
      str(detect_trailing_blank_pages(p_body_bottom)))

p_body_mid = build_pdf("a_body_mid.pdf", [("text", "page1"), ("body_mid",)])
check("★ 末页正文区中间一行字（只有 5 个字、墨量极小）⇒ 0（正文区有字就是内容）",
      detect_trailing_blank_pages(p_body_mid) == 0,
      str(detect_trailing_blank_pages(p_body_mid)))

p_scan = build_pdf("a_scan.pdf", [("text", "page1"), ("white_image",)])
check("扫描件式「整页白图」末页判空白（栅格判据，不看有没有图）⇒ 1",
      detect_trailing_blank_pages(p_scan) == 1, str(detect_trailing_blank_pages(p_scan)))

p_dot = build_pdf("a_dot.pdf", [("text", "page1"), ("dot",)])
check("末页只有 4pt 微小墨点 ⇒ 按噪声算空白 ⇒ 1（容差 ≈1mm²，挡扫描脏点）",
      detect_trailing_blank_pages(p_dot) == 1, str(detect_trailing_blank_pages(p_dot)))

p_mark = build_pdf("a_mark.pdf", [("text", "page1"), ("box",)])
check("★ 末页有 40pt 方块（≈1600 暗像素）⇒ 有内容，不判空白 ⇒ 0",
      detect_trailing_blank_pages(p_mark) == 0, str(detect_trailing_blank_pages(p_mark)))

# 2026-11-01 现场误判（示波器的使用.pdf 第 6 页）：版本 4.6.0 把真标题判成页眉 ⇒ 末页当空白
p_straddle = build_pdf("a_title_straddle.pdf", [("text", "page1"), ("title_straddle",)])
check("★ 标题横跨家具带边界（现场 p6 复刻：y 58~70 / band 67.36）⇒ 0（有内容，绝不能误判空白）",
      detect_trailing_blank_pages(p_straddle) == 0,
      str(detect_trailing_blank_pages(p_straddle)))

p_inband = build_pdf("a_title_in_band.pdf", [("text", "page1"), ("title_in_band_with_frame",)])
check("★ 标题全在带内、但正文区有边框墨迹 ⇒ 0（正文区墨量判据兜住）",
      detect_trailing_blank_pages(p_inband) == 0,
      str(detect_trailing_blank_pages(p_inband)))

p_none = build_pdf("a_none.pdf", [("text", "page1"), ("text", "page2")])
check("全是内容 ⇒ 0", detect_trailing_blank_pages(p_none) == 0)
check("读不了的文件 ⇒ 0（fail-open，绝不影响打印）",
      detect_trailing_blank_pages(os.path.join(_SANDBOX, "not_exists.pdf")) == 0)

_doc = fitz.open(p_1blank)
check("page_is_blank: 内容页 False / 空白页 True",
      page_is_blank(_doc[0]) is False and page_is_blank(_doc[2]) is True)
_doc.close()

# 家具口径的直接判据（2026-11-01）：判据函数本身也要能被单测到
for _p, _label, _want_furn, _want_blank in [
    (p_footer, "只有页码", True, True),
    (p_header, "只有页眉", True, True),
    (p_header_rule, "页码 + 页眉 + 装饰线", True, True),
    (p_body_bottom, "正文区最后一行", False, False),
    (p_body_mid, "正文区中间一行", False, False),
    (p_mark, "40pt 方块（无字）", True, False),
    (p_straddle, "标题横跨家具带（现场误判）", False, False),
    # 文字全在带内 ⇒ 家具口径本身认为是"只剩家具"，但正文区有边框墨迹 ⇒ 仍不能算空白。
    # 这一条钉住 page_is_blank 里的「正文区墨量」判据（删掉它必然变红）。
    (p_inband, "标题全在带内 + 正文区边框", True, False),
]:
    _d = fitz.open(_p)
    _pg = _d[-1]
    _f, _b = page_text_is_furniture(_pg), page_is_blank(_pg)
    check(f"page_text_is_furniture / page_is_blank：{_label} ⇒ furniture={_want_furn} blank={_want_blank}",
          _f is _want_furn and _b is _want_blank, f"furniture={_f} blank={_b}")
    _d.close()

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

print("\n⑥ 云端单：只有顾客勾了「排除末尾空白页」才真排除（2026-11-02 新口径）")
# 旧口径是「云端单一律只标不排」（价格后端已定）；2026-11-02 起顾客可以在小程序/APP
# 按文件勾「排除末尾空白页」—— 勾了就必须真排除，并由设备回报 page_range_truncated
# 让后端重算价格（排掉的空白页不收钱）。没勾的云端单仍然只标不排。
_src = open(os.path.join(LOCAL_TOOL, "gui.py"), encoding="utf-8").read()
check("⑥a 建 job 处仍写 blank_tail（UI 照旧标 ⚠）",
      "blank_tail=blank_tail," in _src)
check("⑥b 云端自动排除被顾客的开关把住（blank_tail_exclude）",
      'if bool(getattr(task, "blank_tail_exclude", False))' in _src)
check("⑥c split_tail_blank 只出现在两处：操作员勾选 + 云端按开关排除",
      _src.count("split_tail_blank(") == 2,
      f"出现 {_src.count('split_tail_blank(')} 次")
# 逐处归属到所在函数：只允许这两个函数里出现
_defs = [(m.start(), m.group(1)) for m in re.finditer(r"^    def (\w+)", _src, re.M)]


def _owner(pos):
    name = "?"
    for start, n in _defs:
        if start < pos:
            name = n
        else:
            break
    return name


_callers = sorted({_owner(m.start()) for m in re.finditer(r"split_tail_blank\(", _src)})
check("⑥d 排除只发生在「操作员勾选」与「云端带开关」两条路径",
      set(_callers) <= {"_set_blank_tail_excluded", "_add_cloud_task_to_new_tab"},
      f"实际所在函数={_callers}")

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
