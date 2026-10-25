# -*- coding: utf-8 -*-
"""验收 · S1-03 客户自选保留期 + 删除我的文件 + 本地副本生命周期

依据：docs/安全加固修复计划_2026-10-23.md §S1-03（DS-06）+ D-6/D-7 拍板。

现场形状：
  · 保留期只有"全局 days/hours"一个口径，客户无法为自己的文件选更短的保留期；
  · 清理只在写新缓存时被调度（10 分钟定时任务），**进程重启后不跑一次**；
  · 顾客没有任何"删除我的文件"入口（D-7）；
  · 店铺机本地副本（file_cache / pdf_cache / 攒批 .keep / 桌面副本）没有 TTL 与清理者。

本脚本：
  A–C. 服务端：静态接线 / 夹紧与删除接口行为 / 变异自证（删掉按文件 TTL 的 SQL ⇒ 转红）。
  D–F. **客户端（S1-03 客户端半）**：
       D. 静态接线：retention.py 常量、GUI 启动扫描与 keep_temp_pdf 警告、
          cloud_client 默认 24h + 启动扫描 + 下载写保留期侧车、后端两通道都下发 retention_hours；
       E. 行为（真实 retention 模块 + 合成目录）：TTL 前后、引用保护、侧车优先、
          **只写不读的启动扫描也清理**、.keep 30 天过期、pdf_cache 过期/孤儿回收、夹紧矩阵；
       F. 变异自证：把 TTL 判定删掉 ⇒ 不再清理；把侧车优先级删掉 ⇒ 单文件保留期失效。

用法：python tests/verify_file_retention.py     （exit 0 = 全部通过）
"""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL_TOOL = os.path.join(REPO, "local_print_tool")
sys.path.insert(0, LOCAL_TOOL)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _sec_harness as H  # noqa: E402
import retention as R  # noqa: E402  （客户端半：本地副本生命周期管理器）

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
H.chk("硬上限 168h / 服务端默认 0h / 立即删除 -1 常量化（D-6 定稿）",
      "MAX_RETENTION_HOURS = 168" in code
      and "DEFAULT_FILE_RETENTION_HOURS = 0" in code
      and "IMMEDIATE_DELETE_RETENTION_HOURS = -1" in code)
H.chk("files.retention_hours 迁移存在",
      "ALTER TABLE files ADD COLUMN retention_hours INTEGER DEFAULT 0" in code)
H.chk("上传接收 retention_hours 并夹紧",
      '_clamp_retention_hours(request.form.get("retention_hours"))' in code)
H.chk("处理 retention_hours 的 SQL 落库",
      "retention_hours" in code and "_retention_hours)" in code)
H.chk("★ 清理按**每个文件**的 retention_hours（不只是全局 cutoff）",
      "COALESCE(retention_hours, 0) > 0" in cleanup
      and "datetime(created_at, '+' || retention_hours || ' hours')" in cleanup)
H.chk("★ 打印成功调用“立即删除”定向清理",
      '_purge_file_after_print(row["file_id"])' in code)
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


H.chk("夹紧矩阵：-1→立即 / 0→服务器 / 24→24 / 999→168 / -5→0 / abc→0",
      [backend._clamp_retention_hours(v) for v in (-1, 0, 24, 999, -5, "abc")]
      == [-1, 0, 24, 168, 0, 0],
      [backend._clamp_retention_hours(v) for v in (-1, 0, 24, 999, -5, "abc")])

r = http.get("/api/retention_config").get_json() or {}
H.chk("口径接口返回两种模式 + 服务器当前 7 天设置",
      r.get("max_retention_hours") == 168
      and r.get("default_retention_hours") == 0
      and r.get("choices_hours") == [-1, 0]
      and r.get("server_retention_hours") == 168)

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

