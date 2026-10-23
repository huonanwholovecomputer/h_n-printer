# -*- coding: utf-8 -*-
"""验收 · S1-04 Office COM 自动化安全（DS-09）

方案要求（docs/安全加固修复计划_2026-10-23.md §S1-04）：
  ① 打开文档**前**：`AutomationSecurity = 3`（msoAutomationSecurityForceDisable）、
     `DisplayAlerts = 0`、`Options.UpdateLinksAtOpen = False`（外加 `Visible = False`）；
     WPS 走同等设置，API 不同时至少禁用宏，并在注释里写明 WPS 实际做了什么；
  ② **云端来源的 Office 文档强制走 LibreOffice**（无宏执行），Word/WPS COM 只服务本地文件；
     来源参数默认为「本地」语义（不能悄悄把本地文件也赶去 LibreOffice）；
  ③ 断言 + 变异自证（删掉任一设置/任一闸门即红）。

⚠ 本脚本**绝不真的跑 COM**（会拉起 Word/WPS 弹窗、甚至真的执行宏）。所有涉及 COM 的
  调用都被替换成记录器，只验证"该调谁、不该调谁"。

用法: python tests/verify_office_automation_security.py     （exit 0 = 全绿）
"""
import io
import os
import re
import shutil
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL_TOOL = os.path.join(REPO, "local_print_tool")
sys.path.insert(0, LOCAL_TOOL)

ok_all = True


def check(name, cond, detail=""):
    global ok_all
    if not cond:
        ok_all = False
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


def section(title):
    print("\n" + "=" * 68)
    print(title)
    print("=" * 68)


_CONV_PATH = os.path.join(LOCAL_TOOL, "converter.py")
_CLOUD_PATH = os.path.join(LOCAL_TOOL, "cloud_client.py")
_GUI_PATH = os.path.join(LOCAL_TOOL, "gui.py")


def read(p):
    with open(p, encoding="utf-8") as f:
        return f.read()


CONV_SRC = read(_CONV_PATH)
CLOUD_SRC = read(_CLOUD_PATH)
GUI_SRC = read(_GUI_PATH)


def _blank_comment_lines(text):
    """`#` 之后置等长空格（保字符偏移）—— 判据不许吃注释（本仓验收的老教训）。"""
    out = []
    for line in text.split("\n"):
        i = line.find("#")
        out.append(line if i < 0 else line[:i] + " " * (len(line) - i))
    return "\n".join(out)


def _func_block(src, name):
    """取出某函数的**整段源码**（含多行签名；类方法也行）——到缩进不深于它的下一行为止。

    ⚠ 不能只匹配单行签名：`_convert_office_to_pdf(file_path: str, output_pdf: str,`
    的参数跨行，单行正则会把整个函数体切掉（本验证第一版就踩了）；
    `convert` 还是类方法（缩进 4 空格），所以按"缩进回退"判断函数结束。"""
    m = re.search(r"^(?P<ind>[ \t]*)def " + re.escape(name) + r"\(", src, re.M)
    if not m:
        return ""
    ind = len(m.group("ind"))
    # 从**下一行**开始扫（签名第一行的剩余部分仍在 def 那一行里，缩进为 0，会误判函数已结束）
    nl = src.find("\n", m.end())
    rest_start = nl + 1 if nl >= 0 else len(src)
    end = len(src)
    for mm in re.finditer(r"^([^\n]*)$", src[rest_start:], re.M):
        line = mm.group(1)
        if not line.strip():
            continue
        lead = len(line) - len(line.lstrip())
        if lead <= ind:
            end = rest_start + mm.start()
            break
    return src[m.start():end]


def _func_body(src, header_regex):
    """按函数头正则取出函数体（到下一个顶格 def / class 为止）。取不到返回 ""。"""
    m = re.search(header_regex, src, re.M)
    if not m:
        return ""
    start = m.end()
    nxt = re.search(r"^(?:def |class |# ={10,})", src[start:], re.M)
    return src[start:start + nxt.start()] if nxt else src[start:]


# ============================================================
# 1. 静态：三处（四项）COM 安全设置
# ============================================================

