# -*- coding: utf-8 -*-
"""验收 · 智能助手控制接口（agent_api.py / agent_ops.py / tools/agent_cli.py）

为什么要有这一套接口：DSH 私人打印助手要按顾客在微信里说好的要求，把附件填进某个
标签页、改份数/单双面/页码范围/附加服务，再回报结果。原来的入口只有「人手拖放」和
「云端任务下发」，没有可编程入口。

本脚本钉住四件事：
  ① 传输层是真的能用的：令牌、Host 校验、体积上限、超时、状态文件（端口/令牌）；
  ② 业务动作复用既有口径：页数/金额与界面逐分一致、单页硬闸门、封面页校验、
     参数不合法时把刚加进去的文件原样撤回（不留半个文件）；
  ③ 写操作总闸门：冻结标签页、正在打印、正在取号一律拒绝；
  ④ GUI 接线：`MainWindow._start_agent_api()` 真起来之后，一条 HTTP 调用能穿过
     QTimer 泵在主线程落成（这是"能用"与"看起来能用"的分界）。

跑法：python other/tests/verify_agent_api.py（退出码 0 = 全绿）
"""
import json
import os
import sys
import tempfile
import threading
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LOCAL_TOOL = os.path.join(REPO, "local_print_tool")

_SANDBOX = tempfile.mkdtemp(prefix="hn_agent_api_")
os.environ["APPDATA"] = _SANDBOX                  # 用户数据（配置/状态文件/暂存副本）→ 沙箱
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, LOCAL_TOOL)
os.chdir(LOCAL_TOOL)

from PySide6.QtWidgets import QApplication     # noqa: E402
_app = QApplication.instance() or QApplication([])

import agent_api      # noqa: E402
import agent_ops      # noqa: E402
import gui            # noqa: E402
import paths          # noqa: E402
import printer_config as pc  # noqa: E402

ok_all = True


def check(name, cond, detail=""):
    global ok_all
    if not cond:
        ok_all = False
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


def section(title):
    print(f"\n=== {title} ===")


from reportlab.lib.pagesizes import A4      # noqa: E402
from reportlab.pdfgen import canvas         # noqa: E402

SRC = os.path.join(_SANDBOX, "src")
os.makedirs(SRC, exist_ok=True)


def make_pdf(name, pages=1):
    path = os.path.join(SRC, name)
    c = canvas.Canvas(path, pagesize=A4)
    for i in range(pages):
        c.drawString(72, 760, f"{name} page {i + 1}")
        c.showPage()
    c.save()
    return path


def make_file(name, body=b"hello\n"):
    path = os.path.join(SRC, name)
    with open(path, "wb") as f:
        f.write(body)
    return path


# ══════════════════════════════════════════════════════════════════════════
# ① 传输层：真 HTTP 服务器 + 假 executor
# ══════════════════════════════════════════════════════════════════════════
section("① 传输层：令牌 / Host 校验 / 体积上限 / 超时 / 状态文件")

STATE1 = os.path.join(_SANDBOX, "api_transport.json")
seen = []


def fake_executor(op, params):
    seen.append((op, params))
    if op == "echo":
        return {"echo": params.get("x")}
    if op == "boom":
        raise agent_api.AgentOpError("invalid", "参数不对")
    if op == "crash":
        raise RuntimeError("故意炸一个")
    if op == "frozen":
        raise agent_api.AgentOpError("frozen", "标签页已冻结")
    if op == "shutdown":
        raise agent_api.AgentOpError("shutdown", "程序正在退出")
    raise agent_api.AgentOpError("unknown_op", f"未知 op: {op}")


srv = agent_api.AgentApiServer(fake_executor, port=0, state_file=STATE1,
                              app_version="test-1.0", call_timeout=5.0)
port = srv.start()
st = agent_api.read_state(STATE1)
check("①a 启动后写入状态文件（端口/令牌/pid/版本）",
      st.get("port") == port and st.get("pid") == os.getpid()
      and len(str(st.get("token") or "")) >= 32 and st.get("app_version") == "test-1.0",
      f"port={st.get('port')} tokenlen={len(str(st.get('token') or ''))}")
