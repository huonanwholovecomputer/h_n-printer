# -*- coding: utf-8 -*-
"""验收 · S0-06 凭据止血（token 判定收敛 / 轮换双接受 / 日志脱敏）

依据：docs/安全加固修复计划_2026-10-23.md §S0-06、DS-02/DS-08/DS-10/DS-18。

现场形状：`app.py` 里有 **26 处**内联 `token != PRINTER_TOKEN`（printer 路由 + SocketIO
鉴权）。要轮换 token 就得逐处改"双接受"，漏一处就是一台设备掉线；而 `cloud_client.py`
把 requests 的异常原文（带完整 URL、含 `?token=`）写进 `logs/local_tool.log`，
日志本身成了凭据分发面。

> 注：轮换/清场（服务器侧生成新 TOKEN、逐台更新门店 print_config.json、删
> `print_config.json.bak-*`）是**运维动作**，本脚本只钉代码侧的三件事。

本脚本：
  A. 静态接线：内联比较清零、判定统一走 `_printer_token_ok`、`TOKEN_PREV` 双接受、
     恒时比较、日志脱敏（app.py + cloud_client.py + stats_server.py 三处）。
  B. 行为：正确/错误/空/未配置/旧 token；脱敏正则只吃敏感参数。
  C. 变异自证：去掉 TOKEN_PREV 候选 → 旧 token 不再被接受（证明双接受真的生效）。

用法：python tests/verify_printer_token_helper.py     （exit 0 = 全部通过）
"""
import io
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _sec_harness as H  # noqa: E402

H.install_cleanup_hook()
raw = H.read_app_source()
code = H.strip_comments(raw)

# ============================================================
# A. 静态接线
# ============================================================
print("=== A. 静态接线 ===")
H.chk("统一判定函数 _printer_token_ok 存在", "def _printer_token_ok(" in code)
n_inline = code.count("if not PRINTER_TOKEN or token != PRINTER_TOKEN")
H.chk("内联 `if not PRINTER_TOKEN or token != PRINTER_TOKEN` 清零（原 26 处）", n_inline == 0,
      "残留 %d 处" % n_inline)
# S1-01（2026-10-23）：printer 入口的**认证**统一走 _authenticate_printer()（per-device
# token 优先、全局 TOKEN 兜底），_printer_token_ok() 只剩"全局 TOKEN 恒时比较"这一层。
n_calls = code.count("_authenticate_printer(token)")
H.chk("所有 printer 入口都改走统一认证（_authenticate_printer 调用点 ≥25 处）",
      n_calls >= 25, "实际 %d" % n_calls)
H.chk("_printer_token_ok 只留在认证入口内部（不再被各路由直接调用）",
      code.count("_printer_token_ok(") <= 4, code.count("_printer_token_ok("))
H.chk("过渡期双接受：PRINTER_TOKEN_PREV 从 config.py 的 TOKEN_PREV 读入",
      "PRINTER_TOKEN_PREV = None" in code
      and 'getattr(config, "TOKEN_PREV", PRINTER_TOKEN_PREV)' in code)
H.chk("恒时比较 hmac.compare_digest（不走按字节短路）",
      "hmac.compare_digest(" in code and "import hmac" in code)
H.chk("未配置现行 token 时失败关闭（PREV 只是补充，不能替代现行值）",
      "if not isinstance(PRINTER_TOKEN, str) or not PRINTER_TOKEN:" in code)

H.chk("app.py 自带脱敏 _scrub_secret + 正则只吃敏感查询参数",
      "def _scrub_secret(" in code and "_SCRUB_RE = re.compile(" in code)
H.chk("前端上报日志落盘前脱敏（log_report 里调用 _scrub_secret）",
      "_scrub_secret(message)" in code)

cc = os.path.join(H.REPO, "local_print_tool", "cloud_client.py")
ss = os.path.join(H.REPO, "local_print_tool", "stats_server.py")
for path, name in ((cc, "cloud_client.py"), (ss, "stats_server.py")):
    text = open(path, encoding="utf-8").read()
    H.chk("%s 有 scrub_secrets + logger 过滤器（一处覆盖全部日志点）" % name,
          "def scrub_secrets(" in text and "class _SecretScrubFilter(" in text
          and "logger.addFilter(_SecretScrubFilter())" in text)
    H.chk("%s 的正则覆盖 token/t/key/access_token" % name,
          "(?:token|t|key|access_token)" in text)

# ============================================================
# B. 行为
# ============================================================
print("\n=== B. 行为（临时后端副本）===")
TMP, DST = H.make_copy("hn_tok_")
backend = H.import_backend(DST)
backend.init_db()