def check_harden_settings(src: str):
    """`_harden_office_com_app` 必须设置四项，且宏那一项用的是强制禁用常量。"""
    body = _blank_comment_lines(_func_body(src, r"^def _harden_office_com_app\(.*\)[^:]*:"))
    if not body:
        return False
    need = [
        r"app\.Visible\s*=\s*False",
        r"app\.DisplayAlerts\s*=\s*0",
        r"app\.AutomationSecurity\s*=\s*MSO_AUTOMATION_SECURITY_FORCE_DISABLE",
        r"app\.Options\.UpdateLinksAtOpen\s*=\s*False",
    ]
    return all(re.search(p, body) for p in need)


def check_force_disable_constant(src: str):
    code = _blank_comment_lines(src)
    m = re.search(r"^MSO_AUTOMATION_SECURITY_FORCE_DISABLE\s*=\s*(\d+)", code, re.M)
    return bool(m) and int(m.group(1)) == 3


def check_every_open_is_hardened(src: str):
    """每个 `Documents.Open` 所在函数都必须在 Open **之前**调用 `_harden_office_com_app`。

    这条是真正要命的判据：宏是在 Open 期间跑的，设置写晚了等于没写。
    """
    code = src
    opens = [m.start() for m in re.finditer(r"\.Documents\.Open\(", code)]
    if not opens:
        return False
    hardened_calls = [m.start() for m in re.finditer(r"_harden_office_com_app\(", code)]
    if not hardened_calls:
        return False
    for pos in opens:
        # 该 Open 之前最近的一次 harden 调用，必须落在同一个函数体内（中间没有新的 def）
        before = [h for h in hardened_calls if h < pos]
        if not before:
            return False
        gap = code[before[-1]:pos]
        if re.search(r"^(?:def |class )", gap, re.M):
            return False          # 中间跨了函数边界 ⇒ 这个 Open 没被保护
        if len(gap) > 4000:
            return False
    return True


def check_no_raw_open_without_harden(src: str):
    """不允许出现"裸设置 + Open"的旧写法（防有人把某处改回 `word.Visible = False`）。"""
    code = _blank_comment_lines(src)
    bad = 0
    for m in re.finditer(r"\.Documents\.Open\(", code):
        # 取 Open 前 15 行，若出现 .Visible = False 但这段里没有 _harden_office_com_app ⇒ 旧写法
        seg = code[max(0, m.start() - 900):m.start()]
        if re.search(r"\.(?:Visible|DisplayAlerts)\s*=", seg) and "_harden_office_com_app(" not in seg:
            bad += 1
    return bad == 0


def check_cloud_forced_to_libreoffice(src: str):
    """`_convert_office_to_pdf` 里 `from_cloud` 分支必须先走 LibreOffice、**不得**碰 COM。"""
    body = _func_block(src, "_convert_office_to_pdf")
    if not body:
        return False
    m = re.search(r"^    if from_cloud and ext in COM_OFFICE_EXTS:\n", body, re.M)
    if not m:
        return False
    # 该分支到下一个**顶格 4 空格**的分支/循环为止（分支内部一律 8 空格起）
    tail = body[m.end():]
    nxt = re.search(r"^    (?:if |for |elif |else:|return |# ──|\w)", tail, re.M)
    branch = tail[:nxt.start()] if nxt else tail
    return ("_convert_office_via_libreoffice(" in branch
            and "_convert_via_word_com(" not in branch
            and "_convert_via_wps_com(" not in branch)


def check_default_is_local(src: str):
    """来源参数的默认值必须是「本地」——否则本地文件也被赶去 LibreOffice（回归）。"""
    conv = _func_block(src, "convert")          # 类方法：先抓 `def convert(`，再取签名
    office = _func_block(src, "_convert_office_to_pdf")

    def _has_local_default(block):
        return bool(re.search(r"from_cloud\s*:\s*bool\s*=\s*False", block))

    return _has_local_default(conv) and _has_local_default(office)


def check_convert_passes_source_through(src: str):
    """`convert()` 必须把 from_cloud 真的传给 Office 转换函数（不是只写在签名里）。"""
    body = _blank_comment_lines(_func_block(src, "convert"))
    return bool(re.search(r"convert_func\(file_path,\s*output_pdf,\s*from_cloud=from_cloud\)", body))