check("①b 只监听回环地址", st.get("host") == "127.0.0.1")
check("①c /ping 不需要令牌", (agent_api.ping(state_file=STATE1) or {}).get("ok") is True)

# 端口选择：Windows 上 SO_REUSEADDR 允许两个 socket 绑同一个端口，所以「bind 成功」
# 不等于「这个口归我」。必须先在绑定前探一次，否则请求会落到旧进程手里（表现为
# 令牌莫名其妙 unauthorized，或者旧进程停了我们却还在被应答）。
import socket as _socket      # noqa: E402

_blocker = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
_blocker.bind(("127.0.0.1", 0))
_blocker.listen(1)
_busy_port = _blocker.getsockname()[1]
STATE2 = os.path.join(_SANDBOX, "api_busy.json")
srv2 = agent_api.AgentApiServer(fake_executor, port=_busy_port, state_file=STATE2,
                                app_version="test-1.0")
port2 = srv2.start()
check("①s 首选端口被占用 → 退到系统分配口", bool(port2) and port2 != _busy_port,
      f"preferred={_busy_port} actual={port2}")
check("①s2 换口后状态文件记账的是实际端口",
      agent_api.read_state(STATE2).get("port") == port2)
check("①s3 换口后 ping 应答的是本进程（没落到别人的服务上）",
      (agent_api.ping(state_file=STATE2) or {}).get("pid") == os.getpid())
srv2.stop()
_blocker.close()

_free_sock = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
_free_sock.bind(("127.0.0.1", 0))
_free_port = _free_sock.getsockname()[1]
_free_sock.close()
STATE3 = os.path.join(_SANDBOX, "api_free.json")
srv3 = agent_api.AgentApiServer(fake_executor, port=_free_port, state_file=STATE3,
                                app_version="test-1.0")
check("①t 首选端口空闲时就用它（不白白换口）", srv3.start() == _free_port)
srv3.stop()

def call_pumped(server, op, params, timeout=10.0):
    """HTTP 线程发请求 + 本线程泵（真实程序里泵在 GUI 线程，这里就借主线程用）。"""
    box = {}

    def _worker():
        box["r"] = agent_api.request(op, params, timeout=timeout, state_file=STATE1)

    th = threading.Thread(target=_worker, daemon=True)
    th.start()
    t0 = time.time()
    while th.is_alive() and time.time() - t0 < timeout:
        server.pump()
        time.sleep(0.005)
    th.join(timeout=2)
    return box.get("r") or {"ok": False, "error": {"code": "no_response", "message": "测试线程没拿到结果"}}


def raw_pumped(server, payload, timeout=10.0):
    """同上，但走裸 HTTP 以便看清状态码（agent_api.request 只回 JSON 体）。"""
    box = {}

    def _worker():
        box["r"] = _raw_http("POST", "/api", payload,
                             headers={"X-Agent-Token": server.token}, timeout=timeout)

    th = threading.Thread(target=_worker, daemon=True)
    th.start()
    t0 = time.time()
    while th.is_alive() and time.time() - t0 < timeout:
        server.pump()
        time.sleep(0.005)
    th.join(timeout=2)
    return box.get("r") or (0, {"error": {"code": "no_response"}})


r = call_pumped(srv, "boom", {})
check("①d 业务错误透传（invalid + 原文消息）",
      r.get("ok") is False and r["error"]["code"] == "invalid"
      and "参数不对" in r["error"]["message"], json.dumps(r, ensure_ascii=False))

r = call_pumped(srv, "echo", {"x": 42})
check("①e 正常调用返回 result", r.get("ok") is True and r["result"]["echo"] == 42)

r = call_pumped(srv, "crash", {})
check("①f 未预料异常 → internal（不把栈抛给调用方）",
      r.get("ok") is False and r["error"]["code"] == "internal"
      and "RuntimeError" in r["error"]["message"])


