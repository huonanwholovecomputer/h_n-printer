# -*- coding: utf-8 -*-
"""外部打印账本 → 收支清算「订单明细」只读数据源验收（GET /api/local/external_prints）。

不需要打印机、不起 HTTP 服务、**一次真网都不发**（owner 查询一律注入假 lister）；
账本全部落在临时目录，绝不读真实 APPDATA%（唯一"碰真实路径"的地方是 import stats_server
时 paths 模块创建的目录，不读不写任何账本/配置）。

必须钉住的十件事：
  ① 只含「已处理」：账本里 new/confirmed/ignored 各一条 → 只回 1 条；
  ② 金额与 print_watch.amount_for 逐条相等（单面 2 页 / 双面 3 页），且单位是元不是分；
  ③ 页数/张数口径：双面 3 页 → 页 3 张 2；单面 2 页 → 页 2 张 2；
  ④ 页数未知 / 单双面未知 ⇒ 该条跳过 + skipped 计数，明细里不得出现 0 元订单；
  ⑤ start_date/end_date 命中与不命中各一组；
  ⑥ owner_name 注入成功 / 失败（含 60 秒缓存）两种；
  ⑦ 坏账本（写半截 JSON）→ 空数组、不抛、不 500；
  ⑧ order_id 是带 ext: 前缀的字符串，绝不是纯数字；
  ⑨ 订单行逐键逐值对照契约（含顶层与 files[0] 全部键）；
  ⑩ 文件明细与顶层金额一致（files[0].total_price == 该文件含份数的总金额 == 订单 total_price）；
  ⑪ 静态接线：路由挂载 + handler 只做「取账本/取单价/取 owner/序列化」。

用法: python tests/verify_external_prints_settlement.py   （exit 0 = 全部通过）
"""
import io
import json
import os
import re
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL_TOOL = os.path.join(REPO, "local_print_tool")
sys.path.insert(0, LOCAL_TOOL)

import print_watch as pw            # noqa: E402
import stats_server as ss           # noqa: E402

ok_all = True
PASSED = 0
FAILED = 0
TMP = tempfile.mkdtemp(prefix="hn_extprints_verify_")

# 契约样例用的单价（工具实配：单面 0.3 / 双面 0.4）。
SIM, DUP = 0.3, 0.4

# 契约里响应体的键（逐字符一致，不是"差不多"）
RESPONSE_KEYS = {"success", "orders", "count", "skipped",
                 "simplex_price", "duplex_price", "owner_name"}
# 契约里订单行的键
ORDER_KEYS = {"order_id", "order_number", "created_at", "total_price",
              "total_page_count", "total_sheet_count", "owner_name",
              "is_admin_print", "source", "openid", "nickname", "auto_print",
              "schedule_mode", "delivery_enabled", "urgency", "cover_page",
              "status", "unsynced", "sync_status", "external", "files"}
# 契约里 files[0] 的键
FILE_KEYS = {"id", "file_name", "copies", "page_count", "duplex", "page_range",
             "total_price", "status", "is_free", "effective_pages", "sheets",
             "total_pages", "total_sheets"}


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
         status=pw.STATUS_CONFIRMED, detected_at="2026-10-28 18:00:13"):
    """造一条账本记录（ExternalPrint 是账本的真实数据模型，不另写假对象）。"""
    return pw.ExternalPrint(job_id=job_id, printer="HP LaserJet 1020", document=document,
                            user="顾客", pages=pages, pages_source=pw.SOURCE_CONFIRMED,
                            duplex=duplex, color=pw.COLOR_MONO, copies=copies,
                            size_bytes=1024, detected_at=detected_at, status=status)


def oid_of(it):
    """按契约口径拼 order_id（测试断言用，与实现同一条公式：ext:<job_id>:<detected_at>）。"""
    return "ext:%d:%s" % (int(it.job_id), it.detected_at)


def build(items, owner="张三", start="", end=""):
    return ss.build_external_orders(items, SIM, DUP, owner, start, end)