def check_wps_note(src: str):
    """WPS 实际做了什么必须写在注释里（API 不同不能含糊过去）。"""
    head = src[:src.find("def _convert_office_to_pdf(")]
    seg = head[head.find("def _harden_office_com_app"):] if "def _harden_office_com_app" in head else ""
    doc = seg[:seg.find('"""', seg.find('"""') + 3)] if seg.count('"""') >= 2 else ""
    return ("WPS" in doc
            and ("AutomationSecurity" in doc)
            and ("宏安全" in doc or "安全级别" in doc))


STATIC_CONV = [
    ("`_harden_office_com_app` 设置 Visible/DisplayAlerts/AutomationSecurity/UpdateLinksAtOpen 四项",
     check_harden_settings),
    ("msoAutomationSecurityForceDisable 常量 = 3", check_force_disable_constant),
    ("★ 每个 Documents.Open 之前都调用了 _harden_office_com_app（宏在 Open 期间跑）",
     check_every_open_is_hardened),
    ("已无\"裸 .Visible= 直接开文档\"的旧写法", check_no_raw_open_without_harden),
    ("★ 云端来源分支强制 LibreOffice、不碰 Word/WPS COM", check_cloud_forced_to_libreoffice),
    ("★ from_cloud 默认值 = False（本地文件语义不变）", check_default_is_local),
    ("convert() 把 from_cloud 传给 Office 转换函数", check_convert_passes_source_through),
    ("注释写明 WPS 侧实际做了什么（AutomationSecurity / 宏安全级别）", check_wps_note),
]


def run_converter_static(src: str) -> list:
    return [(name, bool(fn(src))) for name, fn in STATIC_CONV]


section("1. converter.py 静态判据（COM 安全设置 + 云端强制 LibreOffice）")
results = run_converter_static(CONV_SRC)
for name, ok in results:
    check(name, ok)

# WPS 路径同样有 harden 调用（不是只给 Word 加）
check("WPS COM 路径也调用了 _harden_office_com_app",
      len(re.findall(r"_harden_office_com_app\(", CONV_SRC)) >= 2
      and bool(re.search(r'_harden_office_com_app\(wps', CONV_SRC)))

# ============================================================
# 2. 静态：云端调用点都传了 from_cloud（顺着调用链）
# ============================================================
section("2. 云端调用链：三个入口都把「云端来源」传下去")

check("cloud_client.py 页数分析：converter.convert(..., from_cloud=True)",
      bool(re.search(r"converter\.convert\(temp_dl,\s*from_cloud=True\)", CLOUD_SRC)))

check("gui.py ConvertWorker 接收 from_cloud 并在云端时不走 COM",
      bool(re.search(r"def __init__\(self,\s*row:\s*int,\s*file_path:\s*str,\s*engine:\s*str,"
                     r"\s*source_md5:\s*str\s*=\s*\"\",\s*\n\s*from_cloud:\s*bool\s*=\s*False\)",
                     GUI_SRC))
      and "self._from_cloud" in GUI_SRC
      and bool(re.search(r"and not self\._from_cloud", GUI_SRC)))

check("gui.py 云端任务预转换：_start_convert_worker(..., from_cloud=True)",
      bool(re.search(r"_start_convert_worker\(row,\s*task\.local_path,\s*engine,\s*tab_key=new_key,\s*\n\s*from_cloud=True\)",
                     GUI_SRC)))

check("gui.py 打印兜底转换：按 job.task_id 判定云端来源后传 from_cloud",
      bool(re.search(r'_from_cloud\s*=\s*int\(getattr\(job,\s*"task_id",\s*0\)\s*or\s*0\)\s*>\s*0', GUI_SRC))
      and bool(re.search(r"from_cloud=_from_cloud", GUI_SRC)))

check("gui.py 本地添加文件路径**不传** from_cloud（保持本地语义）",
      bool(re.search(r"self\._start_convert_worker\(i,\s*job\.file_path,\s*job\.engine\)\s*$",
                     GUI_SRC, re.M)))


# ============================================================
# 3. 行为：harden 真的把四项写进 COM 对象（用假对象，不跑 COM）
# ============================================================
section("3. 行为：harden 对 COM 对象逐项赋值（假对象；绝不启动真的 Word/WPS）")

import converter as C  # noqa: E402

