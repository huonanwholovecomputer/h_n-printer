# -*- coding: utf-8 -*-
"""验收 · 本地单的缩略图在移动端显示（2026-11-23 用户需求）

用户原话：「本地订单的略缩图还没有在移动端生效？应该就差显示出来了吧」

现场缺口：本地单的打印稿缩略图以前**只存在店主那台电脑上**
（%APPDATA%\\HN打印工具\\order_thumbs\\<订单号>\\），只喂本机结算页
（`/api/local/order_thumb/<订单号>`，见 verify_local_order_thumb.py）。云端一条都没有，
于是小程序/APP 的详情卡只能写「这单是本地打单 —— 源文件只保存在店主的电脑上，
云端没有副本，所以没有预览图」。

本轮补的路子：
  ① 打印工具收尾渲完本地缩略图，顺手把前几页（1024px、最多 6 页）回传云端
     —— `POST /api/local/order_thumb`，键是 **(订单号, 文件名)**（不能用 order_files.id：
     每次上报都 DELETE + 重插，行 id 会变；也不能用 files.id：本地单根本没有那一行）；
  ② 移动端问 `GET /api/local_order_thumb/info` 拿页数，再逐页取
     `GET /api/local_order_thumb?order_number=&name=&page=`（token 走查询串，
     因为 `<image>` 带不了 Authorization 头，与云端 previewUrl 同一套约定）；
  ③ 可见性与 /api/orders 默认分支同口径：自己的单 / 管理员 / 绑定成员名下的。

本脚本验两层（前端静态断言在 verify_order_file_detail.js 里）：
  A. 后端三个真实路由（Flask test client）：上传/页数/取图/鉴权/可见性/上限/清理
  B. 客户端接线：order_thumbs.load_pages 的缩小与限量、cloud_client 的逐页回传与开关

跑法：python other/tests/verify_local_thumb_mobile.py    （退出码 0 = 全绿）
"""
import base64
import io
import os
import shutil
import sys
import tempfile
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
BACKEND = os.path.join(ROOT, "mobile_apps", "printer-backend")
LOCAL_TOOL = os.path.join(ROOT, "local_print_tool")

ok_all = True


def chk(name, cond, detail=""):
    global ok_all
    if not cond:
        ok_all = False
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  — {detail}" if detail else ""))
    return bool(cond)


def png_bytes(w=48, h=64, color="white"):
    """造一张真图（后端按真实字节判格式，随便一段 bytes 会被 400 掉）。"""
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, "WEBP", quality=80)
    return buf.getvalue()


def jpeg_bytes(w=48, h=64, color="white"):
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, "JPEG", quality=82)
    return buf.getvalue()


# ═══════════════════ A. 后端三个真实路由 ═══════════════════
print("=== A. 后端 /api/local/order_thumb*（真实 Flask + SQLite）===")

TMP = tempfile.mkdtemp(prefix="hn_localthumb_")
DST = os.path.join(TMP, "printer-backend")
shutil.copytree(BACKEND, DST, ignore=shutil.ignore_patterns(
    "__pycache__", "config.py", "config.py.example",
    "orders.db", "users.db", "uploads", "logs", "*.db-wal", "*.db-shm"))
os.chdir(DST)
os.environ["FLASK_ENV"] = "development"
sys.path.insert(0, DST)

import app as backend  # noqa: E402

backend.init_db()
backend.PRINTER_TOKEN = "test-token"
HDR = {"X-Printer-Token": "test-token"}
client = backend.app.test_client()

ORDER = "HN20261123-9001"
NAME = "37-拓奕乐-组合数据类型-字符串.docx"
OWNER = "owner-openid-9001"


def mk_order(num, openid="local", source="local", owner="霍楠"):
    conn = backend.get_db()
    conn.execute(
        """INSERT INTO orders (file, copies, status, created_at, openid, order_number,
                               total_price, source, owner_name, is_admin_print,
                               print_started_at)
           VALUES ('本地打印 1 个文件', 1, 'sent', '2026-11-23 12:15:12', ?, ?, 1.9, ?, ?,
                   1, '2026-11-23 12:38:04')""",
        (openid, num, source, owner),
    )
    oid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        """INSERT INTO order_files (order_id, file_name, copies, page_count, total_price,
                                    status, duplex, created_at, page_range, locked_at,
                                    reject_reason)
           VALUES (?, ?, 1, 9, 1.9, 'sent', 'on', '2026-11-23 12:15:12', '', '', '')""",
        (oid, NAME),
    )
    conn.commit()
    conn.close()
    return oid


