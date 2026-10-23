# -*- coding: utf-8 -*-
"""S1-02 更新链加固验收（DS-03 / DS-05）

被测对象（docs/安全加固修复计划_2026-10-23.md §S1-02，D-1「不买代码签名证书」⇒ 无证书方案
就是最终形态）：
  · local_print_tool/updater.py           host 白名单 / https-only / 重定向后二次校验 /
                                          sha256 优先+md5 兼容且必填 / ed25519 验签（先验签后下包）/
                                          私有目录落地 + 校验后重命名 / 静默装需用户明确确认
  · local_print_tool/gui.py（仅更新相关）  启动检查只提示、默认按钮「稍后」、/VERYSILENT 仅在明确点击后
  · local_print_tool/tools/sign_update_manifest.py  离线签名工具（私钥不入库）
  · local_print_tool/requirements.txt + HN打印工具.spec   cryptography 依赖可被收集
  · mobile_apps/android_app/www/updater.js  sha256/md5 必填 + versionCode 单调 + 禁止 302 + 关闭静默装

三段：
  A. 静态判据（读源码；任何一条被改回去都必须红）
  B. 行为判据（**本地 http 桩 + 临时清单，绝不联网**）：外来 host / 缺哈希 / 错哈希 / 错签名
     （且不下载包）/ 正确签名+正确哈希 → 通过；302 跨 host 拒绝；签名工具端到端
  C. 反向验证（--reverse）：`git show d2e8851:<path>` 取改前版本覆盖源文件 → 跑同一脚本 →
     必须 exit != 0 且**红在断言**（不是 NameError/ImportError/Traceback）；finally 无条件还原并核对 md5

为什么行为段要"改模块里的白名单常量"而不是连本地 http 服务打 https：
  白名单是"硬编码的信任根"，只允许两个真实域名。要验证"外来 host 必须拒绝"就只能在本地
  桩服务上把白名单临时改成 127.0.0.1（或用真实白名单断言 evil 被拒），这样既不联网，
  又能真的让 download_setup 走完"下载 → 校验 → 落盘"的全部代码路径。

用法：
  python tests/verify_update_chain.py              # 正向（默认；run_all 用这个）
  python tests/verify_update_chain.py --reverse    # 反向验证（会临时覆盖 5 个源文件，随后还原）
退出码：0 = 全绿；1 = 有断言红（反向验证时"红"才是期望值）。
"""

from __future__ import annotations

import ast
import base64
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL_TOOL = os.path.join(REPO, "local_print_tool")
UPDATER_PY = os.path.join(LOCAL_TOOL, "updater.py")
GUI_PY = os.path.join(LOCAL_TOOL, "gui.py")
REQUIREMENTS = os.path.join(LOCAL_TOOL, "requirements.txt")
SPEC = os.path.join(LOCAL_TOOL, "HN打印工具.spec")
INSTALLER_JSON = os.path.join(LOCAL_TOOL, "installer", "update.json")
APK_JS = os.path.join(REPO, "mobile_apps", "android_app", "www", "updater.js")
SIGN_TOOL = os.path.join(LOCAL_TOOL, "tools", "sign_update_manifest.py")

BASE_COMMIT = "d2e8851"
HCOS = "hn-printer-updates-1324480550.cos.ap-guangzhou.myqcloud.com"

# ---------------------------------------------------------------- 断言脚手架
failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> bool:
    ok = bool(cond)
    if not ok:
        failures.append(name)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))
    return ok


def section(title: str) -> None:
    print(f"\n================ {title} ================")


def info(msg: str) -> None:
    print(f"       {msg}")


def guarded(name: str, fn, *args, **kwargs):
    """调用被测函数；异常转成 None 并记一条 FAIL。

    反向验证（改前版本）时函数可能不存在/签名不同：必须红在断言上，**不能**崩在
    TypeError/AttributeError 里（那是"假红"，曾经骗过这个仓库好几次）。"""
    if fn is None:
        return None
    try:
        return fn(*args, **kwargs)
    except Exception as e:  # noqa: BLE001
        check(f"{name} 可调用（实现/签名符合 S1-02）", False, f"{type(e).__name__}: {e}")
        return None


def read_text(path: str) -> str:
    try:
        return io.open(path, encoding="utf-8", errors="replace").read()
    except OSError:
        return ""


def read_text_gbk(path: str) -> str:
    """读 update.cmd（写入端用 GBK：cmd 按本地代码页解析，避免中文路径乱码）。"""
    try:
        return io.open(path, encoding="gbk", errors="replace").read()
    except OSError:
        return ""


def func_source(src: str, name: str) -> str:
    """粗略截取一个方法/函数体（到下一个同缩进 def 或文件尾）。"""
    m = re.search(rf"^(\s*)def {re.escape(name)}\s*\(", src, re.M)
    if not m:
        return ""
    indent = len(m.group(1))
    start = m.start()
    tail = src[m.end():]
    nxt = re.search(rf"^\s{{0,{indent}}}def ", tail, re.M)
    return src[start:] if not nxt else src[start:m.end() + nxt.start()]


def top_level_const(src: str, name: str):
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return None
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == name for t in node.targets):
            try:
                return ast.literal_eval(node.value)
            except Exception:
                return None
    return None


def strip_js_comments(src: str) -> str:
    src = re.sub(r"/\*[\s\S]*?\*/", "", src)
    return re.sub(r"(?m)^[ \t]*//.*$", "", src)


# ---------------------------------------------------------------- 导入被测模块
sys.path.insert(0, LOCAL_TOOL)
updater = None
try:
    import updater as _updater  # noqa: E402
    updater = _updater
except Exception as e:  # noqa: BLE001
    check("可以导入 local_print_tool/updater.py", False, f"{type(e).__name__}: {e}")
else:
    check("可以导入 local_print_tool/updater.py", True)


def api(name: str):
    """安全取被测函数：改前版本没有该函数时记一条 FAIL 并返回 None（**不崩**）。

    反向验证的关键：红必须红在断言上。`getattr` 缺失 → 记 FAIL；后续用 `if fn is None: return`
    跳过，绝不抛 AttributeError。"""
    if updater is None:
        check(f"updater.{name} 存在", False, "updater 导入失败")
        return None
    fn = getattr(updater, name, None)
    if fn is None:
        check(f"updater.{name} 存在（S1-02 新增/改造）", False, "缺失")
    return fn


# ---------------------------------------------------------------- http 桩服务
PAYLOAD = b"HN-FAKE-SETUP-PAYLOAD" * 200          # 5KB，够走完流式下载
OTHER = b"OTHER-BYTES-NOT-THE-SETUP" * 100


class _Stub:
    def __init__(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        stub = self
        self.counts: dict[str, int] = {}
        self.manifest = b"{}"

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, fmt, *args):  # 静音
                pass

            def do_GET(self):
                path = self.path.split("?")[0]
                stub.counts[path] = stub.counts.get(path, 0) + 1
                if path == "/pkg.exe":
                    return self._body(PAYLOAD)
                if path == "/other.exe":
                    return self._body(OTHER)
                if path == "/redirect-ok":
                    return self._redirect(
                        f"http://127.0.0.1:{self.server.server_port}/pkg.exe")
                if path == "/redirect-foreign":
                    return self._redirect(
                        f"http://localhost:{self.server.server_port}/pkg.exe")
                if path == "/redirect-file":
                    return self._redirect("file:///C:/Windows/win.ini")
                if path == "/update.json":
                    return self._body(stub.manifest)
                return self.send_error(404)

            def _body(self, data: bytes):
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _redirect(self, location: str):
                self.send_response(302)
                self.send_header("Location", location)
                self.send_header("Content-Length", "0")
                self.end_headers()

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.srv.server_port
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        try:
            self.srv.shutdown()
            self.srv.server_close()
        except Exception:
            pass

    def url(self, path: str, host: str = "127.0.0.1") -> str:
        return f"http://{host}:{self.port}{path}"


