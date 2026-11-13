# -*- coding: utf-8 -*-
"""外部监视单「订单状态订正」验收（v7.16，2026-11-03 用户需求）。

用户原话：「外部监视的订单改不了状态？需要允许调整状态。」
背景：外部监视单（print_watch 账本里人工确认「已处理」的作业）被并进收支清算的「订单明细」，
但它们**不在云端 orders 表**里，云端写接口 /api/admin/order_status 按 id 找不到这单 ——
v7.3 起前端干脆把状态列锁死。现在改成：订正写**本地账本**的 manual_status*（不改监视状态
status），行序列化时覆盖订单状态，结算口径只认 files[].status === 'sent' ⇒ 改了就自动不计钱。

不需要打印机、不起 HTTP 服务、一次真网都不发；账本全落临时目录。

必须钉住的事：
  ① 账本层：MANUAL_STATUSES 只放终态；from_dict 净化脏值；set_manual_status 白名单 + 与 status 正交；
  ② 解析层：parse_external_order_id 只认 `ext:<十进制作业号>:<非空检测时间>`；
  ③ 纯逻辑：set_external_order_status 的四类拒绝 + 成功返回旧状态（页面乐观更新要用）；
  ④ 序列化：订正后订单行 status / files[0].status 一起变、带标记人留痕；脏值不外流；
  ⑤ HTTP 接线：路由登记 + 财务写模块令牌 + handler 的 400/404/500 与审计；
  ⑥ 三处白名单同源：print_watch ↔ settlement.html ↔ 云端 app.py（少一处就会「页面能选、后端不认」）。

用法: python tests/verify_external_order_status.py   （exit 0 = 全部通过）
"""
import io
import json
import os
import re
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LOCAL_TOOL = os.path.join(REPO, "local_print_tool")
sys.path.insert(0, LOCAL_TOOL)

import print_watch as pw            # noqa: E402
import stats_server as ss           # noqa: E402

SIMPLEX, DUPLEX = 0.3, 0.4
TMP = tempfile.mkdtemp(prefix="hn_ext_status_verify_")
HTML_PATH = os.path.join(LOCAL_TOOL, "finance", "settlement.html")
APP_PATH = os.path.join(REPO, "mobile_apps", "printer-backend", "app.py")

ok_all = True
PASSED = 0
FAILED = 0


def check(name, cond, detail=""):
    global ok_all, PASSED, FAILED
    if cond:
        PASSED += 1
    else:
        ok_all = False
        FAILED += 1
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))
    return cond


def section(title):
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


def item(job_id=1, document="三页文档测试.pdf", pages=3, duplex=pw.DUPLEX_ON, copies=1,
         status=pw.STATUS_CONFIRMED, detected_at="2026-10-28 18:00:13",
         manual_status="", manual_status_by="", manual_status_at=""):
    """造一条账本记录（ExternalPrint 是账本的真实数据模型，不另写假对象）。"""
    return pw.ExternalPrint(job_id=job_id, printer="HP LaserJet 1020", document=document,
                            user="顾客", pages=pages, pages_source=pw.SOURCE_CONFIRMED,
                            duplex=duplex, color=pw.COLOR_MONO, copies=copies,
                            size_bytes=1024, detected_at=detected_at, status=status,
                            manual_status=manual_status, manual_status_by=manual_status_by,
                            manual_status_at=manual_status_at)


def write_ledger(tag, items):
    """用真实的 ExternalPrintStore 落一份临时账本（走真实写盘路径，不 mock）。"""
    path = os.path.join(TMP, tag + ".json")
    if os.path.exists(path):
        os.remove(path)
    store = pw.ExternalPrintStore(path)
    for it in items:
        store.add(it)
    return path


def read_html():
    with open(HTML_PATH, "r", encoding="utf-8") as f:
        return f.read()


def read_app():
    with open(APP_PATH, "r", encoding="utf-8") as f:
        return f.read()


