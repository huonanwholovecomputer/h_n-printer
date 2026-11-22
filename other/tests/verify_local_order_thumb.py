# -*- coding: utf-8 -*-
"""验收 · 本地订单缩略图（v7.27，2026-11-19）

现场问题（用户反馈 ②）：收支清算的订单明细里，点文件名看缩略图只对「云端单」有效 ——
本地单上报 /api/local/orders 时 file_id 一律为 NULL，那几行 order_files 是后端现建的，
于是**本机手动打印的单永远点不出图**。

修法：本地打印工具在打印结束时把首页渲成 640px WebP 存在
%APPDATA%\\HN打印工具\\order_thumbs\\<订单号>\\，结算页改走
`/api/local/order_thumb/<订单号>?name=<文件名>`（本地路由、不出站、离线可用）。

本脚本验三层：
  A. order_thumbs.py 纯逻辑（键、路径净化、落盘、按名查找、渲染图/文本、失败不抛）
  B. stats_server 的真实路由（令牌门、200 带 X-Preview-*、404 给人话、路径注入不外泄）
  C. 接线（gui 打印收尾入队、settlement.html 的云端/本地区分、关于页版本号）

跑法：python other/tests/verify_local_order_thumb.py   （退出码 0 = 全绿）
"""
import io
import os
import re
import shutil
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
LOCAL_TOOL = os.path.join(ROOT, "local_print_tool")

# 把用户数据目录指到临时目录：order_thumbs 落在 %APPDATA% 下，绝不能污染真机数据
_TMP_ROOT = tempfile.mkdtemp(prefix="hn_thumb_")
os.environ["APPDATA"] = _TMP_ROOT

sys.path.insert(0, LOCAL_TOOL)
sys.path.insert(0, HERE)

import paths          # noqa: E402
import order_thumbs   # noqa: E402
import cloud_thumb    # noqa: E402

PASSED = 0
FAILED = 0


def chk(name, cond, detail=""):
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print("[PASS] " + name)
    else:
        FAILED += 1
        print("[FAIL] " + name + ("  -- " + str(detail) if detail != "" else ""))


def section(title):
    print("")
    print("=" * 78)
    print(title)
    print("=" * 78)


def read_text(path):
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def make_png(path, w=90, h=140):
    """造一张真 PNG（cloud_thumb 的图片分支要真能解码）。"""
    from PIL import Image as _Image
    img = _Image.new("RGB", (w, h), (255, 255, 255))
    for x in range(0, w, 10):
        for y in range(0, h, 10):
            img.putpixel((x, y), (40, 90, 200))
    img.save(path, "PNG")
    return path