# 立即删除：活跃订单期间保留，打印成功后的定向清理才删除物理文件
r4 = _upload("immediate.pdf", retention=-1)
fid4 = (r4.get_json() or {}).get("file_id")
conn = backend.get_db()
f4 = conn.execute("SELECT path, retention_hours FROM files WHERE id=?", (fid4,)).fetchone()
conn.execute("INSERT INTO orders (id, file, copies, status, created_at, openid) "
             "VALUES (9602, 'immediate.pdf', 1, 'queued', '2026-10-25 10:00:00', 'ret-user')")
conn.execute("INSERT INTO order_files (id, order_id, file_id, file_name, copies, page_count, "
             "status, created_at) VALUES (7402, 9602, ?, 'immediate.pdf', 1, 1, 'queued', "
             "'2026-10-25 10:00:00')", (fid4,))
conn.commit()
conn.close()
backend.cleanup_expired_files()
H.chk("★ 立即删除文件在订单活跃期间仍保留",
      f4["retention_hours"] == -1 and os.path.exists(f4["path"]))
conn = backend.get_db()
conn.execute("UPDATE order_files SET status='sent' WHERE id=7402")
conn.execute("UPDATE orders SET status='sent' WHERE id=9602")
conn.commit()
conn.close()
H.chk("打印完成后定向立即删除成功", backend._purge_file_after_print(fid4) is True)
conn = backend.get_db()
f4_after = conn.execute("SELECT path, size FROM files WHERE id=?", (fid4,)).fetchone()
conn.close()
H.chk("★ 打印完成后物理文件立即删除且 files.path 清空",
      not os.path.exists(f4["path"]) and not (f4_after["path"] or "") and f4_after["size"] == 0)

# 服务器管理（0）不能被“立即删除”逻辑误伤
r_server = _upload("server-managed.pdf", retention=0)
fid_server = (r_server.get_json() or {}).get("file_id")
conn = backend.get_db()
f_server = conn.execute("SELECT path FROM files WHERE id=?", (fid_server,)).fetchone()
conn.close()
H.chk("★ 服务器管理模式不会走立即删除",
      backend._purge_file_after_print(fid_server) is False
      and os.path.exists(f_server["path"]))

# 定时清理兜底：print_success 后的定向清理若未执行，下一轮也能收敛
r5 = _upload("immediate-fallback.pdf", retention=-1)
fid5 = (r5.get_json() or {}).get("file_id")
conn = backend.get_db()
f5 = conn.execute("SELECT path FROM files WHERE id=?", (fid5,)).fetchone()
conn.execute("INSERT INTO orders (id, file, copies, status, created_at, openid) "
             "VALUES (9603, 'immediate-fallback.pdf', 1, 'sent', '2026-10-25 10:00:00', 'ret-user')")
conn.execute("INSERT INTO order_files (id, order_id, file_id, file_name, copies, page_count, "
             "status, created_at) VALUES (7403, 9603, ?, 'immediate-fallback.pdf', 1, 1, 'sent', "
             "'2026-10-25 10:00:00')", (fid5,))
conn.commit()
conn.close()
backend.cleanup_expired_files()
conn = backend.get_db()
f5_after = conn.execute("SELECT path FROM files WHERE id=?", (fid5,)).fetchone()
conn.close()
H.chk("★ 定时清理兜底也会删除已完成订单的“立即删除”文件",
      not os.path.exists(f5["path"]) and f5_after is not None and not (f5_after["path"] or ""))

# 真实 SocketIO print_success：归属设备回报成功后立即触发清理（不是只测辅助函数）
r6 = _upload("immediate-socket.pdf", retention=-1)
fid6 = (r6.get_json() or {}).get("file_id")
conn = backend.get_db()
f6 = conn.execute("SELECT path FROM files WHERE id=?", (fid6,)).fetchone()
conn.execute("INSERT INTO orders (id, file, copies, status, created_at, openid) "
             "VALUES (9604, 'immediate-socket.pdf', 1, 'printing', '2026-10-25 10:00:00', 'ret-user')")
conn.execute("INSERT INTO order_files (id, order_id, file_id, file_name, copies, page_count, "
             "status, created_at, operator_client, last_operator_client) "
             "VALUES (7404, 9604, ?, 'immediate-socket.pdf', 1, 1, 'printing', "
             "'2026-10-25 10:00:00', 'ret-device', 'ret-device')", (fid6,))
