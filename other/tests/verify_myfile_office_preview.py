# -*- coding: utf-8 -*-
"""验收 · 「我的文件」预览图扩到 Office（用户 2026-11-13 要求 2）

用户原话（m00005）：「2. 略缩图尽量显示一下」。追问后选择 A+B：
  既要修好 PDF 那条一直失败的路，也要让 Word / Excel / PPT 出首页缩略图。

背景（为什么以前只有图片和 PDF）：后端 `_has_preview_for()` 只认 `_PREVIEW_IMAGE_EXTS` 和 `.pdf`；
线上服务器还缺 LibreOffice（无 soffice）与 CJK 字体、venv 也没有 PyMuPDF ⇒ 连 PDF 都渲染不出来
（生成失败后前端静默退回类型图标，看起来"只是没有预览图"）。

本脚本钉住两件事：
  ① **接线**：Office 扩展名进白名单、`file_preview` 分发到 `_render_office_preview`、
     `requirements.txt` 钉住 `pymupdf==1.26.7`（2.x 会拖进 onnxruntime 等重依赖，2G 内存的服务器扛不住）；
  ② **行为**：真的把 app.py 里的这几个函数**取出来执行**（stub 掉 subprocess/glob/which），
     验证 `_has_preview_for` 的判定、soffice 命令行参数（独立 profile、--convert-to 过滤器）、
     表格"整表一页"重试、没有输出 PDF 时抛错、以及失败负缓存（10 分钟内不再重试）。

2026-11-13 上线后线上实测又踩到两个坑，同样钉在这里：
  ③ `files.path` 里**本来就带 uploads/ 前缀**，旧代码又拼了一次 UPLOAD_DIR ⇒
     `uploads/uploads/docx/…` 恒不存在，`/api/file_preview` 对**所有类型**（含图片）都回 404；
  ④ systemd 单元里 `Environment=PATH=/home/printer-backend/venv/bin` 是唯一 PATH ⇒
     `shutil.which("soffice")` 恒 None，而 `/usr/bin/soffice` 本身是 shell 包装脚本、
     要 dirname/sed/grep/uname 才能跑（缺了以 127 退出）。所以既要查固定路径，也要补 PATH。

  ⑤ 多页预览 / 上传预热（2026-11-13 第二批需求，用户 m00771）：预览缓存按页编号，
     Office 只转一次 PDF 落盘复用（翻页不重跑 soffice），上传成功后台线程预热前
     PREVIEW_MAX_PAGES 页并写页数元信息；文件删除/过期清理时 `_drop_previews` 必须把
     .jpg / .pdf / .json 一起带走（缩略图随文件一起消失，不留孤儿）。

运行：python other/tests/verify_myfile_office_preview.py   （exit 0 = 全绿）
"""
import ast
import glob as real_glob
import io
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
APP_PY = os.path.join(REPO, "mobile_apps", "printer-backend", "app.py")
REQUIREMENTS = os.path.join(REPO, "mobile_apps", "printer-backend", "requirements.txt")

failures = 0


def check(name, cond, detail=None):
    global failures
    if not cond:
        failures += 1
    line = "[%s] %s" % ("PASS" if cond else "FAIL", name)
    if detail:
        line += "  -- " + str(detail)
    print(line)


SRC = io.open(APP_PY, encoding="utf-8").read()
TREE = ast.parse(SRC)
LINES = SRC.splitlines(True)


def node_src(node):
    return "".join(LINES[node.lineno - 1:node.end_lineno])


# --------------------------------------------------------------------------------------
# 一、接线（源码层）：扩展名 / 分发 / 依赖
# --------------------------------------------------------------------------------------
print("\n--- 1) 接线：Office 进预览白名单 + 分发 + 依赖 ---")

OFFICE_EXTS = (".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx")
check("_PREVIEW_OFFICE_EXTS 覆盖 Word/Excel/PPT 六种扩展名",
      re.search(r'_PREVIEW_OFFICE_EXTS\s*=\s*\([^)]*\.docx[^)]*\.xlsx[^)]*\.pptx', SRC) is not None,
      re.search(r"_PREVIEW_OFFICE_EXTS\s*=\s*\([^)]*\)", SRC).group(0) if re.search(r"_PREVIEW_OFFICE_EXTS\s*=\s*\([^)]*\)", SRC) else "未找到")