def upload(num, name, page, data, fmt="webp"):
    return client.post("/api/local/order_thumb", headers=HDR, json={
        "order_number": num, "name": name, "page": page, "fmt": fmt,
        "data": base64.b64encode(data).decode("ascii"),
    })


def tok(openid):
    return backend._issue_login_token(openid)


def info(num, name, openid):
    return client.get("/api/local_order_thumb/info", query_string={
        "order_number": num, "name": name, "token": tok(openid)})


def get_page(num, name, page, openid):
    return client.get("/api/local_order_thumb", query_string={
        "order_number": num, "name": name, "page": page, "token": tok(openid)})


mk_order(ORDER, openid=OWNER)
IMG1 = png_bytes(color="white")
IMG2 = png_bytes(color="black")

# A0 静态：三端点都在，且不进 /api/proxy 白名单（图片会被 JSON 代理吃成"响应格式错误"）
src = open(os.path.join(DST, "app.py"), encoding="utf-8", errors="replace").read()
chk("A0 三个路由都存在（POST 上传 / info / GET 取图）",
    '@app.route("/api/local/order_thumb", methods=["POST"])' in src
    and '@app.route("/api/local_order_thumb/info", methods=["GET"])' in src
    and '@app.route("/api/local_order_thumb", methods=["GET"])' in src)
chk("A0 上传端点在 require_printer_scope('admin') 之下（本地工具才有的权限）",
    '@require_printer_scope("admin")\ndef local_order_thumb_upload' in src)
proxy_block = src.split("def proxy(")[1][:4000] if "def proxy(" in src else ""
chk("A0 取图端点没有被塞进 JSON 代理白名单", "local_order_thumb" not in proxy_block,
    "proxy 段落里不该出现 local_order_thumb")
chk("A0 缩略图落在 uploads 下（跟着 UPLOAD_DIR 一起部署/清理）",
    'LOCAL_THUMB_DIR = os.path.join(UPLOAD_DIR, "local_thumbs")' in src)

# A1 上传第 1 页
r = upload(ORDER, NAME, 1, IMG1)
body = r.get_json() or {}
chk("A1 上传第 1 页 → 200 且回 pages=1",
    r.status_code == 200 and body.get("success") and body.get("pages") == 1,
    f"HTTP {r.status_code} {body}")
chk("A1 落盘路径是 <订单号>/<文件名键>_p1.webp（文件名键 = md5，不是明文）",
    os.path.isfile(backend._local_thumb_path(ORDER, NAME, 1, "webp", create=False)),
    backend._local_thumb_path(ORDER, NAME, 1, "webp", create=False))
chk("A1 目录里没有 .part 残留（先写 .part 再 os.replace 原子替换）",
    not [n for n in os.listdir(backend._local_thumb_order_dir(ORDER, create=False))
         if n.endswith(".part")])

# A2 第 2 页 + 幂等覆盖
r = upload(ORDER, NAME, 2, IMG2)
body = r.get_json() or {}
chk("A2 上传第 2 页 → pages=2", r.status_code == 200 and body.get("pages") == 2, body)
r = upload(ORDER, NAME, 1, IMG1)
chk("A2 同 (订单号, 文件名, 页) 重复上传是覆盖，不是追加",
    r.status_code == 200 and (r.get_json() or {}).get("pages") == 2,
    (r.get_json() or {}).get("pages"))

# A3 info：自己看自己的单
r = info(ORDER, NAME, OWNER)
body = r.get_json() or {}
chk("A3 info（下单人本人）→ 200 pages=2 并回显文件名",
    r.status_code == 200 and body.get("pages") == 2 and body.get("name") == NAME,
    f"HTTP {r.status_code} {body}")
chk("A3 有图时 info 给 preview_url，没有图时给空串（前端据此决定铺不铺 swiper）",
    body.get("preview_url", "").startswith("/api/local_order_thumb?order_number=")
    and (info("HN20261123-9999", NAME, OWNER).get_json() or {}).get("pages") in (None, 0))

# A4 取图：字节一致 + 分页头
r = get_page(ORDER, NAME, 1, OWNER)
chk("A4 取第 1 页 → 200 且字节与上传一致",
    r.status_code == 200 and r.data == IMG1, f"HTTP {r.status_code} {len(r.data) if r.data else 0}B")