def _raw_http(method, path, body=b"", headers=None, timeout=5.0):
    """裸 HTTP：绕过 agent_api.request，专门造非法请求（没令牌/坏 Host/超大 body）。"""
    import http.client
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    hdrs = {"Content-Type": "application/json"}
    hdrs.update(headers or {})
    conn.request(method, path, body=body, headers=hdrs)
    resp = conn.getresponse()
    raw = resp.read()
    conn.close()
    try:
        return resp.status, json.loads(raw.decode("utf-8"))
    except Exception:
        return resp.status, {"raw": raw[:200].decode("utf-8", "replace")}


status_code, data = _raw_http("POST", "/api",
                              json.dumps({"op": "echo"}).encode(),
                              headers={"X-Agent-Token": ""})
check("①g 缺令牌 → 401 unauthorized",
      status_code == 401 and data.get("error", {}).get("code") == "unauthorized",
      f"{status_code} {data}")

status_code, data = _raw_http("POST", "/api",
                              json.dumps({"op": "echo"}).encode(),
                              headers={"X-Agent-Token": "0" * 64})
check("①h 错令牌 → 401", status_code == 401, f"{status_code}")

status_code, data = _raw_http("POST", "/api",
                              json.dumps({"op": "echo"}).encode(),
                              headers={"X-Agent-Token": srv.token, "Host": "evil.example.com"})
check("①i 伪造 Host（DNS rebinding）→ 403 forbidden",
      status_code == 403 and data.get("error", {}).get("code") == "forbidden",
      f"{status_code} {data}")

status_code, data = _raw_http("POST", "/api", b"x" * (agent_api.MAX_BODY + 10),
                              headers={"X-Agent-Token": srv.token})
check("①j 超大请求体 → 413 too_large", status_code == 413, f"{status_code} {data}")

status_code, data = _raw_http("POST", "/api", b"{not json",
                              headers={"X-Agent-Token": srv.token})
check("①k 坏 JSON → 400 bad_request",
      status_code == 400 and data.get("error", {}).get("code") == "bad_request", f"{status_code}")

status_code, data = _raw_http("POST", "/api", json.dumps({"params": {}}).encode(),
                              headers={"X-Agent-Token": srv.token})
check("①l 缺 op → 400", status_code == 400, f"{status_code} {data}")

status_code, data = _raw_http("GET", "/nope", headers={"X-Agent-Token": srv.token})
check("①m 未知路径 → 404", status_code == 404, f"{status_code}")

status_code, data = raw_pumped(srv, json.dumps({"op": "nope.op"}).encode())
check("①m2 未知 op → 404（不是 400，别让调用方以为是参数错）",
      status_code == 404 and (data.get("error") or {}).get("code") == "unknown_op",
      f"{status_code} {data}")
status_code, data = raw_pumped(srv, json.dumps({"op": "frozen"}).encode())
check("①m3 状态冲突（冻结/忙）→ 409，调用方按「现在不能做」处理",
      status_code == 409 and (data.get("error") or {}).get("code") == "frozen",
      f"{status_code} {data}")
status_code, data = raw_pumped(srv, json.dumps({"op": "shutdown"}).encode())
check("①m4 程序在退出 → 503（让调用方等它起来，而不是当参数错）",
      status_code == 503 and (data.get("error") or {}).get("code") == "shutdown",
      f"{status_code} {data}")

# 超时：不泵（没人执行），call_timeout 到了必须回 timeout 而不是挂死
srv._call_timeout = 0.3
t0 = time.time()
r = agent_api.request("echo", {"x": 1}, state_file=STATE1, timeout=10)
elapsed = time.time() - t0
check("①n 主程序没及时处理 → timeout（HTTP 不挂死）",
      r.get("ok") is False and r["error"]["code"] == "timeout" and elapsed < 5,
      f"{elapsed:.2f}s {r}")
# 已经超时的排队项绝不能再迟到执行（否则人重试一次就变成加了两份文件）
seen_before = len(seen)
srv.pump()
check("①n2 过期调用被丢弃、不会迟到执行", len(seen) == seen_before,
      f"{seen_before} → {len(seen)}")
srv._call_timeout = 0.2
for _ in range(3):
    agent_api.request("echo", {"x": 9}, state_file=STATE1, timeout=5)   # 都不泵 ⇒ 排在那儿
