# -*- coding: utf-8 -*-
r"""字体安装 —— 把用户下载好的 .ttf/.otf/.ttc 装进「当前用户」字体目录（不需要管理员）。

为什么单独成模块（2026-11-15 用户需求）：缺字体提示只告诉你「缺 TimesNewRomanPS-BoldMT」，
用户还得自己去搜、去下载、再右键「安装」。搜索这一步没法可靠自动化（来源五花八门：Google、
GitHub、字体站，还有授权问题），但「下载好之后装进系统」这一步完全可以一键化 ——
而手动装字体恰恰是最容易出岔子的一步（右键装到 HKLM 要 UAC；只装到 HKCU 时旧程序看不见）。

安装 = 四步都做，缺一步就会出现「装了但程序说没装」：
  ① 复制到 %LOCALAPPDATA%\Microsoft\Windows\Fonts（仅当前用户，不需要 UAC）；
  ② AddFontResourceW：让本进程/本次会话立刻能用；
  ③ 写 HKCU\...\Fonts 注册表项：重启后仍生效（值名 = 「字体名 (TrueType)」，值为文件路径）；
  ④ 广播 WM_FONTCHANGE：让 Word 等已在跑的进程立刻重新枚举。

本模块**不做**任何来源校验/授权判断：字体文件由用户自己提供，这里只负责「装进系统」。
也不改 HKLM、不要 UAC —— 打印工具是普通用户权限跑的。

2026-11-15 现场补充：从 PDF 里抽出来的「子集字体」通常只有几十 KB（只含那一份文件用到的字），
装上去也救不了别的文件 —— is_subset_candidate() 给界面用来提示这一点。
"""
from __future__ import annotations

import os
import shutil
import struct

FONT_EXTS = (".ttf", ".otf", ".ttc", ".otc", ".fon")

# 常见正文字体都在 300 KB ~ 1 MB；明显小于这个数多半是从 PDF 里提出来的子集
SUBSET_HINT_BYTES = 120 * 1024


def _fonts_dir() -> str:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "Microsoft", "Windows", "Fonts")


def is_font_file(path: str) -> bool:
    return str(path or "").lower().endswith(FONT_EXTS)


def _sfnt_offset(buf: bytes) -> int:
    """ttc/otc 是「字体集合」：头 12 字节后是各字体的表目录偏移；只看第一个。"""
    if len(buf) >= 16 and buf[:4] == b"ttcf":
        try:
            return int(struct.unpack(">I", buf[12:16])[0])
        except Exception:
            return 0
    return 0


def read_name_table(path: str) -> dict:
    """读 sfnt 的 name 表 → {nameID: 文本}（1=族名 4=全名 6=PostScript 名）。

    不依赖 fontTools（本机没装），手写最小解析：表目录 → name 表 → 记录 → UTF-16BE。
    解析失败一律返回空 dict，调用方退回文件名 —— 装字体不能因为读不出名字就放弃。
    """
    out: dict = {}
    try:
        with open(path, "rb") as f:
            buf = f.read(4 * 1024 * 1024)
    except OSError:
        return out
    try:
        base = _sfnt_offset(buf)
        if base + 12 > len(buf):
            return out
        num = struct.unpack(">H", buf[base + 4: base + 6])[0]
        table = {}
        for i in range(min(num, 64)):
            off = base + 12 + i * 16
            if off + 16 > len(buf):
                break
            tag = buf[off: off + 4]
            t_off, t_len = struct.unpack(">II", buf[off + 8: off + 16])
            table[tag] = (t_off, t_len)
        if b"name" not in table:
            return out
        noff, nlen = table[b"name"]
        if noff + 6 > len(buf):
            return out
        count, str_off = struct.unpack(">HH", buf[noff + 2: noff + 6])
        for i in range(min(count, 256)):
            rec = noff + 6 + i * 12
            if rec + 12 > len(buf):
                break
            pid, eid, lid, nid, ln, so = struct.unpack(">HHHHHH", buf[rec: rec + 12])
            if nid not in (1, 4, 6) or nid in out:
                continue
            start = noff + str_off + so
            raw = buf[start: start + ln]
            if not raw:
                continue
            try:
                text = raw.decode("utf-16-be") if pid == 3 else raw.decode("latin-1")
            except (UnicodeDecodeError, LookupError):
                continue
            text = text.replace("\x00", "").strip()
            if text and all(ord(ch) >= 32 for ch in text):
                out[nid] = text
    except (struct.error, ValueError, IndexError):
        return {}
    return out


