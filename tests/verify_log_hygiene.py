# -*- coding: utf-8 -*-
"""验收 · S2-11 日志 / 错误面收尾

依据：docs/安全加固修复计划_2026-10-23.md §S2-11（DS-20）。

现场形状（修复前）：
  · 上传路由把**顾客原文件名**（`f.filename`）与**绝对路径**（`file_path`）直接打进日志，
    而日志会进 journald（留 30 天）并能被「日志管理」打包回传给运营端；
  · 两处 printer 接口把 `str(e)` 原样回给调用方（可能带 SQL / 路径）；
  · journald 没有容量上限（默认按磁盘 10% 滚动）。

本脚本：
  A. 静态：`_redact_filename` / `_rel_display` / `_redact_paths` 存在且被用到；
     所有 print 里不再出现裸 `{f.filename}` 或绝对路径变量；`str(e)` 不再回 500；
     异常日志过 `_redact_paths`；journald drop-in 已入库。
  B. 行为（真 Flask + 抓 stdout）：
     · 上传一个中文敏感文件名的合法 PDF ⇒ 日志里**看不到**原名、**看不到**绝对路径，
       能看到 `***.pdf` 与相对路径；
     · 让公开接口在深处抛一个带路径的异常 ⇒ 响应是通用文案（不含异常文本/路径），
       日志里路径已被替换成 `<path>`。
  C. 变异自证：把 `_redact_filename` 改成原样返回 ⇒ 上传日志泄漏顾客名（判据能红）。

用法：python tests/verify_log_hygiene.py     （exit 0 = 全部通过）
"""
import contextlib
import io
import os
import re
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _sec_harness as H  # noqa: E402

H.install_cleanup_hook()
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(REPO, "mobile_apps", "printer-backend")
src = H.read_app_source()
code = H.strip_comments(src)

prints = [ln for ln in code.splitlines() if "print(" in ln or ln.strip().startswith('f"')]
print_block = "\n".join(prints)

# ============================================================
# A. 静态
# ============================================================
print("=== A. 静态 ===")
H.chk("脱敏助手齐全：_redact_filename / _rel_display / _redact_paths",
      all(f in code for f in ("def _redact_filename(", "def _rel_display(", "def _redact_paths(")))
H.chk("★ 日志里不再直接打顾客原文件名（{f.filename}）",
      not re.search(r"\{f\.filename\}", print_block), 
      [ln.strip()[:90] for ln in prints if "{f.filename}" in ln][:3])
H.chk("★ 日志里不再直接打绝对路径变量",
      not re.search(r"\{(file_path|fpath|path)\}", print_block),
      [ln.strip()[:90] for ln in prints if re.search(r"\{(file_path|fpath|path)\}", ln)][:3])
H.chk("上传路由的日志用的是脱敏助手",
      "_redact_filename(f.filename)" in code and "_rel_display(file_path)" in code)
H.chk("★ 不再把 str(e) 回给客户端（两处 500 已改通用文案）",
      not re.search(r'"message":\s*str\(e\)', code),
      re.findall(r'.{40}"message":\s*str\(e\).{20}', code)[:2])
H.chk("异常日志走 _redact_paths（路径替换成 <path>）",
      code.count("_redact_paths(_scrub_secret(str(e))") >= 2)
H.chk("journald 容量上限 drop-in 入库",
      os.path.exists(os.path.join(BACKEND, "ops", "journald-hn-limits.conf")))

# ============================================================
# B. 行为
# ============================================================
print("\n=== B. 行为（真 Flask + 抓 stdout）===")
TMP, DST = H.make_copy("hn_log_")
backend = H.import_backend(DST)
backend.init_db()
backend.PRINTER_TOKEN = "log-tok"
backend.ADMIN_OPENIDS = {"log-user"}
http = backend.app.test_client()
AUTH = {"Authorization": "Bearer " + backend.token_serializer.dumps("log-user")}

SECRET_NAME = "顾客秘密甲乙丙.pdf"
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    r = http.post("/api/upload",
                  data={"file": (io.BytesIO(b"%PDF-1.4\n%fake\n"), SECRET_NAME)},
                  headers=AUTH, content_type="multipart/form-data")
log = buf.getvalue()

H.chk("上传成功（否则后面的日志断言没有意义）", r.status_code == 200, r.status_code)
H.chk("★ 日志里**没有**顾客原文件名", SECRET_NAME not in log and "顾客秘密" not in log,
      [ln for ln in log.splitlines() if "***" in ln][:2])
H.chk("★ 日志里**没有**绝对路径（不泄漏部署目录）",
      os.path.abspath(backend.UPLOAD_DIR) not in log and DST not in log,
      [ln for ln in log.splitlines() if "上传" in ln][:2])
H.chk("日志里能看到脱敏后的名字（***.pdf）与相对路径",
      "***.pdf" in log and re.search(r"\*{3}\.pdf -> (pdf|png|jpg|doc|docx)[/\\]", log) is not None,
      [ln.strip()[:120] for ln in log.splitlines() if "文件上传" in ln][:2])

# 强制一个带路径的异常：公开接口 → 全局 errorhandler
_real_load_claim = backend.load_claim


def _boom():
    raise RuntimeError("failed to open /home/printer-backend/orders.db (readonly)")


backend.load_claim = _boom
buf2 = io.StringIO()
with contextlib.redirect_stdout(buf2):
    r2 = http.get("/api/claiming_devices")
log2 = buf2.getvalue()
backend.load_claim = _real_load_claim
body2 = r2.get_json() or {}
H.chk("★ 500 响应是通用文案，不含异常文本/路径",
      r2.status_code == 500 and body2.get("message") == "服务器内部错误"
      and "orders.db" not in (body2.get("message") or ""),
      (r2.status_code, body2.get("message")))
H.chk("★ 异常日志里路径已替换成 <path>",
      "[ERROR]" in log2 and "/home/printer-backend/orders.db" not in log2
      and "<path>" in log2,
      [ln.strip()[:140] for ln in log2.splitlines() if "[ERROR]" in ln][:2])

# ============================================================
# C. 变异自证：脱敏改回原样
# ============================================================
print("\n=== C. 变异自证（关掉文件名脱敏）===")
mut_tmp, mut_dst = H.mutated_copy([
    ('    return ("***" + ext) if ext else "***"',
     '    return name or ""'),
], prefix="hn_log_mut_")
proc = H.run_probe(mut_dst, r"""
import contextlib, io
backend.PRINTER_TOKEN = "probe-tok"
backend.ADMIN_OPENIDS = {"log-user"}
http = backend.app.test_client()
AUTH = {"Authorization": "Bearer " + backend.token_serializer.dumps("log-user")}
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    http.post("/api/upload", data={"file": (io.BytesIO(b"%PDF-1.4\n%fake\n"), "SECRETPROBE.pdf")},
              headers=AUTH, content_type="multipart/form-data")
print("LEAK=%s" % ("SECRETPROBE" in buf.getvalue()))
""")
leak = H.probe_value(proc, "LEAK")
H.chk("★ 变异（脱敏原样返回）⇒ 日志泄漏顾客文件名（判据能红）", leak == "True", leak)

H.finish()