assert "win32com" not in sys.modules, "本脚本不应导入 win32com"


class _FakeWordApp:
    """模拟 Word 的 COM 对象：属性可写、Options 是子对象。"""
    def __init__(self):
        self.Visible = True
        self.DisplayAlerts = 99
        self.AutomationSecurity = None
        self.Options = type("Opts", (), {"UpdateLinksAtOpen": True})()


class _OldWpsApp:
    """模拟老版本 WPS：COM 对象**没有** AutomationSecurity / Options 这些成员。

    ⚠ 用 `__slots__` 才能复现 COM 的真实行为：给不存在的属性赋值会抛异常
    （普通 Python 对象随便加属性，测不出"属性缺失时是否如实记录未生效"）。"""
    __slots__ = ("Visible", "DisplayAlerts")

    def __init__(self):
        self.Visible = True
        self.DisplayAlerts = 99


_fa = _FakeWordApp()
applied = C._harden_office_com_app(_fa, "Word(验收)")
check("Word：四项设置全部生效（返回值全 True）", all(applied.values()), str(applied))
check("Word：Visible=False", _fa.Visible is False)
check("Word：DisplayAlerts=0", _fa.DisplayAlerts == 0)
check("★ Word：AutomationSecurity=3（msoAutomationSecurityForceDisable）",
      _fa.AutomationSecurity == 3, repr(_fa.AutomationSecurity))
check("Word：Options.UpdateLinksAtOpen=False", _fa.Options.UpdateLinksAtOpen is False)

_w = _OldWpsApp()
applied_w = C._harden_office_com_app(_w, "WPS(老版本验收)")
check("WPS 老版本：属性缺失不抛异常（逐项 try/except）", isinstance(applied_w, dict))
check("WPS 老版本：Visible/DisplayAlerts 仍生效",
      _w.Visible is False and _w.DisplayAlerts == 0)
check("WPS 老版本：缺的属性如实记为未生效（不谎报成功）",
      applied_w.get("AutomationSecurity=3") is False
      and applied_w.get("Options.UpdateLinksAtOpen=False") is False, str(applied_w))

# ============================================================
# 4. 行为：云端强制 LibreOffice / 本地默认不变（全部桩替换，无真 COM）
# ============================================================
section("4. 行为：云端→LibreOffice，本地→COM（桩替换引擎，绝不跑真 COM）")

TMP = tempfile.mkdtemp(prefix="hn_s104_")
_SRC = os.path.join(TMP, "cloud.docx")
with open(_SRC, "wb") as f:
    f.write(b"PK\x03\x04fake docx (never opened by a real Office)")
_OUT = os.path.join(TMP, "out.pdf")

calls: list[str] = []
_orig = {
    "_detect_word": C._detect_word,
    "_detect_wps": C._detect_wps,
    "_convert_via_word_com": C._convert_via_word_com,
    "_convert_via_wps_com": C._convert_via_wps_com,
    "_convert_office_via_libreoffice": C._convert_office_via_libreoffice,
    "_find_libreoffice": C._find_libreoffice,
    "_pdf_product_ok": C._pdf_product_ok,
    "_read_docx_last_editor": C._read_docx_last_editor,
    "_discard_invalid_pdf": C._discard_invalid_pdf,
}


def _stub_com(name):
    def _f(file_path, output_pdf, *a, **kw):
        calls.append(name)
        with open(output_pdf, "wb") as fh:
            fh.write(b"%PDF-1.4 stub")
    return _f


def _stub_lo(file_path, output_pdf):
    calls.append("libreoffice")
    with open(output_pdf, "wb") as fh:
        fh.write(b"%PDF-1.4 stub")