# ══════════════════════════════════════════════════════════════════
section("① 账本层：终态白名单 / 脏值净化 / 订正与监视状态正交")
# ══════════════════════════════════════════════════════════════════
check("① MANUAL_STATUSES 就是云端同款五条终态（顺序也一致）",
      tuple(pw.MANUAL_STATUSES) == ("sent", "failed", "abandoned", "canceled", "rejected"),
      repr(pw.MANUAL_STATUSES))
check("① 监视状态与订单状态是两套常量（订正不会把作业标成 queued/printing）",
      "queued" not in pw.MANUAL_STATUSES and "printing" not in pw.MANUAL_STATUSES
      and pw.STATUS_CONFIRMED in pw.VALID_STATUSES,
      json.dumps({"monitor": list(pw.VALID_STATUSES), "manual": list(pw.MANUAL_STATUSES)}))

_dirty = pw.ExternalPrint.from_dict({
    "job_id": 7, "document": "a.pdf", "pages": 2, "duplex": "off",
    "detected_at": "2026-10-28 10:00:00", "status": pw.STATUS_CONFIRMED,
    "manual_status": "printing", "manual_status_by": 123, "manual_status_at": None,
})
check("① 脏 manual_status（不在白名单）在 from_dict 里被清空 —— 手改账本脏值不会流进结算",
      _dirty.manual_status == "", repr(_dirty.manual_status))
check("① manual_status_by/at 一律转成字符串（None 变空串）",
      isinstance(_dirty.manual_status_by, str) and isinstance(_dirty.manual_status_at, str)
      and _dirty.manual_status_at == "", repr((_dirty.manual_status_by, _dirty.manual_status_at)))
_good = pw.ExternalPrint.from_dict({"job_id": 8, "detected_at": "x", "status": pw.STATUS_CONFIRMED,
                                    "manual_status": "failed", "manual_status_by": "霍楠"})
check("① 合法 manual_status 原样保留（并且不碰监视 status）",
      _good.manual_status == "failed" and _good.manual_status_by == "霍楠"
      and _good.status == pw.STATUS_CONFIRMED, repr((_good.manual_status, _good.status)))

_path_a = write_ledger("manual_a", [item(job_id=21, detected_at="2026-10-28 09:00:00")])
_store_a = pw.ExternalPrintStore(_path_a)
try:
    _store_a.set_manual_status(0, "printing")
    _raised = False
except ValueError:
    _raised = True
check("① set_manual_status 拒绝非白名单状态（ValueError，不静默写坏）", _raised)

_upd = _store_a.set_manual_status(0, "failed", by="霍楠", at="2026-11-03 23:20:00")
check("① set_manual_status 返回更新后的对象（页面乐观更新要用）",
      _upd is not None and _upd.manual_status == "failed" and _upd.manual_status_by == "霍楠",
      repr(getattr(_upd, "manual_status", None)))
_reloaded = pw.ExternalPrintStore(_path_a).load(force=True)[0]
check("① 订正真的落盘：重新读账本拿到 manual_status=manual_status_by/at",
      _reloaded.manual_status == "failed" and _reloaded.manual_status_by == "霍楠"
      and _reloaded.manual_status_at == "2026-11-03 23:20:00",
      json.dumps([_reloaded.manual_status, _reloaded.manual_status_by, _reloaded.manual_status_at],
                 ensure_ascii=False))
check("① 订正**不动**监视状态：status 仍是 confirmed（待处理/已确认/已忽略那条线归监视流程）",
      _reloaded.status == pw.STATUS_CONFIRMED, _reloaded.status)

_store_a.set_status(0, pw.STATUS_IGNORED)
_after_monitor = pw.ExternalPrintStore(_path_a).load(force=True)[0]
check("① 监视路径改 status 不会冲掉人工订正（两条写路径互不覆盖，刷新账本也是）",
      _after_monitor.status == pw.STATUS_IGNORED and _after_monitor.manual_status == "failed",
      json.dumps([_after_monitor.status, _after_monitor.manual_status], ensure_ascii=False))