chk("A4 响应头带 X-Preview-Page / X-Preview-Pages（前端翻页角标要用）",
    r.headers.get("X-Preview-Page") == "1" and r.headers.get("X-Preview-Pages") == "2",
    f"{r.headers.get('X-Preview-Page')}/{r.headers.get('X-Preview-Pages')}")

# A5 没上传过的页 ⇒ 404 人话（<image> 出错时前端会摘掉这一页）
r = get_page(ORDER, NAME, 3, OWNER)
chk("A5 没上传过的第 3 页 → 404「该页没有缩略图」",
    r.status_code == 404 and "该页没有缩略图" in (r.get_json() or "").get("message", ""),
    r.get_json())

# A6 文件名键稳定：大小写/首尾空格不同也算同一个文件
r = upload(ORDER, NAME.upper() + "  ", 1, IMG1)
chk("A6 文件名大小写/空格归一后仍是同一个键（md5(name.strip().lower())）",
    r.status_code == 200 and (r.get_json() or {}).get("pages") == 2,
    (r.get_json() or {}).get("pages"))
chk("A6 用另一种写法也能取到同一张图",
    get_page(ORDER, NAME.upper(), 1, OWNER).data == IMG1)

# A7 订单不存在 ⇒ 404（宁可丢图，也不留没有主的目录）
r = upload("HN20261123-9998", NAME, 1, IMG1)
chk("A7 上传到不存在的订单 → 404「订单不存在」",
    r.status_code == 404 and "订单不存在" in (r.get_json() or "").get("message", ""),
    r.get_json())
chk("A7 不存在的订单不会建出目录",
    not os.path.isdir(backend._local_thumb_order_dir("HN20261123-9998", create=False)))

# A8 越权：别人的单看不了（可见性 = 自己的 / 管理员 / 绑定成员）
STRANGER = "stranger-openid-1"
r = info(ORDER, NAME, STRANGER)
chk("A8 别人问 info → 403「无权查看该订单」",
    r.status_code == 403 and "无权" in (r.get_json() or "").get("message", ""), r.get_json())
r = get_page(ORDER, NAME, 1, STRANGER)
chk("A8 别人取图 → 403（不泄露字节）", r.status_code == 403, r.status_code)

# A9 管理员看全部本地单
backend.ADMIN_OPENIDS = list(backend.ADMIN_OPENIDS or []) + ["admin-openid-1"]
chk("A9 管理员能看别人的本地单（与 /api/orders 的 source=local 分支同口径）",
    info(ORDER, NAME, "admin-openid-1").status_code == 200
    and get_page(ORDER, NAME, 1, "admin-openid-1").status_code == 200)

# A10 绑定成员：openid 绑定了成员名 ⇒ 能看到该成员名下的本地单
_orig_bound = backend._get_bound_owner_name
backend._get_bound_owner_name = lambda oid: "" if oid == STRANGER else (
    "霍楠" if oid == "member-openid-1" else _orig_bound(oid))
chk("A10 绑定成员能看到名下本地单（openid 不是下单人也能看）",
    info(ORDER, NAME, "member-openid-1").status_code == 200)
chk("A10 未绑定的人依旧 403（绑定名不匹配就不放行）",
    info(ORDER, NAME, STRANGER).status_code == 403)
MK_CLOUD = "HN20261123-9002"
mk_order(MK_CLOUD, openid="cloud-owner", source="cloud", owner="霍楠")
upload(MK_CLOUD, NAME, 1, IMG1)
chk("A10 云端单不吃绑定名（只见 source='local' 的单）",
    info(MK_CLOUD, NAME, "member-openid-1").status_code == 403)
backend._get_bound_owner_name = _orig_bound

# A11 未登录 / 缺参
chk("A11 不带 token 问 info → 401「未登录」",
    client.get("/api/local_order_thumb/info",
               query_string={"order_number": ORDER, "name": NAME}).status_code == 401)
chk("A11 缺 order_number → 400",
    client.get("/api/local_order_thumb/info",
               query_string={"name": NAME, "token": tok(OWNER)}).status_code == 400)
chk("A11 上传缺 name → 400", client.post(
    "/api/local/order_thumb", headers=HDR,
    json={"order_number": ORDER, "page": 1, "data": base64.b64encode(IMG1).decode()}).status_code == 400)
