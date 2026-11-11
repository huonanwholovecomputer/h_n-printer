# -*- coding: utf-8 -*-
"""钉住「字体缺失检测」（2026-10-31 用户需求）：查源文件用到的字体、本机缺哪些、
表格标黄、打印前确认、装完字体后缓存必须作废重转。

现场来由（2026-10-31 HN20261031-0005）：用户拖进来的 `.docx` 用了 `方正书宋_GBK` /
`方正黑体_GBK`，这台机器没装 ⇒ Word **静默替换**成宋体，打出来字宽和换行全变了，
而控制台、界面、价格全都没提过一个字。用户要求：① 自动检出并标黄 ⚠️；
② 打印开始前弹一次确认；③ 他中途把字体装上以后，程序要自己重新转 PDF。

本脚本钉住的判据（每一条都能红）：
  ① 归一：子集前缀 `ABCDEF+`、大小写、空格/竖排 `@`、中英别名（宋体 ⟷ SimSun）要认同一件事；
  ② 误报过滤：主题引用 `+mn-lt`、PDF 匿名资源名 `F1`/`C2_0`/`TT0`、语言标签 `zh-CN`、
     空/占位名 —— 都不许当成"缺字体"（否则每个文件都报警，等于没报）；
  ③ 家族+字重回退：`等线 Light` 在 `等线` 已装时不算缺（中文 Word 文档几乎人人中招）；
  ④ `.docx` 真解析：fontTable 的 `w:font w:name` 与正文字体都要抓出来；
  ⑤ `.pdf` 真解析：14 标准字体（Helvetica 等）、内嵌字体、Type3 都不算缺；
  ⑥ `check_epoch` 记账：首次不判"变过"（否则升级后首启把所有缓存清一遍），
     之后新增/移除字体要报出来；
  ⑦ `purge_pdf_cache` 只删 `*.pdf`/`*.pdf.tmp`，**索引 json 必须留着**；
  ⑧ `PrintJob` 往返：`missing_fonts`/`font_checked`/`font_fp` 存得下、读得回；
  ⑨ 打印前闸门：缺字体时弹确认，点「否」必须**返回 False**（调用方据此不冻结、不取号、不上报）；
  ⑩ 自动打印线（云端自动开始/无障碍预约/空闲补打/攒批放行）传 `interactive=False` 时
     **绝不弹模态**，只记日志 —— 那台机器要能无人值守，弹窗会把整条队列堵到第二天。

全程不出纸：本脚本不 import 打印流程的任何会真打的东西，也不碰真实打印机。
"""
from __future__ import annotations

import io
import os
import sys
import tempfile
import types
import zipfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LOCAL_TOOL = os.path.join(REPO, "local_print_tool")
sys.path.insert(0, LOCAL_TOOL)

# %APPDATA% 先指向临时沙箱：字体指纹状态文件、PDF 缓存都别写到真实用户目录里
_SANDBOX = tempfile.mkdtemp(prefix="hn_font_check_")
os.environ["APPDATA"] = _SANDBOX
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import font_check  # noqa: E402
import printer_config  # noqa: E402

FAILED: list[str] = []
PASSED = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASSED
    if cond:
        PASSED += 1
        print(f"  ✅ {name}")
    else:
        FAILED.append(f"{name}{(' — ' + detail) if detail else ''}")
        print(f"  ❌ {name}{(' — ' + detail) if detail else ''}")


_W_NS = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'


def _make_docx(path: str, fonts: list[str], body_fonts: list[str],
               body_extra: str = "", styles_xml: str = "", theme_xml: str = "") -> None:
    """造一个最小的 .docx（fontTable / document.xml 的 rFonts，可选 styles.xml 与 theme1.xml）。"""
    ft = "".join(f'<w:font w:name="{f}"/>' for f in fonts)
    rpr = "".join(f'<w:rFonts w:ascii="{f}" w:eastAsia="{f}"/>' for f in body_fonts)
    parts = {
        "word/fontTable.xml":
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<w:fonts {_W_NS}>{ft}</w:fonts>',
        "word/document.xml":
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<w:document {_W_NS}><w:body>'
            f'<w:p><w:pPr><w:rPr>{rpr}</w:rPr></w:pPr><w:r><w:t>x</w:t></w:r></w:p>'
            '<w:p><w:pPr><w:rPr><w:lang w:eastAsia="zh-CN"/></w:rPr></w:pPr></w:p>'
            f'{body_extra}'
            '</w:body></w:document>',
    }
    if styles_xml:
        parts["word/styles.xml"] = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<w:styles {_W_NS}>{styles_xml}</w:styles>')
    if theme_xml:
        parts["word/theme/theme1.xml"] = theme_xml
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in parts.items():
            z.writestr(name, data)