check("_has_preview_for 认 Office 扩展名",
      "ext in _PREVIEW_OFFICE_EXTS" in SRC)
check("file_preview 端点按扩展名分发（图片 → 原图缩放；PDF/Office → 光栅化分页）",
      "_render_preview_page(src_path, display, file_id, mtime_token" in SRC
      and "_PREVIEW_OFFICE_EXTS" in SRC)
check("渲染失败写进负缓存（_preview_fail_note），下次点击不重转",
      "_preview_fail_note(fail_key" in SRC and "_preview_fail_hit(fail_key)" in SRC)
check("files.path 用 _resolve_files_path 解析（旧写法二次拼 uploads/ ⇒ 预览接口恒 404）",
      '_resolve_files_path(row["path"])' in SRC)
check("files.path 二次拼接的老写法已清除（这才是「一直没有缩略图」的真因，不是渲染能力）",
      'os.path.join(UPLOAD_DIR, rel_path)' not in SRC)
check("requirements.txt 钉住 pymupdf==1.26.7（2.x 会拖 onnxruntime 等重依赖）",
      re.search(r"^pymupdf==1\.26\.7$", io.open(REQUIREMENTS, encoding="utf-8").read(),
                re.MULTILINE) is not None)

# 从 app.py 取出需要的常量与函数，直接在本地执行（不 import Flask app，避免连数据库/起调度器）
WANTED_CONST = {"_PREVIEW_IMAGE_EXTS", "_PREVIEW_OFFICE_EXTS", "_PREVIEW_OFFICE_TIMEOUT",
                "_PREVIEW_FAIL_TTL", "_preview_fail_cache", "_soffice_path",
                "_CALC_ONE_PAGE_FILTER", "UPLOAD_DIR", "PREVIEW_DIR", "PREVIEW_MAX_PAGES",
                "_PREVIEW_LOCK_GUARD", "_preview_page_locks",
                "_preview_meta_index_cache"}
# 注意：`_render_pdf_page_bytes` / `_pdf_page_total` 故意**不**从源码抠（它们依赖 PyMuPDF/
# pdftoppm），改由 build_ns 注入桩函数，本脚本才能在任何机器上跑。
WANTED_FUNC = {"_has_preview_for", "_soffice_bin", "_preview_fail_hit", "_preview_fail_note",
               "_soffice_to_pdf", "_render_office_preview", "_resolve_files_path",
               "_ensure_office_pdf", "_preview_pdf_for", "_render_preview_page",
               "_preview_total_pages", "_prewarm_previews", "_drop_previews",
               "_preview_stem", "_preview_cache_path", "_preview_pdf_path",
               "_preview_meta_path", "_read_preview_meta", "_write_preview_meta",
               "_preview_pages_hint", "_preview_lock_for", "_preview_meta_index"}

pieces = []
for node in TREE.body:
    if isinstance(node, ast.Assign):
        for t in node.targets:
            if isinstance(t, ast.Name) and t.id in WANTED_CONST:
                pieces.append(node_src(node))
    elif isinstance(node, ast.FunctionDef) and node.name in WANTED_FUNC:
        pieces.append(node_src(node))
FOUND = set()
for node in TREE.body:
    if isinstance(node, ast.Assign):
        for t in node.targets:
            if isinstance(t, ast.Name):
                FOUND.add(t.id)
    elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        FOUND.add(node.name)
missing = sorted((WANTED_CONST | WANTED_FUNC) - FOUND)
check("能在 app.py 里定位到全部预览常量/函数（改结构时会在这里红，提示同步本脚本）",
      not missing, "missing=" + repr(missing))
CODE = "\n\n".join(pieces)


class FakeProc(object):
    def __init__(self, out=b"", err=b"", code=0):
        self.stdout = out
        self.stderr = err
        self.returncode = code