def write_ledger(tag, items):
    """用真实的 ExternalPrintStore 落一份临时账本，再走真实的 load_external_print_items。"""
    path = os.path.join(TMP, tag + ".json")
    store = pw.ExternalPrintStore(path)
    for it in items:
        store.add(it)
    return path


# ══════════════════════════════════════════════════════════════════
section("① 只含「已处理」（confirmed）；new / ignored 一律不返回")
# ══════════════════════════════════════════════════════════════════
_three = [item(11, status=pw.STATUS_NEW, detected_at="2026-10-28 08:00:01"),
          item(12, status=pw.STATUS_CONFIRMED, detected_at="2026-10-28 08:30:02"),
          item(13, status=pw.STATUS_IGNORED, detected_at="2026-10-28 09:00:03")]
_path_three = write_ledger("three_status", _three)
_loaded_three = ss.load_external_print_items(_path_three)
check("① 临时账本经真实 loader 读回 3 条", len(_loaded_three) == 3, f"读回 {len(_loaded_three)} 条")
_orders_three, _skip_three = build(_loaded_three)
check("① 三种状态各一条 → 只返回 1 条（只有 confirmed 那条）",
      len(_orders_three) == 1 and _orders_three[0]["order_id"] == oid_of(_three[1]),
      "返回 " + json.dumps([o["order_id"] for o in _orders_three], ensure_ascii=False))
check("① new(待处理) 那条不在明细里",
      all(not o["order_id"].startswith("ext:11:") for o in _orders_three), "")
check("① ignored(已忽略) 那条不在明细里",
      all(not o["order_id"].startswith("ext:13:") for o in _orders_three), "")
check("① 其余状态不影响 skipped（那不是算不出钱，是压根不参与）", _skip_three == 0,
      f"skipped={_skip_three}")

# ══════════════════════════════════════════════════════════════════
section("② 金额一律 print_watch.amount_for（单面 2 页 / 双面 3 页），单位是元")
# ══════════════════════════════════════════════════════════════════
_sim2 = item(21, document="两页单面.pdf", pages=2, duplex=pw.DUPLEX_OFF,
             detected_at="2026-10-28 09:10:11")
_dup3 = item(22, document="三页双面.pdf", pages=3, duplex=pw.DUPLEX_ON,
             detected_at="2026-10-28 09:20:22")
_orders_amt, _skip_amt = build([_sim2, _dup3])
_by_oid = {o["order_id"]: o for o in _orders_amt}
_exp_sim = pw.amount_for(_sim2, SIM, DUP)
_exp_dup = pw.amount_for(_dup3, SIM, DUP)
check("② 两条都进了明细（可算出金额的记录一条不丢）",
      len(_orders_amt) == 2 and _skip_amt == 0, f"orders={len(_orders_amt)} skipped={_skip_amt}")
check("② 单面 2 页：total_price 与 amount_for 逐值相等",
      _by_oid[oid_of(_sim2)]["total_price"] == _exp_sim,
      f"接口={_by_oid[oid_of(_sim2)]['total_price']} amount_for={_exp_sim}")
check("② 双面 3 页：total_price 与 amount_for 逐值相等",
      _by_oid[oid_of(_dup3)]["total_price"] == _exp_dup,
      f"接口={_by_oid[oid_of(_dup3)]['total_price']} amount_for={_exp_dup}")
check("② 单位是元（单面 2 页 = 0.60 元，不是 60 分、也不是 0.006）",
      _exp_sim == 0.6 and _by_oid[oid_of(_sim2)]["total_price"] == 0.6, f"{_exp_sim}")
check("② 单位是元（双面 3 页 = 1 张双面 0.40 + 1 张单面 0.30 = 0.70 元）",
      _exp_dup == 0.7 and _by_oid[oid_of(_dup3)]["total_price"] == 0.7, f"{_exp_dup}")

