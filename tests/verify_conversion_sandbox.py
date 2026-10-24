# -*- coding: utf-8 -*-
"""验收 · S2-03 客户端半：Office 转换沙箱（默认关闭）+ 外部引用策略

依据：docs/安全加固修复计划_2026-10-23.md §S2-03（DS-16、DS-01 第二层）。

现场形状：
  · Word/WPS COM 转换在本进程里跑：文档卡死 = 整个打印工具卡死；拉起的 Office 进程
    没有任何生命周期约束（工具退出后可能残留）；
  · Markdown 渲染出的 HTML 里的远程图片/CSS/link/meta-refresh 仍会被 wkhtmltopdf 抓取
    （店铺机可被顾客文档当跳板向内网/外网发请求，也容易拖到 90s 超时）。

本脚本：
  A. 静态接线：office_sandbox（Job Object / kill-on-close / 内存上限 / 超时）、
     convert_worker 的 GO 握手与 RESULT 协议、main.py 在 PySide6 之前分发 worker、
     converter 只对**本地 .doc/.docx** 走沙箱且默认关闭、不可用才回退、
     printer_config 的 conversion_sandbox 默认 False、GUI 启动时应用该开关、
     外部引用剥离函数与调用点。
  B. 行为：should_sandbox 矩阵；远端引用剥离（含 DOM 边界）；**假 wkhtmltopdf** 验证
     传给转换器的 HTML 已无远程引用；真实 Job Object 跑一次 txt→PDF；超时把
     worker + 它拉起的孙进程一起杀掉；worker 拒绝在没有 GO 时执行转换。
  C. 变异自证：删掉"剥外部引用"调用 ⇒ 远程 URL 进到转换输入；should_sandbox 改成
     恒 False ⇒ 本地 docx 不再进沙箱（判据能红）。

用法：python tests/verify_conversion_sandbox.py     （exit 0 = 全部通过）
"""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL_TOOL = os.path.join(REPO, "local_print_tool")
sys.path.insert(0, LOCAL_TOOL)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _sec_harness as H  # noqa: E402

H.install_cleanup_hook()

_TMPS: list[str] = []


def _mkroot(prefix="hn_sbx_"):
    d = tempfile.mkdtemp(prefix=prefix)
    _TMPS.append(d)
    return d


def _read(name):
    with open(os.path.join(LOCAL_TOOL, name), "r", encoding="utf-8", newline="") as f:
        return f.read()


# ============================================================
# A. 静态接线
# ============================================================
print("=== A. 静态接线（沙箱 / worker / 开关 / 外部引用）===")
sbx_src = _read("office_sandbox.py")
wrk_src = _read("convert_worker.py")
main_src = _read("main.py")
conv_src = _read("converter.py")
pc_src = _read("printer_config.py")
gui_src = _read("gui.py")

H.chk("office_sandbox.py 存在且用 Job Object（kill-on-close + 内存上限 + 超时）",
      os.path.isfile(os.path.join(LOCAL_TOOL, "office_sandbox.py"))
      and "_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE" in sbx_src
      and "_JOB_OBJECT_LIMIT_JOB_MEMORY" in sbx_src
      and "TerminateJobObject" in sbx_src
      and "subprocess.TimeoutExpired" in sbx_src)
H.chk("★ 先入 Job 再放行（worker 在 stdin 上等 GO，父进程 Assign 后才写 GO）",
      "AssignProcessToJobObject" in sbx_src
      and 'input=b"GO\\n"' in sbx_src
      and "readline" in wrk_src and 'line != "GO"' in wrk_src)
H.chk("worker 结果走 RESULT:{json}（成败 + 原因 + 产物路径）",
      '"RESULT:"' in wrk_src and "ensure_ascii=False" in wrk_src
      and "os.path.getsize(out) > 0" in wrk_src)
H.chk("★ main.py 静态 import convert_worker 并在导入 PySide6 之前分发 --convert-worker",
      "import convert_worker" in main_src
      and main_src.find("--convert-worker") < main_src.find("from PySide6.QtWidgets import"))
