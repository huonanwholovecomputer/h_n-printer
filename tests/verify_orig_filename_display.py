# -*- coding: utf-8 -*-
"""验收 · 「我 → 我的文件」显示的必须是顾客的原始文件名（2026-10-25 现场）

现场：小程序「我 → 我的文件」列出的是
  `tmp_617c7e76e8ab8f37b3c05d1bfb89c8187591a6394eb80fcc.pdf`（另有「打印中」徽标），
而顾客上传的是「示波器的使用.pdf」。

根因（生产库实测，只读查询）：
  · `files.original_name` 存的是 multipart 文件名。微信小程序 `wx.uploadFile` 的 multipart
    文件名取自「临时路径的 basename」，微信把它变成 `tmp_<40~64 位十六进制>.<ext>`；
    生产库 61 行里 59 行是这种临时名。
  · 真正有意义的名字在 `order_files.file_name`（提交订单时客户端把自己选的文件名带上来）；
    生产库 1472 条 order_files 里 0 条是 tmp_ 名。

四条落地（本脚本逐条钉住）：
  ① 客户端 /api/upload 表单带 `file_name`（小程序 pages/index/index.js）；
  ② 服务端优先用它当 `files.original_name`（`clean_original_name` 规整；不通过退回临时名）；
  ③ `/api/my_files` 用 `order_files.file_name`（该文件最近一次下单用过的名字）兜底显示；
  ④ 提交订单时若该行仍是临时名，按本次提交的真实名回填（自愈，同扩展名才写）。

用法：python tests/verify_orig_filename_display.py     （exit 0 = 全部通过）
"""
import io
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _sec_harness as H  # noqa: E402

H.install_cleanup_hook()

MP_INDEX_JS = os.path.join(REPO, "mobile_apps", "h_n_print", "pages", "index", "index.js")
MP_ME_WXML = os.path.join(REPO, "mobile_apps", "h_n_print", "pages", "me", "me.wxml")
APP_PRINT_JS = os.path.join(REPO, "mobile_apps", "android_app", "www", "print.js")


def read(path):
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


src = H.read_app_source()
code = H.strip_comments(src)
mp_index = read(MP_INDEX_JS)
mp_me_wxml = read(MP_ME_WXML)
app_print = read(APP_PRINT_JS)

TMP_NAME = "tmp_617c7e76e8ab8f37b3c05d1bfb89c8187591a6394eb80fcc.pdf"
REAL_NAME = "示波器的使用.pdf"
PDF = b"%PDF-1.4\n%orig-name test\n%%EOF\n"

_bad = H.failed


def fail_count():
    return _bad()


# ============================================================
# A. 静态接线
# ============================================================
print("=== A. 静态接线 ===")
H.chk("后端：临时名识别 + 展示名规整两个辅助函数存在",
      "def looks_like_temp_upload_name(" in code
      and "def clean_original_name(" in code
      and "_TEMP_UPLOAD_NAME_RE" in code)
H.chk("后端：/api/upload 优先吃客户端自报的真实文件名",
      'request.form.get("file_name")' in code
      and "display_name = clean_original_name(" in code)
H.chk("后端：落库/MD5 索引/页数分析/响应都改用 display_name（不再写 f.filename）",
      "original_name=display_name" in code
      and "(file_id, display_name, saved_name, file_path, file_size," in code
      and "request_page_analysis(file_id, display_name" in code
      and '"original_name": display_name,' in code)
H.chk("后端：/api/my_files 用 order_files.file_name 兜底展示名",
      "display_names" in code
      and 'SELECT file_id, file_name FROM order_files' in code
      and 'display_names.get(r["id"]) or r["original_name"]' in code)
H.chk("后端：提交订单时临时名自愈（persist_original_name 计划项 + 写事务内 UPDATE）",
      "persist_original_name = None" in code
      and "looks_like_temp_upload_name(_stored_name)" in code
      and 'if item.get("persist_original_name"):' in code
      and "UPDATE files SET original_name = ? WHERE id = ?" in code)
H.chk("小程序：上传表单带上真实文件名 file_name（取的是卡片上的 name）",
      "file_name: (() => {" in mp_index
      and "this.data.selectedFiles[i].name" in mp_index)
H.chk("小程序：「我的文件」列表用 original_name 渲染（字段名不变 ⇒ 后端改口径就够）",
      "{{item.original_name}}" in mp_me_wxml
      and "data-name=\"{{item.original_name}}\"" in mp_me_wxml)
H.chk("APP：上传走浏览器 FormData（File.name 即真实名）—— 无需额外字段",
      "fd.append('file', f.file)" in app_print)

# ============================================================
# B. 行为（真 Flask + 真 SQLite）
# ============================================================
print("\n=== B. 行为（真 Flask + 真 SQLite）===")
TMP, DST = H.make_copy("hn_origname_")
backend = H.import_backend(DST)
backend.init_db()
backend.ADMIN_OPENIDS = {"u-a", "u-b"}
http = backend.app.test_client()
A = {"Authorization": "Bearer " + backend.token_serializer.dumps("u-a")}
B = {"Authorization": "Bearer " + backend.token_serializer.dumps("u-b")}


