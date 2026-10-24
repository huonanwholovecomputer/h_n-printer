# -*- coding: utf-8 -*-
"""验收 · 引擎探测绝不碰 COM + 一律私有实例（DispatchEx）

事故（2026-10-24 现场）：工具没运行，但 `tests/run_all.py` 里构造 MainWindow 的测试
触发了 `gui._refresh_engine_availability()` → `converter.get_available_engines()` →
`_detect_word()` 的 `Dispatch("Word.Application")` + `Quit()`。Word 是单实例 COM 服务器，
Dispatch **附着**到用户正开着的 Word 上，Quit 直接把用户的文档关了（界面上看不到任何东西）。

修复后的契约（本脚本钉住）：
  A. 探测只查 exe 存在性，**函数体里没有 Dispatch / win32com**；COM 只在真正转换时用；
  B. 项目里**不存在** `win32com.client.dynamic.Dispatch(`（全部改为 `_dispatch_ex` →
     `DispatchEx` 私有实例），预热也一样；预热默认关闭（HN_WARM_OFFICE=1 才启用）；
  C. GUI 启动不再 QTimer 探测（按需在重建下拉时刷新）；main.py 不再定时刷新；
  D. `HN_NO_ENGINE_PROBE=1` 是硬闸门：探测直接判不可用，连 exe 都不查；run_all 统一注入；
  E. 行为（假 win32com 模块）：探测零 COM 调用；`_safe_word_dispatch` 走 DispatchEx；
  F. 变异自证：把 `_dispatch_ex` 改回 `Dispatch` ⇒ 假模块记录到 Dispatch（判据能红）；
     把探测里的环境闸门删掉 ⇒ HN_NO_ENGINE_PROBE 不再短路（判据能红）。

用法：python tests/verify_engine_probe_no_com.py     （exit 0 = 全部通过）
"""
import io
import os
import subprocess
import sys
import tempfile
import types

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL_TOOL = os.path.join(REPO, "local_print_tool")
sys.path.insert(0, LOCAL_TOOL)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _sec_harness as H  # noqa: E402

H.install_cleanup_hook()


def _read(name):
    with open(os.path.join(LOCAL_TOOL, name), "r", encoding="utf-8", newline="") as f:
        return f.read()


def _blank_comment_lines(text):
    out = []
    for line in text.split("\n"):
        i = line.find("#")
        out.append(line if i < 0 else line[:i] + " " * (len(line) - i))
    return "\n".join(out)


def _strip_docstrings(text):
    """去掉三引号字符串（文档字符串会描述"旧实现用 Dispatch"这类历史原因，不能当判据）。"""
    import re
    return re.sub(r'"""[\s\S]*?"""|\'\'\'[\s\S]*?\'\'\'', '""', text, flags=re.S)


def _func_body(src, name):
    m = None
    for cand in ("def %s(" % name, "    def %s(" % name):
        m = src.find(cand)
        if m >= 0:
            break
    if m < 0:
        return ""
    ind = len(src[:m]) - len(src[:m].rstrip(" "))
    end = len(src)
    pos = m
    while True:
        nl = src.find("\n", pos)
        if nl < 0:
            break
        pos = nl + 1
        line = src[pos:src.find("\n", pos) if src.find("\n", pos) >= 0 else len(src)]
        if line.strip() and not line.startswith(" " * (ind + 1)):
            end = pos
            break
    return src[m:end]


# ============================================================
# A. 静态：探测不碰 COM / 私有实例 / 懒加载 / 环境闸门
# ============================================================
print("=== A. 静态接线（探测不碰 COM / DispatchEx / 懒加载）===")
conv_src = _read("converter.py")
gui_src = _read("gui.py")
main_src = _read("main.py")
runall_src = _read("../tests/run_all.py")
conv_clean = _blank_comment_lines(conv_src)

for fn in ("_detect_word", "_detect_wps"):
    body = _strip_docstrings(_blank_comment_lines(_func_body(conv_src, fn)))
    H.chk("★ %s 函数体里没有 Dispatch / win32com（探测绝不拉起/附着 Office）" % fn,
          bool(body) and "Dispatch" not in body and "win32com" not in body,
          body[:120])

H.chk("★ 项目里不存在 win32com.client.dynamic.Dispatch(（全部 DispatchEx 私有实例）",
      "win32com.client.dynamic.Dispatch(" not in conv_clean)
