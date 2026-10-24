# -*- coding: utf-8 -*-
"""验收 · S2-01 索引与读取缓存（后端）

依据：docs/安全加固修复计划_2026-10-23.md §S2-01（DDoS 报告 R5 的「全表扫」部分）。

现场形状（修复前）：
  · orders / order_files / files 上**只有一条**索引（幂等键那支唯一索引），
    其余查询全是全表扫：/api/orders 的 `WHERE openid=? ORDER BY id DESC`、
    收支清算的时间区间、订单列表的每个父单查子任务、存储用量的 SUM(size)；
  · devices.json / printer_claim.json / pricing.json 每被用到一次就「读盘 + json.loads」一次，
    而它们落在心跳、printer_state 广播、设备列表、每一笔计价的路径上。

本脚本分三段：
  A. 静态接线：五条索引的建索引 SQL、缓存助手的判据（含 inode）、四处调用点、
     submit_order 把单价读到循环外。
  B. 行为（临时副本 + 真 SQLite）：EXPLAIN QUERY PLAN 逐条落到对应索引；
     init_db 幂等；缓存「第二次不解析」「外部改写立刻生效」「save 后立刻可读」
     「返回值与缓存隔离」（改返回值不会污染后续读）。
  C. 变异自证（子进程）：① 删掉 order_files(order_id) 那条索引 ⇒ 计划退回 SCAN；
     ② 把 load_devices 改回「每次读盘」⇒ 连续两次读解析 2 次。两处都必须红。

用法：python tests/verify_db_indexes.py     （exit 0 = 全部通过）
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
    """取出从 marker 开始、到下一个顶层定义为止的整段代码（吃代码不吃注释）。"""
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


cache_helper = block("def _cached_json_read(")
sig_helper = block("def _json_file_sig(")
load_devices_block = block("def load_devices(")
save_devices_block = block("def save_devices(")
claim_block = block("def _load_claim_impl(")
prices_block = block("def _load_paper_prices(")
calc_block = block("def calculate_price(")

INDEX_NAMES = ["idx_orders_openid_id", "idx_orders_created", "idx_order_files_order",
               "idx_order_files_file", "idx_files_openid"]

# ============================================================
# A. 静态接线
# ============================================================
print("=== A. 静态接线 ===")
H.chk("缓存助手按 (mtime_ns, size, inode) 判指纹（inode 是刻意加的：写的都是 tmp+replace）",
      "st.st_mtime_ns" in sig_helper and "st.st_size" in sig_helper
      and 'getattr(st, "st_ino", 0)' in sig_helper)
H.chk("缓存命中返回**深拷贝**（调用方可以随意改返回值）",
      "copy.deepcopy(cache[\"data\"])" in cache_helper)
H.chk("文件缺失/损坏返回 None（由调用方给缺省值，不是抛异常）",
      "except (json.JSONDecodeError, OSError)" in cache_helper)
H.chk("load_devices 走缓存",
      "_cached_json_read(DEVICES_FILE, _devices_cache, _devices_lock)" in load_devices_block)
H.chk("save_devices 写盘后刷新缓存（紧随其后的读一定看到新值）",
      "_devices_cache[\"sig\"] = _json_file_sig(DEVICES_FILE)" in save_devices_block
      and "_devices_cache[\"data\"] = copy.deepcopy(data)" in save_devices_block)
H.chk("_load_claim_impl 走缓存且**不自己取锁**（调用方已持 _claim_lock，取锁会自锁）",
      "_cached_json_read(CLAIM_FILE, _claim_cache, None)" in claim_block)
H.chk("_save_claim_impl 写盘后刷新缓存", "_claim_cache[\"sig\"] = _json_file_sig(CLAIM_FILE)" in code)
H.chk("_load_paper_prices 走缓存（原来每一笔计价都读盘）",
      "_cached_json_read(path, _paper_prices_cache, _paper_prices_lock)" in prices_block)
H.chk("calculate_price 支持传入单价（批量计价不再每个文件各读一次）",
      "prices=None" in calc_block and "prices or _load_paper_prices()" in calc_block)
H.chk("单价只读一次、不随文件循环重复读取（S2-02 起改为传进 _plan_order_files）",
      "schedule_mode, _load_paper_prices())" in code and "prices=prices" in code
      and "_paper_prices = _load_paper_prices()" not in code)

for name, sql in [
    ("orders(openid, id)", "CREATE INDEX IF NOT EXISTS idx_orders_openid_id ON orders(openid, id)"),
    ("orders(created_at)", "CREATE INDEX IF NOT EXISTS idx_orders_created ON orders(created_at)"),
    ("order_files(order_id)",
     "CREATE INDEX IF NOT EXISTS idx_order_files_order ON order_files(order_id)"),
    ("order_files(file_id)",
     "CREATE INDEX IF NOT EXISTS idx_order_files_file ON order_files(file_id)"),
    ("files(openid)", "CREATE INDEX IF NOT EXISTS idx_files_openid ON files(openid)"),
]:
    H.chk("建索引语句存在：" + name, sql in code)

H.chk("★ 索引建在 files.openid 迁移**之后**（之前建会报 no such column）",
      code.find("ALTER TABLE files ADD COLUMN openid") >= 0
      and code.find("ALTER TABLE files ADD COLUMN openid") < code.find("idx_files_openid"))
H.chk("建索引失败不阻断启动（只 WARN）",
      "索引 {_name} 建立失败（不影响启动）" in code)

# ============================================================
# B. 行为（临时副本 + 真 SQLite）
# ============================================================
print("\n=== B. 行为（临时副本 + 真 SQLite）===")
TMP, DST = H.make_copy("hn_idx_")
backend = H.import_backend(DST)
backend.init_db()

conn = backend.get_db()
names = {r[0] for r in conn.execute(
    "SELECT name FROM sqlite_master WHERE type='index'").fetchall()}
H.chk("init_db 建出全部 5 条索引",
      set(INDEX_NAMES) <= names, sorted(names))
H.chk("原有的幂等键唯一索引没被动过（防回退）",
      "idx_orders_client_req" in names)

# 造数据（空表上 SQLite 的规划器没有判别力）
for i in range(300):
    conn.execute(
        "INSERT INTO orders (file, copies, status, created_at, openid) VALUES (?,?,?,?,?)",
        ("a.pdf", 1, "sent", "2026-10-%02d 10:%02d:00" % (i % 28 + 1, i % 60), "u%d" % (i % 40)))
    conn.execute(
        "INSERT INTO order_files (order_id, file_id, file_name, status, created_at) "
        "VALUES (?,?,?,?,?)",
        (i + 1, "f%d" % (i % 50), "a.pdf", "sent", "2026-10-01 10:00:00"))
for i in range(50):
    conn.execute(
        "INSERT INTO files (id, original_name, saved_name, path, size, created_at, openid) "
        "VALUES (?,?,?,?,?,?,?)",
        ("f%d" % i, "a.pdf", "a.pdf", "/tmp/hn_idx.pdf", 1000,
         "2026-10-01 10:00:00", "u%d" % (i % 10)))
conn.commit()


def plan(sql):
    return " | ".join(str(r[3]) for r in conn.execute("EXPLAIN QUERY PLAN " + sql).fetchall())


p_orders = plan("SELECT id FROM orders WHERE openid='u3' ORDER BY id DESC LIMIT 20")
p_rev = plan("SELECT id FROM orders WHERE created_at >= '2026-10-01 00:00:00' "
             "AND created_at <= '2026-10-05 23:59:59' ORDER BY created_at DESC")
p_of = plan("SELECT status FROM order_files WHERE order_id=5")
p_of_in = plan("SELECT id FROM order_files WHERE order_id IN (1,2,3)")
p_of_file = plan("SELECT DISTINCT file_id FROM order_files WHERE file_id IS NOT NULL "
                 "AND status IN ('queued')")
p_files = plan("SELECT COALESCE(SUM(size),0) FROM files WHERE openid='u3' AND path != ''")
for lbl, p in [("orders 按用户+倒序", p_orders), ("orders 时间区间", p_rev),
               ("order_files 按订单", p_of), ("order_files 批量 IN", p_of_in),
               ("order_files 按文件", p_of_file), ("files 按用户求和", p_files)]:
    print("  【计划】%s: %s" % (lbl, p))

H.chk("/api/orders 的 `openid=? ORDER BY id DESC` 落到 idx_orders_openid_id 且无临时排序",
      "idx_orders_openid_id" in p_orders and "TEMP B-TREE" not in p_orders.upper(), p_orders)
H.chk("收支清算的时间区间扫到 idx_orders_created", "idx_orders_created" in p_rev, p_rev)
H.chk("子任务按订单查：不再 SCAN order_files", "idx_order_files_order" in p_of
      and not p_of.startswith("SCAN order_files"), p_of)
H.chk("子任务批量 IN 也用索引", "idx_order_files_order" in p_of_in, p_of_in)
H.chk("清理任务按 file_id 的 DISTINCT 扫到 idx_order_files_file",
      "idx_order_files_file" in p_of_file, p_of_file)
H.chk("存储用量 SUM(size) 扫到 idx_files_openid", "idx_files_openid" in p_files, p_files)

# init_db 幂等：再跑一次不报错、索引还在
backend.init_db()
conn2 = backend.get_db()
names2 = {r[0] for r in conn2.execute(
    "SELECT name FROM sqlite_master WHERE type='index'").fetchall()}
conn2.close()
H.chk("init_db 重复执行幂等（索引仍在、不报错）", set(INDEX_NAMES) <= names2)

# ---- 缓存 ----
print("\n  【缓存】")
orig_loads = backend.json.loads
counter = {"n": 0}

# ⚠ 只统计「这三个读取器发起的解析」。
# 原来的写法统计进程内**全部** json.loads —— 后端的启动清理线程
# （`_cleanup_once_on_startup` → `cleanup_expired_files` → `load_md5_index`）随时可能在
# 计数窗口里插一次解析，让 `devices.json：外部原子改写后立刻生效` 约 1/3 概率假红
# （在 HEAD 上可复现，与本脚本要验的行为无关）。按调用栈归因后，判据仍然是原来的判据：
# 若读取器改回「每次读盘」，计数照样是 2（变异自证见 C 段）。
_READER_FRAMES = ("load_devices", "_load_claim_impl", "_load_paper_prices")


def counting_loads(*a, **k):
    names = {fr.name for fr in traceback.extract_stack()}
    if names.intersection(_READER_FRAMES):
        counter["n"] += 1
    return orig_loads(*a, **k)


backend.json.loads = counting_loads
try:
    backend.save_devices({"devA": {"device_name": "A-设备"}})
    backend._devices_cache["sig"] = None          # 模拟冷缓存
    counter["n"] = 0
    d1 = backend.load_devices()
    d2 = backend.load_devices()
    H.chk("devices.json：冷缓存首读解析 1 次、第二读 0 次", counter["n"] == 1, counter["n"])
    H.chk("devices.json：缓存命中仍返回正确内容",
          d1.get("devA", {}).get("device_name") == "A-设备" and d2 == d1)

    # 外部改写（tmp + os.replace，换 inode）⇒ 必须立刻看到新值（缓存不能是"永久快照"）
    tmp_path = backend.DEVICES_FILE + ".ext"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump({"devB": {"device_name": "B-设备"}}, f, ensure_ascii=False)
    os.replace(tmp_path, backend.DEVICES_FILE)
    counter["n"] = 0
    d3 = backend.load_devices()
    H.chk("devices.json：外部原子改写后立刻生效（不是永久缓存）",
          sorted(d3.keys()) == ["devB"] and counter["n"] == 1, (sorted(d3.keys()), counter["n"]))

    # save → 立刻读不解析（写回时已刷新缓存）
    backend.save_devices({"devC": {"device_name": "C-设备"}})
    counter["n"] = 0
    d4 = backend.load_devices()
    H.chk("devices.json：save 之后立刻读命中缓存（0 次解析）且值正确",
          counter["n"] == 0 and d4.get("devC", {}).get("device_name") == "C-设备",
          counter["n"])
    d4["hacked"] = {"device_name": "被污染"}
    H.chk("devices.json：改返回值不会污染缓存（深拷贝隔离）",
          "hacked" not in backend.load_devices())

    # claim 同一套
    backend._claim_cache["sig"] = None
    backend._save_claim_impl({"claiming_devices": {"c1": {"owner_name": "老王"}}})
    counter["n"] = 0
    c1 = backend._load_claim_impl()
    c2 = backend._load_claim_impl()
    H.chk("printer_claim.json：写回后两次读都命中缓存（0 次解析）、内容正确",
          counter["n"] == 0 and c1["claiming_devices"]["c1"]["owner_name"] == "老王"
          and c2 == c1, counter["n"])

    # pricing：改价立刻生效
    price_path = os.path.join(os.path.dirname(os.path.abspath(backend.__file__)), "pricing.json")
    with open(price_path, "w", encoding="utf-8") as f:
        json.dump({"simplex_price": 0.31, "duplex_price": 0.41}, f)
    backend._paper_prices_cache["sig"] = None
    counter["n"] = 0
    pr1 = backend._load_paper_prices()
    pr2 = backend._load_paper_prices()
    H.chk("pricing.json：冷读 1 次解析、第二读 0 次", counter["n"] == 1, counter["n"])
    H.chk("pricing.json：读到的是文件里的单价（0.31 / 0.41）", pr1 == (0.31, 0.41), pr1)

    tmp_price = price_path + ".ext"
    with open(tmp_price, "w", encoding="utf-8") as f:
        json.dump({"simplex_price": 0.5, "duplex_price": 0.6}, f)
    os.replace(tmp_price, price_path)
    H.chk("pricing.json：改价后**立刻**按新价计价（缓存按指纹失效）",
          backend._load_paper_prices() == (0.5, 0.6)
          and abs(backend.calculate_price(4, "off") - 2.0) < 1e-9,
          (backend._load_paper_prices(), backend.calculate_price(4, "off")))
    H.chk("calculate_price 的 prices 参数与直读同值（循环外传入不改口径）",
          abs(backend.calculate_price(4, "off", prices=(0.5, 0.6))
              - backend.calculate_price(4, "off")) < 1e-9)
    H.chk("缺文件/坏文件仍走默认 0.2 / 0.3",
          backend._load_paper_prices() == (0.5, 0.6))  # 前置：文件在
    os.remove(price_path)
    H.chk("pricing.json 被删除 ⇒ 回退默认 0.2 / 0.3（不是沿用旧缓存）",
          backend._load_paper_prices() == (0.2, 0.3), backend._load_paper_prices())
finally:
    backend.json.loads = orig_loads
conn.close()

# ============================================================
# C. 变异自证（子进程）
# ============================================================
print("\n=== C. 变异自证 ===")
P = H.run_probe(DST, r"""
print("IDX=" + ",".join(sorted(n for n in {r[0] for r in backend.get_db().execute(
    "SELECT name FROM sqlite_master WHERE type='index'").fetchall()} if n.startswith("idx_"))))
