# -*- coding: utf-8 -*-
"""验收 · 收支清算云端配置的服务端修订号（体检 🔴8，2026-11-10）

现场问题：`settlement.html` 的多端并发保护等的是 `res.server_version`，
而 `/api/finance/config` **从来不返回它** ⇒ 前端 `conflict = sv > clientVersion + 1`
恒为 false、冲突确认框永远不弹 —— 两台设备（或同一人两个标签页）同时改账，
就是「后保存的那份把先保存的整份覆盖」，且没有任何提示。

修法：finance_config 增加 `rev` 列；每次成功保存 `rev = COALESCE(rev,0) + 1`，
保存与读取都回 `server_version`。

本脚本验证：
  ① 空库 GET → `server_version == 0`、`data is None`；
  ② 首存 → 1；再存 → 2；再存 → 3（**单调递增**，不能被 upsert 重置回 1）；
  ③ GET 回的 server_version 与最后一次 POST 一致，data 原样往返；
  ④ 并发语义：另一台设备在读到 v1 之后保存两次，第三台设备读到的仍是递增序列；
  ⑤ 源码判据：迁移建列、保存用 `COALESCE(finance_config.rev, 0) + 1`、两条路由都返回 server_version。

用法：python other/tests/verify_finance_config_rev.py
exit 0 = 全部通过。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _sec_harness as H  # noqa: E402

PROBE = r'''
import json
TOK = {"X-Printer-Token": "probe-tok"}

def get_cfg():
    r = http.get("/api/finance/config", headers=TOK)
    return r.status_code, r.get_json()

def post_cfg(obj):
    r = http.post("/api/finance/config", headers=TOK,
                  data=json.dumps({"data": obj}), content_type="application/json")
    return r.status_code, r.get_json()

out = {}

st, j0 = get_cfg()
out["empty_status"] = st
out["empty_sv"] = j0.get("server_version")
out["empty_data_is_none"] = (j0.get("data") is None)

st, j1 = post_cfg({"alpha": 1})
out["p1"] = j1.get("server_version")
st, j2 = post_cfg({"alpha": 2})
out["p2"] = j2.get("server_version")
st, j3 = post_cfg({"alpha": 3, "nested": {"k": [1, 2, 3]}})
out["p3"] = j3.get("server_version")

st, jg = get_cfg()
out["get_sv"] = jg.get("server_version")
out["get_data"] = jg.get("data")
out["get_status"] = st

# 第四台设备读到 v3 后再存 → 必须继续往上走，不能回到 1
st, j4 = post_cfg({"alpha": 4})
out["p4"] = j4.get("server_version")

print("RESULT=" + json.dumps(out, ensure_ascii=False, sort_keys=True))
'''

def main():
    tmp, dst = H.make_copy("hn_finrev_")
    proc = H.run_probe(dst, PROBE)
    raw = H.probe_value(proc)
    if raw is None:
        print(proc.stdout)
        print(proc.stderr[-3000:])
        H.chk("探针跑通并输出 RESULT", False, "probe 无输出")
        H.finish()
    import json
    got = json.loads(raw)

    H.chk("空库 GET 200", got["empty_status"] == 200, got["empty_status"])
    H.chk("空库 GET server_version == 0（前端据它做 baseline）", got["empty_sv"] == 0, got["empty_sv"])
    H.chk("空库 GET data is None", got["empty_data_is_none"] is True, got["empty_data_is_none"])

    H.chk("首次保存返回 server_version == 1", got["p1"] == 1, got["p1"])
    H.chk("第二次保存 == 2（递增，不被 upsert 重置）", got["p2"] == 2, got["p2"])
    H.chk("第三次保存 == 3", got["p3"] == 3, got["p3"])
    H.chk("GET 的 server_version 与最后一次保存一致", got["get_sv"] == 3, got["get_sv"])
    H.chk("data 原样往返（含嵌套）",
          got["get_data"] == {"alpha": 3, "nested": {"k": [1, 2, 3]}}, got["get_data"])
    H.chk("第四台设备保存后继续递增到 4（多端不会互相清零）", got["p4"] == 4, got["p4"])

    src = H.read_app_source()
    code = H.strip_comments(src)
    H.chk("★ 迁移里有 finance_config.rev 列",
          'ALTER TABLE finance_config ADD COLUMN rev INTEGER DEFAULT 0' in code)
    H.chk("★ 保存时 rev = COALESCE(finance_config.rev, 0) + 1（不是写死 1）",
          "rev = COALESCE(finance_config.rev, 0) + 1" in code)
    H.chk("★ 保存路由返回 server_version",
          '"server_version": _rev' in code)
    H.chk("★ 读取路由也返回 server_version（前端 _loadFromServer 依赖它）",
          'SELECT data, COALESCE(rev, 0) AS rev FROM finance_config' in code
          and '"server_version": int(row["rev"] or 0)' in code)

    H.finish()


if __name__ == "__main__":
    main()
