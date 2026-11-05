# -*- coding: utf-8 -*-
"""字体检查 —— 文件用了哪些字体、本机缺哪些、装了新字体后缓存要不要作废。

为什么查**源文件**而不是查转换出来的 PDF：Word 遇到本机没有的字体是**静默替换**的
（方正书宋_GBK → 宋体），转换产物里写的是替换后的字体名 —— 查 PDF 永远查不出"缺字体"。
所以 .docx 直接解析 OOXML 里的原始字体名。

.pdf 是例外：用户上传的 PDF 没经过我们转换，里面的 basefont 就是真相；但**内嵌**的字
体印得出来（驱动直接用里面的轮廓），只有「未内嵌 且 本机也没装」才算缺。

结论三态（调用方按 `ok` 决定要不要标黄/弹确认）：
    ok=True  全部可用（或本来就查不出 ⇒ 见下）
    ok=False 解析不出来（.doc 二进制 / 纯文本 / 图片 / 损坏文件）⇒ 不误报、不代表有缺
真正的"缺字体"看 `missing` 是否非空。

本模块**不依赖 Qt**（要在后台线程里跑，也要能在无 GUI 的验收脚本里跑）。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import zipfile

logger = logging.getLogger(__name__)

# ── 本机已安装字体（进程内缓存）────────────────────────────────────────────
# _installed_extra：GUI 主线程用 QFontDatabase 补种（Qt 的名单比 GDI 枚举更全，
# 尤其某些可变字体/新式字体族）；后台线程只读缓存，不自己调 Qt。
_installed: set[str] | None = None
_installed_extra: set[str] = set()

# 子集前缀：PDF 里内嵌字体常写成 "ABCDEF+SimSun"
_SUBSET_PREFIX = re.compile(r"^[A-Z]{6}\+")
# 主题字体引用（Word 里是 w:asciiTheme="minorHAnsi"，PDF 里是 +mn-lt/+mj-ea）→ 不是真字体名
_THEME_REF = re.compile(r"^(\+|mj-|mn-)", re.I)
# Type3 / 匿名资源名：F1 / C2_0 / TT0 …（没有任何字体身份，不能拿去和系统比）
_ANON = re.compile(r"^[A-Za-z]{1,3}\d{0,3}(_[0-9]+)?$")

# 中英文名互认（字号/样式后缀刻意**不**剥：等线 Light 与 等线 是两个不同字族）
_ALIAS = {
    "simsun": "宋体", "宋体": "宋体",
    "nsimsun": "新宋体", "新宋体": "新宋体",
    "simhei": "黑体", "黑体": "黑体",
    "kaiti": "楷体", "楷体": "楷体", "kaiti_gb2312": "楷体", "楷体_gb2312": "楷体",
    "fangsong": "仿宋", "仿宋": "仿宋", "fangsong_gb2312": "仿宋", "仿宋_gb2312": "仿宋",
    "microsoftyahei": "微软雅黑", "微软雅黑": "微软雅黑",
    "microsoftyaheiui": "微软雅黑ui", "微软雅黑ui": "微软雅黑ui",
    "simli": "隶书", "隶书": "隶书",
    "simyou": "幼圆", "幼圆": "幼圆",
    "stxihei": "华文细黑", "华文细黑": "华文细黑",
    "stsong": "华文宋体", "华文宋体": "华文宋体",
    "stkaiti": "华文楷体", "华文楷体": "华文楷体",
    "stfangso": "华文仿宋", "华文仿宋": "华文仿宋",
    "stzhongsong": "华文中宋", "华文中宋": "华文中宋",
    "stxingka": "华文行楷", "华文行楷": "华文行楷",
    "stliti": "华文隶书", "华文隶书": "华文隶书",
    "sthupo": "华文琥珀", "华文琥珀": "华文琥珀",
    "dengxian": "等线", "等线": "等线",
    "dengxianlight": "等线light", "等线light": "等线light",
}

# 永远不算"缺"的字体：
#  ① PDF 的 14 个标准字体（不内嵌也能靠阅读器/驱动代打，版式完全一致）；
#  ② Word 的默认西文（微软环境里 Word 自己保证有）。
_ALWAYS_OK = {
    "helvetica", "helveticaneue", "times", "timesnewroman", "timesroman",
    "courier", "couriernew", "symbol", "zapfdingbats", "arial",
    "calibri", "cambria", "segoeui", "tahoma", "verdana", "georgia",
}

# Word/Microsoft 365 **自带但不进系统字体表**的字族（云端字体，按需下载到 Office 私有缓存）。
# 系统字体枚举（GDI + 注册表）看不见它们，但 Word 自己拿得到 —— 实测 Word 转出的 PDF
# 里确实嵌了 Aptos 字形，版式不受影响。不豁免就会变成「每个用新模板的 docx 都报缺字体」。
# 2026-11-04 现场：三页文档测试.docx（Office 365 新默认主题）被报「本机没装 Aptos / Aptos Display」。
_WORD_CLOUD_FAMILIES = (
    "aptos",  # 2023 起 Word 的默认正文/标题西文字体（替代 Calibri），含 Display/Mono/Serif/Narrow
)

_JUNK_NAMES = {"", "-", "unknown", "default", "normal", "(未设置)", "none", "auto"}

# 语言标签误命中：`<w:lang w:eastAsia="zh-CN"/>` 也会被 w:eastAsia 正则抓到，必须剔掉
_LOCALE = re.compile(r"^[a-z]{2,3}(-[A-Za-z0-9]{2,8})+$")

# 字重/样式后缀：`等线 Light` 这种「家族名 + 字重」在 Word 里由同族字重渲染，
# 整族装了就不算缺（否则中文 Word 文档几乎人人弹「缺 等线 Light」，纯噪声）
_STYLE_TAILS = ("extralight", "ultralight", "semibold", "demibold", "extrabold",
                "light", "bold", "medium", "black", "heavy", "thin", "italic",
                "oblique", "regular")


# ── 名字归一 ────────────────────────────────────────────────────────────────
def _clean(name: str) -> str:
    """去掉 PDF 子集前缀与首尾空白，保留原样大小写（展示用原始名）。"""
    s = str(name or "").replace("\x00", "").strip()
    return _SUBSET_PREFIX.sub("", s).strip()


def _norm(name: str) -> str:
    """比较用的归一 key：小写、去空格/全角空格/@（@宋体 是竖排字体，同一族）。"""
    s = _clean(name).lower()
    for ch in (" ", "\u3000", "@", "\t"):
        s = s.replace(ch, "")
    return _ALIAS.get(s, s)


def _is_real_font(name: str) -> bool:
    """这个名字有没有资格拿去和系统字体比（过滤主题引用/匿名资源名/语言标签/占位符）。"""
    s = _clean(name)
    if not s or len(s) > 60:
        return False
    if _THEME_REF.match(s) or _ANON.match(s) or _LOCALE.match(s):
        return False
    return _norm(s) not in _JUNK_NAMES


# ── 本机已安装字体 ──────────────────────────────────────────────────────────
def _enum_gdi_fonts() -> set[str]:
    """用 GDI 枚举系统字族（与"Word 能不能用上"同一口径）。

    hdc=NULL 也能枚举（GDI 用一个虚拟屏幕 DC）。这里必须显式声明 argtypes/restype：
    64 位下 HDC 是指针，默认 c_int 返回值会被截断成垃圾句柄。
    """
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:  # 非 Windows / 极端环境
        return set()

    class LOGFONTW(ctypes.Structure):
        _fields_ = [
            ("lfHeight", wintypes.LONG), ("lfWidth", wintypes.LONG),
            ("lfEscapement", wintypes.LONG), ("lfOrientation", wintypes.LONG),
            ("lfWeight", wintypes.LONG), ("lfItalic", wintypes.BYTE),
            ("lfUnderline", wintypes.BYTE), ("lfStrikeOut", wintypes.BYTE),
            ("lfCharSet", wintypes.BYTE), ("lfOutPrecision", wintypes.BYTE),
            ("lfClipPrecision", wintypes.BYTE), ("lfQuality", wintypes.BYTE),
            ("lfPitchAndFamily", wintypes.BYTE),
            ("lfFaceName", wintypes.WCHAR * 32),
        ]

    class ENUMLOGFONTEXW(ctypes.Structure):
        _fields_ = [
            ("elfLogFont", LOGFONTW),
            ("elfFullName", wintypes.WCHAR * 64),
            ("elfStyle", wintypes.WCHAR * 32),
            ("elfScript", wintypes.WCHAR * 32),
        ]

    proc_type = ctypes.WINFUNCTYPE(
        ctypes.c_int, ctypes.POINTER(ENUMLOGFONTEXW),
        ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p)

    names: set[str] = set()

    def _cb(lpelf, _lpntm, _fonttype, _lparam):
        try:
            face = lpelf.contents.elfLogFont.lfFaceName
            if face:
                names.add(face)
        except Exception:
            pass
        return 1

    cb = proc_type(_cb)
    lf = LOGFONTW()
    lf.lfCharSet = 1  # DEFAULT_CHARSET：不限定字符集，否则只枚举到当前代码页的字族
    lf.lfFaceName = ""

    try:
        gdi32 = ctypes.WinDLL("gdi32")
        user32 = ctypes.WinDLL("user32")
        gdi32.EnumFontFamiliesExW.argtypes = [
            wintypes.HDC, ctypes.POINTER(LOGFONTW), proc_type,
            wintypes.LPARAM, wintypes.DWORD]
        gdi32.EnumFontFamiliesExW.restype = ctypes.c_int
        user32.GetDC.argtypes = [wintypes.HWND]
        user32.GetDC.restype = wintypes.HDC
        user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
        user32.ReleaseDC.restype = ctypes.c_int

        hdc = user32.GetDC(None)
        try:
            gdi32.EnumFontFamiliesExW(hdc, ctypes.byref(lf), cb, 0, 0)
        finally:
            if hdc:
                user32.ReleaseDC(None, hdc)
    except Exception as e:
        logger.debug(f"GDI 枚举字体失败: {e}")
    return names


_REG_SUFFIX = re.compile(r"\s*\((TrueType|OpenType|PostScript|Type 1|All res|TrueType Collection)\)\s*$",
                         re.I)


def _enum_registry_fonts() -> set[str]:
    """注册表兜底（GDI 枚举失败时用，也用来补齐"只为当前用户安装"的字体）。

    ⚠ 用户刚装的字体会落在 HKCU（"仅为我安装"），只读 HKLM 会漏 —— 这正是本功能
    最容易翻车的场景（用户装完字体，工具还说没装）。
    """
    out: set[str] = set()
    try:
        import winreg
    except Exception:
        return out
    roots = (
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts"),
    )
    for root, sub in roots:
        try:
            with winreg.OpenKey(root, sub) as key:
                n = winreg.QueryInfoKey(key)[1]
                for i in range(n):
                    try:
                        name, _val, _typ = winreg.EnumValue(key, i)
                    except OSError:
                        continue
                    name = _REG_SUFFIX.sub("", str(name or "")).strip()
                    # "Microsoft YaHei & Microsoft YaHei UI" ⇒ 两个族名
                    for part in re.split(r"\s*&\s*", name):
                        part = part.strip()
                        if part:
                            out.add(part)
        except OSError:
            continue
    return out


def set_installed(families) -> None:
    """GUI 主线程补种（QFontDatabase.families()）；调用后下次 installed_fonts() 重新汇总。"""
    global _installed
    try:
        for f in (families or []):
            f = str(f or "").strip()
            if f:
                _installed_extra.add(f)
    except Exception:
        pass
    _installed = None


def installed_fonts(refresh: bool = False) -> set[str]:
    """本机已安装字族（进程内缓存；refresh=True 重新枚举）。"""
    global _installed
    if _installed is None or refresh:
        s = _enum_gdi_fonts()
        s |= _enum_registry_fonts()
        s |= _installed_extra
        _installed = s
    return set(_installed)


_installed_norm_cache: tuple[int, set[str]] | None = None


def _installed_norm() -> set[str]:
    """归一后的已装字体 key 集合（按原始集合的 size+md5 缓存，避免每次打印重算几千个名字）。"""
    global _installed_norm_cache
    raw = installed_fonts()
    stamp = (len(raw), hash(tuple(sorted(raw))))
    if _installed_norm_cache and _installed_norm_cache[0] == stamp[1]:
        return _installed_norm_cache[1]
    keys = {_norm(n) for n in raw if _norm(n)}
    _installed_norm_cache = (stamp[1], keys)
    return keys


def refresh_installed(qt_families=None) -> str:
    """**强制重新枚举**本机字体，返回新的指纹（系统字体指纹口径见 fonts_fingerprint）。

    为什么需要（2026-11-02 现场）：用户中途装好缺的字体、把文件重新加一遍，⚠ 提示照旧。
    根因在本函数的两个缓存上 —— `_installed` 是进程内快照（GDI/注册表只在第一次枚举），
    `_installed_norm_cache` 又按"原始集合 hash"缓存归一结果。只把 `_installed` 置空而
    不一起作废归一缓存，指纹会照旧算成同一个值 ⇒ 调用方（`_queue_font_checks` 的
    `font_fp == fp` 判据）认为"这个文件查过了"直接跳过 —— 装了字体也不会重新判定。

    `qt_families`：GUI 侧传 `QFontDatabase.families()`（Qt 看得见、但个别字体 GDI 枚举
    不到的家族名，与 `set_installed` 同一口径）。
    """
    global _installed, _installed_norm_cache
    if qt_families:
        set_installed(qt_families)   # 内部会把 _installed 置空
    _installed = None
    _installed_norm_cache = None
    installed_fonts(refresh=True)
    return fonts_fingerprint()


def fonts_fingerprint() -> str:
    """系统字体指纹：装了/删了字体 ⇒ 变化。用来判定"旧转换缓存是否可信"。"""
    keys = sorted(_installed_norm())
    h = hashlib.md5()
    h.update("\n".join(keys).encode("utf-8", "ignore"))
    return h.hexdigest()[:16]


def is_installed(name: str) -> bool:
    """这个字体名本机有没有（含 14 标准字体与"家族 + 字重"回退）。"""
    return not _missing_key(_norm(name))


def _is_word_cloud(key: str) -> bool:
    """Word 自带（云端/私有）字族：系统字体表里查不到，但 Word 一定拿得到（见 _WORD_CLOUD_FAMILIES）。"""
    return any(key == p or key.startswith(p) for p in _WORD_CLOUD_FAMILIES)


def _missing_key(key: str) -> bool:
    """归一 key 是否算"本机缺"。

    家族名已装时，「家族名 + 字重」（等线 Light / 方正黑体_GBK Bold / Segoe UI Light）按
    有字重处理 —— Word 会拿同族渲染，版式一致；否则中文 Word 文档几乎人人弹「缺 等线 Light」。
    """
    if not key or key in _installed_norm() or key in _ALWAYS_OK or _is_word_cloud(key):
        return False
    for tail in _STYLE_TAILS:
        if key.endswith(tail) and len(key) > len(tail):
            base = key[:-len(tail)].strip()
            if base and (base in _installed_norm() or base in _ALWAYS_OK or _is_word_cloud(base)):
                return False
    return True


def missing_fonts(used) -> list[str]:
    """从"文件用到的字体名"里挑出本机没有的（保持原顺序、去重、原样大小写）。"""
    out: list[str] = []
    seen: set[str] = set()
    for raw in (used or []):
        if not _is_real_font(raw):
            continue
        key = _norm(raw)
        if not key or key in seen:
            continue
        seen.add(key)
        if not _missing_key(key):
            continue
        out.append(_clean(raw))
    return out


# ── .docx：解析 OOXML 里的原始字体名 ────────────────────────────────────────
_RFONT_RE = re.compile(r"""w:(?:ascii|hAnsi|eastAsia|cs)\s*=\s*["']([^"']{1,80})["']""")
_FONTNAME_RE = re.compile(r"""<w:font\b[^>]*\bw:name\s*=\s*["']([^"']{1,80})["']""")
# 样式表：<w:style w:styleId="a0">…<w:basedOn w:val="a"/></w:style>，正文用 <w:pStyle w:val="a0"/>
_STYLE_BLOCK_RE = re.compile(r"""<w:style\b([^>]*)>(.*?)</w:style>""", re.S)
_STYLE_ID_RE = re.compile(r"""w:styleId\s*=\s*["']([^"']{1,80})["']""")
_BASED_RE = re.compile(r"""<w:basedOn\b[^>]*\bw:val\s*=\s*["']([^"']{1,80})["']""")
_USED_STYLE_RE = re.compile(r"""<w:(?:pStyle|rStyle|tblStyle)\b[^>]*\bw:val\s*=\s*["']([^"']{1,80})["']""")
_DOCDEFAULT_RE = re.compile(r"""<w:docDefaults\b.*?</w:docDefaults>""", re.S)
# 正文/页眉页脚/脚注/尾注/域：真正会落到纸上的内容都在这些部件里
_BODY_PARTS = ("document.xml", "header", "footer", "footnotes.xml", "endnotes.xml",
               "comments.xml", "numbering.xml")
_MAX_XML_BYTES = 64 * 1024 * 1024
_STYLE_HOPS = 8

# 主题字体引用：`w:asciiTheme="minorHAnsi"` —— 不是字体名，真名在 word/theme/theme1.xml 里
_THEME_ATTR_RE = re.compile(
    r"""w:(?:asciiTheme|hAnsiTheme|eastAsiaTheme|cstheme)\s*=\s*["']([^"']{1,40})["']""")
# Word 的主题 token → theme1.xml 里的槽位
_THEME_ROLE = {
    "majorascii": "mj-lt", "majorhansi": "mj-lt", "majoreastasia": "mj-ea", "majorbidi": "mj-cs",
    "minorascii": "mn-lt", "minorhansi": "mn-lt", "minoreastasia": "mn-ea", "minorbidi": "mn-cs",
}


def _docx_part_names(zf: zipfile.ZipFile, include_styles: bool) -> list[str]:
    out: list[str] = []
    for info in zf.infolist():
        n = info.filename or ""
        if not n.startswith("word/") or not n.endswith(".xml"):
            continue
        base = n.rsplit("/", 1)[-1]
        if base in ("fontTable.xml", "settings.xml", "webSettings.xml", "theme1.xml"):
            # fontTable/settings 由下面单独处理；theme 里是 +mn-lt 这类符号引用
            continue
        if base.startswith("header") or base.startswith("footer"):
            out.append(n)
            continue
        if base in _BODY_PARTS:
            out.append(n)
            continue
        if base == "styles.xml" and include_styles:
            out.append(n)
    return out


def _docx_style_fonts(styles_xml: str, used_ids: set[str]) -> list[str]:
    """只捞「正文点名用到的样式」里的字体，沿 `basedOn` 链往上找，近的优先。

    模板的 `styles.xml` 常驻一堆根本没用到的字体定义（标题 1、页眉横幅、表格样式…），
    全并进来就是误报源；只有被 `<w:pStyle>/<w:rStyle>/<w:tblStyle>` 点过的才算数。
    """
    if not styles_xml or not used_ids:
        return []
    blocks: dict[str, tuple[list[str], str]] = {}
    for attrs, body in _STYLE_BLOCK_RE.findall(styles_xml):
        mid = _STYLE_ID_RE.search(attrs)
        if not mid:
            continue
        based = _BASED_RE.search(body)
        blocks[mid.group(1)] = (_RFONT_RE.findall(body), based.group(1) if based else "")
    out: list[str] = []
    for sid in used_ids:
        name, hops = sid, 0
        while name and hops < _STYLE_HOPS:
            fonts, parent = blocks.get(name, ([], ""))
            if fonts:
                out.extend(fonts)
                break
            name, hops = parent, hops + 1
    return out


def _docx_style_theme_tokens(styles_xml: str, used_ids: set[str]) -> list[str]:
    """样式里**真正会被用到**的主题字体引用（`docDefaults` + 点名的样式，沿 basedOn 链）。

    不能整份 styles.xml 抓：模板里「标题 1」等样式常年写着 `majorHAnsi`，正文没用过它，
    抓进来就会把 Aptos Display 算成正文用到的字体（2026-11-04 现场那份 docx 就是这样）。
    """
    if not styles_xml:
        return []
    out: list[str] = []
    dd = _DOCDEFAULT_RE.search(styles_xml)
    if dd:
        out.extend(_THEME_ATTR_RE.findall(dd.group(0)))
    if not used_ids:
        return out
    blocks: dict[str, tuple[str, str]] = {}
    for attrs, body in _STYLE_BLOCK_RE.findall(styles_xml):
        mid = _STYLE_ID_RE.search(attrs)
        if not mid:
            continue
        based = _BASED_RE.search(body)
        blocks[mid.group(1)] = (body, based.group(1) if based else "")
    for sid in used_ids:
        name, hops = sid, 0
        while name and hops < _STYLE_HOPS:
            body, parent = blocks.get(name, ("", ""))
            if body:
                toks = _THEME_ATTR_RE.findall(body)
                if toks:
                    out.extend(toks)
                    break
            name, hops = parent, hops + 1
    return out


def _theme_font_map(theme_xml: str) -> dict[str, str]:
    """`word/theme/theme1.xml` → {"mj-lt": "Aptos Display", "mn-lt": "Aptos", "mn-ea": "等线", …}。

    `typeface` 为空表示「跟随系统默认」（Word 里 East Asian 主题常年是空的），
    这种槽位不进表 —— 拿空字符串去比对系统字体表只会得出个假结论。
    """
    out: dict[str, str] = {}
    if not theme_xml:
        return out
    for kind, block in re.findall(r"<a:(majorFont|minorFont)>(.*?)</a:\1>", theme_xml, re.S):
        prefix = "mj" if kind == "majorFont" else "mn"
        for tag, slot in (("latin", "lt"), ("ea", "ea"), ("cs", "cs")):
            m = re.search(rf"<a:{tag}\b[^>]*\btypeface=\"([^\"]*)\"", block)
            if m and m.group(1).strip():
                out[f"{prefix}-{slot}"] = m.group(1).strip()
    return out


def fonts_in_docx(path: str) -> tuple[list[str], bool]:
    """返回 (用到的字体名列表, 是否解析成功)。

    口径（2026-11-01 修正，2026-11-04 补主题字体）：**只认正文里真正落字的字体引用** ——
      · 正文/页眉页脚/脚注/尾注的 `w:rFonts`；
      · 这些部件点名用到的样式（`w:pStyle` / `w:rStyle` / `w:tblStyle`）沿 `basedOn` 链的 `w:rFonts`；
      · `docDefaults` / 样式里的 `w:asciiTheme` 这类**主题引用**（按 `theme1.xml` 换成真名）；
      · `docDefaults` 里写死的默认字体。
    `word/fontTable.xml` **只当最后兜底**：Word 从模板起稿时会把主题字体整套登记进去
    （Aptos / Aptos Display / 等线 / Calibri…），正文压根没用过 —— 2026-11-01 现场就是
    正文只有宋体/楷体，却被报「本机没装 Aptos」。

    2026-11-04 现场（三页文档测试.docx，Office 365 新模板）：正文/样式一个显式字体名都没有，
    全靠 `w:asciiTheme="minorHAnsi"` ⇒ 老逻辑「读不出名字」直接退回整张 fontTable，
    把从没用过的 Aptos Display / Arial / Times New Roman 一起报成缺。现在先解析主题，
    查不到真名才退回 fontTable，免得把模板登记的无关字体当成正文用法。
    """
    explicit: list[str] = []
    style_fonts: list[str] = []
    used_ids: set[str] = set()
    ft_fonts: list[str] = []
    theme_tokens: list[str] = []
    theme_xml = ""
    try:
        with zipfile.ZipFile(path) as zf:
            names = set(i.filename for i in zf.infolist())
            total = 0
            for n in _docx_part_names(zf, include_styles=False):
                try:
                    total += int(zf.getinfo(n).file_size or 0)
                except KeyError:
                    continue
                if total > _MAX_XML_BYTES:
                    logger.debug(f"docx 部件过大，只解析已读部分: {path}")
                    break
                try:
                    text = zf.read(n).decode("utf-8", "ignore")
                except (KeyError, OSError, zipfile.BadZipFile):
                    continue
                explicit.extend(_RFONT_RE.findall(text))
                theme_tokens.extend(_THEME_ATTR_RE.findall(text))
                used_ids.update(_USED_STYLE_RE.findall(text))
            if "word/styles.xml" in names:
                try:
                    st = zf.read("word/styles.xml").decode("utf-8", "ignore")
                except (KeyError, OSError, zipfile.BadZipFile):
                    st = ""
                if st:
                    style_fonts = _docx_style_fonts(st, used_ids)
                    theme_tokens.extend(_docx_style_theme_tokens(st, used_ids))
                    dd = _DOCDEFAULT_RE.search(st)
                    if dd:
                        style_fonts.extend(_RFONT_RE.findall(dd.group(0)))
            if "word/theme/theme1.xml" in names:
                try:
                    theme_xml = zf.read("word/theme/theme1.xml").decode("utf-8", "ignore")
                except (KeyError, OSError, zipfile.BadZipFile):
                    theme_xml = ""
            if "word/fontTable.xml" in names:
                try:
                    ft_fonts = _FONTNAME_RE.findall(
                        zf.read("word/fontTable.xml").decode("utf-8", "ignore"))
                except (KeyError, OSError, zipfile.BadZipFile):
                    ft_fonts = []
    except (zipfile.BadZipFile, OSError) as e:
        logger.debug(f"docx 字体解析失败: {path} ({e})")
        return [], False
    # 主题引用 → 真字体名（同一批 token 只查一次；未解析到的 token 直接丢掉）
    theme_fonts: list[str] = []
    if theme_tokens:
        tmap = _theme_font_map(theme_xml)
        for tok in theme_tokens:
            slot = _THEME_ROLE.get(tok.strip().lower())
            if slot and tmap.get(slot):
                theme_fonts.append(tmap[slot])
    # 在这里就把非字体名滤掉，别留到 missing_fonts 再滤：
    # `<w:lang w:eastAsia="zh-CN"/>` 和 `<w:rFonts .../>` 是同族属性，正则会把语言标签一起抓出来；
    # 让它漏进返回值，界面/日志上就会显示「用到的字体：zh-CN」这种鬼东西。
    used = [n for n in explicit + style_fonts if _is_real_font(n)]
    if not used:
        # 整篇靠主题字体排（Office 365 新模板默认就是这种）：只有这时候才轮到主题真名
        used = [n for n in theme_fonts if _is_real_font(n)]
    if not used:
        used = [n for n in ft_fonts if _is_real_font(n)]
    return list(dict.fromkeys(used)), True


# ── .pdf：查 basefont，内嵌的不算缺 ─────────────────────────────────────────
def _fonts_in_pdf_detail(path: str) -> tuple[list[str], list[str], list[str], bool]:
    """返回 (PDF 引用的字体名, 缺的字体名, **没嵌入**的字体名, 是否解析成功)。

    内嵌字体（FontFile/FontFile2/FontFile3 有内容）**不算缺**：打印时驱动直接用文件里的
    轮廓，版式与原文件一致；只有"没内嵌 + 本机也没装"才会被替换。

    第三种返回值（没嵌入）是给界面说话用的：两者要分开讲 ——
      · `没嵌入` ＝ 文件里根本没有字形数据，**谁渲染谁替**（MuPDF 用内置无衬线 CJK 兜底，
        Adobe/福昕用自己的东方字体包），所以"屏幕上是宋体、打出来是雅黑"就这么来的；
      · `本机没装` ＝ 文件里写着要这个字体，这台机没装（docx 走 Word 转换时被替换）。
    现场 2026-10-31：iText 5.5.10 生成的学生成绩单 PDF，字体 `STSong-Light` + `UniGB-UCS2-H`
    编码、extract_font 返回 0 字节 ＝ 纯非嵌入 CID 字体。
    """
    try:
        import fitz  # PyMuPDF
    except Exception as e:  # pragma: no cover - 依赖缺失
        logger.debug(f"PyMuPDF 不可用，跳过 PDF 字体检查: {e}")
        return [], [], [], False
    used: list[str] = []
    missing: list[str] = []
    nonembedded: list[str] = []
    try:
        doc = fitz.open(path)
    except Exception as e:
        logger.debug(f"PDF 打开失败（字体检查跳过）: {path} ({e})")
        return [], [], [], False
    try:
        seen: set[tuple[int, str]] = set()
        for pno in range(int(doc.page_count or 0)):
            try:
                fonts = doc.get_page_fonts(pno, full=False)
            except Exception:
                continue
            for item in fonts or []:
                row = list(item) + [""] * 6
                xref, ftype, basefont = row[0], row[2], row[3]
                base = _clean(basefont)
                if not _is_real_font(base):
                    continue
                key = (int(xref or 0), _norm(base))
                if key in seen:
                    continue
                seen.add(key)
                used.append(base)
                embedded = False
                try:
                    if xref:
                        got = doc.extract_font(xref)
                        embedded = bool(got and len(got) > 3 and got[3])
                except Exception:
                    embedded = False
                # Type3 没有独立字体文件（内容就是字形绘制指令），交给驱动即可印
                _is_type3 = str(ftype or "") == "Type3"
                if not embedded and not _is_type3:
                    nonembedded.append(base)
                if not _missing_key(_norm(base)):
                    continue
                if not embedded and not _is_type3:
                    missing.append(base)
    finally:
        try:
            doc.close()
        except Exception:
            pass
    return used, missing, nonembedded, True


def fonts_in_pdf(path: str) -> tuple[list[str], list[str], bool]:
    """返回 (PDF 引用的字体名, 缺的字体名, 是否解析成功)。详见 `_fonts_in_pdf_detail`。"""
    used, missing, _ne, ok = _fonts_in_pdf_detail(path)
    return used, missing, ok


def pdf_nonembedded_fonts(path: str) -> list[str]:
    """这份 PDF 里**没有嵌入字形**的字体名（打印时只能被替换）。"""
    try:
        return list(_fonts_in_pdf_detail(path)[2])
    except Exception:
        return []


# ── 统一入口 ────────────────────────────────────────────────────────────────
_CHECKABLE_EXTS = (".docx", ".pdf")


def checkable(path: str) -> bool:
    """这个扩展名能不能静态查字体（.doc 二进制 / 纯文本 / 图片都查不出，别误报）。"""
    return os.path.splitext(str(path or ""))[1].lower() in _CHECKABLE_EXTS


def check_file(path: str) -> dict:
    """检查一个文件。返回 {ok, fonts, missing, nonembedded, reason, fingerprint}。

    ok=False 只表示"这个文件没法静态检查"（.doc/.txt/图片/损坏），**不代表有缺字体**。
    `nonembedded` 只对 PDF 有意义：这些字体在文件里没有字形数据（打印时必然被替换）。
    """
    ext = os.path.splitext(str(path or ""))[1].lower()
    out = {"ok": False, "fonts": [], "missing": [], "nonembedded": [], "reason": "",
           "fingerprint": fonts_fingerprint()}
    if not path or not os.path.isfile(path):
        out["reason"] = "文件不存在"
        return out
    if ext == ".docx":
        used, ok = fonts_in_docx(path)
        out["ok"] = ok
        out["fonts"] = list(dict.fromkeys(_clean(n) for n in used if _is_real_font(n)))
        out["missing"] = missing_fonts(out["fonts"])
        out["reason"] = "" if ok else "docx 解析失败（可能是 .doc 改了扩展名）"
        return out
    if ext == ".pdf":
        used, missing, nonembedded, ok = _fonts_in_pdf_detail(path)
        out["ok"] = ok
        out["fonts"] = list(dict.fromkeys(used))
        out["missing"] = list(dict.fromkeys(missing))
        out["nonembedded"] = list(dict.fromkeys(nonembedded))
        out["reason"] = "" if ok else "PDF 打不开"
        return out
    out["reason"] = f"{ext or '未知类型'} 无法静态检查字体（.doc 建议另存为 .docx）"
    return out


# ── 系统字体变化 ⇒ 转换缓存作废 ─────────────────────────────────────────────
def _state_path() -> str:
    try:
        import paths as _paths
        return os.path.join(_paths.get_app_data_dir(), "font_epoch.json")
    except Exception:
        return ""


def read_epoch_state(state_path: str | None = None) -> dict:
    p = state_path or _state_path()
    if not p or not os.path.isfile(p):
        return {}
    try:
        with open(p, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def write_epoch_state(state: dict, state_path: str | None = None) -> None:
    p = state_path or _state_path()
    if not p:
        return
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
        os.replace(tmp, p)
    except OSError as e:
        logger.debug(f"字体指纹落盘失败: {e}")


def check_epoch(state_path: str | None = None, record: bool = True) -> dict:
    """比对当前系统字体与上次记录：返回 {changed, added, removed, fingerprint, names}。

    首次运行（没有记录文件）不算 changed —— 否则升级后第一次启动就把全部缓存清了。
    """
    names = sorted(_installed_norm())
    fp = fonts_fingerprint()
    old = read_epoch_state(state_path)
    old_names = old.get("names") if isinstance(old.get("names"), list) else []
    old_set = set(old_names)
    cur_set = set(names)
    added = sorted(cur_set - old_set) if old_set else []
    removed = sorted(old_set - cur_set) if old_set else []
    changed = bool(old_set) and fp != (old.get("fingerprint") or "")
    if record:
        write_epoch_state({"fingerprint": fp, "count": len(names),
                           "names": names[:8000], "ts": int(__import__("time").time())},
                          state_path)
    return {"changed": changed, "added": len(added), "removed": len(removed),
            "added_names": added[:10], "removed_names": removed[:10], "fingerprint": fp}


def purge_pdf_cache(cache_dir: str | None = None) -> int:
    """清空转换缓存里的 PDF（系统字体变了 ⇒ 旧 PDF 是用替代字体渲染的，必须重转）。

    只删 *.pdf/.pdf.tmp；索引文件留着 —— 索引条目找不到文件时读侧本来就按未命中处理
    （cloud_client._get_cached_pdf 先 isfile 再返回），删索引反而要跨模块耦合。
    返回删除的文件数。
    """
    if cache_dir is None:
        try:
            import paths as _paths
            cache_dir = _paths.pdf_cache_dir()
        except Exception:
            return 0
    n = 0
    try:
        entries = os.listdir(cache_dir)
    except OSError:
        return 0
    for name in entries:
        low = name.lower()
        if not (low.endswith(".pdf") or low.endswith(".pdf.tmp")):
            continue
        p = os.path.join(cache_dir, name)
        try:
            if os.path.isfile(p):
                os.remove(p)
                n += 1
        except OSError as e:
            logger.debug(f"清理缓存失败: {p} ({e})")
    return n
