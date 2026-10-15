# -*- coding: utf-8 -*-
"""TXT/CSV/MD 文本编码自动探测验收（`converter._read_text_auto`）

为什么值得单独验收：`_read_text_auto` 的 docstring 一直宣称"解决 GBK/UTF-16/带 BOM
文本乱码问题"，但**代码里从来没有 UTF-16 分支**（D5 第 1 条）。实测三种 UTF-16 落法
全部读出带 NUL 的乱码：

    '你好世界 hello'.encode('utf-16')     -> 'ÿþ`O}Y\\x16NLu \\x00h\\x00e\\x00l\\x00l\\x00o\\x00'
    '你好世界 hello'.encode('utf-16-le')  -> '`O}Y\\x16NLu \\x00h\\x00e\\x00l\\x00l\\x00o\\x00'
    '你好世界 hello'.encode('utf-16-be')  -> 'O`Y}N\\x16uL\\x00 \\x00h\\x00e\\x00l\\x00l\\x00o'

其中 **LE 无 BOM** 那次连 latin-1 兜底的警告都不触发 —— utf-8 解码器认为那些字节
全都合法（ASCII 字符后跟 \\x00 也是合法 UTF-8），于是"成功"返回一串带 NUL 的字符串，
**静默**打到 PDF 上（Windows 记事本"另存为 Unicode"、Excel 导出的 UTF-16 CSV 都会这样）。

覆盖：
  A. BOM 族（UTF-8/16/32 各字节序）—— 注意 UTF-32LE+BOM 的首两字节与 UTF-16LE+BOM
     完全相同（\\xff\\xfe），**必须按最长 BOM 优先**判，否则 UTF-32 会被 UTF-16 半路截胡
  B. 无 BOM 的 UTF-16 LE/BE（靠 NUL 下标奇偶判字节序）
  C. 既有编码不回归（UTF-8 / GBK / 纯 ASCII / 空文件 / 真 latin-1 兜底）+ 下游拆分
  D. 三个下游调用点全部经过同一函数（静态）
  E. 端到端：UTF-16 的 TXT 真打成 PDF，抽出来的必须是原文（带对照组，避免把
     "环境缺字体"误判成"修复没生效"）
  F. 对拍（替换式变异：删 UTF-32 项 / 颠倒 BOM 顺序 / 关掉 NUL 启发式，都必须报红）

用法: python tests/verify_text_encoding.py   （exit 0 = 全部通过）
"""
import csv
import io
import os
import re
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "local_print_tool"))

import converter  # noqa: E402

ok_all = True


def check(name, cond, detail=""):
    global ok_all
    if not cond:
        ok_all = False
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


TMP = tempfile.mkdtemp(prefix="hn_enc_verify_")

# 样例：中英数混排 + 逗号（CSV 场景）+ 换行（TXT/MD 场景）
_CN = "你好世界，页数 3\nsecond line, 42"
_ASCII = "HELLO WORLD 12345\nSECOND LINE abc"


def _write(name, raw):
    p = os.path.join(TMP, name)
    with open(p, "wb") as f:
        f.write(raw)
    return p


def _read(path):
    return converter._read_text_auto(path)


# ============================================================
# A. BOM 族
# ============================================================
print("\n=== A. BOM 族（UTF-8 / UTF-16 / UTF-32 各字节序）===")
BOM_CASES = [
    ("UTF-8 无 BOM", _CN.encode("utf-8"), _CN),
    ("UTF-8+BOM", b"\xef\xbb\xbf" + _CN.encode("utf-8"), _CN),
    ("UTF-16LE+BOM", _CN.encode("utf-16"), _CN),
    ("UTF-16BE+BOM", b"\xfe\xff" + _CN.encode("utf-16-be"), _CN),
    ("UTF-32LE+BOM", _CN.encode("utf-32"), _CN),
    ("UTF-32BE+BOM", b"\x00\x00\xfe\xff" + _CN.encode("utf-32-be"), _CN),
]
for _nm, _raw, _want in BOM_CASES:
    _p = _write("a_" + _nm.replace(" ", "_").replace("+", "_") + ".txt", _raw)
    try:
        _got = _read(_p)
        check("A %-14s -> 原文一致且无 NUL" % _nm, _got == _want and "\x00" not in _got,
              "got=%r" % (_got[:40],))
    except Exception as e:  # noqa: BLE001
        check("A %-14s -> 原文一致且无 NUL" % _nm, False, "抛异常 %r" % (e,))

# ============================================================
# B. 无 BOM 的 UTF-16（最容易静默出错的一支）
# ============================================================
print("\n=== B. 无 BOM 的 UTF-16 LE/BE ===")
NOBOM_CASES = [
    ("UTF-16LE 无 BOM", _CN.encode("utf-16-le"), _CN),
    ("UTF-16BE 无 BOM", _CN.encode("utf-16-be"), _CN),
]
for _nm, _raw, _want in NOBOM_CASES:
    _p = _write("b_" + _nm.replace(" ", "_") + ".txt", _raw)
    try:
        _got = _read(_p)
        check("B %-14s -> 原文一致且无 NUL" % _nm, _got == _want and "\x00" not in _got,
              "got=%r" % (_got[:40],))
    except Exception as e:  # noqa: BLE001
        check("B %-14s -> 原文一致且无 NUL" % _nm, False, "抛异常 %r" % (e,))