class _Missing:
    pass


_MISSING = _Missing()


class patched:
    """临时改模块全局（测试完无条件还原）。"""

    def __init__(self, module, **attrs):
        self.module = module
        self.attrs = attrs
        self.old: dict[str, object] = {}

    def __enter__(self):
        for k, v in self.attrs.items():
            self.old[k] = getattr(self.module, k, _MISSING)
            setattr(self.module, k, v)
        return self

    def __exit__(self, *exc):
        for k, v in self.old.items():
            if v is _MISSING:
                try:
                    delattr(self.module, k)
                except AttributeError:
                    pass
            else:
                setattr(self.module, k, v)
        return False


_tmpdirs: list[str] = []


def new_tmpdir(prefix: str = "hn-s102-") -> str:
    d = tempfile.mkdtemp(prefix=prefix)
    _tmpdirs.append(d)
    return d


def cleanup_tmpdirs():
    for d in _tmpdirs:
        shutil.rmtree(d, ignore_errors=True)
    _tmpdirs.clear()


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def md5_hex(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def make_key():
    """生成一次性 ed25519 测试密钥 → (private_key, public_key_b64)。"""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    key = Ed25519PrivateKey.generate()
    pub = key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw)
    return key, base64.b64encode(pub).decode("ascii")


def sign_manifest(manifest: dict, key) -> dict:
    canonical = api("canonical_manifest_payload")
    if canonical is None or key is None:
        return dict(manifest)
    payload = canonical(manifest)
    out = dict(manifest)
    out["sig_algo"] = "ed25519"
    out["sig"] = base64.b64encode(key.sign(payload)).decode("ascii")
    return out


def test_allowlist_patch(stub, extra_hosts=("127.0.0.1",)) -> dict:
    """把白名单临时指向本地桩（scheme http + 桩端口），其余语义不变。"""
    return dict(
        ALLOWED_DOWNLOAD_SCHEMES=("http",),
        ALLOWED_DOWNLOAD_HOSTS=tuple(extra_hosts),
        ALLOWED_DOWNLOAD_PORTS=(stub.port,),
    )


