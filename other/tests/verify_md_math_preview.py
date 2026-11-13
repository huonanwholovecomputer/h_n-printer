# -*- coding: utf-8 -*-
"""md 的两个现场问题（2026-11-13）：

① 「md 文件不支持预览 PDF？」—— 右键「👁 预览 PDF」的判据要求 `cached_pdf` / 原件是 PDF /
   `page_count > 0`，而云端页数分析只覆盖 pdf/doc/docx ⇒ md 的页数永远是「?」，
   第一次打印之前菜单恒灰。现在加了第 ④ 条「源文件可转换」，点下去先现转一份再打开。
② 「md 文件貌似还不支持 LaTeX？」—— python-markdown 不解析 TeX，wkhtmltopdf 又禁 JS，
   所以 `$\\frac{x^2}{a^2}$` 原样打进 PDF。现在含公式的 md 改走「本机 Edge/Chrome 无头
   + 本地 KaTeX」渲染，其他 md 仍走原 wkhtmltopdf 链路（不动既有版式）。

这个脚本钉住：判定函数行为、资产齐全（含打包白名单）、浏览器探测、真转换一次、gui 侧
预览判据与排队转换的接线。跑法：`python other/tests/verify_md_math_preview.py`
"""
from __future__ import annotations

import ast
import io
import os
import re
import shutil
import sys
import tempfile
import types

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LP = os.path.join(ROOT, "local_print_tool")
CONV = os.path.join(LP, "converter.py")
GUI = os.path.join(LP, "gui.py")
SPECS = [os.path.join(LP, "HN打印工具.spec"), os.path.join(LP, "HN打印工具_debug.spec")]
ASSETS = os.path.join(LP, "assets", "katex")

sys.path.insert(0, LP)

_fails: list[str] = []
_passed = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global _passed
    if cond:
        _passed += 1
        print(f"[PASS] {name}")
    else:
        _fails.append(name)
        print(f"[FAIL] {name}" + (f" -- {extra}" if extra else ""))


def read(p: str) -> str:
    with io.open(p, "r", encoding="utf-8") as f:
        return f.read()


# ────────────────────────── 1) KaTeX 资产（随程序分发，别漏收） ──────────────────────────
print("\n=== 1) KaTeX 资产 ===")
for fname, minsize in (("katex.min.js", 200_000), ("auto-render.min.js", 2_000),
                       ("katex-inline.css", 200_000)):
    path = os.path.join(ASSETS, fname)
    check(f"资产存在：assets/katex/{fname}", os.path.isfile(path), path)
    check(f"资产大小合理：{fname}", os.path.getsize(path) >= minsize if os.path.isfile(path) else False)

_css = read(os.path.join(ASSETS, "katex-inline.css"))
check("css 里字体已内联（data:font/woff2）", "data:font/woff2;base64," in _css)
check("css 里没有 url(fonts/ 外链（打包后不会再找字体目录）", "url(fonts/" not in _css)
check("css 含 @font-face", "@font-face" in _css)
check("katex.min.js 是 KaTeX 本体", "katex" in read(os.path.join(ASSETS, "katex.min.js")).lower())

for spec in SPECS:
    txt = read(spec)
    check(f"打包白名单收了 assets/katex：{os.path.basename(spec)}",
          "('assets/katex', 'assets/katex')" in txt)

# ────────────────────────── 2) 判定 / 探测（真跑） ──────────────────────────
print("\n=== 2) _md_has_math / _find_headless_browser ===")
import converter  # noqa: E402  （converter 自带 fitz/docx 的 try/except 兜底，可独立导入）

for text, want in (
    ("$$\\frac{a}{b}$$", True),
    ("行内 $x^2+y^2=1$ 结束", True),
    ("\\[a=b\\]", True),
    ("\\(a=b\\)", True),
    ("# 标题\n\n没有公式的一段话。", False),
    ("今天花了 5 元，价格 $ 很便宜", False),
    ("a$b 只有一个美元符", False),
    ("", False),
):
    got = converter._md_has_math(text)
    check(f"_md_has_math({text[:18]!r}…) == {want}", got is want, f"got={got}")

