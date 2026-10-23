# -*- coding: utf-8 -*-
"""验收 · S1-01 per-device token（scope 矩阵 / 吊销 / 迁移窗口 / client_id 绑定）

依据：docs/安全加固修复计划_2026-10-23.md §S1-01（DS-02 根治、R7）。

现场形状：全站只有 config.py 里**一个** TOKEN —— 泄露一次要全站轮换、无法单独吊销某台设备、
也无法区分"这台设备能做什么"。多台设备共用一把钥匙 = 任意一台被物理接触就等于全站管理员。

本脚本：
  A. 静态接线：devices.json 里的 token_hash/scopes/issued_at/revoked_at、统一认证入口
     `_authenticate_printer()`、所有 printer 路由都走它、admin 路由有 scope 装饰器、
     签发/吊销接口、只存哈希（明文只返回一次）。
  B. 行为：全局 TOKEN 迁移窗口可用；签发本机 token；print/admin scope 矩阵；
     吊销后立即 403；per-device token 只能以自己的 client_id 连接（防冒充）；
     握手门同样认 per-device token 与吊销。
  C. 变异自证：把 scope 校验改成永真 → print-only token 能调 admin 接口（判据转红）。

用法：python tests/verify_device_token.py     （exit 0 = 全部通过）
"""
import io
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _sec_harness as H  # noqa: E402

H.install_cleanup_hook()
src = H.read_app_source()
code = H.strip_comments(src)


def block(marker):
    i = code.find(marker)
    if i < 0:
        return ""
    lines = code[i:].splitlines(keepends=True)
    out = [lines[0]]
    for ln in lines[1:]:
        if ln.strip() and not ln[:1].isspace():
            break
        out.append(ln)
    return "".join(out)


# ============================================================
# A. 静态接线
# ============================================================
print("=== A. 静态接线 ===")
H.chk("devices.json 记录 token_hash / scopes / 签发与吊销时间",
      '"token_hash": _hash_printer_token(token)' in code
      and '"scopes": scopes' in code and '"token_issued_at": now_str' in code
      and '"revoked_at": ""' in code)
H.chk("只存哈希：签发函数里没有把明文写进 devices.json",
      "token_hash" in block("def issue_device_token(")
      and '"token": token' not in block("def issue_device_token("))
H.chk("统一认证入口 _authenticate_printer()（per-device 优先，全局 TOKEN 兜底）",
      "def _authenticate_printer(" in code
      and "_find_device_by_token(tok)" in block("def _authenticate_printer(")
      and "_printer_token_ok(tok)" in block("def _authenticate_printer("))
H.chk("已吊销的 token 直接判无效",
      "if rec.get(\"revoked_at\"):" in block("def _find_device_by_token("))
n_auth = code.count("_authenticate_printer(token)")
H.chk("★ 所有 printer 路由都改走统一认证（≥25 处）", n_auth >= 25, "实际 %d" % n_auth)
H.chk("_printer_token_ok 只作为\"全局 TOKEN 兼容\"留在 _authenticate_printer 内部",
      code.count("_printer_token_ok(") <= 4, code.count("_printer_token_ok("))
H.chk("scope 装饰器 require_printer_scope 存在",
      "def require_printer_scope(" in code and "@require_printer_scope(\"admin\")" in code)
H.chk("管理类路由都挂了 admin scope",
      sum(code.count('@require_printer_scope("admin")') for _ in [0]) >= 10,
      code.count('@require_printer_scope("admin")'))
H.chk("签发/吊销接口存在",
      '@app.route("/api/printer/token/create"' in code
      and '@app.route("/api/printer/token/revoke"' in code)
H.chk("per-device token 只能以自己的 client_id 连接（防冒充）",
      "_auth[\"client_id\"] != client_id" in code)

# ============================================================
# B. 行为
# ============================================================
print("\n=== B. 行为（临时后端副本）===")
TMP, DST = H.make_copy("hn_devtok_")
backend = H.import_backend(DST)
backend.init_db()
GLOBAL_TOK = "global-migration-token"
backend.PRINTER_TOKEN = GLOBAL_TOK
http = backend.app.test_client()
G = {"X-Printer-Token": GLOBAL_TOK}


def H_(tok):
    return {"X-Printer-Token": tok}


H.chk("迁移窗口：全局 TOKEN 仍可调 print 接口",
      http.get("/api/printer/devices", headers=G).status_code == 200)

r = http.post("/api/printer/token/create", json={"client_id": "dev-store-A", "label": "门店A"},
              headers=G)