H.chk("converter 沙箱开关默认关闭（默认值必须是 False）",
      "_office_sandbox_enabled = False" in conv_src
      and "def set_office_sandbox(" in conv_src
      and "def office_sandbox_enabled(" in conv_src)
H.chk("★ 只对**本地** .doc/.docx 走沙箱（云端已强制 LibreOffice；xlsx/ppt 不套）",
      "def should_sandbox(" in conv_src
      and "(not from_cloud) and ext_of(file_path) in COM_OFFICE_EXTS" in conv_src)
H.chk("★ 失败闭环：沙箱不可用才回退，超时/失败直接拒转（不回退）",
      '"unavailable"' in conv_src and "回退进程内转换" in conv_src
      and "隔离转换失败" in conv_src
      and "raise RuntimeError(" in conv_src)
H.chk("printer_config 有 conversion_sandbox 字段（默认 False，可持久化）",
      "conversion_sandbox: bool = False" in pc_src
      and 'bool(data.get("conversion_sandbox", False))' in pc_src
      and '"conversion_sandbox": self.conversion_sandbox' in pc_src)
H.chk("GUI 启动时把配置应用到 converter（set_office_sandbox）",
      "set_office_sandbox(bool(self._config.conversion_sandbox))" in gui_src)
H.chk("★ 外部引用剥离函数 + 在 HTML 内容转换前调用",
      "def _strip_remote_refs(" in conv_src
      and "_strip_remote_refs(html_path_or_content)" in conv_src
      and "_REMOTE_URL_RE" in conv_src)
H.chk("剥离覆盖：img/source/video 资源属性、CSS url()/@import、link、base、meta refresh",
      all(k in conv_src for k in ("_FETCH_ATTR_RE", "_CSS_URL_RE", "_CSS_IMPORT_RE",
                                  "_LINK_TAG_RE", "_BASE_TAG_RE", "_META_REFRESH_RE")))

# ============================================================
# B. 行为
# ============================================================
print("\n=== B. 行为（真实 Job Object / 假 wkhtmltopdf / 剥离纯函数）===")
import converter as C  # noqa: E402
import office_sandbox as S  # noqa: E402

_old_env = os.environ.get("HN_OFFICE_SANDBOX")

# should_sandbox 矩阵
os.environ["HN_OFFICE_SANDBOX"] = "0"
_docx_local = C.should_sandbox("a.docx")
_xlsx_local = C.should_sandbox("a.xlsx")
os.environ["HN_OFFICE_SANDBOX"] = "1"
H.chk("★ should_sandbox 矩阵：开关关→全 False；开→仅本地 doc/docx True",
      (not _docx_local) and (not _xlsx_local)
      and C.should_sandbox("a.docx") and C.should_sandbox("a.doc")
      and not C.should_sandbox("a.docx", from_cloud=True)
      and not C.should_sandbox("a.xlsx") and not C.should_sandbox("a.txt")
      and not C.should_sandbox("a.pdf"))

# 外部引用剥离
_html = ('<html><head><base href="http://evil/">'
         '<meta http-equiv="refresh" content="0;url=http://evil/">'
         '<link rel="stylesheet" href="https://evil/a.css">'
         '<style>body{background:url(https://evil/b.png)} '
         '@import url(http://evil/c.css);</style></head>'
         '<body><img src="https://evil/x.png">'
         '<img srcset="//evil/y.png 1x, local.png 2x">'
         '<video poster="http://evil/p.jpg"></video>'
         '<img src="data:image/gif;base64,AAA"><img src="local.png">'
         '<a href="https://example.com/">link</a></body></html>')
_clean, _n = C._strip_remote_refs(_html)
_again, _n2 = C._strip_remote_refs(_clean)
H.chk("★ 远程 img/srcset/poster/CSS/link/base/meta-refresh 全部剥离",
      _n >= 7 and "https://evil" not in _clean and "http://evil" not in _clean
      and "//evil" not in _clean)