def build_ns(which_hit="/usr/bin/soffice", produced=True, run_hook=None):
    """搭一个只含预览相关代码的命名空间（外部依赖全部 stub）。"""
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append({"cmd": list(cmd), "kwargs": kwargs})
        if run_hook:
            run_hook(cmd, kwargs)
        return FakeProc()

    class SubprocessStub(object):
        run = staticmethod(fake_run)

    class ShutilStub(object):
        which = staticmethod(lambda name: which_hit if name == "soffice" else None)
        rmtree = staticmethod(lambda *a, **k: None)
        copyfile = staticmethod(shutil.copyfile)   # _ensure_office_pdf 会把转换结果缓存下来

    def fake_glob(pattern):
        # 只在 --outdir 里"产出"一个 PDF，模拟 soffice 转换成功。
        # 2026-11-13：_ensure_office_pdf 会把这份 PDF copyfile 到缓存路径，
        # 所以这里必须真的落一个文件（以前只回路径，copyfile 必失败）。
        if not produced or not pattern.endswith("*.pdf"):
            return []
        hit = pattern.replace("*.pdf", "input.pdf")
        try:
            with open(hit, "wb") as fh:
                fh.write(b"%PDF-1.4\n% fake preview source\n")
        except OSError:
            return []
        return [hit]

    class GlobStub(object):
        glob = staticmethod(fake_glob)

    class TempfileStub(object):
        mkdtemp = staticmethod(lambda prefix="": tempfile.mkdtemp(prefix=prefix))

    total_box = [1]
    ns = {
        "os": os, "re": re, "time": time, "threading": threading, "json": json,
        "subprocess": SubprocessStub, "shutil": ShutilStub, "glob": GlobStub,
        "tempfile": TempfileStub,
        "_render_pdf_preview": lambda p: b"JPEG:" + os.path.basename(p).encode(),
        # 光栅化 / 页数：注入桩（真实现要 PyMuPDF 或 pdftoppm，本机不一定有）
        "_render_pdf_page_bytes": lambda p, i: (b"JPEG:" + os.path.basename(p).encode()
                                                + b"#" + str(int(i)).encode()),
        "_pdf_page_total": lambda p: int(total_box[0]),
        "_redact_filename": lambda s: s,
        "__total_box__": total_box,
        "_PREVIEW_RENDER_LOCK": threading.Lock(),
    }
    exec(compile(CODE, APP_PY, "exec"), ns)  # noqa: S102 —— 只执行本项目自己的源码片段
    return ns, calls


# --------------------------------------------------------------------------------------
# 二、行为：判定函数
# --------------------------------------------------------------------------------------
print("\n--- 2) _has_preview_for：Office 现在算「有预览图」 ---")
ns, _ = build_ns()
for name in ("a.docx", "b.doc", "c.xlsx", "d.xls", "e.pptx", "f.ppt",
             "g.pdf", "h.PNG", "i.JPEG"):
    check("有预览图：%s" % name, ns["_has_preview_for"](name) is True)
for name in ("a.txt", "b.md", "c.csv", "d.zip", "e", "f.docx.exe"):
    check("没有预览图：%s（前端退回类型图标）" % (name or "空串"),
          ns["_has_preview_for"](name) is False,
          ns["_has_preview_for"](name))
check("大小写扩展名一视同仁（.DOCX）", ns["_has_preview_for"]("报告.DOCX") is True)

