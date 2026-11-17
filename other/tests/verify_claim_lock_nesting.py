# -*- coding: utf-8 -*-
"""验收 · 接单锁 _claim_lock 不得自锁（2026-11-17 生产事故回归）

事故（2026-11-17 13:30）：POST /api/printer/release 之后整站 API 冻结 —— nginx 全 499，
本机打印工具弹「无法启用接单：Read timed out」，之后 13:43 / 15:35 的 POST /api/printer/claim
也全 499。py-spy 显示 16 个 gthread 线程全堵在
load_claim <- get_claiming_devices <- _printer_state_payload <- broadcast_printer_state
<- printer_release。

根因：printer_release 在 with _claim_lock: 块内调 broadcast_printer_state()，而它会沿
_printer_state_payload -> get_claiming_devices -> load_claim 再取同一把 threading.Lock（非重入）
=> 自锁，持锁线程永不释放，其它线程排队等锁，整站永久挂起（连 /api/pricing 都是 499）。

判据三组：
  A. 静态 —— app.py 里任何 with _claim_lock: 块内都不得出现读 claim 的调用
     （broadcast_printer_state / _printer_state_payload / get_claiming_devices /
      get_claiming_device_ids / load_claim / is_claiming）；
  B. 动态 —— 真实 release 请求必须 15s 内返回且落库（僵死时这条红）；
  C. 变异自证 —— 把 broadcast 塞回锁内，B 的探针必须判为卡死（证明判据真能红）。

用法：python tests/verify_claim_lock_nesting.py     # exit 0 = 全绿
"""
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SRC = os.path.join(REPO, "mobile_apps", "printer-backend")
APP_PY = os.path.join(SRC, "app.py")
PY = sys.executable

ok_all = True
_tmpdirs = []


def check(name, cond, detail=""):
    global ok_all
    if not cond:
        ok_all = False
    print("[%s] %s%s" % ("PASS" if cond else "FAIL", name,
                         ("  -- " + str(detail)) if detail and not cond else ""))


def section(t):
    print("\n=== " + t + " ===")


def make_copy(prefix="hn_claimlock_"):
    """复制后端源码到临时目录（不碰真实 orders.db / devices.json）。"""
    tmp = tempfile.mkdtemp(prefix=prefix)
    dst = os.path.join(tmp, "printer-backend")
    shutil.copytree(SRC, dst, ignore=shutil.ignore_patterns(
        "__pycache__", "config.py", "config.py.example", "orders.db", "users.db",
        "uploads", "logs", "*.db-wal", "*.db-shm", "*.bak*"))
    os.makedirs(os.path.join(dst, "uploads", "avatars"), exist_ok=True)
    _tmpdirs.append(tmp)
    return dst


CLAIM_READERS = ("broadcast_printer_state", "_printer_state_payload",
                 "get_claiming_devices", "get_claiming_device_ids", "load_claim",
                 "is_claiming")


def scan_nested_calls(path):
    """返回 [(行号, 调用名, 该行)]：出现在 with _claim_lock: 块内的读 claim 调用。"""
    lines = open(path, encoding="utf-8").read().splitlines()
    hits = []
    for idx, line in enumerate(lines):
        m = re.match(r"(\s*)with _claim_lock\s*:", line)
        if not m:
            continue
        base = len(m.group(1))
        j = idx + 1
        while j < len(lines):
            body = lines[j]
            if body.strip():
                indent = len(body) - len(body.lstrip())
                if indent <= base and not body.lstrip().startswith("#"):
                    break
                for name in CLAIM_READERS:
                    if re.search(r"\b" + name + r"\s*\(", body):
                        hits.append((j + 1, name, body.strip()))
            j += 1
    return hits


section("A. 静态：with _claim_lock 块内不得再取锁")
hits = scan_nested_calls(APP_PY)
check("app.py 的 _claim_lock 块内没有读 claim 的调用（自锁=全站冻结）", not hits,
      "; ".join("app.py:%d: %s()" % (n, name) for n, name, _ in hits))
src_text = open(APP_PY, encoding="utf-8").read()
norm_src = src_text.replace("\r\n", "\n")
check("printer_release 的 broadcast 在锁外（事故修复点仍在位）",
      "    if not released:\n        return jsonify({\"success\": True, \"message\": \"当前设备未启用接单\"})"
      in norm_src and "2026-11-17 生产事故" in src_text)
check("_claim_lock 仍是不可重入的 threading.Lock（换成 RLock 会掩盖同类 bug）",
      re.search(r"_claim_lock\s*=\s*threading\.Lock\(\)", src_text) is not None)