H.chk("data: 与本地相对路径保留；<a href> 不影响打印、原样保留",
      'data:image/gif;base64,AAA' in _clean and 'src="local.png"' in _clean
      and 'href="https://example.com/"' in _clean)
H.chk("幂等：再剥一次不再计数", _n2 == 0 and _again == _clean, _n2)

# 假 wkhtmltopdf：截获真正传给转换器的 HTML（证明调用点在 strip 之后）
_fake_dir = _mkroot("hn_sbx_wk_")
_capture = {"html": ""}


def _fake_run(cmd, **kw):
    inp, out = os.path.abspath(cmd[-2]), os.path.abspath(cmd[-1])
    with open(inp, "r", encoding="utf-8") as f:
        _capture["html"] = f.read()
    with open(out, "wb") as f:
        f.write(b"%PDF-1.4\n%fake\n")
    return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()


_orig_find_wk = C._find_wkhtmltopdf
_orig_run = C.subprocess.run
C._find_wkhtmltopdf = lambda: "wkhtmltopdf-fake.exe"
C.subprocess.run = _fake_run
try:
    _out_pdf = os.path.join(_fake_dir, "out.pdf")
    C._convert_html_to_pdf('<body><img src="https://evil/x.png">ok</body>',
                           _out_pdf, is_content=True)
finally:
    C._find_wkhtmltopdf = _orig_find_wk
    C.subprocess.run = _orig_run
H.chk("★ 端到端：wkhtmltopdf 收到的 HTML 已无远程引用（调用点在正确位置）",
      "https://evil" not in _capture["html"] and "ok" in _capture["html"],
      _capture["html"][:200])

# 真实 Job Object：txt → PDF（证明 worker/协议/Job 全链路可用）
if S.available():
    _d = _mkroot("hn_sbx_real_")
    _src = os.path.join(_d, "a.txt")
    _dst = os.path.join(_d, "a.pdf")
    with open(_src, "w", encoding="utf-8") as f:
        f.write("沙箱端到端\n第二行\n")
    _res = S.run_office_conversion(_src, _dst, timeout=60)
    H.chk("★ 真实沙箱跑通（Job Object + GO 握手 + RESULT）：txt→PDF 有效",
          _res.status == "ok" and os.path.isfile(_dst) and os.path.getsize(_dst) > 0,
          f"status={_res.status} err={_res.error[:200]}")

    # 超时：worker 与它拉起的孙进程一起被杀（kill-on-close 的核心价值）
    _marker = os.path.join(_d, "grandchild_alive.txt")
    _code = (
        "import subprocess, sys, time;"
        "subprocess.Popen([sys.executable, '-c', "
        "\"import time; time.sleep(3); open(r'%s','w').write('x')\"]);"
        "time.sleep(10)" % _marker.replace("\\", "\\\\"))
    _orig_cmd = S._worker_command
    S._worker_command = lambda spec: [sys.executable, "-c", _code]
    try:
        _res2 = S.run_office_conversion(_src, _dst, timeout=0.8)
    finally:
        S._worker_command = _orig_cmd
    time.sleep(3.6)
    H.chk("★ 超时 → TerminateJobObject 把 worker 和孙进程一起杀掉（没有孤儿）",
          _res2.status == "timeout" and not os.path.exists(_marker),
          f"status={_res2.status} marker={os.path.exists(_marker)}")

# worker 拒绝在没有 GO 时执行
_d2 = _mkroot("hn_sbx_nogo_")
_src2 = os.path.join(_d2, "a.txt")
_spec = os.path.join(_d2, "spec.json")
with open(_src2, "w", encoding="utf-8") as f:
    f.write("x")
with open(_spec, "w", encoding="utf-8") as f:
    json.dump({"src": _src2, "dst": os.path.join(_d2, "a.pdf")}, f)