# --------------------------------------------------------------------------------------
# 三、行为：soffice 命令行
# --------------------------------------------------------------------------------------
print("\n--- 3) _soffice_to_pdf：命令行参数必须是实测跑通的那一条 ---")
ns, calls = build_ns()
tmpdir = tempfile.mkdtemp(prefix="hn_preview_test_")
try:
    out = ns["_soffice_to_pdf"]("/usr/bin/soffice", os.path.join(tmpdir, "in.docx"),
                                tmpdir, "pdf")
    cmd = calls[-1]["cmd"]
    check("返回产出的 PDF 路径", out.endswith("input.pdf"), out)
    check("参数含 --headless/--norestore/--invisible/--nologo（没这些会弹 GUI 卡住）",
          all(x in cmd for x in ("--headless", "--norestore", "--invisible", "--nologo")))
    check("参数含 --nodefault/--nolockcheck/--nofirststartwizard（首次运行不要交互）",
          all(x in cmd for x in ("--nodefault", "--nolockcheck", "--nofirststartwizard")))
    prod = [x for x in cmd if x.startswith("-env:UserInstallation=")]
    check("每次调用一个独立 profile（共享 profile 被锁 = 假失败）",
          len(prod) == 1 and os.path.basename(prod[0]) == "profile", prod)
    check("--convert-to 收到传入的过滤器", "--convert-to" in cmd and cmd[cmd.index("--convert-to") + 1] == "pdf")
    check("--outdir 与源文件都在命令行里", "--outdir" in cmd and cmd[-1].endswith("in.docx"))
    check("HOME 指到可写 tmpdir（root 的 HOME 不可写会让 soffice 直接退出）",
          calls[-1]["kwargs"]["env"].get("HOME") == tmpdir)

    # 2026-11-13 线上踩的坑：systemd 单元的 PATH 只有 venv/bin ⇒ /usr/bin/soffice 这个 shell
    # 包装脚本找不到 dirname/sed/grep/uname，直接以 127 退出，表面报「LibreOffice 没有输出 PDF」。
    _saved_path = os.environ.get("PATH", "")
    os.environ["PATH"] = "/home/printer-backend/venv/bin"
    try:
        ns_path, calls_path = build_ns()
        ns_path["_soffice_to_pdf"]("/usr/bin/soffice", "in.docx", tmpdir, "pdf")
        env_path = calls_path[-1]["kwargs"]["env"].get("PATH", "")
    finally:
        os.environ["PATH"] = _saved_path
    parts_path = env_path.split(os.pathsep)
    check("soffice 的 PATH 补齐 /usr/bin 与 /bin（包装脚本要用系统命令，缺了 127）",
          "/usr/bin" in parts_path and "/bin" in parts_path, env_path)
    check("原有 PATH 项保留（venv/bin 不能被挤掉）",
          "/home/printer-backend/venv/bin" in parts_path, env_path)
    check("subprocess 有超时（soffice 卡死不能把 gunicorn 线程一起拖住）",
          calls[-1]["kwargs"].get("timeout") == ns["_PREVIEW_OFFICE_TIMEOUT"])
    check("check=False + capture_output（失败不抛栈，靠返回值判）",
          calls[-1]["kwargs"].get("check") is False and calls[-1]["kwargs"].get("capture_output") is True)

    # 没有输出 PDF ⇒ 返回空串（调用方据此抛 415）
    ns2, _ = build_ns(produced=False)
    check("soffice 没产出 PDF 时返回空串（不是抛异常给用户）",
          ns2["_soffice_to_pdf"]("/usr/bin/soffice", "x.docx", tmpdir, "pdf") == "")
finally:
    shutil.rmtree(tmpdir, ignore_errors=True)

# --------------------------------------------------------------------------------------
# 四、行为：_render_office_preview（分发 / 表格整表一页 / 缺能力时报错）
# --------------------------------------------------------------------------------------
print("\n--- 4) _render_office_preview：docx 直转、xlsx 先试整表一页 ---")
ns, calls = build_ns()
jpg = ns["_render_office_preview"]("a.docx")
check("docx：转一次 PDF 后走 _render_pdf_preview 取第 1 页",
      jpg.startswith(b"JPEG:") and len(calls) == 1 and "pdf" == calls[0]["cmd"][calls[0]["cmd"].index("--convert-to") + 1],
      jpg[:20])

ns, calls = build_ns()
ns["_render_office_preview"]("a.xlsx")
specs = [c["cmd"][c["cmd"].index("--convert-to") + 1] for c in calls]
check("xlsx：先用 SinglePageSheets 过滤器（整表压一页，缩略图能看出表格形状）",
      specs and "SinglePageSheets" in specs[0], specs)
check("xlsx：过滤器写进 pdf:calc_pdf_Export 的 JSON 里",
      specs and specs[0].startswith("pdf:calc_pdf_Export:") and '"SinglePageSheets"' in specs[0])

# 老 LibreOffice 不认这个过滤器 ⇒ 必须退回普通 pdf 再试一次
ns, calls = build_ns()

def _hook(cmd, kwargs):
    pass

ns_fallback, calls_fb = build_ns()

def fake_glob_fallback(pattern):
    # 第一次（整表一页）不出活，第二次（普通 pdf）出活（并且真的落文件，
    # 因为 _ensure_office_pdf 会 copyfile 到缓存路径）
    if len(calls_fb) <= 1:
        return []
    hit = pattern.replace("*.pdf", "input.pdf")
    try:
        with open(hit, "wb") as fh:
            fh.write(b"%PDF-1.4\n% fake fallback\n")
    except OSError:
        return []
    return [hit]