try:
    C._detect_word = lambda: True
    C._detect_wps = lambda: True
    C._convert_via_word_com = _stub_com("word")
    C._convert_via_wps_com = _stub_com("wps")
    C._convert_office_via_libreoffice = _stub_lo
    C._find_libreoffice = lambda: "soffice-stub"
    C._pdf_product_ok = lambda p: True
    C._read_docx_last_editor = lambda p: None
    C._discard_invalid_pdf = lambda p: None

    # 4a. 显式 from_cloud=True
    calls.clear()
    C._convert_office_to_pdf(_SRC, _OUT, from_cloud=True)
    check("★ 云端来源：只调用 LibreOffice", calls == ["libreoffice"], str(calls))
    check("★ 云端来源：Word/WPS COM 一次都没被调用",
          "word" not in calls and "wps" not in calls, str(calls))

    # 4b. 默认（不传）= 本地语义 → 仍走 COM 降级链
    calls.clear()
    C._convert_office_to_pdf(_SRC, _OUT)
    check("★ 默认参数=本地：仍走 Word COM（默认语义没变）", calls == ["word"], str(calls))

    # 4c. 走公开入口 convert()：两条路径分别传递到正确的引擎
    conv = C.UniversalConverter()
    calls.clear()
    conv.convert(_SRC, _OUT, from_cloud=True)
    check("★ convert(..., from_cloud=True) → LibreOffice", calls == ["libreoffice"], str(calls))
    calls.clear()
    conv.convert(_SRC, _OUT)
    check("★ convert(...) 默认 → COM（本地文件零回归）", calls == ["word"], str(calls))

    # 4d. 云端 + 没装 LibreOffice：明确报错，绝不退回 COM
    calls.clear()
    C._find_libreoffice = lambda: None
    err = ""
    try:
        C._convert_office_to_pdf(_SRC, _OUT, from_cloud=True)
    except RuntimeError as e:
        err = str(e)
    check("★ 云端 + 无 LibreOffice：抛错而不是偷偷用 Word", bool(err) and "LibreOffice" in err, err[:80])
    check("★ 云端 + 无 LibreOffice：COM 依然一次都没调用", calls == [], str(calls))

    # 4e. 非 Word 的 Office 格式（xls/ppt）云端/本地都走 LibreOffice（既有行为）
    calls.clear()
    C._find_libreoffice = lambda: "soffice-stub"
    xls = os.path.join(TMP, "t.xlsx")
    open(xls, "wb").close()
    C._convert_office_to_pdf(xls, _OUT, from_cloud=True)
    check("云端 xlsx 仍走 LibreOffice", calls == ["libreoffice"], str(calls))
finally:
    C._detect_word = _orig["_detect_word"]
    C._detect_wps = _orig["_detect_wps"]
    C._convert_via_word_com = _orig["_convert_via_word_com"]
    C._convert_via_wps_com = _orig["_convert_via_wps_com"]
    C._convert_office_via_libreoffice = _orig["_convert_office_via_libreoffice"]
    C._find_libreoffice = _orig["_find_libreoffice"]
    C._pdf_product_ok = _orig["_pdf_product_ok"]
    C._read_docx_last_editor = _orig["_read_docx_last_editor"]
    C._discard_invalid_pdf = _orig["_discard_invalid_pdf"]
    shutil.rmtree(TMP, ignore_errors=True)

check("全程没有导入/使用 win32com（没有拉起真的 Office）", "win32com" not in sys.modules)


# ============================================================
# 5. 变异自证：删掉任一设置 / 任一闸门 → 对应判据必须变红
# ============================================================
section("5. 变异自证（改一处 → 对应判据必须变红；其余保持绿）")

mutations = []

# 5a. 删 AutomationSecurity 一行
_mut = re.sub(r"^.*app\.AutomationSecurity\s*=.*$", "", CONV_SRC, flags=re.M)
mutations.append(("删掉 AutomationSecurity=3",
                  _mut, "`_harden_office_com_app` 设置 Visible/DisplayAlerts/AutomationSecurity/UpdateLinksAtOpen 四项"))

# 5b. 删 UpdateLinksAtOpen 一行
_mut = re.sub(r"^.*app\.Options\.UpdateLinksAtOpen\s*=.*$", "", CONV_SRC, flags=re.M)
mutations.append(("删掉 Options.UpdateLinksAtOpen=False",
                  _mut, "`_harden_office_com_app` 设置 Visible/DisplayAlerts/AutomationSecurity/UpdateLinksAtOpen 四项"))

# 5c. 删 DisplayAlerts 一行
_mut = re.sub(r"^.*app\.DisplayAlerts\s*=\s*0.*$", "", CONV_SRC, flags=re.M)
mutations.append(("删掉 DisplayAlerts=0",
                  _mut, "`_harden_office_com_app` 设置 Visible/DisplayAlerts/AutomationSecurity/UpdateLinksAtOpen 四项"))

