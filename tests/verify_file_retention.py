# -*- coding: utf-8 -*-
"""验收 · S1-03（服务端）客户自选保留期 + 删除我的文件

依据：docs/安全加固修复计划_2026-10-23.md §S1-03（DS-06）+ D-6/D-7 拍板。

现场形状：
  · 保留期只有"全局 days/hours"一个口径，客户无法为自己的文件选更短的保留期；
  · 清理只在写新缓存时被调度（10 分钟定时任务），**进程重启后不跑一次**；
  · 顾客没有任何"删除我的文件"入口（D-7）。

本脚本（服务端部分）：
  A. 静态接线：MAX_RETENTION_HOURS=168、files.retention_hours 迁移、上传接收并夹紧、
     清理按"每个文件的 retention_hours"、启动时也跑一次、删除接口有属主/活跃引用校验、
     保留期口径接口。
  B. 行为：夹紧矩阵（0/24/999/负数/非数字）；上传带 999 → 存 168；到期文件被清空 path；
     **把全局策略设成"永不过期"时，客户自选 24h 的文件仍被清理**（D-6 的核心口径）；
     删除接口：别人删 403 / 本人 200 / 再删 404 / 被排队订单引用 409。
  C. 变异自证：删掉"按文件 TTL"那段 SQL → 过期文件残留、判据转红。

用法：python tests/verify_file_retention.py     （exit 0 = 全部通过）
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


cleanup = block("def cleanup_expired_files(")
del_block = block("def delete_my_file(")

# ============================================================
# A. 静态接线
# ============================================================
print("=== A. 静态接线 ===")
H.chk("硬上限 168h / 默认 24h 常量化（D-6 定稿）",
      "MAX_RETENTION_HOURS = 168" in code and "DEFAULT_FILE_RETENTION_HOURS = 24" in code)
H.chk("files.retention_hours 迁移存在",
      "ALTER TABLE files ADD COLUMN retention_hours INTEGER DEFAULT 0" in code)
H.chk("上传接收 retention_hours 并夹紧",
      '_clamp_retention_hours(request.form.get("retention_hours"))' in code)
H.chk("处理 retention_hours 的 SQL 落库",
      "retention_hours" in code and "_retention_hours)" in code)
H.chk("★ 清理按**每个文件**的 retention_hours（不只是全局 cutoff）",
      "COALESCE(retention_hours, 0) > 0" in cleanup
      and "datetime(created_at, '+' || retention_hours || ' hours')" in cleanup)
H.chk("★ 全局=永不过期时仍执行自选 TTL（早退在自选清理之后）",
      "per_file_rows" in cleanup and
      cleanup.find("per_file_rows = conn.execute(") < cleanup.find("if no_retention:"))
H.chk("启动时也做一次清理（双触发）",
      "def _cleanup_once_on_startup(" in code and "_cleanup_once_on_startup()" in code)
H.chk("保留期口径接口 /api/retention_config",
      '@app.route("/api/retention_config"' in code)
H.chk("删除接口：仅属主", "(row[\"openid\"] or \"\") != g.openid" in del_block)
H.chk("删除接口：被活跃订单引用时拒绝", "status IN ({placeholders}) LIMIT 1" in del_block
      and "409" in del_block)
H.chk("删除接口：删物理文件 + 清 MD5 索引",
      "os.remove(path)" in del_block and "save_md5_index(index)" in del_block)

# ============================================================
# B. 行为
# ============================================================
print("\n=== B. 行为（临时后端副本）===")
TMP, DST = H.make_copy("hn_ret_")
backend = H.import_backend(DST)
backend.init_db()
backend.PRINTER_TOKEN = "ret-tok"
backend.ADMIN_OPENIDS = {"ret-user"}
http = backend.app.test_client()
tok = backend.token_serializer.dumps("ret-user")
A = {"Authorization": "Bearer " + tok}


def _upload(name, retention=None):
    data = {"file": (io.BytesIO(b"%PDF-1.4 fake"), name)}
    if retention is not None:
        data["retention_hours"] = str(retention)
    return http.post("/api/upload", data=data, headers=A, content_type="multipart/form-data")


H.chk("夹紧矩阵：0→0 / 24→24 / 999→168 / -5→0 / abc→0",
      [backend._clamp_retention_hours(v) for v in (0, 24, 999, -5, "abc")]
      == [0, 24, 168, 0, 0],
      [backend._clamp_retention_hours(v) for v in (0, 24, 999, -5, "abc")])

r = http.get("/api/retention_config").get_json() or {}
H.chk("口径接口返回上限/默认/选项",
      r.get("max_retention_hours") == 168 and r.get("default_retention_hours") == 24
      and 168 in (r.get("choices_hours") or []))

r = _upload("a.pdf", retention=999)
fid = (r.get_json() or {}).get("file_id")
conn = backend.get_db()
row = conn.execute("SELECT retention_hours FROM files WHERE id=?", (fid,)).fetchone()
conn.close()
H.chk("上传时 999 被夹到 168 落库", row and row["retention_hours"] == 168,
      row["retention_hours"] if row else None)

# 到期 → path 被清空
conn = backend.get_db()
conn.execute("UPDATE files SET created_at='2026-01-01 00:00:00', retention_hours=24 WHERE id=?", (fid,))
conn.commit()
conn.close()
backend.cleanup_expired_files()
conn = backend.get_db()
row = conn.execute("SELECT path FROM files WHERE id=?", (fid,)).fetchone()
conn.close()
H.chk("自选 24h 且早已创建 → 清理后 path 为空", row is not None and not (row["path"] or ""))

# 全局"永不过期" + 自选 TTL：仍必须清理
cfg = backend.load_retention_config()
cfg["days"], cfg["hours"] = 0, 0
backend.save_retention_config(cfg)
conn = backend.get_db()
conn.execute("UPDATE files SET path='uploads/stale.pdf', created_at='2026-01-01 00:00:00', "
             "retention_hours=24 WHERE id=?", (fid,))
conn.commit()
conn.close()
with open(os.path.join(DST, "uploads", "stale.pdf"), "wb") as f:
    f.write(b"x")
backend.cleanup_expired_files()
conn = backend.get_db()
row = conn.execute("SELECT path FROM files WHERE id=?", (fid,)).fetchone()
conn.close()
H.chk("★ 全局=永不过期时，客户自选 24h 的文件仍被清理（D-6 的核心）",
      row is not None and not (row["path"] or ""))

# 删除接口
r2 = _upload("b.pdf")
fid2 = (r2.get_json() or {}).get("file_id")
other = {"Authorization": "Bearer " + backend.token_serializer.dumps("ret-other")}
H.chk("别人删我的文件 → 403", http.delete("/api/file/" + fid2, headers=other).status_code == 403)
H.chk("本人删 → 200", http.delete("/api/file/" + fid2, headers=A).status_code == 200)
H.chk("删完再删 → 404", http.delete("/api/file/" + fid2, headers=A).status_code == 404)

r3 = _upload("c.pdf")
fid3 = (r3.get_json() or {}).get("file_id")
conn = backend.get_db()
conn.execute("INSERT INTO orders (id, file, copies, status, created_at, openid, schedule_mode, "
             "schedule_frozen, scheduled_at, target_client) VALUES "
             "(9601, 'c.pdf', 1, 'queued', '2026-10-23 10:00:00', 'ret-user', 'now', 0, '', '')")
conn.execute("INSERT INTO order_files (id, order_id, file_id, file_name, copies, page_count, "
             "status, created_at) VALUES (7401, 9601, ?, 'c.pdf', 1, 1, 'queued', "
             "'2026-10-23 10:00:00')", (fid3,))
conn.commit()
conn.close()
H.chk("被排队订单引用的文件 → 409（不能删正在用的）",
      http.delete("/api/file/" + fid3, headers=A).status_code == 409)

# ============================================================
# C. 变异自证：删掉"按文件 TTL"清理
# ============================================================
print("\n=== C. 变异自证（删掉按文件 TTL 的清理 SQL）===")
mut_tmp, mut_dst = H.mutated_copy([
    ('''        \"\"\"SELECT id, path, retention_hours FROM files
            WHERE path != '' AND COALESCE(retention_hours, 0) > 0
              AND datetime(created_at, '+' || retention_hours || ' hours')
                  <= datetime('now', 'localtime')\"\"\"''',
     '''        \"\"\"SELECT id, path, retention_hours FROM files WHERE 0\"\"\"'''),
], prefix="hn_ret_mut_")
proc = H.run_probe(mut_dst, """
# 全局策略设成"永不过期"，否则全局 cutoff 也会把这个老文件删掉、看不出差异
_cfg = backend.load_retention_config(); _cfg["days"], _cfg["hours"] = 0, 0
backend.save_retention_config(_cfg)
conn = backend.get_db()
conn.execute("INSERT INTO files (id, original_name, saved_name, path, size, openid, page_count, "
             "page_count_verified, created_at, retention_hours) VALUES "
             "('f-mut','m.pdf','m.pdf','uploads/m.pdf',1,'u',1,1,'2026-01-01 00:00:00',24)")
conn.commit(); conn.close()
open("uploads/m.pdf","wb").write(b"x")
backend.cleanup_expired_files()
conn = backend.get_db()
row = conn.execute("SELECT path FROM files WHERE id='f-mut'").fetchone()
conn.close()
print("RESULT=%r" % (row["path"] if row else None))
""")
mut_val = H.probe_value(proc)
H.chk("删掉按文件 TTL 的清理后，过期文件仍留在盘上（判据能红）",
      mut_val not in (None, "''", '""') and mut_val != "''",
      "变异副本 path=%r stderr=%r" % (mut_val, (proc.stderr or "")[-200:]))

H.finish()
