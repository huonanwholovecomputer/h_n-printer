# -*- coding: utf-8 -*-
"""客户端回传首页缩略图 —— 阶段二（2026-11-19）。

本地打印工具把订单文件转成 PDF 打印时，顺手把首页光栅化成 640px WebP/JPEG 上传，
服务端直接落进 GET 会先查的那条缓存路径 ⇒ 不再自己起 soffice 渲染，并且覆盖
服务端**根本不会渲**的类型（txt/csv/md —— 「纯文本没有首页」）。

钉住的契约：
  ① 只收首页（page=1）、只收 JPEG/WebP（按真实字节判，不信请求里的 fmt）
  ② 落盘名与 GET 的缓存路径一致 ⇒ 上传完 GET 立刻命中，不再走渲染
  ③ 服务端没有 WebP 编码能力时拒收 WebP（415），不静默落一张别人打不开的图
  ④ txt 这类「服务端不会渲」的类型：有 t/p 档文件就放行 415 闸门、GET 直接发那张
  ⑤ 只服务第 1 页 —— 拿首页冒充第 2 页比没有更糟
  ⑥ 老的多页缓存 `<stem>_p2.jpg` 不能被误认成 p 档（只差一个字符）
  ⑦ `_drop_previews` 按前缀清理，档位文件跟文件保留期一起删
  ⑧ 负缓存（渲染失败 600s）在成功后要清掉，否则上传了还是回图标
"""
import io
import json
import os
import shutil
import sys
import tempfile
import traceback

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SRC = os.path.join(REPO, "mobile_apps", "printer-backend")

ok_all = True


def check(name, cond, detail=""):
    global ok_all
    if not cond:
        ok_all = False
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


def png_jpeg_webp():
    """生成三种真实字节的小图（WebP 不可用时给 None）。"""
    from PIL import Image

    def enc(fmt, **kw):
        buf = io.BytesIO()
        Image.new("RGB", (64, 96), (200, 40, 40)).save(buf, fmt, **kw)
        return buf.getvalue()

    webp = None
    try:
        webp = enc("WEBP", quality=80)
    except Exception:  # noqa: BLE001  本机 Pillow 没 WebP 编码时跳过相关断言
        webp = None
    return enc("JPEG", quality=82), webp, enc("PNG")


tmp = tempfile.mkdtemp(prefix="hn_preview_thumb_")
dst = os.path.join(tmp, "printer-backend")
shutil.copytree(SRC, dst, ignore=shutil.ignore_patterns(
    "__pycache__", "orders.db", "users.db", "uploads", "logs", "venv", "*.bak-*"))
sys.path.insert(0, dst)
os.chdir(dst)

try:
    import app as backend
except Exception:  # noqa: BLE001
    traceback.print_exc()
    sys.exit("无法导入 app.py")

backend.init_db()
backend.PRINTER_TOKEN = "probe-tok"
client = backend.app.test_client()
HDR = {"X-Printer-Token": "probe-tok", "X-Client-Id": "probe-client"}

JPEG_BYTES, WEBP_BYTES, PNG_BYTES = png_jpeg_webp()
HAS_WEBP = WEBP_BYTES is not None


def make_txt_file(file_id="f-txt-001", name="笔记.txt", text="第一行\n第二行\n",
                  openid="openid-probe"):
    rel = os.path.join("uploads", "txt", f"{file_id}.txt")
    os.makedirs(os.path.dirname(rel), exist_ok=True)
    with open(rel, "w", encoding="utf-8") as fh:
        fh.write(text)
    conn = backend.get_db()
    conn.execute(
        "INSERT OR REPLACE INTO files (id, original_name, saved_name, path, size, created_at, openid)"
        " VALUES (?,?,?,?,?,?,?)",
        (file_id, name, f"{file_id}.txt", rel, os.path.getsize(rel),
         "2026-11-19 12:00:00", openid),
    )
    conn.commit()
    conn.close()
    return rel