def font_display_name(path: str) -> str:
    """字体给人的名字（优先全名，其次族名，最后文件名去扩展名）。"""
    names = read_name_table(path)
    for nid in (4, 1, 6):
        if names.get(nid):
            return names[nid]
    return os.path.splitext(os.path.basename(str(path or "")))[0]


def is_subset_candidate(path: str) -> bool:
    """是不是「从 PDF 里抽出来的子集字体」（几十 KB、只含那一份文件的字）。"""
    try:
        return 0 < os.path.getsize(path) < SUBSET_HINT_BYTES
    except OSError:
        return False


def _safe_dest(name: str, src: str) -> str:
    """目标文件名：用字体自己的全名，冲突时加 -2。扩展名沿用源文件。"""
    ext = os.path.splitext(src)[1] or ".ttf"
    bad = '<>:"/\\|?*'
    stem = "".join("_" if ch in bad else ch for ch in (name or "font")).strip(" .") or "font"
    d = _fonts_dir()
    dest = os.path.join(d, stem + ext)
    n = 2
    while os.path.exists(dest) and os.path.getsize(dest) != os.path.getsize(src):
        dest = os.path.join(d, "{0}-{1}{2}".format(stem, n, ext))
        n += 1
    return dest


def plan_install(paths) -> list:
    """只算出「会装成什么」，不动系统（给界面预览、给验收脚本断言）。"""
    out = []
    for p in (paths or []):
        p = str(p or "")
        if not p or not os.path.isfile(p):
            out.append({"path": p, "ok": False, "reason": "文件不存在"})
            continue
        if not is_font_file(p):
            out.append({"path": p, "ok": False, "reason": "不是字体文件（.ttf/.otf/.ttc）"})
            continue
        name = font_display_name(p)
        out.append({
            "path": p, "ok": True, "name": name,
            "dest": _safe_dest(name, p),
            "size": os.path.getsize(p),
            "subset": is_subset_candidate(p),
        })
    return out


def _add_font_resource(path: str) -> bool:
    try:
        import ctypes
        return bool(ctypes.windll.gdi32.AddFontResourceW(str(path)))
    except Exception:
        return False


def _broadcast_font_change() -> None:
    try:
        import ctypes
        from ctypes import wintypes
        HWND_BROADCAST = 0xFFFF
        WM_FONTCHANGE = 0x001D
        SMTO_ABORTIFHUNG = 0x0002
        ctypes.windll.user32.SendMessageTimeoutW(
            HWND_BROADCAST, WM_FONTCHANGE, 0, 0, SMTO_ABORTIFHUNG, 1000,
            ctypes.byref(wintypes.DWORD()))
    except Exception:
        pass


def _register(path: str, name: str) -> bool:
    """写 HKCU:\\...\\Fonts（用户级 = 不需要管理员，也不会污染其它账号）。"""
    try:
        import winreg
        key = winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER,
            r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts",
            0, winreg.KEY_SET_VALUE)
        try:
            winreg.SetValueEx(key, "{0} (TrueType)".format(name), 0, winreg.REG_SZ, str(path))
        finally:
            winreg.CloseKey(key)
        return True
    except Exception:
        return False


def install(paths, dry_run: bool = False) -> dict:
    """把字体装进「当前用户」。返回 {installed, skipped, errors, dry_run}。

    dry_run=True 只跑 plan_install，不碰文件系统 —— 验收脚本用它，绝不真的改机器字体。
    """
    rep = {"installed": [], "skipped": [], "errors": [], "dry_run": bool(dry_run)}
    for item in plan_install(paths):
        if not item.get("ok"):
            rep["skipped"].append((item.get("path", ""), item.get("reason", "")))
            continue
        if dry_run:
            rep["installed"].append(item.get("name") or item.get("path"))
            continue
        try:
            d = _fonts_dir()
            os.makedirs(d, exist_ok=True)
            dest = item["dest"]
            if not (os.path.exists(dest) and os.path.getsize(dest) == item["size"]):
                shutil.copy2(item["path"], dest)
            _add_font_resource(dest)
            if not _register(dest, item.get("name") or ""):
                rep["skipped"].append(
                    (item["path"], "复制成功但写注册表失败（本次会话可用，重启后失效）"))
                continue
            rep["installed"].append(item.get("name") or os.path.basename(dest))
        except OSError as e:
            rep["errors"].append((item.get("path", ""), str(e)))
    if rep["installed"] and not dry_run:
        _broadcast_font_change()
    return rep