# Word 从默认模板起稿时的主题字体：majorFont=Aptos Display / minorFont=Aptos
_THEME_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<a:theme xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><a:themeElements>'
    '<a:fontScheme name="Office">'
    '<a:majorFont><a:latin typeface="Aptos Display"/><a:ea typeface=""/></a:majorFont>'
    '<a:minorFont><a:latin typeface="Aptos"/><a:ea typeface=""/></a:minorFont>'
    '</a:fontScheme></a:themeElements></a:theme>'
)


def _make_pdf(path: str, text: str = "hello") -> None:
    import fitz
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), text, fontname="helv", fontsize=12)
    doc.save(path)
    doc.close()


def _make_nonembedded_pdf(path: str) -> None:
    """造一份**没有嵌入字形**的 Type0/CID 字体 PDF —— 就是 2026-10-31 那张成绩单的样子。

    iText 5.5 那类生成器只写 `BaseFont=STSong-Light` + `Encoding=UniGB-UCS2-H`，不给 FontFile
    字体程序 ⇒ 文件里只有字体名、没有轮廓，谁渲染谁替（查看器有自己的东方字体包，我们走
    MuPDF 光栅化就被换成内置无衬线兜底字形 —— 现场"屏幕宋体、纸上雅黑"的根因）。
    """
    import fitz
    doc = fitz.open()
    page = doc.new_page()
    fd = doc.get_new_xref()
    doc.update_object(fd, "<</Type/FontDescriptor/FontName/STSong-Light/Flags 4"
                          "/FontBBox[0 -200 1000 900]/ItalicAngle 0"
                          "/Ascent 880/Descent -120/CapHeight 700/StemV 80>>")
    cid = doc.get_new_xref()
    doc.update_object(cid, "<</Type/Font/Subtype/CIDFontType0/BaseFont/STSong-Light"
                           "/CIDSystemInfo<</Registry(Adobe)/Ordering(GB1)/Supplement(4)>>"
                           f"/FontDescriptor {fd} 0 R/DW 1000>>")
    fnt = doc.get_new_xref()
    doc.update_object(fnt, "<</Type/Font/Subtype/Type0/BaseFont/STSong-Light"
                           f"/Encoding/UniGB-UCS2-H/DescendantFonts[{cid} 0 R]>>")
    # 整块写 Resources（`Resources/Font/...` 走不通：新页的 Resources 是间接对象）
    doc.xref_set_key(page.xref, "Resources", f"<</Font<</F1 {fnt} 0 R>>>>")
    doc.save(path, garbage=1, deflate=True)
    doc.close()


print("① 归一：子集前缀 / 大小写 / 空格 / 竖排 @ / 中英别名")
_inst = font_check._norm("宋体")
check("宋体 与 SimSun 归一成同一件事",
      _inst == font_check._norm("SimSun") or font_check.is_installed("SimSun"))
check("子集前缀 ABCDEF+ 被剥掉",
      font_check._norm("ABCDEF+SimSun") == font_check._norm("SimSun"),
      font_check._norm("ABCDEF+SimSun"))
check("空格与竖排 @ 被清掉",
      font_check._norm("@Microsoft  YaHei") == font_check._norm("Microsoft YaHei"))

print("② 误报过滤：主题引用 / 匿名资源名 / 语言标签 / 占位符")
for junk in ("+mn-lt", "+mj-ea", "F1", "C2_0", "TT0", "zh-CN", "en-US", "", "-"):
    check(f"「{junk or '(空)'}」不算真字体名", not font_check._is_real_font(junk))
check("真字体名照常通过", font_check._is_real_font("方正书宋_GBK"))
check("语言标签 zh-CN 不进 missing", font_check.missing_fonts(["zh-CN"]) == [])

print("③ 家族 + 字重回退")
if font_check.is_installed("等线") or font_check.is_installed("DengXian"):
    check("等线 Light 在等线已装时不算缺", font_check.missing_fonts(["等线 Light"]) == [])
else:
    print("  ⏭ 本机没装「等线」，跳过该回退判据")
check("家族名 + 字重但整族没装 → 仍算缺",
      font_check.missing_fonts(["假字体族XY Light"]) == ["假字体族XY Light"])

