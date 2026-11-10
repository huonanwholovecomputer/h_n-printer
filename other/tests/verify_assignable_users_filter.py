# -*- coding: utf-8 -*-
"""结算页「用户」列候选名单：不许出现「只登录过一次、从没下过单」的占位账号。

2026-11-10 现场：候选名单里挂着 `user_s0iaj5b0`（首次登录自动分配的 8 位占位昵称，
2026-08-11 之后一次都没下过单），操作员点进去还以为是个真客户。

判据（后端 `assignable_users`）：
    ① 占位昵称（`user_` + 8 位小写字母数字）**且** 名下没有任何有效订单 → 不出现；
    ② 占位昵称但真下过单 → 必须保留（否则那些老订单的归属人就消失了）；
    ③ 真实昵称（哪怕 0 单，比如刚注册的同事）→ 保留；
    ④ 已移除账号 / 设备账号 dev_* → 本来就不出现（零回归）；
    ⑤ 统计订单数失败时**不过滤**（宁可多列一个名字，也不能把真人吞掉）。
"""
import io
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _sec_harness as H  # noqa: E402

_tmp, _dst = H.make_copy("hn_assignable_")

_PROBE = r'''
import json, sqlite3
uc = backend.get_user_db()
rows = [
    ("oid-ph-0", "user_aaaa1111", "guest", None),   # 占位 + 0 单 → 不该出现
    ("oid-ph-1", "user_bbbb2222", "guest", None),   # 占位 + 有单 → 要出现
    ("oid-real", "姚懿祥", "guest", None),           # 真名 + 0 单 → 要出现
    ("oid-gone", "user_cccc3333", "guest", "2026-11-01 10:00:00"),  # 已移除 → 不出现
    ("dev_abc",  "前台机",        "guest", None),    # 设备账号 → 不出现
]
for oid, nick, role, removed in rows:
    uc.execute(
        "INSERT OR REPLACE INTO users (openid, nickname, role, avatar_path, updated_at,"
        " temp_until, theme_mode, removed_at, removed_by, bound_openid)"
        " VALUES (?, ?, ?, '', '2026-08-11 19:19:24', '', '', ?, '', '')",
        (oid, nick, role, removed or ""))
uc.commit()
uc.close()

db = backend.get_db()
cols = [r[1] for r in db.execute("PRAGMA table_info(orders)").fetchall()]
vals = {"file": "x.docx", "copies": 1, "status": "sent", "created_at": "2026-11-09 12:00:00",
        "openid": "oid-ph-1", "duplex": "on", "page_count": 2, "total_price": 0.8}
cols = [c for c in cols if c in vals and c != "id"]
db.execute("INSERT INTO orders (%s) VALUES (%s)" % (",".join(cols), ",".join("?" * len(cols))),
           [vals[c] for c in cols])
db.commit()
db.close()

r = http.get("/api/admin/statistics/revenue?start_date=2026-11-01&end_date=2026-11-30",
             headers={"X-Printer-Token": "probe-tok"})
d = r.get_json() or {}
names = sorted(u.get("nickname") or "" for u in (d.get("data") or {}).get("assignable_users") or [])
print("RESULT=" + json.dumps({"code": r.status_code, "names": names}, ensure_ascii=False))
'''

_p = H.run_probe(_dst, _PROBE)
import json  # noqa: E402
_raw = H.probe_value(_p)
if not _raw:
    print(_p.stdout[-3000:])
    print(_p.stderr[-3000:])
    H.chk("探针跑通", False, "无 RESULT 输出")
    H.finish()
_res = json.loads(_raw)
_names = _res.get("names") or []
H.chk("接口可用（200）", _res.get("code") == 200, _res.get("code"))
H.chk("① 占位昵称 + 0 单 → 不出现在候选名单",
      "user_aaaa1111" not in _names, _names)
H.chk("② 占位昵称但有单 → 保留（老订单的归属人不能消失）",
      "user_bbbb2222" in _names, _names)
H.chk("③ 真实昵称（0 单）→ 保留", "姚懿祥" in _names, _names)
H.chk("④ 已移除账号 / 设备账号 → 不出现",
      "user_cccc3333" not in _names and "前台机" not in _names, _names)

print("\n候选名单实际内容：" + " / ".join(_names))
H.finish()