# ══════════════════════════════════════════════════════════════════
section("② 解析层：parse_external_order_id 只认 ext:<十进制作业号>:<非空检测时间>")
# ══════════════════════════════════════════════════════════════════
check("② 合法 id → (job_id, detected_at)",
      ss.parse_external_order_id("ext:8:2026-10-28 18:00:13") == (8, "2026-10-28 18:00:13"),
      repr(ss.parse_external_order_id("ext:8:2026-10-28 18:00:13")))
for _bad in ("", "8", "HN20261028-0001", "ext:", "ext:8", "ext:8:", "ext:abc:2026-10-28 18:00:13",
             "ext:8:   ", None, 123):
    check("② 非法 id 一律 None：%r" % (_bad,), ss.parse_external_order_id(_bad) is None,
          repr(ss.parse_external_order_id(_bad)))


# ══════════════════════════════════════════════════════════════════
section("③ 纯逻辑：set_external_order_status 的四类拒绝 + 成功回旧状态")
# ══════════════════════════════════════════════════════════════════
_path_b = write_ledger("logic_b", [
    item(job_id=31, detected_at="2026-10-28 11:00:00"),
    item(job_id=32, detected_at="2026-10-28 11:30:00", status=pw.STATUS_NEW),
])
_store_b = pw.ExternalPrintStore(_path_b)
_oid31 = "ext:31:2026-10-28 11:00:00"

ok, code, message, info = ss.set_external_order_status(_store_b, _oid31, "queued")
check("③ 非终态（queued）被拒：code=bad_status，消息里列出允许值",
      ok is False and code == "bad_status" and "failed" in message, message)

ok, code, message, info = ss.set_external_order_status(_store_b, "8", "failed")
check("③ 非法订单号被拒：code=bad_order_id", ok is False and code == "bad_order_id", message)

ok, code, message, info = ss.set_external_order_status(
    _store_b, "ext:32:2026-10-28 11:30:00", "failed")
check("③ 还没确认「已处理」的作业不给订正：code=not_confirmed（连钱都没算，谈不上结算状态）",
      ok is False and code == "not_confirmed", message)

ok, code, message, info = ss.set_external_order_status(
    _store_b, "ext:999:2026-10-28 11:00:00", "failed")
check("③ 账本里没有这单：code=not_found", ok is False and code == "not_found", message)

ok, code, message, info = ss.set_external_order_status(_store_b, _oid31, "failed", by="霍楠")
check("③ 成功：ok=True 且回 order_id / status / previous_status（页面拿它做乐观更新）",
      ok is True and code == "" and info.get("order_id") == _oid31
      and info.get("status") == "failed" and info.get("previous_status") == "sent",
      json.dumps(info, ensure_ascii=False))
check("③ 成功回执带标记人与时间（留痕，不靠前端自己补）",
      info.get("manual_status_by") == "霍楠" and bool(info.get("manual_status_at")),
      json.dumps({k: info.get(k) for k in ("manual_status_by", "manual_status_at")},
                 ensure_ascii=False))

ok, code, message, info = ss.set_external_order_status(_store_b, _oid31, "canceled", by="霍楠")
check("③ 再订正一次：previous_status 是上一次的订正结果（不是永远回 sent）",
      ok is True and info.get("previous_status") == "failed",
      json.dumps(info, ensure_ascii=False))
_reload_b = pw.ExternalPrintStore(_path_b).load(force=True)
check("③ 两次订正只动同一条记录，另一条不受影响",
      _reload_b[0].manual_status == "canceled" and _reload_b[1].manual_status == "",
      json.dumps([i.manual_status for i in _reload_b], ensure_ascii=False))


# ══════════════════════════════════════════════════════════════════
section("④ 序列化：订单行 / files[0].status 一起变，脏值不外流")
# ══════════════════════════════════════════════════════════════════
_rows, _skipped = ss.build_external_orders(
    [item(job_id=41, detected_at="2026-10-28 12:00:00", manual_status="failed",
          manual_status_by="霍楠", manual_status_at="2026-11-03 23:30:00"),
     item(job_id=42, detected_at="2026-10-28 12:30:00")],
    SIMPLEX, DUPLEX, "张三")