# ═══════════════════════════════════════════════════════════════════════════
# A. order_thumbs 纯逻辑
# ═══════════════════════════════════════════════════════════════════════════
def test_module():
    section("A. order_thumbs（本机缩略图仓库）")

    # A1 数据目录必须在 %APPDATA%\HN打印工具 下（不写程序目录）
    root = order_thumbs.thumbs_dir()
    chk("A1 缩略图根目录在用户数据目录下（order_thumbs）",
        os.path.isdir(root) and root.startswith(paths.get_app_data_dir()) and
        os.path.basename(root) == order_thumbs.DIR_NAME, root)

    # A2 name_key：大小写 / 路径分隔符 / 目录前缀都不影响同一个文件
    k1 = order_thumbs.name_key("报告.DOCX")
    k2 = order_thumbs.name_key("C:\\Users\\x\\Desktop\\报告.docx")
    k3 = order_thumbs.name_key("/home/u/报告.DOCX")
    chk("A2 name_key 对大小写与路径不敏感（同一文件同一键）",
        k1 == k2 == k3 and len(k1) == 20 and re.match(r"^[0-9a-f]{20}$", k1), (k1, k2, k3))

    # A3 订单目录净化：恶意订单号绝不能逃出根目录
    evil = order_thumbs.order_dir("../../etc/passwd")
    ok_dir = order_thumbs.order_dir("HN20261119-0040")
    chk("A3 订单号被净化，目录不逃出根目录",
        os.path.dirname(os.path.abspath(evil)) == os.path.abspath(root) and
        os.path.dirname(os.path.abspath(ok_dir)) == os.path.abspath(root) and
        os.path.basename(ok_dir) == "HN20261119-0040", (evil, ok_dir, root))

    # A4 落盘 + 按名查找（含大小写变体）
    data = b"RIFF\x00\x00\x00\x00WEBPVP8 " + b"x" * 40
    p = order_thumbs.save_bytes("HN20261119-0040", "第三次作业-选择结构1.docx", data, "webp")
    found = order_thumbs.find("HN20261119-0040", "第三次作业-选择结构1.docx")
    found_upper = order_thumbs.find("HN20261119-0040", "第三次作业-选择结构1.DOCX")
    chk("A4 save_bytes 落盘、find 能按名取回（大小写不敏感）",
        bool(p) and os.path.isfile(p) and p.endswith(".webp") and found == p == found_upper,
        (p, found, found_upper))
    chk("A4b 落盘内容与写入字节一致、没有 .part 残渣",
        read_bin(p) == data and not any(n.endswith(".part") for n in os.listdir(os.path.dirname(p))),
        os.listdir(os.path.dirname(p)))

    # A4c 多页：第 N 页带 _pN 后缀，find 只认请求的那页（v7.30）
    p2 = order_thumbs.save_bytes("HN20261119-0040", "第三次作业-选择结构1.docx", data, "webp",
                                 page=2)
    chk("A4c 第 2 页落盘成 <key>_p2.webp，find(page=2) 取回它",
        bool(p2) and p2.endswith("_p2.webp") and os.path.isfile(p2)
        and order_thumbs.find("HN20261119-0040", "第三次作业-选择结构1.docx", page=2) == p2,
        p2)
    chk("A4d 没存过的第 3 页返回空串（不拿第 2 页顶替）",
        order_thumbs.find("HN20261119-0040", "第三次作业-选择结构1.docx", page=3) == "")
    chk("A4e page_count 报已存页数（2 页）",
        order_thumbs.page_count("HN20261119-0040", "第三次作业-选择结构1.docx") == 2,
        order_thumbs.page_count("HN20261119-0040", "第三次作业-选择结构1.docx"))

    # A4f save_pages 重渲时先把旧页清掉（页数变少不许留残页）
    n0 = order_thumbs.save_pages("HN20261119-0040", "第三次作业-选择结构1.docx",
                                 [(1, data, "webp")])
    chk("A4f 重渲只剩 1 页：旧的第 2 页被清掉、page_count 回 1",
        n0 == 1
        and order_thumbs.find("HN20261119-0040", "第三次作业-选择结构1.docx", page=2) == ""
        and order_thumbs.page_count("HN20261119-0040", "第三次作业-选择结构1.docx") == 1)

    # A5 没写过的订单 / 文件名 → 空串（不抛）
    chk("A5 查不到时返回空串", order_thumbs.find("HN20261119-9999", "x.pdf") == "" and
        order_thumbs.find("", "x.pdf") == "")

    # A6 render_job：图片文件真能被渲染成缩略图
    img = make_png(os.path.join(_TMP_ROOT, "thumb_src.png"))
    made = order_thumbs.render_job("HN20261119-0041", "thumb_src.png", img, "")
    head = read_bin(made)[:12] if made else b""
    chk("A6 render_job 把 PNG 渲成 WebP/JPEG 并落盘",
        bool(made) and os.path.isfile(made) and (head[:4] == b"RIFF" or head[:2] == b"\xff\xd8"),
        (made, head))
    chk("A6b render_job 的产品能被 find 命中（结算页查得到同一张）",
        order_thumbs.find("HN20261119-0041", "thumb_src.png") == made)

    # A7 文本文件走文字排版分支（结算页也要能看 .txt/.md）
    txt = os.path.join(_TMP_ROOT, "notes.txt")
    with open(txt, "w", encoding="utf-8") as f:
        f.write("第一行\n第二行\n")
    made_txt = order_thumbs.render_job("HN20261119-0041", "notes.txt", txt, "")
    chk("A7 render_job 支持纯文本文件", bool(made_txt) and os.path.isfile(made_txt), made_txt)

    # A8 失败绝不抛（文件不存在 / 未知扩展名 / 空订单号）
    ok = True
    try:
        order_thumbs.render_job("HN20261119-0041", "不存在.docx",
                                os.path.join(_TMP_ROOT, "nope.docx"), "")
        order_thumbs.render_job("", "x.pdf", img, "")
        order_thumbs.render_order("HN20261119-0041", [None, 3, {"name": ""}])
    except Exception as e:                                              # noqa: BLE001
        ok = False
        detail = "%s: %s" % (type(e).__name__, e)
    chk("A8 渲染失败 / 空入参一律返回空值，绝不抛异常", ok, "" if ok else detail)

    # A9 render_order：已有图不重复渲染，返回成功张数
    files = [{"name": "thumb_src.png", "path": img}, {"name": "notes.txt", "path": txt}]
    n = order_thumbs.render_order("HN20261119-0042", files)
    n2 = order_thumbs.render_order("HN20261119-0042", files)
    chk("A9 render_order 批量渲染且幂等（第二次不重渲、仍算已就绪）",
        n == 2 and n2 == 2 and order_thumbs.order_has_thumbs("HN20261119-0042"), (n, n2))

    # A10 上限常量存在且 _prune 在本机数据上不炸
    ok = True
    try:
        order_thumbs._prune()
    except Exception as e:                                              # noqa: BLE001
        ok = False
        detail = "%s: %s" % (type(e).__name__, e)
    chk("A10 上限（单订单张数 / 总量）存在，_prune 可安全跑",
        ok and order_thumbs.MAX_PER_ORDER > 0 and order_thumbs.MAX_TOTAL_BYTES > 0,
        "" if ok else detail)

    # A11 换号（离线单 -L → 云端号）：目录跟着改名，旧目录不留空壳
    made = order_thumbs.render_job("HN20261119-0099-L", "换号文件.png", img, "")
    new_dir = order_thumbs.rename_order("HN20261119-0099-L", "HN20261120-0099")
    chk("A11 rename_order 把缩略图搬到新订单号下、旧目录删掉",
        bool(made) and bool(new_dir)
        and order_thumbs.find("HN20261120-0099", "换号文件.png", page=1) != ""
        and not os.path.isdir(os.path.dirname(made)),
        (made, new_dir))