H.chk("_dispatch_ex 用 DispatchEx，并注明为什么不能用 Dispatch",
      "win32com.client.DispatchEx(prog_id)" in conv_clean
      and "DispatchEx" in conv_clean and "附着" in conv_src)
H.chk("Word/WPS 预热也走 _dispatch_ex，且默认关闭（HN_WARM_OFFICE=1 才启用）",
      "_dispatch_ex(\"Word.Application\")" in conv_src
      and "_dispatch_ex(pid)" in conv_src
      and conv_src.count('HN_WARM_OFFICE') >= 2)
H.chk("HN_NO_ENGINE_PROBE 硬闸门存在，且两个探测函数都先判它",
      "def _engine_probe_disabled(" in conv_src
      and _blank_comment_lines(_func_body(conv_src, "_detect_word")).find("_engine_probe_disabled()") >= 0
      and _blank_comment_lines(_func_body(conv_src, "_detect_wps")).find("_engine_probe_disabled()") >= 0)
H.chk("★ GUI 启动不再 QTimer 探测引擎", "QTimer.singleShot(0, self._refresh_engine_availability)" not in gui_src)
H.chk("★ main.py 不再定时刷新引擎可用性（QTimer 500）",
      "QTimer.singleShot(500, window._refresh_engine_availability)" not in main_src)
H.chk("★ run_all 给每个验收脚本注入 HN_NO_ENGINE_PROBE=1（第二道闸门）",
      'HN_NO_ENGINE_PROBE' in runall_src and 'env.setdefault("HN_NO_ENGINE_PROBE", "1")' in runall_src)

# ============================================================
# B. 行为：假 win32com，探测零 COM 调用；dispatch 走 DispatchEx
# ============================================================
print("\n=== B. 行为（假 win32com；不启动任何 Office）===")
import converter as C  # noqa: E402

_counts = {"dispatch": 0, "dispatchex": 0}
_orig_modules = {k: sys.modules.get(k) for k in ("win32com", "win32com.client", "pythoncom")}


class _FakeApp:
    Visible = True

    def Quit(self):
        pass


_fake_client = types.ModuleType("win32com.client")
_fake_client.Dispatch = lambda prog_id: (_counts.__setitem__("dispatch", _counts["dispatch"] + 1) or _FakeApp())
_fake_client.DispatchEx = lambda prog_id: (_counts.__setitem__("dispatchex", _counts["dispatchex"] + 1) or _FakeApp())
_fake_win32 = types.ModuleType("win32com")
_fake_win32.client = _fake_client
sys.modules["win32com"] = _fake_win32
sys.modules["win32com.client"] = _fake_client
sys.modules["pythoncom"] = types.SimpleNamespace(CoInitialize=lambda: None,
                                                 CoUninitialize=lambda: None)

_orig = {
    "isfile": os.path.isfile,
    "which": C.shutil.which,
    "cache": dict(C._engine_cache),
    "progid": C._wps_progid,
}


def _fake_isfile(p):
    s = str(p).lower()
    if "winword.exe" in s or s.endswith("wps.exe"):
        return True
    return _orig["isfile"](p)


try:
    os.path.isfile = _fake_isfile
    C.shutil.which = lambda name: "C:/fake/WINWORD.EXE" if "WINWORD" in str(name).upper() else None
    C._engine_cache = {"word": None, "wps": None}
    C._wps_progid = None
    os.environ.pop("HN_NO_ENGINE_PROBE", None)

    w_ok = C._detect_word()
    p_ok = C._detect_wps()
    H.chk("★ 探测判定可用时零 COM 调用（Dispatch=0 / DispatchEx=0）",
          w_ok and p_ok and _counts["dispatch"] == 0 and _counts["dispatchex"] == 0,
          _counts)

    app = C._safe_word_dispatch()
    H.chk("★ _safe_word_dispatch 用 DispatchEx 私有实例（Dispatch 仍为 0）",
          app is not None and _counts["dispatchex"] == 1 and _counts["dispatch"] == 0, _counts)

    # 环境闸门：连 exe 都不查
    _probe_calls = {"n": 0}

    def _counting_isfile(p):
        _probe_calls["n"] += 1
        return False

    os.environ["HN_NO_ENGINE_PROBE"] = "1"
    C._engine_cache = {"word": None, "wps": None}
    os.path.isfile = _counting_isfile
    H.chk("★ HN_NO_ENGINE_PROBE=1 ⇒ 判不可用且不扫 exe（探测次数 0）",
          (not C._detect_word()) and (not C._detect_wps()) and _probe_calls["n"] == 0,
          _probe_calls)
