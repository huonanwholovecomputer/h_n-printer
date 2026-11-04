# -*- coding: utf-8 -*-
"""验收 · S2-03 上传类型白名单 + 魔数一致性（服务端）

依据：docs/安全加固修复计划_2026-10-23.md §S2-03（DS-16、DS-01 第二层）。

现场形状（修复前）：上传面只有「扩展名正则」+「S0-05 的危险类型 denylist」——
其余一律照收，压缩包/未知类型/改名伪装的文件都会进 uploads，交给店铺机的转换管线。
denylist 是"已知坏名单"，新花样默认放行；改名伪装（.pdf 里其实是 .html）它也拦不住。

本脚本：
  A. 静态：白名单常量 + 家族表 + `_check_upload_type` 的接线位置（白名单在落盘**前**、
     魔数在保存**后立即**校验且失败删文件）、denylist 仍在。
  B. 行为（真 Flask + 真 uploads 目录）：
     · 白名单外类型（.zip）→ 400 且 **uploads 零残留**；
     · 危险类型（.html）仍 400（denylist 没被绕过）；
     · 正常 pdf/docx/doc(OLE)/doc(RTF)/png/jpg/txt/csv/md → 200；
     · 改名伪装：.pdf 里是 PNG、.docx 里是 PDF、.png 里是 PDF → 400 且零残留；
     · 伪文本（.txt 含 NUL）→ 400；
     · MIME：text/html 配 .pdf → 400；image/png 配 .pdf → 400；**octet-stream 不误杀**（200）。
  C. 变异自证：把「家族不匹配就拒」改成放行 ⇒ 改名伪装被收下（判据能红）。

用法：python tests/verify_upload_type_whitelist.py     （exit 0 = 全部通过）
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
check_fn = block("def _check_upload_type(")
whitelist = block("UPLOAD_EXT_WHITELIST =")
families = block("_UPLOAD_EXT_FAMILIES =")

# ============================================================
# A. 静态接线
# ============================================================
print("=== A. 静态接线 ===")
for ext in ("pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "txt", "csv", "md",
            "jpg", "jpeg", "png", "bmp", "gif", "webp", "tiff", "tif"):
    H.chk("白名单含 .%s" % ext, '"%s"' % ext in whitelist, whitelist[:120])
H.chk("白名单**不含** html/htm/svg/js（D-2 永久拒绝）",
      all(('"%s"' % e) not in whitelist for e in ("html", "htm", "svg", "js", "mjs")))
H.chk("家族表区分 OLE(doc/xls/ppt) 与 ZIP(docx/xlsx/pptx)",
      '"doc": ("ole", "rtf")' in families and '"docx": ("zip",)' in families)
H.chk("文本类走「伪二进制」判据（无魔数）",
      "_TEXT_UPLOAD_EXTS" in code and "_looks_like_binary(" in check_fn)
H.chk("★ 白名单判定在**落盘之前**（压缩包/未知类型连临时文件都不写）",
      0 < upload.find("if ext_lower not in UPLOAD_EXT_WHITELIST:") < upload.find("f.save(temp_path)"))
H.chk("★ 魔数校验在保存之后、失败即删临时文件",
      upload.find("f.save(temp_path)") < upload.find("_check_upload_type(ext_lower, _head, _client_mime)")
      and "os.remove(temp_path)" in upload)
H.chk("denylist 仍在（两层防线，S0-05 不回退）",
      "if ext_lower in DANGEROUS_UPLOAD_EXTS:" in upload)
H.chk("危险 MIME 与具体 MIME 冲突都拒绝、octet-stream 放行",
      '_DANGEROUS_MIMES' in code and "octet-stream" in code)

# ============================================================
# B. 行为
# ============================================================
print("\n=== B. 行为（真 Flask + 真 uploads 目录）===")
TMP, DST = H.make_copy("hn_upw_")
backend = H.import_backend(DST)
backend.init_db()
backend.PRINTER_TOKEN = "upw-tok"
backend.ADMIN_OPENIDS = {"upw-user"}
http = backend.app.test_client()
AUTH = {"Authorization": "Bearer " + backend.token_serializer.dumps("upw-user")}


def upload(name, data, mime=None):
    kwargs = {"data": {"file": (io.BytesIO(data), name)}, "headers": AUTH,
              "content_type": "multipart/form-data"}
    if mime:
        kwargs["data"]["file"] = (io.BytesIO(data), name, mime)
    return http.post("/api/upload", **kwargs)


def leftovers():
    """uploads 根目录下的残留文件数（临时文件都落在这里，成功后会 move 到子目录）。"""
    try:
        return [n for n in os.listdir(backend.UPLOAD_DIR)
                if os.path.isfile(os.path.join(backend.UPLOAD_DIR, n))]
    except OSError:
        return []


PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
PDF = b"%PDF-1.4\n%fake\n"
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 16
ZIP = b"PK\x03\x04" + b"\x00" * 32
OLE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 32
RTF = b"{\\rtf1\\ansi test}"

# 正常格式不回归
for name, data in [("a.pdf", PDF), ("b.docx", ZIP), ("c.doc", OLE), ("d.doc", RTF),
                   ("e.png", PNG), ("f.jpg", JPEG), ("g.txt", "测试文本\n".encode("utf-8")),
                   ("h.csv", b"a,b\n1,2\n"), ("i.md", b"# test body")]:
    r = upload(name, data)
    H.chk("正常上传 %s → 200" % name, r.status_code == 200,
          (r.status_code, (r.get_json() or {}).get("message")))

# 白名单外
before = leftovers()
r = upload("evil.zip", ZIP)
H.chk("★ 白名单外 .zip → 400", r.status_code == 400, r.status_code)
H.chk("★ .zip 被拒后 uploads 根目录零残留", leftovers() == before,
      (before, leftovers()))

# 危险类型（denylist 仍生效）
r = upload("evil.html", b"<html><script>alert(1)</script></html>")
H.chk("危险类型 .html 仍 400（denylist 未被绕过）", r.status_code == 400, r.status_code)

# 改名伪装：扩展名对、内容不对
for name, data, why in [("fake.pdf", PNG, ".pdf 里是 PNG"),
                        ("fake.docx", PDF, ".docx 里是 PDF"),
                        ("fake.png", PDF, ".png 里是 PDF"),
                        ("fake.jpg", ZIP, ".jpg 里是 ZIP")]:
    before = leftovers()
    r = upload(name, data)
    H.chk("★ 改名伪装 %s（%s）→ 400" % (name, why), r.status_code == 400, r.status_code)
    H.chk("★ 伪装 %s 被拒后零残留" % name, leftovers() == before)

# 伪文本
r = upload("fake.txt", b"abc\x00\x01def")
H.chk("★ 伪文本（.txt 含 NUL）→ 400", r.status_code == 400, r.status_code)

# MIME 三方
r = upload("m1.pdf", PDF, mime="text/html")
H.chk("★ MIME=text/html 配 .pdf → 400", r.status_code == 400, r.status_code)
r = upload("m2.pdf", PDF, mime="image/png")
H.chk("★ MIME=image/png 配 .pdf（具体类型冲突）→ 400", r.status_code == 400, r.status_code)
r = upload("m3.pdf", PDF, mime="application/octet-stream")
H.chk("★ MIME=application/octet-stream 不误杀（微信就是这么发的）→ 200",
      r.status_code == 200, r.status_code)

# ============================================================
# C. 变异自证：家族不匹配也放行
# ============================================================
print("\n=== C. 变异自证（关掉「家族不匹配就拒」）===")
mut_tmp, mut_dst = H.mutated_copy([
    ('    if actual not in ext_families:\n'
     '        return False, ("文件内容与扩展名不一致（可能是改名后的其他格式），已拒绝——"\n'
     '                       "请用原始文件或另存为 PDF 后重试")\n',
     '    if False:\n'
     '        return False, "disabled"\n'),
], prefix="hn_upw_mut_")
proc = H.run_probe(mut_dst, r"""
import io
backend.PRINTER_TOKEN = "probe-tok"
backend.ADMIN_OPENIDS = {"upw-user"}
http = backend.app.test_client()
AUTH = {"Authorization": "Bearer " + backend.token_serializer.dumps("upw-user")}
r = http.post("/api/upload",
              data={"file": (io.BytesIO(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32), "fake.pdf")},
              headers=AUTH, content_type="multipart/form-data")
print("STATUS=%s" % r.status_code)
""")
mut_status = H.probe_value(proc, "STATUS")
H.chk("★ 变异（关掉家族校验）⇒ 改名伪装被收下（200，判据能红）", mut_status == "200", mut_status)

H.finish()
