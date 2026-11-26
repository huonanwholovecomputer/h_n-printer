# -*- coding: utf-8 -*-
"""验收 · .doc 的「作者程序」识别（决定首选转换引擎）

背景（2026-11-25 现场）：`计科2504朱佳琳.doc` 由 WPS 12.1 生成，页码是页脚里的**文本框**。
Word 渲染时文本框里的「12」「13」只输出第一位「1」（实测同一份文件：WPS 转出 p14/p15 =
12/13，Word 转出 1/1）⇒ 老格式 .doc 也必须按「谁生成的」选引擎，不能一律 Word 优先。

.doc 里没有 XML，作者痕迹是字节串：`WpsCustomData`（WPS 私有存储流）/ `WPS Office` /
`Microsoft Office Word` / `MSWordDoc`。两种编码都要扫 —— CFB 流名与 WPS 属性串是 UTF-16LE，
老 Word 按 ANSI(GBK) 存的 SummaryInformation 是裸 ASCII（2026-11-25 微信 301 份实测）。本脚本覆盖：
 1. `converter._read_doc_last_editor` 的判定表（WpsCustomData 优先、word 次之、ASCII/UTF-16LE、
    大小写、空/随机/非 .doc、跨 1 MiB 块边界）；
 2. `converter._read_office_last_editor` 统一入口仍对 .docx 转调 `_read_docx_last_editor`
    （多处验收脚本 stub 的就是它，不能被绕开）；
 3. 源码接线：转换/添加文件/云端任务/页数分析四处都走统一入口，且缓存按 .doc 隔离；
 4. 现场文件（存在才算）识别为 wps。
"""
import os
import sys
import tempfile

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LOCAL_TOOL = os.path.join(REPO, "local_print_tool")
sys.path.insert(0, LOCAL_TOOL)
os.chdir(LOCAL_TOOL)

import converter as cv  # noqa: E402

ok_all = True


def check(name, cond, detail=""):
    global ok_all
    if not cond:
        ok_all = False
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


TMP = tempfile.mkdtemp(prefix="doc_engine_detect_")


def make(path, payload: bytes) -> str:
    with open(path, "wb") as f:
        f.write(payload)
    return path


def u16(s: str) -> bytes:
    return s.encode("utf-16-le")


# ─────────────────────────────────────────────────────────────
print("=== 1. _read_doc_last_editor：字节扫 UTF-16LE 判定作者程序 ===")

p = make(os.path.join(TMP, "wps_流名.doc"), b"\xd0\xcf\x11\xe0" + b"\x00" * 64 + u16("WpsCustomData"))
check("WpsCustomData（WPS 私有流）⇒ wps", cv._read_doc_last_editor(p) == "wps",
      repr(cv._read_doc_last_editor(p)))

p = make(os.path.join(TMP, "wps_程序名.doc"), b"\x00" * 32 + u16("WPS Office_12.1.0.29690"))
check("程序名里只有 WPS Office ⇒ wps", cv._read_doc_last_editor(p) == "wps",
      repr(cv._read_doc_last_editor(p)))

p = make(os.path.join(TMP, "kingsoft.doc"), b"\x00" * 8 + u16("Kingsoft Writer"))
check("Kingsoft ⇒ wps", cv._read_doc_last_editor(p) == "wps",
      repr(cv._read_doc_last_editor(p)))

p = make(os.path.join(TMP, "word_程序名.doc"), b"\x00" * 32 + u16("Microsoft Office Word"))
check("Microsoft Office Word ⇒ word", cv._read_doc_last_editor(p) == "word",
      repr(cv._read_doc_last_editor(p)))

p = make(os.path.join(TMP, "msworddoc.doc"), b"\x00" * 8 + u16("MSWordDoc"))
check("MSWordDoc ⇒ word", cv._read_doc_last_editor(p) == "word",
      repr(cv._read_doc_last_editor(p)))

