# -*- coding: utf-8 -*-
"""验收 · S2-07 依赖锁定与 CVE 治理

依据：docs/安全加固修复计划_2026-10-23.md §S2-07（DS-17）。

现场形状（修复前）：`requirements.txt` 只有 10 行**不带版本**的包名 ——
`pip install -r` 每次装到的可能是任意新版本（含被投毒/刚爆 CVE 的版本），
且 `eventlet` 是历史遗留（实时通道早就改成 threading + gthread），留着只是风险面。

本脚本：
  A. 静态：每一行都用 `==` 固定；**eventlet 永久禁用**；gevent 只允许作为
     `HN_RUNTIME=gevent` 开关背后的可选运行时（S3-01a），且必须真有开关接线；
     app.py 里出现的第三方顶层 import 全部有对应的固定版本依赖；gunicorn 在列
     （systemd 就是靠它起服务的）。
  B. 行为（纯本地）：用同一份检查函数喂"未锁版本 / 少依赖 / 混入 eventlet"三种坏清单，
     必须逐条报出问题（证明判据不是恒真）。
  C. 变异自证：把 requirements.txt 的一行改回不带版本的写法 ⇒ 同一函数必须报错。

用法：python tests/verify_dependencies.py     （exit 0 = 全部通过）
"""
import io
import os
import re
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _sec_harness as H  # noqa: E402

H.install_cleanup_hook()
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(REPO, "mobile_apps", "printer-backend")
REQ = os.path.join(BACKEND, "requirements.txt")
req_text = open(REQ, encoding="utf-8").read()
app_src = open(os.path.join(BACKEND, "app.py"), encoding="utf-8").read()

# app.py 的第三方顶层 import → 发行包名（只列直接依赖；传递依赖不在此表）
IMPORT_PACKAGES = {
    "flask": "flask",
    "flask_cors": "flask-cors",
    "flask_socketio": "flask-socketio",
    "apscheduler": "APScheduler",
    "requests": "requests",
    "socketio": "python-socketio",
    "itsdangerous": "itsdangerous",
    "werkzeug": "werkzeug",
    "PIL": "Pillow",
    "pypdf": "pypdf",
}
# 允许"用到但由别的包带入"的（Werkzeug/itsdangerous 是 Flask 的传递依赖，
# 但 app.py 直接 import 了它们 ⇒ 必须显式固定，避免 Flask 升级时被动换版本）
# eventlet 永久禁用（S2-07 已删；2026-09 那次 eventlet×threading 混用的现场证据见
# gunicorn_config.py）。gevent 允许，但**只能是开关背后的可选运行时**（S3-01a）：
# 装了却没接 `HN_RUNTIME` 就等于默认启用了一个不同的事件模型，必须报错。
FORBIDDEN = ("eventlet",)


def _parse_requirements(text):
    """返回 (pins: dict[name_lower] = version, problems: list[str])。"""
    pins, problems = {}, []
    for ln, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("-"):
            continue                      # -r/-e 之类交给调用方
        m = re.match(r"^([A-Za-z0-9_.\-]+)\s*(.*)$", line)
        if not m:
            problems.append(f"第 {ln} 行无法解析：{raw!r}")
            continue
        name, rest = m.group(1), m.group(2).strip()
        if "==" not in rest:
            problems.append(f"{name} 没有锁版本（{raw!r}）")
            continue
        version = rest.split("==", 1)[1].strip()
        if not re.match(r"^[0-9][0-9A-Za-z.\-+]*$", version):
            problems.append(f"{name} 版本号不合法（{version!r}）")
            continue
        pins[name.lower()] = version
    return pins, problems


def _check(text, app_source):
    pins, problems = _parse_requirements(text)
    for bad in FORBIDDEN:
        if bad in pins:
            problems.append(f"依赖里仍有已停用的并发后端：{bad}")
    if "gevent" in pins and 'os.environ.get("HN_RUNTIME"' not in app_source:
        problems.append("装了 gevent 但 app.py 没有 HN_RUNTIME 开关（等于默认换事件模型）")
    for mod, pkg in IMPORT_PACKAGES.items():
        used = re.search(r"^\s*(?:import\s+%s\b|from\s+%s\b)" % (re.escape(mod), re.escape(mod)),
                         app_source, re.M) is not None
        if used and pkg.lower() not in pins:
            problems.append(f"app.py 用了 {mod}，但 requirements 里没有固定版本的 {pkg}")
    if "gunicorn" not in pins:
        problems.append("缺少 gunicorn（systemd 用它起服务）")
    return pins, problems


