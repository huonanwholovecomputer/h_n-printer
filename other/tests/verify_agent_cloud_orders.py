# -*- coding: utf-8 -*-
"""验收 · 小印看云端订单数据（后端 GET /api/printer/orders + 本地 op cloud.orders）

为什么要有这一条：`/api/orders`、`/api/admin/recent_orders` 都走 `@login_required`
（微信会话），而本地打印工具手里只有**设备 token** —— 小印（DSH 助手）要看云端订单，
只能经由打印现场这台机器代查。于是新开一个只读口，用小印已有的 op 通道暴露出去。

本脚本钉住五件事：
  ① 后端口子只认 printer token + admin scope（没 token 一律 403），且**只读**；
  ② 「不限时间」是真不限：2020 年的老单默认也在结果里，只有显式给 since/until 才收窄；
  ③ 订单卡口径与「最近订单」同一份（页数/张数/金额/聚合状态），不是第二套算法；
  ④ 本地 op 用**设备 token**（`auth_token`）走 `X-Printer-Token` 头，绝不用已失效的
     全局 `client.token`（2026-11-04 轮换后它会 403，见 403 排查结论）；
  ⑤ 出错分得清：云没连上 → cloud_off、网络不通 → cloud_timeout、云端拒绝 → cloud_error。

跑法：python other/tests/verify_agent_cloud_orders.py（退出码 0 = 全绿）
"""
import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LOCAL_TOOL = os.path.join(REPO, "local_print_tool")
BACKEND = os.path.join(REPO, "mobile_apps", "printer-backend")

_SANDBOX = tempfile.mkdtemp(prefix="hn_cloud_orders_")
os.environ["APPDATA"] = _SANDBOX                  # 用户数据（配置/状态文件）→ 沙箱
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, LOCAL_TOOL)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _sec_harness as H          # noqa: E402  （chk / make_copy / run_probe / finish）

APP_PY = os.path.join(BACKEND, "app.py")


def read_app() -> str:
    with open(APP_PY, "r", encoding="utf-8", newline="") as f:
        return f.read()


def section(title: str) -> None:
    print(f"\n=== {title} ===")


# ============================================================
# A. 静态：代码里确实有这条口子，且没有第二套口径
# ============================================================
section("A. 静态检查")

code = read_app()


def block(start_marker: str, size: int = 4000) -> str:
    idx = code.find(start_marker)
    return code[idx:idx + size] if idx >= 0 else ""


H.chk("后端有 GET /api/printer/orders", '@app.route("/api/printer/orders", methods=["GET"])' in code)
H.chk("接口要求 admin scope（printer token）",
      'def printer_orders_view()' in code
      and '@require_printer_scope("admin")' in block('@app.route("/api/printer/orders"'))
H.chk("接口是只读的：没有 UPDATE/INSERT/DELETE",
      not any(k in block('def printer_orders_view()')
              for k in ("UPDATE ", "INSERT INTO", "DELETE FROM", "conn.commit()")))
H.chk("默认不过滤时间（不存在写死的 since/until 兜底）",
      "created_at >= ?" not in block('def printer_orders_view()')
      or "if since:" in block('def printer_orders_view()'))
H.chk("订单卡组装抽成 _order_view_dict（口径唯一）", "def _order_view_dict(" in code)
H.chk("「最近订单」也走同一个 _order_view_dict（不留第二份）",
      code.count("_order_view_dict(") >= 3      # 定义 + 两个调用点
      and "_order_view_dict(conn, r, files_by_order.get(r[\"id\"]) or [], profiles)"
      in code)
H.chk("页数/张数仍走 _file_range_stats（v7.2 唯一口径）",
      "_file_range_stats(" in block("def _order_view_dict("))
H.chk("状态用 aggregate_order_status 聚合（不直接信 orders.status）",
      "aggregate_order_status(conn, oid)" in block("def _order_view_dict("))
H.chk("summary 有金额与状态分布",
      all(k in block('def printer_orders_view()', 9000) for k in
          ('"by_status"', '"by_source"', '"amount"', '"first_at"', '"last_at"')))

import agent_ops      # noqa: E402  （A 段末尾再导，避免走在 backend 副本之后）

with open(os.path.join(LOCAL_TOOL, "agent_ops.py"), "r", encoding="utf-8") as _f:
    ops_src = _f.read()

H.chk("_OPS 注册了 cloud.orders", "cloud.orders" in agent_ops._OPS)
H.chk("op 用设备 token（auth_token），不是 client.token",
      'getattr(client, "auth_token"' in ops_src)
H.chk("op 绝不用已失效的全局口令 client.token",
      'getattr(client, "token"' not in ops_src)
H.chk("op 走 X-Printer-Token 头（复用 auth_headers）",
      "from cloud_client import auth_headers" in ops_src)