# ══════════════════════════════════════════════════════════════════
section("③ 页数 / 张数口径：双面 3 页 → 页 3 张 2；单面 2 页 → 页 2 张 2")
# ══════════════════════════════════════════════════════════════════
_row_dup3 = _by_oid[oid_of(_dup3)]
_row_sim2 = _by_oid[oid_of(_sim2)]
check("③ 双面 3 页：total_page_count=3 / total_sheet_count=2",
      _row_dup3["total_page_count"] == 3 and _row_dup3["total_sheet_count"] == 2,
      f"页 {_row_dup3['total_page_count']} 张 {_row_dup3['total_sheet_count']}")
check("③ 双面 3 页：files[0] 的 effective_pages=3 / sheets=2 / total_pages=3 / total_sheets=2",
      (_row_dup3["files"][0]["effective_pages"], _row_dup3["files"][0]["sheets"],
       _row_dup3["files"][0]["total_pages"], _row_dup3["files"][0]["total_sheets"]) == (3, 2, 3, 2),
      json.dumps({k: _row_dup3["files"][0][k] for k in
                  ("effective_pages", "sheets", "total_pages", "total_sheets")}))
check("③ 单面 2 页：total_page_count=2 / total_sheet_count=2（每页各占一张纸）",
      _row_sim2["total_page_count"] == 2 and _row_sim2["total_sheet_count"] == 2,
      f"页 {_row_sim2['total_page_count']} 张 {_row_sim2['total_sheet_count']}")
check("③ 单面 2 页：files[0] sheets=2 / total_sheets=2",
      _row_sim2["files"][0]["sheets"] == 2 and _row_sim2["files"][0]["total_sheets"] == 2, "")

# ══════════════════════════════════════════════════════════════════
section("④ 页数未知 / 单双面未知 ⇒ 跳过 + skipped 计数，出不了 0 元订单")
# ══════════════════════════════════════════════════════════════════
_no_pages = item(41, document="页数未知.pdf", pages=0, duplex=pw.DUPLEX_ON,
                 detected_at="2026-10-28 10:00:00")
_no_duplex = item(42, document="单双面未确认.pdf", pages=3, duplex=pw.DUPLEX_UNKNOWN,
                  detected_at="2026-10-28 10:05:00")
_ok_one = item(43, document="正常.pdf", pages=2, duplex=pw.DUPLEX_OFF,
               detected_at="2026-10-28 10:10:00")
_orders_unk, _skip_unk = build([_no_pages, _no_duplex, _ok_one])
check("④ 页数未知（pages=0）那条被跳过", all(not o["order_id"].startswith("ext:41:")
                                            for o in _orders_unk), "")
check("④ 单双面未知（duplex 为空字符串）那条被跳过",
      all(not o["order_id"].startswith("ext:42:") for o in _orders_unk), "")
check("④ skipped 计数 = 2（两条算不出金额）", _skip_unk == 2, f"skipped={_skip_unk}")
check("④ 正常那条照常返回（跳过的不会连累别人）",
      len(_orders_unk) == 1 and _orders_unk[0]["order_id"] == oid_of(_ok_one),
      json.dumps([o["order_id"] for o in _orders_unk], ensure_ascii=False))
check("④ 明细里没有 0 元订单（绝不按 0 算）",
      all(float(o["total_price"]) > 0 for o in _orders_unk),
      json.dumps([o["total_price"] for o in _orders_unk]))
_zp = ss.build_external_prints_payload([_no_pages, _no_duplex], SIM, DUP, "张三")
check("④ 全是算不出的记录 ⇒ orders=[] / count=0 / skipped=2",
      _zp["orders"] == [] and _zp["count"] == 0 and _zp["skipped"] == 2,
      json.dumps({k: _zp[k] for k in ("count", "skipped")}))