# ============================================================
# C. 既有编码不回归 + 下游拆分
# ============================================================
print("\n=== C. 既有编码不回归 ===")
LEGACY_CASES = [
    ("GBK", _CN.encode("gbk"), _CN),
    ("纯 ASCII", _ASCII.encode("ascii"), _ASCII),
    ("空文件", b"", ""),
    ("UTF-8 无 BOM（纯 ASCII 内容）", _ASCII.encode("utf-8"), _ASCII),
]
for _nm, _raw, _want in LEGACY_CASES:
    _p = _write("c_" + _nm.replace(" ", "_") + ".txt", _raw)
    try:
        _got = _read(_p)
        check("C %-26s -> 不变" % _nm, _got == _want, "got=%r" % (_got[:40],))
    except Exception as e:  # noqa: BLE001
        check("C %-26s -> 不变" % _nm, False, "抛异常 %r" % (e,))

# latin-1 兜底：UTF-8 与 GBK 都解不出的字节序列，必须**永不失败**
_lp = _write("c_latin1.bin", b"\xff\x00\xfe\xff")
try:
    _lg = _read(_lp)
    check("C latin-1 兜底永不失败（UTF-8/GBK 都解不出）", isinstance(_lg, str) and len(_lg) == 4,
          "got=%r" % (_lg,))
except Exception as e:  # noqa: BLE001
    check("C latin-1 兜底永不失败（UTF-8/GBK 都解不出）", False, "抛异常 %r" % (e,))

# 下游：CSV 拆分（converter.py:933 走的是同一函数的输出）
_cp = _write("c_rows.csv", "a,b\n1,2\n".encode("utf-16"))
try:
    _rows = list(csv.reader(_read(_cp).splitlines()))
    check("C UTF-16 的 CSV 能拆出正确行（含表头）", _rows == [["a", "b"], ["1", "2"]],
          repr(_rows))
except Exception as e:  # noqa: BLE001
    check("C UTF-16 的 CSV 能拆出正确行（含表头）", False, "抛异常 %r" % (e,))

# ============================================================
# D. 静态判据：三个下游调用点都经过同一函数
# ============================================================
print("\n=== D. 静态判据（5 条）===")
SRC = open(converter.__file__, encoding="utf-8").read()
_m = re.search(r"(?ms)^def _read_text_auto\(.*?(?=^def |\Z)", SRC)
BODY = _m.group(0) if _m else ""

_v = []
if not BODY:
    _v.append("NO_FN: 找不到 _read_text_auto")
else:
    if "_TEXT_BOMS" not in BODY:
        _v.append("BOM_PROBE: _read_text_auto 未走 BOM 表")
    if not re.search(r"[\"']\\x00[\"']\s+(?:not\s+)?in\s+", BODY):
        _v.append("NOBOM_HEURISTIC: 没有\"解出 NUL ⇒ 无 BOM 的 UTF-16\"启发式")
    if "utf-16" not in BODY:
        _v.append("NO_UTF16: 函数体里没有任何 utf-16 解码路径")
# 模块级 BOM 表：UTF-16/32 各两种字节序，共 4 项（少的那些就是"会被截胡"的那些）
_n_bom = len(re.findall(r'\(b"\\x(?:ff\\xfe|fe\\xff|00\\x00)[^"]*",\s*"(?:utf-16|utf-32)"\)', SRC))
if _n_bom < 4:
    _v.append("BOMS_DEF: BOM 表应有 4 项（UTF-16/32 × LE/BE），实际 %d" % _n_bom)
_n_call = len(re.findall(r"_read_text_auto\(file_path\)", SRC))
if _n_call != 3:
    _v.append("CONSUMERS: 调用点应为 3 处（TXT/CSV/MD），实际 %d" % _n_call)
check("D 静态判据全绿（BOM 表 / NUL 启发式 / utf-16 路径 / 3 个下游）",
      not _v, "；".join(_v))

# ============================================================
# E. 端到端：UTF-16 的 TXT -> PDF
# ============================================================
print("\n=== E. 端到端：UTF-16 的 TXT 打成 PDF ===")
try:
    import pypdf  # noqa: PLC0415
except Exception:  # noqa: BLE001
    pypdf = None

if pypdf is None:
    print("[SKIP] 未安装 pypdf，跳过 PDF 文本抽取")