p = make(os.path.join(TMP, "both.doc"),
         b"\x00" * 8 + u16("Microsoft Office Word") + b"\x00" * 8 + u16("WpsCustomData"))
check("WPS 私有流残留 + Word 程序名 ⇒ word（Application 是最后一次保存者）",
      cv._read_doc_last_editor(p) == "word", repr(cv._read_doc_last_editor(p)))

p = make(os.path.join(TMP, "stream_only.doc"),
         b"\x00" * 8 + u16("WpsCustomData"))
check("只有 WPS 私有流、没有程序名 ⇒ wps",
      cv._read_doc_last_editor(p) == "wps", repr(cv._read_doc_last_editor(p)))

p = make(os.path.join(TMP, "no_stream_both.doc"),
         b"\x00" * 8 + u16("WPS Office") + b"\x00" * 8 + u16("Microsoft Office Word"))
check("两种程序名都有 ⇒ word（后者记的是最后保存者）",
      cv._read_doc_last_editor(p) == "word", repr(cv._read_doc_last_editor(p)))

p = make(os.path.join(TMP, "upper.doc"), b"\x00" * 8 + u16("WPS OFFICE"))
check("大写变体也能认（WPS OFFICE ⇒ wps）", cv._read_doc_last_editor(p) == "wps",
      repr(cv._read_doc_last_editor(p)))

p = make(os.path.join(TMP, "random.doc"), bytes(range(256)) * 64)
check("看不出来 ⇒ None（调用方回落到原来的 Word 优先链）",
      cv._read_doc_last_editor(p) is None, repr(cv._read_doc_last_editor(p)))

p = make(os.path.join(TMP, "empty.doc"), b"")
check("空文件 ⇒ None", cv._read_doc_last_editor(p) is None)

p = make(os.path.join(TMP, "ascii_only.doc"), b"WpsCustomData" + b"WPS Office")
check("只有 ASCII（ANSI 属性串）也能识别 ⇒ wps",
      cv._read_doc_last_editor(p) == "wps", repr(cv._read_doc_last_editor(p)))

# 2026-11-25 微信 301 份 .doc 实测：老 Word 把 SummaryInformation 按 ANSI(GBK) 存，
# 程序名是**裸 ASCII**（71 份里一个 UTF-16LE 标记都没有），只扫 UTF-16LE 会全判 None。
p = make(os.path.join(TMP, "ansi_word.doc"), b"\x00" * 16 + b"Microsoft Office Word")
check("ANSI 存的程序名（裸 ASCII）也能认 ⇒ word",
      cv._read_doc_last_editor(p) == "word", repr(cv._read_doc_last_editor(p)))

p = make(os.path.join(TMP, "ansi_msworddoc.doc"), b"MSWordDoc")
check("ANSI MSWordDoc ⇒ word",
      cv._read_doc_last_editor(p) == "word", repr(cv._read_doc_last_editor(p)))

p = make(os.path.join(TMP, "ansi_wps.doc"), b"WPS Office_12.8.2.18205")
check("ANSI WPS Office ⇒ wps",
      cv._read_doc_last_editor(p) == "wps", repr(cv._read_doc_last_editor(p)))

p = make(os.path.join(TMP, "named.docx"), u16("WpsCustomData"))
check("扩展名不是 .doc ⇒ None（.docx 走 XML 判定）", cv._read_doc_last_editor(p) is None)

check("不存在的路径 ⇒ None（不抛异常）",
      cv._read_doc_last_editor(os.path.join(TMP, "nope.doc")) is None)

# 标记正好跨 1 MiB 分块边界：前 5 字节在首块、其余在下一块
marker = u16("WpsCustomData")
boundary = make(os.path.join(TMP, "boundary.doc"), b"\x00" * ((1 << 20) - 5) + marker)
check("标记跨 1 MiB 分块边界也能命中（块间 overlap 生效）",
      cv._read_doc_last_editor(boundary) == "wps", repr(cv._read_doc_last_editor(boundary)))