_by_id = {o["order_id"]: o for o in _rows}
_row_fixed = _by_id["ext:41:2026-10-28 12:00:00"]
check("④ 订正过的行：订单 status 与 files[0].status 都变成 failed（结算口径只认文件级状态）",
      _row_fixed["status"] == "failed" and _row_fixed["files"][0]["status"] == "failed",
      json.dumps({k: _row_fixed.get(k) for k in ("status",) }, ensure_ascii=False))
check("④ 行里带标记人留痕（manual_status_by / manual_status_at，订单级与文件级都有）",
      _row_fixed.get("manual_status_by") == "霍楠"
      and _row_fixed.get("manual_status_at") == "2026-11-03 23:30:00"
      and _row_fixed["files"][0].get("manual_status_by") == "霍楠",
      json.dumps({k: _row_fixed.get(k) for k in ("manual_status_by", "manual_status_at")},
                 ensure_ascii=False))
_row_plain = _by_id["ext:42:2026-10-28 12:30:00"]
check("④ 没订正过的行仍是 sent（行为不变，老账本零迁移）",
      _row_plain["status"] == "sent" and _row_plain["files"][0]["status"] == "sent"
      and _row_plain["manual_status_by"] == "",
      json.dumps([_row_plain["status"], _row_plain["manual_status_by"]]))

_dirty_row = ss.build_external_orders(
    [item(job_id=43, detected_at="2026-10-28 13:00:00", manual_status="weird")],
    SIMPLEX, DUPLEX, "张三")[0][0]
check("④ 绕过 from_dict 的脏 manual_status 也不会流到行里（行序列化再兜一层）",
      _dirty_row["status"] == "sent" and _dirty_row["files"][0]["status"] == "sent",
      json.dumps([_dirty_row["status"], _dirty_row["files"][0]["status"]]))


# ══════════════════════════════════════════════════════════════════
section("⑤ HTTP 接线：路由登记 / 写模块令牌 / handler 的状态码与审计")
# ══════════════════════════════════════════════════════════════════
with open(os.path.join(LOCAL_TOOL, "stats_server.py"), "r", encoding="utf-8") as f:
    SERVER_SRC = f.read()
check("⑤ do_POST 里挂了 /api/local/external_prints/status → _handle_post_external_status",
      re.search(r'if path == "/api/local/external_prints/status":\s*\n\s*return self\.'
                r'_handle_post_external_status\(\)', SERVER_SRC) is not None)
check("⑤ 财务写模块登记为 orderstatus（与云端订正同一把锁，未登记写路由是 fail-closed）",
      ss.FINANCE_WRITE_MODULES.get("/api/local/external_prints/status") == "orderstatus",
      repr(ss.FINANCE_WRITE_MODULES.get("/api/local/external_prints/status")))
check("⑤ 订正入口在 FINANCE_MODULES 里也认 orderstatus（解锁这一模块即可写）",
      "orderstatus" in ss.FINANCE_MODULES, repr(ss.FINANCE_MODULES))


class _FakeHandler(object):
    """只实现 handler 用到的四样东西：_read_body / _send_json / _fin().audit / 账本覆盖。"""

    client_id = "cid-fake"
    api_url = "http://stub.invalid"
    token = "tok-fake"

    def __init__(self, body):
        self._body = body
        self.sent = None
        self.audits = []

    def _read_body(self):
        return self._body

    def _send_json(self, data, status=200):
        self.sent = (data, status)

    def _fin(self):
        return self

    def audit(self, event, module="", fields=None, extra=None):
        self.audits.append({"event": event, "module": module,
                            "fields": fields or {}, "extra": extra or {}})


def call_handler(body):
    h = _FakeHandler(body)
    ss._StatsHandler._handle_post_external_status(h)
    return h