print("④ .docx 真解析")
tmp = tempfile.mkdtemp(prefix="fc_")
docx = os.path.join(tmp, "t.docx")
_make_docx(docx, ["宋体", "测试缺字体XY", "Helvetica"], ["宋体", "测试缺字体XY"])
used, ok = font_check.fonts_in_docx(docx)
check("能解析 .docx", ok is True and bool(used), str(used))
check("正文写的字体被抓到", "测试缺字体XY" in used, str(used))
check("正文字体被抓到", "宋体" in used, str(used))
check("w:lang 的 zh-CN 没被当成字体", "zh-CN" not in used, str(used))
check("只在 fontTable 里登记的字体不算用到（模板残留）", "Helvetica" not in used, str(used))
_rep = font_check.check_file(docx)
check("check_file 报出缺的那个字体", _rep["missing"] == ["测试缺字体XY"], str(_rep["missing"]))
check("check_file 给出指纹", bool(_rep.get("fingerprint")))

print("④b 现场复现：正文只有宋体/楷体，不许报主题字体 Aptos（2026-11-01）")
_real = os.path.join(tmp, "theme.docx")
_make_docx(_real, ["Aptos", "Aptos Display", "宋体", "楷体"], ["宋体", "楷体"],
           theme_xml=_THEME_XML)
_u, _ok = font_check.fonts_in_docx(_real)
check("Aptos / Aptos Display 不算正文用到", _ok and "Aptos" not in _u and "Aptos Display" not in _u, str(_u))
check("正文显式写的宋体/楷体照常报", "宋体" in _u and "楷体" in _u, str(_u))
check("这份文件的 missing 是空（不再误报）", font_check.check_file(_real)["missing"] == [],
      str(font_check.check_file(_real)["missing"]))

print("④c 样式里写的字体：只有被正文点名用到的才算")
_STYLES = (
    '<w:docDefaults><w:rPrDefault><w:rPr>'
    '<w:rFonts w:asciiTheme="minorHAnsi" w:eastAsiaTheme="minorEastAsia"/>'
    '</w:rPr></w:rPrDefault></w:docDefaults>'
    '<w:style w:type="paragraph" w:styleId="BodyCN"><w:name w:val="正文"/>'
    '<w:rPr><w:rFonts w:ascii="仿宋" w:eastAsia="仿宋"/></w:rPr></w:style>'
    '<w:style w:type="paragraph" w:styleId="ChildCN"><w:name w:val="正文缩进"/>'
    '<w:basedOn w:val="BodyCN"/><w:rPr><w:b/></w:rPr></w:style>'
    '<w:style w:type="paragraph" w:styleId="NeverUsed"><w:name w:val="标题 1"/>'
    '<w:rPr><w:rFonts w:ascii="本模板没用到的字体ZZ" w:eastAsia="本模板没用到的字体ZZ"/></w:rPr>'
    '</w:style>'
)
_sty = os.path.join(tmp, "styles.docx")
_make_docx(_sty, ["仿宋", "本模板没用到的字体ZZ", "Aptos"], [],
           body_extra='<w:p><w:pPr><w:pStyle w:val="ChildCN"/></w:pPr><w:r><w:t>y</w:t></w:r></w:p>',
           styles_xml=_STYLES, theme_xml=_THEME_XML)
_u2, _ok2 = font_check.fonts_in_docx(_sty)
check("样式（沿 basedOn 链）里写的字体被抓到", _ok2 and "仿宋" in _u2, str(_u2))
check("没被正文点名的样式字体不算", "本模板没用到的字体ZZ" not in _u2, str(_u2))
check("docDefaults 只写主题引用时不报主题字体", "Aptos" not in _u2, str(_u2))

print("④d 2026-11-04 现场：整篇靠主题字体排（Office 365 新模板，fontTable 里还有一堆没用过的）")
# 真实文件的样子：正文/样式没有一个显式字体名，全是 w:asciiTheme="minorHAnsi"；
# fontTable 里却登记着 Aptos / Aptos Display / Arial / Times New Roman（模板残留）。
_DD_THEME = (
    '<w:docDefaults><w:rPrDefault><w:rPr>'
    '<w:rFonts w:asciiTheme="minorHAnsi" w:hAnsiTheme="minorHAnsi" w:eastAsiaTheme="minorEastAsia"/>'
    '</w:rPr></w:rPrDefault></w:docDefaults>'
)
_theme_only = os.path.join(tmp, "theme_only.docx")
_make_docx(_theme_only, ["Aptos", "Aptos Display", "Arial", "Times New Roman"], [],
           styles_xml=_DD_THEME, theme_xml=_THEME_XML,
           body_extra='<w:p><w:pPr><w:rPr><w:rFonts w:hint="eastAsia"/></w:rPr></w:pPr>'
                      '<w:r><w:t>正文</w:t></w:r></w:p>')