def thumb(data, size="p", fmt="webp", page=1, headers=None):
    body = {"file_id": "f-txt-001", "page": page, "size": size, "fmt": fmt,
            "data": None if data is None else __import__("base64").b64encode(data).decode("ascii")}
    return client.post("/api/local/file_thumb", data=json.dumps(body),
                       headers=headers if headers is not None else HDR,
                       content_type="application/json")


def get_preview(size="p", fmt="webp", page=1):
    """管理端入口（printer token admin scope）—— 与用户端共用同一份 _preview_page_response。"""
    return client.get(f"/api/admin/file_preview/f-txt-001?size={size}&fmt={fmt}&page={page}",
                      headers=HDR)


def get_preview_user(size="p", fmt="webp", page=1):
    """用户端入口（小程序/APP 真实走的那条）。"""
    tok = backend.token_serializer.dumps("openid-probe")
    return client.get(f"/api/file_preview/f-txt-001?size={size}&fmt={fmt}&page={page}"
                      f"&token={tok}")


# ── 0. 前置 ────────────────────────────────────────────────────────────────
make_txt_file()
check("服务端 WebP 能力与测试图一致",
      backend._preview_webp_ready() == HAS_WEBP,
      f"server={backend._preview_webp_ready()} test={HAS_WEBP}")
check("txt 不在服务端可渲染类型里", not backend._has_preview_for("笔记.txt"))
check("上传前 txt 的 GET 是 415（闸门）", get_preview().status_code == 415,
      str(get_preview().status_code))

# ── 1. 鉴权 / 参数校验 ─────────────────────────────────────────────────────
check("无 printer token 拒绝", client.post("/api/local/file_thumb", json={}).status_code in (401, 403))
check("缺 file_id → 400",
      client.post("/api/local/file_thumb", json={"data": "AA=="},
                  headers=HDR, content_type="application/json").status_code == 400)
check("page=2 不收（只认首页）", thumb(JPEG_BYTES, page=2).status_code == 400)
check("坏 base64 → 400", thumb(b"not-base64", fmt="jpeg").status_code == 400)
check("超 512KB → 400", thumb(b"\xff\xd8\xff" + b"0" * (backend._PREVIEW_UPLOAD_MAX_BYTES + 16),
                             fmt="jpeg").status_code == 400)
check("PNG 不收（只认 JPEG/WebP）", thumb(PNG_BYTES, fmt="jpeg").status_code == 400)
check("不存在的文件 → 404",
      client.post("/api/local/file_thumb",
                  json={"file_id": "nope", "page": 1, "size": "p", "fmt": "jpeg",
                        "data": __import__("base64").b64encode(JPEG_BYTES).decode("ascii")},
                  headers=HDR, content_type="application/json").status_code == 404)

# ── 2. 正常回传 + GET 命中 ─────────────────────────────────────────────────
r = thumb(JPEG_BYTES, size="p", fmt="jpeg")
check("JPEG 上传成功", r.status_code == 200 and r.get_json().get("success") is True, r.get_data(as_text=True))
check("回执 fmt 按真实字节（请求填 webp 也认 jpeg）",
      thumb(JPEG_BYTES, size="p", fmt="webp").get_json().get("fmt") == "jpeg")

r = get_preview(size="p", fmt="jpeg")
check("上传后 GET 立刻命中（txt 不再是 415）", r.status_code == 200, str(r.status_code))
check("GET 返回的就是上传的那张字节", r.data == JPEG_BYTES, f"{len(r.data)}B")
check("GET 的 MIME 是 image/jpeg", r.mimetype == "image/jpeg", r.mimetype)
check("GET 带 X-Preview-Pages（首页=1）", r.headers.get("X-Preview-Pages") == "1",
      str(r.headers.get("X-Preview-Pages")))

if HAS_WEBP:
    r = thumb(WEBP_BYTES, size="t", fmt="webp")
    check("WebP 上传成功", r.status_code == 200 and r.get_json().get("fmt") == "webp",
          r.get_data(as_text=True))
    r = get_preview(size="t", fmt="webp")
    check("t 档 WebP GET 命中且 MIME 正确",
          r.status_code == 200 and r.data == WEBP_BYTES and r.mimetype == "image/webp",
          f"{r.status_code} {r.mimetype} {len(r.data)}B")
    r = get_preview(size="z", fmt="webp")
    check("z 档没传过就退回已传档位（先同格式，再任意格式）",
          r.status_code == 200 and r.data in (WEBP_BYTES, JPEG_BYTES)
          and r.mimetype == ("image/webp" if r.data == WEBP_BYTES else "image/jpeg"),
          f"{r.status_code} {r.mimetype} {len(r.data)}B")