conn.commit()
conn.close()
sio_client = backend.socketio.test_client(
    backend.app, query_string="?token=ret-tok&client_id=ret-device")
H.chk("立即删除验收：SocketIO 设备已连接", sio_client.is_connected())
sio_client.emit("print_success", {"task_id": 7404, "copies": 1, "duplex": "off", "page_count": 1})
time.sleep(0.05)
sio_client.get_received()
conn = backend.get_db()
sent6 = conn.execute("SELECT status FROM order_files WHERE id=7404").fetchone()
f6_after = conn.execute("SELECT path, size FROM files WHERE id=?", (fid6,)).fetchone()
conn.close()
sio_client.disconnect()
H.chk("★ 真实 print_success 回报后立即删除（同一事件链路）",
      sent6 and sent6["status"] == "sent"
      and f6_after is not None and not (f6_after["path"] or "")
      and not os.path.exists(f6["path"]),
      (sent6["status"] if sent6 else None,
       f6_after["path"] if f6_after else None,
       os.path.exists(f6["path"])))

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

# ============================================================
# D. 客户端静态接线（S1-03 客户端半）
# ============================================================
print("\n=== D. 客户端静态接线（retention.py / GUI 启动扫描 / cloud_client / 后端下发）===")

RET_PATH = os.path.join(LOCAL_TOOL, "retention.py")
GUI_PATH = os.path.join(LOCAL_TOOL, "gui.py")
CC_PATH = os.path.join(LOCAL_TOOL, "cloud_client.py")


def _read(path):
    with open(path, "r", encoding="utf-8", newline="") as f:
        return f.read()


ret_src = _read(RET_PATH)
gui_src = _read(GUI_PATH)
cc_src = _read(CC_PATH)
app_src = H.read_app_source()

H.chk("客户端保留期管理器 retention.py 存在",
      os.path.isfile(RET_PATH))
H.chk("上限 168h / 默认 24h / .keep 30 天 常量化",
      "MAX_RETENTION_HOURS = 168" in ret_src
      and "DEFAULT_RETENTION_HOURS = 24" in ret_src
      and "KEEP_MARKER_MAX_DAYS = 30" in ret_src)
H.chk("★ GUI 启动扫描接入（keep_paths + 攒批 .keep + 服务端口径）",
      "retention.startup_sweep(" in gui_src
      and "keep_paths=self._staged_reference_paths()" in gui_src
      and "stack_root=paths.duplex_stack_dir()" in gui_src
      and "self._cloud_client.cache_retention_hours" in gui_src)
H.chk("GUI：云端未启用时也扫 PDF 缓存（不只靠 CloudClient.start）",
      "retention.sweep_pdf_cache_dir(" in gui_src and "paths.pdf_cache_dir()" in gui_src)
H.chk("★ keep_temp_pdf 有显式警告 + 开启前确认 + 取消回滚",
      "retention.KEEP_TEMP_WARNING" in gui_src
      and "QMessageBox.question(" in gui_src
      and "self._keep_temp_check.blockSignals(True)" in gui_src
      and "self._keep_temp_check.setCurrentIndex(0)" in gui_src)
H.chk("cloud_client 默认保留期 = 24h（旧 168h 硬编码已换口径）",
      "_CACHE_RETENTION_HOURS = retention.DEFAULT_RETENTION_HOURS" in cc_src)
H.chk("★ cloud_client 启动扫描（进程重启后也跑一次）",
      "def cleanup_caches_on_startup(" in cc_src
      and "self.cleanup_caches_on_startup()" in cc_src)
H.chk("服务端推送的保留期也被夹紧（0/超限都归一）",
      "retention.effective_hours(int(days) * 24 + int(hours))" in cc_src)
