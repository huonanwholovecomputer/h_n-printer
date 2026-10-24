# -*- coding: utf-8 -*-
"""验收 · S2-08 隐私告知与删除权（DS-19 / D-6 / D-7）

依据：docs/安全加固修复计划_2026-10-23.md §S2-08。

现场形状（修复前）：
  · 仓库内**没有任何隐私告知**：顾客上传证件/简历类高敏文件，界面不说用途、不说保留多久、
    不说第三方（微信登录 / 腾讯云 COS 分发）；也没有"我同意"这一步；
  · 保留期只有管理员侧全局设置，顾客**无法自选**，也不存在"删除我的文件"入口
    （S1-03 服务端 `DELETE /api/file/<id>` 已就绪，但移动端没有任何地方调用它）。

本脚本：
  A. 静态（两端 + 后端）：告知文案（用途/保留期/第三方）、逐单勾选闸门、保留期选择器与
     上传/提交两处 payload、提交成功后复位勾选、两端「我的文件」入口与删除接线、
     后端 `/api/my_files` 与提交时改写 retention_hours。
  B. 行为（真 Flask + 真 SQLite）：
     · 上传带 retention_hours=true 值入库；提交时以**提交时**的选择为准（999→168 夹紧；
       0/非法 = 不动）；只改自己名下的文件；
     · `/api/my_files` 只回自己的文件、未登录 401；含 retention_hours / auto_delete_at /
       in_active_order；全局设成永不过期时 auto_delete_at 为空；
     · 被排队订单引用的文件 in_active_order=True 且 DELETE 409；订单结束后可删，删完列表不再出现。
  C. 变异自证：去掉勾选闸门 / 去掉提交 payload 的 retention_hours / 去掉移动端 DELETE 调用 /
     去掉后端"提交时改写保留期" —— 对应判据必须变红。

用法：python tests/verify_privacy_notice.py     （exit 0 = 全部通过）
"""
import io
import os
import re
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _sec_harness as H  # noqa: E402

H.install_cleanup_hook()

REPO = H.REPO
P_MP_JS = os.path.join(REPO, "mobile_apps", "h_n_print", "pages", "index", "index.js")
P_MP_WXML = os.path.join(REPO, "mobile_apps", "h_n_print", "pages", "index", "index.wxml")
P_MP_ME_JS = os.path.join(REPO, "mobile_apps", "h_n_print", "pages", "me", "me.js")
P_MP_ME_WXML = os.path.join(REPO, "mobile_apps", "h_n_print", "pages", "me", "me.wxml")
P_APP_HTML = os.path.join(REPO, "mobile_apps", "android_app", "www", "index.html")
P_APP_JS = os.path.join(REPO, "mobile_apps", "android_app", "www", "print.js")
P_APP_ME_JS = os.path.join(REPO, "mobile_apps", "android_app", "www", "me.js")


def read(path):
    with io.open(path, encoding="utf-8", newline="") as f:
        return f.read().replace("\r\n", "\n")


TEXT = {
    "mp_js": read(P_MP_JS),
    "mp_wxml": read(P_MP_WXML),
    "mp_me_js": read(P_MP_ME_JS),
    "mp_me_wxml": read(P_MP_ME_WXML),
    "app_html": read(P_APP_HTML),
    "app_js": read(P_APP_JS),
    "app_me_js": read(P_APP_ME_JS),
    "backend": H.strip_comments(H.read_app_source()),
}


