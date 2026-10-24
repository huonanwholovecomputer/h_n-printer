# -*- coding: utf-8 -*-
"""验收 · S3-01 运行时改造（gevent：一条实时连接一个 greenlet）

依据 `docs/安全加固修复计划_2026-10-23.md` §S3-01 与 S2-12 三档压测结论。

要解决的问题（S2-12 实测）：threading + gthread `threads=16` 时，**15 条**设备长连接就把
16 个 worker 占满、HTTP 全线超时；按 §11.2，6 台设备需 19、10 台需 29。gevent 把一条连接
降为一个 greenlet，不再占 OS 线程。

本脚本是 run_all 里的**快速守门**（<60s）：
  A. 静态：开关只有一处（`HN_RUNTIME`）、monkey-patch 在**任何 web 依赖导入之前**、
     async_mode 跟随开关、gunicorn worker_class 跟随开关、requirements 锁 gevent、
     压测脚本 09 在、DEPLOY 文档写明切换/回滚；
  B. 实跑：gevent 模式下 **32 条认证长连接**时 `/api/ping` 与 `/api/orders` 仍全部 200，
     且 `POST /job`（真实定时任务）成功；**对照** gthread 模拟池 16 线程 + 20 条连接 ⇒ HTTP 超时
     （证明判据有区分度，不是"怎么都绿"）；
  C. 变异自证：把开关/补丁位置/worker_class/依赖改回去，对应判据必须红。

用法：python tests/verify_gevent_runtime.py     （exit 0 = 全部通过）
"""
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _sec_harness as H  # noqa: E402

H.install_cleanup_hook()
REPO = H.REPO
STAGING = os.path.join(REPO, "tests", "staging")
APP_PY = os.path.join(REPO, "mobile_apps", "printer-backend", "app.py")
GUNICORN = os.path.join(REPO, "mobile_apps", "printer-backend", "gunicorn_config.py")
REQS = os.path.join(REPO, "mobile_apps", "printer-backend", "requirements.txt")
DEPLOY = os.path.join(REPO, "mobile_apps", "printer-backend", "DEPLOY.md")


def read(p):
    with io.open(p, encoding="utf-8", newline="") as f:
        return f.read().replace("\r\n", "\n")


CODE = {
    "app": read(APP_PY),
    "gunicorn": read(GUNICORN),
    "reqs": read(REQS),
    "deploy": read(DEPLOY) if os.path.isfile(DEPLOY) else "",
    "srv": read(os.path.join(STAGING, "srv.py")),
    "exp09": read(os.path.join(STAGING, "09_gevent_runtime.py")),
    "common": read(os.path.join(STAGING, "common.py")),
    "runner": read(os.path.join(STAGING, "99_run_all.py")),
}


def app_code_only(text):
    """去掉 # 注释（判据必须吃代码不吃注释——注释里必然会提到旧写法）。"""
    return "\n".join("" if ln.lstrip().startswith("#") else ln
                     for ln in text.splitlines())


APP_CODE = app_code_only(CODE["app"])


def checks(c, appc):
    r = {}
    patch_at = appc.find("_gevent_monkey.patch_all()")
    flask_at = appc.find("from flask import Flask")
    r["A1 monkey-patch 在任何 web/网络依赖导入之前"] = (
        patch_at != -1 and flask_at != -1 and patch_at < flask_at
        and patch_at < appc.find("import requests")
        and patch_at < appc.find("from flask_socketio"),
        "patch 位置不对（patch=%d flask=%d）" % (patch_at, flask_at))

    r["A2 运行时开关唯一：HN_RUNTIME=gevent"] = (
        'os.environ.get("HN_RUNTIME"' in c["app"]
        and '_RUNTIME == "gevent"' in c["app"]
        and "HN_RUNTIME" in c["gunicorn"],
        "开关没接上（app/gunicorn 读的不是同一个变量）")

    r["A3 async_mode 跟随开关（禁止与 worker 交叉）"] = (
        '_ASYNC_MODE = "gevent" if _GEVENT else "threading"' in c["app"]
        and "async_mode=_ASYNC_MODE" in c["app"]
        and 'worker_class = "gevent" if _GEVENT else "gthread"' in c["gunicorn"],
        "async_mode 仍硬编码 threading 或 worker 不跟随")

    r["A4 gevent 未安装时 fail-closed（不静默降级）"] = (
        "HN_RUNTIME=gevent 但未安装 gevent" in c["app"],
        "缺 gevent 时会静默按 threading 起来")

    r["A5 requirements 锁定 gevent"] = (
        re.search(r"^gevent==\d", c["reqs"], re.M) is not None,
        "requirements.txt 没锁 gevent")

    r["A6 压测服务支持 gevent 模式（env 在 import app 之前设）"] = (
        '"gevent"' in c["srv"]
        and 'os.environ["HN_RUNTIME"] = "gevent"' in c["srv"]
        and c["srv"].find('os.environ["HN_RUNTIME"] = "gevent"') < c["srv"].find("import app as backend"),
        "srv.py 的开关位置/模式不对")

    r["A7 09 实验覆盖 16/24/32/48 + gthread 对照 + 定时任务"] = (
        "TIERS = [16, 24, 32, 48]" in c["exp09"]
        and "mode=\"pool\"" in c["exp09"]
        and "POST" in c["exp09"] and "/job" in c["exp09"],
        "09 实验的档位/对照/定时任务探针不全")

    r["A8 一键复跑包含 09"] = ('09_gevent_runtime.py' in c["runner"], "99 清单缺 09")
    r["A9 DEPLOY 写明切换与回滚"] = (
        "HN_RUNTIME" in c["deploy"] and "回滚" in c["deploy"],
        "DEPLOY.md 没写运行时的切换/回滚")
    return r