# ============================================================
# A. 静态
# ============================================================
print("=== A. 静态（真实 requirements.txt）===")
pins, problems = _check(req_text, app_src)
H.chk("★ 每一行都锁死了版本（pip install 可复现）", not problems, problems[:5])
H.chk("★ 没有 eventlet；gevent 仅作为 HN_RUNTIME 开关背后的可选运行时",
      all(b not in pins for b in FORBIDDEN)
      and ("gevent" not in pins or 'os.environ.get("HN_RUNTIME"' in app_src),
      sorted(pins))
H.chk("app.py 用到的第三方包都在列（含 Flask 的传递依赖 Werkzeug/itsdangerous）",
      all(p.lower() in pins for p in ("flask", "flask-cors", "flask-socketio", "apscheduler",
                                      "requests", "python-socketio", "pypdf", "pillow",
                                      "werkzeug", "itsdangerous")), sorted(pins))
H.chk("gunicorn 在列且锁版本", "gunicorn" in pins, pins.get("gunicorn"))
H.chk("版本与线上 venv 对齐的说明写在文件里（生产实测：Flask 3.1.3 / gunicorn 22.0.0）",
      "线上 venv" in req_text and pins.get("flask") == "3.1.3")

# ============================================================
# B. 判据的判别力（坏清单必须被抓住）
# ============================================================
print("\n=== B. 判据判别力（喂坏清单）===")
bad1 = "flask\nflask-cors==6.0.5\ngunicorn==21.2.0\n"
_, p1 = _check(bad1, app_src)
H.chk("未锁版本 ⇒ 被报出", any("没有锁版本" in x for x in p1), p1)

bad2 = re.sub(r"^gunicorn==[^\n]*\n", "", req_text, flags=re.M)
_, p2 = _check(bad2, app_src)
H.chk("删掉 gunicorn ⇒ 被报出", any("gunicorn" in x for x in p2), p2)

bad3 = req_text + "eventlet==0.41.0\n"
_, p3 = _check(bad3, app_src)
H.chk("混入 eventlet ⇒ 被报出", any("eventlet" in x for x in p3), p3)

# S3-01a：gevent 本身允许（可选运行时），但**必须真有 HN_RUNTIME 开关**；
# 把开关拿掉后仍带着 gevent ⇒ 等于默认换了事件模型，必须被报出。
bad4b = req_text + "gevent==26.9.0\n"
_, p4b = _check(bad4b, app_src.replace('os.environ.get("HN_RUNTIME"',
                                       'os.environ.get("X_RUNTIME"'))
H.chk("装了 gevent 但没有 HN_RUNTIME 开关 ⇒ 被报出",
      any("HN_RUNTIME" in x for x in p4b), p4b)


bad4 = re.sub(r"^pypdf==[^\n]*\n", "", req_text, flags=re.M)
_, p4 = _check(bad4, app_src)
H.chk("删掉 app.py 真用到的包（pypdf）⇒ 被报出", any("pypdf" in x for x in p4), p4)

# ============================================================
# C. 变异自证：把一行改回不锁版本
# ============================================================
print("\n=== C. 变异自证 ===")
mut = req_text.replace("flask==3.1.3", "flask")
_, pm = _check(mut, app_src)
H.chk("★ 变异（flask 改回不带版本）⇒ 判据转红",
      any("flask" in x and "没有锁版本" in x for x in pm), pm)

# pip-audit（可选：装了就跑，联网失败按 SKIP 处理，不当失败）
_proc = None
try:
    import importlib.util
    if importlib.util.find_spec("pip_audit") is not None:
        import subprocess
        _proc = subprocess.run([sys.executable, "-m", "pip_audit", "-r", REQ, "--format", "columns"],
                               capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=180)
        H.chk("pip-audit 无高危漏洞", _proc.returncode == 0,
              ((_proc.stdout or "")[-600:] + ((" " + _proc.stderr[-300:]) if _proc.stderr else "")))
    else:
        print("SKIP: 本机未安装 pip-audit（发布前请在联网机器上跑一次）")
except Exception as e:                      # noqa: BLE001
    print("SKIP: pip-audit 运行失败（%s）" % type(e).__name__)

H.finish()
