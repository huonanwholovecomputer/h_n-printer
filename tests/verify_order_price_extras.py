# -*- coding: utf-8 -*-
"""验收 · 🔴-C4 后端「结算总价」漏加附加费（与 orders.total_price 不同口径）

现场形状（审查报告 D2-🟡6）：
  · `POST /api/submit_order` 建单时，父订单价 = 文件小计 + 加急费 + 首页费 + 派送费
    并写入 `orders.total_price`（`app.py` 的 `parent_total_price`）。
  · 但 `/api/order_price/<id>` 与 `/api/license/finish` 的"总价"却是
    `sum(order_files.total_price)` —— **只算文件小计、三类附加费全丢**。
  · 前端把该字段直接当「结算总价」拼进剪贴板（`android_app/www/me.js`、
    `h_n_print/pages/me/me.js` 的 `text += '\\n总价: ¥' + d.total_price.toFixed(2)`）
    ⇒ 管理员复制给顾客的是一笔**低于实收**的钱；只要勾了首页（默认 ¥0.10）必现。

本脚本：
  A. 静态接线：两个接口都必须读 `orders.total_price`（同一表达式、各 1 处），
     旧的"逐文件求和"写法必须消失；两份前端镜像仍把该字段当结算总价（口径契约）。
  B. 行为断言：把 printer-backend 复制到临时目录、导入**真实** app，用 Flask
     test_client 走**真实** `/api/submit_order` 建单（真实计费 + 真实落库），
     再比对 `/api/order/<id>`（已知正确的订单详情口径）、`/api/order_price/<id>`、
     `license_finish` 三者的 total_price 与库里 `orders.total_price` 是否一致。
     覆盖：三类附加费全开 / 无附加费对照 / 只勾首页 / 多文件多份 + 派送 /
     奇数页双面（尾页走单面价）/ 许可密钥结束打印链路 / 归属过滤下的管理员路径。
  C（🟡-C4b，2026-10-16）：`get_order_price` 的归属过滤 `WHERE id = ? AND openid = ?` 让
     管理员读**他人**订单恒 404，而本接口唯一调用方就是管理员的「活跃密钥 → 结算」按钮
     ⇒ 该按钮线上从未可用。修法：管理员不过滤 openid，其余用户保持原判（同一条 404 文案）。
     断言：G1/G2 管理员放行且金额不变、G3 非归属非管理员仍 404（放行不得外溢）、G4 归属者
     本人不受影响；A6 静态判据 + 两条"变异必红"对拍。

用法：python tests/verify_order_price_extras.py     （exit 0 = 全部通过）
"""
import io
import json
import os
import re
import shutil
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(REPO, "mobile_apps", "printer-backend")
APP = os.path.join(SRC, "app.py")
APP_JS = os.path.join(REPO, "mobile_apps", "android_app", "www", "me.js")
MP_JS = os.path.join(REPO, "mobile_apps", "h_n_print", "pages", "me", "me.js")

bad = 0


def chk(name, cond, detail=""):
    global bad
    if not cond:
        bad += 1
    print("[%s] %s%s" % ("PASS" if cond else "FAIL", name,
                         ("  -- " + detail) if (not cond and detail) else ""))


src = open(APP, encoding="utf-8").read()

# ============================================================
# A. 静态接线
# ============================================================
print("=== A. 静态接线 ===")

# A1 旧的"逐文件求和"写法必须彻底消失（两处都曾用它，且是本 bug 的载体）
chk("已无 `sum(f.get(\"total_price\", 0) for f in files)` 旧写法",
    'sum(f.get("total_price", 0) for f in files)' not in src)
chk("已无 `total_files_price` 死变量残留", "total_files_price" not in src)

# A2 两处都必须读订单级 total_price，且是**同一个表达式**（口径同源）
TOTAL_EXPR = re.compile(r'round\(\s*float\(order\.get\("total_price"\)\s+or\s+0\),\s*2\s*\)')
_hits = TOTAL_EXPR.findall(src)
chk("两个接口都读订单级 orders.total_price（同一表达式，恰 2 处）",
    len(_hits) == 2, "实际 %d 处" % len(_hits))

for name, sig in (("/api/order_price", "def get_order_price(order_id):"),
                  ("/api/license/finish", "def license_finish():")):
    body = src.split(sig, 1)[1].split("\n@app.route", 1)[0] if sig in src else ""
    chk("%s 函数体内含订单级总价表达式" % name, TOTAL_EXPR.search(body) is not None)

