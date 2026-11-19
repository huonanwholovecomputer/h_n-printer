# -*- coding: utf-8 -*-
"""验收：hairline（stroke width = 0）细线 PDF 不能被单色硬阈值打成一页白纸。

现场（2026-11-19）：用户拿 CAD 导出的 `Drawing1(4)(cad2pdf).pdf` 打印，纸几乎全白，
只剩中心几个圆圈；同一文件 Edge 打出来是完整的。根因是 MuPDF 抗锯齿把 1 设备像素
宽的线摊到相邻 2 个像素上（每像素覆盖率 30%~45%，灰度 140~180），而单色路径的硬阈值
是 136 ⇒ 所有细线被判成白。修法：渲染前把 MuPDF 最小线宽抬到 1.6 设备像素
（`pdf_printer.MONO_MIN_LINE_WIDTH`），缩略图抬到 1.0。

本脚本两层：
  A 渲染层 —— 合成一张 hairline PDF，比较「不抬线宽」与「抬线宽」的墨迹覆盖率，
    并直接断言直线/圆在阈值化后是**连续**的（不是断点虚线）。
  B 接线层 —— 打印与缩略图两条路径都真的套上了这个上下文，且退出时会恢复原值。
"""
import io
import os
import re
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LOCAL_TOOL = os.path.join(REPO, "local_print_tool")
sys.path.insert(0, LOCAL_TOOL)

PASSED = 0
FAILED = []


def check(name, cond, detail=""):
    global PASSED
    if cond:
        PASSED += 1
        print(f"[PASS] {name}" + (f"  — {detail}" if detail else ""))
    else:
        FAILED.append(name)
        print(f"[FAIL] {name}" + (f"  — {detail}" if detail else ""))


def src(path):
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


# ── A. 渲染层 ────────────────────────────────────────────────────────────────
import fitz  # noqa: E402
import pdf_printer as P  # noqa: E402

DPI = 600
PAGE_W, PAGE_H = 596.0, 843.0


def make_hairline_pdf(path):
    """一张"工程图"：外框 + 内框 + 十字靶标 + 一条长对角线，全部 stroke width = 0。"""
    doc = fitz.open()
    page = doc.new_page(width=PAGE_W, height=PAGE_H)
    # ⚠ 必须自己写内容流：fitz 的 `shape.finish(width=0.0)` 不会真的落 `0 w`
    # （PyMuPDF 会把 0 当"默认线宽"补成 1），拿它做样本根本复现不了现场。
    # 这里是逐字节的 `0 w`，与 CAD 导出（Drawing1(4)(cad2pdf).pdf 的 430 条 0 宽路径）同一形态。
    ops = ["0 G", "0 w"]
    for inset in (40, 90):                        # 外框 / 内框
        ops.append(f"{inset} {inset} {PAGE_W - 2 * inset:.2f} {PAGE_H - 2 * inset:.2f} re S")
    for row in range(3):                          # 12 个十字靶标（方框 + 横竖各一条）
        for col in range(4):
            cx = 90 + col * 110
            cy = 150 + row * 110
            half = 34
            ops.append(f"{cx - half} {cy - half} {2 * half} {2 * half} re S")
            ops.append(f"{cx - 55} {cy} m {cx + 55} {cy} l S")
            ops.append(f"{cx} {cy - 55} m {cx} {cy + 55} l S")
    ops.append(f"60 780 m {PAGE_W - 60:.2f} 60 l S")     # 贯穿页面的对角线
    xref = doc.get_new_xref()
    doc.update_object(xref, "<<>>")
    doc.update_stream(xref, ("\n".join(ops) + "\n").encode("latin-1"))
    doc.xref_set_key(page.xref, "Contents", f"{xref} 0 R")
    doc.save(path)
    doc.close()