def upload(headers, multipart_name=TMP_NAME, form_name=None):
    form = {"file": (io.BytesIO(PDF), multipart_name)}
    if form_name is not None:
        form["file_name"] = form_name
    return http.post("/api/upload", data=form, headers=headers,
                     content_type="multipart/form-data")


def file_row(fid):
    conn = backend.get_db()
    try:
        return conn.execute("SELECT * FROM files WHERE id = ?", (fid,)).fetchone()
    finally:
        conn.close()


def submit(headers, fid, name, crid):
    return http.post("/api/submit_order", json={
        "client": "app", "duplex": "off",
        "files": [{"file_id": fid, "file": name, "copies": 1}],
        "client_request_id": crid,
    }, headers=headers)


def my_files(headers):
    r = http.get("/api/my_files?page=1&per_page=50", headers=headers)
    d = r.get_json() or {}
    return {f["file_id"]: f for f in (d.get("files") or [])}


# ---- B1/B2：上传口径 ----
r = upload(A, form_name=REAL_NAME)
fid_real = (r.get_json() or {}).get("file_id")
H.chk("B1 上传带 file_name → files.original_name = 顾客的原文件名",
      file_row(fid_real)["original_name"] == REAL_NAME,
      file_row(fid_real)["original_name"] if fid_real else "无 file_id")
H.chk("B1b 上传响应里回的也是原文件名",
      (r.get_json() or {}).get("original_name") == REAL_NAME,
      (r.get_json() or {}).get("original_name"))

r = upload(A)                      # 老客户端：没有 file_name 字段
fid_old = (r.get_json() or {}).get("file_id")
H.chk("B2 老客户端（无 file_name）→ 退回 multipart 名（行为不变，不报错）",
      file_row(fid_old)["original_name"] == TMP_NAME)

# ---- B3/B4/B5：展示名校验（路径/异扩展名/控制字符）----
r = upload(A, form_name="../../etc/passwd.pdf")
fid_path = (r.get_json() or {}).get("file_id")
H.chk("B3 带路径的文件名只取 basename（客户端不能借展示名改路径）",
      file_row(fid_path)["original_name"] == "passwd.pdf",
      file_row(fid_path)["original_name"] if fid_path else "无")

r = upload(A, form_name="evil.exe")
fid_bad_ext = (r.get_json() or {}).get("file_id")
H.chk("B4 扩展名与本次上传不一致 → 忽略自报名，退回 multipart 名（不拒单）",
      file_row(fid_bad_ext)["original_name"] == TMP_NAME,
      file_row(fid_bad_ext)["original_name"] if fid_bad_ext else "无")

r = upload(A, form_name="a\x00b\n.pdf")
fid_ctrl = (r.get_json() or {}).get("file_id")
H.chk("B5 自报名里的控制字符/换行被剔除",
      file_row(fid_ctrl)["original_name"] == "ab.pdf",
      file_row(fid_ctrl)["original_name"] if fid_ctrl else "无")

# ---- B6：老行（original_name 还是 tmp_）+ 有下单记录 → my_files 显示真实名 ----
r = submit(A, fid_old, REAL_NAME, "origname-b6")
ok_b6 = (r.get_json() or {}).get("success")
H.chk("B6 提交订单成功（前置）", bool(ok_b6), r.get_json())
mine = my_files(A)
H.chk("B6b /api/my_files 对 tmp_ 行用 order_files.file_name 兜底",
      mine.get(fid_old, {}).get("original_name") == REAL_NAME,
      mine.get(fid_old, {}).get("original_name"))
H.chk("B6c 自愈后 files.original_name 也已是真实名（提交时回填）",
      file_row(fid_old)["original_name"] == REAL_NAME,
      file_row(fid_old)["original_name"])

# ---- B7：临时名 + 没有下单记录 → 仍显示 multipart 名（不谎报）----
r = upload(A)                      # 新的一行，从未下单
fid_noorder = (r.get_json() or {}).get("file_id")
mine = my_files(A)
H.chk("B7 没有下单记录的临时名行 → 照实显示（不编造名字）",
      mine.get(fid_noorder, {}).get("original_name") == TMP_NAME,
      mine.get(fid_noorder, {}).get("original_name"))

# ---- B8：不是临时名就别覆盖（用户本来正确的名字优先）----
r = upload(A, form_name="我自己的名字.pdf")
fid_keep = (r.get_json() or {}).get("file_id")
submit(A, fid_keep, "别的名字.pdf", "origname-b8")
H.chk("B8 已是真实名的行不被提交名覆盖",
      file_row(fid_keep)["original_name"] == "我自己的名字.pdf",
      file_row(fid_keep)["original_name"])

# ---- B9：异扩展名的提交名不写进临时名行（防止展示名与真实格式脱钩）----
r = upload(A, multipart_name="tmp_" + "a" * 40 + ".pdf")   # 真正的临时名
fid_ext = (r.get_json() or {}).get("file_id")
submit(A, fid_ext, "伪装.docx", "origname-b9")
H.chk("B9 提交名扩展名与文件不一致 → 不回填（避免按扩展名分支的逻辑被带偏）",
      file_row(fid_ext)["original_name"].startswith("tmp_"),
      file_row(fid_ext)["original_name"])

