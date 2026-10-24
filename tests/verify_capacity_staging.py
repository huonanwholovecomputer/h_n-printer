# -*- coding: utf-8 -*-
"""验收 · S2-12 容量压测夹具（3 / 6 / 10 台三档）

依据：docs/安全加固修复计划_2026-10-23.md §S2-12 与 §11.2 设备增长模型。

S2-12 的完整验收是「7 个脚本全绿 + 三档容量结论」：
  PYTHONPATH= PYTHONUTF8=1 "…/Python311/python.exe" tests/staging/99_run_all.py
（00 建隔离副本 → 01 线程成本/握手门 → 02 池饱和 → 03 登录阻塞 → 04 ProxyFix 分桶 →
  05 限速内存 → 06 输入成本 → 07 long-polling → 08 三档容量；约 5 分钟，纯本机隔离副本。）

本脚本是它在 `tests/run_all.py` 里的**快速守门**（<60s），防止夹具与结论悄悄失效：
  A. 静态：9 个脚本都在、都按 S2-12 口径改过（认证连接 / 断言 / 三档公式 / 一键 runner）；
  B. 快速实跑：起 16 线程 pool（gthread 模拟）+ 隔离副本，
     8 条认证连接 → HTTP 正常（= 3 台档余量）；16 条 → 池满、HTTP 超时；关闭 → 立即恢复；
  C. 数学：§11.2 required_threads = ceil(设备×2.5)+4 → 3/6/10 台 = 12/19/29，
     与 16 线程的余量判定（3 台过、6 台无余量、10 台超限）；
  D. 变异自证：把夹具改回「未认证连接」「满容量每插一格全量清扫」或改错公式，判据必须红。

用法：python tests/verify_capacity_staging.py     （exit 0 = 全部通过）
"""
import importlib.util
import io
import math
import os
import re
import sys
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _sec_harness as H  # noqa: E402

H.install_cleanup_hook()
REPO = H.REPO
SCRIPTS = os.path.join(REPO, "tests", "staging")
APP_PY = os.path.join(REPO, "mobile_apps", "printer-backend", "app.py")
RATE_TEST = os.path.join(REPO, "tests", "verify_rate_limit_memory.py")

NAMES = ["common.py", "srv.py", "00_make_copy.py", "01_threads_per_ws.py",
         "02_pool_saturation.py", "03_login_block.py", "04_proxyfix_ratelimit.py",
         "05_rate_limits_memory.py", "06_input_cost.py", "07_longpolling.py",
         "08_staging_tiers.py", "99_run_all.py"]


def read(p):
    with io.open(p, encoding="utf-8", newline="") as f:
        return f.read().replace("\r\n", "\n")


CODE = {n: read(os.path.join(SCRIPTS, n)) for n in NAMES
        if os.path.isfile(os.path.join(SCRIPTS, n))}
CODE["app"] = H.strip_comments(read(APP_PY))
CODE["rate_test"] = read(RATE_TEST)