check("①n3 pending() 真反映队列长度（status 里的 pending_calls 就是它）",
      srv.pending() == 3, f"{srv.pending()}")
srv._call_timeout = 5.0
srv.pump()
check("①n4 泵过之后队列清空", srv.pending() == 0, f"{srv.pending()}")

srv.stop()
check("①o 停止后 ping 失败（not_running）",
      agent_api.ping(state_file=STATE1, timeout=1).get("ok") is False)

# 开关默认开、显式 false 才关
check("①p 开关默认是开", agent_api.is_enabled(STATE1) is True)
agent_api.set_enabled(False, STATE1)
check("①q 显式写 false 才关", agent_api.is_enabled(STATE1) is False)
check("①r 写开关不会丢掉端口/令牌",
      agent_api.read_state(STATE1).get("port") == port
      and agent_api.read_state(STATE1).get("token") == srv.token)


# ══════════════════════════════════════════════════════════════════════════
# ② 真 MainWindow：AgentOps 直接调用（业务口径）
# ══════════════════════════════════════════════════════════════════════════
section("② 真 MainWindow：建页 / 加文件 / 改参数 / 计费口径")

# 先把控制接口开关关掉：MainWindow 的 QTimer.singleShot(1600) 会自动起服务，
# 本段要自己控制起停（下面 ③ 再单独验自动接线）。
agent_api.set_enabled(False)

win = gui.MainWindow(config_path=paths.config_path())
win.resize(1400, 900)
win.show()
_app.processEvents()

# 成员：订单必须归属成员；沙箱配置里默认没有成员
win._config.admin_names = ["测试成员"]
ops = agent_ops.AgentOps(win, app_version="test-1.0")

# 桩掉预转换线程（本脚本只验数据结构与计费，不烧 CPU 真转 PDF）
win._start_convert_worker = lambda row, file_path, engine="word", tab_key="", **kw: None

st = ops("status", {})
check("②a status 带版本/标签页数/打印机名",
      st["version"] == "test-1.0" and st["tab_count"] >= 1 and "printer_name" in st,
      json.dumps(st, ensure_ascii=False))

PDF3 = make_pdf("three.pdf", 3)
PDF1 = make_pdf("one.pdf", 1)
TXT = make_file("note.txt")

created = ops("tabs.create", {"switch": True})
tab_key = created["tab"]
check("②b 新建标签页返回 key 且已切换",
      tab_key in win._config.tabs and win._current_tab == tab_key, json.dumps(created))

r = ops("files.add", {"tab": tab_key, "paths": [PDF3, TXT]})
check("②c 一次加两个文件（PDF + TXT）", r.get("added") == 2 and len(r["jobs"]) == 2,
      json.dumps(r, ensure_ascii=False)[:300])
check("②d 3 页 PDF 的页数被读出来", r["jobs"][0]["page_count"] == 3,
      json.dumps(r["jobs"][0], ensure_ascii=False))
check("②e 暂存副本已生成（原件路径被记住）",
      r["jobs"][0]["path"] == os.path.abspath(PDF3) and r["jobs"][0]["name"] == "three.pdf")

# 计费口径：与 calc_cost 逐分一致（新加的文件用默认参数，可能是双面 —— 按它自己的值算）
expected, _ = pc.calc_cost(r["jobs"][0]["page_count"], r["jobs"][0]["copies"],
                           r["jobs"][0]["duplex"], win._config.simplex_price,
                           win._config.duplex_price, r["jobs"][0]["page_range"],
                           r["jobs"][0]["simplex_pages"] if r["jobs"][0]["simplex_enabled"] else "")
check("②f 单文件金额 == printer_config.calc_cost（同一口径，不另算一套）",
      abs(r["jobs"][0]["cost"] - round(expected, 2)) < 1e-9,
      f"job={r['jobs'][0]['cost']} calc_cost={expected}")

brief = ops("tabs.get", {"tab": tab_key})
check("②g 标签页摘要的合计金额 == 各文件之和",
      abs(brief["cost"] - round(r["jobs"][0]["cost"] + r["jobs"][1]["cost"], 2)) < 1e-9,
      f"tab={brief['cost']}")

