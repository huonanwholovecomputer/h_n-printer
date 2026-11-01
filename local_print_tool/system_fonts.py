"""system_fonts.py — 让打印用上「本机装了、但 PDF 没把字形嵌进去」的字体。

现场（2026-11-01）：
    PDF 里写的是 `STSong-Light-UniGB-UCS2-H`，但生成方（政务/校园系统，iText 一类）
    **只写了字体名、没带字形**。打印走 PyMuPDF(MuPDF) **整页栅格化**（pdf_printer.py
    的 `_print_pdf_native`：`page.get_pixmap(...)` → GDI `StretchDIBits`），而 MuPDF
    **不查 Windows 字体表**，遇到没嵌入的字体一律用自己内置的兜底字形 —— 中文是一套
    无衬线黑体，观感就是「微软雅黑」。于是操作员明明装了 STSong-Light，打出来还是黑体。

    MuPDF 留了钩子 `fz_install_load_system_font_funcs`：找不到嵌入字形时先问宿主程序
    （PyMuPDF 2.x 是 SWIG director 版，可直接传 Python 回调）。本模块注册一个 **CJK**
    回调：按归一化族名在注册表（HKLM + HKCU，用户级安装的字体落在后者的
    `%LOCALAPPDATA%\\Microsoft\\Windows\\Fonts`）里找同名字体文件，命中就用它渲染。

没命中一律返回 None ⇒ 退回 MuPDF 内置兜底，行为与改造前**完全一致**（宁可不动，也别猜错字体）。
只装 CJK 回调（拉丁文的 `f` 回调传 None）：拉丁字体替换的排版风险大，收益小，不碰。

⚠ 一次性、且要趁早
    `fz_install_load_system_font_funcs` 在 MuPDF 里只生效一次；字体解析结果还会进 MuPDF
    的字体缓存。⇒ 必须在该进程**第一次渲染 PDF 之前**装好（见 `pdf_printer._ensure_system_fonts`
    在「空白页扫描」和「原生打印」两处渲染之前调用）。装晚了，那一页已经按兜底字形缓存过了。

⚠ 只影响「没嵌入的字体」
    嵌入完整的 PDF 一律照原样渲染（MuPDF 直接用文件里的字形），本模块连问都问不到。
"""
from __future__ import annotations

import logging
import os
import re
import threading

logger = logging.getLogger(__name__)

# 注册表值名后缀：`STSong-Light-UniGB-UCS2-H (TrueType)` → `STSong-Light-UniGB-UCS2-H`
_REG_SUFFIX = re.compile(r"\s*\((?:TrueType|OpenType|Type1|TrueType Collection|Bitmap)\)\s*$", re.I)

# 允许出现的「额外词」：字体名后面的编码/变体标记。CJK 字体在注册表里常带
# `-UniGB-UCS2-H` 这种后缀，而 MuPDF 问的是光秃秃的族名（`STSong-Light`）。
# 额外词必须全部落在这个集合里才算「同一个族」—— `SimSun` vs `SimSun-ExtB`
# 的 `extb` 不在集合内 ⇒ 直接放弃匹配（ExtB 只有扩展 B 区字形，用它渲染正文会全变豆腐块）。
_ENC_TOKENS = frozenset({
    "uni", "unigb", "unicns", "unijis", "uniks", "unifull",
    "ucs", "ucs2", "ucs4", "utf", "utf8",
    "gb", "gbk", "gb18030", "gb2312", "big5", "hkscs", "cns", "cns1", "cns2",
    "jis", "jis78", "jis83", "jis90", "ksc", "sjis", "euc",
    "h", "v", "w", "winansi", "identity", "cid",
})

_LOCK = threading.Lock()
_FAMILY: dict[str, str] | None = None      # 归一化键（token 串）→ 字体文件绝对路径
_OVERRIDE: dict[str, str] = {}             # 测试注入：{token 串: path}
_INSTALLED = False
_HITS: list[tuple[str, str]] = []          # [(MuPDF 问的名字, 用的文件)]
_MISSES: list[str] = []                    # 问过但本机没有的名字


def _tokens(name: str) -> list[str]:
    """族名 → 小写 token 表。`STSong-Light` → ['stsong', 'light']。"""
    s = _REG_SUFFIX.sub("", str(name or "")).strip().lower()
    return [t for t in re.split(r"[^a-z0-9]+", s) if t]


def _is_prefix(short: list[str], long_: list[str]) -> bool:
    """short 是 long_ 的前缀（含相等）。"""
    return bool(short) and len(short) <= len(long_) and long_[:len(short)] == short


def _norm(name: str) -> str:
    """族名 → 归一化键（token 用空格连起来；空串表示不可用）。"""
    return " ".join(_tokens(name))


def _match_key(name: str, keys) -> str | None:
    """在 keys 里找与 name 同族的键。

    只接受两类匹配，其余一律 None（宁可不动）：
      ① 完全相同；
      ② name 的 token 是 key 的 token 前缀，且**多出来的 token 全是编码/变体词**
         （`stsong light` → `stsong light unigb ucs2 h`）。
    多候选时取「多出来的 token 最少」的那个。
    """
    want = _tokens(name)
    if not want:
        return None
    want_key = " ".join(want)
    best: tuple[int, str] | None = None
    for k in keys:
        if k == want_key:
            return k
        kt = k.split(" ")
        if _is_prefix(want, kt):                  # 问的名字更短：stsong light → …unigb ucs2 h
            extra = kt[len(want):]
        elif _is_prefix(kt, want):                # 问的名字更全：反向也要成立
            extra = want[len(kt):]
        else:
            continue
        if not extra or not set(extra) <= _ENC_TOKENS:
            continue
        if best is None or len(extra) < best[0]:
            best = (len(extra), k)
    return best[1] if best else None


