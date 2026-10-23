# -*- coding: utf-8 -*-
"""验收 · S0-02 Engine.IO 握手前置 token 门

依据：docs/安全加固修复计划_2026-10-23.md §S0-02、DDoS R1、附录 A-3 / A-4。

现场形状：token 校验原先只在 Socket.IO 的 `40 CONNECT` 事件里做。攻击者只要完成
Engine.IO 握手（`/socket.io/?EIO=4&transport=polling`，**不发 40**）就能占住一个
gthread，16 条这样的连接即打满 `threads=16`（实测基线：线上 access.log 里已有 4 条
匿名 `/socket.io/` 扫描，UA `rawgrab`）。

本脚本：
  A. 静态接线：门定义在 `SocketIO(...)` **之后**赋值、ProxyFix 在门**外层**、只拦无 sid
     的握手、判定走统一 token 助手、不把 token 打进日志。
  B. 行为（真实 Flask test_client）：
     无 token 握手 403；query/header 带 token 握手 200；带 sid 的 poll 不被门拦（400 是
     engineio 自己回的"未知会话"）；`PRINTER_TOKEN` 未配置时失败关闭（403）。
  C. 变异自证：把门的两行赋值删掉 → 无 token 握手不再 403（403 确实来自门，而不是别处）。

用法：python tests/verify_sio_gate.py     （exit 0 = 全部通过）
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

# ============================================================
# A. 静态接线
# ============================================================
print("=== A. 静态接线 ===")
i_socketio = code.find("socketio = SocketIO(")
i_gate_def = code.find("def _sio_handshake_gate(")
i_gate_assign = code.find("app.wsgi_app = _sio_handshake_gate(app.wsgi_app)")
i_proxy = code.find("app.wsgi_app = ProxyFix(")

H.chk("门函数存在", i_gate_def > 0)
H.chk("门在 SocketIO() 之后定义/赋值（附录 A-4：在它之前赋值会被当成内层 WSGI app）",
      i_socketio > 0 and i_gate_def > i_socketio and i_gate_assign > i_socketio)
H.chk("ProxyFix 在门**外层**（后赋值 = 更外层，否则真实 IP 永远是 127.0.0.1）",
      i_proxy > i_gate_assign > 0)
H.chk("ProxyFix 没有 x_real_ip 参数（附录 A-1：传了会 TypeError、后端起不来）",
      "x_real_ip" not in code[i_proxy:i_proxy + 200])
H.chk("只拦无 sid 的握手", 'if "sid" not in params:' in code and
      'params = (dict(urllib.parse.parse_qsl' in code)
H.chk("超长 query 有上限（不把 parse_qsl 变成 CPU 放大面）",
      "_SIO_GATE_MAX_QUERY" in code and "len(qs) <= _SIO_GATE_MAX_QUERY" in code)
H.chk("token 判定走统一助手 _printer_token_ok（而不是内联比较）",
      "_printer_token_ok(tok)" in code[i_gate_def:i_gate_def + 2500])
H.chk("同时接受 X-Printer-Token 头与 ?token= query",
      'environ.get("HTTP_X_PRINTER_TOKEN")' in code and 'params.get("token")' in code)
def _top_level_block(text, marker):
    """截取从 marker 起、到下一个列 0 非空行为止的代码块（避免把后面 3000 字一起吞进来）。"""
    i = text.find(marker)
    if i < 0:
        return ""
    lines = text[i:].splitlines(keepends=True)
    out = [lines[0]]
    for ln in lines[1:]:
        if ln.strip() and not ln[:1].isspace():
            break
        out.append(ln)
    return "".join(out)


gate_body = _top_level_block(code, "def _sio_handshake_gate(")
H.chk("门内异常失败关闭（回 403，不放行）",
      gate_body.count("return _forbid()") >= 2)
H.chk("403 不回显 token、不打 token 明文（门内完全不打印、响应体不含凭据）",
      "print(" not in gate_body
      and "{tok" not in gate_body
      and "% tok" not in gate_body
      and 'b"Forbidden"' in gate_body)

# ============================================================
# B. 行为
# ============================================================
print("\n=== B. 行为（临时后端副本 + test_client）===")
TMP, DST = H.make_copy("hn_gate_")
backend = H.import_backend(DST)
backend.init_db()
TOK = "gate-test-token"
backend.PRINTER_TOKEN = TOK
http = backend.app.test_client()

HS = "/socket.io/?EIO=4&transport=polling"

r = http.get(HS)
H.chk("无 token 握手 → 403", r.status_code == 403, "实际 %s" % r.status_code)
H.chk("403 响应体是纯文本 Forbidden（不回显任何凭据）",
      r.data.strip() == b"Forbidden", r.data[:80])
H.chk("403 响应头不含 Set-Cookie / 不暴露内部信息", "Set-Cookie" not in r.headers)

r = http.get(HS + "&token=" + TOK)
H.chk("query 带 token 握手 → 200（engineio open 包）", r.status_code == 200,
      "实际 %s" % r.status_code)
H.chk("200 响应体是 engineio open 包（0{...}）", r.data[:1] == b"0", r.data[:40])

r = http.get(HS, headers={"X-Printer-Token": TOK})
H.chk("header 带 token 握手 → 200（官方新版客户端的形态）", r.status_code == 200,
      "实际 %s" % r.status_code)

r = http.get(HS + "&token=wrong-token")
H.chk("错误 token 握手 → 403", r.status_code == 403, "实际 %s" % r.status_code)

r = http.get("/socket.io/?EIO=4&transport=polling&sid=fake-sid-without-token")
H.chk("带 sid 的 poll 不被门拦（403 之外；engineio 自回 400 未知会话）",
      r.status_code != 403, "实际 %s" % r.status_code)

backend.PRINTER_TOKEN = None
r = http.get(HS)
H.chk("PRINTER_TOKEN 未配置 → 失败关闭 403（不是放行）", r.status_code == 403,
      "实际 %s" % r.status_code)
backend.PRINTER_TOKEN = TOK

r = http.get("/api/ping")
H.chk("普通 HTTP 不受门影响（/api/ping 200）", r.status_code == 200)
r = http.get("/api/printer_status")
H.chk("匿名只读接口不受门影响（/api/printer_status 200）", r.status_code == 200)

# 真实 SocketIO 连接（带 token）仍然可用 —— 门没有把合法设备挡在门外
try:
    c = backend.socketio.test_client(backend.app,
                                     query_string="?token=%s&client_id=gate-dev" % TOK)
    H.chk("合法设备 Socket.IO 连接仍然成功（未回归）", c.is_connected())
    c.disconnect()
except Exception as e:  # noqa: BLE001
    H.chk("合法设备 Socket.IO 连接仍然成功（未回归）", False, repr(e))

H.chk("门计数器可用（供监控取证）",
      isinstance(backend._SIO_GATE_STATS, dict) and backend._SIO_GATE_STATS.get("rejected", 0) >= 1,
      backend._SIO_GATE_STATS)

# ============================================================
# C. 变异自证：删掉门 → 无 token 握手不再 403
# ============================================================
print("\n=== C. 变异自证（删掉门的两行赋值）===")
mut_tmp, mut_dst = H.mutated_copy([
    ("app.wsgi_app = _sio_handshake_gate(app.wsgi_app)",
     "# [mutation] gate assignment removed"),
], prefix="hn_gate_mut_")

proc = H.run_probe(mut_dst, """
r = http.get("/socket.io/?EIO=4&transport=polling")
print("RESULT=%s" % r.status_code)
""")
mut_status = H.probe_value(proc)
H.chk("删掉门后无 token 握手不再是 403（证明上面的 403 确实来自门）",
      mut_status is not None and mut_status != "403",
      "变异副本 status=%r stderr=%r" % (mut_status, (proc.stderr or "")[-300:]))

if proc.returncode != 0:
    print((proc.stderr or "")[-800:])

H.finish()