def render_gray(path, min_width):
    doc = fitz.open(path)
    try:
        page = doc.load_page(0)
        mat = fitz.Matrix(DPI / 72.0, DPI / 72.0)
        with P._MinLineWidth(min_width):
            pix = page.get_pixmap(matrix=mat, colorspace=fitz.csGRAY, alpha=False)
        return pix
    finally:
        doc.close()


def to_img(pix):
    from PIL import Image
    # frombuffer 是零拷贝视图：pixmap 释放后那块内存会被下一次渲染复用 ⇒ 必须 copy，
    # 否则"抬线宽前/后"两张图读到的是同一份像素（对比全部失效）。
    return Image.frombuffer("L", (pix.width, pix.height), pix.samples, "raw", "L", pix.stride, 1).copy()


def ink_ratio(img):
    hist = img.histogram()
    black = sum(hist[:136])
    return black / float(max(1, img.width * img.height))


def run_pixels(img):
    """沿某一行统计最长连续黑像素段（判断线是不是被打成虚线）。"""
    w, h = img.size
    best = 0
    for y in range(0, h, 7):                      # 抽样扫行，别太慢
        run = 0
        for x in range(w):
            if img.getpixel((x, y)) < 136:
                run += 1
                best = max(best, run)
            else:
                run = 0
    return best


def col_run_pixels(img):
    w, h = img.size
    best = 0
    for x in range(0, w, 7):
        run = 0
        for y in range(h):
            if img.getpixel((x, y)) < 136:
                run += 1
                best = max(best, run)
            else:
                run = 0
    return best