class _FallbackGlob(object):
    glob = staticmethod(fake_glob_fallback)

ns_fallback["glob"] = _FallbackGlob
jpg = ns_fallback["_render_office_preview"]("a.xlsx")
specs_fb = [c["cmd"][c["cmd"].index("--convert-to") + 1] for c in calls_fb]
check("xlsx：整表一页失败时退回普通 pdf 再试（老版本 LibreOffice 也能出图）",
      specs_fb == [ns_fallback["_CALC_ONE_PAGE_FILTER"], "pdf"] and jpg.startswith(b"JPEG:"),
      specs_fb)

ns, calls = build_ns(which_hit="")
try:
    ns["_render_office_preview"]("a.docx")
    raised = False
except RuntimeError as e:
    raised = "LibreOffice" in str(e)
check("服务器没有 LibreOffice ⇒ 抛 RuntimeError（端点回 415，前端退回图标，不 500）",
      raised and not calls)

# --------------------------------------------------------------------------------------
# 4.5) _soffice_bin：which 找不到时要查固定路径（systemd 窄 PATH 的兜底）
# --------------------------------------------------------------------------------------
print("\n--- 4.5) _soffice_bin：which 之外还要认固定安装位置 ---")


class _PathNS(object):
    """只回答"某个路径在不在"的 os.path 替身。"""

    def __init__(self, visible):
        self._visible = visible

    def isfile(self, p):
        return p in self._visible

    def isabs(self, p):
        return os.path.isabs(p)

    def normpath(self, p):
        return os.path.normpath(p)

    def join(self, *a):
        return os.path.join(*a)

    def basename(self, p):
        return os.path.basename(p)

    def dirname(self, p):
        return os.path.dirname(p)

    def splitext(self, p):
        return os.path.splitext(p)


class _OsNS(object):
    """把 os 换成"只认指定可见路径"的替身，其余属性照常转发。"""

    def __init__(self, visible, env_path="/home/printer-backend/venv/bin"):
        self.path = _PathNS(visible)
        self.environ = {"PATH": env_path}
        self.sep = os.sep
        self.pathsep = os.pathsep

    def access(self, p, mode):
        return p in self.path._visible

    def __getattr__(self, name):
        return getattr(os, name)


ns_abs, _ = build_ns(which_hit="")
ns_abs["os"] = _OsNS({"/usr/bin/soffice"})
check("which 返回 None 时退回 /usr/bin/soffice（apt 装的就在这）",
      ns_abs["_soffice_bin"]() == "/usr/bin/soffice", ns_abs["_soffice_bin"]())

ns_none, _ = build_ns(which_hit="")
ns_none["os"] = _OsNS(set())
check("到处都没有 soffice ⇒ 返回空串（调用方据此抛 RuntimeError → 415）",
      ns_none["_soffice_bin"]() == "", ns_none["_soffice_bin"]())

ns_which, _ = build_ns(which_hit="/opt/lo/soffice")
check("which 命中时优先用它的结果（自定义安装优先）",
      ns_which["_soffice_bin"]() == "/opt/lo/soffice", ns_which["_soffice_bin"]())

# --------------------------------------------------------------------------------------
# 五、行为：失败负缓存（10 分钟）
# --------------------------------------------------------------------------------------
print("\n--- 5) 失败负缓存：同一 (file_id, mtime) 10 分钟内不重转 ---")
ns, _ = build_ns()
key = ("abc", "1712345678")
check("没记过 ⇒ 不命中", ns["_preview_fail_hit"](key) is False)
ns["_preview_fail_note"](key, "soffice 没输出 PDF")
check("记过 ⇒ 命中（用户再点不重转，服务器不被打爆）", ns["_preview_fail_hit"](key) is True)
ns["_preview_fail_cache"][key] = (time.time() - 1, "expired")
check("超过 TTL ⇒ 不再命中（文件被换掉后还能重新生成）", ns["_preview_fail_hit"](key) is False)
check("TTL 是 600 秒", ns["_PREVIEW_FAIL_TTL"] == 600, ns["_PREVIEW_FAIL_TTL"])
for i in range(600):
    ns["_preview_fail_note"](("f%d" % i, "m"), "x")
