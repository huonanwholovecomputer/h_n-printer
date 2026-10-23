# -*- coding: utf-8 -*-
"""验收 · S0-05 上传面危险类型拦截 + Markdown 危险语法扫描

依据：docs/安全加固修复计划_2026-10-23.md §S0-05、D-2（已拍板：永久拒绝）、DS-01/DS-16。

现场形状：顾客上传的 `.html` 会在店铺机的 wkhtmltopdf 管线里执行脚本、读
`file:///…/print_config.json`，并把全站 printer token 外发 —— 一条链把"能下单"
变成"能读店铺机任意文件"。上传面原先只有扩展名正则（`^[a-zA-Z0-9]{1,8}$`）。

本脚本：
  A. 静态接线：denylist 常量化（7 类）、拒绝发生在**落盘之前**、Markdown 扫描 8 类危险
     语法、命中后删除临时文件、文案给出 PDF/DOCX 替代路径。
  B. 行为：7 类危险扩展名 → 400 且 uploads 根目录**不留临时文件**；含 `<script>` /
     `file:///` / UNC / `onerror=` 的 .md → 400；普通 .md/.docx/.pdf/.jpg → 200。
  C. 变异自证：删掉 denylist 判断 → `.html` 又被接收（证明 400 来自 denylist）。

用法：python tests/verify_upload_dangerous_types.py     （exit 0 = 全部通过）
"""
import io
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _sec_harness as H  # noqa: E402

H.install_cleanup_hook()
src = H.read_app_source()
code = H.strip_comments(src)


def block(marker):
    i = code.find(marker)
    if i < 0:
        return ""
    lines = code[i:].splitlines(keepends=True)
    out = [lines[0]]
    for ln in lines[1:]:
        if ln.strip() and not ln[:1].isspace():
            break
        out.append(ln)
    return "".join(out)


upload = block("def upload_file(")
consts = block("DANGEROUS_UPLOAD_EXTS =")

# ============================================================
# A. 静态接线
# ============================================================
print("=== A. 静态接线 ===")
for ext in ("html", "htm", "shtml", "xhtml", "svg", "js", "mjs"):
    H.chk("denylist 含 .%s（D-2 永久拒绝）" % ext, '"%s"' % ext in consts, consts)
H.chk("denylist 拒绝位于**落盘之前**（不写临时文件）",
      0 < upload.find("if ext_lower in DANGEROUS_UPLOAD_EXTS:")
      < upload.find("f.save(temp_path)"))
H.chk("Markdown 危险语法扫描函数存在且被上传路由调用",
      "def _scan_unsafe_markdown(" in code and "_scan_unsafe_markdown(_md_text)" in upload)
for tok, label in (("<script", "script"), ("<iframe", "iframe"), ("<object", "object"),
                   ("<embed", "embed"), ("on-event", "on 事件属性"),
                   ("data:text/html", "data:text/html"), ("file-url", "file: URL"),
                   ("unc", "UNC 路径")):
    H.chk("Markdown 扫描覆盖 %s" % label, tok in code)
H.chk("Markdown 命中后删除临时文件（不留半成品给后续流程）",
      "os.remove(temp_path)" in upload.split("if _hit:")[1][:400])
H.chk("拒绝文案给出「另存为 PDF/DOCX」替代路径",
      "另存为 PDF/DOCX" in upload)
H.chk("Markdown 只读前 1 MB（不让扫描本身成为内存放大面）",
      "read(1024 * 1024)" in upload)

# ============================================================
# B. 行为
# ============================================================
print("\n=== B. 行为（临时后端副本 + test_client）===")
TMP, DST = H.make_copy("hn_upload_")
backend = H.import_backend(DST)
backend.init_db()
backend.PRINTER_TOKEN = "upload-tok"
backend.ADMIN_OPENIDS = {"up-admin"}
atok = backend.token_serializer.dumps("up-admin")
AUTH = {"Authorization": "Bearer " + atok}
UP = os.path.join(DST, "uploads")
http = backend.app.test_client()


def _root_files():
    try:
        return [n for n in os.listdir(UP) if os.path.isfile(os.path.join(UP, n))]
    except OSError:
        return []


def upload(name, data, ctype=None):
    return http.post("/api/upload",
                     data={"file": (io.BytesIO(data), name)},
                     headers=AUTH, content_type="multipart/form-data")


before = set(_root_files())
for ext in ("html", "htm", "shtml", "xhtml", "svg", "js", "mjs"):
    r = upload("evil." + ext, b"<html><script>alert(1)</script></html>")
    H.chk(".%s 上传 → 400（永久拒绝）" % ext, r.status_code == 400,
          "实际 %s %s" % (r.status_code, r.get_json()))
H.chk("被拒的危险文件在 uploads 根目录**零残留**",
      not (set(_root_files()) - before), set(_root_files()) - before)

BAD_MD = {
    "含 <script>": b"# t\n<script>alert(1)</script>",
    "含 file:///": b"# t\n![x](file:///C:/Windows/win.ini)",
    "含 UNC 路径": b"# t\n![x](\\\\server\\share\\a.png)",
    "含 onerror=": b"# t\n<img src=x onerror=alert(1)>",
    "含 iframe": b"# t\n<iframe src=file:///C:/a></iframe>",
    "含 data:text/html": b"# t\n<a href=data:text/html,x>y</a>",
}
for label, content in BAD_MD.items():
    r = upload("evil.md", content)
    H.chk("恶意 Markdown（%s）→ 400" % label, r.status_code == 400,
          "实际 %s %s" % (r.status_code, r.get_json()))
H.chk("被拒的 Markdown 零残留", not (set(_root_files()) - before),
      set(_root_files()) - before)

GOOD = [
    ("ok.md", "# 标题\n\n| a | b |\n|---|---|\n| 1 | 2 |\n".encode("utf-8")),
    ("ok.docx", b"PK\x03\x04 fake docx bytes"),
    ("ok.pdf", b"%PDF-1.4 fake"),
    ("ok.jpg", b"\xff\xd8\xff\xe0 fake jpeg"),
]
for name, content in GOOD:
    r = upload(name, content)
    H.chk("正常 %s 上传 → 200（不误伤）" % name, r.status_code == 200,
          "实际 %s %s" % (r.status_code, r.get_json()))

# ============================================================
# C. 变异自证：删掉 denylist 判断
# ============================================================
print("\n=== C. 变异自证（删掉 denylist 判断）===")
mut_tmp, mut_dst = H.mutated_copy([
    ("    if ext_lower in DANGEROUS_UPLOAD_EXTS:",
     "    if False and ext_lower in DANGEROUS_UPLOAD_EXTS:"),
], prefix="hn_upload_mut_")
proc = H.run_probe(mut_dst, """
backend.PRINTER_TOKEN = "probe-tok"
backend.ADMIN_OPENIDS = {"up-admin"}
tok = backend.token_serializer.dumps("up-admin")
r = http.post("/api/upload",
              data={"file": (io.BytesIO(b"<html></html>"), "evil.html")},
              headers={"Authorization": "Bearer " + tok},
              content_type="multipart/form-data")
print("RESULT=%s" % r.status_code)
""")
mut_status = H.probe_value(proc)
H.chk("取消 denylist 后 .html 又被接收（证明 400 来自 denylist）",
      mut_status is not None and mut_status != "400",
      "变异副本 status=%r stderr=%r" % (mut_status, (proc.stderr or "")[-400:]))

H.finish()