PROBE = """# -*- coding: utf-8 -*-
import io, json, os, sys, threading
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)
sys.path.insert(0, os.getcwd())
os.environ["FLASK_ENV"] = "development"
import app as backend
backend.init_db()
backend.PRINTER_TOKEN = "probe-tok"
http = backend.app.test_client()
h = {"X-Printer-Token": "probe-tok"}

r = http.post("/api/printer/claim", json={"client_id": "PROBE-PC-1"}, headers=h)
assert r.status_code == 200, r.data
# broadcast_printer_state() 在「没有在线设备」时会直接 return —— 那样锁内嵌套根本走不到，
# 变异体也不会卡死。这里造一台在线设备（假 sid，emit 会被忽略），确保广播真跑。
backend.touch_printer_heartbeat("PROBE-PC-1", sid="probe-sid")
called = {"n": 0}
_orig_broadcast = backend.broadcast_printer_state


def _spy():
    called["n"] += 1
    return _orig_broadcast()


backend.broadcast_printer_state = _spy
res = {}


def go():
    try:
        res["r"] = http.post("/api/printer/release", json={"client_id": "PROBE-PC-1"}, headers=h)
    except BaseException as e:  # noqa: BLE001
        res["err"] = repr(e)


t = threading.Thread(target=go, daemon=True)
t.start()
t.join(WAIT)
out = {"hung": t.is_alive()}
if not out["hung"]:
    rr = res.get("r")
    out["status"] = rr.status_code if rr is not None else -1
    try:
        out["json"] = rr.get_json()
    except Exception:
        out["json"] = None
    claim = (backend.load_claim() or {}).get("claiming_devices") or {}
    out["still_claiming"] = "PROBE-PC-1" in claim
    out["broadcast_calls"] = called["n"]
    out["err"] = res.get("err")
print("RESULT=" + json.dumps(out, ensure_ascii=False))
"""


def run_probe(dst, wait=15, timeout=120):
    body = PROBE.replace("WAIT", str(wait))
    env = dict(os.environ)
    env["PYTHONPATH"] = ""
    env["PYTHONUTF8"] = "1"
    env["FLASK_ENV"] = "development"
    return subprocess.run([PY, "-c", body], cwd=dst, env=env, capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=timeout)


def probe_result(proc):
    for line in (proc.stdout or "").splitlines():
        if line.startswith("RESULT="):
            return json.loads(line[len("RESULT="):])
    return None


section("B. 动态：release 必须 15s 内返回（僵死时这里红）")
dst = make_copy()
try:
    proc = run_probe(dst, wait=15)
    got = probe_result(proc)
    if got is None:
        print((proc.stdout or "")[-1500:])
        print((proc.stderr or "")[-3000:])
    check("release 探针拿到结论（没有把整个进程拖死）", got is not None, "stdout/stderr 见上")
    if got:
        check("release 未卡死", got.get("hung") is False)
        check("release 返回 200 + success=True",
              got.get("status") == 200 and isinstance(got.get("json"), dict)
              and got["json"].get("success") is True,
              json.dumps(got, ensure_ascii=False))
        check("release 真把设备移出接单表", got.get("still_claiming") is False,
              json.dumps(got, ensure_ascii=False))
        check("release 真跑了广播（在线设备存在时 broadcast 不是空转）",
              (got.get("broadcast_calls") or 0) >= 1, json.dumps(got, ensure_ascii=False))
except subprocess.TimeoutExpired:
    check("release 探针未超时（整体 120s）", False, "子进程整段超时 = 又自锁了")

section("C. 变异自证：把 broadcast 塞回锁内，B 必须红")
mut = make_copy(prefix="hn_claimlock_mut_")
mpath = os.path.join(mut, "app.py")
with open(mpath, "r", encoding="utf-8", newline="") as f:
    msrc = f.read()
nl = "\r\n" if "\r\n" in msrc else "\n"
anchor = "            released = True" + nl
if msrc.count(anchor) != 1:
    check("变异锚点唯一命中（release 的 released=True 赋值）", False,
          "命中 %d 次" % msrc.count(anchor))
else:
    msrc = msrc.replace(anchor, "            broadcast_printer_state()" + nl + anchor)
    with open(mpath, "w", encoding="utf-8", newline="") as f:
        f.write(msrc)
    mhits = scan_nested_calls(mpath)
    check("变异副本静态判据转红（锁内又出现 broadcast_printer_state）", bool(mhits))
    check("变异副本命中的是 printer_release 的广播",
          any(h[1] == "broadcast_printer_state" for h in mhits), str(mhits[:3]))
    try:
        proc = run_probe(mut, wait=8, timeout=90)
        mgot = probe_result(proc)
    except subprocess.TimeoutExpired:
        mgot = {"hung": True}
    check("变异副本上 release 卡死 => B 的判据真能红",
          bool(mgot) and mgot.get("hung") is True,
          json.dumps(mgot, ensure_ascii=False) if mgot else "无 RESULT")

for d in _tmpdirs:
    shutil.rmtree(d, ignore_errors=True)

print("\n" + ("✅ 全部通过" if ok_all else "❌ 有失败"))
sys.exit(0 if ok_all else 1)