check("缓存超过 500 条会整表清空（不能被文件名刷爆内存）",
      len(ns["_preview_fail_cache"]) <= 500, len(ns["_preview_fail_cache"]))
long_key = ("long", "m")
ns["_preview_fail_note"](long_key, "x" * 5000)
check("失败原因截断到 200 字（不把整段栈存进内存）",
      len(ns["_preview_fail_cache"][long_key][1]) == 200,
      len(ns["_preview_fail_cache"][long_key][1]))

# --------------------------------------------------------------------------------------
# 七、行为：多页 / 上传预热 / 缓存清理（2026-11-13 第二批需求）
# --------------------------------------------------------------------------------------
print("\n--- 7) 多页预览 / 上传预热 / 缓存清理 ---")
check("file_preview 支持 ?page=N（多页翻页）", 'request.args.get("page"' in SRC)
check("响应头带 X-Preview-Page / X-Preview-Pages（前端据此渲染页码）",
      '"X-Preview-Page"' in SRC and '"X-Preview-Pages"' in SRC)
check("有 /api/file_preview/<file_id>/info 端点（先预热再回总页数）",
      '/api/file_preview/<file_id>/info' in SRC)
check("有 /api/file_detail/<file_id> 端点（订单卡片点文件 → 独立详情弹窗）",
      '/api/file_detail/<file_id>' in SRC and 'def get_file_detail' in SRC)
check("列表项下发 preview_pages（详情不用再问一次接口）", '"preview_pages"' in SRC)
check("上传成功后挂后台预热线程（不是等用户点开才生成）",
      "_spawn_preview_prewarm(file_id, display_name)" in SRC)
check("_drop_previews 清的不只是 .jpg（还有中间 PDF / 页数元信息）",
      'f"{safe_id}_*"' in SRC)
# 2026-11-13 线上事故：_preview_request_context 成功分支也曾返回 tuple，
# 而调用方用 isinstance(ctx, tuple) 判"这是错误响应" ⇒ 视图把 ctx 交给 Flask 当响应元组
# ⇒ 所有预览请求一律 500 `TypeError: The view function did not return a valid response tuple.`
check("预览前置成功分支返回 dict（不能是 tuple，否则调用方判错 / 视图 500）",
      '"mtime_token": mtime_token}' in SRC
      and "return row, src_path, display, mtime_token" not in SRC)
check("调用方从 dict 取字段（src_path/display/mtime_token）",
      'src_path = ctx["src_path"]' in SRC and 'mtime_token = ctx["mtime_token"]' in SRC)

