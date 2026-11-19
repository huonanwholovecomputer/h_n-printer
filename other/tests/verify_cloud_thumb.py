# -*- coding: utf-8 -*-
"""本地生成订单文件首页缩略图（2026-11-19 阶段二）。

为什么值得一条独立验收：缩略图要覆盖**服务端根本渲不了**的类型（txt/csv/md），
而这些恰恰最容易被「顺手写个 except: pass」静默吞掉 —— 那种失败用户看不出来，
只会觉得「点了没反应」。这里逐类型真跑一遍，并钉住「失败必须返回 None 而不是抛」。

钉住的契约：
  ① 图片：长边缩到 640，输出是真 WebP（Pillow 有 WebP 时）或 JPEG
  ② PDF：只渲第 1 页，长边 640
  ③ 纯文本：排出一页可辨认的内容（不是纯白图）
  ④ Office：**没有**已转换的 PDF 就返回 None（绝不为缩略图去起 Word/WPS/LibreOffice）
  ⑤ 坏文件/未知类型不抛异常，只返回 None
  ⑥ 上传接线：下载成功与缓存命中两条路都要回传；HN_CLOUD_THUMB=0 可一键关掉
  ⑦ 后端契约：print_task 载荷带 file_id，/api/local/file_thumb 只收首页
"""
import io
import os
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, "local_print_tool"))

ok_all = True


def check(name, cond, detail=""):
    global ok_all
    if not cond:
        ok_all = False
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