_u3, _ok3 = font_check.fonts_in_docx(_theme_only)
check("主题引用解析成真名（minorHAnsi → Aptos）", _ok3 and _u3 == ["Aptos"], str(_u3))
check("没用过的 Aptos Display / Arial / Times New Roman 不算正文用到",
      all(x not in _u3 for x in ("Aptos Display", "Arial", "Times New Roman")), str(_u3))
check("这份文件不再误报缺字体", font_check.check_file(_theme_only)["missing"] == [],
      str(font_check.check_file(_theme_only)["missing"]))

print("④e Word 自带云字体豁免（Aptos 族）；豁免不能一刀切")
check("Aptos 全族不算缺（Word 转出的 PDF 里嵌的就是它）",
      font_check.missing_fonts(["Aptos", "Aptos Display", "Aptos Mono", "Aptos Light"]) == [],
      str(font_check.missing_fonts(["Aptos", "Aptos Display"])))
check("豁免只针对 Word 自带字族，别的字照报",
      font_check.missing_fonts(["Aptos", "假字体族XY"]) == ["假字体族XY"],
      str(font_check.missing_fonts(["Aptos", "假字体族XY"])))
_THEME_MISS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<a:theme xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><a:themeElements>'
    '<a:fontScheme name="Office">'
    '<a:majorFont><a:latin typeface="缺字体族主题XY"/><a:ea typeface=""/></a:majorFont>'
    '<a:minorFont><a:latin typeface="缺字体族主题XY"/><a:ea typeface=""/></a:minorFont>'
    '</a:fontScheme></a:themeElements></a:theme>'
)
_theme_miss = os.path.join(tmp, "theme_miss.docx")
_make_docx(_theme_miss, ["宋体"], [], styles_xml=_DD_THEME, theme_xml=_THEME_MISS)
check("主题字体是真缺的照样报（解析主题不是把嘴堵上）",
      font_check.check_file(_theme_miss)["missing"] == ["缺字体族主题XY"],
      str(font_check.check_file(_theme_miss)["missing"]))

_ft_only = os.path.join(tmp, "ft_only.docx")
_make_docx(_ft_only, ["仅登记字体QQ"], [])
check("正文读不出字体名时退回 fontTable（免得不吭声）",
      "仅登记字体QQ" in font_check.fonts_in_docx(_ft_only)[0],
      str(font_check.fonts_in_docx(_ft_only)))

print("⑤ .pdf 真解析")
if not font_check.is_installed("Helvetica") and "helvetica" not in font_check._ALWAYS_OK:
    print("  ⏭ 标准字体表异常，跳过")
pdf = os.path.join(tmp, "t.pdf")
_make_pdf(pdf)
_used, _miss, _ok = font_check.fonts_in_pdf(pdf)
check("能解析 .pdf", _ok is True, str(_used))
check("Helvetica（14 标准字体）不算缺", _miss == [], str(_miss))

# 非嵌入字体：这是"PDF 也会出问题"的唯一情形，必须能和"本机没装"区分开
ne_pdf = os.path.join(tmp, "nonembedded.pdf")
_make_nonembedded_pdf(ne_pdf)
_rep_ne = font_check.check_file(ne_pdf)
check("没嵌入字形的 Type0 字体被判为「缺」",
      _rep_ne.get("missing") == ["STSong-Light"], str(_rep_ne.get("missing")))
check("check_file 同时标明它是「没嵌入」（不是本机没装）",
      "STSong-Light" in (_rep_ne.get("nonembedded") or []),
      str(_rep_ne.get("nonembedded")))
check("pdf_nonembedded_fonts 同口径",
      "STSong-Light" in font_check.pdf_nonembedded_fonts(ne_pdf),
      str(font_check.pdf_nonembedded_fonts(ne_pdf)))

print("⑥ check_epoch 记账")
_st = os.path.join(tmp, "epoch.json")
_r1 = font_check.check_epoch(state_path=_st, record=True)
check("首次没有记录 ⇒ 不判「变过」（否则升级首启清空全部缓存）", _r1["changed"] is False, str(_r1))
_r2 = font_check.check_epoch(state_path=_st, record=True)
check("指纹没变 ⇒ changed=False", _r2["changed"] is False, str(_r2))
_state = font_check.read_epoch_state(_st)
_state["fingerprint"] = "deadbeefdeadbeef"
_state["names"] = ["宋体"]
font_check.write_epoch_state(_state, _st)
_r3 = font_check.check_epoch(state_path=_st, record=True)
check("指纹变了 ⇒ changed=True", _r3["changed"] is True, str(_r3))
check("报出新增字体", bool(_r3["added_names"]) and "宋体" not in _r3["added_names"],
      str(_r3["added_names"])[:120])

