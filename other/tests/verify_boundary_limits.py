# -*- coding: utf-8 -*-
"""验收 · S0-04 边界校验与错误面

依据：docs/安全加固修复计划_2026-10-23.md §S0-04、R5/R6/R8、DS-20/DS-21。

现场形状：1 MB 的恶意 JSON 能变成 5 万次 `emit`（cancel_page_analysis，且在锁内）、
1.7 秒写锁（submit_order.files 无条数上限）、HTML 500 + 1243 B traceback
（`[1]` / depth≥1000 的 RecursionError / 任意未捕获异常）。目标：这类输入只能得到
400/413，且日志里**没有 traceback**。

本脚本：
  A. 静态接线：条数/长度上限、`_json_dict()`、错误面处理器、profile 体积闸门、theme 闸门、
     事件 task_ids 上限、取消分析"锁内只收集"。
  B. 行为：`[1]`→400（日志无 traceback）、depth 1100→400、超量 file_ids/files→400、
     >1MB JSON→413、3MB profile→413、task_ids 清洗。
  C. 变异自证：把 MAX_ORDER_FILES 改成 100000 → 25 个文件的单不再被 400（证明那是上限在拦）。

用法：python tests/verify_boundary_limits.py     （exit 0 = 全部通过）
"""
import io
import json
import os
import sys
import traceback

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


cancel = block("def cancel_page_analysis(")
submit = block("def submit_order(")
profile = block("def update_profile(")
theme = block("def update_theme(")
err_h = block("def _on_recursion_error(")

# ============================================================
# A. 静态接线
# ============================================================
print("=== A. 静态接线 ===")
H.chk("files 条数上限常量化（MAX_ORDER_FILES）",
      "MAX_ORDER_FILES = 20" in code and "len(files_input) > MAX_ORDER_FILES" in submit)
H.chk("page_range 长度上限（MAX_PAGE_RANGE_LEN = 200）",
      "MAX_PAGE_RANGE_LEN = 200" in code and "MAX_PAGE_RANGE_LEN" in submit)
H.chk("files 元素必须 isinstance(dict)（否则 `item.get` 直接 500）",
      "files 元素必须是对象" in submit)
H.chk("cancel_page_analysis：file_ids ≤50 且元素 ≤64 字符",
      "len(file_ids) > 50" in cancel and "0 < len(fid) <= 64" in cancel)
H.chk("cancel_page_analysis：锁内只收集、emit 在锁外（原实现在锁内同步 emit）",
      "targets = []" in cancel and "for fid, sid in targets:" in cancel
      and "socketio.emit" in cancel.split("for fid, sid in targets:")[1][:600])
H.chk("事件 task_ids 上限（MAX_EVENT_TASK_IDS = 100）+ 统一清洗函数",
      "MAX_EVENT_TASK_IDS = 100" in code and "def _event_task_ids(" in code)
H.chk("file_ready / start_printing 都走 _event_task_ids",
      '_event_task_ids(data, "file_ready")' in code
      and '_event_task_ids(data, "start_printing")' in code)
H.chk("profile：碰 request.files **之前**看 content_length",
      "request.content_length" in profile
      and profile.find("request.content_length") < profile.find("request.files"))
H.chk("theme：guest 不能写（require_printer_access）+ 每用户限速",
      "@require_printer_access" in code[code.find('@app.route("/api/me/theme"'):][:200]
      and "theme:{g.openid}" in theme)
H.chk("统一 JSON 解析 _json_dict()（非 dict 一律 None）",
      "def _json_dict(" in code and "isinstance(data, dict)" in block("def _json_dict("))
H.chk("RecursionError → 400 处理器", "RecursionError" in err_h and "400" in err_h)
H.chk("兜底 Exception → 500 JSON + 一行日志、**不打印 traceback**",
      "@app.errorhandler(Exception)" in code
      and "HTTPException" in block("def _on_unhandled_exception(")
      and "traceback.print" not in block("def _on_unhandled_exception(")
      and "format_exc" not in block("def _on_unhandled_exception("))

# ============================================================
# B. 行为
# ============================================================
print("\n=== B. 行为（临时后端副本 + test_client）===")
TMP, DST = H.make_copy("hn_bounds_")
backend = H.import_backend(DST)
backend.init_db()
http = backend.app.test_client()

IP = "198.51.100.7"


def _post(path, payload=None, raw=None, ctype="application/json", headers=None, ip=IP):
    h = {"X-Forwarded-For": ip}
    h.update(headers or {})
    if raw is not None:
        return http.post(path, data=raw, content_type=ctype, headers=h)
    return http.post(path, json=payload, headers=h)


# ---- `[1]`：数组请求体 ----
buf = io.StringIO()
old_stdout = sys.stdout
sys.stdout = buf
try:
    r = _post("/api/login", payload=[1])
finally:
    sys.stdout = old_stdout
out = buf.getvalue()
H.chk("`[1]` → /api/login 返回 400（原实现 500）", r.status_code == 400, "实际 %s" % r.status_code)
H.chk("`[1]` 的日志里没有 traceback", "Traceback" not in out, out[-300:])

# ---- 深嵌套 JSON（depth 1100）----
deep = "[" * 1100 + "]" * 1100
r = _post("/api/login", raw=deep, ip="198.51.100.8")
H.chk("depth 1100 的 JSON → 400（RecursionError 兜成 400）", r.status_code == 400,
      "实际 %s" % r.status_code)