# 5d. 常量改成 1（msoAutomationSecurityLow = 允许宏）
_mut = CONV_SRC.replace("MSO_AUTOMATION_SECURITY_FORCE_DISABLE = 3",
                        "MSO_AUTOMATION_SECURITY_FORCE_DISABLE = 1")
mutations.append(("把强制禁用宏改成 1（允许宏）",
                  _mut, "msoAutomationSecurityForceDisable 常量 = 3"))

# 5e. 云端分支整段删掉（云端文件又回到 COM）
_mut = re.sub(r"    if from_cloud and ext in COM_OFFICE_EXTS:\n(?:        .*\n|\n)*?        return\n",
              "", CONV_SRC, count=1)
mutations.append(("删掉\"云端来源强制 LibreOffice\"分支",
                  _mut, "★ 云端来源分支强制 LibreOffice、不碰 Word/WPS COM"))

# 5f. 默认值改成 True（把本地文件也赶去 LibreOffice）
_mut = CONV_SRC.replace("from_cloud: bool = False", "from_cloud: bool = True")
mutations.append(("把 from_cloud 默认值改成 True（本地文件也被赶走）",
                  _mut, "★ from_cloud 默认值 = False（本地文件语义不变）"))

# 5g. 某个 Open 之前的 harden 调用删掉（WPS 转换路径）
_mut = CONV_SRC.replace(
    '        wps = win32com.client.dynamic.Dispatch(prog_id)\n'
    '        # S1-04：WPS 打开前同样收紧（宏/弹窗/外链）；属性缺失时日志记录实际生效项\n'
    '        _harden_office_com_app(wps, "WPS")\n\n'
    '        doc = wps.Documents.Open(os.path.abspath(temp_input), ReadOnly=True)',
    '        wps = win32com.client.dynamic.Dispatch(prog_id)\n\n'
    '        doc = wps.Documents.Open(os.path.abspath(temp_input), ReadOnly=True)')
mutations.append(("删掉 WPS 转换路径的 harden 调用",
                  _mut, "★ 每个 Documents.Open 之前都调用了 _harden_office_com_app（宏在 Open 期间跑）"))

for label, mutated_src, target_name in mutations:
    changed = mutated_src != CONV_SRC
    res = dict(run_converter_static(mutated_src))
    target_red = res.get(target_name) is False
    others_red = [n for n, ok in res.items() if not ok and n != target_name]
    check(f"变异「{label}」→ 判据「{target_name}」变红"
          + (f"（连带 {len(others_red)} 条也红，允许）" if others_red else ""),
          changed and target_red,
          f"changed={changed}, target_red={target_red}")

# 判据自证：基线（未变异）必须全绿，否则变异"变红"没有意义
check("基线：八条静态判据全绿（变异自证的前提）", all(ok for _n, ok in results),
      str([n for n, ok in results if not ok]))

# 5h. 云端调用点去掉 from_cloud → 接线判据必须红
_mut_cloud = CLOUD_SRC.replace("converter.convert(temp_dl, from_cloud=True)",
                              "converter.convert(temp_dl)")
check("变异「云端页数分析不传 from_cloud」→ 接线判据变红",
      not re.search(r"converter\.convert\(temp_dl,\s*from_cloud=True\)", _mut_cloud)
      and bool(re.search(r"converter\.convert\(temp_dl,\s*from_cloud=True\)", CLOUD_SRC)))

_mut_gui = GUI_SRC.replace(
    "self._start_convert_worker(row, task.local_path, engine, tab_key=new_key,\n"
    "                                       from_cloud=True)",
    "self._start_convert_worker(row, task.local_path, engine, tab_key=new_key)")
check("变异「云端任务预转换不传 from_cloud」→ 接线判据变红",
      not re.search(r"_start_convert_worker\(row,\s*task\.local_path,\s*engine,"
                    r"\s*tab_key=new_key,\s*\n\s*from_cloud=True\)", _mut_gui))

print("\n" + "=" * 68)
print("S1-04 Office COM 自动化安全验收:", "全部通过 ✅" if ok_all else "存在失败 ❌")
sys.exit(0 if ok_all else 1)