_path_c = write_ledger("http_c", [
    item(job_id=51, detected_at="2026-10-28 14:00:00"),
    item(job_id=52, detected_at="2026-10-28 14:30:00", status=pw.STATUS_NEW),
])
_orig_override = ss.EXTERNAL_LEDGER_OVERRIDE
ss.EXTERNAL_LEDGER_OVERRIDE = _path_c
try:
    _h = call_handler(json.dumps({"order_id": "ext:51:2026-10-28 14:00:00",
                                  "status": "failed", "by": "霍楠"}).encode("utf-8"))
    _payload, _code = _h.sent or ({}, None)
    check("⑤ 成功：200 + success=True + previous_status（页面据此乐观更新）",
          _code == 200 and _payload.get("success") is True
          and _payload.get("previous_status") == "sent",
          json.dumps({"code": _code, "payload": _payload}, ensure_ascii=False))
    check("⑤ 成功也写审计：event=external_order_fix / module=orderstatus / 含前后状态与标记人",
          len(_h.audits) == 1 and _h.audits[0]["event"] == "external_order_fix"
          and _h.audits[0]["module"] == "orderstatus"
          and _h.audits[0]["fields"].get("before") == "sent"
          and _h.audits[0]["fields"].get("status") == "failed"
          and _h.audits[0]["extra"].get("success") is True,
          json.dumps(_h.audits, ensure_ascii=False))

    _h2 = call_handler(json.dumps({"order_id": "ext:52:2026-10-28 14:30:00",
                                   "status": "failed"}).encode("utf-8"))
    _p2, _c2 = _h2.sent or ({}, None)
    check("⑤ 未确认的作业：400 + code=not_confirmed（业务拒绝，不是 500）",
          _c2 == 400 and _p2.get("code") == "not_confirmed",
          json.dumps({"code": _c2, "payload": _p2}, ensure_ascii=False))
    check("⑤ 失败也进审计（success=False + code），事后能查谁试过",
          len(_h2.audits) == 1 and _h2.audits[0]["extra"].get("success") is False
          and _h2.audits[0]["extra"].get("code") == "not_confirmed",
          json.dumps(_h2.audits, ensure_ascii=False))

    _h3 = call_handler(json.dumps({"order_id": "ext:999:2026-10-28 14:00:00",
                                   "status": "failed"}).encode("utf-8"))
    _p3, _c3 = _h3.sent or ({}, None)
    check("⑤ 找不到的单：404 + code=not_found", _c3 == 404 and _p3.get("code") == "not_found",
          json.dumps({"code": _c3, "payload": _p3}, ensure_ascii=False))

    _h4 = call_handler(json.dumps({"order_id": "ext:51:2026-10-28 14:00:00",
                                   "status": "printing"}).encode("utf-8"))
    _p4, _c4 = _h4.sent or ({}, None)
    check("⑤ 非终态：400 + code=bad_status（白名单在后端也拦一次）",
          _c4 == 400 and _p4.get("code") == "bad_status",
          json.dumps({"code": _c4, "payload": _p4}, ensure_ascii=False))

    _h5 = call_handler(b"{not json")
    _p5, _c5 = _h5.sent or ({}, None)
    check("⑤ 坏 JSON：400 + code=bad_json（不乱抛、不 500）",
          _c5 == 400 and _p5.get("code") == "bad_json",
          json.dumps({"code": _c5, "payload": _p5}, ensure_ascii=False))

    _h6 = call_handler(b'["not-an-object"]')
    _p6, _c6 = _h6.sent or ({}, None)
    check("⑤ 请求体不是对象：400 + code=bad_json",
          _c6 == 400 and _p6.get("code") == "bad_json",
          json.dumps({"code": _c6, "payload": _p6}, ensure_ascii=False))

    _h7 = call_handler(json.dumps({"order_id": "ext:51:2026-10-28 14:00:00",
                                   "status": "abandoned", "by": "x" * 80}).encode("utf-8"))
    _p7, _c7 = _h7.sent or ({}, None)
    check("⑤ 由 failed 改成 abandoned：previous_status 跟上一条订正结果",
          _c7 == 200 and _p7.get("previous_status") == "failed",
          json.dumps({"code": _c7, "previous": _p7.get("previous_status")}, ensure_ascii=False))
    check("⑤ 标记人长度被截断（by 最多 32 字符，防超长写入账本/审计）",
          len(_p7.get("manual_status_by") or "") == 32,
          str(len(_p7.get("manual_status_by") or "")))