# 界面上的合计必须与接口一致（硬规矩 7：只有一套口径）
win._update_total_cost()
label = win._total_label.text()
check("②h 界面上「合计」与接口数字一致", f"{brief['cost']:.2f}" in label, f"{label!r}")

# 参数：份数 / 页码范围 / 双面
r = ops("files.update", {"tab": tab_key, "index": 0, "copies": 2, "duplex": "on",
                         "page_range": "1-2"})
check("②i 改份数/双面/页码范围生效",
      r["copies"] == 2 and r["duplex"] == "on" and r["page_range"] == "1-2"
      and r["pages_selected"] == 2, json.dumps(r, ensure_ascii=False))

expect2, _ = pc.calc_cost(3, 2, "on", win._config.simplex_price,
                          win._config.duplex_price, "1-2", "")
check("②j 改参数后的金额仍走 calc_cost", abs(r["cost"] - round(expect2, 2)) < 1e-9,
      f"job={r['cost']} calc_cost={expect2}")

# 封面单独打印（simplex）：校验通过才写入
r = ops("files.update", {"tab": tab_key, "index": 0, "simplex_enabled": True,
                         "simplex_pages": "1"})
check("②k 封面单独打印写入成功", r["simplex_enabled"] is True and r["simplex_pages"] == "1")
try:
    ops("files.update", {"tab": tab_key, "index": 0, "simplex_enabled": True,
                         "simplex_pages": "9"})
    check("②l 封面页超出总页数被拒", False)
except agent_api.AgentOpError as e:
    check("②l 封面页超出总页数被拒（invalid）", e.code == "invalid", e.message)

# 单页硬闸门：1 页文件无论怎么请求，双面一律 off（硬规矩 1）
r = ops("files.add", {"tab": tab_key, "paths": [PDF1], "options": {"duplex": "on"}})
check("②m 1 页文件请求双面 → 生效值 off（单页硬闸门）",
      r["jobs"][0]["duplex"] == "off", json.dumps(r["jobs"][0], ensure_ascii=False))

# 参数不合法 → 回滚：不留半个文件
before = len(win._config.tabs[tab_key].jobs)
try:
    ops("files.add", {"tab": tab_key, "paths": [make_pdf("rollback.pdf", 2)],
                      "options": {"copies": 0}})
    check("②n 非法参数被拒", False)
except agent_api.AgentOpError as e:
    after = len(win._config.tabs[tab_key].jobs)
    check("②n 非法参数被拒且刚加进去的文件已撤回",
          e.code == "invalid" and after == before, f"{e.code} {before}->{after}")

# 重复添加同一个文件：既有路径直接跳过 → 明确报 rejected（不是静默成功）
try:
    ops("files.add", {"tab": tab_key, "paths": [PDF3]})
    check("②o 重复添加同一文件被明确拒绝", False)
except agent_api.AgentOpError as e:
    check("②o 重复添加同一文件被明确拒绝（rejected）", e.code == "rejected", e.message)

# 附加服务：派送 / 加急 / 首页
r = ops("tabs.update", {"tab": tab_key, "delivery_enabled": True,
                        "delivery_location": "图书馆", "urgency": "中", "cover_page": True,
                        "remark": "3 号楼 201"})
check("②i 附加服务写入成功",
      r["delivery_enabled"] and r["delivery_location"] == "图书馆"
      and r["urgency"] == "中" and r["cover_page"] and r["remark"] == "3 号楼 201",
      json.dumps(r, ensure_ascii=False))
base_only = sum(j["cost"] for j in ops("tabs.get", {"tab": tab_key})["jobs"])
check("②j 合计含附加服务（派送 10% + 加急 0.08 + 首页 0.10）",
      ops("tabs.get", {"tab": tab_key})["cost"] > base_only, f"{base_only} → tab")

# 移除文件 + 清空 + 删除标签页
n_before = len(win._config.tabs[tab_key].jobs)
r = ops("files.remove", {"tab": tab_key, "index": 0})
check("②k 移除文件后数量 -1",
      r["files"] == n_before - 1 and len(win._config.tabs[tab_key].jobs) == n_before - 1)