# A3 注释理由必须在（防"静默回退"到旧写法）
chk("两处都带 🔴-C4 溯源注释", src.count("🔴-C4") >= 2, "实际 %d 处" % src.count("🔴-C4"))

# A4 口径契约：两份前端镜像仍把该字段当「结算总价」复制
app_js = open(APP_JS, encoding="utf-8").read()
mp_js = open(MP_JS, encoding="utf-8").read()
chk("App 端仍把 d.total_price 当结算总价",
    "text += '\\n总价: ¥' + (typeof d.total_price === 'number' ? d.total_price : 0).toFixed(2);" in app_js)
chk("小程序端仍把 d.total_price 当结算总价",
    "text += '\\n总价: ¥' + (typeof d.total_price === 'number' ? d.total_price : 0).toFixed(2)" in mp_js)

# A5 权价来源：pricing.json（本脚本的期望值由它推导，防止换价后断言数字失真）
PRICING = json.load(open(os.path.join(SRC, "pricing.json"), encoding="utf-8"))
chk("权威定价 simplex=0.30 / duplex=0.40 / cover=0.10（期望值的前提）",
    abs(float(PRICING["simplex_price"]) - 0.30) < 1e-9
    and abs(float(PRICING["duplex_price"]) - 0.40) < 1e-9
    and abs(float(PRICING["cover_page_price"]) - 0.10) < 1e-9,
    str({k: PRICING.get(k) for k in ("simplex_price", "duplex_price", "cover_page_price")}))

# A6（🟡-C4b）归属分支：管理员放行 + 经典 openid 过滤仍在 else 侧。
# 判据抽成函数，配"变异后必红"的对拍（否则这类静态判据很容易写成永远为绿）。


def _blank_comment_lines(text):
    """把每行 `#` 之后的内容换成等长空格（保字符偏移）——
    **判据不许吃注释**：注释里天然会引用被考的 SQL / 写法，不置空会让"删掉代码里的条件"
    的对拍永远为绿（D3/D4 都踩过这一条）。"""
    out = []
    for line in text.split("\n"):
        i = line.find("#")
        out.append(line if i < 0 else line[:i] + " " * (len(line) - i))
    return "\n".join(out)


def _c4b_admin_branch(text):
    """get_order_price 函数体里必须同时有：管理员放行分支 + 非管理员侧的 openid 归属条件。"""
    if "def get_order_price(order_id):" not in text:
        return False
    body = _blank_comment_lines(
        text.split("def get_order_price(order_id):", 1)[1].split("\n@app.route", 1)[0])
    return ('compute_role(g.openid) == "admin"' in body
            and "WHERE id = ? AND openid = ?" in body)


chk("A6：get_order_price 含管理员放行分支（🟡-C4b 的修复点）", _c4b_admin_branch(src))
# 对拍 1：删掉管理员分支 ⇒ 判据必红
chk("A6 对拍①：去掉 `compute_role(g.openid) == \"admin\"` 后判据必红",
    _c4b_admin_branch(src.replace('if compute_role(g.openid) == "admin":', "if False:", 1)) is False)
# 对拍 2：把 else 侧的 openid 归属条件也去掉（过度放行）⇒ 判据必红。
# 注意此处**只删代码里的那句、注释里留着**：正是为了钉死"判据不许吃注释"。
chk("A6 对拍②：else 侧丢掉 openid 归属条件后判据必红（注释里还留着同一句话）",
    _c4b_admin_branch(src.replace(
        '"SELECT id, total_price, is_free FROM orders WHERE id = ? AND openid = ?",\n            (order_id, g.openid),',
        '"SELECT id, total_price, is_free FROM orders WHERE id = ?",\n            (order_id,),', 1)) is False)
chk("A6 对拍③：置空注释不影响真源码上的判据（不误杀）",
    _blank_comment_lines(src).count("compute_role(g.openid) == \"admin\"") >= 1
    and _c4b_admin_branch(src))

# ============================================================
# B. 行为断言：真实后端 + 真实建单管线（临时目录隔离）
# ============================================================
print("=== B. 真实链路（临时后端副本 + Flask test_client）===")

TMP = tempfile.mkdtemp(prefix="hn_price_")
DST = os.path.join(TMP, "printer-backend")
# 隔离：不复制真实 config.py（含生产 SECRET_KEY / 管理员 openid）与真实库
shutil.copytree(SRC, DST, ignore=shutil.ignore_patterns(
    "__pycache__", "config.py", "config.py.example",
    "orders.db", "users.db", "uploads", "logs", "*.db-wal", "*.db-shm"))