finally:
    ss.EXTERNAL_LEDGER_OVERRIDE = _orig_override


# ══════════════════════════════════════════════════════════════════
section("⑤b v7.20：外部监视单「用户 / 类型」订正（HTTP 层）")
# ══════════════════════════════════════════════════════════════════
# 现场（2026-11-13 用户）：「本地监视的任务不能调整这两个内容？需要能够调整才行。」
check("⑤b do_POST 里挂了 /api/local/external_prints/meta → _handle_post_external_meta",
      re.search(r'path == "/api/local/external_prints/meta"', SERVER_SRC) is not None
      and re.search(r'_handle_post_external_meta\(\)', SERVER_SRC) is not None)
check("⑤b 元数据订正的写权限与状态订正同源（FINANCE_WRITE_MODULES → orderstatus）",
      ss.FINANCE_WRITE_MODULES.get("/api/local/external_prints/meta") == "orderstatus",
      repr(ss.FINANCE_WRITE_MODULES.get("/api/local/external_prints/meta")))


def call_meta(body):
    h = _FakeHandler(body)
    ss._StatsHandler._handle_post_external_meta(h)
    return h


_path_m = write_ledger("http_meta", [
    item(job_id=61, detected_at="2026-10-28 15:00:00"),
    item(job_id=62, detected_at="2026-10-28 15:30:00", status=pw.STATUS_NEW),
])
_orig_override_m = ss.EXTERNAL_LEDGER_OVERRIDE
ss.EXTERNAL_LEDGER_OVERRIDE = _path_m
try:
    _m1 = call_meta(json.dumps({"order_id": "ext:61:2026-10-28 15:00:00",
                                "owner_name": "霍楠"}).encode("utf-8"))
    _mp1, _mc1 = _m1.sent or ({}, None)
    check("⑤b 改用户：200 + success + 回传 owner_name",
          _mc1 == 200 and _mp1.get("success") is True and _mp1.get("owner_name") == "霍楠",
          json.dumps({"code": _mc1, "payload": _mp1}, ensure_ascii=False))
    check("⑤b 改用户也写审计：event=external_order_meta / module=orderstatus / 带 owner_name",
          len(_m1.audits) == 1 and _m1.audits[0]["event"] == "external_order_meta"
          and _m1.audits[0]["module"] == "orderstatus"
          and _m1.audits[0]["fields"].get("owner_name") == "霍楠"
          and _m1.audits[0]["extra"].get("success") is True,
          json.dumps(_m1.audits, ensure_ascii=False))

    _m2 = call_meta(json.dumps({"order_id": "ext:61:2026-10-28 15:00:00",
                                "order_type": "self"}).encode("utf-8"))
    _mp2, _mc2 = _m2.sent or ({}, None)
    check("⑤b 改类型为自打：200 + order_type=self",
          _mc2 == 200 and _mp2.get("order_type") == "self",
          json.dumps({"code": _mc2, "payload": _mp2}, ensure_ascii=False))

    _m3 = call_meta(json.dumps({"order_id": "ext:61:2026-10-28 15:00:00",
                                "order_type": "canceled"}).encode("utf-8"))
    _mp3, _mc3 = _m3.sent or ({}, None)
    check("⑤b 非法类型：400 + code=bad_type", _mc3 == 400 and _mp3.get("code") == "bad_type",
          json.dumps({"code": _mc3, "payload": _mp3}, ensure_ascii=False))

    _m4 = call_meta(json.dumps({"order_id": "ext:61:2026-10-28 15:00:00",
                                "owner_name": "   "}).encode("utf-8"))
    _mp4, _mc4 = _m4.sent or ({}, None)
    check("⑤b 空所属人：400 + code=bad_owner", _mc4 == 400 and _mp4.get("code") == "bad_owner",
          json.dumps({"code": _mc4, "payload": _mp4}, ensure_ascii=False))

    _m5 = call_meta(json.dumps({"order_id": "ext:62:2026-10-28 15:30:00",
                                "owner_name": "霍楠"}).encode("utf-8"))
    _mp5, _mc5 = _m5.sent or ({}, None)
    check("⑤b 未确认的作业：400 + code=not_confirmed",
          _mc5 == 400 and _mp5.get("code") == "not_confirmed",
          json.dumps({"code": _mc5, "payload": _mp5}, ensure_ascii=False))

    _m6 = call_meta(json.dumps({"order_id": "ext:999:2026-10-28 15:00:00",
                                "order_type": "receive"}).encode("utf-8"))
    _mp6, _mc6 = _m6.sent or ({}, None)
    check("⑤b 找不到的单：404 + code=not_found", _mc6 == 404 and _mp6.get("code") == "not_found",
          json.dumps({"code": _mc6, "payload": _mp6}, ensure_ascii=False))

    _m7 = call_meta(b"{not json")
    _mp7, _mc7 = _m7.sent or ({}, None)
    check("⑤b 坏 JSON：400 + code=bad_json（不 500）",
          _mc7 == 400 and _mp7.get("code") == "bad_json",
          json.dumps({"code": _mc7, "payload": _mp7}, ensure_ascii=False))
