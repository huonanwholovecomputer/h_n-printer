
# -*- coding: utf-8 -*-
"""生成《隐私与取件须知》：一份排好版的 docx + 一张可直接发给顾客的长图。"""
import os, sys
from PIL import Image, ImageDraw, ImageFont

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DOCX_OUT = os.path.join(ROOT, "隐私与取件须知.docx")
PNG_OUT  = os.path.join(ROOT, "隐私与取件须知_v1.png")

VERSION = "v1"
EFFECTIVE = "2026-11-17"
SERVER_DAYS = 30          # 线上 /home/printer-backend/uploads/retention_config.json: days=30

TITLE = "隐私与取件须知"
SUBTITLE = f"{VERSION} · {EFFECTIVE} 起生效"

SECTIONS = [
    ("一、你的文件怎么被处理", "blue", [
        "上传的文件只用于本次打印：转成 PDF、计算价格，由本店打印终端下载后直接打印，**不做其他用途**。",
        "转换和计价会在云端服务器与本店电脑上产生临时缓存。同一份文件（内容完全一致）只存一份、只转一次以节省时间；**缓存只服务同一份文件的打印，不会把你的文件内容或文件名展示、提供给其他顾客**。",
        f"保留期限：文件上传后默认保留 **{SERVER_DAYS} 天**，到期自动删除。如果你想打完就删，或想提前删除，随时微信告诉店主即可。",
        "第三方：文件只保存在本店自己的服务器上，仅用于打印和计价，**不会提供给任何第三方**。",
        "你的权利：可随时要求查看、删除自己的文件，删除后不可恢复。需要协助，请微信联系店主。",
    ]),
    ("二、发文件、取件与配送", "orange", [
        "**尽早发文件、说清需求**：确定要打印，就尽早把文件发过来，连同打印要求（份数、单双面）和配送地址一起说明白——店主才好提前安排打印和送件；临时发来的，可能要等一等。",
        "**到宿舍自取**（男生 1 号楼）：来之前请先用微信问店主现在有没有时间；未询问、店主说没时间、或还没回复时，**请不要敲门进宿舍**，在楼下等回复即可。",
        "进宿舍后请随手关门——门敞着容易丢件，也影响同宿舍同学。",
        "**需要配送**（女生 4 号楼等）：请先约好时间和地点，我们会安排送件；请尽量不要错过约定时间，有急事提前告知，并与送件员保持联系。",
    ]),
    ("三、说明", "gray", [
        "把文件交给店主打印，即表示你已阅读并同意本说明。",
        "本说明更新后以最新版本为准，版本号与生效日期见页首。",
    ]),
]

FOOTER_BIG = "感谢配合！"
FOOTER_SMALL = "有疑问、或需要提前删除文件，请微信联系店主。"

# ---------------- docx ----------------
def build_docx():
    import docx
    from docx.shared import Pt, RGBColor, Cm
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml.ns import qn

    FONT = "微软雅黑"

    def style_run(run, size, bold=False, color=None, font=FONT):
        run.font.size = Pt(size)
        run.font.bold = bold
        run.font.name = font
        rpr = run._element.get_or_add_rPr()
        rfonts = rpr.find(qn("w:rFonts"))
        if rfonts is None:
            rfonts = rpr.makeelement(qn("w:rFonts"), {})
            rpr.append(rfonts)
        rfonts.set(qn("w:eastAsia"), font)
        rfonts.set(qn("w:ascii"), font)
        rfonts.set(qn("w:hAnsi"), font)
        if color:
            run.font.color.rgb = RGBColor.from_string(color)

    def add_rich(p, text, size, color=None, base_bold=False):
        for i, seg in enumerate(text.split("**")):
            if not seg:
                continue
            r = p.add_run(seg)
            style_run(r, size, bold=(base_bold or i % 2 == 1), color=color)

    d = docx.Document()
    sec = d.sections[0]
    sec.top_margin = Cm(2.0); sec.bottom_margin = Cm(2.0)
    sec.left_margin = Cm(2.2); sec.right_margin = Cm(2.2)

    p = d.add_paragraph(); p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.paragraph_format.space_after = Pt(4)
    style_run(p.add_run(TITLE), 20, bold=True)

    p = d.add_paragraph(); p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.paragraph_format.space_after = Pt(18)
    style_run(p.add_run(SUBTITLE), 9, color="8E8E93")

    for heading, _tone, items in SECTIONS:
        p = d.add_paragraph()
        p.paragraph_format.space_before = Pt(14); p.paragraph_format.space_after = Pt(8)
        style_run(p.add_run(heading), 13, bold=True)
        for i, text in enumerate(items, 1):
            p = d.add_paragraph()
            pf = p.paragraph_format
            pf.space_after = Pt(7); pf.line_spacing = 1.55
            pf.left_indent = Cm(0.75); pf.first_line_indent = Cm(-0.75)
            style_run(p.add_run(f"{i}. "), 10.5)
            add_rich(p, text, 10.5)

    p = d.add_paragraph(); p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.paragraph_format.space_before = Pt(20); p.paragraph_format.space_after = Pt(4)
    style_run(p.add_run(FOOTER_BIG), 13, bold=True)

    p = d.add_paragraph(); p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    style_run(p.add_run(FOOTER_SMALL), 8.5, color="8E8E93")

    d.save(DOCX_OUT)
    return DOCX_OUT