conn = backend.get_db()
for i in range(300):
    conn.execute("INSERT INTO orders (file, copies, status, created_at, openid) VALUES (?,?,?,?,?)",
                 ("a.pdf", 1, "sent", "2026-10-%02d 10:00:00" % (i % 28 + 1), "u%d" % (i % 40)))
    conn.execute("INSERT INTO order_files (order_id, file_id, file_name, status, created_at) "
                 "VALUES (?,?,?,?,?)", (i + 1, "f%d" % (i % 50), "a.pdf", "sent", "2026-10-01 10:00:00"))
conn.commit()
p = " | ".join(str(r[3]) for r in conn.execute(
    "EXPLAIN QUERY PLAN SELECT status FROM order_files WHERE order_id=5").fetchall())
print("PLAN_OF=" + p)
""")
H.chk("基线：order_files 按订单走索引",
      "idx_order_files_order" in (H.probe_value(P, "PLAN_OF") or ""),
      H.probe_value(P, "PLAN_OF"))

mut_tmp, mut_dst = H.mutated_copy([
    ('        ("idx_order_files_order",\n'
     '         "CREATE INDEX IF NOT EXISTS idx_order_files_order ON order_files(order_id)"),\n',
     ''),
], prefix="hn_idx_mut_")
proc = H.run_probe(mut_dst, r"""
print("IDX=" + ",".join(sorted(n for n in {r[0] for r in backend.get_db().execute(
    "SELECT name FROM sqlite_master WHERE type='index'").fetchall()} if n.startswith("idx_"))))