sys.path.insert(0, os.path.join(LOCAL_TOOL, "tools"))
import agent_cli      # noqa: E402

_args = agent_cli.build_parser().parse_args(
    ["cloud", "--status", "timeout", "--since", "2026-11-01", "--limit", "5"])
H.chk("CLI 有 cloud 子命令且参数能解析",
      _args.cmd == "cloud" and _args.status == "timeout" and _args.limit == 5)

# ============================================================
# B. 本地 op：透传、头部、错误码（桩服务端，不碰真实云端）
# ============================================================
section("B. 本地 op cloud.orders（桩服务端）")

HITS = []          # 每次请求的记录：(path, query, headers)


class _StubHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    payload = {}
    status = 200
    raw_body = b""

    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        path, _, query = self.path.partition("?")
        HITS.append((path, query, {k.lower(): v for k, v in self.headers.items()}))
        body = self.raw_body or json.dumps(self.payload, ensure_ascii=False).encode("utf-8")
        self.send_response(self.status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


_srv = ThreadingHTTPServer(("127.0.0.1", 0), _StubHandler)
threading.Thread(target=_srv.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{_srv.server_address[1]}"


class _StubClient:
    api_url = BASE
    auth_token = "DEVICE-TOKEN-OK"
    client_id = "R7000-AHP10-168dbdc3d1"
    token = "STALE-GLOBAL-TOKEN"      # 旧全局口令：op 绝不能用它


class _StubWin:
    _cloud_client = _StubClient()


class _OfflineWin:
    _cloud_client = None


ops = agent_ops.AgentOps(_StubWin(), "test")

_StubHandler.status = 200
_StubHandler.payload = {
    "success": True,
    "total": 2, "limit": 2, "offset": 0,
    "since": "2026-11-01 00:00:00", "until": "",
    "orders": [{"order_id": 1, "order_number": "HN20261105-0015", "status": "timeout"},
               {"order_id": 2, "order_number": "HN20261105-0016", "status": "sent"}],
    "summary": {"orders": 2, "amount": 1.6, "by_status": {"timeout": 1, "sent": 1},
                "by_source": {"wechat": 2}, "first_at": "2026-11-05 08:00:00",
                "last_at": "2026-11-05 12:08:06"},
}
HITS.clear()
res = ops("cloud.orders", {"status": "timeout,sent", "since": "2026-11-01",
                           "owner": "霍楠", "limit": 2})
H.chk("op 返回 orders/total/summary", res.get("total") == 2
      and res["orders"][0]["order_number"] == "HN20261105-0015"
      and res["summary"]["amount"] == 1.6, str(res)[:200])
H.chk("一次请求、打到 /api/printer/orders",
      len(HITS) == 1 and HITS[0][0] == "/api/printer/orders", str(HITS[:1])[:200])
_q = HITS[0][1] if HITS else ""
for frag in ("status=timeout%2Csent", "since=2026-11-01", "owner=", "limit=2"):
    H.chk(f"查询串透传 {frag}", frag in _q, _q)
_h = HITS[0][2] if HITS else {}
H.chk("带设备 token 头（不是旧全局口令）",
      _h.get("x-printer-token") == "DEVICE-TOKEN-OK", str(_h.get("x-printer-token")))
H.chk("带 X-Client-Id（订单归属靠它）",
      _h.get("x-client-id") == "R7000-AHP10-168dbdc3d1", str(_h.get("x-client-id")))


def _code_of(fn):
    try:
        fn()
    except agent_ops.AgentOpError as e:
        return e.code
    except Exception as e:                       # noqa: BLE001
        return f"{type(e).__name__}: {e}"
    return ""


H.chk("没连云端 → cloud_off",
      _code_of(lambda: agent_ops.AgentOps(_OfflineWin(), "t")("cloud.orders", {})) == "cloud_off")

_StubHandler.status = 403
_StubHandler.payload = {"success": False, "message": "token 无效"}
H.chk("云端拒绝（403）→ cloud_error 且带原文",
      _code_of(lambda: ops("cloud.orders", {})) == "cloud_error")

_StubHandler.status = 200
_StubHandler.raw_body = b"<html>not json</html>"
H.chk("非 JSON 响应 → cloud_error", _code_of(lambda: ops("cloud.orders", {})) == "cloud_error")


class _DeadClient(_StubClient):
    api_url = "http://127.0.0.1:9"      # 必然连不上（discard 端口）


class _DeadWin:
    _cloud_client = _DeadClient()


H.chk("网络不通 → cloud_timeout",
      _code_of(lambda: agent_ops.AgentOps(_DeadWin(), "t")("cloud.orders", {})) == "cloud_timeout")

H.chk("未知 op 仍然报 unknown_op",
      _code_of(lambda: ops("cloud.nope", {})) == "unknown_op")

_srv.shutdown()

# ============================================================
# C. 后端行为（临时副本 + test_client，子进程跑，免得与本地工具抢 sys.path）
# ============================================================
section("C. 后端 GET /api/printer/orders（临时副本）")

_TMP, _DST = H.make_copy("hn_cloud_orders_")

_PROBE = r'''
import json, sqlite3
R = []
def rec(name, ok, detail=""):
    R.append([name, bool(ok), str(detail)[:160]])

conn = sqlite3.connect(backend.DATABASE)
conn.row_factory = sqlite3.Row


def add_order(number, source, openid, owner, created, total, status, files,
              page_count=2, duplex="off", is_admin=0):
    cur = conn.execute(
        "INSERT INTO orders (file, copies, status, created_at, openid, duplex, page_count,"
        " total_price, order_number, source, owner_name, is_admin_print)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        ("本地打印 1 个文件", 1, status, created, openid, duplex, page_count, total,
         number, source, owner, is_admin))
    oid = cur.lastrowid
    for name, pages, copies, dup, st, reason in files:
        conn.execute(
            "INSERT INTO order_files (order_id, file_name, copies, page_count, duplex,"
            " status, created_at, page_range, reject_reason)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (oid, name, copies, pages, dup, st, created, "", reason))
    return oid


# 2020 的老单：证明「不限时间」不是嘴上说说
old_id = add_order("HN20200101-0001", "local", "local", "霍楠", "2020-01-01 10:00:00",
                   0.8, "sent", [("老单.pdf", 2, 1, "off", "sent", "")])
# 今天的三单：一单正常完成、一单超时（用户现场 HN20261105-0015 那种）、一单放弃
ok_id = add_order("HN20261105-0016", "wechat", "oA1", "闫承翰", "2026-11-05 13:00:07",
                  2.6, "sent", [("申请书.docx", 2, 1, "off", "sent", "")], page_count=2)
to_id = add_order("HN20261105-0015", "wechat", "oA2", "姚懿祥", "2026-11-05 12:08:06",
                  0.8, "timeout",
                  [("国家励志奖学金申请书.docx", 2, 1, "on", "timeout",
                    "打印机已确认，但 3 分钟内未开始打印/未回报结果，已超时")])
ab_id = add_order("HN20261030-0009", "local", "local", "霍楠", "2026-10-30 09:00:00",
                  0.4, "abandoned", [("旧单.pdf", 1, 1, "off", "abandoned", "")])
# 聚合口径用：一个订单里 sent + timeout 混合（不能给顾客看"已完成"）
mix_id = add_order("HN20261105-0100", "wechat", "oA3", "闫承翰", "2026-11-05 14:00:00",
                   1.2, "printing",
                   [("a.pdf", 1, 1, "off", "sent", ""),
                    ("b.pdf", 1, 1, "off", "timeout", "超时")])
conn.commit()
before = [tuple(r) for r in conn.execute(
    "SELECT id, status FROM orders ORDER BY id").fetchall()]
conn.close()

TOK = {"X-Printer-Token": backend.PRINTER_TOKEN, "X-Client-Id": "probe-client"}


def get(path="/api/printer/orders", headers=TOK, **params):
    r = http.get(path, query_string=params, headers=headers)
    try:
        return r.status_code, (r.get_json() or {})
    except Exception:
        return r.status_code, {}


sc, _ = get(headers={})
rec("没有 token → 403", sc == 403, sc)

sc, d = get()
rec("默认返回全部 5 单（含 2020 老单 = 不限时间）",
    sc == 200 and d.get("total") == 5
    and any(o["order_number"] == "HN20200101-0001" for o in d.get("orders") or []),
    f"{sc} total={d.get('total')}")
rec("summary 金额 = 各行 total_price 之和",
    abs(float((d.get("summary") or {}).get("amount") or 0) - 5.8) < 1e-6,
    (d.get("summary") or {}).get("amount"))
rec("summary 有状态/来源分布",
    (d.get("summary") or {}).get("by_status", {}).get("sent") == 2
    and (d.get("summary") or {}).get("by_source", {}).get("local") == 2,
    (d.get("summary") or {}))

sc, d = get(since="2026-11-01")
rec("since=2026-11-01 收窄（2020 老单与 10-30 那单都被排除）",
    d.get("total") == 3
    and not any(o["order_number"] in ("HN20200101-0001", "HN20261030-0009")
                for o in d.get("orders") or []),
    d.get("total"))
sc, d2 = get(since="2026-11-05", until="2026-11-05")
rec("until 只给日期时含整天（当天 3 单全在）",
    d2.get("total") == 3, d2.get("total"))

sc, d = get(status="timeout")
rec("status=timeout 只回超时单（两单都是真的 timeout）",
    d.get("total") == 2
    and {o.get("order_number") for o in d.get("orders") or []}
    == {"HN20261105-0015", "HN20261105-0100"},
    d.get("total"))

sc, d = get(order="HN20200101-0001")
rec("order=订单号 精确命中 1 单",
    d.get("total") == 1 and (d.get("orders") or [{}])[0].get("order_id") == old_id,
    d.get("total"))
o = (d.get("orders") or [{}])[0]
rec("订单卡字段与「最近订单」同一份（页数/张数/金额/状态）",
    o.get("status") == "sent" and o.get("page_count") == 2 and o.get("sheet_count") == 2
    and abs(float(o.get("total_price") or 0) - 0.8) < 1e-6
    and (o.get("files") or [{}])[0].get("file_name") == "老单.pdf",
    json.dumps(o, ensure_ascii=False)[:200])
rec("归属真名 + 脱敏名都给（前端只展示脱敏）",
    o.get("owner") == "霍楠" and o.get("owner_masked", "").startswith("霍"),
    f"{o.get('owner')} / {o.get('owner_masked')}")

sc, d = get(order=str(to_id))
o = (d.get("orders") or [{}])[0]
rec("订单级原因来自子任务 reject_reason",
    "3 分钟内未开始打印" in str(o.get("status_reason") or ""), o.get("status_reason"))

sc, d = get(limit=1, offset=1)
rec("分页：total 仍是全量、返回 1 条",
    d.get("total") == 5 and len(d.get("orders") or []) == 1
    and d.get("limit") == 1 and d.get("offset") == 1,
    f"total={d.get('total')} n={len(d.get('orders') or [])}")

sc, d = get(file="奖学金")
rec("file= 按子任务文件名模糊命中",
    d.get("total") == 1 and (d.get("orders") or [{}])[0].get("order_id") == to_id,
    d.get("total"))
sc, d = get(owner="霍楠")
rec("owner= 模糊命中两个人的单",
    d.get("total") == 2, d.get("total"))
sc, d = get(source="wechat")
rec("source=wechat 只回云端单",
    d.get("total") == 3, d.get("total"))
sc, d = get(since="昨天")
rec("时间格式非法 → 400 并说清楚", sc == 400, sc)
sc, d = get(limit="abc")
rec("limit 非法 → 400", sc == 400, sc)
sc, d = get(limit=99999)
rec("limit 上限夹到 1000（不吃爆内存）", d.get("limit") == 1000, d.get("limit"))

# 聚合口径：一个订单里 sent + timeout 混合 ⇒ 显示超时（不能给顾客看"已完成"）
sc, d = get(order=str(mix_id))
rec("混合终态聚合为 timeout（沿用 aggregate_order_status）",
    (d.get("orders") or [{}])[0].get("status") == "timeout",
    (d.get("orders") or [{}])[0].get("status"))

# 只读：这一串调用之后库里的行与状态一字未改
conn = sqlite3.connect(backend.DATABASE)
after = [tuple(r) for r in conn.execute(
    "SELECT id, status FROM orders ORDER BY id").fetchall()]
conn.close()
rec("只读：调用前后 orders 的 id/status 完全一致",
    after == [b for b in before], f"before={before} after={after}")

print("RESULT=" + json.dumps(R, ensure_ascii=False))
'''

proc = H.run_probe(_DST, _PROBE)
lines = [ln for ln in (proc.stdout or "").splitlines() if ln.startswith("RESULT=")]
if not lines:
    print(proc.stdout[-2000:])
    print(proc.stderr[-2000:])
    H.chk("后端探针跑通（有 RESULT 行）", False, f"exit={proc.returncode}")
else:
    for name, ok, detail in json.loads(lines[0][len("RESULT="):]):
        H.chk(name, ok, detail)

# 变异自证：把「不限时间」改成默认近 30 天，本脚本必须能红
_mut_tmp, _mut_dst = H.mutated_copy([
    ('    where, params = [], []\n    order_arg = (request.args.get("order") or "").strip()',
     '    where, params = [], []\n    where.append("created_at >= ?")\n'
     '    params.append("2026-10-06 00:00:00")\n'
     '    order_arg = (request.args.get("order") or "").strip()'),
], prefix="hn_cloud_orders_mut_")
_proc2 = H.run_probe(_mut_dst, _PROBE)
_l2 = [ln for ln in (_proc2.stdout or "").splitlines() if ln.startswith("RESULT=")]
_hidden = False
if _l2:
    for name, ok, _detail in json.loads(_l2[0][len("RESULT="):]):
        if name.startswith("默认返回全部") and not ok:
            _hidden = True
H.chk("变异自证：默认时间窗一加，老单就查不到（脚本会红）", _hidden)

H.finish()