# ══════════════════════════════════════════════════════════════════
section("⑤ 日期过滤：start_date / end_date 命中与不命中")
# ══════════════════════════════════════════════════════════════════
_d26 = item(51, pages=2, duplex=pw.DUPLEX_OFF, detected_at="2026-10-26 23:59:59")
_d27 = item(52, pages=3, duplex=pw.DUPLEX_ON, detected_at="2026-10-27 12:00:00")
_d28 = item(53, pages=1, duplex=pw.DUPLEX_OFF, detected_at="2026-10-28 00:00:00")
_all_days = [_d26, _d27, _d28]
_o_hit, _s_hit = build(_all_days, start="2026-10-27", end="2026-10-27")
check("⑤ 起止都=2026-10-27 ⇒ 只命中中间那条（含边界当天）",
      [o["order_id"] for o in _o_hit] == [oid_of(_d27)] and _s_hit == 0,
      json.dumps([o["created_at"] for o in _o_hit]))
_o_left, _ = build(_all_days, end="2026-10-26")
check("⑤ 只有 end_date=2026-10-26 ⇒ 只命中 10-26 那条",
      [o["order_id"] for o in _o_left] == [oid_of(_d26)],
      json.dumps([o["created_at"] for o in _o_left]))
_o_right, _ = build(_all_days, start="2026-10-28")
check("⑤ 只有 start_date=2026-10-28 ⇒ 只命中 10-28 那条",
      [o["order_id"] for o in _o_right] == [oid_of(_d28)],
      json.dumps([o["created_at"] for o in _o_right]))
_o_miss, _s_miss = build(_all_days, start="2026-11-01", end="2026-11-30")
check("⑤ 区间不命中（2026-11）⇒ 0 条、且不计入 skipped",
      _o_miss == [] and _s_miss == 0, f"orders={len(_o_miss)} skipped={_s_miss}")
_o_nofilter, _ = build(_all_days)
check("⑤ 两个参数都空 ⇒ 全部返回（3 条）", len(_o_nofilter) == 3, str(len(_o_nofilter)))

# ══════════════════════════════════════════════════════════════════
section("⑥ owner_name 注入成功 / 失败（含 60 秒缓存；一次真网都不发）")
# ══════════════════════════════════════════════════════════════════
_calls = {"ok": 0, "fail": 0, "ttl": 0}


def _lister_ok(client_id, api_url, token):
    _calls["ok"] += 1
    assert client_id and api_url, "注入的 lister 也必须收到 client_id/api_url"
    return "张三"


def _lister_fail(client_id, api_url, token):
    _calls["fail"] += 1
    raise RuntimeError("模拟后端离线")


def _lister_ttl(client_id, api_url, token):
    _calls["ttl"] += 1
    return "李四"


_owner_ok = ss.fetch_device_owner("cid-ok", "http://stub.invalid", "tok-1",
                                  now=1000.0, lister=_lister_ok)
check("⑥ 注入取 owner 成功 ⇒ owner_name=张三", _owner_ok == "张三" and _calls["ok"] == 1,
      f"owner={_owner_ok!r} 调用 {_calls['ok']} 次")
_owner_cached = ss.fetch_device_owner("cid-ok", "http://stub.invalid", "tok-1",
                                      now=1050.0, lister=_lister_ok)
check("⑥ 缓存期内（50 秒）不再打后端，仍回同一个 owner",
      _owner_cached == "张三" and _calls["ok"] == 1, f"lister 调用 {_calls['ok']} 次")
_owner_expired = ss.fetch_device_owner("cid-ok", "http://stub.invalid", "tok-1",
                                       now=1000.0 + ss.EXTERNAL_OWNER_TTL + 1, lister=_lister_ok)
check("⑥ 超过 60 秒缓存过期 ⇒ 重新取（值不变、调用数 +1）",
      _owner_expired == "张三" and _calls["ok"] == 2, f"lister 调用 {_calls['ok']} 次")
_owner_fail = ss.fetch_device_owner("cid-fail", "http://stub.invalid", "tok-2",
                                    now=2000.0, lister=_lister_fail)
check("⑥ 取 owner 抛异常 ⇒ owner_name=''（不抛、订单照常返回）",
      _owner_fail == "" and _calls["fail"] == 1, f"owner={_owner_fail!r}")
_owner_fail_cached = ss.fetch_device_owner("cid-fail", "http://stub.invalid", "tok-2",
                                           now=2010.0, lister=_lister_fail)