def sniff(data: bytes) -> str:
    if data[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if len(data) >= 12 and data[8:12] == b"WEBP":
        return "webp"
    return ""


def decode_size(data: bytes):
    from PIL import Image
    with Image.open(io.BytesIO(data)) as im:
        return im.size


tmp = tempfile.mkdtemp(prefix="hn_thumb_")
try:
    import cloud_thumb as T

    # ── ① 图片 ───────────────────────────────────────────────────────────
    from PIL import Image
    big = os.path.join(tmp, "big.png")
    Image.new("RGB", (1600, 1200), (30, 120, 200)).save(big)
    r = T.make_thumb(big, "大图.png")
    check("图片能出缩略图", bool(r), str(r and len(r[0])))
    if r:
        data, fmt = r
        check("图片缩略图格式与真实字节一致", sniff(data) == fmt, f"{fmt} vs {sniff(data)}")
        w, h = decode_size(data)
        check("图片长边缩到 640", max(w, h) == T.THUMB_EDGE, f"{w}x{h}")
        check("图片缩略图体积很小（<100KB）", len(data) < 100 * 1024, f"{len(data)}B")

    # ── ② PDF ────────────────────────────────────────────────────────────
    pdf = os.path.join(tmp, "doc.pdf")
    try:
        import fitz
        d = fitz.open()
        p = d.new_page(width=595, height=842)          # A4
        p.insert_text((72, 120), "HN PRINT 首页缩略图", fontsize=28)
        d.save(pdf)
        d.close()
        r = T.make_thumb(pdf, "文档.pdf")
        check("PDF 能出缩略图", bool(r))
        if r:
            data, fmt = r
            w, h = decode_size(data)
            check("PDF 缩略图是竖版 A4 比例且长边 640",
                  max(w, h) == T.THUMB_EDGE and h > w, f"{w}x{h}")
    except ImportError:
        check("PDF 缩略图（本机无 PyMuPDF，跳过）", True)

    # ── ③ 纯文本（服务端完全渲不了的类型）────────────────────────────────
    txt = os.path.join(tmp, "note.txt")
    with open(txt, "w", encoding="utf-8") as fh:
        fh.write("实验六 TTL 与 CMOS 集成逻辑门测试\n\n"
                 "一、实验目的\n测量与非门的高低电平阈值，验证 TTL 的噪声容限。\n"
                 + "\n".join(f"{i}. 第 {i} 行数据：VOH=3.6V, VOL=0.2V" for i in range(1, 40)))
    r = T.make_thumb(txt, "实验笔记.txt")
    check("txt 能出缩略图", bool(r))
    if r:
        data, fmt = r
        check("txt 缩略图格式正确", sniff(data) == fmt, f"{fmt} vs {sniff(data)}")
        w, h = decode_size(data)
        check("txt 缩略图长边 640 且竖版", max(w, h) == T.THUMB_EDGE and h > w, f"{w}x{h}")
        # 「排出了字」而不是一张白图：细笔画缩到 453px 后暗像素很少，
        # 所以判据是「确实有墨」+「不是整片黑」，不看固定百分比。
        with Image.open(io.BytesIO(data)) as im:
            gray = im.convert("L").resize((160, 226))
        lo, hi = gray.getextrema()
        dark = sum(1 for v in gray.getdata() if v < 128) / float(160 * 226)
        check("txt 缩略图有可辨认的文字（有暗像素但不整片黑）",
              lo < 120 and hi > 200 and dark < 0.6, f"lo={lo} dark={dark:.4f}")

    csv = os.path.join(tmp, "data.csv")
    with open(csv, "w", encoding="utf-8") as fh:
        fh.write("姓名,金额,状态\n孤Gu枭,3.14,已完成\n姚懿祥,0.70,已预约\n")
    check("csv 能出缩略图", bool(T.make_thumb(csv, "明细.csv")))

    empty = os.path.join(tmp, "empty.txt")
    open(empty, "w").close()
    check("空 txt 也能出图（写「空文件」而不是抛异常）", bool(T.make_thumb(empty, "空.txt")))

    # ── ④ Office：没有转换缓存 ⇒ 不出图 ───────────────────────────────────
    docx = os.path.join(tmp, "a.docx")
    with open(docx, "wb") as fh:
        fh.write(b"PK\x03\x04fake-docx")
    check("Office 没有已转换 PDF ⇒ None（不自己起 Office）",
          T.make_thumb(docx, "合同.docx") is None)
    if os.path.isfile(pdf):
        check("Office 有已转换 PDF ⇒ 借那张 PDF 出图",
              bool(T.make_thumb(docx, "合同.docx", pdf_path=pdf)))

    # ── ⑤ 坏文件 / 未知类型 ───────────────────────────────────────────────
    junk = os.path.join(tmp, "junk.bin")
    with open(junk, "wb") as fh:
        fh.write(b"\x00\x01\x02 not an image")
    try:
        r = T.make_thumb(junk, "垃圾.bin")
        check("未知类型不抛异常", r is None or len(r[0]) > 0, str(r is None))
    except Exception as e:  # noqa: BLE001
        check("未知类型不抛异常", False, repr(e))
    check("不存在的路径返回 None", T.make_thumb(os.path.join(tmp, "nope.pdf")) is None)
    with open(os.path.join(tmp, "broken.pdf"), "wb") as fh:
        fh.write(b"not a pdf at all")
    try:
        check("坏 PDF 返回 None（不抛）", T.make_thumb(os.path.join(tmp, "broken.pdf")) is None)
    except Exception as e:  # noqa: BLE001
        check("坏 PDF 返回 None（不抛）", False, repr(e))

    # ── ⑥ 上传接线（源码级，避免真起 Qt）────────────────────────────────
    cc = open(os.path.join(REPO, "local_print_tool", "cloud_client.py"), encoding="utf-8").read()
    check("下载成功路径会回传缩略图",
          cc.count("self._spawn_thumb_upload(task, ") >= 2,
          f"{cc.count('self._spawn_thumb_upload(task, ')} 处")
    check("回传走 /api/local/file_thumb", '"/api/local/file_thumb"' in cc)
    check("可一键关闭（HN_CLOUD_THUMB=0）", '"HN_CLOUD_THUMB"' in cc)
    # 判据只认代码结构（不拿中文提示文案当契约，见 判据写法约定.md）
    check("缩略图失败不影响主流程（模块导入失败也吞）",
          "except Exception" in cc.split("from cloud_thumb import")[1][:900])
    check("Office 只借缓存 PDF、不自己起 Office",
          "pdf_path=pdf_path" in cc and "get_converter" not in cc.split("_upload_thumb")[1][:2600])

    # ── ⑦ 后端契约 ───────────────────────────────────────────────────────
    app_src = open(os.path.join(REPO, "mobile_apps", "printer-backend", "app.py"),
                   encoding="utf-8").read()
    check("print_task 载荷带 file_id",
          '"file_id": file_id,' in app_src and '"file_url": download_url,' in app_src)
    check("后端有 /api/local/file_thumb 且只要首页",
          '@app.route("/api/local/file_thumb", methods=["POST"])' in app_src)
    check("后端拒收服务端编不出的 WebP", "_preview_webp_ready()" in app_src)
finally:
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)

print()
print("全部通过 ✅" if ok_all else "有失败 ❌")
sys.exit(0 if ok_all else 1)