backend.PRINTER_TOKEN = "new-token-abc"
backend.PRINTER_TOKEN_PREV = None
H.chk("正确 token → True", backend._printer_token_ok("new-token-abc") is True)
H.chk("错误 token → False", backend._printer_token_ok("wrong") is False)
H.chk("空 token → False", backend._printer_token_ok("") is False)
H.chk("None token → False（不抛异常）", backend._printer_token_ok(None) is False)
H.chk("非字符串 token → False（不抛异常）", backend._printer_token_ok(12345) is False)

backend.PRINTER_TOKEN_PREV = "old-token-xyz"
H.chk("轮换过渡期：旧 token 也被接受", backend._printer_token_ok("old-token-xyz") is True)
H.chk("过渡期：新 token 仍然有效", backend._printer_token_ok("new-token-abc") is True)
H.chk("过渡期的错误 token 仍被拒", backend._printer_token_ok("nope") is False)

backend.PRINTER_TOKEN = None
backend.PRINTER_TOKEN_PREV = "old-token-xyz"
H.chk("PRINTER_TOKEN 未配置 → 一律拒绝（fail-closed，旧写法同语义）",
      backend._printer_token_ok("old-token-xyz") is False
      and backend._printer_token_ok("anything") is False)

# 脱敏
samples = {
    "GET /socket.io/?token=SECRET&client_id=abc": "SECRET" not in "GET /socket.io/?token=***",
    "https://h/api?access_token=ZZZ&x=1": "ZZZ" not in "https://h/api?access_token=***&x=1",
    "?t=1792731299.7493&transport=polling": "1792731299" not in "?t=***&transport=polling",
    "?key=K123&q=keep": True,
}
out1 = backend._scrub_secret("GET /socket.io/?token=SECRET&client_id=abc")
H.chk("脱敏：token= 的值被替换", "SECRET" not in out1 and "client_id=abc" in out1, out1)
out2 = backend._scrub_secret("https://h/api?access_token=ZZZ&x=1")
H.chk("脱敏：access_token= 的值被替换", "ZZZ" not in out2 and "&x=1" in out2, out2)
out3 = backend._scrub_secret("?t=1792731299.7493&transport=polling")
H.chk("脱敏：短参数 t= 也被替换（socket.io 的时间戳参数带 token 同名风险）",
      "1792731299" not in out3 and "transport=polling" in out3, out3)
H.chk("脱敏：不含敏感参数的文本原样返回",
      backend._scrub_secret("订单 #123 打印完成") == "订单 #123 打印完成")

# 客户端过滤器（PySide6 可用时）：直接构造 LogRecord 验证
try:
    sys.path.insert(0, os.path.join(H.REPO, "local_print_tool"))
    import logging
    import cloud_client as ccmod
    rec = logging.LogRecord("x", logging.WARNING, __file__, 1,
                            "拉取失败: HTTPSConnectionPool(url=/api/x?token=LEAK&client_id=c)",
                            None, None)
    ccmod._SecretScrubFilter().filter(rec)
    H.chk("cloud_client 的 logger 过滤器改写了带 token 的记录",
          "LEAK" not in rec.getMessage() and "***" in rec.getMessage(), rec.getMessage())
    H.chk("cloud_client 的过滤器把 args 清空（避免二次格式化又把原文拼回去）",
          rec.args == ())
except Exception as e:  # noqa: BLE001
    H.chk("cloud_client 的 logger 过滤器可用（需要 PySide6）", False, repr(e))

# ============================================================
# C. 变异自证：去掉 TOKEN_PREV 候选
# ============================================================
print("\n=== C. 变异自证（去掉 TOKEN_PREV 候选）===")
mut_tmp, mut_dst = H.mutated_copy([
    ("    if isinstance(PRINTER_TOKEN_PREV, str) and PRINTER_TOKEN_PREV:\n"
     "        candidates.append(PRINTER_TOKEN_PREV)",
     "    if False:\n"
     "        candidates.append(PRINTER_TOKEN_PREV)"),
], prefix="hn_tok_mut_")
proc = H.run_probe(mut_dst, """
backend.PRINTER_TOKEN = "new-token-abc"
backend.PRINTER_TOKEN_PREV = "old-token-xyz"
print("RESULT=%s" % backend._printer_token_ok("old-token-xyz"))
""")
mut_val = H.probe_value(proc)
H.chk("去掉 TOKEN_PREV 后旧 token 被拒（证明双接受是代码里真的生效的）",
      mut_val == "False", "变异副本 %r stderr=%r" % (mut_val, (proc.stderr or "")[-300:]))

H.finish()