check("⑥ 失败结果也缓存 60 秒（不放大成请求风暴）",
      _owner_fail_cached == "" and _calls["fail"] == 1, f"lister 调用 {_calls['fail']} 次")
_owner_local = ss.fetch_device_owner("", "", "", now=3000.0, lister=_lister_ttl)
check("⑥ 本地模式（无 client_id/api_url）直接 ''，且不触发任何查询",
      _owner_local == "" and _calls["ttl"] == 0, f"lister 调用 {_calls['ttl']} 次")
_owner_ttl = ss.fetch_device_owner("cid-ttl", "http://stub.invalid/", "tok-3",
                                   now=4000.0, lister=_lister_ttl)
check("⑥ api_url 末尾斜杠不影响缓存键（同一台设备只查一次）",
      _owner_ttl == "李四" and _calls["ttl"] == 1, f"lister 调用 {_calls['ttl']} 次")
_o_owner, _ = build([_ok_one], owner="张三")
_o_none, _ = build([_ok_one], owner="")
check("⑥ 注入的 owner 落进订单行", _o_owner[0]["owner_name"] == "张三", _o_owner[0]["owner_name"])
check("⑥ 未绑定 ⇒ owner_name 为空字符串（前端显示为未绑定），订单照常返回",
      _o_none[0]["owner_name"] == "" and len(_o_none) == 1, repr(_o_none[0]["owner_name"]))

# ══════════════════════════════════════════════════════════════════
section("⑦ 坏账本（半截 JSON / 不存在）⇒ 空数组、不抛、不 500")
# ══════════════════════════════════════════════════════════════════
_bad_path = os.path.join(TMP, "corrupt_ledger.json")
with open(_bad_path, "w", encoding="utf-8") as _f:
    _f.write('[{"job_id": 1, "document": "\u534a\u622a', )   # 断电/并发写最常见的形态
_bad_items = None
_bad_error = ""
try:
    _bad_items = ss.load_external_print_items(_bad_path)
except Exception as _e:                                        # 不许抛
    _bad_error = f"{type(_e).__name__}: {_e}"
check("⑦ 半截 JSON 的账本 → 返回空列表（不抛异常）",
      _bad_items == [] and not _bad_error, _bad_error or f"items={_bad_items!r}")
_bad_payload = ss.build_external_prints_payload(_bad_items or [], SIM, DUP, "")
check("⑦ 坏账本的响应体 = {success:true, orders:[], count:0, skipped:0}（不是 500）",
      _bad_payload["success"] is True and _bad_payload["orders"] == []
      and _bad_payload["count"] == 0 and _bad_payload["skipped"] == 0,
      json.dumps({k: _bad_payload[k] for k in ("success", "count", "skipped")}))
_missing = ss.load_external_print_items(os.path.join(TMP, "never_exists.json"))
check("⑦ 账本不存在 → 同样空列表、不抛", _missing == [], repr(_missing))

# ══════════════════════════════════════════════════════════════════
section("⑧ order_id 是带前缀的字符串，绝不是纯数字")
# ══════════════════════════════════════════════════════════════════
_all_rows = _orders_three + _orders_amt + _orders_unk + _o_hit
check("⑧ 每条 order_id 都是 str 且以 ext: 开头",
      all(isinstance(o["order_id"], str) and re.match(r"^ext:", o["order_id"])
          for o in _all_rows), json.dumps([o["order_id"] for o in _all_rows], ensure_ascii=False)[:120])
check("⑧ 没有一条 order_id 是纯数字（re.fullmatch('[0-9]+') 全部为 None）",
      all(re.fullmatch(r"[0-9]+", o["order_id"]) is None for o in _all_rows), "")
check("⑧ 订单号同样不是纯数字（EXT- 前缀）",
      all(str(o["order_number"]).startswith("EXT-") for o in _all_rows),
      json.dumps([o["order_number"] for o in _all_rows], ensure_ascii=False)[:120])