ns, _ = build_ns()
ns["PREVIEW_DIR"] = tempfile.mkdtemp(prefix="hn_prev_dir_")
try:
    stem = ns["_preview_stem"]("f-1/../2", "1712345678abc")
    check("缓存前缀去掉路径分隔符（防目录穿越）",
          "/" not in stem and ".." not in stem and stem.endswith("1712345678"), stem)
    p1 = ns["_preview_cache_path"]("f1", "17", 1)
    p2 = ns["_preview_cache_path"]("f1", "17", 2)
    check("第 1 页沿用旧文件名（历史缓存继续命中）", p1.endswith("f1_17.jpg"), p1)
    check("第 2 页起带 _pN 后缀（多页缓存互不覆盖）", p2.endswith("f1_17_p2.jpg"), p2)
    check("Office 中间 PDF 有独立缓存路径（翻页不重跑 soffice）",
          ns["_preview_pdf_path"]("f1", "17").endswith("f1_17.pdf"))
    check("页数元信息也有缓存文件", ns["_preview_meta_path"]("f1", "17").endswith("f1_17.json"))

    check("图片的总页数恒为 1", ns["_preview_total_pages"]("a.jpg", "a.jpg", "f1", "17") == 1)
    ns["__total_box__"][0] = 5
    check("docx 的总页数取自转换后的 PDF（不是 files.page_count）",
          ns["_preview_total_pages"]("a.docx", "a.docx", "f2", "17") == 5)
    page2 = ns["_render_preview_page"]("a.docx", "a.docx", "f2", "17", 1)
    check("第 2 页传给光栅化的是 1 基页-1（0 基）", page2.endswith(b"#1"), page2)
    try:
        ns["_render_preview_page"]("a.jpg", "a.jpg", "f3", "17", 1)
        raised = False
    except RuntimeError as e:
        raised = "只有一页" in str(e)
    check("图片没有第 2 页 ⇒ 抛 RuntimeError（端点回落 415，不 500）", raised)

    # --- 上传预热：渲染 min(总页数, PREVIEW_MAX_PAGES) 页 + 写元信息 ---
    up_dir = tempfile.mkdtemp(prefix="hn_prev_up_")
    real_docx = os.path.join(up_dir, "real.docx")
    with open(real_docx, "wb") as fh:
        fh.write(b"docx")
    ns["UPLOAD_DIR"] = up_dir

    class _FakeConn(object):
        def __init__(self, row):
            self._row = row
            self.closed = False

        def execute(self, sql, params=()):
            class _Cur(object):
                def __init__(self, row):
                    self._row = row

                def fetchone(self):
                    return self._row
            return _Cur(self._row)

        def close(self):
            self.closed = True

    ns["get_db"] = lambda: _FakeConn({"id": "f9", "original_name": "real.docx",
                                      "path": real_docx})
    ns["__total_box__"][0] = 3
    check("预热成功返回 True", ns["_prewarm_previews"]("f9") is True)
    rendered = [p for p in os.listdir(ns["PREVIEW_DIR"]) if p.startswith("f9_") and p.endswith(".jpg")]
    check("预热按总页数生成 JPEG（3 页 → 3 个文件）", len(rendered) == 3, sorted(rendered))
    # 元信息按真实 mtime token 落盘，这里直接用同名文件反查
    meta_paths = [p for p in os.listdir(ns["PREVIEW_DIR"]) if p.startswith("f9_") and p.endswith(".json")]
    check("预热写页数元信息（前端列表不用再问接口）", len(meta_paths) == 1, meta_paths)
    with open(os.path.join(ns["PREVIEW_DIR"], meta_paths[0]), encoding="utf-8") as fh:
        meta = json.load(fh)
    check("元信息里 pages/rendered 都对", meta.get("pages") == 3 and meta.get("rendered") == 3, meta)

    calls = {"n": 0}
    real_render = ns["_render_pdf_page_bytes"]
    ns["_render_pdf_page_bytes"] = lambda p, i: (calls.__setitem__("n", calls["n"] + 1),
                                                 real_render(p, i))[1]
    ns["_prewarm_previews"]("f9")
    check("同一文件第二次预热直接返回（不重复渲染）", calls["n"] == 0, calls["n"])
    ns["_render_pdf_page_bytes"] = real_render

    # 列表页数：meta（真实渲染页数）优先于 files.page_count
    # 线上实例：docx 分析出 page_count=2，LibreOffice 转出来 3 页 ⇒ 只信 page_count 会漏页
    glob_stub = ns["glob"]          # 上面为 soffice 注入的 GlobStub，下面几行结束后要还原
    ns["glob"] = real_glob          # 这里要看真实缓存文件
    check("列表页数优先取预热 meta（3 页），不是传入的 page_count(2)",
          ns["_preview_pages_hint"]("f9", "real.docx", 2) == 3,
          ns["_preview_pages_hint"]("f9", "real.docx", 2))
    check("没有 meta 的老文件仍退回 page_count（不会白白变成 1 页）",
          ns["_preview_pages_hint"]("nometa", "old.docx", 5) == 5)
    check("图片一律 1 页（哪怕有别的 meta）", ns["_preview_pages_hint"]("f9", "a.png", 99) == 1)
    # 目录索引要能感知新写入的 meta（mtime 变了就重建），否则新上传的文件列表页数会一直是 0
    ns["_write_preview_meta"]("f38", "999", 7, 7)
    check("新写入的 meta 立刻进索引（不用重启进程）",
          ns["_preview_pages_hint"]("f38", "new.docx", 0) == 7,
          ns["_preview_pages_hint"]("f38", "new.docx", 0))
    ns["glob"] = glob_stub

    # 页数超上限时只预热前 PREVIEW_MAX_PAGES 页
    shutil.rmtree(ns["PREVIEW_DIR"], ignore_errors=True)
    os.makedirs(ns["PREVIEW_DIR"], exist_ok=True)
    ns["__total_box__"][0] = 30
    ns["_prewarm_previews"]("f9")
    capped = [p for p in os.listdir(ns["PREVIEW_DIR"]) if p.endswith(".jpg")]
    check("超长文档只预热前 PREVIEW_MAX_PAGES 页（2G 机器不能被 200 页的 PDF 拖死）",
          len(capped) == ns["PREVIEW_MAX_PAGES"], (len(capped), ns["PREVIEW_MAX_PAGES"]))

    # --- 清理：预览缓存必须跟文件一起消失（.jpg/.pdf/.json 全带走） ---
    # 这里要用真的 glob（上面那套 GlobStub 只会伪造 soffice 的 *.pdf 输出）
    shutil.rmtree(ns["PREVIEW_DIR"], ignore_errors=True)
    os.makedirs(ns["PREVIEW_DIR"], exist_ok=True)
    ns["glob"] = real_glob
    for name in ("abc_1.jpg", "abc_1_p2.jpg", "abc_1.pdf", "abc_1.json", "abcd_1.jpg"):
        with open(os.path.join(ns["PREVIEW_DIR"], name), "wb") as fh:
            fh.write(b"x")
    ns["_drop_previews"]("abc")
    left = sorted(os.listdir(ns["PREVIEW_DIR"]))
    check("删除文件时预览缓存（含多页/中间 PDF/元信息）一起清掉",
          left == ["abcd_1.jpg"], left)

    # --- 负缓存 key 带页号：第 2 页失败不能连累第 1 页 ---
    ns["_preview_fail_note"](("fid", "17", 2), "页 2 炸了")
    check("失败的负缓存按页记账（第 2 页失败不影响第 1 页）",
          ns["_preview_fail_hit"](("fid", "17", 2)) is True
          and ns["_preview_fail_hit"](("fid", "17", 1)) is False)