def checks(c):
    """静态判据（纯文本，变异自证直接复用）。返回 {名字: (bool, detail)}。"""
    r = {}
    r["A0 夹具齐全（common/srv/00-08/99）"] = (
        all(n in c for n in NAMES), "缺少: %s" % [n for n in NAMES if n not in c])

    r["A1 夹具支持认证连接（token 走请求头）"] = (
        "PRINTER_TOKEN" in c.get("common.py", "")
        and "def eio_ws_status" in c.get("common.py", "")
        and "X-Printer-Token" in c.get("common.py", "")
        and "token=PRINTER_TOKEN" in c.get("02_pool_saturation.py", "")
        and "token=PRINTER_TOKEN" in c.get("07_longpolling.py", ""),
        "认证连接接线缺失（02/07 或 common）")

    r["A2 01 断言握手门 403 且线程零增长"] = (
        "assert all(c == 403" in c.get("01_threads_per_ws.py", "")
        and '"delta_vs_base"] == 0' in c.get("01_threads_per_ws.py", ""),
        "01 未断言 S0-02 的握手门")

    r["A3 03 打桩盖住 requests 路径（不再真打微信）"] = (
        "_wx_http" in c.get("srv.py", "") and "connectionerror" in c.get("srv.py", "").lower(),
        "srv.py 缺少 _wx_http 打桩（登录实验会外发）")

    r["A4 04 验证 ProxyFix（最右 XFF 生效 + 另一 IP 仍 200）"] = (
        'assert all(p["remote_addr"] == p["expect"]' in c.get("04_proxyfix_ratelimit.py", "")
        and "assert first_b == 200" in c.get("04_proxyfix_ratelimit.py", ""),
        "04 仍在断言修复前的共享桶行为")

    r["A5 05 验证有界容量与过期清扫"] = (
        "_RATE_LIMIT_MAX_KEYS" in c.get("05_rate_limits_memory.py", "")
        and "expired_key_removed" in c.get("05_rate_limits_memory.py", ""),
        "05 未验证容量上限/清扫")

    r["A6 08 三档默认 3/6/10 + §11.2 公式"] = (
        'default="3,6,10"' in c.get("08_staging_tiers.py", "")
        and "2.5" in c.get("08_staging_tiers.py", "")
        and "+ 4" in c.get("08_staging_tiers.py", "")
        and 'required_threads"] > args.pool' in c.get("08_staging_tiers.py", ""),
        "08 的档位/公式/判定不全")

    r["A7 99 一键复跑覆盖 00→08"] = (
        all(n in c.get("99_run_all.py", "") for n in
            ["00_make_copy.py", "01_threads_per_ws.py", "02_pool_saturation.py",
             "03_login_block.py", "04_proxyfix_ratelimit.py", "05_rate_limits_memory.py",
             "06_input_cost.py", "07_longpolling.py", "08_staging_tiers.py"]),
        "99 的脚本清单不全")

    # 全文件只允许**一处**「主动调用清扫」的语句行（60s 周期那处）；def 行不算调用。
    sweep_calls = [ln.strip() for ln in c["app"].splitlines()
                   if ln.strip().startswith("_sweep_rate_limits_locked(now)")]
    r["A8 S2-12 修复：满容量淘汰时不再每次全量清扫"] = (
        # 淘汰分支的结构里不得夹着清扫；且主动调用清扫的语句只允许一处
        re.search(r"if len\(_rate_limits\) >= _RATE_LIMIT_MAX_KEYS:\s*\n"
                  r"\s*while len\(_rate_limits\) >= _RATE_LIMIT_MAX_KEYS:", c["app"]) is not None
        and len(sweep_calls) == 1,
        "app.py 的淘汰分支又被塞回全量清扫（O(N×M) 放大器）：主动清扫调用 %d 处"
        % len(sweep_calls))

    r["A9 限速测试有插入耗时守卫 + 变异自证"] = (
        "插入耗时 < 10s" in c["rate_test"] and "mut2" in c["rate_test"],
        "verify_rate_limit_memory 缺耗时守卫/变异")
    return r


print("=" * 64)
print("A. 静态判据（S2-12 夹具与结论接线）")
print("=" * 64)
base = checks(CODE)
for name, (ok, detail) in base.items():
    H.chk(name, ok, detail)

# ---------------------------------------------------------------- C. 数学
print("\n" + "=" * 64)
print("C. §11.2 公式：required_threads = ceil(设备×2.5) + 4（线程 16）")
print("=" * 64)
formula = {d: math.ceil(d * 2.5) + 4 for d in (3, 6, 10)}
H.chk("3 台 → 12 线程（≤16，余量 4）", formula[3] == 12, formula[3])
H.chk("6 台 → 19 线程（>16，无余量）", formula[6] == 19, formula[6])
H.chk("10 台 → 29 线程（>16，超限）", formula[10] == 29, formula[10])
H.chk("按公式判定的 S3-01 触发档位 = {6, 10}",
      {d for d, v in formula.items() if v > 16} == {6, 10}, formula)

# ---------------------------------------------------------------- B. 快速实跑
print("\n" + "=" * 64)
print("B. 快速实跑：16 线程 pool（gthread 模拟）+ 隔离副本")
print("=" * 64)

spec = importlib.util.spec_from_file_location(
    "ddos_common", os.path.join(SCRIPTS, "common.py"))
common = importlib.util.module_from_spec(spec)
spec.loader.exec_module(common)

# 每次从干净副本开始（00 会重建 %TEMP%\ddos_audit_copy，假 config、清空 DB/uploads/logs）
import subprocess  # noqa: E402
mk = subprocess.run([common.PY, os.path.join(SCRIPTS, "00_make_copy.py")],
                    cwd=SCRIPTS, env=common.child_env(), capture_output=True, text=True)
H.chk("B0 隔离副本可重建（00_make_copy.py）", mk.returncode == 0,
      (mk.stderr or "")[-300:])

