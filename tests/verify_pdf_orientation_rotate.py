# -*- coding: utf-8 -*-
"""验收脚本 · PDF 页面方向必须按 /Rotate 折算（2026-10-28 现场 bug）

现场：`桌面\打印列表\...\王睿豪.pdf`、`...\王晓伟.pdf` 明明是横版，软件「方向」列却写竖。
根因：get_pdf_info() 直接拿 PyPDF2 的 `page.mediabox` 判宽高，而调研件是
「A4 竖 mediabox(595×842) + /Rotate 90」—— mediabox 是**未旋转**的物理框，
阅读器里的横版是 /Rotate 转出来的。手机扫描 / 学校系统导出普遍如此。

本脚本钉住：
1. 逐组合方向判定：无旋转 / 页级 /Rotate 90·180·270 / 从 /Pages 继承 / 页横+旋转 / 混排；
2. 与**独立引擎** PyMuPDF（page.rect 已含旋转）逐页一致 —— 两套口径不许再分叉；
3. 变异自证：按旧算法（只看 mediabox）重算，必须在 /Rotate 90 的夹具上判成 portrait
   （证明断言有判别力，不是"碰巧都对"）；
4. 打印几何等价：横版页在「竖纸 + 程序转 90°」与「横纸 + 驱动转」两条路上铺满比例相同
   ⇒ 方向判对后改走横向 DEVMODE 不会把版面缩小；
5. 真机现场文件（存在才算，缺失 SKIP）：王睿豪.pdf / 王晓伟.pdf 必须判 landscape。

    python tests/verify_pdf_orientation_rotate.py     # exit 0 = 全绿
"""
import io
import os
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL_TOOL = os.path.join(REPO, "local_print_tool")
sys.path.insert(0, LOCAL_TOOL)

from PyPDF2 import PdfReader, PdfWriter  # noqa: E402
from PyPDF2.generic import NameObject, NumberObject  # noqa: E402

from pdf_printer import get_pdf_info, plan_page_draw  # noqa: E402

ok_all = True


def check(name, cond, detail=""):
    global ok_all
    if not cond:
        ok_all = False
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


TMP = tempfile.mkdtemp(prefix="hn_pdfori_")


def build(name, pages):
    """pages: [(w, h, rotate_or_None)]；rotate 写在页对象上。返回路径。"""
    w = PdfWriter()
    for pw, ph, _rot in pages:
        w.add_blank_page(width=pw, height=ph)
    for page, (_pw, _ph, rot) in zip(w.pages, pages):
        if rot is not None:
            page[NameObject("/Rotate")] = NumberObject(rot)
    path = os.path.join(TMP, name)
    with open(path, "wb") as fh:
        w.write(fh)
    return path


def build_inherited_rotate(name, w_pt, h_pt, rot):
    """把 /Rotate 只写在 /Pages 节点上（页面自己不写）= 继承。"""
    w = PdfWriter()
    w.add_blank_page(width=w_pt, height=h_pt)
    w._root_object[NameObject("/Pages")][NameObject("/Rotate")] = NumberObject(rot)
    path = os.path.join(TMP, name)
    with open(path, "wb") as fh:
        w.write(fh)
    return path


A4P = (595.0, 842.0)          # A4 竖
A4L = (842.0, 595.0)          # A4 横

print("\n① 逐组合方向判定")
cases = [
    ("plain_portrait.pdf", build("plain_portrait.pdf", [A4P + (None,)]), "portrait",
     "A4 竖、无旋转"),
    ("plain_landscape.pdf", build("plain_landscape.pdf", [A4L + (None,)]), "landscape",
     "A4 横 mediabox、无旋转（旧口径本来就对）"),
    ("rot90.pdf", build("rot90.pdf", [A4P + (90,)]), "landscape",
     "★ 现场同款：竖 mediabox + /Rotate 90 = 阅读器里横版"),
    ("rot270.pdf", build("rot270.pdf", [A4P + (270,)]), "landscape",
     "/Rotate 270 同样是横版"),
    ("rot180.pdf", build("rot180.pdf", [A4P + (180,)]), "portrait",
     "/Rotate 180 只翻个面，朝向不变"),
    ("land_rot90.pdf", build("land_rot90.pdf", [A4L + (90,)]), "portrait",
     "横 mediabox + /Rotate 90 = 竖版（别只认旋转、也别只认 mediabox）"),
    ("inherit90.pdf", build_inherited_rotate("inherit90.pdf", 595.0, 842.0, 90),
     "landscape", "/Rotate 只写在 /Pages 节点上（继承）"),
    ("mixed.pdf", build("mixed.pdf", [(A4P + (90,)), (A4P + (None,))]), "mixed",
     "一页横一页竖 ⇒ mixed"),
]
for _n, _p, _want, _why in cases:
    got = get_pdf_info(_p)["orientation"]
    check(f"{_why} ⇒ {_want}", got == _want, f"实际 {got}  ({_n})")