check("第 2 页不拿首页顶替", get_preview(page=2).status_code != 200,
      str(get_preview(page=2).status_code))

r = get_preview_user(size="p", fmt="jpeg")
check("用户端入口（?token=）同样命中上传的图",
      r.status_code == 200 and r.data == JPEG_BYTES, f"{r.status_code} {len(r.data)}B")
r = client.get("/api/file_preview/f-txt-001?size=p&fmt=jpeg&token=" +
               backend.token_serializer.dumps("openid-other"))
check("不是本人且非管理员 → 403（没被缩略图绕过）", r.status_code == 403, str(r.status_code))

# ── 2b. 元信息端点：有客户端缩略图就报「1 页」────────────────────────────
tok = backend.token_serializer.dumps("openid-probe")
r = client.get(f"/api/file_preview/f-txt-001/info?token={tok}")
body = r.get_json() or {}
check("有客户端缩略图的 txt，/info 报 1 页（前端才敢开 swiper）",
      r.status_code == 200 and body.get("pages") == 1, f"{r.status_code} {body}")

# ── 3. 缓存文件名 / 前缀识别 ───────────────────────────────────────────────
mt = str(int(os.path.getmtime(os.path.join("uploads", "txt", "f-txt-001.txt"))))
p_cache = backend._preview_cache_path("f-txt-001", mt, 1, "p", "jpeg")
check("落盘路径 = GET 的 p 档缓存路径", os.path.isfile(p_cache), p_cache)
check("档位文件被识别为客户端缩略图", backend._preview_tier_exists("f-txt-001"))

legacy = os.path.join(backend.PREVIEW_DIR, f"f-txt-001_{mt}_p2.jpg")
with open(legacy, "wb") as fh:
    fh.write(JPEG_BYTES)
check("老的多页缓存 _p2.jpg 不被误认成 p 档",
      not backend._PREVIEW_TIER_RE.search(os.path.basename(legacy)))
check("老的多页缓存仍算「有缩略图」（同一前缀，_drop_previews 会一起清）",
      backend._preview_tier_exists("f-txt-001"))

# ── 4. 负缓存清理 ──────────────────────────────────────────────────────────
backend._preview_fail_cache[("f-txt-001", mt, 1)] = (9e18, "旧失败")
thumb(JPEG_BYTES, size="p", fmt="jpeg")
check("上传成功后清掉负缓存", ("f-txt-001", mt, 1) not in backend._preview_fail_cache)

# ── 5. 保留期同删 ──────────────────────────────────────────────────────────
before = backend._preview_tier_files("f-txt-001")
backend._drop_previews("f-txt-001")
after = backend._preview_tier_files("f-txt-001")
check("_drop_previews 把档位文件一起清掉", before and not after,
      f"{len(before)} -> {len(after)}")
check("清理后 GET 又回到 415", get_preview().status_code == 415)

# ── 6. 列表口径（源码级：两处 has_preview 都要认客户端缩略图）──────────────
with open(os.path.join(dst, "app.py"), encoding="utf-8") as fh:
    src = " ".join(fh.read().split())          # 折掉换行，多行表达式才好做子串判断
check("my_files 的 has_preview 认客户端缩略图",
      '_has_preview_for(name_or_path) or _preview_tier_exists(row["id"])' in src)
check("订单明细的 has_preview 认客户端缩略图",
      '(_has_preview_for(of_row["file_name"] or "") or _preview_tier_exists(of_row["file_id"]))'
      in src)

shutil.rmtree(tmp, ignore_errors=True)
print()
print("全部通过 ✅" if ok_all else "有失败 ❌")
sys.exit(0 if ok_all else 1)
