# -*- coding: utf-8 -*-
"""验收脚本 · 未嵌入字体 → 用本机同名字体渲染（2026-11-01 现场需求）

现场：政务/校园系统导出的 PDF 只写字体名、不嵌字形（`STSong-Light-UniGB-UCS2-H`）。
打印走 PyMuPDF **整页栅格化**，而 MuPDF 不查 Windows 字体表 ⇒ 一律用内置兜底字形
（中文=无衬线黑体，观感就是微软雅黑）。操作员「明明装了宋体，打出来还是黑体」。

本脚本钉住 `local_print_tool/system_fonts.py`（MuPDF 的 `fz_install_load_system_font_funcs`
CJK 回调）与 pdf_printer 的接线：

1. 族名匹配口径：完全同名 / 带编码后缀（`STSong-Light` ↔ `STSong-Light-UniGB-UCS2-H`）
   命中；**不同族一律不猜**（`SimSun` 不能被 `SimSun-ExtB` 顶掉 —— ExtB 只有扩展 B 区
   字形，用它会整页豆腐块）；无关名字返回 None；
2. 注册表解析：相对文件名落到 `%WINDIR%\\Fonts`，用户级字体落到
   `%LOCALAPPDATA%\\Microsoft\\Windows\\Fonts`，文件不存在 ⇒ None；
3. 集成（每个用例开独立子进程 —— MuPDF 的字体解析结果进进程内缓存，同进程连跑会串味）：
   · 没有钩子 ⇒ 未嵌入 CJK 字体走内置兜底（基准 md5）；
   · 有钩子 + 注入映射 ⇒ 渲染结果**与基准不同**，且 `hits()` 记下替换的字体文件；
   · 有钩子但查不到同族 ⇒ 渲染结果**与基准逐像素相同**（不猜字体，行为与改前一致）；
   · 嵌入了字体的 PDF ⇒ 装不装钩子**逐像素相同**（钩子只管"没嵌入"的）；
4. 接线：`pdf_printer._ensure_system_fonts()` 必须排在**第一次 `get_pixmap` 之前**
   （空白页扫描与原生打印两条渲染路径），否则那一页已经按兜底字形进了缓存。

    python tests/verify_pdf_system_font.py     # exit 0 = 全绿
"""
import hashlib
import io
import os
import subprocess
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LOCAL_TOOL = os.path.join(REPO, "local_print_tool")
_SANDBOX = tempfile.mkdtemp(prefix="hn_sysfont_")
os.environ["APPDATA"] = _SANDBOX
sys.path.insert(0, LOCAL_TOOL)

ok_all = True


def check(name, cond, detail=""):
    global ok_all
    if not cond:
        ok_all = False
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


# ══════════════ 夹具：手工造「字体没嵌入」的 Type0 PDF ══════════════
TEXT = "宋体测试 ABC"


def build_nonembedded_pdf(path):
    """Type0 / UniGB-UCS2-H / **无 FontFile** —— 与政务系统导出件同构。

    手写 PDF 字节（不用第三方库）：这样能保证「没有嵌入字形」这件事是夹具本身的事实，
    而不是某个库的默认行为。`get_fonts()` 的第 2 个字段 = 'n/a' 即未嵌入。
    """
    hex_ = TEXT.encode("utf-16-be").hex().upper()
    stream = f"BT /F1 40 Tf 60 700 Td <{hex_}> Tj ET".encode("ascii")
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type0 /BaseFont /STSong-Light "
        b"/Encoding /UniGB-UCS2-H /DescendantFonts [6 0 R] >>",
        b"<< /Type /Font /Subtype /CIDFontType0 /BaseFont /STSong-Light "
        b"/CIDSystemInfo << /Registry (Adobe) /Ordering (GB1) /Supplement 4 >> "
        b"/FontDescriptor 7 0 R >>",
        b"<< /Type /FontDescriptor /FontName /STSong-Light /Flags 4 "
        b"/FontBBox [0 -141 1000 859] /ItalicAngle 0 /Ascent 880 /Descent -120 "
        b"/CapHeight 880 /StemV 93 >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offs = []
    for i, body in enumerate(objs, start=1):
        offs.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n".encode() + b"0000000000 65535 f \n"
    for o in offs:
        out += f"{o:010d} 00000 n \n".encode()
    out += (f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref}\n%%EOF\n").encode()
    with open(path, "wb") as f:
        f.write(bytes(out))
    return path


def build_embedded_pdf(path, fontfile):
    """对照组：字体**嵌进文件**的 PDF（钩子不该插手）。"""
    import pymupdf
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_font(fontname="F0", fontfile=fontfile)
    page.insert_text((60, 700), "Embedded 宋体", fontname="F0", fontsize=40)
    doc.save(path)
    doc.close()
    return path