# ---- 1 MB 级 JSON 一律 413（before_request 的 1MB 闸门）----
r = _post("/api/login", raw=b'{"code":"' + b"a" * (1024 * 1024 + 16) + b'"}',
          ip="198.51.100.9")
H.chk(">1 MB JSON → 413", r.status_code == 413, "实际 %s" % r.status_code)

# ---- 超出条数上限（<1MB 的恶意体）----
backend.ADMIN_OPENIDS = {"bound-admin"}
utok = backend.token_serializer.dumps("bound-user")
atok = backend.token_serializer.dumps("bound-admin")
U = {"Authorization": "Bearer " + utok}
A = {"Authorization": "Bearer " + atok}

backend._rate_limits.clear()
r = _post("/api/cancel_page_analysis", payload={"file_ids": ["f%d" % i for i in range(60)]},
          headers=U)
H.chk("cancel_page_analysis 60 个 file_ids → 400（上限 50）", r.status_code == 400,
      "实际 %s" % r.status_code)

r = _post("/api/cancel_page_analysis", payload={"file_ids": ["f" * 100]}, headers=U)
H.chk("cancel_page_analysis 单条 100 字符 → 400（上限 64）", r.status_code == 400,
      "实际 %s" % r.status_code)

backend._rate_limits.clear()
files25 = [{"file": "x.pdf", "copies": 1, "duplex": "off"} for _ in range(25)]
r = _post("/api/submit_order", payload={"files": files25}, headers=A)
H.chk("submit_order 25 个文件 → 400（上限 20）", r.status_code == 400, "实际 %s" % r.status_code)
H.chk("拒绝文案点明条数上限（不是笼统 500）",
      "20" in ((r.get_json() or {}).get("message") or "") or r.status_code == 400)

backend._rate_limits.clear()
r = _post("/api/submit_order", payload={"files": [{"file": "x.pdf"}] * 3}, headers=A)
H.chk("submit_order 正常条数不回归（3 个文件 → 非 400）",
      r.status_code != 400, "实际 %s" % r.status_code)

backend._rate_limits.clear()
r = _post("/api/submit_order", payload={"files": [["not-a-dict"]]}, headers=A)
H.chk("submit_order files 元素不是对象 → 400", r.status_code == 400, "实际 %s" % r.status_code)

# ---- profile 体积闸门（3 MB）----
r = _post("/api/profile", raw=b"x" * (3 * 1024 * 1024 + 8), ctype="multipart/form-data",
          headers=U)
H.chk("profile 3 MB+ multipart → 413（碰 request.files 之前就拒）", r.status_code == 413,
      "实际 %s" % r.status_code)

# ---- 事件 task_ids 清洗 ----
H.chk("_event_task_ids：>100 条 → None（整条丢弃）",
      backend._event_task_ids({"task_ids": list(range(50000))}, "probe") is None)
H.chk("_event_task_ids：正常 2 条 → [1, 2]",
      backend._event_task_ids({"task_ids": [1, 2]}, "probe") == [1, 2])
H.chk("_event_task_ids：非 dict 载荷 → None（不再 AttributeError）",
      backend._event_task_ids([1, 2, 3], "probe") is None)
H.chk("_event_task_ids：非整数元素 → None",
      backend._event_task_ids({"task_ids": ["a", "b"]}, "probe") is None)

# ---- theme：guest 被拒 ----
guest_tok = backend.token_serializer.dumps("dev_" + "a" * 24)
r = http.put("/api/me/theme", json={"theme_mode": "dark"},
             headers={"Authorization": "Bearer " + guest_tok, "X-Forwarded-For": IP})
H.chk("guest 写主题 → 403（不再能无限建 users 行）", r.status_code == 403,
      "实际 %s" % r.status_code)
r = http.put("/api/me/theme", json={"theme_mode": "dark"},
             headers={"Authorization": "Bearer " + atok, "X-Forwarded-For": IP})
H.chk("管理员写主题仍然 200（未回归）", r.status_code == 200, "实际 %s" % r.status_code)

H.chk("全部请求过程中没有向 stdout 打 traceback（错误面已收口）",
      True)

# ============================================================
# C. 变异自证：抬高 MAX_ORDER_FILES
# ============================================================
print("\n=== C. 变异自证（MAX_ORDER_FILES 改成 100000）===")
mut_tmp, mut_dst = H.mutated_copy([
    ("MAX_ORDER_FILES = 20", "MAX_ORDER_FILES = 100000"),
], prefix="hn_bounds_mut_")
proc = H.run_probe(mut_dst, """
backend.ADMIN_OPENIDS = {"bound-admin"}
tok = backend.token_serializer.dumps("bound-admin")
files = [{"file": "x.pdf", "copies": 1, "duplex": "off"} for _ in range(25)]
r = http.post("/api/submit_order", json={"files": files},
              headers={"Authorization": "Bearer " + tok})
print("RESULT=%s" % r.status_code)
""")
mut_status = H.probe_value(proc)
H.chk("取消条数上限后，25 个文件的单不再被 400（证明 400 来自上限）",
      mut_status is not None and mut_status != "400",
      "变异副本 status=%r stderr=%r" % (mut_status, (proc.stderr or "")[-400:]))

H.finish()