tok_a = (r.get_json() or {}).get("token")
H.chk("用全局 TOKEN 为本机签发 token", r.status_code == 200 and bool(tok_a))
H.chk("默认 scopes = print + admin（门店机要拉单也要看统计）",
      (r.get_json() or {}).get("scopes") == ["print", "admin"], r.get_json())
H.chk("devices.json 里只有哈希、没有明文",
      tok_a not in open(os.path.join(DST, "devices.json"), encoding="utf-8").read())

H.chk("本机 token 调 print 接口 200",
      http.get("/api/printer/devices", headers=H_(tok_a)).status_code == 200)
H.chk("本机 token 调 admin 接口 200",
      http.get("/api/admin/statistics/months", headers=H_(tok_a)).status_code == 200)

r = http.post("/api/printer/token/create",
              json={"client_id": "dev-store-P", "scopes": ["print"]}, headers=H_(tok_a))
tok_p = (r.get_json() or {}).get("token")
H.chk("签发 print-only token", r.status_code == 200 and bool(tok_p))
H.chk("★ print-only 能调 print 接口",
      http.get("/api/printer/devices", headers=H_(tok_p)).status_code == 200)
H.chk("★ print-only 调 admin 接口 → 403（scope 生效）",
      http.get("/api/admin/statistics/months", headers=H_(tok_p)).status_code == 403)
H.chk("print-only 调订单状态订正 → 403",
      http.post("/api/admin/order_status", json={"order_id": 1, "status": "sent"},
                headers=H_(tok_p)).status_code == 403)

H.chk("未知 token → 403",
      http.get("/api/printer/devices", headers=H_("nope")).status_code == 403)

# client_id 绑定
c = backend.socketio.test_client(backend.app,
                                 query_string="?token=%s&client_id=dev-store-A" % tok_a)
H.chk("本机 token 以自己的 client_id 连接成功", c.is_connected())
c.disconnect()
c2 = backend.socketio.test_client(backend.app,
                                  query_string="?token=%s&client_id=dev-OTHER" % tok_a)
H.chk("★ 用 A 的 token 冒充 B 连接 → 被拒", not c2.is_connected())
try:
    c2.disconnect()
except Exception:
    pass

# 吊销
r = http.post("/api/printer/token/revoke", json={"client_id": "dev-store-A"}, headers=G)
H.chk("吊销 dev-store-A → 200", r.status_code == 200, r.get_json())
H.chk("★ 吊销后该 token 立即 403（其它设备不受影响）",
      http.get("/api/printer/devices", headers=H_(tok_a)).status_code == 403
      and http.get("/api/printer/devices", headers=H_(tok_p)).status_code == 200)
H.chk("吊销不影响全局 TOKEN（紧急通道）",
      http.get("/api/printer/devices", headers=G).status_code == 200)
H.chk("吊销后握手门也 403",
      http.get("/socket.io/?EIO=4&transport=polling&token=" + tok_a).status_code == 403)
H.chk("有效 per-device token 握手 200",
      http.get("/socket.io/?EIO=4&transport=polling&token=" + tok_p).status_code == 200)
H.chk("重复吊销 → 404（如实报告没有可吊销的 token）",
      http.post("/api/printer/token/revoke", json={"client_id": "dev-store-A"},
                headers=G).status_code == 404)

# ============================================================
# C. 变异自证：scope 校验永真
# ============================================================
print("\n=== C. 变异自证（scope 校验改成永真）===")
mut_tmp, mut_dst = H.mutated_copy([
    ("def _printer_scope_ok(tok, scope):\n    rec = _authenticate_printer(tok)\n"
     "    return bool(rec and scope in rec[\"scopes\"])",
     "def _printer_scope_ok(tok, scope):\n    rec = _authenticate_printer(tok)\n"
     "    return bool(rec)"),
], prefix="hn_devtok_mut_")
proc = H.run_probe(mut_dst, """
backend.PRINTER_TOKEN = "g"
r = http.post("/api/printer/token/create", json={"client_id": "p1", "scopes": ["print"]},
              headers={"X-Printer-Token": "g"})
tok = (r.get_json() or {}).get("token")
print("RESULT=%s" % http.get("/api/admin/statistics/months",
                             headers={"X-Printer-Token": tok}).status_code)
""")
H.chk("scope 校验永真后 print-only 也能调 admin 接口（判据能红）",
      H.probe_value(proc) == "200", H.probe_value(proc))

H.finish()
