# -*- coding: utf-8 -*-
"""验收 · 🟡-D5 第 4 条：三引擎降级链必须校验产物（COM「静默成功」要能继续降级）

缺陷（审查报告 D5）：`_convert_office_to_pdf` 只看"引擎函数没抛异常"就 `return` —— 而
Word/WPS 的 COM 导出会**静默成功**：`ExportAsFixedFormat` 正常返回、不抛异常，产物却是
0 字节 / 半截文件 / 根本不是 PDF。真正的产物校验在**链外**（`convert_to_pdf` 末尾），
于是这类情况 LibreOffice 兜底**永远轮不到**，用户拿到一个打不开的文件（还可能被当成
"转换成功"送去打印）。

修复后的语义（本脚本钉住）：
  A. 每一级引擎返回后都判产物（存在 + 非空 + `%PDF` 头）；不达标 → 丢掉残次品继续降级。
  B. 真的产出合法 PDF → 立即返回，**不再**调用后面的引擎（零回归，不许多余动作）。
  C. 引擎抛异常 → 照旧降级（原有路径不变）。
  D. 链外校验与链内**同一份实现**（口径只有一份），且 `%PDF` 头也认。

用法: python tests/verify_converter_chain.py     （exit 0 = 全绿）
"""
import io
import os
import shutil
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL_TOOL = os.path.join(REPO, "local_print_tool")

_SANDBOX = tempfile.mkdtemp(prefix="hn_conv_chain_")
os.environ["APPDATA"] = _SANDBOX
sys.path.insert(0, LOCAL_TOOL)
os.chdir(LOCAL_TOOL)

import converter as cv          # noqa: E402

ok_all = True


def check(name, cond, detail=""):
    global ok_all
    if not cond:
        ok_all = False
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


def _blank_comment_lines(text):
    """`#` 之后置等长空格（保字符偏移）—— 判据不许吃注释。"""
    out = []
    for line in text.split("\n"):
        i = line.find("#")
        out.append(line if i < 0 else line[:i] + " " * (len(line) - i))
    return "\n".join(out)


_WORK = tempfile.mkdtemp(prefix="hn_conv_work_")
_OUT_SEQ = [0]                     # 每轮产物路径唯一（防"陈旧产物"骗过判定）
_DOCX = os.path.join(_WORK, "报告.docx")
_DOC = os.path.join(_WORK, "报告.doc")
for _p in (_DOCX, _DOC):
    with open(_p, "wb") as f:
        f.write(b"PK\x03\x04 fake office")


class _Run:
    """一次 _convert_office_to_pdf 的调用记录。"""

    def __init__(self):
        self.engines = []          # 被调用过的引擎
        self.saw_existing_output = []  # 「轮到我时，上一个引擎的残次品还在吗」

    def stub(self, monkeypatch_engines: dict):
        self.engines = []
        self.saw_existing_output = []

        def _wrap(name, fn):
            short = name.replace("_convert_office_via_", "").replace("_convert_via_", "") \
                        .replace("_com", "")

            def _inner(file_path, output_pdf):
                self.engines.append(short)
                self.saw_existing_output.append(
                    (short, os.path.isfile(output_pdf) and os.path.getsize(output_pdf) > 0))
                return fn(file_path, output_pdf)
            return _inner

        for name, fn in monkeypatch_engines.items():
            setattr(cv, name, _wrap(name, fn))
        return self


def _run_chain(src, word=None, wps=None, lo=None, last_editor=None):
    prev = (cv._detect_word, cv._detect_wps, cv._read_docx_last_editor,
            cv._convert_via_word_com, cv._convert_via_wps_com, cv._convert_office_via_libreoffice)
    cv._detect_word = lambda: True
    cv._detect_wps = lambda: True
    cv._read_docx_last_editor = lambda p: last_editor
    run = _Run().stub({
        "_convert_via_word_com": word or (lambda f, o: open(o, "wb").write(b"%PDF-1.4 word")),
        "_convert_via_wps_com": wps or (lambda f, o: open(o, "wb").write(b"%PDF-1.4 wps")),
        "_convert_office_via_libreoffice": lo or (lambda f, o: open(o, "wb").write(b"%PDF-1.4 lo")),
    })
    # 输出路径必须**每轮唯一**：共用同一个文件名会让上一轮留下的合法 PDF 变成"陈旧产物"，
    # 把"这一轮引擎其实什么都没写"骗成判定通过（写这个脚本时真踩到过：A3 假红）。
    # 同时先建一个空壳文件，与真实调用方（mkstemp 建空文件后交给转换链）保持一致。
    _OUT_SEQ[0] += 1
    out = os.path.join(_WORK, "out_%d.pdf" % _OUT_SEQ[0])
    open(out, "wb").close()
    try:
        cv._convert_office_to_pdf(src, out)
        body = open(out, "rb").read() if os.path.isfile(out) else b""
    finally:
        (cv._detect_word, cv._detect_wps, cv._read_docx_last_editor,
         cv._convert_via_word_com, cv._convert_via_wps_com,
         cv._convert_office_via_libreoffice) = prev
    return run, out, body


print("=== A. COM 静默成功（0 字节 / 非 PDF）必须继续降级 ===")
# A1：Word 正常返回但什么都不写（0 字节是 mkstemp 留下的空壳）
run, out, body = _run_chain(_DOC, word=lambda f, o: None)
check("★ Word 写不出东西 → 仍继续调用 WPS", run.engines[:2] == ["word", "wps"], str(run.engines))
check("★ 最终产物是 WPS 写的合法 PDF（改前这里拿到的是一份空文件）",
      body.startswith(b"%PDF-1.4 wps"), repr(body[:20]))