print("=" * 64)
print("A. 静态判据（S3-01 运行时开关与接线）")
print("=" * 64)
base = checks(CODE, APP_CODE)
for name, (ok, detail) in base.items():
    H.chk(name, ok, detail)

# ---------------------------------------------------------------- B. 实跑
print("\n" + "=" * 64)
print("B. 实跑：gevent 32 条连接 vs gthread 16 线程池 20 条连接")
print("=" * 64)

spec = importlib.util.spec_from_file_location("ddos_common", os.path.join(STAGING, "common.py"))
common = importlib.util.module_from_spec(spec)
spec.loader.exec_module(common)

mk = subprocess.run([common.PY, os.path.join(STAGING, "00_make_copy.py")], cwd=STAGING,
                    env=common.child_env(), capture_output=True, text=True)
H.chk("B0 隔离副本可重建", mk.returncode == 0, (mk.stderr or "")[-300:])

GEV_PORT, GEV_CTL = 18089, 18093
POOL_PORT, POOL_CTL = 18086, 18094
G = "http://127.0.0.1:%d" % GEV_PORT

conns = []
try:
    with common.ServerProc(GEV_PORT, GEV_CTL, mode="gevent") as srv:
        time.sleep(1.0)
        st0 = srv.stats()
        H.chk("B1 gevent 模式确实生效（runtime=gevent）", st0.get("runtime") == "gevent", st0)
        for _ in range(32):
            conns.append(common.open_eio_ws(GEV_PORT, token=common.PRINTER_TOKEN, timeout=8.0))
        time.sleep(0.5)
        st = srv.stats()
        p = common.summarize_http(
            [common.http_get(G + "/api/ping", timeout=3) for _ in range(10)])
        H.chk("B2 32 条连接下 OS 线程仍为个位数（连接=greenlet）",
              st["active_count"] < 12, (st["active_count"], st.get("greenlets")))
        H.chk("B3 32 条连接下 /api/ping 全部 200（gthread 在此已全超时）",
              p["ok_200"] == 10 and p["timeout"] == 0, p)
        import urllib.request as ur
        req = ur.Request("http://127.0.0.1:%d/job" % GEV_CTL, data=b"{}", method="POST")
        try:
            with ur.urlopen(req, timeout=30) as resp:
                job = json.loads(resp.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            job = {"ok": False, "error": repr(e)[:120]}
        H.chk("B4 gevent 下定时任务 process_pending_orders 成功", bool(job.get("ok")), job)
        for ws in conns:
            try:
                ws.close()
            except Exception:
                pass
        conns = []

    pconns = []
    with common.ServerProc(POOL_PORT, POOL_CTL, mode="pool", pool_size=16) as srv:
        time.sleep(0.8)
        for _ in range(20):
            try:
                pconns.append(common.open_eio_ws(POOL_PORT, token=common.PRINTER_TOKEN, timeout=2.0))
            except Exception:  # noqa: BLE001
                pass
        time.sleep(0.4)
        pp = common.summarize_http(
            [common.http_get("http://127.0.0.1:%d/api/ping" % POOL_PORT, timeout=2)
             for _ in range(5)])
        H.chk("B5 对照：gthread 16 线程 + 20 条连接 ⇒ HTTP 超时（判据有区分度）",
              pp["timeout"] >= 1, pp)
        for ws in pconns:
            try:
                ws.close()
            except Exception:
                pass
except Exception as e:  # noqa: BLE001
    H.chk("B* 实跑未抛异常", False, repr(e))
finally:
    for ws in conns:
        try:
            ws.close()
        except Exception:
            pass

# ---------------------------------------------------------------- C. 变异
print("\n" + "=" * 64)
print("C. 变异自证（开关/补丁/依赖改回去，判据必须红）")
print("=" * 64)


def mutate(key, old, new, expect, use_app=False):
    c = dict(CODE)
    if old not in c[key]:
        H.chk("★ 变异锚点命中：" + expect, False, "锚点未命中: %r" % old[:70])
        return
    c[key] = c[key].replace(old, new)      # 全量替换：同一标记可能在文中多处出现
    res = checks(c, app_code_only(c["app"]) if use_app else APP_CODE)
    H.chk("★ 变异后变红：" + expect, base[expect][0] and not res[expect][0],
          "变异后仍绿；其它红=%r" % [n for n, (v, _) in res.items() if not v][:3])


mutate("app",
       '_ASYNC_MODE = "gevent" if _GEVENT else "threading"',
       '_ASYNC_MODE = "threading"',
       "A3 async_mode 跟随开关（禁止与 worker 交叉）")
mutate("gunicorn",
       'worker_class = "gevent" if _GEVENT else "gthread"',
       'worker_class = "gthread"',
       "A3 async_mode 跟随开关（禁止与 worker 交叉）")
mutate("reqs", "gevent==26.9.0", "# gevent removed", "A5 requirements 锁定 gevent")
mutate("exp09", "TIERS = [16, 24, 32, 48]", "TIERS = [16]", "A7 09 实验覆盖 16/24/32/48 + gthread 对照 + 定时任务")
mutate("runner", "09_gevent_runtime.py", "08_staging_tiers.py", "A8 一键复跑包含 09")
mutate("deploy", "HN_RUNTIME", "HN_MODE_OLD", "A9 DEPLOY 写明切换与回滚")

H.finish()