# ══════════════════════════════════════════════════════════════════
section("⑨ 订单行逐键逐值对照契约（顶层 + files[0] 全部键）")
# ══════════════════════════════════════════════════════════════════
# 契约原文样例：job_id=8 / 三页双面 / copies=1 / 2026-10-28 18:00:13 / owner=张三
_contract_item = item(8, document="三页文档测试.pdf", pages=3, duplex=pw.DUPLEX_ON,
                      copies=1, detected_at="2026-10-28 18:00:13")
_contract_path = write_ledger("contract", [_contract_item])
_contract_payload = ss.build_external_prints_payload(
    ss.load_external_print_items(_contract_path), SIM, DUP, "张三")
_contract_row = _contract_payload["orders"][0]
EXPECTED_ROW = {
    "order_id": "ext:8:2026-10-28 18:00:13",
    "order_number": "EXT-20261028-180013-8",
    "created_at": "2026-10-28 18:00:13",
    "total_price": 0.7,
    "total_page_count": 3,
    "total_sheet_count": 2,
    "owner_name": "张三",
    "is_admin_print": False,
    "source": "external",
    "openid": "local",
    "nickname": "",
    "auto_print": False,
    "schedule_mode": None,
    "delivery_enabled": False,
    "urgency": "低",
    "cover_page": False,
    "status": "sent",
    "unsynced": False,
    "sync_status": "synced",
    "external": True,
    "files": [{
        "id": "ext-8", "file_name": "三页文档测试.pdf", "copies": 1, "page_count": 3,
        "duplex": "on", "page_range": "", "total_price": 0.7, "status": "sent",
        "is_free": False, "effective_pages": 3, "sheets": 2, "total_pages": 3,
        "total_sheets": 2,
    }],
}
check("⑨ 订单行与契约样例**整行相等**（键与值一起对）",
      _contract_row == EXPECTED_ROW,
      "" if _contract_row == EXPECTED_ROW else json.dumps(
          {k: _contract_row.get(k) for k in sorted(set(EXPECTED_ROW) | set(_contract_row))},
          ensure_ascii=False)[:400])
check("⑨ 顶层键集合逐字符一致（21 个，多一个少一个都算不一致）",
      set(_contract_row) == ORDER_KEYS,
      "缺 " + str(sorted(ORDER_KEYS - set(_contract_row)))
      + " 多 " + str(sorted(set(_contract_row) - ORDER_KEYS)))
check("⑨ files[0] 键集合逐字符一致（13 个）",
      set(_contract_row["files"][0]) == FILE_KEYS,
      "缺 " + str(sorted(FILE_KEYS - set(_contract_row["files"][0])))
      + " 多 " + str(sorted(set(_contract_row["files"][0]) - FILE_KEYS)))
check("⑨ 响应体键集合逐字符一致（success/orders/count/skipped/simplex_price/duplex_price/owner_name）",
      set(_contract_payload) == RESPONSE_KEYS,
      "缺 " + str(sorted(RESPONSE_KEYS - set(_contract_payload)))
      + " 多 " + str(sorted(set(_contract_payload) - RESPONSE_KEYS)))
check("⑨ 响应体的 count/skipped/单价/owner 与契约一致",
      _contract_payload["count"] == 1 and _contract_payload["skipped"] == 0
      and _contract_payload["simplex_price"] == 0.3
      and _contract_payload["duplex_price"] == 0.4
      and _contract_payload["owner_name"] == "张三",
      json.dumps({k: _contract_payload[k] for k in
                  ("count", "skipped", "simplex_price", "duplex_price", "owner_name")},
                 ensure_ascii=False))
check("⑨ files[0].duplex 是 'on'/'off' 原值（不是中文标签，前端按它折算张数）",
      _contract_row["files"][0]["duplex"] == "on"
      and _by_oid[oid_of(_sim2)]["files"][0]["duplex"] == "off", "")