def checks(t):
    """返回 {判据名: 是否满足}（纯文本判据，变异自证直接复用）。"""
    b = t["backend"]
    c = {}
    # ---- 小程序：告知 + 勾选 + 保留期 ----
    c["mp_notice"] = all(k in t["mp_wxml"] for k in
                         ("文件使用与隐私说明", "用途", "保留期限", "第三方", "微信", "COS"))
    c["mp_consent_gate"] = ("if (!this.data.privacyAgreed)" in t["mp_js"]
                            and "请先阅读并勾选" in t["mp_js"])
    c["mp_retention_pick"] = ("onSelectRetention" in t["mp_wxml"]
                              and "retentionChoices" in t["mp_wxml"]
                              and "retention_hours: String(this.data.retentionHours" in t["mp_js"])
    c["mp_retention_submit"] = ("retention_hours: Number(this.data.retentionHours)" in t["mp_js"])
    c["mp_consent_reset"] = "privacyAgreed: false" in t["mp_js"]
    # ---- APP：同一套 ----
    c["app_notice"] = all(k in t["app_html"] for k in
                          ("文件使用与隐私说明", "用途", "保留期限", "第三方", "微信", "COS"))
    c["app_consent_gate"] = ("if (!printState.privacyAgreed)" in t["app_js"]
                             and "请先阅读并勾选" in t["app_js"])
    c["app_retention_pick"] = ("retentionChips" in t["app_html"]
                               and "selectRetention" in t["app_js"]
                               and "fd.append('retention_hours'" in t["app_js"])
    c["app_retention_submit"] = "retention_hours: Number(printState.retentionHours)" in t["app_js"]
    c["app_consent_reset"] = "printState.privacyAgreed = false" in t["app_js"]
    # ---- 两端「我的文件」入口 ----
    c["mp_myfiles"] = ('class="myfiles-entry"' in t["mp_me_wxml"]
                       and "/api/my_files" in t["mp_me_js"]
                       and "'/api/file/' + encodeURIComponent(fileId)" in t["mp_me_js"]
                       and "method: 'DELETE'" in t["mp_me_js"])
    c["app_myfiles"] = ('class="myfiles-entry"' in t["app_html"]
                        and "myFilesModal" in t["app_html"]
                        and "/api/my_files" in t["app_me_js"]
                        and "'/api/file/' + encodeURIComponent(fileId)" in t["app_me_js"]
                        and "method: 'DELETE'" in t["app_me_js"])
    # ---- 后端 ----
    c["backend_my_files"] = ('@app.route("/api/my_files"' in b
                             and "WHERE openid = ? AND path != ''" in b
                             and '"in_active_order"' in b
                             and '"auto_delete_at"' in b)
    c["backend_submit_retention"] = ("order_retention_hours = _clamp_retention_hours"
                                     in b
                                     and "UPDATE files SET retention_hours = ?" in b
                                     and "WHERE openid = ? AND id IN" in b)
    return c


# ============================================================
# A. 静态
# ============================================================
print("=== A. 静态（两端 + 后端）===")
res = checks(TEXT)
for name, ok in res.items():
    H.chk(name, ok)

# ============================================================
# B. 行为（真 Flask + 真 SQLite）
# ============================================================
print("\n=== B. 行为（真 Flask + 真 SQLite）===")
TMP, DST = H.make_copy("hn_privacy_")
backend = H.import_backend(DST)
backend.init_db()
backend.PRINTER_TOKEN = "priv-tok"
backend.ADMIN_OPENIDS = {"p-user", "p-other"}
http = backend.app.test_client()
A = {"Authorization": "Bearer " + backend.token_serializer.dumps("p-user")}
A2 = {"Authorization": "Bearer " + backend.token_serializer.dumps("p-other")}

PDF = b"%PDF-1.4\n%privacy test\n%%EOF\n"


def upload(headers, retention=None, name="a.pdf"):
    form = {"file": (io.BytesIO(PDF), name)}
    if retention is not None:
        form["retention_hours"] = str(retention)
    return http.post("/api/upload", data=form, headers=headers,
                     content_type="multipart/form-data")


def row_of(fid):
    conn = backend.get_db()
    try:
        return conn.execute("SELECT * FROM files WHERE id = ?", (fid,)).fetchone()
    finally:
        conn.close()


def submit(headers, fid, retention=None, crid=None):
    payload = {"client": "app", "duplex": "off",
               "files": [{"file_id": fid, "file": "a.pdf", "copies": 1}],
               "client_request_id": crid or ("priv-" + fid[:8])}
    if retention is not None:
        payload["retention_hours"] = retention
    return http.post("/api/submit_order", json=payload, headers=headers)


# B1. 上传带 retention_hours → 入库
r = upload(A, retention=72)
fid1 = (r.get_json() or {}).get("file_id")
H.chk("上传带 retention_hours=72 → 入库 72", row_of(fid1)["retention_hours"] == 72,
      row_of(fid1)["retention_hours"])

# B2. 提交时以提交时的选择为准（上传时没选，提交时选 999 → 夹到 168）
r2 = upload(A)
fid2 = (r2.get_json() or {}).get("file_id")
H.chk("上传不带保留期 → 行上 0（= 用全局默认）", row_of(fid2)["retention_hours"] == 0,
      row_of(fid2)["retention_hours"])
