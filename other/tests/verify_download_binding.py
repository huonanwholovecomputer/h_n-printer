# -*- coding: utf-8 -*-
"""验收 · S1-06b 下载 URL 绑定（openid + file_id + task_id）与设备密钥

依据：docs/安全加固修复计划_2026-10-23.md §S1-06（DS-11）。

现场形状：
  · 下载 URL 的签名 payload **只有 file_id** ⇒ 任何拿到 URL 的人（日志、截图、转发）
    都能下这个文件；URL 与"谁的任务"无关，1 小时内有效。
  · `device_login` 只凭自报的 `device_id` 就签发 token ⇒ 猜到一个 device_id 就能拿到
    该设备身份（APP 侧的 guest token）。

本脚本：
  A. 静态接线：payload 带 openid/task_id、下载侧校验 order_files⋈orders.openid、
     设备密钥落盘（哈希 + 原子写）、宽限期常量、错误密钥拒绝。
  B. 行为：绑定 URL 正常下载；绑定到别人 openid → 403；绑定到不存在的 task → 403；
     未绑定（分析请求）仍可下载；篡改 token → 403。
     设备密钥：首次下发 → 带正确密钥 200 / 错误 403 / 宽限期内不带密钥轮换下发 /
     宽限期后不带密钥 403。
  C. 变异自证：去掉 openid 校验 → 别人的绑定 URL 也能下载（判据转红）。

用法：python tests/verify_download_binding.py     （exit 0 = 全部通过）
"""
import io
import os
import sys
import time

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


dl_block = block("def download_file(")
mk_block = block("def make_download_url(")

# ============================================================
# A. 静态接线
# ============================================================
print("=== A. 静态接线 ===")
H.chk("make_download_url 接受 openid / task_id",
      "def make_download_url(file_id, openid=\"\", task_id=None)" in code)
H.chk("payload 写入 file_id / openid / task_id",
      'payload = {"f": file_id, "o": openid or ""}' in code and 'payload["t"] = int(task_id)' in code)
H.chk("下载侧校验任务归属（order_files ⋈ orders.openid）",
      "SELECT order_id FROM order_files WHERE id = ? AND file_id = ?" in dl_block
      and "SELECT openid FROM orders WHERE id = ?" in dl_block)
H.chk("两种 payload 形态都处理（dict 新 / str 旧）",
      "if isinstance(payload, dict):" in dl_block and "elif isinstance(payload, str):" in dl_block)
H.chk("设备密钥：哈希落盘 + 原子写 + 锁",
      "DEVICE_SECRETS_FILE" in code and "def _hash_device_secret(" in code
      and "os.replace(tmp, DEVICE_SECRETS_FILE)" in code and "_device_secrets_lock" in code)
H.chk("设备密钥：宽限期常量存在且 > 0",
      "DEVICE_SECRET_GRACE_SECONDS = 14 * 24 * 3600" in code)
H.chk("device_login 调用密钥校验（不再只凭自报 device_id）",
      "_check_device_secret(" in block("def device_login("))
H.chk("只下发 secret 的哈希（明文只在响应里出现一次）",
      '"secret_hash": _hash_device_secret(' in code and '"secret": new_secret' not in code)

# ============================================================
# B. 行为
# ============================================================
print("\n=== B. 行为（临时后端副本）===")
TMP, DST = H.make_copy("hn_dlbind_")
backend = H.import_backend(DST)
backend.init_db()
http = backend.app.test_client()

conn = backend.get_db()
conn.execute("INSERT INTO orders (id, file, copies, status, created_at, openid, schedule_mode, "
             "schedule_frozen, scheduled_at, target_client) VALUES "
             "(9801, 'a.pdf', 1, 'queued', '2026-10-23 10:00:00', 'owner-a', 'now', 0, '', '')")
conn.execute("INSERT INTO files (id, original_name, saved_name, path, size, openid, page_count, "
             "page_count_verified, created_at) VALUES "
             "('f-9801', 'a.pdf', 'f-9801.pdf', 'uploads/f-9801.pdf', 10, 'owner-a', 1, 1, "
             "'2026-10-23 10:00:00')")
conn.execute("INSERT INTO order_files (id, order_id, file_id, file_name, copies, page_count, status, "
             "created_at) VALUES (7601, 9801, 'f-9801', 'a.pdf', 1, 1, 'queued', '2026-10-23 10:00:00')")
conn.commit()
conn.close()
with open(os.path.join(DST, "uploads", "f-9801.pdf"), "wb") as f:
    f.write(b"%PDF-1.4 fake")


def hit(url):
    return http.get(url.split(backend.PUBLIC_BASE_URL)[1]).status_code