def read_bin(path):
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        return b""


# ═══════════════════════════════════════════════════════════════════════════
# B. stats_server 真实路由
# ═══════════════════════════════════════════════════════════════════════════
def test_route():
    section("B. stats_server：GET /api/local/order_thumb/<订单号>?name=<文件名>")

    import http.client
    import json as _json
    import stats_server as ss

    data_file = os.path.join(_TMP_ROOT, "route", "print_data.json")
    os.makedirs(os.path.dirname(data_file), exist_ok=True)
    srv = ss.StatsServer(api_url="", token="route-token", port=0,
                         data_file=data_file, finance_mode="local")
    srv.start_in_thread()

    def get(path, with_token=True):
        # 请求行必须是 ASCII：中文文件名一律百分号编码（浏览器 fetch 也是这么发的）
        from urllib.parse import quote as _quote
        path = _quote(path, safe="/?=&%")
        conn = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=20)
        try:
            conn.putrequest("GET", path, skip_accept_encoding=True)
            if with_token:
                conn.putheader("X-Launch-Token", srv.launch_token)
            conn.endheaders()
            r = conn.getresponse()
            body = r.read()
            hdrs = {k.lower(): v for k, v in r.getheaders()}
            return r.status, hdrs, body
        finally:
            try:
                conn.close()
            except Exception:                                          # noqa: BLE001
                pass

    try:
        # 先备好一单的图（走模块真实落盘，与打印时同一条路径）
        img = make_png(os.path.join(_TMP_ROOT, "route_src.png"), 120, 160)
        made = order_thumbs.render_job("HN20261119-0040", "route_src.png", img, "")
        # v7.30：再补第 2 页（打印收尾时从打印 PDF 逐页落盘的样子）
        order_thumbs.save_bytes("HN20261119-0040", "route_src.png",
                                read_bin(made) or b"", "webp", page=2)

        # B1 没令牌 → 403（/api/* 统一门）
        st, hd, _ = get("/api/local/order_thumb/HN20261119-0040?name=route_src.png",
                        with_token=False)
        chk("B1 不带 X-Launch-Token → 403（与其它 /api/* 同一道门）", st == 403, st)

        # B2 有令牌 + 有图 → 200 image/*，带 X-Preview-Page/Pages=1/1
        st, hd, body = get("/api/local/order_thumb/HN20261119-0040?name=route_src.png")
        ctype = str(hd.get("content-type") or "")
        chk("B2 命中 → 200 图片字节（image/webp|jpeg）",
            st == 200 and ctype.startswith("image/") and len(body) == os.path.getsize(made),
            (st, ctype, len(body), made))
        chk("B2b 带 X-Preview-Page/Pages = 1/2（两页时页面要能翻）",
            hd.get("x-preview-page") == "1" and hd.get("x-preview-pages") == "2",
            {k: v for k, v in hd.items() if k.startswith("x-")})
        chk("B2c Cache-Control: no-store（重打后要看到新图）",
            hd.get("cache-control") == "no-store", hd.get("cache-control"))

        # B2d 第 2 页也走同一条路由（&page=N）
        st, hd, body = get("/api/local/order_thumb/HN20261119-0040?name=route_src.png&page=2")
        chk("B2d ?page=2 → 200 且 X-Preview-Page=2 / Pages=2",
            st == 200 and hd.get("x-preview-page") == "2"
            and hd.get("x-preview-pages") == "2" and len(body) > 0,
            (st, {k: v for k, v in hd.items() if k.startswith("x-")}, len(body)))
        st, hd, body = get("/api/local/order_thumb/HN20261119-0040?name=route_src.png&page=9")
        try:
            msg = _json.loads(body.decode("utf-8")).get("message", "")
        except Exception:                                              # noqa: BLE001
            msg = ""
        chk("B2e 不存在的第 9 页 → 404 且说明本地只有 2 页",
            st == 404 and "这一页没有本地缩略图" in msg, (st, msg))

        # B3 订单存在但文件名对不上 → 404 + 人话 message
        st, hd, body = get("/api/local/order_thumb/HN20261119-0040?name=别的文件.pdf")
        try:
            msg = _json.loads(body.decode("utf-8")).get("message", "")
        except Exception:                                              # noqa: BLE001
            msg = ""
        chk("B3 文件名对不上 → 404 + 能读懂的 message（页面 toast 用）",
            st == 404 and "缩略图" in msg, (st, msg))

        # B4 路径注入：订单号里塞 ../ 不能读到根目录外的文件，也不能 500
        st, hd, body = get("/api/local/order_thumb/..%2F..%2Fwindows%2Fwin.ini?name=win.ini")
        chk("B4 订单号注入 ../ → 404（不 500、不吐根目录文件）",
            st == 404 and b"[fonts]" not in body, (st, body[:80]))

        # B5 空订单号 → 400
        st, hd, body = get("/api/local/order_thumb/?name=x.pdf")
        chk("B5 空订单号 → 400", st == 400, st)
    finally:
        try:
            srv.stop()
        except Exception:                                              # noqa: BLE001
            pass