# A2：Word 写了个「不是 PDF」的非空文件
run, out, body = _run_chain(_DOC, word=lambda f, o: open(o, "wb").write("<html>非PDF</html>".encode("utf-8")))
check("★ Word 产出非 PDF 的非空文件 → 也要降级到 WPS",
      run.engines[:2] == ["word", "wps"] and body.startswith(b"%PDF-1.4 wps"),
      f"{run.engines} / {body[:16]!r}")

# A3：两个 COM 都静默成功（0 字节）→ 必须落到 LibreOffice
run, out, body = _run_chain(_DOC, word=lambda f, o: None, wps=lambda f, o: None)
check("★ Word/WPS 都静默成功 → 最终落到 LibreOffice 兜底",
      run.engines == ["word", "wps", "libreoffice"], str(run.engines))
check("★ 最终产物是 LibreOffice 写的（改前 WPS 那次就 return 了，兜底轮不到）",
      body.startswith(b"%PDF-1.4 lo"), repr(body[:20]))

check("★ 降级前残次品已被丢弃（下一引擎看不到上一个引擎的空壳）",
      run.saw_existing_output[1:] == [("wps", False), ("libreoffice", False)],
      str(run.saw_existing_output))

# A4：docx 首选 WPS（最后编辑者是 wps）时同样成立
run, out, body = _run_chain(_DOCX, last_editor="wps", wps=lambda f, o: None)
check("★ docx 首选 WPS 静默成功 → 降级到 Word（顺序也正确）",
      run.engines[:2] == ["wps", "word"] and body.startswith(b"%PDF-1.4 word"),
      f"{run.engines} / {body[:16]!r}")

print("=== B. 合法产物 → 立即返回（零回归，不许多余调用）===")
run, out, body = _run_chain(_DOC)
check("Word 产出合法 PDF → 只调用 Word 一次就结束",
      run.engines == ["word"] and body.startswith(b"%PDF-1.4 word"),
      f"{run.engines} / {body[:16]!r}")

print("=== C. 引擎抛异常 → 照旧降级（原有路径不变）===")


def _boom(f, o):
    raise RuntimeError("模拟 COM 失败")


run, out, body = _run_chain(_DOC, word=_boom)
check("Word 抛异常 → 降级到 WPS 并成功", run.engines[:2] == ["word", "wps"]
      and body.startswith(b"%PDF-1.4 wps"), f"{run.engines} / {body[:16]!r}")

print("=== D. 产物判定只有一份实现（链内链外同源）===")
_SRC = open(os.path.join(LOCAL_TOOL, "converter.py"), encoding="utf-8").read()
_CODE = _blank_comment_lines(_SRC)
check("源码里有 _pdf_product_ok 实现", "def _pdf_product_ok(" in _CODE)
check("★ 降级链内两处都判产物（Word / WPS 各一次）",
      _CODE.count("if _pdf_product_ok(output_pdf):") == 2,
      str(_CODE.count("if _pdf_product_ok(output_pdf):")))
check("★ 链外旧写法已消失（不再直接 getsize == 0）",
      "os.path.getsize(output_pdf) == 0" not in _CODE)
check("★ 链外改用同一实现（convert_to_pdf 末尾也调 _pdf_product_ok）",
      _CODE.count("_pdf_product_ok(output_pdf)") == 3,
      str(_CODE.count("_pdf_product_ok(output_pdf)")))
check("★ 无效产物会被丢弃（降级前先删残次品）",
      _CODE.count("_discard_invalid_pdf(output_pdf)") == 2,
      str(_CODE.count("_discard_invalid_pdf(output_pdf)")))

# 行为级再钉一次判定口径（避免静态判据被改名绕过）。
# ⚠ 用 getattr 兜底：反向验证时源码退回改前版本、**没有** _pdf_product_ok ——
# 直接 `cv._pdf_product_ok(...)` 会 AttributeError 崩在中间，那时"红"就不落在断言上了
# （项目老教训：崩在中间不算红）。
_get_ok = getattr(cv, "_pdf_product_ok", None)
_ok_pdf = os.path.join(_WORK, "ok.pdf")
with open(_ok_pdf, "wb") as f:
    f.write(b"%PDF-1.4\n%%EOF\n")
_empty = os.path.join(_WORK, "empty.pdf")
open(_empty, "wb").close()
_notpdf = os.path.join(_WORK, "notpdf.pdf")
with open(_notpdf, "wb") as f:
    f.write(b"this is not a pdf")
check("判定口径：合法 PDF → True", _get_ok is not None and _get_ok(_ok_pdf) is True)
check("判定口径：0 字节 → False", _get_ok is not None and _get_ok(_empty) is False)
check("判定口径：非 PDF 内容 → False", _get_ok is not None and _get_ok(_notpdf) is False)
check("判定口径：文件不存在 → False",
      _get_ok is not None and _get_ok(os.path.join(_WORK, "nope.pdf")) is False)

print("\n" + "=" * 52)
print("转换降级链产物校验:", "全部通过 ✅" if ok_all else "存在失败 ❌")
shutil.rmtree(_SANDBOX, ignore_errors=True)
shutil.rmtree(_WORK, ignore_errors=True)
sys.exit(0 if ok_all else 1)