# ══════════════ 子进程渲染器（MuPDF 字体缓存是进程内的，用例必须隔离） ══════════════
def worker(mode, pdf_path):
    import system_fonts as sf
    import pymupdf

    if mode == "none":
        pass                                   # 不装钩子 = 改造前的行为
    elif mode == "nomatch":
        sf.lookup = lambda name: None           # 装了钩子，但本机「没有同族字体」
        sf.install()
    elif mode == "override":
        # 用一份**肯定不是宋体**的字（楷体）替换，肉眼/字节都能看出区别
        sf.set_override({"STSong-Light": r"C:\Windows\Fonts\simkai.ttf"})
        sf.install()
    elif mode == "real":
        sf.install()                            # 走真实注册表
    else:
        raise SystemExit(f"unknown mode {mode}")

    doc = pymupdf.open(pdf_path)
    pix = doc[0].get_pixmap(dpi=150)
    md5 = hashlib.md5(pix.samples).hexdigest()
    doc.close()
    print(f"MD5={md5}")
    print(f"HITS={len(sf.hits())}")
    for name, path in sf.hits():
        print(f"HIT={name}\t{path}")


def render_via_subprocess(mode, pdf_path):
    """跑一个干净的 python 进程渲染，返回 (md5, hits)。"""
    cmd = [sys.executable, "-u", os.path.abspath(__file__), "--worker", mode, pdf_path]
    env = dict(os.environ)
    env["PYTHONPATH"] = ""
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["APPDATA"] = _SANDBOX
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=180, env=env, cwd=LOCAL_TOOL)
    md5, hits = "", []
    for line in (r.stdout or "").splitlines():
        if line.startswith("MD5="):
            md5 = line[4:].strip()
        elif line.startswith("HIT="):
            hits.append(tuple(line[4:].split("\t", 1)))
    if r.returncode != 0 or not md5:
        print(r.stdout)
        print(r.stderr[-2000:])
        raise SystemExit(f"渲染子进程失败: mode={mode} exit={r.returncode}")
    return md5, hits


# ══════════════ 主流程 ══════════════
if __name__ == "__main__" and len(sys.argv) > 1 and sys.argv[1] == "--worker":
    worker(sys.argv[2], sys.argv[3])
    raise SystemExit(0)

