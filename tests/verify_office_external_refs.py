# -*- coding: utf-8 -*-
"""验收 · Office 转换前的**外部引用剥离**（S2-03 批 B，2026-10-25）

依据 docs/S2-03沙箱三引擎验证_2026-10-25.md §6.2/§7.2-3：Office 打开文档时会去抓
`TargetMode="External"` 的链接图片（诱捕服务实测 Word 抓 2 次、WPS 1 次、LibreOffice 1 次，
**开沙箱照样抓**），而 `AutomationSecurity`/`UpdateLinksAtOpen` 只挡 `INCLUDEPICTURE` 类域，
挡不住 blip 的 `r:link`。本批在转换前对 docx 副本做净化。

三段判据：
  A. **诱捕自检（必须有命中）**：不经净化直接让 Word 打开该文档 ⇒ 诱捕服务命中 > 0。
     这一条先证明"诱捕服务是活的"，否则后面 0 命中可能是假阴性（报告里踩过这个坑）。
  B. **净化后 0 命中**：走 `converter._convert_office_to_pdf`（带净化装饰器）⇒ 命中 0，
     产物是合法 PDF，原始文件字节不变，临时副本已被清理。
  C. **净化器本身的单元行为**：`_strip_docx_external_refs` 返回新路径、计数 ≥ 2
     （外部关系 + INCLUDEPICTURE 域各一次）、新包里没有 `TargetMode="External"`、
     没有 `INCLUDEPICTURE`，且 python-docx 仍能打开（没有把 zip 写坏）。

⚠ Word 不可用的机器上 A/B 段打印 SKIP（不判失败）；C 段任何机器都跑。

用法: PYTHONPATH= PYTHONUTF8=1 <python311> tests/verify_office_external_refs.py
exit 0 = 全绿
"""
import hashlib
import io
import os
import re
import shutil
import socket
import sys
import tempfile
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL = os.path.join(REPO, "local_print_tool")
sys.path.insert(0, TOOL)

ok_all = True
skipped = []


def check(name, cond, detail=""):
    global ok_all
    if not cond:
        ok_all = False
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))
    return bool(cond)


def skip(name, why):
    skipped.append(name)
    print(f"[SKIP] {name}  -- {why}")


# ── 诱捕 HTTP 服务（记录每一次命中）────────────────────────────────
class _Trap(BaseHTTPRequestHandler):
    hits = []

    def do_GET(self):  # noqa: N802
        _Trap.hits.append(self.path)
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", "68")
        self.end_headers()
        self.wfile.write(_MIN_PNG)

    def log_message(self, *a):  # 静音
        pass


_MIN_PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06"
            b"\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00"
            b"\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82")


def start_trap():
    srv = HTTPServer(("127.0.0.1", 0), _Trap)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


def build_trapped_docx(path: str, url: str) -> None:
    """用 python-docx 做一份带**内嵌**图片的文档，再把那条关系改成外部链接 + 加一个
    INCLUDEPICTURE 域 —— 两种抓取路径各一个。"""
    from docx import Document
    from docx.shared import Inches
    from PIL import Image

    tmp_png = os.path.join(os.path.dirname(path), "_trap_src.png")
    Image.new("RGB", (40, 40), (200, 30, 30)).save(tmp_png)
    doc = Document()
    doc.add_heading("外部引用剥离验收", level=1)
    doc.add_paragraph("下面这张图本应是内嵌的，验收里被改成外部 URL。")
    doc.add_picture(tmp_png, width=Inches(1))
    doc.save(path)
    os.remove(tmp_png)

    with zipfile.ZipFile(path) as z:
        rels_name = "word/_rels/document.xml.rels"
        doc_name = "word/document.xml"
        rels = z.read(rels_name)
        body = z.read(doc_name)

    m = re.search(rb'<Relationship\b[^>]*Type="[^"]*/image"[^>]*/>', rels)
    if not m:
        raise RuntimeError("没找到图片关系，无法构造外部引用样本")
    rid = re.search(rb'Id="([^"]+)"', m.group(0)).group(1)
    new_rel = (b'<Relationship Id="' + rid + b'" '
               b'Type="http://schemas.openxmlformats.org/officeDocument/2006/'
               b'relationships/image" Target="' + url.encode() + b'" TargetMode="External"/>')
    rels = rels.replace(m.group(0), new_rel)
    # 内嵌引用改成外部链接引用
    body = body.replace(b'r:embed="' + rid + b'"', b'r:link="' + rid + b'"')
    # 再塞一个 INCLUDEPICTURE 域（另一条抓取路径）
    field = ('<w:p><w:r><w:instrText xml:space="preserve"> INCLUDEPICTURE "' + url +
             '" \\d </w:instrText></w:r></w:p>').encode()
    body = body.replace(b"</w:body>", field + b"</w:body>")

    tmp = path + ".new"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout, zipfile.ZipFile(path) as zin:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename == rels_name:
                data = rels
            elif item.filename == doc_name:
                data = body
            zi = zipfile.ZipInfo(item.filename, date_time=item.date_time)
            zi.compress_type = item.compress_type
            zout.writestr(zi, data)
    os.replace(tmp, path)