# ============================================================================
# A. 静态判据
# ============================================================================
def run_static():
    src = read_text(UPDATER_PY)

    section("A1. host 白名单：只含 hn-space.cn + COS 桶，且只允许 https")
    hosts = top_level_const(src, "ALLOWED_DOWNLOAD_HOSTS")
    check("ALLOWED_DOWNLOAD_HOSTS 存在且是固定元组", isinstance(hosts, tuple), repr(hosts))
    check("白名单恰好两个域（不多不少）",
          isinstance(hosts, tuple) and set(hosts) == {"hn-space.cn", HCOS}, repr(hosts))
    check("白名单含 hn-space.cn", isinstance(hosts, tuple) and "hn-space.cn" in hosts)
    check("白名单含 COS 桶域名", isinstance(hosts, tuple) and HCOS in hosts)
    schemes = top_level_const(src, "ALLOWED_DOWNLOAD_SCHEMES")
    check("ALLOWED_DOWNLOAD_SCHEMES == ('https',)（https-only）", schemes == ("https",),
          repr(schemes))
    ports = top_level_const(src, "ALLOWED_DOWNLOAD_PORTS")
    check("ALLOWED_DOWNLOAD_PORTS == (443,)（不收非默认端口）", ports == (443,), repr(ports))
    check("源码里没有把 %TEMP% 当更新落地目录（os.environ['TEMP'] 类写法为 0）",
          not re.search(r"(environ\.get|getenv)\(\s*[\"']TEMP[\"']", src),
          "更新包必须落 %LOCALAPPDATA%\\HN打印工具\\updates")
    check("私有目录常量指向 LOCALAPPDATA + HN打印工具 + updates",
          "LOCALAPPDATA" in src and "UPDATES_DIR_NAME" in src and '"updates"' in src)

    section("A2. validate_download_url 行为：白名单内放行；外来 host / 非 https 一律拒绝")
    validate = api("validate_download_url")
    if validate is not None:
        for good in (f"https://hn-space.cn/updates/update.json",
                     f"https://{HCOS}/h_n-printer_setup_4.5.33.exe"):
            try:
                validate(good)
                check(f"放行 {good[:56]}…", True)
            except ValueError as e:
                check(f"放行 {good[:56]}…", False, str(e))
        blocked = [
            ("外来 host", "https://evil.example.com/a.exe"),
            ("同形异义域", "https://hn-space.cn.evil.example/a.exe"),
            ("明文 http", "http://hn-space.cn/a.exe"),
            ("本地文件", "file:///C:/Windows/System32/calc.exe"),
            ("带口令的 URL", "https://user:pass@hn-space.cn/a.exe"),
            ("非 443 端口", "https://hn-space.cn:8443/a.exe"),
            ("空地址", ""),
        ]
        for label, url in blocked:
            try:
                validate(url)
                check(f"拒绝{label}: {url[:44]!r}", False, "竟然通过")
            except ValueError:
                check(f"拒绝{label}: {url[:44]!r}", True)

    section("A3. 重定向后二次校验（handler 每次跳转都过白名单 + 最终 geturl 再验）")
    check("存在自定义 HTTPRedirectHandler 子类", "HTTPRedirectHandler" in src)
    check("重写了 redirect_request", "def redirect_request" in src)
    redir_body = src.split("def redirect_request", 1)[-1][:900] if "def redirect_request" in src else ""
    check("redirect_request 内对**新地址**调用 validate_download_url",
          "validate_download_url" in redir_body)
    final_body = func_source(src, "_check_final_url")
    check("跟随结束后对 resp.geturl() 的最终地址再校验一次",
          "geturl()" in final_body and "validate_download_url" in final_body)
    check("白名单 handler 被真正装进 opener", "_AllowlistRedirectHandler" in func_source(src, "_build_opener"))

    section("A4. 哈希字段必填：sha256 优先、md5 兼容；缺失/非法一律拒绝")
    expected_hash = api("expected_hash")
    if expected_hash is not None:
        try:
            expected_hash({})
            check("缺少 sha256/md5 → 抛错（不再只 warning）", False, "竟然通过")
        except Exception:
            check("缺少 sha256/md5 → 抛错（不再只 warning）", True)
        try:
            expected_hash({"sha256": "zz", "md5": md5_hex(PAYLOAD)})
            check("sha256 格式非法 → 拒绝且**不**静默回落 md5", False, "竟然通过")
        except Exception:
            check("sha256 格式非法 → 拒绝且**不**静默回落 md5", True)
        try:
            algo, _ = expected_hash({"sha256": sha256_hex(PAYLOAD), "md5": md5_hex(PAYLOAD)})
            check("sha256 + md5 同时存在 → 优先 sha256", algo == "sha256", f"algo={algo}")
        except Exception as e:
            check("sha256 + md5 同时存在 → 优先 sha256", False, str(e))
        try:
            algo, digest = expected_hash({"md5": md5_hex(PAYLOAD)})
            check("只有 md5 → 兼容（algo=md5 且校验值正确）",
                  algo == "md5" and digest == md5_hex(PAYLOAD), f"{algo}={digest}")
        except Exception as e:
            check("只有 md5 → 兼容（algo=md5 且校验值正确）", False, str(e))

    section("A5. 顺序：下载清单 → 验签 → 下载包 → 校验哈希")
    prep = func_source(src, "prepare_update")
    i_sig = prep.find("validate_manifest")
    i_dl = prep.find("download_setup")
    check("prepare_update 内先 validate_manifest（含验签）后 download_setup",
          i_sig >= 0 and i_dl > i_sig, f"validate_manifest@{i_sig} download_setup@{i_dl}")
    fetch_body = func_source(src, "fetch_update_info")
    check("fetch_update_info 拉到清单后立刻过 validate_manifest",
          "validate_manifest(data)" in fetch_body)
    check("prepare_update 要求用户明确确认（user_confirmed）才继续",
          "user_confirmed" in prep and "if not user_confirmed" in prep)
    check("复用本地安装包也必须哈希匹配（旧实现 expected 为空就复用 = 零校验）",
          "verify_file_hash(final_path" in src)
    check("落盘后对最终文件再复算一次哈希（缩小 TOCTOU）",
          src.count("verify_file_hash(") >= 2)

    section("A6. 签名：ed25519 公钥内置、缺 sig 兼容告警、验签失败硬拒绝")
    pub = top_level_const(src, "UPDATE_PUBLIC_KEY_B64")
    check("内置了 ed25519 公钥（base64）", isinstance(pub, str) and len(pub) > 20,
          f"{str(pub)[:16]}…")
    check("公钥有 32 字节", isinstance(pub, str)
          and len(base64.b64decode(pub + "=" * (-len(pub) % 4))) == 32
          if isinstance(pub, str) else False)
    check("签名算法固定 ed25519", top_level_const(src, "SIG_ALGO") == "ed25519")
    check("兼容期开关 ALLOW_UNSIGNED_MANIFEST 存在且写明解除条件",
          top_level_const(src, "ALLOW_UNSIGNED_MANIFEST") is True
          and "≥ 4.5.33" in src and "TODO(S1-02" in src)
    verify_sig = api("verify_manifest_signature")
    if verify_sig is not None:
        try:
            state = verify_sig({"version": "9.9.9", "url": f"https://hn-space.cn/a.exe"})
            check("缺少 sig → 兼容期返回 SIG_UNSIGNED（告警后按哈希校验）",
                  state == getattr(updater, "SIG_UNSIGNED", "unsigned"), repr(state))
        except Exception as e:
            check("缺少 sig → 兼容期返回 SIG_UNSIGNED（告警后按哈希校验）", False, str(e))
        try:
            verify_sig({"version": "9.9.9", "sig_algo": "rsa", "sig": "AAAA"})
            check("未知签名算法 → 硬拒绝", False, "竟然通过")
        except Exception:
            check("未知签名算法 → 硬拒绝", True)
        try:
            verify_sig({"version": "9.9.9", "sig_algo": "ed25519", "sig": "AAAA"})
            check("sig 存在但验签失败 → 硬拒绝（绝不降级成告警）", False, "竟然通过")
        except Exception:
            check("sig 存在但验签失败 → 硬拒绝（绝不降级成告警）", True)

    section("A7. 关闭「启动即自动装」：启动只提示、默认「稍后」、静默默认拒绝")
    gui = read_text(GUI_PY)
    check('GUI 源码可读', bool(gui), GUI_PY)
    result_fn = func_source(gui, "_on_update_check_result")
    check("启动检查路径写明「启动时不会自动更新」", "启动时不会自动更新" in result_fn)
    check("更新确认框有「稍后」按钮", "稍后" in result_fn)
    check("默认按钮设成稍后（setDefaultButton(btn_later)）",
          "setDefaultButton(btn_later)" in result_fn)
    check("不再用「默认 Yes」的二选一框（QMessageBox.Yes 计数为 0）",
          "QMessageBox.Yes" not in result_fn)
    check("整个 GUI 只有一处发起下载（self._perform_update 调用点 == 1）",
          gui.count("self._perform_update(") == 1,
          f"count={gui.count('self._perform_update(')}")
    check("该调用点位于 manual 分支（先 if manual:）",
          result_fn.find("if manual:") >= 0
          and result_fn.find("if manual:") < result_fn.find("self._perform_update(manifest)"))
    check("下载时传 user_confirmed=True（第二道闸）",
          "user_confirmed=True" in func_source(gui, "_update_download_worker"))
    check("生成 update.cmd 时显式 allow_silent_install=True",
          "allow_silent_install=True" in func_source(gui, "_update_download_worker"))
    check("启动定时器注释写明「只提示」（防回归成自动下载）",
          "启动检查**只提示**" in gui)
    write_cmd = api("write_update_cmd")
    if write_cmd is not None:
        tmp = new_tmpdir()
        try:
            write_cmd(os.path.join(tmp, "setup.exe"), sys.executable,
                      os.path.join(tmp, "update.cmd"))
            check("write_update_cmd 默认（未获用户确认）必须拒绝", False, "竟然生成成功")
        except ValueError:
            check("write_update_cmd 默认（未获用户确认）必须拒绝", True)
        except Exception as e:  # noqa: BLE001 —— 改前版本签名不同：红在断言，不崩
            check("write_update_cmd 默认（未获用户确认）必须拒绝", False,
                  f"{type(e).__name__}: {e}")

        # 真实布局：<root>\ready\setup.exe —— 回滚包应归档到 <root>\previous\
        # 每个变体都用**自己唯一的临时目录** + 显式 cwd（2026-10-23 偶发红的加固点）。
        work = tempfile.mkdtemp(prefix="hn-s102-cmd-")
        _tmpdirs.append(work)
        ok_gbk, diag_gbk, cmd_text = _run_archive_case(
            work, "gbk", "chcp 936 >nul", "gbk", write_cmd)
        if cmd_text:
            check("用户明确确认后才允许写 update.cmd（含 /VERYSILENT）",
                  "/VERYSILENT" in cmd_text)
            check("update.cmd 先固定代码页 chcp 936（GBK 文件在任何控制台下都能正确解析）",
                  "chcp 936" in cmd_text
                  and cmd_text.index("chcp 936") < cmd_text.index("previous"),
                  "缺 chcp 936 时，从 UTF-8 控制台启动会把中文路径解成乱码")
            prev_expected = os.path.join(
                work, "gbk", "HN打印工具", "previous")
            check("★ update.cmd 安装成功后把安装包移进 previous\\（回滚材料），不再直接删",
                  "move /y" in cmd_text and prev_expected in cmd_text,
                  "旧写法 del /q 会让 previous\\ 永远为空")
            check("★ update.cmd 不再直接删安装包（无 `del /q \"<setup>\"`）",
                  f'del /q "{os.path.join(work, "gbk", "HN打印工具", "ready", "h_n-printer_setup_4.5.33.exe")}"'
                  not in cmd_text)
        else:
            check("write_update_cmd 能写出 update.cmd", False, diag_gbk)

        # ① 生产同款：GBK 写的 update.cmd + 强制 chcp 936（中文 Windows 的 ANSI 代码页）
        if _ansi_codepage() == 936:
            check("★ 回滚归档命令在 cmd 里真能执行（GBK + chcp 936，中文路径/引号）",
                  ok_gbk, diag_gbk)
        else:
            info(f"SKIP GBK 变体（系统 ANSI 代码页={_ansi_codepage()}，非 936）: {diag_gbk}")

        # ② 与控制台代码页无关：同一批命令用 UTF-8 写 + chcp 65001 —— 证明引号/中文路径
        #    本身没问题，把"编码/代码页"这个变量彻底从判据里剔除（这就是偶发红的根因变量）。
        ok_utf8, diag_utf8, _ = _run_archive_case(
            work, "utf8", "chcp 65001 >nul", "utf-8", write_cmd)
        check("★ 回滚归档命令在 cmd 里真能执行（UTF-8 + chcp 65001，与继承代码页无关）",
              ok_utf8, diag_utf8)

    section("A8. 依赖与打包：cryptography 进 requirements，spec 能收集")
    req = read_text(REQUIREMENTS).lower()
    check("requirements.txt 含 cryptography", "cryptography" in req)
    spec = read_text(SPEC)
    check("spec 的 hiddenimports 含 cryptography", "'cryptography'" in spec or '"cryptography"' in spec)
    check("spec 的 hiddenimports 含 ed25519 子模块",
          "cryptography.hazmat.primitives.asymmetric.ed25519" in spec)
    check("spec 的 hiddenimports 含 serialization（签名工具/验签共用）",
          "cryptography.hazmat.primitives.serialization" in spec)
    check("spec 的 hiddenimports 含 Rust binding（漏收会 ImportError）",
          "cryptography.hazmat.bindings._rust" in spec)
    try:
        from PyInstaller.utils.hooks import collect_all
        _d, _b, hidden = collect_all("cryptography")
        check("PyInstaller 确实能收集 cryptography（collect_all 覆盖 ed25519）",
              any("ed25519" in h for h in hidden),
              f"hiddenimports={len(hidden)}")
    except Exception as e:  # noqa: BLE001 —— 构建工具不在运行环境时不算失败
        info(f"SKIP collect_all 校验（PyInstaller 不可用: {type(e).__name__}）")

    section("A9. 离线签名工具存在且不泄露私钥")
    tool_src = read_text(SIGN_TOOL)
    check("tools/sign_update_manifest.py 存在", bool(tool_src))
    check("签名工具复用 canonical_manifest_payload（与客户端同一份载荷定义）",
          "canonical_manifest_payload" in tool_src)
    check("支持 --write 写回 sig/sig_algo", "--write" in tool_src and "sig_algo" in tool_src)
    check("私钥只在文件里读，不打印内容（只打印公钥）",
          "private_bytes" in tool_src and "print(pem" not in tool_src)

    section("A10. installer/update.json 自身不自相矛盾（无假签名）")
    try:
        manifest = json.loads(read_text(INSTALLER_JSON))
    except Exception as e:  # noqa: BLE001
        manifest = None
        check("installer/update.json 可解析", False, str(e))
    if isinstance(manifest, dict):
        check("installer/update.json 可解析", True)
        validate = api("validate_download_url")
        if validate is not None:
            try:
                validate(manifest.get("url", ""))
                check("清单 url 在 host 白名单内", True)
            except ValueError as e:
                check("清单 url 在 host 白名单内", False, str(e))
        expected_hash = api("expected_hash")
        if expected_hash is not None:
            try:
                algo, digest = expected_hash(manifest)
                check("清单哈希字段合法", True, f"{algo}={digest[:12]}…")
            except Exception as e:  # noqa: BLE001
                check("清单哈希字段合法", False, str(e))
        if manifest.get("sig"):
            try:
                updater.verify_manifest_signature(manifest)
                check("清单若带 sig 则必须能用内置公钥验过（禁止填假签名）", True)
            except Exception as e:  # noqa: BLE001
                check("清单若带 sig 则必须能用内置公钥验过（禁止填假签名）", False, str(e))
        else:
            info("installer/update.json 当前无 sig：走兼容期（告警 + 哈希校验）；"
                 "发布 4.5.33 后按 §S1-02 用签名工具补签")

    section("A11. APK 侧 www/updater.js：白名单 / 禁 302 / 哈希必填 / versionCode / 关静默装")
    js = read_text(APK_JS)
    code = strip_js_comments(js)
    check("updater.js 可读", bool(js))
    check("APK 地址白名单常量存在且含两个域",
          "UPDATE_ALLOWED_HOSTS" in code
          and "'hn-space.cn'" in js and HCOS in js)
    check("只允许 https", "UPDATE_ALLOWED_SCHEMES" in code and "'https:'" in js)
    check("清单请求禁止重定向（redirect: 'error'）", re.search(r"redirect\s*:\s*'error'", code) is not None)
    check("校验 res.redirected（被重定向就拒绝）", "redirected" in code)
    check("sha256 必填（64 位十六进制正则）", re.search(r"0-9a-fA-F\]\{64\}", js) is not None)
    check("md5 兼容（32 位十六进制正则）", re.search(r"0-9a-fA-F\]\{32\}", js) is not None)
    check("哈希缺失/非法 → 拒绝更新（提示 + return）",
          "缺少有效哈希" in js and "return;" in js)
    check("versionCode 必须为正整数", "Math.floor(remote) !== remote" in code)
    check("versionCode 必须严格大于本机（单调递增，防降级）", "remote <= current" in code)
    check("★ 不再调用原生 downloadAndInstall（应用内静默安装已关闭）",
          not re.search(r"\.downloadAndInstall\s*\(", code))
    check("改为打开下载页（_openUpdateDownloadPage + openUrl/window.open）",
          "_openUpdateDownloadPage" in code and re.search(r"openUrl|window\.open", code) is not None)
    check("更新锁仍走统一入口（D5 第 12 条不回归）", "_setUpdateBusy" in code)