css, js, auto = converter._katex_assets()
check("_katex_assets() 返回三段非空文本", all(len(x) > 1000 for x in (css, js, auto)))

_old_env = os.environ.get("HN_PRINT_BROWSER")
try:
    fake = os.path.abspath(sys.executable)
    os.environ["HN_PRINT_BROWSER"] = fake
    check("HN_PRINT_BROWSER 优先于自动探测", converter._find_headless_browser() == fake,
          converter._find_headless_browser())
finally:
    if _old_env is None:
        os.environ.pop("HN_PRINT_BROWSER", None)
    else:
        os.environ["HN_PRINT_BROWSER"] = _old_env

_real_isfile = os.path.isfile
try:
    os.path.isfile = lambda p: False  # type: ignore[assignment]
    check("找不到任何浏览器时返回空串（上层据此回落 wkhtmltopdf）",
          converter._find_headless_browser() == "")
finally:
    os.path.isfile = _real_isfile  # type: ignore[assignment]

# ────────────────────────── 3) 真转换：公式必须被排版，不能是原始 TeX ──────────────────────────
print("\n=== 3) 真转换（含 LaTeX 的 md） ===")
browser = converter._find_headless_browser()
if not browser:
    print("[SKIP] 本机没有 Edge/Chrome，公式链路无法真跑")
else:
    tmpdir = tempfile.mkdtemp(prefix="hnmdmath_")
    try:
        md = os.path.join(tmpdir, "t.md")
        with io.open(md, "w", encoding="utf-8") as f:
            f.write("# 标题\n\n行内 $\\frac{x^2}{a^2}+\\frac{y^2}{b^2}=1$ 公式。\n\n"
                    "$$\\vec{AB}\\cdot\\vec{AC}=0$$\n")
        out = os.path.join(tmpdir, "t.pdf")
        converter._convert_markdown_to_pdf(md, out)   # 该函数返回 None，产物落到 out
        check("含公式的 md 转出了 PDF", os.path.isfile(out), str(out))
        if os.path.isfile(out):
            check("PDF 体积合理（>3KB）", os.path.getsize(out) > 3000, str(os.path.getsize(out)))
            try:
                import fitz  # noqa: F401
                _doc = fitz.open(out)
                _text = "".join(p.get_text() for p in _doc)
                _doc.close()
                check("PDF 文本层不含原始 TeX（\\frac / \\vec 没被当正文打出来）",
                      "\\frac" not in _text and "\\vec" not in _text, _text[:120])
            except Exception as e:  # noqa: BLE001
                print(f"[SKIP] 没有 PyMuPDF，跳过文本层检查：{e}")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

# ────────────────────────── 4) 无公式的 md 仍走原链路 ──────────────────────────
print("\n=== 4) 无公式 md 走原 wkhtmltopdf 链路 ===")
_conv_src = read(CONV)
check("_convert_markdown_to_pdf 里公式分支在回落前 return",
      re.search(r"if _md_has_math\(.*?\):\s*\n\s*try:\s*\n\s*_convert_math_html_to_pdf\(", _conv_src, re.S) is not None)
check("公式渲染失败有 warning 回落（不是静默）",
      "公式渲染失败" in _conv_src and "logger.warning" in _conv_src)
check("回落分支仍调用 _convert_html_to_pdf（wkhtmltopdf）",
      "_convert_html_to_pdf(html" in _conv_src)

# ────────────────────────── 5) gui：预览判据 + 现转排队接线 ──────────────────────────
print("\n=== 5) gui 侧：md 也能点「预览 PDF」 ===")
_gui_src = read(GUI)
_tree = ast.parse(_gui_src)
_want = {"_preview_pdf_available", "_preview_pdf_convertible", "_preview_queue_convert",
         "_preview_convert_finished", "_preview_pdf_rows"}