r = ops("tabs.clear", {"tab": tab_key})
check("②l 清空标签页保留标签页本身",
      r["removed"] >= 0 and tab_key in win._config.tabs
      and len(win._config.tabs[tab_key].jobs) == 0)
r = ops("tabs.delete", {"tab": tab_key})
check("②m 删除标签页", r["removed"] == [tab_key] and tab_key not in win._config.tabs,
      json.dumps(r))


# ══════════════════════════════════════════════════════════════════════════
# ③ 写操作总闸门 + 参数校验
# ══════════════════════════════════════════════════════════════════════════
section("③ 总闸门：冻结 / 正在打印 / 非法输入")

tab_key = ops("tabs.create", {})["tab"]
ops("files.add", {"tab": tab_key, "paths": [make_pdf("gate.pdf", 2)]})


def expect_error(name, fn, code):
    try:
        fn()
    except agent_api.AgentOpError as e:
        check(name, e.code == code, f"{e.code}: {e.message}")
        return
    except Exception as e:
        check(name, False, f"意外异常 {type(e).__name__}: {e}")
        return
    check(name, False, "没有报错")


expect_error("③a 不存在的标签页 → not_found",
             lambda: ops("tabs.get", {"tab": "999"}), "not_found")
expect_error("③b HTML 文件 → invalid",
             lambda: ops("files.add", {"tab": tab_key,
                                       "paths": [make_file("x.html", b"<html>")]}), "invalid")
expect_error("③c 不存在的文件 → invalid",
             lambda: ops("files.add", {"tab": tab_key, "paths": ["C:/nope/none.pdf"]}), "invalid")
expect_error("③d 不支持的格式（.exe）→ invalid",
             lambda: ops("files.add", {"tab": tab_key,
                                       "paths": [make_file("bad.exe", b"MZ")]}), "invalid")
expect_error("③e 未知派送地点 → invalid",
             lambda: ops("tabs.update", {"tab": tab_key, "delivery_location": "火星"}), "invalid")
expect_error("③f 未知加急档 → invalid",
             lambda: ops("tabs.update", {"tab": tab_key, "urgency": "特急"}), "invalid")
expect_error("③g 未知 op → unknown_op",
             lambda: ops("nope.op", {}), "unknown_op")
expect_error("③h 没确认就打印 → confirm_required",
             lambda: ops("tabs.print", {"tab": tab_key}), "confirm_required")
expect_error("③i 页码范围无效 → invalid",
             lambda: ops("files.update", {"tab": tab_key, "index": 0, "page_range": "abc"}),
             "invalid")

win._config.tabs[tab_key].frozen = True
expect_error("③j 冻结标签页加文件 → frozen",
             lambda: ops("files.add", {"tab": tab_key, "paths": [make_pdf("f.pdf", 1)]}),
             "frozen")
expect_error("③k 冻结标签页改附加服务 → frozen",
             lambda: ops("tabs.update", {"tab": tab_key, "urgency": "高"}), "frozen")
expect_error("③l 冻结标签页打印 → frozen",
             lambda: ops("tabs.print", {"tab": tab_key, "confirm": True}), "frozen")
win._config.tabs[tab_key].frozen = False

_orig_busy = win._stack_busy
win._stack_busy = lambda: True
expect_error("③m 打印机忙 → busy",
             lambda: ops("tabs.print", {"tab": tab_key, "confirm": True}), "busy")
win._stack_busy = _orig_busy

# 云端订单标签页（有 order_id）：附加服务锁定，但备注可改
tab = win._config.tabs[tab_key]
tab.jobs[0].order_id = 12345
expect_error("③n 云端订单标签页改派送 → cloud_locked",
             lambda: ops("tabs.update", {"tab": tab_key, "delivery_enabled": True}),
             "cloud_locked")
r = ops("tabs.update", {"tab": tab_key, "remark": "顾客要求"})
check("③o 云端订单标签页仍可写备注", r["remark"] == "顾客要求")
tab.jobs[0].order_id = None