H.chk("PDF 缓存清理复用统一口径 + 不再有 0=永不过期早退",
      "retention.sweep_pdf_cache(" in cc_src
      and "_CACHE_RETENTION_HOURS <= 0" not in cc_src)
H.chk("云端下载落地时写保留期侧车（该文件的 retention_hours）",
      "retention.write_sidecar(dest, task.retention_hours)" in cc_src)
H.chk("CloudTask 解析并透传 retention_hours",
      '"retention_hours"' in cc_src and "self.retention_hours" in cc_src)
H.chk("★ 后端 pull/push 两通道都随任务下发 retention_hours",
      "f.retention_hours as retention_hours" in app_src
      and 'task_msg["retention_hours"]' in app_src
      and '"retention_hours": int(task.get("retention_hours") or 0)' in app_src)

# ============================================================
# E. 客户端行为（真实 retention 模块 + 合成目录，全部只写不读）
# ============================================================
print("\n=== E. 客户端行为（真实 retention + 合成目录）===")

_CLIENT_TMPS: list[str] = []


def _mkroot(prefix="hn_ret_c_"):
    d = tempfile.mkdtemp(prefix=prefix)
    _CLIENT_TMPS.append(d)
    return d


def _mkfile(path, age_hours, data=b"x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)
    t = time.time() - age_hours * 3600
    os.utime(path, (t, t))
    return path


def _created(hours_ago):
    return time.strftime("%Y-%m-%d %H:%M:%S",
                         time.localtime(time.time() - hours_ago * 3600))


# 夹紧矩阵
_matrix = [0, -5, None, "abc", 24, 999, 1, 168, "48"]
H.chk("★ 夹紧矩阵：0/-5/None/abc→24；999→168；1→1；168→168；'48'→48",
      [R.clamp_hours(v) for v in _matrix] == [24, 24, 24, 24, 24, 168, 1, 168, 48],
      [R.clamp_hours(v) for v in _matrix])
H.chk("effective_hours 与 clamp_hours 同口径（服务端 0 = 未指定 → 24h，不再永不过期）",
      R.effective_hours(0) == 24 and R.effective_hours(720) == 168)

# file_cache TTL：30h 删、20h 留
root = _mkroot()
old = _mkfile(os.path.join(root, "hash1", "old.pdf"), 30)
new = _mkfile(os.path.join(root, "hash2", "new.pdf"), 20)
res = R.sweep_file_cache(root, hours=24)
H.chk("★ 启动扫描按 TTL 清理（30h 前删、20h 内留），不依赖任何事件",
      res.removed == 1 and not os.path.exists(old) and os.path.exists(new),
      "removed=%d old=%s new=%s" % (res.removed, os.path.exists(old), os.path.exists(new)))

# 引用保护：超期但仍在任务里 → 不删、只计数预警
kept = _mkfile(os.path.join(root, "hash3", "kept.pdf"), 40)
res = R.sweep_file_cache(root, keep_paths=[kept], hours=24)
H.chk("★ 仍被任务引用的超期副本不删（打印/预约还要用），只计入 skipped_referenced",
      res.skipped_referenced == 1 and os.path.exists(kept),
      "skipped=%d exists=%s" % (res.skipped_referenced, os.path.exists(kept)))

# 侧车优先：cloud_<task_id> 目录里该文件的 retention_hours 覆盖全局
c1 = _mkfile(os.path.join(root, "cloud_7", "a.pdf"), 3)
H.chk("写侧车成功（只存小时数）", R.write_sidecar(c1, 1) and R.read_sidecar(os.path.dirname(c1)) == 1)
res = R.sweep_file_cache(root, hours=24)
H.chk("★ 侧车 1h 优先于全局 24h（3h 前的云端文件被删）",
      not os.path.exists(c1), "exists=%s" % os.path.exists(c1))
c2 = _mkfile(os.path.join(root, "cloud_8", "b.pdf"), 3)
R.write_sidecar(c2, 8)
res = R.sweep_file_cache(root, hours=24)
H.chk("★ 侧车 8h 时同一全局口径下 3h 文件保留（按文件而非全局）",
      os.path.exists(c2), "exists=%s" % os.path.exists(c2))

# .part 半截残留：超过 1h 收掉
part = _mkfile(os.path.join(root, "hash4", "x.pdf.part"), 2)
R.sweep_file_cache(root, hours=24)
H.chk("写了一半的 .part 残留超过 1h 被回收", not os.path.exists(part))

# 只写不读的启动扫描：file_cache + .keep 一次跑完
root2 = _mkroot()
x1 = _mkfile(os.path.join(root2, "h1", "x.pdf"), 50)
x2 = _mkfile(os.path.join(root2, "cloud_9", "y.pdf"), 50)
stack = _mkroot("hn_ret_stack_")
old_keep = _mkfile(os.path.join(stack, "batchOld", ".keep"), 31 * 24)
new_keep = _mkfile(os.path.join(stack, "batchNew", ".keep"), 5 * 24)
rep = R.startup_sweep(root2, keep_paths=(), stack_root=stack, hours=24)
H.chk("★ startup_sweep 只写不读也清理（2 个超期文件）",
      rep["file_cache"].removed == 2 and not os.path.exists(x1) and not os.path.exists(x2),
      "removed=%d" % rep["file_cache"].removed)
H.chk("★ 攒批 .keep 超过 30 天自动解除，未超期的不动",
      rep["keep_expired"] == ["batchOld"]
      and not os.path.exists(old_keep) and os.path.exists(new_keep),
      rep["keep_expired"])

# pdf_cache：过期条目 + 孤儿回收（可重建，删了打印时自动重转）
pdf_dir = _mkroot("hn_ret_pdf_")
idx = {
    "oldkey": {"created_at": _created(30)},
    "newkey": {"created_at": _created(2)},
    "badkey": {"created_at": "garbage"},
}
_mkfile(os.path.join(pdf_dir, "oldkey.pdf"), 30)
_mkfile(os.path.join(pdf_dir, "newkey.pdf"), 2)
_mkfile(os.path.join(pdf_dir, "badkey.pdf"), 2)
_mkfile(os.path.join(pdf_dir, "orphan_old.pdf"), 40)
_mkfile(os.path.join(pdf_dir, "orphan_fresh.pdf"), 1)
removed, idx2, freed = R.sweep_pdf_cache(pdf_dir, idx, hours=24)
H.chk("★ PDF 缓存过期条目连同文件一起删、新条目保留、无法解析时间的条目不动",
      removed == 2 and "oldkey" not in idx2 and "newkey" in idx2 and "badkey" in idx2
      and not os.path.exists(os.path.join(pdf_dir, "oldkey.pdf"))
      and os.path.exists(os.path.join(pdf_dir, "newkey.pdf")),
      "removed=%d keys=%s" % (removed, sorted(idx2)))
H.chk("无索引孤儿 .pdf 按 mtime 回收（旧的删、新的留）",
      not os.path.exists(os.path.join(pdf_dir, "orphan_old.pdf"))
      and os.path.exists(os.path.join(pdf_dir, "orphan_fresh.pdf")))

# 独立入口（云端未启用）：自己读/写 index.json
pdf_dir2 = _mkroot("hn_ret_pdf2_")
_mkfile(os.path.join(pdf_dir2, "k1.pdf"), 30)
_mkfile(os.path.join(pdf_dir2, "k2.pdf"), 2)
with open(os.path.join(pdf_dir2, "index.json"), "w", encoding="utf-8") as f:
    json.dump({"k1": {"created_at": _created(30)}, "k2": {"created_at": _created(2)}}, f)
rep_pdf = R.sweep_pdf_cache_dir(pdf_dir2, hours=24)
with open(os.path.join(pdf_dir2, "index.json"), "r", encoding="utf-8") as f:
    idx_after = json.load(f)
H.chk("独立入口 sweep_pdf_cache_dir 清理并回写 index.json",
      rep_pdf.removed == 1 and "k1" not in idx_after and "k2" in idx_after,
      "removed=%d keys=%s" % (rep_pdf.removed, sorted(idx_after)))

H.chk("keep_temp_pdf 警告文案点明「桌面 / 全文 / 不受清理保护」",
      "桌面" in R.KEEP_TEMP_WARNING and "全文" in R.KEEP_TEMP_WARNING
      and "清理" in R.KEEP_TEMP_WARNING)


# ============================================================
# F. 客户端变异自证（判据能红）
# ============================================================
print("\n=== F. 客户端变异自证（TTL 判定 / 侧车优先级）===")


def _run_module_probe(module_dir, body):
    """在子进程里 import 指定目录下的 retention 模块（变异副本必须换进程）。"""
    probe = os.path.join(module_dir, "_probe.py")
    with open(probe, "w", encoding="utf-8") as f:
        f.write(
            "# -*- coding: utf-8 -*-\n"
            "import io, os, sys, tempfile, time\n"
            "sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')\n"
            "sys.path.insert(0, %r)\n"
            "import retention as R\n" % module_dir + body)
    env = dict(os.environ)
    env["PYTHONPATH"] = ""
    env["PYTHONUTF8"] = "1"
    return subprocess.run([sys.executable, probe], cwd=module_dir, env=env,
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=120)


def _mutated_retention(pairs):
    d = _mkroot("hn_ret_mut_")
    src = _read(RET_PATH)
    for old, new in pairs:
        if old not in src:
            raise AssertionError("变异锚点未命中: %r" % old[:80])
        src = src.replace(old, new, 1)
    with open(os.path.join(d, "retention.py"), "w", encoding="utf-8") as f:
        f.write(src)
    return d


def _probe_nums(proc):
    out = {}
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if "=" in line:
            k, v = line.split("=", 1)
            if k.isupper():
                out[k] = v
    return out


# 变异 1：删掉 TTL 判定（退回"永不按时间清理"）
mut1 = _mutated_retention([
    ("    if age <= ttl:\n        return\n",
     "    if True:  # 变异：取消 TTL 判定\n        return\n"),
])
p1 = _run_module_probe(mut1, """
root = tempfile.mkdtemp()
p = os.path.join(root, "h1", "old.pdf")
os.makedirs(os.path.dirname(p))
open(p, "wb").write(b"x")
t = time.time() - 30 * 3600
os.utime(p, (t, t))
res = R.sweep_file_cache(root, hours=24)
print("REMOVED=%d" % res.removed)
print("EXISTS=%d" % int(os.path.exists(p)))
""")
v1 = _probe_nums(p1)
H.chk("删掉 TTL 判定后，30h 前的副本不再被清（E 的判据能红）",
      v1.get("REMOVED") == "0" and v1.get("EXISTS") == "1",
      "probe=%r stderr=%r" % (v1, (p1.stderr or "")[-200:]))

# 变异 2：侧车优先级删掉（按全局 TTL 清）
mut2 = _mutated_retention([
    ("        dir_hours = read_sidecar(top) or hours\n",
     "        dir_hours = hours\n"),
])
p2 = _run_module_probe(mut2, """
root = tempfile.mkdtemp()
d = os.path.join(root, "cloud_5")
os.makedirs(d)
p = os.path.join(d, "a.pdf")
open(p, "wb").write(b"x")
t = time.time() - 3 * 3600
os.utime(p, (t, t))
R.write_sidecar(p, 1)
res = R.sweep_file_cache(root, hours=24)
print("REMOVED=%d" % res.removed)
print("EXISTS=%d" % int(os.path.exists(p)))
""")
v2 = _probe_nums(p2)
H.chk("删掉侧车优先级后，按文件保留期失效（3h 文件在全局 24h 下被留下 ⇒ 判据能红）",
      v2.get("REMOVED") == "0" and v2.get("EXISTS") == "1",
      "probe=%r stderr=%r" % (v2, (p2.stderr or "")[-200:]))

for _d in _CLIENT_TMPS:
    shutil.rmtree(_d, ignore_errors=True)

H.finish()