def _resolve(fname: str, base_dir: str) -> str | None:
    """注册表值 → 存在的字体文件绝对路径（注册表里可能只写文件名）。"""
    fname = str(fname or "").strip().strip('"')
    if not fname:
        return None
    cands = [fname] if os.path.isabs(fname) else []
    if not cands:
        cands = [os.path.join(base_dir, fname)]
        # 用户级安装的字体：%LOCALAPPDATA%\Microsoft\Windows\Fonts
        lad = os.environ.get("LOCALAPPDATA")
        if lad:
            cands.append(os.path.join(lad, "Microsoft", "Windows", "Fonts", fname))
        # 少数驱动把字体摊在 system32 / 系统目录下
        sysroot = os.environ.get("SystemRoot") or r"C:\Windows"
        cands.append(os.path.join(sysroot, "System32", fname))
        cands.append(os.path.join(sysroot, fname))
    for c in cands:
        try:
            if os.path.isfile(c):
                return os.path.abspath(c)
        except OSError:
            continue
    return None


def font_files(refresh: bool = False) -> dict[str, str]:
    """本机字体：{归一化键: 文件路径}（进程内缓存）。只收注册表里带文件名的项。"""
    global _FAMILY
    with _LOCK:
        if _FAMILY is not None and not refresh:
            return _FAMILY
        table: dict[str, str] = {}
        try:
            import winreg
        except Exception:
            _FAMILY = table
            return table
        windir = os.environ.get("SystemRoot") or r"C:\Windows"
        base = os.path.join(windir, "Fonts")
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
                            name, val, _typ = winreg.EnumValue(key, i)
                        except OSError:
                            continue
                        path = _resolve(val, base)
                        if not path:
                            continue
                        reg = _REG_SUFFIX.sub("", str(name or ""))
                        # `Microsoft YaHei & Microsoft YaHei UI` ⇒ 两个族名，同一文件
                        for part in re.split(r"\s*&\s*", reg):
                            k = _norm(part)
                            if k:
                                table.setdefault(k, path)
            except OSError:
                continue
        _FAMILY = table
        return table


def lookup(name: str) -> str | None:
    """MuPDF 问的族名 → 本机字体文件；没有同族就 None（调用方退回内置兜底）。"""
    table = dict(font_files())
    table.update(_OVERRIDE)          # 注入项优先（验收要能确定地覆盖真实注册表）
    key = _match_key(name, table.keys())
    if not key:
        return None
    return table.get(key)


def set_override(mapping: dict | None) -> None:
    """测试用：强制把某些族名映射到指定文件（不影响真实注册表扫描）。"""
    with _LOCK:
        _OVERRIDE.clear()
        for k, v in (mapping or {}).items():
            nk = _norm(k)
            if nk and v:
                _OVERRIDE[nk] = str(v)


def hits() -> list[tuple[str, str]]:
    """已命中的替换记录（排障用：打印出来像「宋体」但不知道用了哪个文件时看这里）。"""
    return list(_HITS)


def misses() -> list[str]:
    """问过但本机没有的族名（这些仍走 MuPDF 内置兜底）。"""
    return list(_MISSES)


def _mupdf():
    import pymupdf
    return pymupdf.mupdf


def _cb_cjk(name, ordering, serif):
    """MuPDF 的 CJK 字体回调（ordering: 0=Adobe-CNS 1=Adobe-GB 2=Adobe-Japan 3=Adobe-Korea 4=Adobe-GB1?）。

    返回 fz_font 就让 MuPDF 用它渲染，返回 None 退回内置兜底。
    任何异常都吞掉返回 None —— 打印不能因为字体找不齐就失败。
    """
    try:
        path = lookup(str(name or ""))
        if not path:
            if len(_MISSES) < 500:
                _MISSES.append(str(name or ""))
            return None
        m = _mupdf()
        font = m.fz_new_font_from_file(str(name), path, 0, 0)
        if font is None:
            return None
        if len(_HITS) < 500:
            _HITS.append((str(name or ""), path))
            logger.info(f"系统字体接管未嵌入字体: {name} -> {path}")
        return font
    except Exception as e:  # pragma: no cover - 回调里不许抛
        logger.debug(f"系统字体回调失败 ({name}): {e}")
        return None


def install() -> bool:
    """装上 CJK 系统字体钩子（幂等；安装失败返回 False，打印照旧走内置兜底）。

    ⚠ 锁内**不许**再调 font_files()/lookup()：`_LOCK` 不是可重入锁，而 font_files()
    也要拿它（第一版就这么写，`install()` 直接把自己锁死 —— 2026-11-01 实测）。
    """
    global _INSTALLED
    with _LOCK:
        if _INSTALLED:
            return True
        try:
            m = _mupdf()
            m.fz_install_load_system_font_funcs(None, _cb_cjk, None)
        except Exception as e:
            logger.warning(f"未嵌入字体的系统字体接管不可用（打印仍走 MuPDF 内置兜底）: {e}")
            return False
        _INSTALLED = True
    try:
        logger.info(f"已装系统字体钩子：本机字体 {len(font_files())} 项可按名接管未嵌入的 CJK 字体")
    except Exception:
        pass
    return True


def installed() -> bool:
    return _INSTALLED


def _reset_for_tests() -> None:
    """仅测试用：清掉缓存与命中记录（钩子本身在进程内不可撤销）。"""
    global _INSTALLED, _FAMILY
    with _LOCK:
        _INSTALLED = False
        _FAMILY = None
        _OVERRIDE.clear()
        _HITS.clear()
        _MISSES.clear()