# ══════════════════════════════════════════════════════════════════
section("⑩ 文件明细与顶层金额一致（单条单文件）")
# ══════════════════════════════════════════════════════════════════
_f0 = _contract_row["files"][0]
check("⑩ 单文件 copies=1：files[0].total_price == 订单 total_price",
      abs(_f0["total_price"] - _contract_row["total_price"]) < 1e-9,
      f"{_f0['total_price']} vs {_contract_row['total_price']}")
_copies2 = item(31, document="三页双面两份.pdf", pages=3, duplex=pw.DUPLEX_ON,
                copies=2, detected_at="2026-10-28 11:00:00")
_row_c2 = build([_copies2])[0][0]
_f2 = _row_c2["files"][0]
check("⑩ copies=2：files[0].total_price == 订单 total_price（文件金额含份数，与云端 order_files 同义）",
      abs(_f2["total_price"] - _row_c2["total_price"]) < 1e-9,
      f"{_f2['total_price']} vs {_row_c2['total_price']}")
check("⑩ copies=2：「订单金额 − 文件小计」= 0（否则结算页会把差额当成附加服务费）",
      abs(float(_row_c2["total_price"]) - sum(float(f["total_price"]) for f in _row_c2["files"])) < 1e-9,
      f"{_row_c2['total_price']} − {_f2['total_price']}")
check("⑩ copies=2：顶层金额 == amount_for（份数已算进去，不是只算一份）",
      _row_c2["total_price"] == pw.amount_for(_copies2, SIM, DUP) == 1.4,
      f"{_row_c2['total_price']} vs {pw.amount_for(_copies2, SIM, DUP)}")
check("⑩ copies=2：页数/张数也乘了份数（3 页 2 张 × 2 份 = 页 6 张 4）",
      (_row_c2["total_page_count"], _row_c2["total_sheet_count"]) == (6, 4),
      f"页 {_row_c2['total_page_count']} 张 {_row_c2['total_sheet_count']}")
check("⑩ 单文件明细只有 1 条（一条外部作业 = 一个文件行）",
      len(_contract_row["files"]) == 1 and _f0["id"] == "ext-8", _f0["id"])

# ══════════════════════════════════════════════════════════════════
section("⑪ 静态接线：路由挂载 + handler 只做取数/取价/取 owner/序列化")
# ══════════════════════════════════════════════════════════════════
with open(os.path.join(LOCAL_TOOL, "stats_server.py"), "r", encoding="utf-8") as _f:
    _src = _f.read()
check("⑪ do_GET 里挂载了 /api/local/external_prints → _handle_local_external_prints",
      'if path == "/api/local/external_prints":' in _src
      and "return self._handle_local_external_prints(parsed.query)" in _src, "")
check("⑪ handler 与纯函数都在（build_external_orders / build_external_prints_payload）",
      "def _handle_local_external_prints" in _src
      and "def build_external_orders" in _src
      and "def build_external_prints_payload" in _src, "")
_handler_src = _src.split("def _handle_local_external_prints")[1].split("def _handle_post_data")[0]
check("⑪ handler 只取账本/单价/owner 再交给纯函数（自己不重写金额/页数口径）",
      "load_external_print_items()" in _handler_src
      and "load_external_prices()" in _handler_src
      and "fetch_device_owner(" in _handler_src
      and "build_external_prints_payload(" in _handler_src
      and "amount_for" not in _handler_src
      and "calc_cost" not in _handler_src, "")
check("⑪ owner 查询走共享 Session + _PROXY_TIMEOUT + X-Printer-Token 请求头",
      '"%s/api/printer/devices" % api_url' in _src
      and '_get_proxy_session().get(' in _src
      and '"X-Printer-Token": token' in _src
      and "timeout=_PROXY_TIMEOUT" in _src, "")
check("⑪ 账本读取用 print_watch.ExternalPrintStore（不另写一份 JSON 解析）",
      "ExternalPrintStore(path)" in _src and "def _read_file" not in _handler_src, "")

# ══════════════════════════════════════════════════════════════════
section("⑫ handler 胶水层：解析 start_date/end_date 并只序列化一次（仍不发真网）")
# ══════════════════════════════════════════════════════════════════