_wrk = os.path.join(LOCAL_TOOL, "convert_worker.py")
_p = subprocess.run([sys.executable, _wrk, _spec], input="", capture_output=True,
                    text=True, encoding="utf-8", errors="replace", timeout=60)
_has_go = "GO" in (_p.stdout or "")
H.chk("worker 拒绝在没有父进程 GO 的情况下转换（不会在沙箱之外跑 Office）",
      _p.returncode != 0 and _has_go and not os.path.exists(os.path.join(_d2, "a.pdf")),
      f"rc={_p.returncode} out={(_p.stdout or '')[-160:]}")

if _old_env is None:
    os.environ.pop("HN_OFFICE_SANDBOX", None)
else:
    os.environ["HN_OFFICE_SANDBOX"] = _old_env


# ============================================================
# C. 变异自证
# ============================================================
print("\n=== C. 变异自证（删 strip 调用 / should_sandbox 恒 False）===")


def _run_conv_probe(module_dir, body):
    probe = os.path.join(module_dir, "_probe_s2.py")
    with open(probe, "w", encoding="utf-8") as f:
        f.write(
            "# -*- coding: utf-8 -*-\n"
            "import io, os, sys, tempfile\n"
            "sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')\n"
            "sys.path.insert(0, %r)\n"
            "import converter as C\n" % module_dir + body)
    env = dict(os.environ)
    env["PYTHONPATH"] = ""
    env["PYTHONUTF8"] = "1"
    return subprocess.run([sys.executable, probe], cwd=module_dir, env=env,
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=120)


def _mutant_converter(replacements, prefix="hn_sbx_mut_"):
    d = _mkroot(prefix)
    src = _read("converter.py")
    for old, new in replacements:
        if old not in src:
            raise AssertionError("变异锚点未命中: %r" % old[:80])
        src = src.replace(old, new, 1)
    with open(os.path.join(d, "converter.py"), "w", encoding="utf-8") as f:
        f.write(src)
    return d


# 变异 1：不再剥外部引用
_mut1 = _mutant_converter([
    ("            html_text, n_remote = _strip_remote_refs(html_path_or_content)",
     "            html_text, n_remote = html_path_or_content, 0  # 变异：不剥离"),
])
_p1 = _run_conv_probe(_mut1, """
import types
cap = {}
def fake_run(cmd, **kw):
    inp, out = os.path.abspath(cmd[-2]), os.path.abspath(cmd[-1])
    cap['html'] = open(inp, encoding='utf-8').read()
    open(out, 'wb').write(b'%PDF-1.4')
    return types.SimpleNamespace(returncode=0, stdout='', stderr='')
C.subprocess.run = fake_run
C._find_wkhtmltopdf = lambda: 'wk.exe'
out = os.path.join(tempfile.mkdtemp(), 'o.pdf')
C._convert_html_to_pdf('<img src="https://evil/x.png">', out, is_content=True)
print("RESULT=%d" % int("https://evil" in cap.get('html', '')))
""")
_v1 = H.probe_value(_p1)
H.chk("删掉剥离调用后，远程 URL 进了转换输入（B 的判据能红）",
      _v1 == "1", f"probe={_v1} stderr={(_p1.stderr or '')[-200:]}")

# 变异 2：should_sandbox 恒 False
_mut2 = _mutant_converter([
    ("    return (not from_cloud) and ext_of(file_path) in COM_OFFICE_EXTS and office_sandbox_enabled()",
     "    return False  # 变异：永不进沙箱"),
])
_p2 = _run_conv_probe(_mut2, """
os.environ['HN_OFFICE_SANDBOX'] = '1'
print("RESULT=%d" % int(C.should_sandbox('a.docx')))
""")
_v2 = H.probe_value(_p2)
H.chk("should_sandbox 恒 False 后本地 docx 不再进沙箱（B 的矩阵判据能红）",
      _v2 == "0", f"probe={_v2} stderr={(_p2.stderr or '')[-200:]}")

for _d in _TMPS:
    shutil.rmtree(_d, ignore_errors=True)

H.finish()