# ============================================================================
# B. 行为判据（本地 http 桩；绝不联网）
# ============================================================================
def run_behavior():
    if updater is None:
        check("行为段可运行（updater 导入成功）", False, "updater 导入失败")
        return

    stub = _Stub().start()
    try:
        _run_behavior_with_stub(stub)
    finally:
        stub.stop()


def _run_behavior_with_stub(stub: _Stub):
    validate = api("validate_download_url")
    validate_manifest = api("validate_manifest")
    download_setup = api("download_setup")
    prepare_update = api("prepare_update")
    fetch_update_info = api("fetch_update_info")
    try:
        key, pub_b64 = make_key()
        check("测试环境装有 cryptography（验签依赖）", True)
    except Exception as e:  # noqa: BLE001
        # 没有 cryptography 时验签功能本身不可用：记 FAIL 并继续，后面的签名用例会跟着红。
        check("测试环境装有 cryptography（验签依赖）", False, f"{type(e).__name__}: {e}")
        key, pub_b64 = None, ""

    section("B1. 外来 host → 拒绝（真实白名单；清单与安装包两条路径）")
    if validate_manifest is not None:
        try:
            validate_manifest({"version": "9.9.9", "url": "https://evil.example.com/a.exe",
                               "sha256": sha256_hex(PAYLOAD)})
            check("清单 url 是外来 host → 拒绝", False, "竟然通过")
        except Exception:
            check("清单 url 是外来 host → 拒绝", True)
        with patched(updater, **test_allowlist_patch(stub, extra_hosts=("127.0.0.1",))):
            try:
                validate_manifest({"version": "9.9.9", "url": stub.url("/pkg.exe", "localhost"),
                                   "sha256": sha256_hex(PAYLOAD)})
                check("白名单只含 127.0.0.1 时，localhost 仍被拒绝（逐 host 比对）", False, "竟然通过")
            except Exception:
                check("白名单只含 127.0.0.1 时，localhost 仍被拒绝（逐 host 比对）", True)
    if download_setup is not None:
        tmp = new_tmpdir()
        got = download_setup(stub.url("/pkg.exe", "localhost"), sha256_hex(PAYLOAD), tmp)
        check("download_setup 对外来 host 立即返回 None", got is None, repr(got))
        check("外来 host 时**一个请求都没发**", not stub.counts.get("/pkg.exe"),
              f"counts={stub.counts}")

    section("B2. 缺哈希 → 拒绝（连请求都不发；旧行为是 warning 后跳过校验）")
    if validate_manifest is not None:
        try:
            validate_manifest({"version": "9.9.9",
                               "url": f"https://hn-space.cn/updates/x.exe"})
            check("清单缺哈希 → validate_manifest 拒绝", False, "竟然通过")
        except Exception:
            check("清单缺哈希 → validate_manifest 拒绝", True)
    if download_setup is not None:
        tmp = new_tmpdir()
        before = stub.counts.get("/pkg.exe", 0)
        with patched(updater, **test_allowlist_patch(stub)):
            got = download_setup(stub.url("/pkg.exe"), "", tmp)
        check("download_setup 缺期望哈希 → None（fail-closed）", got is None, repr(got))
        check("缺哈希时不发请求、不落盘",
              stub.counts.get("/pkg.exe", 0) == before and not _list_exe(tmp),
              f"counts={stub.counts}")

    section("B3. 错哈希 → 拒绝且不留文件")
    if download_setup is not None:
        tmp = new_tmpdir()
        with patched(updater, **test_allowlist_patch(stub)):
            got = guarded("download_setup", download_setup,
                          stub.url("/pkg.exe"), "0" * 64, tmp, algo="sha256")
        check("错误 sha256 → None", got is None, repr(got))
        check("错误哈希的安装包不留在 ready/（半截文件已清）", not _list_exe(tmp), repr(_list_exe(tmp)))
        check("确实下载了一次（说明是校验拦下的，不是没下）",
              stub.counts.get("/pkg.exe", 0) >= 1, f"counts={stub.counts}")

    section("B4. 错签名 → 拒绝，且**一步安装包都不下载**（先验签后下包）")
    calls = {"download": 0}
    real_download = download_setup

    def _spy_download(*a, **kw):
        calls["download"] += 1
        return real_download(*a, **kw) if real_download else None

    if validate_manifest is not None and prepare_update is not None:
        good = {"version": "9.9.9", "url": stub.url("/pkg.exe"),
                "sha256": sha256_hex(PAYLOAD), "notes": "t"}
        signed = sign_manifest(good, key)
        tampered = dict(signed)
        tampered["notes"] = "tampered-after-signing"        # 签名后改内容 → 必须被发现
        with patched(updater, UPDATE_PUBLIC_KEY_B64=pub_b64,
                     **test_allowlist_patch(stub)), \
                patched(updater, download_setup=_spy_download):
            try:
                validate_manifest(tampered)
                check("签名后篡改清单内容 → validate_manifest 拒绝", False, "竟然通过")
            except Exception:
                check("签名后篡改清单内容 → validate_manifest 拒绝", True)
            before = stub.counts.get("/pkg.exe", 0)
            calls["download"] = 0
            got = guarded("prepare_update", prepare_update, tampered, "4.5.32",
                          user_confirmed=True)
            check("prepare_update 对错签名清单返回 None", got is None, repr(got))
            check("★ 错签名时 download_setup 一次都没被调用", calls["download"] == 0,
                  f"calls={calls['download']}")
            check("★ 错签名时安装包请求数为 0（没下载包）",
                  stub.counts.get("/pkg.exe", 0) == before, f"counts={stub.counts}")

        bad_algo = dict(signed)
        bad_algo["sig_algo"] = "rsa"
        with patched(updater, UPDATE_PUBLIC_KEY_B64=pub_b64,
                     **test_allowlist_patch(stub)):
            try:
                validate_manifest(bad_algo)
                check("未知签名算法 → 拒绝", False, "竟然通过")
            except Exception:
                check("未知签名算法 → 拒绝", True)

        # 换一把私钥签 → 与内置公钥不匹配
        other_key, _other_pub = make_key() if key is not None else (None, "")
        wrong_key_signed = sign_manifest(good, other_key)
        with patched(updater, UPDATE_PUBLIC_KEY_B64=pub_b64,
                     **test_allowlist_patch(stub)):
            try:
                validate_manifest(wrong_key_signed)
                check("用别的私钥签 → 验签失败被拒", False, "竟然通过")
            except Exception:
                check("用别的私钥签 → 验签失败被拒", True)

    section("B5. 正确签名 + 正确哈希 → 通过（落私有目录 ready/，并留回滚材料）")
    if prepare_update is not None and download_setup is not None:
        root = new_tmpdir()
        good = {"version": "9.9.9", "url": stub.url("/pkg.exe"),
                "sha256": sha256_hex(PAYLOAD), "notes": "真实路径"}
        signed = sign_manifest(good, key)
        with patched(updater, UPDATE_PUBLIC_KEY_B64=pub_b64,
                     updates_root=lambda: root,
                     **test_allowlist_patch(stub)):
            # ① 未获用户确认 → 不下载（启动检查只提示的代码级保证）
            before = stub.counts.get("/pkg.exe", 0)
            got = guarded("prepare_update", prepare_update, signed, "4.5.32",
                          user_confirmed=False)
            check("user_confirmed=False → None（启动检查不会触发下载）", got is None, repr(got))
            check("user_confirmed=False 时安装包请求数为 0",
                  stub.counts.get("/pkg.exe", 0) == before)
            # ② 明确确认 → 下载 + 校验 + 落盘
            got = guarded("prepare_update", prepare_update, signed, "4.5.32",
                          user_confirmed=True)
            check("★ 正确签名 + 正确哈希 → 准备成功", bool(got), repr(got))
            if got:
                setup_path, exe_path = got
                with open(setup_path, "rb") as f:
                    data = f.read()
                check("落地文件内容与远端一致", data == PAYLOAD, f"{len(data)}B")
                check("落地在私有目录的 ready\\ 下（不是 %TEMP%）",
                      os.path.dirname(setup_path) == os.path.join(root, "ready"),
                      setup_path)
                check("download_setup 用随机子目录下载、随后重命名到位（无 dl-* 残留）",
                      not [n for n in os.listdir(root) if n.startswith("dl-")],
                      repr(os.listdir(root)))
                check("保留上一版目录 previous\\ 已建好",
                      os.path.isdir(os.path.join(root, "previous")))
                check("写了回滚说明 ROLLBACK_README.txt",
                      os.path.isfile(os.path.join(root, "ROLLBACK_README.txt")))
                check("exe_path 指向当前解释器", os.path.abspath(exe_path) == os.path.abspath(sys.executable))

        # ③ 只有 md5 的旧清单也能升级（兼容），且用 md5 校验
        root2 = new_tmpdir()
        md5_manifest = {"version": "9.9.9", "url": stub.url("/pkg.exe"),
                        "md5": md5_hex(PAYLOAD)}
        signed_md5 = sign_manifest(md5_manifest, key)
        with patched(updater, UPDATE_PUBLIC_KEY_B64=pub_b64,
                     updates_root=lambda: root2,
                     **test_allowlist_patch(stub)):
            got = guarded("prepare_update", prepare_update, signed_md5, "4.5.32",
                          user_confirmed=True)
            check("只有 md5 的清单（存量发布脚本）仍可升级", bool(got), repr(got))

        # ④ sha256 与 md5 同时存在但 md5 故意写错 → 仍应通过（证明优先用 sha256）
        root3 = new_tmpdir()
        both = {"version": "9.9.9", "url": stub.url("/pkg.exe"),
                "sha256": sha256_hex(PAYLOAD), "md5": "f" * 32}
        with patched(updater, UPDATE_PUBLIC_KEY_B64=pub_b64,
                     updates_root=lambda: root3,
                     **test_allowlist_patch(stub)):
            got = guarded("prepare_update", prepare_update, sign_manifest(both, key),
                          "4.5.32", user_confirmed=True)
            check("sha256 与 md5 同时存在 → 用 sha256 校验（md5 错也不影响）", bool(got), repr(got))

        # ⑤ 正确签名 + 错哈希 → 拒绝
        root4 = new_tmpdir()
        bad_hash = dict(good)
        bad_hash["sha256"] = "1" * 64
        with patched(updater, UPDATE_PUBLIC_KEY_B64=pub_b64,
                     updates_root=lambda: root4,
                     **test_allowlist_patch(stub)):
            got = guarded("prepare_update", prepare_update, sign_manifest(bad_hash, key),
                          "4.5.32", user_confirmed=True)
            check("正确签名 + 错误哈希 → 拒绝", got is None, repr(got))
            check("错哈希不落盘到 ready/", not _list_exe(os.path.join(root4, "ready")))

        # ⑥ 连着升两版 → 上一版安装包必须归档到 previous\（改动 5 的回滚材料）
        root5 = new_tmpdir()
        first = {"version": "9.9.9", "url": stub.url("/pkg.exe"),
                 "sha256": sha256_hex(PAYLOAD)}
        second = {"version": "9.9.10", "url": stub.url("/other.exe"),
                  "sha256": sha256_hex(OTHER)}
        with patched(updater, UPDATE_PUBLIC_KEY_B64=pub_b64,
                     updates_root=lambda: root5,
                     **test_allowlist_patch(stub)):
            got1 = guarded("prepare_update", prepare_update, sign_manifest(first, key),
                           "9.9.9", user_confirmed=True)
            got2 = guarded("prepare_update", prepare_update, sign_manifest(second, key),
                           "9.9.9", user_confirmed=True)
        check("连续两次升级都成功", bool(got1) and bool(got2), f"{got1} / {got2}")
        prev_dir = os.path.join(root5, "previous")
        check("★ 新版本落地后，上一版安装包归档到 previous\\（可手动回滚）",
              os.path.isfile(os.path.join(prev_dir, "pkg.exe")),
              repr(_dir_entries(prev_dir)))
        ready_now = sorted(n for n in _dir_entries(os.path.join(root5, "ready"))
                           if n.lower().endswith(".exe"))
        check("ready\\ 只留最新这一份安装包（不堆积）", ready_now == ["other.exe"],
              repr(ready_now))

    section("B6. 重定向：跨 host / 到 file:// 一律拒绝；同 host 正常重定向放行")
    if download_setup is not None:
        with patched(updater, **test_allowlist_patch(stub, extra_hosts=("127.0.0.1",))):
            tmp = new_tmpdir()
            got = download_setup(stub.url("/redirect-foreign"), sha256_hex(PAYLOAD), tmp)
            check("302 跨 host（127.0.0.1 → localhost）→ 拒绝", got is None, repr(got))
            tmp2 = new_tmpdir()
            got = download_setup(stub.url("/redirect-file"), sha256_hex(PAYLOAD), tmp2)
            check("302 到 file:// → 拒绝", got is None, repr(got))
            tmp3 = new_tmpdir()
            got = download_setup(stub.url("/redirect-ok"), sha256_hex(PAYLOAD), tmp3)
            check("同 host 重定向（白名单内）正常放行", bool(got), repr(got))
            if got:
                with open(got, "rb") as f:
                    check("同 host 重定向后内容与哈希一致", f.read() == PAYLOAD)

    section("B7. fetch_update_info 端到端（清单拉取 → 验签 → 哈希必填）")
    if fetch_update_info is not None and validate_manifest is not None:
        good = {"version": "9.9.9", "url": stub.url("/pkg.exe"),
                "sha256": sha256_hex(PAYLOAD), "notes": "e2e"}
        patch = dict(test_allowlist_patch(stub),
                     UPDATE_MANIFEST_URL=stub.url("/update.json"))
        # 未签名：兼容期应放行（告警 + 按哈希校验）
        stub.manifest = json.dumps(good).encode("utf-8")
        with patched(updater, **patch):
            got = fetch_update_info(timeout=5)
        check("未签名清单 → 兼容期仍接受（不打断存量升级链路）",
              isinstance(got, dict) and got.get("version") == "9.9.9", repr(got)[:200])
        # 错签名：必须拒绝且给出原因
        # 错签名：必须拒绝且给出原因（签名后改内容）
        stub.manifest = json.dumps({**sign_manifest(good, key), "notes": "tampered"}).encode("utf-8")
        with patched(updater, UPDATE_PUBLIC_KEY_B64=pub_b64, **patch):
            got = fetch_update_info(timeout=5)
        check("错签名清单 → fetch_update_info 返回 None（拒绝）", got is None, repr(got))
        check("拒绝原因可读（GUI 会显示）",
              bool(updater.last_reject_reason()) if hasattr(updater, "last_reject_reason") else False,
              getattr(updater, "last_reject_reason", lambda: "")())
        # 外来 host 的清单：拒绝（scheme 同时放开，确保拦在 host 上）
        evil = dict(good)
        evil["url"] = "https://evil.example.com/a.exe"
        stub.manifest = json.dumps(evil).encode("utf-8")
        with patched(updater, **dict(patch, ALLOWED_DOWNLOAD_SCHEMES=("http", "https"))):
            got = fetch_update_info(timeout=5)
        check("清单 url 是外来 host → fetch_update_info 返回 None", got is None, repr(got))
        check("外来 host 的拒绝原因指向白名单（不是笼统的网络错误）",
              "白名单" in (updater.last_reject_reason()
                          if hasattr(updater, "last_reject_reason") else ""),
              getattr(updater, "last_reject_reason", lambda: "")())
        # 缺哈希的清单：拒绝
        no_hash = {"version": "9.9.9", "url": stub.url("/pkg.exe")}
        stub.manifest = json.dumps(no_hash).encode("utf-8")
        with patched(updater, **patch):
            got = fetch_update_info(timeout=5)
        check("清单缺哈希 → fetch_update_info 返回 None", got is None, repr(got))
        # 正确签名 → 通过
        stub.manifest = json.dumps(sign_manifest(good, key)).encode("utf-8")
        with patched(updater, UPDATE_PUBLIC_KEY_B64=pub_b64, **patch):
            got = fetch_update_info(timeout=5)
        check("★ 正确签名清单 → fetch_update_info 通过", isinstance(got, dict), repr(got)[:120])

    section("B8. 签名工具端到端（生成密钥 → 签临时清单 → 客户端公钥验过）")
    tmp = new_tmpdir()
    key_path = os.path.join(tmp, "update_signing_key.pem")
    manifest_path = os.path.join(tmp, "update.json")
    payload_manifest = {"version": "9.9.9", "url": f"https://hn-space.cn/updates/x.exe",
                        "sha256": sha256_hex(PAYLOAD), "notes": "工具自测"}
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(payload_manifest, f, ensure_ascii=False, indent=2)
    gen = subprocess.run([sys.executable, SIGN_TOOL, "--gen-key", key_path],
                         capture_output=True)
    gen_out = (gen.stdout or b"").decode("utf-8", "replace")
    check("签名工具能生成 ed25519 密钥对", gen.returncode == 0 and os.path.isfile(key_path),
          f"rc={gen.returncode}")
    pub_match = re.search(r'UPDATE_PUBLIC_KEY_B64 = "([^"]+)"', gen_out)
    tool_pub = pub_match.group(1) if pub_match else ""
    check("生成的公钥可被解析（base64 32 字节）",
          bool(tool_pub) and len(base64.b64decode(tool_pub)) == 32)
    sign_res = subprocess.run([sys.executable, SIGN_TOOL, manifest_path,
                               "--key", key_path, "--write"], capture_output=True)
    check("签名工具 --write 成功（rc=0）", sign_res.returncode == 0,
          " | ".join(((sign_res.stderr or b"").decode("utf-8", "replace").splitlines()
                      or (sign_res.stdout or b"").decode("utf-8", "replace").splitlines())[:2])[:300])
    signed = json.loads(read_text(manifest_path))
    check("清单已带上 sig + sig_algo",
          bool(signed.get("sig")) and signed.get("sig_algo") == "ed25519")
    try:
        state = updater.verify_manifest_signature(signed, public_key_b64=tool_pub)
        check("★ 客户端用同一份载荷定义能验过该签名",
              state == getattr(updater, "SIG_OK", "ok"), repr(state))
    except Exception as e:  # noqa: BLE001
        check("★ 客户端用同一份载荷定义能验过该签名", False, f"{type(e).__name__}: {e}")
    try:
        tampered = dict(signed)
        tampered["version"] = "9.9.10"
        updater.verify_manifest_signature(tampered, public_key_b64=tool_pub)
        check("签名工具签过的清单被改一个字节 → 验签失败", False, "竟然通过")
    except Exception:
        check("签名工具签过的清单被改一个字节 → 验签失败", True)
    check_res = subprocess.run([sys.executable, SIGN_TOOL, manifest_path, "--check"],
                               capture_output=True)
    # --check 用内置公钥校验：工具生成的是一次性密钥，与内置公钥不同 → 预期失败（rc=1），
    # 但必须给出明确原因而不是崩掉。
    check("--check 对「非内置公钥签的清单」如实报失败（不谎报通过）",
          check_res.returncode == 1,
          f"rc={check_res.returncode}")
    check("--check 的失败原因可读（不是崩在异常里）",
          "验签失败" in (check_res.stderr or b"").decode("utf-8", "replace")
          or "验签失败" in (check_res.stdout or b"").decode("utf-8", "replace"),
          (check_res.stderr or b"").decode("utf-8", "replace")[:200])