# ═══════════════════════════════════════════════════════════════════════════
# C. 接线
# ═══════════════════════════════════════════════════════════════════════════
def test_wiring():
    section("C. 接线（打印收尾入队 + 结算页取图 + 版本号）")

    gui_src = read_text(os.path.join(LOCAL_TOOL, "gui.py"))
    html = read_text(os.path.join(LOCAL_TOOL, "finance", "settlement.html"))

    # C1 每个任务打完都入队（不然只有"碰巧有 file_id"的单才有图）
    i = gui_src.find("def _on_job_finished")
    seg = gui_src[i:i + 900] if i >= 0 else ""
    chk("C1 _on_job_finished 收尾时把任务交给缩略图队列",
        "_queue_order_thumb(job)" in seg and "_report_job_result(job" in seg, seg[:200])

    # C2 渲染在后台线程（不能卡打印收尾）
    chk("C2 缩略图在后台线程渲染（hn-order-thumb，daemon）",
        'name="hn-order-thumb"' in gui_src and "threading.Thread(target=self._thumb_worker" in gui_src)
    chk("C2b 入队失败不影响打印（整段 try/except 且注释写明）",
        "缩略图是锦上添花" in gui_src and "def _queue_order_thumb" in gui_src)

    # C3 结算页：本地单不再只认 file_id
    chk("C3 结算页对「整单没有 file_id」的单降级到本地缩略图",
        "_anyCloudFile" in html and "_pvLocal" in html and "order_thumb/" in html)
    chk("C3b 取图 URL 按云端/本地分流（云端仍是 file_preview）",
        "/api/local/file_preview/" in html and "'/api/local/order_thumb/'" in html)

    # C4 兑现「每一单都要有」：本地单请求必须带订单号与文件名
    chk("C4 本地取图带订单号 + 文件名（encodeURIComponent 防注入）",
        "(m._fpOrder && m._fpOrder.order_number)" in html and
        "encodeURIComponent(f.file_name" in html)

    # C5 关于页版本号（单一来源；≥ v7.27 即可 —— 后续版本会继续往上走）
    m = re.search(r"var APP_VER = '([^']+)'", html)
    _ver = m.group(1) if m else ""
    chk("C5 关于页版本号 ≥ v7.27 且只此一处",
        bool(m) and _ver.startswith("v7.") and int(_ver[3:]) >= 27
        and html.count("var APP_VER") == 1,
        _ver or "missing")


def main():
    print("本地订单缩略图验收（v7.27）  ROOT=" + ROOT)
    order_thumbs._prune()   # 测试开始前清一次历史（临时目录，安全）
    test_module()
    test_route()
    test_wiring()

    print("")
    print("=" * 78)
    if FAILED == 0:
        print("本地订单缩略图验收：全部通过 ✅  （PASS %d / %d）" % (PASSED, PASSED + FAILED))
    else:
        print("本地订单缩略图验收：失败 ❌  （FAIL %d / %d）" % (FAILED, PASSED + FAILED))
    try:
        shutil.rmtree(_TMP_ROOT, ignore_errors=True)
    except Exception:                                                  # noqa: BLE001
        pass
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