def main() -> int:
    import logging
    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    import converter

    tmp = tempfile.mkdtemp(prefix="s2_03_refs_")
    try:
        srv, port = start_trap()
        url = f"http://127.0.0.1:{port}/trap.png"
        src = os.path.join(tmp, "trapped.docx")
        build_trapped_docx(src, url)
        src_sha = hashlib.sha256(open(src, "rb").read()).hexdigest()

        print("\n=== C. 净化器单元行为（任何机器都跑） ===")
        sanitize = getattr(converter, "_strip_docx_external_refs", None)
        if not callable(sanitize):
            check("★ 实现了 docx 外部引用剥离器（_strip_docx_external_refs）", False,
                  "修复前：Office 路径零实现")
            print("\n" + "=" * 56)
            print("Office 外部引用剥离验收: 存在失败 ❌")
            return 1
        check("★ 实现了 docx 外部引用剥离器（_strip_docx_external_refs）", True)
        clean_path, n = converter._strip_docx_external_refs(src)
        check("★ 返回了净化后的新文件（不是原路径）", clean_path != src,
              f"count={n}")
        check("★ 剥离计数 ≥ 2（外部关系 + INCLUDEPICTURE 域各一次）", n >= 2, f"n={n}")
        with zipfile.ZipFile(clean_path) as z:
            rels = z.read("word/_rels/document.xml.rels")
            body = z.read("word/document.xml")
        check("★ 净化后包里没有 TargetMode=\"External\"", b'TargetMode="External"' not in rels)
        check("★ 净化后包里没有 INCLUDEPICTURE",
              b"INCLUDEPICTURE" not in body.upper())
        check("原来的 r:link 属性也被去掉了",
              b'r:link="' not in body)
        try:
            from docx import Document
            d = Document(clean_path)
            n_par = len(d.paragraphs)
            check("净化后的 docx 仍能被 python-docx 打开（没把 zip 写坏）", n_par > 0,
                  f"{n_par} 段")
        except Exception as e:  # noqa: BLE001
            check("净化后的 docx 仍能被 python-docx 打开（没把 zip 写坏）", False, str(e))
        check("净化不修改原文件（sha256 不变）",
              hashlib.sha256(open(src, "rb").read()).hexdigest() == src_sha)
        os.remove(clean_path)

        # 没有外部引用时不该产生临时文件
        plain = os.path.join(tmp, "plain.docx")
        from docx import Document as _D
        _D().save(plain)
        p2, n2 = converter._strip_docx_external_refs(plain)
        check("普通 docx（无外部引用）原样返回、不产生临时文件", p2 == plain and n2 == 0)

        print("\n=== A. 诱捕自检：不净化直连 Word，必须抓到（证明诱捕是活的） ===")
        if not converter._detect_word():
            skip("A/B 段（Word 真机抓取）", "本机没有 Word")
        else:
            _Trap.hits.clear()
            out_raw = os.path.join(tmp, "raw.pdf")
            try:
                converter._convert_via_word_com(src, out_raw)
                hit_a = len(_Trap.hits)
                check("★ 未净化时 Word 访问了外部 URL（诱捕有命中，否则后面 0 命中不可信）",
                      hit_a > 0, f"命中 {hit_a} 次: {_Trap.hits[:4]}")
            except Exception as e:  # noqa: BLE001
                skip("A 段", f"Word 直连转换失败：{e}")

            print("\n=== B. 净化后：走 _convert_office_to_pdf，必须 0 命中 ===")
            _Trap.hits.clear()
            out_pdf = os.path.join(tmp, "clean.pdf")
            log_buf = io.StringIO()
            h = None
            try:
                import logging
                h = logging.StreamHandler(log_buf)
                converter.logger.addHandler(h)
                converter._convert_office_to_pdf(src, out_pdf)
            except Exception as e:  # noqa: BLE001
                check("净化后仍能转换成功", False, str(e))
            finally:
                if h is not None:
                    converter.logger.removeHandler(h)
            hit_b = len(_Trap.hits)
            check("★ 净化后诱捕命中为 0（Word 不再访问外网）", hit_b == 0,
                  f"命中 {hit_b} 次: {_Trap.hits[:4]}")
            check("净化后产物是合法 PDF",
                  os.path.isfile(out_pdf) and open(out_pdf, "rb").read(4) == b"%PDF",
                  f"{os.path.getsize(out_pdf) if os.path.isfile(out_pdf) else 0} B")
            check("日志里写明了剥离条数（可追溯）",
                  "已剥离" in log_buf.getvalue() and "外部引用" in log_buf.getvalue())
            left = [f for f in os.listdir(tempfile.gettempdir()) if f.startswith("_hn_noref_")]
            check("临时净化副本已被清理（不堆在 %TEMP%）", not left, f"残留 {left[:3]}")
            check("走完整转换路径也不修改原文件",
                  hashlib.sha256(open(src, "rb").read()).hexdigest() == src_sha)

        srv.shutdown()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "=" * 56)
    if skipped:
        print("跳过：", "; ".join(skipped))
    print("Office 外部引用剥离验收:", "全部通过 ✅" if ok_all else "存在失败 ❌")
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