chk("A11 上传不带 printer token → 403",
    client.post("/api/local/order_thumb", json={
        "order_number": ORDER, "name": NAME, "page": 1,
        "data": base64.b64encode(IMG1).decode()}).status_code == 403)

# A12 数据校验：非图 / 超限 / 坏 base64
r = upload(ORDER, NAME, 4, b"not an image at all")
chk("A12 非图片字节 → 400「只接受 JPEG / WebP」",
    r.status_code == 400 and "JPEG" in (r.get_json() or "").get("message", ""), r.get_json())
r = upload(ORDER, NAME, 4, b"\x00" * (backend._PREVIEW_UPLOAD_MAX_BYTES + 1024))
chk("A12 超过 512 KB → 400「图片过大」",
    r.status_code == 400 and "过大" in (r.get_json() or "").get("message", ""), r.get_json())
r = client.post("/api/local/order_thumb", headers=HDR, json={
    "order_number": ORDER, "name": NAME, "page": 4, "data": "@@@@"})
chk("A12 坏 base64 → 400（不是 500）", r.status_code == 400, r.status_code)
chk("A12 校验失败的页不会落盘", backend._local_thumb_find(ORDER, NAME, 4) == "")

# A13 JPEG 也收（服务端没有 WebP 支持时客户端会回落）
r = upload(ORDER, "纯文本说明.txt", 1, jpeg_bytes(), fmt="jpeg")
chk("A13 JPEG 字节 → 200，落盘是 .jpg",
    r.status_code == 200 and backend._local_thumb_find(ORDER, "纯文本说明.txt", 1).lower().endswith(".jpg"),
    backend._local_thumb_find(ORDER, "纯文本说明.txt", 1))
chk("A13 get_page 的 MIME 跟着真实格式走（webp / jpeg 都对）",
    get_page(ORDER, "纯文本说明.txt", 1, OWNER).headers.get("Content-Type", "").startswith("image/"))

# A14 清理：45 天前的目录被删；超过 900 个按 mtime 从旧到新删
_orig_max, _orig_keep = backend._LOCAL_THUMB_MAX_ORDERS, backend._LOCAL_THUMB_KEEP_DAYS
old_dir = backend._local_thumb_order_dir("HN20250101-0001", create=True)
old_file = os.path.join(old_dir, "x_p1.webp")
with open(old_file, "wb") as fh:
    fh.write(IMG1)
old_ts = time.time() - 60 * 86400
os.utime(old_dir, (old_ts, old_ts))
backend._local_thumb_prune_ts = 0.0
removed = backend._prune_local_thumbs()
chk("A14 45 天前的订单目录被清掉（订单不删，所以靠上传时顺手清）",
    removed >= 1 and not os.path.isdir(old_dir), f"removed={removed}")
backend._LOCAL_THUMB_MAX_ORDERS = 1
backend._local_thumb_prune_ts = 0.0
removed2 = backend._prune_local_thumbs()
now_dirs = [d for d in os.listdir(backend.LOCAL_THUMB_DIR)
            if os.path.isdir(os.path.join(backend.LOCAL_THUMB_DIR, d))]
chk("A14 超过上限时按 mtime 删到上限（本次上限压到 1 个）",
    removed2 >= 1 and len(now_dirs) <= 1, f"removed={removed2} left={now_dirs}")
chk("A14 保留的是最近那个订单（不是刚被删的旧单）",
    not now_dirs or now_dirs[0] != "HN20250101-0001", now_dirs)
backend._LOCAL_THUMB_MAX_ORDERS = _orig_max
backend._LOCAL_THUMB_KEEP_DAYS = _orig_keep


# ═══════════════════ B. 客户端接线 ═══════════════════
print("\n=== B. 本地工具回传（order_thumbs.load_pages / cloud_client）===")

_TMP_APP = tempfile.mkdtemp(prefix="hn_localthumb_app_")
os.environ["APPDATA"] = _TMP_APP
sys.path.insert(0, LOCAL_TOOL)

import order_thumbs  # noqa: E402

NUM = "HN20261123-9101"
big = png_bytes(1024, 1400, "white")
for page in (1, 2, 3):
    order_thumbs.save_bytes(NUM, "报告.docx", big, "webp", page=page)

pages = order_thumbs.load_pages(NUM, "报告.docx")
chk("B1 load_pages 默认给前 6 页（实际只有 3 页就回 3 页）",
    len(pages) == 3 and [p[0] for p in pages] == [1, 2, 3], [p[0] for p in pages])