def _list_exe(root: str) -> list[str]:
    out = []
    for dirpath, _dirs, files in os.walk(root):
        out.extend(os.path.join(dirpath, f) for f in files if f.lower().endswith(".exe"))
    return out


def _dir_entries(path: str) -> list[str]:
    """目录不存在时返回 []（反向验证跑改前代码时目录结构可能完全不同，不能崩）。"""
    try:
        return sorted(os.listdir(path))
    except OSError:
        return []


def _decode_console(data: bytes) -> str:
    """解码 cmd 子进程输出：优先 mbcs（跟随当前 ANSI/控制台代码页），再 gbk / utf-8。"""
    if not data:
        return ""
    for enc in ("mbcs", "gbk", "utf-8"):
        try:
            return data.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    return data.decode("utf-8", "replace")


def _ansi_codepage() -> int:
    """系统 ANSI 代码页（中文 Windows=936）。取不到返回 0。"""
    if os.name != "nt":
        return 0
    try:
        import ctypes
        return int(ctypes.windll.kernel32.GetACP())
    except Exception:  # noqa: BLE001
        return 0


def _run_bat(bat_path: str, cwd: str, attempts: int = 3):
    """跑一个 .bat，返回 (proc, 实际尝试次数)。瞬时失败（文件被 AV/索引器占用）自动重试。

    为什么必须自己固定代码页（2026-10-23 偶发红根因）：
      `cmd` 子进程**继承控制台代码页**。本仓库的 update.cmd 用 GBK 写（含中文路径，
      见 updater.write_update_cmd），如果它被从 chcp 65001 的终端/控制台启动，GBK 字节
      会被按 UTF-8 解码 → 路径变乱码 →「系统找不到指定的文件」。
      全量套件里它时绿时红，正是"外层控制台 CP 不同"造成的。所以测试 bat 第一行固定
      代码页（GBK 版 chcp 936 / UTF-8 版 chcp 65001），不依赖继承来的 CP。
    """
    last = None
    n = max(1, attempts)
    for i in range(n):
        last = subprocess.run(["cmd", "/c", bat_path], cwd=cwd, capture_output=True)
        if last.returncode == 0:
            return last, i + 1
        time.sleep(0.15)  # 给 AV/索引器松句柄的时间
    return last, n