print("⑦ purge_pdf_cache 只删 PDF、留索引")
_cdir = os.path.join(tmp, "pdf_cache")
os.makedirs(_cdir, exist_ok=True)
for _n in ("a.pdf", "b.pdf.tmp", "_index.json", "keep.txt"):
    with open(os.path.join(_cdir, _n), "w", encoding="utf-8") as f:
        f.write("x")
_purged = font_check.purge_pdf_cache(cache_dir=_cdir)
check("删掉 2 个 PDF/临时文件", _purged == 2, str(_purged))
check("索引 json 还在", os.path.isfile(os.path.join(_cdir, "_index.json")))
check("其它文件不动", os.path.isfile(os.path.join(_cdir, "keep.txt")))

print("⑧ PrintJob 往返")
_j = printer_config.PrintJob(file_path=docx, copies=1, missing_fonts=["测试缺字体XY"],
                             font_checked=True, font_fp="abcd1234")
_d = _j.to_dict()
check("to_dict 带 missing_fonts", _d.get("missing_fonts") == ["测试缺字体XY"], str(_d.get("missing_fonts")))
_j2 = printer_config.PrintJob.from_dict(_d)
check("from_dict 还原 missing_fonts", _j2.missing_fonts == ["测试缺字体XY"], str(_j2.missing_fonts))
check("from_dict 还原 font_checked/font_fp",
      _j2.font_checked is True and _j2.font_fp == "abcd1234")
check("脏数据不炸（非字符串项被丢掉）",
      printer_config.PrintJob.from_dict(
          {"file_path": docx, "missing_fonts": ["ok字体", 3, None, ""]}).missing_fonts == ["ok字体"])

# 两种"缺"必须分开存（2026-11-01 现场：混成一个字段，行 tooltip 只能说一句含糊话）
_jne = printer_config.PrintJob(file_path=ne_pdf, copies=1,
                               nonembedded_fonts=["STSong-Light"], font_checked=True)
_dne = _jne.to_dict()
check("to_dict 带 nonembedded_fonts", _dne.get("nonembedded_fonts") == ["STSong-Light"],
      str(_dne.get("nonembedded_fonts")))
check("from_dict 还原 nonembedded_fonts",
      printer_config.PrintJob.from_dict(_dne).nonembedded_fonts == ["STSong-Light"])
check("nonembedded_fonts 脏数据同样被过滤",
      printer_config.PrintJob.from_dict(
          {"file_path": ne_pdf, "nonembedded_fonts": ["ok字体", 3, None, ""]}).nonembedded_fonts == ["ok字体"])
check("默认两个列表互不共享（可变默认值的坑）",
      printer_config.PrintJob(file_path=docx).nonembedded_fonts == [])

print("⑨⑩ 打印前闸门（缺字体弹确认 / 自动线不弹）")
try:
    import gui  # noqa: E402
    _has_gui = True
except Exception as _e:      # 没装 PySide6 的环境跳过这一段（其余判据仍然有效）
    _has_gui = False
    print(f"  ⏭ 无法 import gui（{type(_e).__name__}: {_e}），跳过闸门行为测试")