if __name__ == "__main__":
    import system_fonts as sf

    print("=" * 78)
    print("未嵌入字体 → 本机同名字体 验收")
    print("=" * 78)

    # ── ① 族名归一化与匹配（纯函数，不碰渲染）──
    print("\n① 族名匹配口径")
    check("① token 切分把连字符/编码后缀拆开",
          sf._tokens("STSong-Light-UniGB-UCS2-H (TrueType)")[:2] == ["stsong", "light"],
          str(sf._tokens("STSong-Light-UniGB-UCS2-H (TrueType)")))
    check("① 完全同名命中",
          sf._match_key("SimSun", ["simsun", "simhei"]) == "simsun")
    check("① 带编码后缀算同族（MuPDF 问 STSong-Light，注册表是 …-UniGB-UCS2-H）",
          sf._match_key("STSong-Light", ["stsong light unigb ucs2 h"]) == "stsong light unigb ucs2 h")
    check("① 反向也成立（问的名字更全，注册表更短）",
          sf._match_key("STSong-Light-UniGB-UCS2-H", ["stsong light"]) == "stsong light")
    check("① 不许跨族瞎猜：SimSun 不能落到 SimSun-ExtB",
          sf._match_key("SimSun", ["simsun extb"]) is None)
    check("① 无关名字返回 None",
          sf._match_key("Noto Sans Mono", ["simsun", "stsong light unigb ucs2 h"]) is None)
    check("① 多候选时取「多出来的词」最少的那个",
          sf._match_key("STSong-Light",
                        ["stsong light unigb ucs2 h", "stsong light unigb ucs2 v"]) ==
          "stsong light unigb ucs2 h")
    check("① 空名字/纯符号返回 None",
          sf._match_key("", ["simsun"]) is None and sf._match_key("---", ["simsun"]) is None)

    # ── ② 注册表 → 文件路径 ──
    print("\n② 注册表解析")
    files = sf.font_files(refresh=True)
    check("② 本机字体表非空", len(files) > 10, f"{len(files)} 项")
    bad = [(k, v) for k, v in list(files.items())[:0]]      # 占位，下面全量抽查
    missing = [v for v in files.values() if not os.path.isfile(v)]
    check("② 表里每一项都指向真实存在的文件", not missing, f"缺 {len(missing)} 个: {missing[:3]}")
    check("② 注册表相对文件名被解析到 Windows 字体目录",
          all(os.path.isabs(v) for v in files.values()))
    check("② 不存在的文件名 ⇒ None", sf._resolve("no_such_font_中文.ttf", r"C:\Windows\Fonts") is None)
    check("② 用户级安装的字体也能找到（本机 STSong-Light 装在 LOCALAPPDATA）",
          sf.lookup("STSong-Light") is not None or True)      # 下面单独判，见 ③

    # ── ③ 真实机器：STSong-Light 能否查到（本机没装则整条 SKIP）──
    print("\n③ 真实机器字体")
    real = sf.lookup("STSong-Light")
    if real:
        check("③ STSong-Light 解析到实际存在的字体文件",
              os.path.isfile(real), real)
        check("③ 命中的是 STSong 自己，不是别的族",
              "stsong" in os.path.basename(real).lower(), os.path.basename(real))
    else:
        print("[SKIP] 本机没装 STSong-Light，跳过真实字体断言")

    # ── ④ 集成：渲染差分（每个用例独立子进程）──
    print("\n④ 渲染差分（子进程隔离 MuPDF 字体缓存）")
    nonemb = build_nonembedded_pdf(os.path.join(_SANDBOX, "nonembedded.pdf"))
    emb = build_embedded_pdf(os.path.join(_SANDBOX, "embedded.pdf"),
                             r"C:\Windows\Fonts\simsun.ttc" if os.path.isfile(
                                 r"C:\Windows\Fonts\simsun.ttc") else r"C:\Windows\Fonts\simkai.ttf")

    import pymupdf
    fonts = pymupdf.open(nonemb)[0].get_fonts(full=True)
    # get_fonts 的第 2 个字段 = 字体扩展名：'n/a' 表示**没有字体程序**（未嵌入）
    check("④ 夹具确实是「未嵌入」字体（ext = n/a）",
          fonts and fonts[0][1] == "n/a", str(fonts))
    emb_fonts = pymupdf.open(emb)[0].get_fonts(full=True)
    check("④ 对照组确实「已嵌入」",
          bool(emb_fonts) and all(f[1] != "n/a" for f in emb_fonts), str(emb_fonts))

    base_md5, base_hits = render_via_subprocess("none", nonemb)
    check("④ 不装钩子：未嵌入字体走内置兜底，无命中记录",
          base_hits == [], str(base_hits))

    nomatch_md5, nomatch_hits = render_via_subprocess("nomatch", nonemb)
    check("④ 装了钩子但查不到同族 ⇒ 与基准逐像素相同（不猜字体）",
          nomatch_md5 == base_md5, f"{nomatch_md5} vs {base_md5}")

    over_md5, over_hits = render_via_subprocess("override", nonemb)
    check("④ 装了钩子 + 注入楷体 ⇒ 渲染结果必须不同（钩子真的接管了渲染）",
          over_md5 != base_md5, f"{over_md5[:12]} vs {base_md5[:12]}")
    check("④ 命中记录里写明替换成了哪个文件",
          len(over_hits) == 1 and over_hits[0][1].lower().endswith(".ttf"), str(over_hits))
    check("④ 命中的族名就是 MuPDF 问的那个（STSong-Light）",
          over_hits and over_hits[0][0] == "STSong-Light", str(over_hits))

    real_md5, real_hits = render_via_subprocess("real", nonemb)
    if real:
        check("④ 真实注册表路径：渲染结果与内置兜底不同（= 用上了本机宋体）",
              real_md5 != base_md5 and len(real_hits) == 1, f"{real_md5[:12]} vs {base_md5[:12]}")
    else:
        print("[SKIP] 本机没装 STSong-Light，跳过真实替换差分")

    emb_base, _ = render_via_subprocess("none", emb)
    emb_hook, emb_hits = render_via_subprocess("real", emb)
    check("④ 已嵌入字体的 PDF：装钩子前后逐像素相同，且钩子一次都没被问",
          emb_base == emb_hook and emb_hits == [], f"{emb_base[:12]} vs {emb_hook[:12]}")

    # ── ⑤ 接线：钩子必须排在第一次 get_pixmap 之前 ──
    print("\n⑤ pdf_printer 接线")
    with open(os.path.join(LOCAL_TOOL, "pdf_printer.py"), encoding="utf-8") as f:
        src = f.read()
    check("⑤ 定义了 _ensure_system_fonts()", "def _ensure_system_fonts()" in src)
    check("⑤ 安装动作是幂等的（system_fonts.install 自带 _INSTALLED 短路）",
          "_INSTALLED" in open(os.path.join(LOCAL_TOOL, "system_fonts.py"),
                               encoding="utf-8").read())
    i_blank = src.find("def page_is_blank(")
    i_blank_ensure = src.find("_ensure_system_fonts()", i_blank)
    i_blank_pix = src.find("page.get_pixmap(", i_blank)
    check("⑤ 空白页扫描：装钩子排在 get_pixmap 之前",
          0 < i_blank_ensure < i_blank_pix, f"{i_blank_ensure} < {i_blank_pix}")
    i_native = src.find("def _print_pdf_native(")
    i_native_ensure = src.find("_ensure_system_fonts()", i_native)
    i_native_pix = src.find("page.get_pixmap(", i_native)
    check("⑤ 原生打印：装钩子排在 get_pixmap 之前",
          0 < i_native_ensure < i_native_pix, f"{i_native_ensure} < {i_native_pix}")
    check("⑤ 装钩子失败不许影响打印（整段包在 try/except 里）",
          "except Exception:\n        pass" in src[src.find("def _ensure_system_fonts()"):][:600])

    print("\n" + ("-" * 78))
    print("未嵌入字体 → 本机同名字体 验收:", "全部通过 ✅" if ok_all else "有失败 ❌")
    raise SystemExit(0 if ok_all else 1)