finally:
    shutil.rmtree(ns["PREVIEW_DIR"], ignore_errors=True)
    shutil.rmtree(up_dir, ignore_errors=True)

# --------------------------------------------------------------------------------------
# 六、行为：files.path 的两种历史形态（uploads 前缀）
# --------------------------------------------------------------------------------------
print("\n--- 6) _resolve_files_path：files.path 带不带 uploads/ 前缀都要认 ---")
tmp_root = tempfile.mkdtemp(prefix="hn_path_test_")
_saved_cwd = os.getcwd()
try:
    os.makedirs(os.path.join(tmp_root, "uploads", "docx"))
    _sample = os.path.join(tmp_root, "uploads", "docx", "x.docx")
    with open(_sample, "wb") as _fh:
        _fh.write(b"docx")
    os.chdir(tmp_root)  # app.py 里 UPLOAD_DIR 是相对 cwd 的 "uploads"
    ns_p, _ = build_ns()
    got = ns_p["_resolve_files_path"]("uploads/docx/x.docx")
    check("已是真实相对路径（带 uploads/ 前缀，现网数据形态）⇒ 直接命中",
          os.path.isfile(got) and os.path.basename(got) == "x.docx", got)
    check("不会变成 uploads/uploads/docx/…（线上 404 的根因）",
          os.path.normpath(got).count("uploads") == 1, got)
    got_old = ns_p["_resolve_files_path"]("docx/x.docx")
    check("老库里存的是相对 uploads/ 的路径 ⇒ 自动补前缀",
          os.path.isfile(got_old) and os.path.basename(got_old) == "x.docx", got_old)
    check("绝对路径原样返回",
          ns_p["_resolve_files_path"](_sample) == _sample)
    check("空串 / None ⇒ 空串（端点据此回 404，不抛）",
          ns_p["_resolve_files_path"]("") == "" and ns_p["_resolve_files_path"](None) == "")
    miss = ns_p["_resolve_files_path"]("uploads/docx/nope.docx")
    check("文件真不存在 ⇒ 仍返回字符串，由端点判 404",
          isinstance(miss, str) and not os.path.isfile(miss), miss)
finally:
    os.chdir(_saved_cwd)
    shutil.rmtree(tmp_root, ignore_errors=True)

# --------------------------------------------------------------------------------------
print("\n" + ("=" * 60))
if failures:
    print("存在 FAIL ❌（%d 条）" % failures)
    sys.exit(1)
print("Office 预览（docx/xls/ppt 首页缩略图）: 全部通过 ✅")