if _has_gui:
    class _Stub:
        """只借 MainWindow 的字体相关方法（不构造真窗口：那要 QApplication + 显示器）。"""
        _font_gate = gui.MainWindow._font_gate
        _font_sync_epoch = gui.MainWindow._font_sync_epoch
        _font_theme_colors = gui.MainWindow._font_theme_colors
        _font_tip_blocks = gui.MainWindow._font_tip_blocks

        def __init__(self):
            self.logs: list[str] = []
            self._config = types.SimpleNamespace(tabs={})
            self._current_tab = "1"
            self._theme_manager = None
            self._font_epoch_fp = ""

        def _log(self, msg):
            self.logs.append(str(msg))

    _asked: list[tuple] = []

    def _fake_question(parent, title, text, *a, **kw):
        _asked.append((title, text))
        return _Stub.answer

    _orig_q = gui.QMessageBox.question
    gui.QMessageBox.question = staticmethod(_fake_question)
    try:
        _gate_job = printer_config.PrintJob(file_path=docx, copies=1, page_count=1)
        _stub = _Stub()

        _Stub.answer = gui.QMessageBox.No
        _asked.clear()
        _r = _stub._font_gate([_gate_job], interactive=True)
        check("缺字体 + 点「否」⇒ 返回 False（不许开打）", _r is False, str(_r))
        check("确实弹了一次确认框", len(_asked) == 1, str(len(_asked)))
        check("弹框文案里点名了缺的字体",
              bool(_asked) and "测试缺字体XY" in _asked[0][1], str(_asked[:1]))
        check("取消时写了「用户取消打印」日志",
              any("取消打印" in m for m in _stub.logs), str(_stub.logs[-2:]))

        _Stub.answer = gui.QMessageBox.Yes
        _asked.clear()
        _stub2 = _Stub()
        _r2 = _stub2._font_gate([_gate_job], interactive=True)
        check("缺字体 + 点「是」⇒ 返回 True（继续打）", _r2 is True, str(_r2))
        # 刻意不记住上次的「是」：每次开打都是新决定 —— 用户可能是先放弃去装字体、
        # 也可能装了一半就想先打。命中已查结果时只是不重复扫盘，框照弹。
        _asked.clear()
        check("同一批再开打仍会确认一次（不复用上次的「是」）",
              _stub2._font_gate([_gate_job], interactive=True) is True and len(_asked) == 1,
              str(len(_asked)))

        # PDF 未嵌入字体 ≠ 本机没装：弹框必须讲对，否则用户会去「装字体」然后发现没用
        _ne_job = printer_config.PrintJob(file_path=ne_pdf, copies=1, page_count=1)
        _asked.clear()
        _Stub.answer = gui.QMessageBox.Yes
        _stub_ne = _Stub()
        _r_ne = _stub_ne._font_gate([_ne_job], interactive=True)
        _txt = _asked[0][1] if _asked else ""
        check("PDF 未嵌入字体：照样弹确认（不是静默开打）",
              _r_ne is True and len(_asked) == 1, f"r={_r_ne} asked={len(_asked)}")
        check("弹框讲的是「没把字体嵌进文件」，不是「本机没装」",
              "没有把字体嵌进文件" in _txt and "本机没有安装的字体" not in _txt, _txt[:160])
        check("弹框里点名了那个字体和 PDF 文件名",
              "STSong-Light" in _txt and "nonembedded.pdf" in _txt, _txt[:160])

        # 关键：**本机装了字体也一样要拦** —— 字形不在文件里，装字体不解决问题。
        # （现场：用户装完 STSong-Light 后 missing 变空，但打出来还是雅黑字形）
        _asked.clear()
        _ne_job2 = printer_config.PrintJob(file_path=ne_pdf, copies=1, page_count=1,
                                           missing_fonts=[])
        _stub_ne2 = _Stub()
        _r_ne2 = _stub_ne2._font_gate([_ne_job2], interactive=True)
        check("PDF 没嵌入字体：即使 missing_fonts 已为空也照样弹确认",
              len(_asked) == 1 and "没有把字体嵌进文件" in (_asked[0][1] if _asked else ""),
              f"asked={len(_asked)}")

        # 自动线：interactive=False 时一个框都不许弹
        _asked.clear()
        _stub3 = _Stub()
        _auto_job = printer_config.PrintJob(file_path=docx, copies=1, page_count=1)
        _r3 = _stub3._font_gate([_auto_job], interactive=False)
        check("自动打印线不弹模态、直接放行", _r3 is True and len(_asked) == 0,
              f"r={_r3} asked={len(_asked)}")
        check("自动线仍把缺字体写进日志",
              any("测试缺字体XY" in m for m in _stub3.logs), str(_stub3.logs[:2]))

        # 没有缺字体时：任何情况下都不弹、直接放行
        _ok_docx = os.path.join(tmp, "clean.docx")
        _make_docx(_ok_docx, ["宋体"], ["宋体"])
        _asked.clear()
        _stub4 = _Stub()
        _clean = printer_config.PrintJob(file_path=_ok_docx, copies=1, page_count=1)
        check("不缺字体 ⇒ 不弹框直接放行",
              _stub4._font_gate([_clean], interactive=True) is True and not _asked)
    finally:
        gui.QMessageBox.question = _orig_q

    # ⑩b 回写与日志：两种"缺"必须分开说 —— 这正是 2026-11-01 现场反馈的歧义点
    class _StubRow(_Stub):
        _on_font_checked = gui.MainWindow._on_font_checked

        def __init__(self, jobs):
            super().__init__()
            self._config = types.SimpleNamespace(tabs={"1": types.SimpleNamespace(jobs=jobs)})
            self._current_tab = "1"
            self.rows = []

        def _fill_table_row(self, idx, job):
            self.rows.append((idx, job))

    _both = printer_config.PrintJob(file_path=ne_pdf, copies=1)
    _sr = _StubRow([_both])
    _sr._on_font_checked(ne_pdf, ["测试缺字体XY"], "fpX", ["STSong-Light"])
    check("回写：本机没装的进 missing_fonts、未嵌入的进 nonembedded_fonts",
          _both.missing_fonts == ["测试缺字体XY"]
          and _both.nonembedded_fonts == ["STSong-Light"],
          f"{_both.missing_fonts} / {_both.nonembedded_fonts}")
    check("回写后只重画命中那一行", len(_sr.rows) == 1 and _sr.rows[0][0] == 0, str(_sr.rows))
    _logtxt = "\n".join(_sr.logs)
    check("日志把「文件没嵌入」与「本机没装」分成两条",
          "没把字体嵌进文件" in _logtxt and "用到本机没有的字体" in _logtxt, _logtxt[:240])
    check("未嵌入那条讲清「装了会自动用它、没装用内置字形」",
          "本机装了这个字体会自动用它渲染" in _logtxt, _logtxt[:240])

    # 关键：字体已经装好（missing 为空）时，未嵌入这件事照样得报 —— 否则用户以为没事了
    _ne_only = printer_config.PrintJob(file_path=ne_pdf, copies=1)
    _sr2 = _StubRow([_ne_only])
    _sr2._on_font_checked(ne_pdf, [], "fpY", ["STSong-Light"])
    check("missing 为空时仍报「没把字体嵌进文件」",
          any("没把字体嵌进文件" in m for m in _sr2.logs), str(_sr2.logs))
    check("missing 为空时不再说「本机没有的字体」",
          not any("用到本机没有的字体" in m for m in _sr2.logs), str(_sr2.logs))
    check("missing 为空时 nonembedded_fonts 仍被写上",
          _ne_only.nonembedded_fonts == ["STSong-Light"], str(_ne_only.nonembedded_fonts))

    # ⑩c 行 tooltip 文案（纯字符串）：两种"缺"各写一段，各说清该怎么办
    _tip_both = "\n\n".join(_Stub()._font_tip_blocks(_both))
    check("tooltip 里两种原因各占一段",
          "这份 PDF 生成时没把字体嵌进文件" in _tip_both
          and "本机没装这些字体" in _tip_both, _tip_both[:240])
    check("tooltip 点名了两个字体名",
          "STSong-Light" in _tip_both and "测试缺字体XY" in _tip_both, _tip_both[:240])
    check("未嵌入那段说明「装了就用它渲染」并把重新导出列为最稳出路",
          "本机装了这个字体 ⇒ 打印时自动用它渲染" in _tip_both
          and "重新导出「嵌入字体」的 PDF" in _tip_both)
    # 2026-11-02：出路从「重新添加这个文件」改成右键「🔄 重新转换」——
    # 因为重加文件走的是同一份转换缓存 + 同一份进程内字体缓存，
    # 人装了字体也不会重判（`font_check.refresh_installed()` 才真的重枚举）。
    check("本机没装那段给了「装上 → 右键重新转换」的出路",
          "把字体装上" in _tip_both and "重新转换" in _tip_both
          and "重新添加这个文件" not in _tip_both, _tip_both[:240])
    check("未嵌入那段不再吓唬人（页数/位置/分页不受影响要说）",
          "页数、位置、分页不受影响" in _tip_both)
    _tip_ne = "\n\n".join(_Stub()._font_tip_blocks(_ne_only))
    check("只有未嵌入时，tooltip 不出现「本机没装」误诊",
          "本机没装这些字体" not in _tip_ne
          and "本机装了这个字体 ⇒ 打印时自动用它渲染" in _tip_ne, _tip_ne[:240])
    _tip_miss = "\n\n".join(_Stub()._font_tip_blocks(
        printer_config.PrintJob(file_path=docx, copies=1, missing_fonts=["测试缺字体XY"])))
    check("只有本机没装时，tooltip 不出现「没嵌入」误诊",
          "没把字体嵌进文件" not in _tip_miss and "本机没装这些字体" in _tip_miss, _tip_miss[:240])
    check("不缺字体时 tooltip 为空（不标黄、不加提示）",
          _Stub()._font_tip_blocks(printer_config.PrintJob(file_path=docx, copies=1)) == [])

    print("⑪ GUI 接线（源码级断言：这些位置漏一处，功能等于没做）")
    _src = open(os.path.join(LOCAL_TOOL, "gui.py"), encoding="utf-8", errors="replace").read()
    check("_start_print_worker 有 interactive 参数", "interactive: bool = True" in _src)
    check("闸门在 _start_print_worker 里被调用",
          "self._font_gate(flat_jobs, interactive=interactive)" in _src)
    check("闸门之后才是冻结标签页（取消不留下已冻结的空单）",
          _src.index("self._font_gate(flat_jobs, interactive=interactive)")
          < _src.index("tab.frozen = True", _src.index("self._font_gate(flat_jobs")))
    _auto_sites = _src.count("interactive=False")
    check("自动打印线都传了 interactive=False（≥4 处）", _auto_sites >= 4, str(_auto_sites))
    check("_fill_table_row 里有缺字体标黄分支", 'name_item.setText(f"⚠️ {display}")' in _src)
    check("行 tooltip 走 _font_tip_blocks（文案只留一份，别内联复制）",
          "join(self._font_tip_blocks(job))" in _src)
    check("启动时会初始化字体检测", "self._init_font_check()" in _src)
    check("加文件后排队检查", "self._queue_font_checks(jobs[rows_before:]" in _src)
    check("字体变了会作废 PDF 缓存", "font_check.purge_pdf_cache()" in _src)

    check("打印前闸门走 selective（只清引用、不删 PDF）",
          "self._font_sync_epoch(selective=True)" in _src)
    _gate_body = _src.split("def _font_gate(", 1)[1].split("\n    def ", 1)[0]
    check("打印前闸门里**不调** purge_pdf_cache（2026-11-11 现场：删了 34 份 PDF ⇒ 预览失效）",
          "purge_pdf_cache" not in _gate_body)

    # ⑫ selective 的行为级验收：只清"用到变更字体"那几个，其余 cached_pdf 一动不动，
    #    并且**一个 PDF 文件都不删**（删文件 = 预览失效，2026-11-11 现场）。
    print("⑫ selective 作废：只清引用、不删文件")
    _tabs = {}
    _hit = printer_config.PrintJob(file_path=docx, copies=1, page_count=1)
    _hit.cached_pdf = os.path.join(_SANDBOX, "hit.pdf")
    _hit.missing_fonts = ["华文琥珀"]
    _miss = printer_config.PrintJob(file_path=docx, copies=1, page_count=1)
    _miss.cached_pdf = os.path.join(_SANDBOX, "miss.pdf")
    _miss.missing_fonts = ["别的字体"]
    _tab = types.SimpleNamespace(jobs=[_hit, _miss])
    _tabs["1"] = _tab

    _sel = _Stub()
    _sel._config = types.SimpleNamespace(tabs=_tabs)
    _calls = {"purge": 0}
    _orig_epoch = font_check.check_epoch
    _orig_purge = font_check.purge_pdf_cache
    font_check.check_epoch = lambda *a, **k: {
        "fingerprint": "fp-x", "changed": True,
        "added_names": ["华文琥珀"], "removed_names": []}
    font_check.purge_pdf_cache = lambda *a, **k: (_calls.__setitem__("purge", _calls["purge"] + 1) or 0)
    try:
        _changed = _sel._font_sync_epoch(selective=True)
    finally:
        font_check.check_epoch = _orig_epoch
        font_check.purge_pdf_cache = _orig_purge
    check("selective 返回 True（确实变了）", _changed is True, str(_changed))
    check("selective 一次 purge_pdf_cache 都不调（绝不删 PDF 文件）",
          _calls["purge"] == 0, str(_calls))
    check("用到变更字体的文件：cached_pdf 被清空（下次按新字体重转）",
          _hit.cached_pdf == "", repr(_hit.cached_pdf))
    check("没用到变更字体的文件：cached_pdf 原样保留（预览照常可用）",
          _miss.cached_pdf.endswith("miss.pdf"), repr(_miss.cached_pdf))
    check("两个文件的 font_checked 都重置（下次打印前按新指纹重查）",
          _hit.font_checked is False and _miss.font_checked is False)

print("")
if FAILED:
    print(f"❌ 失败 {len(FAILED)} 条 / 通过 {PASSED} 条")
    for f in FAILED:
        print(f"   - {f}")
    sys.exit(1)
print(f"✅ 全部通过（{PASSED} 条判据）")
sys.exit(0)