def _edge_of(data):
    from PIL import Image
    return max(Image.open(io.BytesIO(data)).size)


chk("B1 每页都缩到 1024 以内（2026-11-24 起：移动端可全屏放大，640 撑满屏幕偏糊）",
    all(_edge_of(d) <= 1024 for _, d, _ in pages)
    and all(len(d) < len(big) for _, d, _ in pages),
    [(_edge_of(d), len(d)) for _, d, _ in pages])
chk("B1 返回 (page_no, bytes, fmt) 三元组，fmt 是 webp 或 jpeg",
    all(isinstance(p, tuple) and len(p) == 3 and p[2] in ("webp", "jpeg") for p in pages),
    [p[2] for p in pages])
chk("B1 limit=2 只给两页（详情卡最多铺 6 页，别把整单都传上去）",
    len(order_thumbs.load_pages(NUM, "报告.docx", limit=2)) == 2)
chk("B1 没有缩略图的单回空列表（绝不抛异常给打印主流程）",
    order_thumbs.load_pages("HN20261123-9999", "x.docx") == [])

import cloud_client  # noqa: E402

calls = []


class _Resp(object):
    status_code = 200

    @staticmethod
    def json():
        return {"success": True}


class _FakeHTTP(object):
    @staticmethod
    def post(url, headers=None, timeout=None, json=None):
        calls.append((url, dict(json or {})))
        return _Resp()


_orig_http = cloud_client.http_requests
cloud_client.http_requests = _FakeHTTP
os.environ.pop("HN_CLOUD_THUMB", None)


class _Client(object):
    api_url = "https://api.test"
    _auth_headers = lambda self: {"Authorization": "Bearer t"}  # noqa: E731


cnt = cloud_client.CloudClient.upload_order_thumb_pages(
    _Client(), NUM, "报告.docx", order_thumbs.load_pages(NUM, "报告.docx"), label="t")
chk("B2 逐页回传 /api/local/order_thumb，返回成功页数",
    cnt == 3 and len(calls) == 3 and all(u == "https://api.test/api/local/order_thumb" for u, _ in calls),
    f"cnt={cnt} urls={set(u for u, _ in calls)}")
chk("B2 键是 (订单号, 文件名, 页号) —— 不带 file_id（本地单没有云端文件行）",
    all(j.get("order_number") == NUM and j.get("name") == "报告.docx"
        and j.get("page") in (1, 2, 3) and "file_id" not in j for _, j in calls),
    calls[0][1] if calls else None)
calls.clear()
os.environ["HN_CLOUD_THUMB"] = "0"
chk("B2 HN_CLOUD_THUMB=0 时一页都不传（现场排障开关）",
    cloud_client.CloudClient.upload_order_thumb_pages(
        _Client(), NUM, "报告.docx", order_thumbs.load_pages(NUM, "报告.docx")) == 0
    and not calls)
os.environ.pop("HN_CLOUD_THUMB", None)
chk("B2 空参数直接 0（不拼 URL、不发请求）",
    cloud_client.CloudClient.upload_order_thumb_pages(_Client(), "", "", []) == 0 and not calls)
cloud_client.http_requests = _orig_http

# B3 接线静态：只有真的连着云端才回传（离线时为几张图等 6×30s 超时没有意义）
GUI_SRC = open(os.path.join(LOCAL_TOOL, "gui.py"), encoding="utf-8", errors="replace").read()
_thumb_worker = GUI_SRC.split("def _thumb_worker(")[1][:4000] if "def _thumb_worker(" in GUI_SRC else ""
chk("B3 打印收尾的缩略图线程会调 upload_order_thumb_pages",
    "upload_order_thumb_pages(" in _thumb_worker)
chk("B3 回传前先判 _connected（离线直接跳过，不阻塞主流程）",
    '_connected' in _thumb_worker
    and _thumb_worker.index("_connected") < _thumb_worker.index("upload_order_thumb_pages("))
chk("B3 回传放在 render_pages 成功之后（没渲出来就没得传）",
    _thumb_worker.index("render_pages(") < _thumb_worker.index("upload_order_thumb_pages("))

print()
if ok_all:
    print("✅ 全部通过")
else:
    print("❌ 有失败项")
sys.exit(0 if ok_all else 1)