finally:
    os.path.isfile = _orig["isfile"]
    C.shutil.which = _orig["which"]
    C._engine_cache = _orig["cache"]
    C._wps_progid = _orig["progid"]
    for k, v in _orig_modules.items():
        if v is None:
            sys.modules.pop(k, None)
        else:
            sys.modules[k] = v

# ============================================================
# C. 变异自证
# ============================================================
print("\n=== C. 变异自证（Dispatch 回潮 / 环境闸门失效）=== ")
_TMPS: list[str] = []


def _mutant(replacements, prefix="hn_probe_mut_"):
    d = tempfile.mkdtemp(prefix=prefix)
    _TMPS.append(d)
    src = _read("converter.py")
    for old, new in replacements:
        if old not in src:
            raise AssertionError("变异锚点未命中: %r" % old[:80])
        src = src.replace(old, new, 1)
    with open(os.path.join(d, "converter.py"), "w", encoding="utf-8") as f:
        f.write(src)
    return d


def _run_probe(module_dir, body):
    probe = os.path.join(module_dir, "_probe.py")
    with open(probe, "w", encoding="utf-8") as f:
        f.write(
            "# -*- coding: utf-8 -*-\n"
            "import io, os, sys, types\n"
            "sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')\n"
            "counts = {'dispatch': 0}\n"
            "fc = types.ModuleType('win32com.client')\n"
            "fc.Dispatch = lambda pid: (counts.__setitem__('dispatch', counts['dispatch']+1) or object())\n"
            "fc.DispatchEx = lambda pid: object()\n"
            "fw = types.ModuleType('win32com'); fw.client = fc\n"
            "sys.modules['win32com'] = fw; sys.modules['win32com.client'] = fc\n"
            "sys.path.insert(0, %r)\n"
            "sys.path.insert(0, %r)\n"
            "import converter as C\n" % (LOCAL_TOOL, module_dir) + body)
    env = dict(os.environ)
    env["PYTHONPATH"] = ""
    env["PYTHONUTF8"] = "1"
    return subprocess.run([sys.executable, probe], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=120, env=env)


# 变异 1：_dispatch_ex 改回 Dispatch（会附着用户实例）
_m1 = _mutant([("    return win32com.client.DispatchEx(prog_id)",
                "    return win32com.client.Dispatch(prog_id)  # 变异：附着用户实例")])
_p1 = _run_probe(_m1, """
C._dispatch_ex('Word.Application')
print('DISPATCH=%d' % counts['dispatch'])
""")
H.chk("变异回 Dispatch 后，假模块记录到 Dispatch（B 的判据能红）",
      H.probe_value(_p1, "DISPATCH") == "1",
      "probe=%s err=%s" % (H.probe_value(_p1, "DISPATCH"), (_p1.stderr or "")[-160:]))

# 变异 2：探测里的环境闸门删掉（HN_NO_ENGINE_PROBE 不再短路）
_m2 = _mutant([("    if _engine_probe_disabled():\n"
                "        logger.info(\"HN_NO_ENGINE_PROBE=1：跳过 Office 引擎探测（测试/CI 强制）\")\n"
                "        _engine_cache[\"word\"] = False\n"
                "        return False\n",
                "    # 变异：删除环境闸门\n"
                "    if False:\n"
                "        pass\n")])
_p2 = _run_probe(_m2, """
os.environ['HN_NO_ENGINE_PROBE'] = '1'
calls = {'n': 0}
real = os.path.isfile
def cnt(p):
    calls['n'] += 1
    return real(p)
os.path.isfile = cnt
C._engine_cache = {'word': None}
C._detect_word()
print('PROBES=%d' % calls['n'])
""")
H.chk("删掉环境闸门后，HN_NO_ENGINE_PROBE 不再短路（仍会扫 exe ⇒ 判据能红）",
      int(H.probe_value(_p2, "PROBES") or 0) > 0,
      "probe=%s err=%s" % (H.probe_value(_p2, "PROBES"), (_p2.stderr or "")[-160:]))

for d in _TMPS:
    import shutil
    shutil.rmtree(d, ignore_errors=True)

H.finish()