os.chdir(DST)
# 允许内置 SECRET_KEY（否则 app.py 在生产模式下直接 RuntimeError）
os.environ["FLASK_ENV"] = "development"
sys.path.insert(0, DST)

try:
    import app as backend
except Exception as e:  # noqa: BLE001
    print("✗ 无法导入 app.py: %r" % (e,))
    shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(1)

backend.init_db()

ADMIN_OID = "openid-admin-c4"
GUEST_OID = "openid-guest-c4"
backend.ADMIN_OPENIDS.add(ADMIN_OID)

client = backend.app.test_client()


def HDR(openid):
    return {"Authorization": "Bearer " + backend.token_serializer.dumps(openid)}


def _mk_file(fid, pages, openid, name):
    """预置一条 files 行（页数已"验证"）。

    跳过 /api/upload 是**有意的**：页数探测（pypdf / LibreOffice）不是本案的被测对象，
    而 submit_order 对 page_count>0 的缓存行会直接采用（`cached = frow["page_count"]`），
    所以这样仍然走的是**真实的计费管线**，只是把不确定的页数变量固定下来。
    """
    sub = os.path.join(backend.UPLOAD_DIR, "pdf")
    os.makedirs(sub, exist_ok=True)
    path = os.path.abspath(os.path.join(sub, fid + ".pdf"))
    with open(path, "wb") as fh:
        fh.write(b"%PDF-1.4\n% fixture\n")
    conn = backend.get_db()
    conn.execute(
        """INSERT INTO files (id, original_name, saved_name, path, size, created_at,
                              page_count, page_count_verified, md5, openid)
           VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)""",
        (fid, name, fid + ".pdf", path, os.path.getsize(path),
         "2026-01-01 00:00:00", pages, "md5-" + fid, openid),
    )
    conn.commit()
    conn.close()
    return fid


F20 = _mk_file("f20p-c4", 20, ADMIN_OID, "二十页.pdf")     # 双面 10 张 × 0.4 = 4.00
F10 = _mk_file("f10p-c4", 10, ADMIN_OID, "十页.pdf")       # 双面  5 张 × 0.4 = 2.00
F7 = _mk_file("f07p-c4", 7, ADMIN_OID, "七页.pdf")         # 双面 4 张：3×0.4 + 1×0.3 = 1.50
F20G = _mk_file("f20g-c4", 20, GUEST_OID, "二十页-顾客.pdf")  # 归属顾客（临时许可用户）


def submit(openid, files, **extra):
    body = {"files": files, "duplex": "on", "client": "wechat"}
    body.update(extra)
    r = client.post("/api/submit_order", headers=HDR(openid), json=body)
    j = r.get_json() or {}
    assert j.get("success") is True, "建单失败 %s: %s" % (r.status_code, j.get("message"))
    return j["order_id"]


def raw_order(oid):
    conn = backend.get_db()
    row = conn.execute(
        """SELECT total_price, cover_page, cover_page_price, urgency, urgency_price,
                  delivery_enabled, delivery_percentage
           FROM orders WHERE id = ?""", (oid,)).fetchone()
    conn.close()
    return dict(row)


def files_sum(oid):
    conn = backend.get_db()
    row = conn.execute(
        "SELECT COALESCE(SUM(total_price), 0) AS s FROM order_files WHERE order_id = ?",
        (oid,)).fetchone()
    conn.close()
    return round(float(row["s"]), 2)


def api_price(oid, openid):
    return client.get("/api/order_price/%d" % oid, headers=HDR(openid))


def api_detail(oid, openid):
    """订单详情口径（/api/order/<id>，返回 {"success":…, "order":{…}}）。"""
    r = client.get("/api/order/%d" % oid, headers=HDR(openid))
    return (r.get_json() or {}).get("order") or {}


def extras_of(oid):
    """按**建单公式**独立算出附加费（附加费基准 = 文件小计）。"""
    o = raw_order(oid)
    base = files_sum(oid)
    urg = round(float(o["urgency_price"] or 0), 2)
    cover = round(float(o["cover_page_price"] or 0) * int(o["cover_page"] or 0), 2)
    deliv = round(base * float(o["delivery_percentage"] or 0) / 100, 2) if o["delivery_percentage"] else 0.0
    return base, urg, cover, deliv


