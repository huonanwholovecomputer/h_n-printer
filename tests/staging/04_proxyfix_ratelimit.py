"""04 · 反代后 remote_addr 与限速桶（复跑版：验证 S0-03 的修复）

> 修复前（2026-10-23 审计）：没装 ProxyFix ⇒ `request.remote_addr` 恒为 127.0.0.1 ⇒
> 全站共用一个 IP 桶，一个人发 20 次匿名 device_login 就能把「新设备登录」锁死 1 小时。
> 修复后（S0-03）：`ProxyFix(app.wsgi_app, x_for=1)` ⇒ 取 nginx 追加的**最右一项** XFF；
> 且 device_login 拆成 `device_login:ip:<ip>`（120/h）+ `device_login:dev:<hash>`（20/h）两个桶。
>
> 本实验（真代码、进程内 Flask test_client，无服务器/无网络）验证：
>   1) 伪造 XFF 时 remote_addr = 最右一项（左侧伪造无效），无 XFF 时回落 127.0.0.1；
>   2) IP A 打满自己的 120/h 后，**IP B 仍 200**（桶按真实客户端分离）；
>   3) 同一 device_id 换 20 个 IP 后第 21 次 429（每设备桶仍然生效）。
>
> 用法：
>   PYTHONPATH= PYTHONUTF8=1 "C:/.../python.exe" "tests/staging/04_proxyfix_ratelimit.py"
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import COPY, save_result  # noqa: E402

os.chdir(COPY)
sys.path.insert(0, COPY)
import app as backend  # noqa: E402
from flask import request, jsonify  # noqa: E402


def install_probe():
    if any(getattr(r, "rule", "") == "/__audit/remote_addr" for r in backend.app.url_map.iter_rules()):
        return

    @backend.app.route("/__audit/remote_addr")
    def _audit_remote_addr():
        return jsonify({
            "remote_addr": request.remote_addr,
            "x_forwarded_for": request.headers.get("X-Forwarded-For"),
            "x_real_ip": request.headers.get("X-Real-IP"),
            "wsgi_remote_addr": request.environ.get("REMOTE_ADDR"),
        })


def device_login(c, ip, device_id):
    return c.post("/api/device_login", json={"device_id": device_id},
                  headers={"X-Forwarded-For": ip})


def main():
    install_probe()
    backend.init_db()          # 隔离副本首次跑：建表（副本 DB 由 00_make_copy 清空）
    c = backend.app.test_client()
    out = {"experiment": "反代后 remote_addr 与限速桶（S2-12 复跑）", "copy": COPY}

    # ---- 1) ProxyFix：最右一项 XFF 生效 ----
    probes = []
    for xff, expect in [("203.0.113.7", "203.0.113.7"),
                        ("198.51.100.9, 10.0.0.1", "10.0.0.1"),   # 左侧为客户端伪造
                        (None, "127.0.0.1")]:
        headers = {"X-Forwarded-For": xff} if xff else {}
        d = c.get("/__audit/remote_addr", headers=headers).get_json()
        probes.append({"sent_xff": xff, "expect": expect, **d})
        print(f"[1] XFF={xff!r} → remote_addr={d['remote_addr']!r}（期望 {expect!r}）")
    assert all(p["remote_addr"] == p["expect"] for p in probes), probes
    out["remote_addr_probes"] = probes
    print("[1] 断言通过：ProxyFix(x_for=1) 生效 —— 取最右一项，左侧伪造无效；无 XFF 回落 127.0.0.1")

    # ---- 2) 每 IP 桶独立：A 打满不影响 B ----
    backend._rate_limits.clear()
    limit = 120   # device_login:ip 的硬值（app.py: _rate_limit(f"device_login:ip:{ip}", 120, 3600)）
    ip_a, ip_b = "203.0.113.50", "203.0.113.60"
    codes_a = [device_login(c, ip_a, f"burst-a-{i:03d}").status_code for i in range(limit)]
    extra_a = device_login(c, ip_a, "burst-a-extra").status_code
    first_b = device_login(c, ip_b, "legit-b-0001").status_code
    print(f"[2] IP A 连续 {limit} 次 → 200×{codes_a.count(200)}；A 第 {limit + 1} 次 → {extra_a}；"
          f"IP B 首次 → {first_b}")
    out["ip_buckets"] = {"limit_per_hour": limit, "a_200": codes_a.count(200),
                         "a_extra": extra_a, "b_first": first_b,
                         "buckets": len(backend._rate_limits)}
    assert codes_a.count(200) == limit and extra_a == 429, (codes_a.count(200), extra_a)
    assert first_b == 200, first_b
    print("[2] 断言通过：A 打满自己的桶后，B 仍然 200（修复前两者都是 429）")

    # ---- 3) 每设备桶：同一 device_id 换 20 个 IP 后仍 429 ----
    backend._rate_limits.clear()
    dev_limit = backend._SECURITY["device_login_rate_limit"]
    dev = "same-device-id-0001"
    codes = [device_login(c, f"198.18.0.{i + 1}", dev).status_code for i in range(dev_limit)]
    blocked = device_login(c, "198.18.0.201", dev).status_code
    print(f"[3] 同一 device_id 换 {dev_limit} 个 IP → 200×{codes.count(200)}；第 {dev_limit + 1} 次"
          f"（再换 IP）→ {blocked}")
    out["device_bucket"] = {"limit_per_hour": dev_limit, "ok": codes.count(200),
                            "extra": blocked}
    assert codes.count(200) == dev_limit and blocked == 429, (codes.count(200), blocked)
    print("[3] 断言通过：换 IP 绕不过每设备桶")

    out["conclusion"] = ("ProxyFix 让 remote_addr 取 nginx 追加的最右一项 XFF（客户端伪造左侧无效）；"
                         "device_login 拆成每 IP 120/h + 每设备 20/h 两个桶 ⇒ 单一来源无法再锁死全站，"
                         "而同一设备反复注册仍被钳住。")
    save_result("04_proxyfix_ratelimit", out)
    print("\n[CONCLUSION] " + out["conclusion"])


if __name__ == "__main__":
    main()