else:
    _env_ok = False
    try:
        _ctl_in = _write("e_ctl.txt", _ASCII.encode("utf-8"))
        _ctl_pdf = os.path.join(TMP, "e_ctl.pdf")
        converter._convert_txt_to_pdf(_ctl_in, _ctl_pdf)
        _ctl_txt = "\n".join((pg.extract_text() or "") for pg in pypdf.PdfReader(_ctl_pdf).pages)
        _env_ok = "HELLO WORLD 12345" in _ctl_txt
        if not _env_ok:
            print("   对照组：PDF 生成了但抽不到原文（字体/抽取问题）-> 视为环境不可用")
    except Exception as e:  # noqa: BLE001
        print("   对照组抛异常：%r -> 视为环境不可用" % (e,))
    if not _env_ok:
        print("[SKIP] 本环境无法生成/抽取 TXT 的 PDF，跳过端到端节（不判红，避免环境假红）")
    else:
        for _nm, _enc in [("utf-16", "utf-16"), ("utf-16-le", "utf-16-le"), ("utf-16-be", "utf-16-be")]:
            _p = _write("e_%s.txt" % _nm, _ASCII.encode(_enc))
            _out = os.path.join(TMP, "e_%s.pdf" % _nm)
            try:
                converter._convert_txt_to_pdf(_p, _out)
                _txt = "\n".join((pg.extract_text() or "")
                                 for pg in pypdf.PdfReader(_out).pages)
                check("E %-10s -> PDF 抽出原文且无 NUL" % _nm,
                      "HELLO WORLD 12345" in _txt and "\x00" not in _txt, repr(_txt[:60]))
            except Exception as e:  # noqa: BLE001
                check("E %-10s -> PDF 抽出原文且无 NUL" % _nm, False, "抛异常 %r" % (e,))

# ============================================================
# F. 对拍（替换式变异 —— 改坏了必须报红）
# ============================================================
print("\n=== F. 对拍（替换式变异）===")


def _mut_fn(src):
    """把变异后的 converter 源码 exec 出来，取 _read_text_auto。
    顶层只有定义，exec 安全（不触发副作用）。"""
    ns = {}
    exec(compile(src, "<mutated converter>", "exec"), ns)  # noqa: S102
    return ns["_read_text_auto"]


_ALL_CASES = BOM_CASES + NOBOM_CASES + LEGACY_CASES


def _mut_check(name, mut_src, expect_fail):
    """跑变异体：expect_fail=True 时必须至少有一例读出**错误结果**（否则是假守卫）。"""
    if mut_src == SRC:
        check("F 对拍：%s" % name, False,
              "变异锚点不存在（修复还没做？）—— 如实判红，不做 assert")
        return
    try:
        fn = _mut_fn(mut_src)
    except Exception as e:  # noqa: BLE001
        check("F 对拍：%s -> 必须报红" % name, False, "变异体无法装载 %r" % (e,))
        return
    got = {}
    for nm, raw, want in _ALL_CASES:
        p = _write("f_" + nm.replace(" ", "_").replace("+", "_") + ".txt", raw)
        try:
            got[nm] = (fn(p) == want)
        except Exception:  # noqa: BLE001
            got[nm] = False
    all_ok = all(got.values())
    check("F 对拍：%s -> %s" % (name, "必须报红" if expect_fail else "必须仍绿"),
          all_ok != expect_fail,
          "各例通过情况=%s" % {k: v for k, v in got.items() if not v})


# F0 只改 docstring 的空白（无关改动）-> 必须仍绿。这条自证对拍工具不是"永远为红"
_f0 = SRC.replace('"""自动探测编码读取文本文件', '"""自动探测编码读取文本文件 ', 1)
_mut_check("只改 docstring 空白（无关改动必须仍绿）", _f0, False)

# F1 删掉 UTF-32 的两项 -> UTF-32 用例必须失败（被 UTF-16 半路截胡）
_f1 = re.sub(r'\n\s*\(b"\\xff\\xfe\\x00\\x00",\s*"utf-32"\),', "", SRC, count=1)
_f1 = re.sub(r'\n\s*\(b"\\x00\\x00\\xfe\\xff",\s*"utf-32"\),', "", _f1, count=1)
_mut_check("删掉 UTF-32 的 BOM 项", _f1, True)

# F2 颠倒顺序（UTF-16LE 排到 UTF-32LE 前面）-> UTF-32LE+BOM 必须失败
_l32 = re.search(r'(?m)^[ \t]*\(b"\\xff\\xfe\\x00\\x00",\s*"utf-32"\),[^\n]*\n', SRC)
_l16 = re.search(r'(?m)^[ \t]*\(b"\\xff\\xfe",\s*"utf-16"\),[^\n]*\n', SRC)
if _l32 and _l16:
    _f2 = SRC.replace(_l32.group(0), "", 1)
    _f2 = _f2.replace(_l16.group(0), _l16.group(0) + _l32.group(0), 1)
else:
    _f2 = SRC
_mut_check("把 UTF-16LE 的 BOM 项排到 UTF-32LE 前面", _f2, True)

# F3 关掉「原始字节里的 NUL 分布」启发式 -> UTF-16 无 BOM 用例必须失败
_f3 = SRC.replace('    if enc is None and b"\\x00" in raw:\n', "    if False:\n", 1)
_mut_check("关掉『原始字节里的 NUL 分布』启发式", _f3, True)

# ============================================================
import shutil  # noqa: E402

shutil.rmtree(TMP, ignore_errors=True)
print("\n文本编码探测验收: %s" % ("全部通过 ✅" if ok_all else "存在失败 ❌"))
sys.exit(0 if ok_all else 1)
