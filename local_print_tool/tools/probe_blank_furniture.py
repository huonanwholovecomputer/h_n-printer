"""取证：找出"只带页码/页眉"的末尾页在墨量上到底是什么量级。

用法：python tools/probe_blank_furniture.py [pdf]
不给路径时，在 %APPDATA%\\HN打印工具\\file_cache 里找 3 页、含"陕西省家庭经济困难"的 PDF。
"""
import os
import sys

import fitz

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

CACHE = os.path.join(os.environ.get("APPDATA", ""), "HN打印工具", "file_cache")


def find_candidate():
    for root, _dirs, files in os.walk(CACHE):
        for fn in files:
            if not fn.lower().endswith(".pdf"):
                continue
            p = os.path.join(root, fn)
            try:
                d = fitz.open(p)
            except Exception:
                continue
            try:
                if d.page_count != 3:
                    continue
                if "陕西省家庭经济困难" in d[0].get_text("text"):
                    return p
            finally:
                d.close()
    return None


def dump(pdf):
    doc = fitz.open(pdf)
    print("PDF:", pdf, "pages:", doc.page_count)
    for i, page in enumerate(doc):
        r = page.rect
        pix = page.get_pixmap(dpi=72, colorspace=fitz.csGRAY)
        data = pix.samples
        dark = sum(1 for v in data if v < 245)
        text = page.get_text("text").strip()
        print(f"\n--- page {i + 1}  {r.width:.0f}x{r.height:.0f}  dark={dark} of {len(data)} "
              f"ratio={dark / len(data):.6f}")
        print("  text:", repr(text[:120]))
        d = page.get_text("dict")
        for b in d.get("blocks", []):
            if b.get("type", 0) != 0:
                print("  [image block]", [round(v, 1) for v in b.get("bbox", ())])
                continue
            for line in b.get("lines", []):
                for sp in line.get("spans", []):
                    t = str(sp.get("text", ""))
                    if not t.strip():
                        continue
                    bb = [round(v, 1) for v in sp.get("bbox", ())]
                    print(f"  span y0={bb[1]:.1f} y1={bb[3]:.1f} h={r.height:.0f} "
                          f"y0/h={bb[1] / r.height:.3f} text={t!r}")
        # 上下 12% 带以外的暗像素（正文区墨量）
        top = int(r.height * 0.12)
        bot = int(r.height * (1 - 0.12))
        # pix 是 72dpi，即 1pt = 1px
        mid_dark = 0
        w = pix.width
        for y in range(top, min(bot, pix.height)):
            row = data[y * w:(y + 1) * w]
            mid_dark += sum(1 for v in row if v < 245)
        print(f"  mid-band dark (y {top}..{bot}) = {mid_dark}")


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else find_candidate()
    if not target:
        print("没找到候选 PDF")
        sys.exit(1)
    dump(target)