rs = submit(A, fid2, retention=999, crid="priv-clamp")
H.chk("提交带 retention_hours=999 → 200", rs.status_code == 200, rs.status_code)
H.chk("★ 提交时 999 被夹到 168 并写回 files 行", row_of(fid2)["retention_hours"] == 168,
      row_of(fid2)["retention_hours"])

# B3. 提交时 0/非法 = 不动（保留上传时的值）
r3 = upload(A, retention=24)
fid3 = (r3.get_json() or {}).get("file_id")
rs = submit(A, fid3, retention=0, crid="priv-zero")
H.chk("提交带 retention_hours=0 → 不改（仍是上传时的 24）",
      rs.status_code == 200 and row_of(fid3)["retention_hours"] == 24,
      (rs.status_code, row_of(fid3)["retention_hours"]))

# B4. 不能借提交改别人的文件保留期
r_other = upload(A2, retention=168, name="other.pdf")
fid_other = (r_other.get_json() or {}).get("file_id")
rs = submit(A, fid_other, retention=1, crid="priv-steal")
H.chk("★ 拿别人的 file_id 提交 → 400（归属校验在预计算阶段）", rs.status_code == 400,
      rs.status_code)
H.chk("★ 别人的文件保留期没被改动（仍 168）", row_of(fid_other)["retention_hours"] == 168,
      row_of(fid_other)["retention_hours"])

# B5. /api/my_files：未登录 401、只回自己的
H.chk("未登录访问 /api/my_files → 401", http.get("/api/my_files").status_code == 401)
mf = http.get("/api/my_files?per_page=50", headers=A)
j = mf.get_json() or {}
mine = {f["file_id"] for f in (j.get("files") or [])}
H.chk("★ /api/my_files 只回自己的文件（不含别人的）",
      mf.status_code == 200 and fid1 in mine and fid_other not in mine,
      (sorted(mine), fid_other))
H.chk("★ 列表带 retention_hours / auto_delete_at / in_active_order",
      all(k in (j["files"][0] if j.get("files") else {})
          for k in ("retention_hours", "auto_delete_at", "in_active_order")))

# B6. auto_delete_at = created_at + 保留期
row = row_of(fid1)
expect = (backend.datetime.strptime(row["created_at"], "%Y-%m-%d %H:%M:%S")
          + backend.timedelta(hours=72)).strftime("%Y-%m-%d %H:%M:%S")
item1 = [f for f in j["files"] if f["file_id"] == fid1][0]
H.chk("★ auto_delete_at == created_at + 72h（保留期对外可验证）",
      item1["auto_delete_at"] == expect, (item1["auto_delete_at"], expect))

# B7. 活跃订单引用：in_active_order=True 且 DELETE 409
rs = submit(A, fid1, retention=72, crid="priv-active")
H.chk("为 fid1 建一单（保持 queued）→ 200", rs.status_code == 200, rs.status_code)
item1 = [f for f in (http.get("/api/my_files?per_page=50", headers=A).get_json() or {})
         .get("files", []) if f["file_id"] == fid1][0]
H.chk("★ 被排队订单引用 → in_active_order=True", item1["in_active_order"] is True, item1)
H.chk("★ 此时删除 → 409", http.delete("/api/file/" + fid1, headers=A).status_code == 409)

# B8. 订单结束后可删；删完列表不再出现
conn = backend.get_db()
conn.execute("UPDATE order_files SET status='sent' WHERE file_id = ?", (fid1,))
conn.execute("UPDATE orders SET status='sent' WHERE id IN "
             "(SELECT order_id FROM order_files WHERE file_id = ?)", (fid1,))
conn.commit()
conn.close()
H.chk("订单结束后删除 → 200", http.delete("/api/file/" + fid1, headers=A).status_code == 200)
mine2 = {f["file_id"] for f in (http.get("/api/my_files?per_page=50", headers=A).get_json()
                                or {}).get("files", [])}
H.chk("★ 删除后不再出现在「我的文件」列表", fid1 not in mine2, sorted(mine2))