# ---- B10：别人的文件不出现在我的列表里（顺带守住 openid 口径）----
other = my_files(B)
H.chk("B10 my_files 只回自己的行", fid_old not in other and fid_real not in other)

# ============================================================
# C. 变异自证（判据必须能红）
# ============================================================
print("\n=== C. 变异自证 ===")

PROBE_BODY = r"""
import io, json
backend.ADMIN_OPENIDS = {"u-a"}
A = {"Authorization": "Bearer " + backend.token_serializer.dumps("u-a")}
PDF = b"%PDF-1.4\\n%m\\n%%EOF\\n"
r = http.post("/api/upload", data={"file": (io.BytesIO(PDF), "tmp_%s.pdf" % ("b" * 40)),
                                   "file_name": "真实名.pdf"},
              headers=A, content_type="multipart/form-data")
fid = (r.get_json() or {}).get("file_id")
row = file_row(fid) if False else backend.get_db().execute(
    "SELECT original_name FROM files WHERE id = ?", (fid,)).fetchone()
print("STORED=%s" % (row["original_name"] if row else "none"))
http.post("/api/submit_order", json={"client": "app", "duplex": "off",
          "files": [{"file_id": fid, "file": "真实名.pdf", "copies": 1}],
          "client_request_id": "mut-origname-1"}, headers=A)
# 再造一行并模拟「历史数据」：上传成临时名 → 下单（自愈成真名）→ 再把
# files.original_name 改回临时名（2026-10-25 之前上传留下的正是这个样子），
# 然后看列表口径走的是不是 order_files.file_name。
r2 = http.post("/api/upload", data={"file": (io.BytesIO(PDF), "tmp_%s.pdf" % ("c" * 40))},
               headers=A, content_type="multipart/form-data")
fid2 = (r2.get_json() or {}).get("file_id")
tmp2 = "tmp_%s.pdf" % ("c" * 40)
http.post("/api/submit_order", json={"client": "app", "duplex": "off",
          "files": [{"file_id": fid2, "file": "列表里的真名.pdf", "copies": 1}],
          "client_request_id": "mut-origname-2"}, headers=A)
c2 = backend.get_db()
c2.execute("UPDATE files SET original_name = ? WHERE id = ?", (tmp2, fid2))
c2.commit()
c2.close()
d = (http.get("/api/my_files?page=1&per_page=50", headers=A).get_json() or {})
row2 = [f for f in (d.get("files") or []) if f["file_id"] == fid2]
print("LISTED=%s" % (row2[0]["original_name"] if row2 else "none"))
print("BASE_STORED=%s" % (backend.get_db().execute(
    "SELECT original_name FROM files WHERE id = ?", (fid2,)).fetchone()["original_name"]))
"""

# C0. 基线（未变异）必须绿
proc = H.run_probe(DST, PROBE_BODY)
H.chk("C0 基线：上传存真实名 + 列表回真实名（探针本身可信）",
      H.probe_value(proc, "STORED") == "真实名.pdf"
      and H.probe_value(proc, "LISTED") == "列表里的真名.pdf",
      (proc.stdout or "")[-400:])

# C1. 变异：my_files 不再用 order_files 兜底
tmpm, dstm = H.mutated_copy([(
    '            "original_name": display_names.get(r["id"]) or r["original_name"],',
    '            "original_name": r["original_name"],',
)], prefix="hn_origname_mut1_")
proc = H.run_probe(dstm, PROBE_BODY)
H.chk("★ C1 变异（my_files 不兜底）⇒ 列表回的是 tmp_ 名（判据能红）",
      (H.probe_value(proc, "LISTED") or "").startswith("tmp_"),
      H.probe_value(proc, "LISTED"))

# C2. 变异：上传不再吃 file_name
tmpm2, dstm2 = H.mutated_copy([(
    'display_name = clean_original_name(request.form.get("file_name"), ext_lower) or f.filename',
    'display_name = f.filename',
)], prefix="hn_origname_mut2_")
proc = H.run_probe(dstm2, PROBE_BODY)
H.chk("★ C2 变异（上传忽略 file_name）⇒ 落库是 tmp_ 名（判据能红）",
      (H.probe_value(proc, "STORED") or "").startswith("tmp_"),
      H.probe_value(proc, "STORED"))

# C3. 变异：提交不自愈
tmpm3, dstm3 = H.mutated_copy([(
    '            if item.get("persist_original_name"):',
    '            if False:',
)], prefix="hn_origname_mut3_")
proc = H.run_probe(dstm3, PROBE_BODY)
H.chk("★ C3 变异（提交不自愈）⇒ files.original_name 仍是 tmp_（判据能红）",
      (H.probe_value(proc, "BASE_STORED") or "").startswith("tmp_"),
      H.probe_value(proc, "BASE_STORED"))

H.finish()
