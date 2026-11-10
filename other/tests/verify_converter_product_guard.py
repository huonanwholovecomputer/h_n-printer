#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""验收 · 转换产物校验（健壮性体检 🔴16）

背景（2026-11-10）：
  `gui.py` 的 docx 直转分支（engine=word/wps）以前写完就当成功 —— Word/WPS 的 COM
  导出会「静默成功」：不抛异常，但产物是 0 字节 / 半截 / 根本不是 PDF。以前只读页数，
  读不出就 warning + emit(0)、**不删文件** ⇒ 坏 PDF 永久占据这个缓存 key，之后每次打印
  都命中它，而且再也不会有第二次转换机会。

本脚本钉住四件事：
  ① 0 字节产物：被删掉，并且降级到 LibreOffice（最终交付的是能用的 PDF）
  ② 非 PDF 产物（写了别的东西）：同样被删 + 降级
  ③ 正常产物：不降级（零回归 —— 别为了防坏产物把好产物也重转一遍）
  ④ 读不出页数的分支也不再留坏文件（源码层）

反向验证（必红）：
  · 把 gui.py 里 `if not _pdf_product_ok(temp_pdf):` 整段删掉 → ① / ② 红
  · 把 `raise RuntimeError(...)` 改成 `pass`（只删不降级）→ ① / ② 红（没有可用产物）
"""
import os
import sys
import tempfile
import traceback

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LOCAL_TOOL = os.path.join(REPO, "local_print_tool")
_SANDBOX = tempfile.mkdtemp(prefix="hn_conv_guard_")
os.environ["APPDATA"] = _SANDBOX
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, LOCAL_TOOL)
os.chdir(LOCAL_TOOL)

from PySide6.QtWidgets import QApplication  # noqa: E402

_app = QApplication.instance() or QApplication([])

import gui          # noqa: E402
import converter    # noqa: E402
import paths        # noqa: E402

_fails = []


def check(name, cond, detail=""):
    if cond:
        print(f"  [PASS] {name}")
    else:
        _fails.append(name)
        print(f"  [FAIL] {name}  ← {detail}")


def make_pdf(path, pages=1):
    """造一份真 PDF（reportlab 与项目其它验收脚本同一套用法）。"""
    from reportlab.pdfgen import canvas
    c = canvas.Canvas(path, pagesize=(595, 842))
    for i in range(pages):
        c.drawString(72, 760, f"page {i + 1}")
        c.showPage()
    c.save()
    return path


def make_docx(path):
    """造一个最小 .docx（只是给 worker 一个真实存在的文件路径；COM 会被打桩）。"""
    import zipfile
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("[Content_Types].xml",
                   '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/'
                   'package/2006/content-types"><Default Extension="xml" '
                   'ContentType="application/xml"/></Types>')
    return path


class Rec:
    def __init__(self):
        self.finished = []
        self.com_calls = []
        self.lo_calls = []


class FakeConverter:
    """LibreOffice 兜底：写一份**真的** PDF。"""

    def __init__(self, rec, pages=1):
        self.rec = rec
        self.pages = pages

    def convert(self, file_path, from_cloud=False, allow_cloud_com=False):
        self.rec.lo_calls.append(file_path)
        out = os.path.join(_SANDBOX, f"lo_{len(self.rec.lo_calls)}.pdf")
        return make_pdf(out, self.pages)


def run_case(kind):
    """跑一次 docx 直转：kind = empty / not_pdf / good。"""
    rec = Rec()
    work = os.path.join(_SANDBOX, kind)
    os.makedirs(work, exist_ok=True)
    docx = make_docx(os.path.join(work, f"t_{kind}.docx"))
    src_md5 = "0123456789abcdef0123456789abcdef"

    _orig_word = converter._convert_via_word_com
    _orig_conv = converter.get_converter

    def fake_word(src, out):
        rec.com_calls.append(out)
        if kind == "empty":
            open(out, "wb").close()                      # 0 字节
        elif kind == "not_pdf":
            with open(out, "wb") as f:
                f.write(b"HELLO, NOT A PDF")             # 写了别的东西
        else:
            make_pdf(out, 1)                             # 正常产物

    converter._convert_via_word_com = fake_word
    converter.get_converter = lambda: FakeConverter(rec)
    try:
        w = gui.ConvertWorker(0, docx, "word", source_md5=src_md5)
        w.finished.connect(lambda *a: rec.finished.append(a))
        w._run_convert()
    finally:
        converter._convert_via_word_com = _orig_word
        converter.get_converter = _orig_conv
    return rec


print("── ① 0 字节产物：必须丢掉并降级 LibreOffice ──")
rec = run_case("empty")
_bad = rec.com_calls[0] if rec.com_calls else ""
check("①-a 确实走了 COM 直转分支", len(rec.com_calls) == 1, rec.com_calls)
check("①-b 坏产物（0 字节）已被删除，没留在 pdf_cache 里",
      bool(_bad) and not os.path.isfile(_bad), _bad)
check("①-c 降级到了 LibreOffice", len(rec.lo_calls) == 1, rec.lo_calls)
check("①-d 最终交付的是真 PDF、页数正常",
      bool(rec.finished) and rec.finished[-1][3] == 1
      and os.path.isfile(rec.finished[-1][2]), rec.finished)

print("── ② 非 PDF 产物（COM 写了别的东西）：同样丢掉并降级 ──")
rec = run_case("not_pdf")
_bad = rec.com_calls[0] if rec.com_calls else ""
check("②-a 坏产物已被删除", bool(_bad) and not os.path.isfile(_bad), _bad)
check("②-b 降级到了 LibreOffice", len(rec.lo_calls) == 1, rec.lo_calls)
check("②-c 最终交付的是真 PDF", bool(rec.finished) and rec.finished[-1][3] == 1,
      rec.finished)

print("── ③ 正常产物：不许被误判（零回归）──")
rec = run_case("good")
_good = rec.com_calls[0] if rec.com_calls else ""
check("③-a 好产物没有被删", bool(_good) and os.path.isfile(_good), _good)
check("③-b 没有多余的 LibreOffice 重转", len(rec.lo_calls) == 0, rec.lo_calls)
check("③-c 交付路径就是那份缓存文件",
      bool(rec.finished) and os.path.samefile(rec.finished[-1][2], _good),
      rec.finished)

print("── ④ 源码层：两处都要有产物校验 ──")
src = open(os.path.join(LOCAL_TOOL, "gui.py"), encoding="utf-8").read()
_run = src.split("def _run_convert(self):", 1)[1].split("\ndef ", 1)[0]
check("④-a docx 直转分支里有 `_pdf_product_ok(temp_pdf)` 守卫",
      "if not _pdf_product_ok(temp_pdf):" in _run)
check("④-b 判坏后必须 raise（否则不会降级到 LibreOffice）",
      "raise RuntimeError" in _run.split("if not _pdf_product_ok(temp_pdf):", 1)[-1][:800])
check("④-c 读不出页数的分支也会回收坏产物",
      "not _pdf_product_ok(temp_pdf)" in _run.split("get_pdf_info(temp_pdf)", 1)[-1][:600])
check("④-d 产物校验与 converter 同一份实现（不是本地重写一套）",
      "_pdf_product_ok" in _run and "from converter import" in _run)

print()
if _fails:
    print(f"存在失败 ❌ {len(_fails)} 项：" + "、".join(_fails))
    sys.exit(1)
print("全部通过 ✅ 转换产物校验（🔴16）")