# B9. 全局"永不过期" → 没自选保留期的文件 auto_delete_at 为空
cfg = backend.load_retention_config()
cfg["days"], cfg["hours"] = 0, 0
backend.save_retention_config(cfg)
r5 = upload(A, name="never.pdf")
fid5 = (r5.get_json() or {}).get("file_id")
item5 = [f for f in (http.get("/api/my_files?per_page=50", headers=A).get_json() or {})
         .get("files", []) if f["file_id"] == fid5][0]
H.chk("★ 全局永不过期且未自选 → auto_delete_at 为空（保留期口径一致）",
      item5["retention_hours"] == 0 and item5["auto_delete_at"] == "",
      (item5["retention_hours"], item5["auto_delete_at"]))

# ============================================================
# C. 变异自证
# ============================================================
print("\n=== C. 变异自证（改回修复前形态必须红）===")


def mutate_and_check(name, key, old, new, target):
    """把源码改回修复前形态，判定指定判据必须变红。返回 True = 判据确实红了。"""
    mut = dict(TEXT)
    if old not in mut[key]:
        print("[FAIL] 变异锚点未命中：%s" % name)
        return False
    mut[key] = mut[key].replace(old, new, 1)
    red = not checks(mut).get(target, False)
    print("[%s] %s（判据 %s %s）" % ("PASS" if red else "FAIL", name, target,
                                    "变红" if red else "仍然绿 —— 判据不成立"))
    return red


mutations = [
    ("去掉小程序的隐私勾选闸门", "mp_js",
     "if (!this.data.privacyAgreed)", "if (false)", "mp_consent_gate"),
    ("小程序提交不再发 retention_hours", "mp_js",
     "retention_hours: Number(this.data.retentionHours) || 0,", "", "mp_retention_submit"),
    ("小程序上传不再带 retention_hours", "mp_js",
     "retention_hours: String(this.data.retentionHours || 0),", "", "mp_retention_pick"),
    ("APP 去掉隐私勾选闸门", "app_js",
     "if (!printState.privacyAgreed)", "if (false)", "app_consent_gate"),
    ("APP 提交不再发 retention_hours", "app_js",
     "retention_hours: Number(printState.retentionHours) || 0,", "", "app_retention_submit"),
    ("小程序「我的文件」不再调用删除接口", "mp_me_js",
     "'/api/file/' + encodeURIComponent(fileId)", "''", "mp_myfiles"),
    ("APP「我的文件」不再调用删除接口", "app_me_js",
     "'/api/file/' + encodeURIComponent(fileId)", "''", "app_myfiles"),
    ("后端提交时不再改写保留期", "backend",
     "UPDATE files SET retention_hours = ? WHERE openid = ? AND id IN",
     "UPDATE files SET size = size WHERE openid = ? AND id IN",
     "backend_submit_retention"),
]
bad = 0
for name, key, old, new, target in mutations:
    if not mutate_and_check(name, key, old, new, target):
        bad += 1
H.chk("全部变异都被判据打红（%d 处）" % len(mutations), bad == 0, bad)

# 后端行为变异：去掉「提交时改写保留期」→ 行上仍是 0
mut_tmp, mut_dst = H.mutated_copy([
    ("        if order_retention_hours > 0:\n", "        if False:\n"),
], prefix="hn_privacy_mut_")
proc = H.run_probe(mut_dst, r"""
import io
backend.PRINTER_TOKEN = "probe-tok"
backend.ADMIN_OPENIDS = {"p-user"}
http = backend.app.test_client()
A = {"Authorization": "Bearer " + backend.token_serializer.dumps("p-user")}
r = http.post("/api/upload", data={"file": (io.BytesIO(b"%PDF-1.4\n%x\n"), "a.pdf")},
              headers=A, content_type="multipart/form-data")
fid = (r.get_json() or {}).get("file_id")
http.post("/api/submit_order", json={"client": "app", "duplex": "off",
          "files": [{"file_id": fid, "file": "a.pdf", "copies": 1}],
          "client_request_id": "mut-ret-1", "retention_hours": 72}, headers=A)
row = backend.get_db().execute("SELECT retention_hours FROM files WHERE id=?", (fid,)).fetchone()
print("RET=%s" % (row["retention_hours"] if row else "none"))
""")
H.chk("★ 变异（提交时不改写保留期）⇒ 行上仍是 0（判据能红）",
      H.probe_value(proc, "RET") == "0", H.probe_value(proc, "RET"))

H.finish()
