# -*- coding: utf-8 -*-
"""安全加固批次（docs/安全加固修复计划_2026-10-23.md）验收脚本共用的后端夹具。

- 文件名以下划线开头 ⇒ `tests/run_all.py` 的 `discover()` 不会把它当脚本执行（同
  `_pick_js_rounding.py` 的约定）。只被 `verify_sio_gate.py` 等脚本 import。
- 提供三件事：
  ① `make_copy()` / `import_backend()`：把 `mobile_apps/printer-backend` 复制到临时目录再导入，
     绝不碰仓库里的 `orders.db` / `users.db` / `uploads/`；
  ② `mutated_copy()`：复制一份并把源码改回**修复前形态**（变异自证用）；
  ③ `run_probe()`：在子进程里导入某个副本并执行探针代码 —— 变异自证必须换进程，
     否则同一进程里 `import app` 只会命中已缓存的模块。
"""
import io
import os
import shutil
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(REPO, "mobile_apps", "printer-backend")
APP_PY = os.path.join(SRC, "app.py")
PY = sys.executable

IGNORE = shutil.ignore_patterns(
    "__pycache__", "config.py", "config.py.example", "orders.db", "users.db",
    "uploads", "logs", "*.db-wal", "*.db-shm", "*.bak*")

_created = []
_bad = [0]


# ---------------------------------------------------------------- 断言
def chk(name, cond, detail=""):
    """记一条判定。失败不中断（最后统一 exit 1），避免反向验证红在 AssertionError 上。"""
    if not cond:
        _bad[0] += 1
    print("[%s] %s%s" % ("PASS" if cond else "FAIL", name,
                         ("  -- " + str(detail)) if (not cond and detail) else ""))
    return cond


def failed():
    return _bad[0]


def finish():
    cleanup()
    print("\n%s（%d 条失败）" % ("❌ 有失败" if _bad[0] else "✅ 全部通过", _bad[0]))
    sys.exit(1 if _bad[0] else 0)


# ---------------------------------------------------------------- 临时副本
def make_copy(prefix="hn_sec_"):
    """复制后端源码到临时目录。返回 (tmp_root, dst)。"""
    tmp = tempfile.mkdtemp(prefix=prefix)
    dst = os.path.join(tmp, "printer-backend")
    shutil.copytree(SRC, dst, ignore=IGNORE)
    os.makedirs(os.path.join(dst, "uploads", "avatars"), exist_ok=True)
    _created.append(tmp)
    return tmp, dst


def import_backend(dst):
    """在**当前进程**导入临时副本的 app 模块，并把 cwd 切到副本目录。

    ⚠ 同一进程只能导入一次（模块缓存）。需要多份互不影响的副本时用 `run_probe()`。
    """
    os.chdir(dst)
    os.environ["FLASK_ENV"] = "development"
    if dst not in sys.path:
        sys.path.insert(0, dst)
    import app as backend  # noqa: PLC0415  （延迟导入是刻意的）
    return backend


def mutated_copy(replacements, prefix="hn_sec_mut_"):
    """复制一份后端源码，并逐条做**字面替换**（每条必须命中 1 次，否则抛错）。

    用来把修复"改回去"，证明判据能红。
    """
    tmp, dst = make_copy(prefix)
    path = os.path.join(dst, "app.py")
    with open(path, "r", encoding="utf-8", newline="") as f:
        src = f.read()
    nl = "\r\n" if "\r\n" in src else "\n"
    for old, new in replacements:
        o = old.replace("\n", nl)
        if src.count(o) < 1:
            raise AssertionError("变异锚点未命中：%r" % old[:120])
        src = src.replace(o, new.replace("\n", nl))
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(src)
    return tmp, dst


# ---------------------------------------------------------------- 子进程探针
_PROBE_HEAD = """# -*- coding: utf-8 -*-
import io, json, os, sys
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)
sys.path.insert(0, os.getcwd())
os.environ["FLASK_ENV"] = "development"
import app as backend
backend.init_db()
backend.PRINTER_TOKEN = "probe-tok"
http = backend.app.test_client()
"""


def run_probe(dst, body, timeout=180):
    """在子进程里以 cwd=dst 导入该副本的 app 模块，执行 body（可用 backend / http）。

    返回 CompletedProcess；body 负责把结论打成一行 `RESULT=...`。
    """
    env = dict(os.environ)
    env["PYTHONPATH"] = ""
    env["PYTHONUTF8"] = "1"
    env["FLASK_ENV"] = "development"
    p = subprocess.run([PY, "-c", _PROBE_HEAD + body], cwd=dst, env=env,
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=timeout)
    return p


def probe_value(proc, key="RESULT"):
    """从探针 stdout 里取最后一个 `KEY=值`。"""
    val = None
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if line.startswith(key + "="):
            val = line[len(key) + 1:]
    return val


# ---------------------------------------------------------------- 收尾
def cleanup():
    try:
        os.chdir(REPO)
    except Exception:
        pass
    for t in list(_created):
        shutil.rmtree(t, ignore_errors=True)
    _created.clear()


def read_app_source():
    with open(APP_PY, "r", encoding="utf-8", newline="") as f:
        return f.read()


def strip_comments(text):
    """整行注释置空：判据必须吃代码不吃注释（注释里天然会引用被考的片段）。"""
    return "\n".join("" if ln.lstrip().startswith("#") else ln for ln in text.splitlines())


def _sig_interrupt(signum, frame):  # pragma: no cover
    cleanup()
    raise SystemExit(1)


def install_cleanup_hook():
    import atexit
    import signal
    atexit.register(cleanup)
    for s in (getattr(signal, "SIGINT", None), getattr(signal, "SIGTERM", None)):
        if s is not None:
            try:
                signal.signal(s, _sig_interrupt)
            except Exception:
                pass