# ─────────────────────────────────────────────────────────────
print("\n=== 2. _read_office_last_editor：统一入口不绕开 .docx 的旧函数 ===")

_docx_calls = []
_orig_docx = cv._read_docx_last_editor


def _stub_docx(path):
    _docx_calls.append(path)
    return "wps"


cv._read_docx_last_editor = _stub_docx
try:
    got = cv._read_office_last_editor(os.path.join(TMP, "x.docx"))
    check("入口仍转调 _read_docx_last_editor（验收脚本 stub 它才继续有效）",
          got == "wps" and len(_docx_calls) == 1, f"{got!r}/{len(_docx_calls)}")
finally:
    cv._read_docx_last_editor = _orig_docx

check("入口对 .doc 走字节扫（同一个 wps 文件 ⇒ wps）",
      cv._read_office_last_editor(os.path.join(TMP, "wps_流名.doc")) == "wps")
check("入口对其它扩展名 ⇒ None",
      cv._read_office_last_editor(os.path.join(TMP, "a.txt")) is None
      and cv._read_office_last_editor(os.path.join(TMP, "a.pdf")) is None)

# ─────────────────────────────────────────────────────────────
print("\n=== 3. 源码接线：转换/添加文件/云端任务/页数分析都走统一入口 ===")

_conv = open(os.path.join(LOCAL_TOOL, "converter.py"), encoding="utf-8").read()
_gui = open(os.path.join(LOCAL_TOOL, "gui.py"), encoding="utf-8").read()
_cc = open(os.path.join(LOCAL_TOOL, "cloud_client.py"), encoding="utf-8").read()

check("converter._convert_office_to_pdf 用 _read_office_last_editor 定首选引擎",
      'if _read_office_last_editor(file_path) == "wps":' in _conv
      and "preferred = \"wps\"" in _conv)
check("gui 不再用「.docx 才读作者」的旧写法",
      'if ext == ".docx" else None' not in _gui
      and "_read_docx_last_editor(work_path) if" not in _gui)
check("gui 里添加文件与云端任务都调统一入口（≥2 处）",
      _gui.count("_read_office_last_editor(") >= 2,
      f"{_gui.count('_read_office_last_editor(')} 处")
check("gui 转换 worker 的注释明确 .doc 也带引擎后缀",
      "`.doc` / `.docx` 缓存 key 含引擎后缀" in _gui)
check("cloud_client 的隔离扩展名清单包含 .doc",
      '_CACHE_ENGINE_ISOLATED_EXTS = (".doc", ".docx")' in _cc)
check("页数分析侧也对 .doc 推断引擎（_infer_cache_engine 用隔离清单 + 统一入口）",
      "_read_office_last_editor(path)" in _cc
      and "if ext not in _CACHE_ENGINE_ISOLATED_EXTS or not path:" in _cc)
check("脏条目清理不再把 .doc 的 _e* 当垃圾",
      "src_ext not in _CACHE_ENGINE_ISOLATED_EXTS" in _cc)

# ─────────────────────────────────────────────────────────────
print("\n=== 4. 现场文件（存在才算）===")

LIVE = r"C:\Users\Administrator\Desktop\计科2504朱佳琳.doc"
if os.path.isfile(LIVE):
    got = cv._read_doc_last_editor(LIVE)
    check("★ 现场 .doc（WPS 12.1 生成）识别为 wps", got == "wps", repr(got))
    got_office = cv._read_office_last_editor(LIVE)
    check("统一入口对现场文件同样给 wps", got_office == "wps", repr(got_office))
else:
    print(f"[SKIP] 现场文件不存在：{LIVE}")

print("\n" + ("=" * 60))
print("结果：" + ("全部通过" if ok_all else "存在失败项"))
sys.exit(0 if ok_all else 1)