_imported = [n for n in _tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
# 这几个都是 MainWindow 的方法 ⇒ 必须 ast.walk（类体里的 FunctionDef 不在 _tree.body 顶层）
_funcs = {n.name: n for n in ast.walk(_tree)
          if isinstance(n, ast.FunctionDef) and n.name in _want}
_ns: dict = {"os": os, "logger": types.SimpleNamespace(debug=lambda *a, **k: None,
                                                       warning=lambda *a, **k: None)}
exec(compile(ast.Module(body=_imported, type_ignores=[]), GUI, "exec"), _ns)
for name in _want:
    node = _funcs.get(name)
    check(f"gui.py 有 {name}()", node is not None)
    if node is not None:
        exec(compile(ast.Module(body=[node], type_ignores=[]), GUI, "exec"), _ns)


class _Job:
    def __init__(self, **kw):
        self.file_path = ""
        self.cached_pdf = ""
        self.page_count = 0
        self.original_name = ""
        self.display_name = ""
        self.__dict__.update(kw)


class _Win:
    def __init__(self):
        self._log_lines: list[str] = []
        self._current_tab = "tab1"
        self._convert_workers: list = []
        self._written = None

    def _log(self, msg: str) -> None:
        self._log_lines.append(msg)


_win = _Win()
for _m in ("_preview_pdf_convertible", "_preview_pdf_available"):
    if _m in _ns:
        setattr(_win, _m, types.MethodType(_ns[_m], _win))

_tmp = tempfile.mkdtemp(prefix="hnprev_")
try:
    md = os.path.join(_tmp, "a.md")
    with io.open(md, "w", encoding="utf-8") as f:
        f.write("# t\n")
    xz = os.path.join(_tmp, "a.xyz")
    with io.open(xz, "w", encoding="utf-8") as f:
        f.write("x")

    check("md（页数未知）也算可预览", _win._preview_pdf_convertible(_Job(file_path=md)) is True)
    check("不认得的扩展名不算可预览", _win._preview_pdf_convertible(_Job(file_path=xz)) is False)
    check("文件已不在 ⇒ 不算可预览", _win._preview_pdf_convertible(_Job(file_path=os.path.join(_tmp, "no.md"))) is False)
    check("空路径 ⇒ 不算可预览", _win._preview_pdf_convertible(_Job(file_path="")) is False)

    # 判据整体（④ 只是兜底，前三条件照旧）
    ok_pdf = os.path.join(_tmp, "c.pdf")
    with io.open(ok_pdf, "wb") as f:
        f.write(b"%PDF-1.4\n")
    check("cached_pdf 存在 ⇒ 可预览",
          _win._preview_pdf_available(_Job(file_path=md, cached_pdf=ok_pdf)) is True)
    check("原件就是 PDF ⇒ 可预览", _win._preview_pdf_available(_Job(file_path=ok_pdf)) is True)
    check("页数已知（>0）⇒ 可预览（重启后 cached_pdf 空也算）",
          _win._preview_pdf_available(_Job(file_path=os.path.join(_tmp, "gone.md"), page_count=3)) is True)
    check("md + 页数未知 ⇒ 兜底判可预览（本次修复点）",
          _win._preview_pdf_available(_Job(file_path=md)) is True)
    check("不认得的扩展名 + 页数未知 ⇒ 仍不可预览",
          _win._preview_pdf_available(_Job(file_path=xz)) is False)
finally:
    shutil.rmtree(_tmp, ignore_errors=True)

check("_preview_pdf_available 末尾走 _preview_pdf_convertible 兜底",
      re.search(r"return self\._preview_pdf_convertible\(job\)", _gui_src) is not None)
check("_preview_pdf_rows 在没产物时先排队现转",
      "self._preview_queue_convert(row, job, name)" in _gui_src)
check("现转走的是打印同一条链路（_start_convert_worker）",
      re.search(r"def _preview_queue_convert.*?_start_convert_worker\(", _gui_src, re.S) is not None)
check("现转请求带 from_cloud / allow_cloud_com（云端单不让 Word 碰）",
      "_allow_cloud_com" not in _gui_src or "allow_cloud_com=bool(_from_cloud and self._cloud_com_allowed(job))" in _gui_src)
check("转换完成用默认程序打开产物", "os.startfile(cached_pdf)" in _gui_src)
check("排队表按规范路径登记（避免同一文件两路请求）",
      "pending[key] = name" in _gui_src
      and "os.path.normcase(os.path.abspath(src))" in _gui_src)

# ────────────────────────── 6) gui：排队现转 → 转完自动打开（真跑） ──────────────────────────
print("\n=== 6) 现转排队 + 完成槽（真跑，桩掉 worker） ===")


class _FakeWorker:
    def __init__(self):
        self.finished = types.SimpleNamespace(connect=self._connect)

    def _connect(self, fn):
        self.slot = fn


class _Win2(_Win):
    def __init__(self):
        super().__init__()
        self.started: list[tuple] = []
        self.opened: list[str] = []

    def _resolve_engine(self, job):
        return "word"

    def _cloud_com_allowed(self, job):
        return False

    def _start_convert_worker(self, row, src, engine, tab_key="", from_cloud=False,
                              allow_cloud_com=False):
        self.started.append((row, src, engine, self._current_tab, from_cloud, allow_cloud_com))
        self._convert_workers.append(_FakeWorker())


for _m in ("_preview_pdf_convertible", "_preview_pdf_available", "_preview_queue_convert",
           "_preview_convert_finished"):
    if _m in _ns:
        setattr(_Win2, _m, _ns[_m])

_tmp2 = tempfile.mkdtemp(prefix="hnprev2_")
_real_startfile = getattr(os, "startfile", None)
try:
    md2 = os.path.join(_tmp2, "b.md")
    with io.open(md2, "w", encoding="utf-8") as f:
        f.write("# b\n")
    pdf2 = os.path.join(_tmp2, "b.pdf")
    with io.open(pdf2, "wb") as f:
        f.write(b"%PDF-1.4\n")

    w2 = _Win2()
    queued = w2._preview_queue_convert(3, _Job(file_path=md2), "b.md")
    key = os.path.normcase(os.path.abspath(md2))
    check("可转的 md 会排队现转", queued is True)
    check("排队表按规范路径登记了这份文件", key in getattr(w2, "_preview_convert_pending", {}))
    check("走的是打印同一条链路 _start_convert_worker", len(w2.started) == 1)
    check("交给 worker 的是源文件 + 解析出的引擎",
          bool(w2.started) and w2.started[0][1] == md2 and w2.started[0][2] == "word")
    check("日志告诉用户「正在转换、转好自动打开」",
          any("正在转换用于预览" in x for x in w2._log_lines), str(w2._log_lines))
    check("额外挂上了完成槽", bool(w2._convert_workers) and hasattr(w2._convert_workers[-1], "slot"))

    opened: list[str] = []
    os.startfile = lambda p: opened.append(p)   # type: ignore[assignment]
    w2._preview_convert_finished(3, md2, pdf2, 1, "portrait")
    check("转换完成 ⇒ 用默认程序打开了产物", opened == [pdf2], str(opened))
    check("打开后排队表清空（不会重复打开）", key not in w2._preview_convert_pending)
    check("写了一条「已用默认程序打开」日志",
          any("已用默认程序打开" in x for x in w2._log_lines), str(w2._log_lines))

    # 非排队文件的结果不该被这个槽吃掉
    w2._preview_convert_finished(0, os.path.join(_tmp2, "other.docx"), pdf2, 1, "")
    check("不是在排队的文件 ⇒ 完成槽不理它", len(opened) == 1, str(opened))

    # 转换失败：清排队 + 说清楚，不留悬着的「正在转换」
    w2._preview_queue_convert(4, _Job(file_path=md2), "b.md")
    w2._preview_convert_finished(4, md2, "", 0, "")
    check("转换失败 ⇒ 记一条失败日志", any("预览 PDF 转换失败" in x for x in w2._log_lines))
    check("失败也把排队表清干净", key not in w2._preview_convert_pending)
finally:
    if _real_startfile is not None:
        os.startfile = _real_startfile   # type: ignore[assignment]
    shutil.rmtree(_tmp2, ignore_errors=True)

print(f"\n{'=' * 60}\n结果：{_passed} 条通过，{len(_fails)} 条失败")
for name in _fails:
    print(f"  ✗ {name}")
sys.exit(0 if not _fails else 1)