H.chk("绑定 URL（正确 openid + task）可下载", hit(
    backend.make_download_url("f-9801", openid="owner-a", task_id=7601)) == 200)
H.chk("★ 绑定到别人的 openid → 403（泄露的 URL 不能跨人用）", hit(
    backend.make_download_url("f-9801", openid="owner-b", task_id=7601)) == 403)
H.chk("绑定到不存在的 task → 403", hit(
    backend.make_download_url("f-9801", openid="owner-a", task_id=7602)) == 403)
H.chk("task 与 file 不匹配 → 403", hit(
    backend.make_download_url("f-9801", openid="owner-a", task_id=7603)) == 403)
H.chk("未绑定（分析请求场景）仍可下载", hit(
    backend.make_download_url("f-9801")) == 200)
H.chk("file_id 与 URL 路径不一致 → 403", hit(
    backend.make_download_url("f-9801", openid="owner-a").replace("/f-9801?", "/f-9999?")) == 403)
H.chk("篡改 token → 403", hit(
    backend.make_download_url("f-9801", openid="owner-a")[:-4] + "zzzz") == 403)

# 设备密钥
DEV = "bind-probe-device-01"
r = http.post("/api/device_login", json={"device_id": DEV})
j = r.get_json() or {}
sec = j.get("device_secret")
H.chk("首次 device_login 下发 device_secret", r.status_code == 200 and bool(sec), j.keys())
H.chk("带正确 device_secret → 200", http.post(
    "/api/device_login", json={"device_id": DEV, "device_secret": sec}).status_code == 200)
H.chk("带错误 device_secret → 403", http.post(
    "/api/device_login", json={"device_id": DEV, "device_secret": "nope"}).status_code == 403)
r = http.post("/api/device_login", json={"device_id": DEV})
H.chk("宽限期内不带 secret → 200 且轮换下发新 secret",
      r.status_code == 200 and bool((r.get_json() or {}).get("device_secret")))
new_sec = (r.get_json() or {}).get("device_secret")
H.chk("轮换后旧 secret 失效、新 secret 可用",
      http.post("/api/device_login", json={"device_id": DEV, "device_secret": sec}).status_code == 403
      and http.post("/api/device_login",
                    json={"device_id": DEV, "device_secret": new_sec}).status_code == 200)

# 宽限期外：不带 secret 必须拒绝
smap = backend._load_device_secrets()
for k in smap:
    smap[k]["issued_at_ts"] = time.time() - backend.DEVICE_SECRET_GRACE_SECONDS - 60
backend._save_device_secrets(smap)
r = http.post("/api/device_login", json={"device_id": DEV})
H.chk("宽限期后不带 secret → 403（设备凭证成为硬要求）", r.status_code == 403,
      r.get_json())

# ============================================================
# C. 变异自证：去掉 openid 绑定校验
# ============================================================
print("\n=== C. 变异自证（去掉 openid 绑定校验）===")
mut_tmp, mut_dst = H.mutated_copy([
    ('                if not orow or (orow["openid"] or "") != bound_openid:',
     '                if False:'),
], prefix="hn_dlbind_mut_")
proc = H.run_probe(mut_dst, """
conn = backend.get_db()
conn.execute("INSERT INTO orders (id, file, copies, status, created_at, openid, schedule_mode, schedule_frozen, scheduled_at, target_client) VALUES (9801,'a.pdf',1,'queued','2026-10-23 10:00:00','owner-a','now',0,'','')")
conn.execute("INSERT INTO files (id, original_name, saved_name, path, size, openid, page_count, page_count_verified, created_at) VALUES ('f-9801','a.pdf','f-9801.pdf','uploads/f-9801.pdf',10,'owner-a',1,1,'2026-10-23 10:00:00')")
conn.execute("INSERT INTO order_files (id, order_id, file_id, file_name, copies, page_count, status, created_at) VALUES (7601,9801,'f-9801','a.pdf',1,1,'queued','2026-10-23 10:00:00')")
conn.commit(); conn.close()
open("uploads/f-9801.pdf","wb").write(b"%PDF-1.4 fake")
url = backend.make_download_url("f-9801", openid="owner-b", task_id=7601)
print("RESULT=%s" % http.get(url.split(backend.PUBLIC_BASE_URL)[1]).status_code)
""")
mut_status = H.probe_value(proc)
H.chk("去掉 openid 校验后，别人的绑定 URL 也能下载（判据能红）",
      mut_status == "200", "变异副本 status=%r stderr=%r"
      % (mut_status, (proc.stderr or "")[-300:]))

H.finish()