ops("tabs.delete", {"tab": tab_key})


# ══════════════════════════════════════════════════════════════════════════
# ④ GUI 接线：真起服务 + QTimer 泵 + 一条 HTTP 调用落成
# ══════════════════════════════════════════════════════════════════════════
section("④ 接线：MainWindow._start_agent_api() + 主线程泵 + HTTP 往返")

agent_api.set_enabled(True)
check("④a set_enabled(True) 后可启动", win._start_agent_api(announce=False) is True)
check("④b 服务在跑且端口已记账",
      win._agent_api is not None and win._agent_api.running and win._agent_api.port > 0,
      f"port={getattr(win._agent_api, 'port', None)}")
st = agent_api.read_state()
check("④c 状态文件里的端口与运行中服务一致",
      st.get("port") == win._agent_api.port and len(str(st.get("token") or "")) >= 32,
      f"{st.get('port')} vs {win._agent_api.port}")


def http_call(op, params, timeout=20.0):
    """HTTP 线程发起调用；主线程反复 processEvents + pump（模拟真实事件循环）。"""
    box = {}

    def _worker():
        box["resp"] = agent_api.request(op, params, timeout=timeout)

    th = threading.Thread(target=_worker, daemon=True)
    th.start()
    t0 = time.time()
    while th.is_alive() and time.time() - t0 < timeout:
        _app.processEvents()
        if win._agent_api is not None:
            win._agent_api.pump()
        time.sleep(0.01)
    th.join(timeout=3)
    return box.get("resp") or {"ok": False, "error": {"code": "no_response",
                                                     "message": "测试线程没拿到结果"}}


r = http_call("status", {})
check("④d HTTP → 主线程 → status 成功",
      r.get("ok") is True and r["result"]["version"] == gui.APP_VERSION,
      json.dumps(r, ensure_ascii=False)[:200])
check("④d2 菜单勾选项跟着真状态走（开着就勾上）",
      win._agent_api_action is not None and win._agent_api_action.isChecked())
check("④d3 泵挂在一个 40ms 的定时器上",
      win._agent_api_timer.isActive() and win._agent_api_timer.interval() == 40,
      f"active={win._agent_api_timer.isActive()} ms={win._agent_api_timer.interval()}")


def http_call_timer_only(op, params, timeout=10.0):
    """**不手动泵**：只跑事件循环，让 40ms 定时器自己把队列泵掉——真实程序就是这条路。"""
    box = {}

    def _worker():
        box["resp"] = agent_api.request(op, params, timeout=timeout)

    th = threading.Thread(target=_worker, daemon=True)
    th.start()
    t0 = time.time()
    while th.is_alive() and time.time() - t0 < timeout:
        _app.processEvents()
        time.sleep(0.01)
    th.join(timeout=3)
    return box.get("resp") or {"ok": False, "error": {"code": "no_response",
                                                     "message": "测试线程没拿到结果"}}


r = http_call_timer_only("status", {})
check("④d4 只靠事件循环里的定时器也能跑通（不手动 pump）",
      r.get("ok") is True, json.dumps(r, ensure_ascii=False)[:200])

r = http_call("tabs.create", {"switch": True})
tab_key = (r.get("result") or {}).get("tab")
check("④e HTTP 建标签页真的落到 win 上",
      r.get("ok") is True and tab_key in win._config.tabs, json.dumps(r)[:200])

r = http_call("files.add", {"tab": tab_key, "paths": [make_pdf("http.pdf", 2)],
                            "options": {"copies": 3, "duplex": "off"}})
check("④f HTTP 加文件 + 参数一次生效",
      r.get("ok") is True and r["result"]["added"] == 1
      and r["result"]["jobs"][0]["copies"] == 3, json.dumps(r, ensure_ascii=False)[:300])

r = http_call("tabs.print", {"tab": tab_key, "confirm": False})
check("④g HTTP 路径下的 confirm 闸门同样生效",
      r.get("ok") is False and r["error"]["code"] == "confirm_required", json.dumps(r))

# 幂等探活
check("④h ping 不需要令牌就能确认程序在跑",
      agent_api.ping().get("ok") is True)