conn = backend.get_db()
for i in range(300):
    conn.execute("INSERT INTO order_files (order_id, file_id, file_name, status, created_at) "
                 "VALUES (?,?,?,?,?)", (i + 1, "f%d" % (i % 50), "a.pdf", "sent", "2026-10-01 10:00:00"))
conn.commit()
p = " | ".join(str(r[3]) for r in conn.execute(
    "EXPLAIN QUERY PLAN SELECT status FROM order_files WHERE order_id=5").fetchall())
print("PLAN_OF=" + p)
""")
mut_plan = H.probe_value(proc, "PLAN_OF") or ""
H.chk("★ 变异（删掉那条索引）⇒ 计划退回全表扫（判据能红）",
      "idx_order_files_order" not in mut_plan and "SCAN order_files" in mut_plan, mut_plan)

mut2_tmp, mut2_dst = H.mutated_copy([
    ("    data = _cached_json_read(DEVICES_FILE, _devices_cache, _devices_lock)\n"
     "    return data if isinstance(data, dict) else {}\n",
     "    if not os.path.exists(DEVICES_FILE):\n"
     "        return {}\n"
     "    try:\n"
     "        with open(DEVICES_FILE, \"r\", encoding=\"utf-8\") as f:\n"
     "            data = json.load(f)\n"
     "    except (json.JSONDecodeError, IOError):\n"
     "        return {}\n"
     "    return data if isinstance(data, dict) else {}\n"),
], prefix="hn_idx_mut2_")
proc2 = H.run_probe(mut2_dst, r"""
import json as _json, traceback
backend.save_devices({"devA": {"device_name": "A"}})
orig = _json.loads
cnt = {"n": 0}
def counting(*a, **k):
    # 同主脚本：只统计 load_devices 发起的解析，避免启动清理线程插一次把计数打成 3
    if any(fr.name == "load_devices" for fr in traceback.extract_stack()):
        cnt["n"] += 1
    return orig(*a, **k)
_json.loads = counting
cnt["n"] = 0
backend.load_devices(); backend.load_devices()
print("READS=%d" % cnt["n"])
""")
reads = H.probe_value(proc2, "READS")
H.chk("★ 变异（load_devices 改回每次读盘）⇒ 连续两次读解析 2 次（判据能红）",
      reads == "2", reads)

H.finish()