print("\n② 与独立引擎 PyMuPDF（page.rect 含旋转）逐页一致")
try:
    import fitz
except Exception as exc:                              # pragma: no cover
    print(f"[SKIP] PyMuPDF 不可用：{exc}")
    fitz = None
if fitz is not None:
    for _n, _p, _want, _why in cases:
        doc = fitz.open(_p)
        rects = [(round(pg.rect.width, 1), round(pg.rect.height, 1)) for pg in doc]
        doc.close()
        fitz_ori = ("landscape" if rects[0][0] > rects[0][1] else "portrait")
        if _want == "mixed":
            ori = {"landscape" if w > h else "portrait" for w, h in rects}
            fitz_ori = "mixed" if len(ori) > 1 else next(iter(ori))
        check(f"PyMuPDF 与 get_pdf_info 同判 {_want}", fitz_ori == _want,
              f"fitz={fitz_ori} rects={rects}  ({_n})")

print("\n③ 变异自证：旧算法（只看 mediabox）必须判错 /Rotate 90 的夹具")


def old_orientation(pdf_path):
    """2026-10-28 之前的实现（保留在这里当变异基线，别当死代码删掉）。"""
    reader = PdfReader(pdf_path)
    has_p = has_l = False
    for page in reader.pages:
        mb = page.mediabox
        w, h = float(mb.width), float(mb.height)
        if w > h:
            has_l = True
        else:
            has_p = True
    return "mixed" if (has_l and has_p) else ("landscape" if has_l else "portrait")


_rot_path = dict((n, p) for n, p, _w, _y in cases)["rot90.pdf"]
check("旧算法在 /Rotate 90 夹具上判成 portrait（这正是现场 bug）",
      old_orientation(_rot_path) == "portrait",
      "旧算法=" + old_orientation(_rot_path))
check("新实现判 landscape（两者必须相反，否则本脚本量不到东西）",
      get_pdf_info(_rot_path)["orientation"] == "landscape")

print("\n④ 打印几何：横版页两条路铺满比例应该一样（改判方向不会缩小版面）")
PW, PH = 4736, 6824            # LJ1020 可打印区（A4 @600dpi，竖）
ang_p, dw_p, dh_p = plan_page_draw(842.0, 595.0, 600, 600, PW, PH)[:3]
ang_l, dw_l, dh_l = plan_page_draw(842.0, 595.0, 600, 600, PH, PW)[:3]
frac_p = (dw_p * dh_p) / float(PW * PH)
frac_l = (dw_l * dh_l) / float(PH * PW)
check("竖纸 + 程序转 90°：angle=90", ang_p == 90, f"angle={ang_p}")
check("横纸（驱动转）：angle=0", ang_l == 0, f"angle={ang_l}")
check("两条路铺满比例相同（差 < 0.5%）且都 > 97%",
      abs(frac_p - frac_l) < 0.005 and frac_p > 0.97 and frac_l > 0.97,
      f"{frac_p:.4f} vs {frac_l:.4f}")

print("\n⑤ 现场文件（存在才算）")
REAL = [
    (r"C:\Users\Administrator\Desktop\打印列表\2026年10月28日\王睿豪\王睿豪.pdf", "王睿豪.pdf"),
    (r"C:\Users\Administrator\Desktop\打印列表\2026年10月27日\王晓伟\王晓伟.pdf", "王晓伟.pdf"),
    (r"C:\Users\Administrator\Desktop\打印列表\2026年10月27日\崔清阳\崔清阳-成绩-横向-所有.pdf",
     "崔清阳-成绩-横向.pdf"),
]
for _p, _label in REAL:
    if not os.path.isfile(_p):
        print(f"[SKIP] {_label} 不在本机")
        continue
    info = get_pdf_info(_p)
    check(f"{_label} 判 landscape（现场投诉的就是这个）",
          info["orientation"] == "landscape", str(info))
    if fitz is not None:
        doc = fitz.open(_p)
        rects = [(round(pg.rect.width, 1), round(pg.rect.height, 1)) for pg in doc]
        doc.close()
        check(f"{_label} 与 PyMuPDF 一致",
              all(w > h for w, h in rects), f"fitz={rects}")

print("\n" + ("PASS 全部通过" if ok_all else "FAIL 有失败项"))
sys.exit(0 if ok_all else 1)