def main():
    tmp = tempfile.mkdtemp(prefix="hn_hairline_")
    pdf = os.path.join(tmp, "hairline.pdf")
    make_hairline_pdf(pdf)

    pix0 = render_gray(pdf, 0.0)
    pix1 = render_gray(pdf, P.MONO_MIN_LINE_WIDTH)
    img0 = P._to_bilevel(to_img(pix0))
    img1 = P._to_bilevel(to_img(pix1))
    r0, r1 = ink_ratio(img0), ink_ratio(img1)

    check("A1 复现现场：不抬线宽时 hairline 几乎全丢（墨迹 < 0.2%）", r0 < 0.002,
          f"ink={r0 * 100:.4f}%")
    # r0 可能正好是 0（整页白纸，就是现场那张）——倍数就没意义了，只要求"真的有线"
    _ratio = "整页白 →" if r0 <= 1e-6 else f"{r1 / r0:.1f} 倍"
    check("A2 抬到 MONO_MIN_LINE_WIDTH 后墨迹显著回来（≥ 0.3%，且不靠阈值边缘）",
          r1 >= 0.003 and (r0 <= 1e-6 or r1 >= r0 * 8),
          f"{r0 * 100:.4f}% → {r1 * 100:.4f}%（{_ratio}）")
    check("A3 外框长直线在阈值化后是连续的（≥ 300 px 不断）", run_pixels(img1) >= 300,
          f"最长连续 {run_pixels(img1)} px")
    check("A4 竖线同样连续（十字靶标的竖笔不能断）", col_run_pixels(img1) >= 300,
          f"最长连续 {col_run_pixels(img1)} px")
    check("A5 线没有被抬粗到糊成一片（墨迹仍 < 5%）", r1 < 0.05, f"ink={r1 * 100:.3f}%")

    # 现场那两张真实文件（存在才测；不存在就跳过，不把别人机器的路径写死）
    real = os.path.join(os.path.expanduser("~"), "Documents")

    def find_doc(match):
        for root, dirs, files in os.walk(real):
            for fn in files:
                if match(fn.lower()):
                    return os.path.join(root, fn)
        return None

    def real_case(tag, label, found):
        if not found:
            print(f"[SKIP] {tag} 本机没有现场那张 {label}，跳过真实文件对比")
            return
        g0 = P._to_bilevel(to_img(render_gray(found, 0.0)))
        g1 = P._to_bilevel(to_img(render_gray(found, P.MONO_MIN_LINE_WIDTH)))
        a, b = ink_ratio(g0), ink_ratio(g1)
        check(f"{tag} 现场 {label}：修复前几乎全白、修复后线条全回来（≥ 8 倍）",
              b >= a * 8 and b >= 0.002,
              f"{a * 100:.4f}% → {b * 100:.4f}%  ({os.path.basename(found)})")

    real_case("A6", "CAD 图 Drawing1*cad2pdf*.pdf",
              find_doc(lambda n: n.startswith("drawing1") and n.endswith(".pdf") and "cad2pdf" in n))
    # 武.pdf：另一张 CAD 导出（A4 横向、104 条 0 宽路径），同一晚同一根因
    real_case("A7", "CAD 图 武.pdf",
              find_doc(lambda n: n == "武.pdf"))

    # ── B. 接线层 ────────────────────────────────────────────────────────────
    pp = src(os.path.join(LOCAL_TOOL, "pdf_printer.py"))
    check("B1 打印路径：mono 且非抖动页才抬线宽（抖动页是灰阶内容，不该加粗）",
          "MONO_MIN_LINE_WIDTH if (mono and not _dither) else 0.0" in pp)
    check("B2 打印路径：get_pixmap 真的在那个上下文里",
          re.search(r"with _MinLineWidth\(_lw\):\s*\n\s*pix = page\.get_pixmap", pp) is not None)
    check("B3 常量与注释写明设备像素语义", "MONO_MIN_LINE_WIDTH = 1.6" in pp
          and "设备像素" in pp)

    # 上下文必须恢复原值：MuPDF 的这个设置是进程级全局
    before = fitz.TOOLS.show_aa_level()["graphics_min_line_width"]
    with P._MinLineWidth(1.6):
        inside = fitz.TOOLS.show_aa_level()["graphics_min_line_width"]
    after = fitz.TOOLS.show_aa_level()["graphics_min_line_width"]
    check("B4 进出上下文后恢复原值（不把后续渲染全都画粗）",
          abs(inside - 1.6) < 1e-6 and abs(after - before) < 1e-6,
          f"{before} → {inside} → {after}")

    try:
        with P._MinLineWidth(1.6):
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    check("B5 上下文内抛异常也会恢复原值",
          abs(fitz.TOOLS.show_aa_level()["graphics_min_line_width"] - before) < 1e-6)

    class _Boom:
        pass
    # 宽度 <= 0 时必须是空操作（不抢锁、不改全局）
    with P._MinLineWidth(0):
        check("B6 宽度为 0 时不改全局设置",
              abs(fitz.TOOLS.show_aa_level()["graphics_min_line_width"] - before) < 1e-6)

    ct = src(os.path.join(LOCAL_TOOL, "cloud_thumb.py"))
    check("B7 缩略图路径也抬了线宽（CAD 缩略图否则一片白）",
          "with _min_line_width(1.0):" in ct and "_from_pdf" in ct)

    # 缩略图实测：同一张 hairline PDF，出图后暗像素占比要够
    import cloud_thumb
    res = cloud_thumb._from_pdf(pdf)
    ok_thumb = False
    detail = "无输出"
    if res:
        from PIL import Image
        data, fmt = res
        im = Image.open(io.BytesIO(data)).convert("L")
        dark = sum(1 for v in im.getdata() if v < 128) / float(max(1, im.width * im.height))
        ok_thumb = dark >= 0.001
        detail = f"{im.size} {fmt} 暗像素 {dark * 100:.3f}%"
    check("B8 缩略图里 hairline 看得见（暗像素 ≥ 0.1%）", ok_thumb, detail)

    print()
    if FAILED:
        print(f"❌ 失败 {len(FAILED)} 项：{FAILED}")
        return 1
    print(f"Hairline 细线保真验收：全部通过 ✅  （PASS {PASSED}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