# 关掉 = 立即停止监听
win._stop_agent_api()
time.sleep(0.2)
check("④i 关掉后 ping 失败", agent_api.ping(timeout=1).get("ok") is False)
check("④j 关掉后不再起后台泵", win._agent_api is None and not win._agent_api_timer.isActive())
check("④j2 关掉后菜单文案不再说「运行中」",
      win._agent_api_action is not None and "运行中" not in win._agent_api_action.text(),
      win._agent_api_action.text() if win._agent_api_action is not None else "无菜单项")

# 菜单勾选 = 开关 + 启停（操作员唯一的入口，必须真的通）
# 先归零：④i 是代码直接停的（勾选状态=用户意图，仍是勾着的），不归零这次 setChecked(True) 不会发信号
win._agent_api_action.setChecked(False)
_app.processEvents()
check("④k0 取消勾选 → 关掉开关并停止监听",
      not agent_api.is_enabled() and win._agent_api is None)

win._agent_api_action.setChecked(True)
_app.processEvents()
check("④k 勾选菜单 → 写入开关并真的起服务",
      agent_api.is_enabled() and win._agent_api is not None and win._agent_api.running,
      f"enabled={agent_api.is_enabled()} server={win._agent_api is not None}")
check("④k2 菜单文案带上实际端口", f"端口 {win._agent_api.port}" in win._agent_api_action.text(),
      win._agent_api_action.text())
r = http_call("status", {})
check("④k3 菜单启动后照样能调用", r.get("ok") is True, json.dumps(r, ensure_ascii=False)[:150])
win._agent_api_action.setChecked(False)
_app.processEvents()
check("④l 取消勾选 → 关掉开关并停止监听",
      not agent_api.is_enabled() and win._agent_api is None)
check("④l2 取消勾选后 ping 失败", agent_api.ping(timeout=1).get("ok") is False)


# ══════════════════════════════════════════════════════════════════════════
# ⑤ 静态一致性：两处格式清单不许漂移
# ══════════════════════════════════════════════════════════════════════════
section("⑤ 静态一致性")

src = open(os.path.join(LOCAL_TOOL, "gui.py"), encoding="utf-8").read()
start = src.find("def __add_files_to_table_impl")
seg = src[start:start + 4000]
lit_start = seg.find("allowed_types = {") + len("allowed_types = {")
lit_end = seg.find("}", lit_start)
gui_exts = {p.strip().strip('"').strip("'") for p in seg[lit_start:lit_end].split(",")
            if p.strip() and "=" not in p}
gui_exts = {e for e in gui_exts if e.startswith(".")}
check("⑤a agent_ops.ALLOWED_EXTS 与 gui.py 的 allowed_types 完全一致",
      gui_exts == agent_ops.ALLOWED_EXTS,
      f"only_gui={sorted(gui_exts - agent_ops.ALLOWED_EXTS)} "
      f"only_ops={sorted(agent_ops.ALLOWED_EXTS - gui_exts)}")

cli = os.path.join(LOCAL_TOOL, "tools", "agent_cli.py")
check("⑤b 命令行工具存在且能编译", os.path.isfile(cli) and
      compile(open(cli, encoding="utf-8").read(), cli, "exec") is not None)

# GUI 文案里不许出现 markdown 星号（硬规矩 8）——本接口新加的提示语也在名单里
bad = [ln for ln in src.splitlines()
       if ("助手" in ln or "智能助手" in ln) and "**" in ln and not ln.lstrip().startswith("#")]
check("⑤c 新增文案不含 markdown 星号", not bad, str(bad[:3]))

# 关窗/退出要停监听（否则进程退出后端口还被当活着的）——取**最后一个** closeEvent（MainWindow）
close_start = src.rfind("def closeEvent")
check("⑤d MainWindow.closeEvent 里会 _stop_agent_api（退出不留监听口）",
      "_stop_agent_api" in src[close_start:close_start + 2200])

print("\n" + ("ALL PASS" if ok_all else "有 FAIL，见上"))
sys.exit(0 if ok_all else 1)