PORT, CTL = 18086, 18093
PING = f"http://127.0.0.1:{PORT}/api/ping"
conns = []
try:
    with common.ServerProc(PORT, CTL, mode="pool", pool_size=16) as srv:
        time.sleep(0.8)
        base_st = srv.stats()
        H.chk("B1 服务起在隔离副本上（inflight=0）",
              (base_st.get("pool") or {}).get("inflight") == 0, base_st.get("pool"))

        # 3 台档：8 条认证连接 → HTTP 正常
        for _ in range(8):
            conns.append(common.open_eio_ws(PORT, token=common.PRINTER_TOKEN))
        time.sleep(0.6)
        st8 = srv.stats()
        p8 = common.summarize_http(
            [common.http_get(PING, timeout=3) for _ in range(10)])
        H.chk("B2 8 条认证连接占 8/16 worker", (st8.get("pool") or {}).get("inflight") == 8,
              st8.get("pool"))
        H.chk("B3 3 台档：串行 ping 全部 200（有余量）",
              p8["ok_200"] == 10 and p8["timeout"] == 0, p8)

        # 10 台档：补到 16 条 → 池满、HTTP 超时
        added, fail = 0, 0
        for _ in range(8):
            try:
                conns.append(common.open_eio_ws(PORT, token=common.PRINTER_TOKEN, timeout=4.0))
                added += 1
            except Exception:
                fail += 1
        time.sleep(0.6)
        st16 = srv.stats()
        p16 = common.summarize_http(
            [common.http_get(PING, timeout=2) for _ in range(10)])
        H.chk("B4 补到 16 条后池内 inflight=16（或握手已被拒）",
              (st16.get("pool") or {}).get("inflight") == 16 or fail > 0,
              (st16.get("pool"), {"added": added, "fail": fail}))
        H.chk("B5 池满时 HTTP 被阻塞（串行 ping 出现超时）",
              p16["timeout"] >= 5, p16)
        for ws in conns:
            try:
                ws.close()
            except Exception:
                pass
        conns = []
        time.sleep(3.0)
        p_after = common.summarize_http([common.http_get(PING, timeout=3) for _ in range(5)])
        H.chk("B6 关闭连接后 worker 回收、ping 恢复 200（被占用而非被打坏）",
              p_after["ok_200"] == 5, p_after)
except Exception as e:  # noqa: BLE001
    H.chk("B* 快速实跑未抛异常", False, repr(e))
finally:
    for ws in conns:
        try:
            ws.close()
        except Exception:
            pass

# ---------------------------------------------------------------- D. 变异自证
print("\n" + "=" * 64)
print("D. 变异自证（夹具/结论改回去，判据必须红）")
print("=" * 64)


def mutate(key, old, new, expect):
    mt = dict(CODE)
    if old not in mt[key]:
        H.chk("★ 变异锚点命中：" + expect, False, "锚点未命中: %r" % old[:70])
        return
    mt[key] = mt[key].replace(old, new, 1)
    res = checks(mt)
    H.chk("★ 变异后变红：" + expect,
          base[expect][0] and (not res[expect][0]),
          "变异后仍绿；其它红=%r" % [n for n, (v, _) in res.items() if not v][:3])


mutate("02_pool_saturation.py",
       "open_eio_ws(APP_PORT, token=PRINTER_TOKEN)",
       "open_eio_ws(APP_PORT)",
       "A1 夹具支持认证连接（token 走请求头）")
mutate("01_threads_per_ws.py",
       "assert all(c == 403 for r in gate_rows for c in r[\"statuses\"]), gate_rows",
       "pass",
       "A2 01 断言握手门 403 且线程零增长")
mutate("08_staging_tiers.py",
       'default="3,6,10"', 'default="3"',
       "A6 08 三档默认 3/6/10 + §11.2 公式")
mutate("app",
       "            if len(_rate_limits) >= _RATE_LIMIT_MAX_KEYS:\n"
       "                while len(_rate_limits) >= _RATE_LIMIT_MAX_KEYS:",
       "            if len(_rate_limits) >= _RATE_LIMIT_MAX_KEYS:\n"
       "                _sweep_rate_limits_locked(now)\n"
       "                while len(_rate_limits) >= _RATE_LIMIT_MAX_KEYS:",
       "A8 S2-12 修复：满容量淘汰时不再每次全量清扫")

H.finish()