# ---------------- png ----------------
W = 1080
M = 64
CARD_W = W - 2 * M
PAD = 46
TEXT_W = CARD_W - PAD * 2 - 62

BG = (242, 242, 247)
CARD = (255, 255, 255)
INK = (28, 28, 30)
INK2 = (60, 60, 67)
GRAY = (142, 142, 147)
LINE = (229, 229, 234)

TONES = {
    "blue":   ((0, 122, 255), (0, 85, 204)),
    "orange": ((255, 149, 0), (230, 126, 0)),
    "gray":   ((142, 142, 147), (110, 110, 115)),
}

F_REG = "C:/Windows/Fonts/msyh.ttc"
F_BOLD = "C:/Windows/Fonts/msyhbd.ttc"

S_BODY, S_ITEM_TITLE, S_SEC, S_TITLE, S_BRAND, S_SUB = 33, 40, 40, 62, 27, 25
LH = 60
ITEM_GAP = 22


def fonts():
    return {
        "body": ImageFont.truetype(F_REG, S_BODY, index=0),
        "bodyb": ImageFont.truetype(F_BOLD, S_BODY, index=0),
        "sec": ImageFont.truetype(F_BOLD, S_SEC, index=0),
        "num": ImageFont.truetype(F_BOLD, 24, index=0),
        "title": ImageFont.truetype(F_BOLD, S_TITLE, index=0),
        "brand": ImageFont.truetype(F_BOLD, S_BRAND, index=0),
        "sub": ImageFont.truetype(F_REG, S_SUB, index=0),
        "big": ImageFont.truetype(F_BOLD, 40, index=0),
        "small": ImageFont.truetype(F_REG, 26, index=0),
    }


def runs_of(text):
    out = []
    for i, seg in enumerate(text.split("**")):
        if seg:
            out.append((seg, i % 2 == 1))
    return out


NO_START = "，。、；：！？）】》」』”’%‰…·,.;:!?)]}"
NO_END = "（【《「『“‘([{"


def _merge(chars):
    out = []
    for ch, bold in chars:
        if out and out[-1][1] == bold:
            out[-1] = (out[-1][0] + ch, bold)
        else:
            out.append((ch, bold))
    return out


def wrap(draw, runs, F, max_w):
    """按字符折行；中文排版规则：标点不落行首、开括号不落行尾。"""
    lines, cur, cur_w = [], [], 0.0
    for text, bold in runs:
        f = F["bodyb"] if bold else F["body"]
        for ch in text:
            w = draw.textlength(ch, font=f)
            if cur_w + w > max_w and cur:
                lines.append(cur); cur, cur_w = [], 0.0
            cur.append((ch, bold)); cur_w += w
    if cur:
        lines.append(cur)
    for i in range(1, len(lines)):
        while lines[i] and lines[i][0][0] in NO_START:
            lines[i - 1].append(lines[i].pop(0))
        while len(lines[i - 1]) > 1 and lines[i - 1][-1][0] in NO_END:
            lines[i].insert(0, lines[i - 1].pop())
    return [_merge(l) for l in lines]