finally:
    ss.EXTERNAL_LEDGER_OVERRIDE = _orig_override_m


# ══════════════════════════════════════════════════════════════════
section("⑥ 三处白名单同源（print_watch ↔ settlement.html ↔ 云端 app.py）")
# ══════════════════════════════════════════════════════════════════
_html = read_html()
_app = read_app()
_m_html = re.search(r"var ORDER_FIXABLE_STATUSES = \[([^\]]*)\];", _html)
_html_statuses = tuple(re.findall(r"'([a-z_]+)'", _m_html.group(1))) if _m_html else ()
_m_app = re.search(r"MANUAL_FIX_STATUSES = \(([^)]*)\)", _app)
_app_statuses = tuple(re.findall(r'"([a-z_]+)"', _m_app.group(1))) if _m_app else ()
check("⑥ 前端 ORDER_FIXABLE_STATUSES 与账本 MANUAL_STATUSES 同一份清单",
      _html_statuses == tuple(pw.MANUAL_STATUSES),
      json.dumps({"html": _html_statuses, "ledger": list(pw.MANUAL_STATUSES)}))
check("⑥ 云端后端 MANUAL_FIX_STATUSES 也同一份清单（页面能选 ⇔ 后端认）",
      _app_statuses == tuple(pw.MANUAL_STATUSES),
      json.dumps({"app": _app_statuses, "ledger": list(pw.MANUAL_STATUSES)}))
check("⑥ 前端外部单的状态入口走本地账本接口（不是云端 order_status）",
      "fetch('/api/local/external_prints/status'" in _html
      and "function _applyExternalOrderStatus(" in _html
      and re.search(r"if \(_isExternalOrder\(oid\)\) return _applyExternalOrderStatus\(", _html)
      is not None)
# v7.20（2026-11-13 用户要求「本地监视的任务也要能调类型」）：外部单不再是整列只读，
# 类型写本地账本（manual_type），判据与状态同源。
check("⑥ 类型列对外部单也可改（判据与状态同源，写本地账本）",
      re.search(r"function _orderTypeEditable\(oidS\) \{\s*return _orderStatusEditable\(oidS\);",
                _html) is not None
      and "if (_isExternalOrder(oid)) {\n    _orderTypeSaving = true;\n"
          "    return _applyExternalOrderMeta(o, null, type" in _html)


# ══════════════════════════════════════════════════════════════════
print("\n" + "=" * 78)
print(f"PASS {PASSED} / FAIL {FAILED} / 共 {PASSED + FAILED} 条")
print("全部通过 ✅" if ok_all else "有失败项 ❌")
print("=" * 78)
sys.exit(0 if ok_all else 1)