def check_order(tag, oid, openid, expect_total):
    """核心不变量：**任何**暴露"结算总价"的接口都必须等于 orders.total_price。"""
    o = raw_order(oid)
    base, urg, cover, deliv = extras_of(oid)
    db_total = round(float(o["total_price"] or 0), 2)

    chk("%s：真实建单落库 total_price == 文件小计 + 三类附加费" % tag,
        abs(db_total - (base + urg + cover + deliv)) < 0.005,
        "库=%.2f 文件=%.2f 加急=%.2f 首页=%.2f 派送=%.2f" % (db_total, base, urg, cover, deliv))
    if expect_total is not None:
        chk("%s：落库 total_price == ¥%.2f（锚点）" % (tag, expect_total),
            abs(db_total - expect_total) < 1e-9, "实际 %.2f" % db_total)

    det = api_detail(oid, openid)
    chk("%s：/api/order/<id> 总价 == 库值（既有正确口径）" % tag,
        abs(round(float(det.get("total_price") or 0), 2) - db_total) < 1e-9,
        "%.2f vs %.2f" % (float(det.get("total_price") or 0), db_total))

    r = api_price(oid, openid)
    j = r.get_json() or {}
    chk("%s：/api/order_price 返回 200 + success" % tag,
        r.status_code == 200 and j.get("success") is True, "%s %s" % (r.status_code, j))
    got = round(float(j.get("total_price") or 0), 2)
    chk("★ %s：/api/order_price 总价 == 库值（🔴-C4 的修复点）" % tag,
        abs(got - db_total) < 1e-9,
        "接口 %.2f vs 库 %.2f（差 %.2f = 被漏掉的附加费）" % (got, db_total, db_total - got))
    return base, db_total, j


print("\n--- 场景 A：三类附加费全开（加急 + 首页 + 派送）---")
oid_a = submit(ADMIN_OID,
               [{"file_id": F20, "copies": 1, "duplex": "on"}],
               cover_page=1, delivery_enabled=1, delivery_location="图书馆",
               urgency="高", urgency_price=0.15)
# 期望：文件 4.00 + 加急 0.15 + 首页 0.10 + 派送 4.00×15%=0.60 → 4.85
base_a, total_a, pj_a = check_order("A", oid_a, ADMIN_OID, 4.85)
chk("A：旧写法会少收 ¥%.2f（接口值应严格大于文件小计）" % (total_a - base_a),
    abs(total_a - base_a) > 1e-9 and total_a - base_a > 0)

print("\n--- 场景 B：无附加费（对照组，修复前后必须都是 4.00）---")
oid_b = submit(ADMIN_OID, [{"file_id": F20, "copies": 1, "duplex": "on"}])
base_b, _, _ = check_order("B", oid_b, ADMIN_OID, 4.00)
chk("B：无附加费时 接口值 == 文件小计（修复未改变无附加费口径）",
    abs(raw_order(oid_b)["total_price"] - base_b) < 1e-9)

print("\n--- 场景 C：只勾首页（默认 ¥0.10，最易复现）---")
oid_c = submit(ADMIN_OID, [{"file_id": F20, "copies": 1, "duplex": "on"}], cover_page=1)
check_order("C", oid_c, ADMIN_OID, 4.10)

print("\n--- 场景 D：多文件多份 + 派送 ---")
oid_d = submit(ADMIN_OID,
               [{"file_id": F20, "copies": 2, "duplex": "on"},
                {"file_id": F10, "copies": 1, "duplex": "on"}],
               delivery_enabled=1, delivery_location="图书馆")
# 期望：文件 4.00×2 + 2.00 = 10.00；派送 10.00×15% = 1.50 → 11.50
check_order("D", oid_d, ADMIN_OID, 11.50)

print("\n--- 场景 D2：奇数页双面（尾页按单面价）+ 首页费 ---")
oid_d2 = submit(ADMIN_OID, [{"file_id": F7, "copies": 1, "duplex": "on"}], cover_page=1)
# 期望：7 页双面 = 3 张×0.4 + 1 页×0.3 = 1.50；+ 首页 0.10 → 1.60
check_order("D2", oid_d2, ADMIN_OID, 1.60)

print("\n--- 场景 E：许可密钥链路（license_finish 的 price_detail）---")
r = client.post("/api/license/create", headers=HDR(ADMIN_OID), json={"validity_minutes": 10})
key = (r.get_json() or {}).get("key")
chk("E：管理员可创建许可密钥", bool(key), str(r.get_json()))
r = client.post("/api/license/redeem", headers=HDR(GUEST_OID), json={"key": key})
chk("E：临时许可用户可兑换密钥", (r.get_json() or {}).get("success") is True, str(r.get_json()))
oid_e = submit(GUEST_OID, [{"file_id": F20G, "copies": 1, "duplex": "on"}], cover_page=1)
# 期望：4.00 + 0.10 = 4.10
check_order("E", oid_e, GUEST_OID, 4.10)