def _run_archive_case(work: str, label: str, chcp_line: str, encoding: str,
                      write_cmd) -> tuple[bool, str, str]:
    """在**独立唯一临时目录**里真跑 update.cmd 的归档三行（mkdir / del / move）。

    返回 (ok, 诊断文本, 生成出来的 update.cmd 文本)。完全显式指定 cwd、bat 路径、
    文件路径，不依赖进程 cwd / %TEMP% 里既有内容 / 继承的控制台代码页。
    """
    base = os.path.join(work, label)
    root = os.path.join(base, "HN打印工具")
    ready = os.path.join(root, "ready")
    previous = os.path.join(root, "previous")
    try:
        shutil.rmtree(base, ignore_errors=True)
        os.makedirs(ready)
    except OSError as e:
        return False, f"准备临时目录失败: {type(e).__name__}: {e}", ""
    setup = os.path.join(ready, "h_n-printer_setup_4.5.33.exe")
    with open(setup, "wb") as f:
        f.write(b"fake setup payload" * 64)      # 非空，避免"0 字节文件"被特殊处理
    cmd_path = os.path.join(ready, "update.cmd")
    if write_cmd is None:
        return False, "write_update_cmd 缺失（S1-02 未实现）", ""
    ok_gen = guarded("write_update_cmd", write_cmd, setup, sys.executable, cmd_path,
                     allow_silent_install=True)
    cmd_text = read_text_gbk(cmd_path)
    if not ok_gen or not cmd_text:
        return False, f"write_update_cmd 未写出 cmd（ok={ok_gen}）", cmd_text
    move_lines = [ln for ln in cmd_text.splitlines()
                  if "previous" in ln and ("mkdir" in ln or "move /y" in ln or "del /q" in ln)]
    if len(move_lines) != 3:
        return False, f"归档命令行数不对（{len(move_lines)}，期望 3）: {move_lines}", cmd_text
    bat = os.path.join(base, "archive_only.bat")
    with io.open(bat, "w", encoding=encoding, errors="replace", newline="") as f:
        f.write("\r\n".join(["@echo off", chcp_line] + move_lines) + "\r\n")
    proc, tries = _run_bat(bat, root)
    moved = os.path.isfile(os.path.join(previous, os.path.basename(setup)))
    gone = not os.path.isfile(setup)
    ok = proc.returncode == 0 and moved and gone
    diag = (f"变体={label} cwd={root} enc={encoding} chcp={chcp_line!r} "
            f"rc={proc.returncode} 尝试={tries} 归档={moved} 源已移走={gone}; "
            f"ANSI_CP={_ansi_codepage()}; "
            f"stdout={_decode_console(proc.stdout).strip()[:180]!r}; "
            f"stderr={_decode_console(proc.stderr).strip()[:180]!r}; "
            f"previous={_dir_entries(previous)}; ready={_dir_entries(ready)}")
    if not ok:
        # 失败诊断（Lead 要求"把 cmd 的 stdout/stderr 原文打进详情"）：
        # 生产命令里带 `>nul` / `>nul 2>nul`，move/mkdir 的报错可能正好被吃掉 ——
        # 这里把重定向去掉再跑一次，专门取原文。只做诊断，不参与判定。
        try:
            _reset_case_fixture(previous, setup)
            plain = [ln.replace(">nul 2>nul", "").replace(">nul", "") for ln in move_lines]
            bat2 = os.path.join(base, "archive_diag.bat")
            with io.open(bat2, "w", encoding=encoding, errors="replace", newline="") as f:
                f.write("\r\n".join(["@echo off", chcp_line] + plain) + "\r\n")
            p2, _n2 = _run_bat(bat2, root, attempts=1)
            diag += (f" | 诊断(去重定向) rc={p2.returncode} "
                     f"stdout={_decode_console(p2.stdout).strip()[:300]!r} "
                     f"stderr={_decode_console(p2.stderr).strip()[:300]!r}")
        except Exception as e:  # noqa: BLE001 —— 诊断本身绝不能把测试搞崩
            diag += f" | 诊断失败: {type(e).__name__}: {e}"
    return ok, diag, cmd_text


