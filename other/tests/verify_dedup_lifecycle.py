# -*- coding: utf-8 -*-
"""验收 · S2-05 MD5 去重的文件生命周期（引用计数 / 副本 / 存在性预言机）

依据：docs/安全加固修复计划_2026-10-23.md §S2-05（DS-14）。

现场形状（修复前）：上传按 MD5 去重，**多个 `files` 行共享同一物理文件**。后果两条：
  ① 生命周期：任一行到期/被属主删除就 `os.remove` ⇒ 另一方行指向不存在的文件
     （下载 404、订单永久卡死）；
  ② 预言机：响应里的 `reused:true`（和「文件已存在」文案）让任何人上传一份候选内容
     就能问出"这份内容是否已经在系统里"（可用来确认某人是否上传过某份材料）。

修法（本批）：
  · 每个上传都保存**自己的副本**（MD5 只用于「已验证页数」缓存命中，不再共享路径）；
  · 三种删除口径都加「同一物理路径还被别的行引用就不删盘」的**存量兜底**
    （批量清理用一次扫描的 `_path_reference_index`，单行删除用 `_path_shared_by_others`）；
  · 响应去掉 `reused` 字段与「文件已存在」文案。

本脚本：
  A. 静态：上传块不再复用别人的路径 / 不再删临时文件、响应无 `reused`；
     清理与删除接口的共享保护接线；页数缓存的 files 表兜底存在。
  B. 行为（真 Flask + 真 uploads + 真 SQLite）：
     · 两个用户上传同一内容 → 两条独立副本、都能下载；响应无 `reused`、文案一致；
     · **一方删除**后另一方仍能下载（旧行为在这里 404）；
     · 存量共享行：一方到期清理 → 另一方仍能下载；最后一方到期才真删盘；
     · 存量共享行：属主删自己的行 → 另一方仍能下载；最后一个引用删除才真删盘；
     · 页数缓存：删掉副本后，同 MD5 再上传仍能从 files 表命中已验证页数。
  C. 变异自证（子进程，改回修复前形态必须红）：
     · 关掉清理的共享保护 ⇒ 一方到期把另一方文件删掉（悬空）；
     · 关掉删除接口的共享保护 ⇒ 属主删除把另一方文件删掉（下载 404）；
     · 把 `reused` 字段加回响应 ⇒ 预言机重新成立。

用法：python tests/verify_dedup_lifecycle.py     （exit 0 = 全部通过）
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
cleanup = block("def cleanup_expired_files(")
del_block = block("def delete_my_file(")
cache_fn = block("def _cached_verified_page_count(")
refidx_fn = block("def _path_reference_index(")
shared_fn = block("def _path_shared_by_others(")

# ============================================================
# A. 静态接线
# ============================================================
print("=== A. 静态接线 ===")
H.chk("★ 上传块不再把新行指向别人的物理文件（无 existing_path 复用分支）",
      upload != "" and "existing_path" not in upload, "upload 块仍有 existing_path")
H.chk("★ 上传块在算完 MD5 之后不再删临时文件（没有「命中就删掉本次上传」）",
      upload.find("file_md5 = get_file_md5(temp_path)") >= 0 and
      "os.remove(temp_path)" not in upload[upload.find("file_md5 = get_file_md5(temp_path)"):],
      "MD5 之后还出现 os.remove(temp_path)")
H.chk("★ 每个上传都落一份自己的副本（shutil.move 在 MD5 之后）",
      0 < upload.find("file_md5 = get_file_md5(temp_path)") <
      upload.find("shutil.move(temp_path, final_path)"))
H.chk("★ 响应不再带 reused 字段 / 不再有「文件已存在」文案",
      '"reused"' not in upload and "文件已存在" not in upload)
H.chk("页数缓存有 files 表兜底（删掉副本也不丢「这版文档几页」）",
      cache_fn != "" and "FROM files WHERE md5 = ?" in cache_fn
      and "page_count_verified = 1" in cache_fn)
H.chk("★ 页数缓存只认**同一属主**（否则「拿到页数 vs 等分析」是存在性旁路）",
      "AND openid = ?" in cache_fn and 'entry.get("openid") == openid' in cache_fn)
H.chk("★ 批量清理的共享保护：一次扫描 + 集合判断",
      refidx_fn != "" and "_path_reference_index(conn)" in cleanup
      and "path_refs.get(_norm_path)" in cleanup)
H.chk("★ 共享保护必须在 os.remove(file_path) 之前（否则文件已经没了）",
      0 <= cleanup.find("_ref_ids and len(_ref_ids - {file_id})")
      < cleanup.find("os.remove(file_path)"))
H.chk("★ 删除接口的共享保护：共享时不删盘",
      shared_fn != "" and "shared = _path_shared_by_others(conn, path, file_id)" in del_block
      and "if path and not shared and os.path.exists(path):" in del_block)
H.chk("删除接口共享时不清 MD5 索引（另一方还要靠它命中页数）",
      "if path and not shared:" in del_block)

# ============================================================
# B. 行为
# ============================================================
print("\n=== B. 行为（真 Flask + 真 uploads）===")
TMP, DST = H.make_copy("hn_dedup_")
backend = H.import_backend(DST)
backend.init_db()
backend.PRINTER_TOKEN = "dedup-tok"
backend.ADMIN_OPENIDS = {"u1", "u2"}
http = backend.app.test_client()
A1 = {"Authorization": "Bearer " + backend.token_serializer.dumps("u1")}
A2 = {"Authorization": "Bearer " + backend.token_serializer.dumps("u2")}

PDF = b"%PDF-1.4\n%S2-05 lifecycle test\n%%EOF\n"


def upload(name, data, headers, retention=None):
    form = {"file": (io.BytesIO(data), name)}
    if retention is not None:
        form["retention_hours"] = str(retention)
    return http.post("/api/upload", data=form, headers=headers,
                     content_type="multipart/form-data")


def dl(fid, openid):
    tok = backend.download_serializer.dumps({"f": fid, "o": openid})
    return http.get("/api/download/%s?t=%s" % (fid, tok))


def row_of(fid):
    conn = backend.get_db()
    try:
        return conn.execute("SELECT * FROM files WHERE id = ?", (fid,)).fetchone()
    finally:
        conn.close()


def norm(p):
    return os.path.normpath(p or "")


r1 = upload("same.pdf", PDF, A1)
j1 = r1.get_json() or {}
H.chk("用户 1 上传 → 200 且成功", r1.status_code == 200 and j1.get("success") is True,
      (r1.status_code, j1))
H.chk("★ 响应不带 reused（存在性预言机已拆）", "reused" not in j1, sorted(j1.keys()))
H.chk("文案统一为「文件上传成功」（不透露是否已存在）",
      j1.get("message") == "文件上传成功", j1.get("message"))

r2 = upload("same.pdf", PDF, A2)
j2 = r2.get_json() or {}
H.chk("用户 2 上传同一内容 → 200", r2.status_code == 200 and j2.get("success") is True,
      (r2.status_code, j2))
H.chk("★ 同一内容的响应字段形状一致、文案一致（问不出「已存在」）",
      set(j1.keys()) == set(j2.keys()) and j1.get("message") == j2.get("message"),
      (sorted(j1.keys()), sorted(j2.keys())))
H.chk("★ 响应不带 reused（第二个上传也一样）", "reused" not in j2)

fid1, fid2 = j1.get("file_id"), j2.get("file_id")
p1, p2 = norm(row_of(fid1)["path"]), norm(row_of(fid2)["path"])
H.chk("★ 两个用户各有一份**独立副本**（路径不同）", p1 and p2 and p1 != p2, (p1, p2))
H.chk("两份副本都在磁盘上", os.path.exists(p1) and os.path.exists(p2))
H.chk("两份都计入各自的配额（size 均 > 0）",
      int(row_of(fid1)["size"] or 0) > 0 and int(row_of(fid2)["size"] or 0) > 0)

r3 = upload("same-again.pdf", PDF, A1)
fid3 = (r3.get_json() or {}).get("file_id")
p3 = norm(row_of(fid3)["path"])
H.chk("★ 同一用户重复上传也是新副本（第 3 条路径）", p3 and len({p1, p2, p3}) == 3,
      (p1, p2, p3))

H.chk("删除前用户 2 的文件可下载", dl(fid2, "u2").status_code == 200)
H.chk("用户 1 删除自己的文件 → 200", http.delete("/api/file/" + fid1, headers=A1).status_code == 200)
H.chk("★ 另一方（用户 2）的文件仍可下载（未被连带删除）",
      dl(fid2, "u2").status_code == 200 and os.path.exists(p2))
H.chk("★ 用户 1 的另一份（fid3）不受影响", os.path.exists(p3)
      and dl(fid3, "u1").status_code == 200)

# ---- 存量共享行：清理（一方到期，另一方未到期）----
conn = backend.get_db()
shared_dir = os.path.join(backend.UPLOAD_DIR, "pdf")
os.makedirs(shared_dir, exist_ok=True)
shared_path = os.path.join(shared_dir, "legacy_shared.pdf")
with open(shared_path, "wb") as f:
    f.write(PDF)
conn.execute(
    "INSERT INTO files (id, original_name, saved_name, path, size, created_at, page_count,"
    " page_count_verified, md5, openid, retention_hours) VALUES"
    " ('L1', 'old.pdf', 'legacy_shared.pdf', ?, ?, '2026-01-01 00:00:00', 1, 1, '', 'u1', 24)",
    (shared_path, len(PDF)))
conn.execute(
    "INSERT INTO files (id, original_name, saved_name, path, size, created_at, page_count,"
    " page_count_verified, md5, openid, retention_hours) VALUES"
    " ('L2', 'keep.pdf', 'legacy_shared.pdf', ?, ?, ?, 1, 1, '', 'u2', 168)",
    (shared_path, len(PDF), backend.datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
conn.commit()
conn.close()
backend.cleanup_expired_files()
H.chk("存量共享：L1 到期后记录被清（path 为空）", not (row_of("L1")["path"] or ""),
      row_of("L1")["path"])
H.chk("★ 存量共享：物理文件仍存在（另一方 L2 还要用）", os.path.exists(shared_path))
H.chk("★ 存量共享：L2 仍能正常下载（未悬空）", dl("L2", "u2").status_code == 200)

# 让 L2 也到期：最后一个引用消失后才真删盘
conn = backend.get_db()
conn.execute("UPDATE files SET created_at = '2026-01-01 00:00:00', retention_hours = 24 "
             "WHERE id = 'L2'")
conn.commit()
conn.close()
backend.cleanup_expired_files()
H.chk("★ 最后一个引用到期后才真删盘", not os.path.exists(shared_path))
H.chk("L2 记录被清（path 为空）", not (row_of("L2")["path"] or ""))

# ---- 存量共享行：属主删除（另一方未到期）----
shared2 = os.path.join(shared_dir, "legacy_shared2.pdf")
with open(shared2, "wb") as f:
    f.write(PDF)
conn = backend.get_db()
for fid, oid in (("D1", "u1"), ("D2", "u2")):
    conn.execute(
        "INSERT INTO files (id, original_name, saved_name, path, size, created_at, page_count,"
        " page_count_verified, md5, openid, retention_hours) VALUES"
        " (?, ?, 'legacy_shared2.pdf', ?, ?, ?, 1, 1, '', ?, 168)",
        (fid, fid + ".pdf", shared2, len(PDF),
         backend.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), oid))
conn.commit()
conn.close()
H.chk("存量共享：D1 属主删除 → 200", http.delete("/api/file/D1", headers=A1).status_code == 200)
H.chk("★ 存量共享：属主删除后物理文件仍在（另一方还要下载）", os.path.exists(shared2))
H.chk("★ 存量共享：另一方 D2 仍可下载", dl("D2", "u2").status_code == 200)
H.chk("存量共享：D2 属主删除 → 200", http.delete("/api/file/D2", headers=A2).status_code == 200)
H.chk("★ 最后一个引用被删后才真删盘", not os.path.exists(shared2))

# ---- 单属主删除仍真删盘（不因新保护而漏删）----
r4 = upload("solo.pdf", PDF + b"solo", A1)
fid4 = (r4.get_json() or {}).get("file_id")
solo_path = norm(row_of(fid4)["path"])
H.chk("单独属主删除 → 200", http.delete("/api/file/" + fid4, headers=A1).status_code == 200)
H.chk("★ 无共享引用时物理文件被真正删除（不是「永不删」）", not os.path.exists(solo_path))

# ---- 页数缓存：删掉副本后仍从 files 表命中「已验证页数」（只对**同一属主**）----
ZIP = b"PK\x03\x04" + b"\x00" * 64 + b"docx-pages-cache"
r5 = upload("cached.docx", ZIP, A1)
fid5 = (r5.get_json() or {}).get("file_id")
md5_5 = row_of(fid5)["md5"]
conn = backend.get_db()
conn.execute("UPDATE files SET page_count = 5, page_count_verified = 1 WHERE id = ?", (fid5,))
conn.commit()
conn.close()
# 把索引条目摘掉（模拟"那一份副本被删/清理后索引条目也没了"）
idx = backend.load_md5_index()
idx.pop(md5_5, None)
backend.save_md5_index(idx)
H.chk("索引里已无该 MD5 条目", md5_5 not in backend.load_md5_index())
r6 = upload("cached-again.docx", ZIP, A1)
j6 = r6.get_json() or {}
H.chk("★ 索引缺失时仍从 files 表命中已验证页数（同一属主，避免重复分析/多等一轮）",
      r6.status_code == 200 and j6.get("page_count") == 5, (r6.status_code, j6.get("page_count")))
r7 = upload("cached-other.docx", ZIP, A2)
j7 = r7.get_json() or {}
H.chk("★ 页数缓存**不跨用户**（否则「上传即拿到页数 vs 要等分析」会重新变成存在性旁路）",
      r7.status_code == 200 and j7.get("page_count") == 0, (r7.status_code, j7.get("page_count")))

# ============================================================
# C. 变异自证
# ============================================================
print("\n=== C. 变异自证（改回修复前形态必须红）===")

# C1：关掉清理的共享保护 ⇒ 一方到期把另一方文件删掉
mut_tmp, mut_dst = H.mutated_copy([
    ("        if _ref_ids and len(_ref_ids - {file_id}) > 0:\n", "        if False:\n"),
], prefix="hn_dedup_mut1_")
proc = H.run_probe(mut_dst, r"""
import os
conn = backend.get_db()
d = os.path.join(backend.UPLOAD_DIR, "pdf")
os.makedirs(d, exist_ok=True)
p = os.path.join(d, "legacy_mut.pdf")
open(p, "wb").write(b"%PDF-1.4\nmut\n")
conn.execute(
    "INSERT INTO files (id, original_name, saved_name, path, size, created_at, page_count,"
    " page_count_verified, md5, openid, retention_hours) VALUES"
    " ('M1', 'old.pdf', 'legacy_mut.pdf', ?, 12, '2026-01-01 00:00:00', 1, 1, '', 'u1', 24),"
    " ('M2', 'keep.pdf', 'legacy_mut.pdf', ?, 12, ?, 1, 1, '', 'u2', 168)",
    (p, p, backend.datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
conn.commit(); conn.close()
backend.cleanup_expired_files()
row = backend.get_db().execute("SELECT path FROM files WHERE id='M2'").fetchone()
print("EXISTS=%s" % os.path.exists(p))
print("L2PATH=%s" % ("yes" if (row and row["path"]) else "no"))
""")
H.chk("★ 变异（删掉清理的共享保护）⇒ 物理文件被删、而另一方仍指向它（悬空）",
      H.probe_value(proc, "EXISTS") == "False" and H.probe_value(proc, "L2PATH") == "yes",
      (H.probe_value(proc, "EXISTS"), H.probe_value(proc, "L2PATH")))

# C2：关掉删除接口的共享保护 ⇒ 属主删除把另一方文件删掉
mut_tmp, mut_dst = H.mutated_copy([
    ("        shared = _path_shared_by_others(conn, path, file_id)\n", "        shared = False\n"),
], prefix="hn_dedup_mut2_")
proc = H.run_probe(mut_dst, r"""
import io, os
http = backend.app.test_client()
A1 = {"Authorization": "Bearer " + backend.token_serializer.dumps("u1")}
conn = backend.get_db()
d = os.path.join(backend.UPLOAD_DIR, "pdf")
os.makedirs(d, exist_ok=True)
p = os.path.join(d, "del_mut.pdf")
open(p, "wb").write(b"%PDF-1.4\ndel\n")
for fid, oid in (("X1", "u1"), ("X2", "u2")):
    conn.execute(
        "INSERT INTO files (id, original_name, saved_name, path, size, created_at, page_count,"
        " page_count_verified, md5, openid, retention_hours) VALUES"
        " (?, ?, 'del_mut.pdf', ?, 12, ?, 1, 1, '', ?, 168)",
        (fid, fid + ".pdf", p, backend.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), oid))
conn.commit(); conn.close()
r = http.delete("/api/file/X1", headers=A1)
print("STATUS=%s" % r.status_code)
print("EXISTS=%s" % os.path.exists(p))
""")
H.chk("★ 变异（删掉删除接口的共享保护）⇒ 属主删除连带删掉另一方文件",
      H.probe_value(proc, "STATUS") == "200" and H.probe_value(proc, "EXISTS") == "False",
      (H.probe_value(proc, "STATUS"), H.probe_value(proc, "EXISTS")))

# C3：把 reused 字段加回响应 ⇒ 预言机重新成立
mut_tmp, mut_dst = H.mutated_copy([
    ('            "message": "文件上传成功",\n',
     '            "message": "文件上传成功",\n            "reused": True,\n'),
], prefix="hn_dedup_mut3_")
proc = H.run_probe(mut_dst, r"""
import io
backend.ADMIN_OPENIDS = {"u1"}
http = backend.app.test_client()
A1 = {"Authorization": "Bearer " + backend.token_serializer.dumps("u1")}
r = http.post("/api/upload", data={"file": (io.BytesIO(b"%PDF-1.4\noracle\n"), "o.pdf")},
              headers=A1, content_type="multipart/form-data")
j = r.get_json() or {}
print("STATUS=%s" % r.status_code)
print("HAS_REUSED=%s" % ("reused" in j))
""")
H.chk("★ 变异（响应加回 reused）⇒ 存在性预言机重新成立",
      H.probe_value(proc, "HAS_REUSED") == "True", H.probe_value(proc, "HAS_REUSED"))

H.finish()