r = client.post("/api/license/finish", headers=HDR(ADMIN_OID), json={"key": key})
j = r.get_json() or {}
pd = j.get("price_detail") or {}
db_total_e = round(float(raw_order(oid_e)["total_price"] or 0), 2)
chk("E：license_finish 返回 price_detail", j.get("success") is True and bool(pd), str(j.get("message")))
chk("★ E：license_finish 的 price_detail.total_price == orders.total_price（🔴-C4 第二处）",
    abs(round(float(pd.get("total_price") or 0), 2) - db_total_e) < 1e-9,
    "price_detail %.2f vs 库 %.2f" % (float(pd.get("total_price") or 0), db_total_e))
chk("E：两处接口口径一致（order_price == license_finish）",
    abs(round(float(pd.get("total_price") or 0), 2)
        - round(float((api_price(oid_e, GUEST_OID).get_json() or {}).get("total_price") or 0), 2)) < 1e-9)

print("\n--- 场景 F：响应结构不回归（只换总价口径，不动其它字段）---")
j = api_price(oid_d, ADMIN_OID).get_json() or {}
files = j.get("files") or []
chk("F：order_id 为 int 且与请求一致", isinstance(j.get("order_id"), int) and j["order_id"] == oid_d,
    repr(j.get("order_id")))
chk("F：files 明细条数 == 2", len(files) == 2, "%d" % len(files))
need = {"id", "file_name", "copies", "page_count", "duplex", "page_range", "total_price",
        "is_free", "status", "per_copy_price", "effective_pages", "sheets",
        "total_pages", "total_sheets", "unit"}
chk("F：每个文件明细字段齐备",
    all(need <= set(f.keys()) for f in files),
    str(sorted(need - set(files[0].keys())) if files else "无 files"))
chk("F：is_free 仍为 bool（无子任务时才可能为 True，本单含子任务 → False）",
    j.get("is_free") is False, repr(j.get("is_free")))
chk("F：文件小计之和 == 场景 D 的文件小计 10.00",
    abs(sum(round(float(f.get("total_price") or 0), 2) for f in files) - 10.00) < 1e-9)

print("\n--- 场景 G：归属 —— 管理员放行（🟡-C4b）/ 非归属非管理员仍 404 ---")
r = client.get("/api/order_price/%d" % oid_e, headers=HDR(ADMIN_OID))
chk("★ G1：管理员读**他人**订单现在返回 200（🟡-C4b 修复点；改前恒 404）",
    r.status_code == 200, "HTTP %s" % r.status_code)
got_g = round(float((r.get_json() or {}).get("total_price") or 0), 2)
chk("★ G2：管理员读到他人订单时总额仍 == 库值（放行不改口径）",
    r.status_code == 200 and abs(got_g - db_total_e) < 1e-9,
    "HTTP %s / %.2f vs 库 %.2f" % (r.status_code, got_g, db_total_e))

# G3 放行不得外溢：既非归属者、也非管理员的普通用户必须仍拿 404（且文案不泄露存在性）
OTHER_OID = "openid-other-c4"          # 库里没有该用户 ⇒ compute_role 判 guest
r_other = client.get("/api/order_price/%d" % oid_e, headers=HDR(OTHER_OID))
chk("★ G3：非归属、非管理员用户仍 404（放行只给管理员）",
    r_other.status_code == 404, "HTTP %s %s" % (r_other.status_code, r_other.get_json()))
chk("★ G3：404 文案与「无权访问」一致（不区分不存在/无权，不泄露存在性）",
    (r_other.get_json() or {}).get("message") == "任务不存在或无权访问",
    repr((r_other.get_json() or {}).get("message")))

# G4 归属者本人（顾客 / 普通用户）读自己的订单不受影响
r_own = client.get("/api/order_price/%d" % oid_e, headers=HDR(GUEST_OID))
chk("★ G4：订单归属者本人仍可读（未回归）", r_own.status_code == 200,
    "HTTP %s" % r_own.status_code)

# 清理
os.chdir(REPO)
sys.path.remove(DST)
shutil.rmtree(TMP, ignore_errors=True)

print("=" * 60)
print("后端结算总价口径验收:", "全部通过 ✅" if bad == 0 else "存在失败 ❌")
sys.exit(1 if bad else 0)