def _reset_case_fixture(previous: str, setup: str) -> None:
    """重置归档用例的现场：删掉 previous\\、重建待归档的 setup 文件。"""
    shutil.rmtree(previous, ignore_errors=True)
    try:
        os.remove(setup)
    except OSError:
        pass
    with open(setup, "wb") as f:
        f.write(b"fake setup payload" * 64)


# ============================================================================
# C. 反向验证
# ============================================================================
# gui.py 刻意**不在**覆盖清单里：另一位队友正在并发编辑它（COM 安全 + 客户端韧性），
# 覆盖/还原会丢掉他未提交的改动。gui 侧的静态判据（启动只提示 / 默认「稍后」 /
# /VERYSILENT 需确认）仍会跑，只是不参与"改前版本必须红"的证明 —— 其余 5 个文件
# 已覆盖本任务全部核心改动（白名单 / 哈希必填 / 验签 / 私有目录 / APK JS / 依赖 / spec）。
REVERSE_FILES = [UPDATER_PY, REQUIREMENTS, SPEC, INSTALLER_JSON, APK_JS]
REVERSE_SKIPPED = [GUI_PY]


def run_reverse() -> int:
    print("=== 反向验证：用 git show %s:<path> 取改前版本覆盖源文件，跑同一脚本 ===\n"
          % BASE_COMMIT)
    backups: dict[str, tuple[bytes, str]] = {}
    restored_ok: list[bool] = []
    try:
        for path in REVERSE_FILES:
            rel = os.path.relpath(path, REPO).replace("\\", "/")
            old = subprocess.run(["git", "show", f"{BASE_COMMIT}:{rel}"],
                                 cwd=REPO, capture_output=True)
            if old.returncode != 0:
                info(f"SKIP（git 取不到改前版本）: {rel}")
                continue
            try:
                cur = io.open(path, "rb").read()
            except OSError as e:
                info(f"SKIP（读不到当前文件）: {rel}: {e}")
                continue
            backups[path] = (cur, hashlib.md5(cur).hexdigest())
            io.open(path, "wb").write(old.stdout)
            info(f"已覆盖为改前版本: {rel}")
        for path in REVERSE_SKIPPED:
            info(f"SKIP（共享编辑中，避免覆盖队友改动）: {os.path.relpath(path, REPO)}")

        cmd = [sys.executable, os.path.abspath(__file__), "--positive-only"]
        env = dict(os.environ)
        env["HN_VERIFY_REVERSE_CHILD"] = "1"
        proc = subprocess.run(cmd, cwd=REPO, capture_output=True, env=env)
        out = (proc.stdout or b"").decode("utf-8", "replace")
        err = (proc.stderr or b"").decode("utf-8", "replace")
        tail = "\n".join(out.splitlines()[-40:])
        print("\n---- 改前版本跑同一脚本的输出（末尾 40 行）----")
        print(tail)
        if err.strip():
            print("---- stderr ----")
            print(err[-2000:])
        print("-----------------------------------------------\n")

        red = len(re.findall(r"(?m)^\[FAIL\]", out))
        # "崩溃"只认**子进程的 stderr**（未捕获异常的出口）+ 缺总结行。
        # 不能拿 stdout 全文搜 Traceback：某些断言的 detail 里会引用被捕获的异常文本
        # （例如签名工具子进程的报错），那属于"红在断言"，不是脚本崩了。
        summary_seen = ("S1-02 更新链加固验收：全部通过" in out
                        or "S1-02 验收：存在失败项" in out)
        crash = re.search(
            r"Traceback \(most recent call last\)|NameError|AttributeError|"
            r"ModuleNotFoundError|ImportError|SyntaxError", err)
        check("反向验证：改前版本必须 exit != 0", proc.returncode != 0,
              f"rc={proc.returncode}")
        check("反向验证：红在断言上（有 [FAIL] 行）", red > 0, f"[FAIL] 行数={red}")
        check("反向验证：不是崩溃（无 Traceback/NameError/AttributeError/ImportError）",
              crash is None and summary_seen,
              (crash.group(0) if crash else "") + ("" if summary_seen else " 缺总结行（疑似中途崩溃）"))
        check("反向验证：确实取到了改前版本（覆盖了 5 个文件）",
              len(backups) == len(REVERSE_FILES), f"覆盖 {len(backups)}/{len(REVERSE_FILES)}")
    finally:
        print()
        for path, (data, before_md5) in backups.items():
            try:
                with open(path, "wb") as f:
                    f.write(data)
                after_md5 = hashlib.md5(io.open(path, "rb").read()).hexdigest()
            except OSError as e:  # noqa: BLE001
                after_md5 = f"<restore failed: {e}>"
            ok = after_md5 == before_md5
            restored_ok.append(ok)
            check(f"finally 还原并核对 md5: {os.path.relpath(path, REPO)}", ok,
                  f"{before_md5} -> {after_md5}")
        status = subprocess.run(["git", "status", "--porcelain", "--"]
                                + [os.path.relpath(p, REPO) for p in backups],
                                cwd=REPO, capture_output=True)
        changed = [ln for ln in (status.stdout or b"").decode("utf-8", "replace").splitlines()
                   if ln.strip()]
        info(f"还原后 git status（这些文件应为空）: {changed or '干净'}")

    print("\n" + ("反向验证通过（改前版本确实红在断言上，且源文件已还原）"
                  if not failures else "反向验证存在失败项"))
    return 0 if not failures else 1


# ============================================================================
# main
# ============================================================================
def run_positive() -> int:
    run_static()
    run_behavior()
    cleanup_tmpdirs()
    print("\n" + ("S1-02 更新链加固验收：全部通过 ✅"
                  if not failures else f"S1-02 验收：存在失败项 ❌ ({len(failures)})"))
    if failures:
        for name in failures:
            print(f"   · {name}")
    return 0 if not failures else 1


def main(argv: list[str]) -> int:
    args = set(argv)
    if "--reverse" in args:
        return run_reverse()
    return run_positive()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