def draw_lines(draw, lines, x, y, F, fill=INK2):
    for line in lines:
        cx = x
        for txt, bold in line:
            f = F["bodyb"] if bold else F["body"]
            draw.text((cx, y), txt, font=f, fill=fill, anchor="la")
            cx += draw.textlength(txt, font=f)
        y += LH
    return y


def gradient(w, h, c1, c2):
    img = Image.new("RGB", (w, h))
    d = ImageDraw.Draw(img)
    for yy in range(h):
        t = yy / max(1, h - 1)
        d.line([(0, yy), (w, yy)], fill=tuple(int(c1[i] + (c2[i] - c1[i]) * t) for i in range(3)))
    return img


def rounded(img, radius):
    mask = Image.new("L", img.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, img.size[0] - 1, img.size[1] - 1], radius=radius, fill=255)
    return img, mask


def build_png():
    F = fonts()
    probe = ImageDraw.Draw(Image.new("RGB", (10, 10)))

    # ---- measure ----
    header_h = 300
    blocks = []
    for heading, tone, items in SECTIONS:
        h = PAD + 62 + 18
        wrapped = []
        for i, text in enumerate(items, 1):
            lines = wrap(probe, runs_of(text), F, TEXT_W)
            wrapped.append(lines)
            h += len(lines) * LH + ITEM_GAP
        h += PAD - ITEM_GAP
        blocks.append((heading, tone, wrapped, h))

    footer_h = 150
    total = 28 + header_h + 26 + sum(b[3] + 26 for b in blocks) + footer_h + 56

    img = Image.new("RGB", (W, total), BG)
    d = ImageDraw.Draw(img)

    # ---- header ----
    hx, hy = M, 28
    g = gradient(CARD_W, header_h, (10, 132, 255), (0, 78, 190))
    g, mask = rounded(g, 34)
    img.paste(g, (hx, hy), mask)
    d = ImageDraw.Draw(img)
    d.text((hx + PAD, hy + 44), "HN 打印", font=F["brand"], fill=(255, 255, 255, 205), anchor="la")
    d.text((hx + PAD, hy + 96), TITLE, font=F["title"], fill=(255, 255, 255), anchor="la")
    d.text((hx + PAD, hy + 200), SUBTITLE, font=F["sub"], fill=(255, 255, 255, 190), anchor="la")

    # ---- sections ----
    y = hy + header_h + 26
    for heading, tone, wrapped, h in blocks:
        c1, c2 = TONES[tone]
        d.rounded_rectangle([M + 2, y + 5, M + CARD_W - 2, y + h + 5], radius=30, fill=(226, 226, 234))
        d.rounded_rectangle([M, y, M + CARD_W, y + h], radius=30, fill=CARD)

        d.rounded_rectangle([M + PAD, y + PAD + 2, M + PAD + 10, y + PAD + 44], radius=5, fill=c1)
        d.text((M + PAD + 24, y + PAD), heading, font=F["sec"], fill=INK, anchor="la")

        iy = y + PAD + 62 + 18
        for idx, lines in enumerate(wrapped, 1):
            cy = iy + LH // 2
            d.ellipse([M + PAD, cy - 22, M + PAD + 44, cy + 22], fill=c1)
            d.text((M + PAD + 22, cy), str(idx), font=F["num"], fill=(255, 255, 255), anchor="mm")
            iy = draw_lines(d, lines, M + PAD + 62, iy, F) + ITEM_GAP
        y += h + 26

    # ---- footer ----
    d.text((W // 2, y + 34), FOOTER_BIG, font=F["big"], fill=INK, anchor="ma")
    d.text((W // 2, y + 96), FOOTER_SMALL, font=F["small"], fill=GRAY, anchor="ma")

    img.save(PNG_OUT, "PNG", optimize=True)
    return PNG_OUT, total


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    p1 = build_docx()
    p2, h = build_png()
    print("DOCX", p1, os.path.getsize(p1))
    print("PNG ", p2, os.path.getsize(p2), "size", W, "x", h)