class _FakeHandler(object):
    """只实现 handler 用到的三样东西：client_id/api_url/token + _send_json。"""

    client_id = "cid-fake"
    api_url = "http://stub.invalid"
    token = "tok-fake"

    def __init__(self):
        self.sent = None

    def _send_json(self, data, status=200):
        self.sent = (data, status)


_orig = (ss.load_external_print_items, ss.load_external_prices, ss.fetch_device_owner)
try:
    ss.load_external_print_items = lambda path="": [_d27, _d28]
    ss.load_external_prices = lambda: (SIM, DUP)
    ss.fetch_device_owner = lambda *a, **k: "张三"
    _fake = _FakeHandler()
    ss._StatsHandler._handle_local_external_prints(
        _fake, "start_date=2026-10-28&end_date=2026-10-28")
finally:
    ss.load_external_print_items, ss.load_external_prices, ss.fetch_device_owner = _orig
_sent_payload, _sent_status = _fake.sent or ({}, None)
check("⑫ handler 只在 HTTP 200 上序列化一次（绝不 500）", _sent_status == 200, str(_sent_status))
check("⑫ 查询串里的日期真的被用上（只回 10-28 那条）",
      _sent_payload.get("count") == 1
      and _sent_payload["orders"][0]["created_at"].startswith("2026-10-28"),
      json.dumps([o["created_at"] for o in _sent_payload.get("orders", [])]))
check("⑫ handler 响应体与纯函数口径一致（键集合 + owner 注入）",
      set(_sent_payload) == RESPONSE_KEYS and _sent_payload["owner_name"] == "张三"
      and _sent_payload["simplex_price"] == 0.3 and _sent_payload["duplex_price"] == 0.4,
      json.dumps(sorted(_sent_payload)))

# ══════════════════════════════════════════════════════════════════
section("证据：真实构造的响应 JSON（临时账本 → 真实的 load_external_print_items → 纯函数）")
# ══════════════════════════════════════════════════════════════════
_evidence_items = [
    item(7, document="两页单面.pdf", pages=2, duplex=pw.DUPLEX_OFF, copies=1,
         status=pw.STATUS_CONFIRMED, detected_at="2026-10-28 17:55:00"),
    item(8, document="三页文档测试.pdf", pages=3, duplex=pw.DUPLEX_ON, copies=1,
         status=pw.STATUS_CONFIRMED, detected_at="2026-10-28 18:00:13"),
    item(9, document="待处理的作业.pdf", pages=4, duplex=pw.DUPLEX_ON, copies=1,
         status=pw.STATUS_NEW, detected_at="2026-10-28 18:05:00"),
    item(10, document="页数未知.pdf", pages=0, duplex=pw.DUPLEX_ON, copies=1,
         status=pw.STATUS_CONFIRMED, detected_at="2026-10-28 18:10:00"),
]
_evidence_path = write_ledger("evidence", _evidence_items)
_evidence_payload = ss.build_external_prints_payload(
    ss.load_external_print_items(_evidence_path), SIM, DUP, "张三",
    "2026-10-28", "2026-10-28")
print(json.dumps(_evidence_payload, ensure_ascii=False, indent=2))
check("证据：4 条账本（new/页数未知 各一条）→ 明细 2 条、skipped 1、new 不参与",
      _evidence_payload["count"] == 2 and _evidence_payload["skipped"] == 1
      and [o["order_number"] for o in _evidence_payload["orders"]]
      == ["EXT-20261028-180013-8", "EXT-20261028-175500-7"],
      json.dumps({"count": _evidence_payload["count"],
                  "skipped": _evidence_payload["skipped"],
                  "order_numbers": [o["order_number"] for o in _evidence_payload["orders"]]},
                 ensure_ascii=False))

print("\n" + "=" * 78)
print(f"PASS {PASSED} / FAIL {FAILED} / 共 {PASSED + FAILED} 条")
print("全部通过 ✅" if ok_all else "有失败项 ❌")
print("=" * 78)
sys.exit(0 if ok_all else 1)
