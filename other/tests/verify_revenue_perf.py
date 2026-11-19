# -*- coding: utf-8 -*-
"""验收 · 收支清算云端取数性能（2026-11-19 用户反馈「数据越多越慢」）

现场实测（生产，2026-11-19 22:05，1500 单 / 2936 文件 / 1.2 MB 库）：
  · 服务端 /api/admin/statistics/revenue 全量区间 0.364 s / 2812255 B；11 月 0.190 s / 1137306 B
  · 本机 → https://hn-space.cn 同一 11 月请求 3.1~4.3 s；响应**没有任何压缩**
    （http 级只写了 `gzip on`，`gzip_types` 还是默认 text/html）
  · 服务端内部分解：SQL 0.02 s、逐单聚合 0.009 s、**逐文件 glob 判缩略图 0.402 s**、
    json 0.018 s；其余 0.3 s 是 1500 单 × 2936 文件的 Python 侧计算
  · 优化后：全量 0.30 s；nginx 对 /api/ 开 gzip ⇒ 全量 160349 B、11 月 68366 B，
    公网耗时 0.53~1.76 s / 0.26~0.28 s

本次三处改动（本脚本逐条钉住）：
  ① `_preview_tier_exists` 不再逐文件 glob：一次 listdir 建「有首页缩略图的 file_id 集合」，
     按目录 mtime 缓存（语义必须与老 glob 完全等价，见 B 段逐 id 对拍）。
  ② revenue 的订单查询去掉 `LEFT JOIN order_files` + `GROUP BY o.id`（JOIN 不筛行、不取列，
     只是把 2936 行子任务物化后再收成 1500 行）。
  ③ 逐单 `aggregate_order_status(conn, oid)`（N+1）改为 `aggregate_status_from_statuses(...)`
     复用已批量查到的子任务状态 —— 与逐单查询同一份聚合代码，见 A 段真实库对拍。
  + nginx 生产配置给 /api/ 开 gzip（C 段静态守卫）。

A/B 段都跑真实模块（后端复制到临时目录导入；PREVIEW_DIR 指到临时目录），exit 0 = 全绿。
"""
import io
import os
import re
import shutil
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BACKEND = os.path.join(REPO, "mobile_apps", "printer-backend")

ok_all = True


def chk(name, cond, detail=""):
    global ok_all
    if not cond:
        ok_all = False
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  — {detail}" if detail else ""))
    return bool(cond)


TMP = tempfile.mkdtemp(prefix="hn_revperf_")
DST = os.path.join(TMP, "printer-backend")
shutil.copytree(BACKEND, DST, ignore=shutil.ignore_patterns(
    "__pycache__", "config.py", "config.py.example",
    "orders.db", "users.db", "uploads", "logs", "*.db-wal", "*.db-shm"))
os.chdir(DST)
os.environ["FLASK_ENV"] = "development"
sys.path.insert(0, DST)

import app as backend  # noqa: E402

backend.init_db()


# ═══════════════════ A. 聚合口径：纯函数 vs 逐单查询（真实库对拍）═══════════════════
print("=== A. aggregate_status_from_statuses 与 aggregate_order_status 逐单对拍 ===")

CASES = [
    (["sent"], "sent"),
    (["sent", "sent"], "sent"),
    (["failed"], "failed"),
    (["sent", "failed"], "failed"),           # failed 压过 sent
    (["sent", "timeout"], "timeout"),         # 超时压过已完成（v7.16 口径）
    (["canceled", "sent"], "canceled"),       # 用户取消优先
    (["rejected", "rejected"], "rejected"),
    (["abandoned"], "abandoned"),
    (["offline_unknown"], "offline_unknown"),
    (["accepted"], "accepted"),
    (["printing", "queued"], "printing"),     # 非终态取优先级最高
    (["queued", "printing", "sent"], "printing"),
    (["downloading", "waiting"], "waiting"),
]


def _mk_order(statuses):
    conn = backend.get_db()
    cur = conn.execute(
        """INSERT INTO orders (file, copies, status, created_at, openid, order_number,
                               total_price, source, owner_name, is_admin_print)
           VALUES ('本地打印 1 个文件', 1, 'queued', '2026-11-19 10:00:00', 'local', ?, 3.5,
                   'local', '霍楠', 1)""",
        ("HN20261119-%04d" % (9000 + len(statuses)),))
    oid = cur.lastrowid
    for st in statuses:
        conn.execute(
            """INSERT INTO order_files (order_id, file_name, copies, page_count, duplex,
                                        total_price, status, created_at)
               VALUES (?, 'a.pdf', 1, 1, 'off', 0.0, ?, '2026-11-19 10:00:00')""", (oid, st))
    conn.commit()
    return oid


_bad = []
for statuses, want in CASES:
    oid = _mk_order(statuses)
    conn = backend.get_db()
    via_query = backend.aggregate_order_status(conn, oid)
    via_list = backend.aggregate_status_from_statuses(statuses)
    if not (via_query == via_list == want):
        _bad.append((statuses, want, via_query, via_list))
chk("A1 13 组状态组合：纯函数聚合 == 逐单查询聚合 == 期望值", not _bad,
    "全部一致" if not _bad else f"不一致 {_bad[:3]}")

chk("A2 无子任务的订单：两者都返回 None（父订单 status 不被改写）",
    backend.aggregate_order_status(backend.get_db(), _mk_order([])) is None
    and backend.aggregate_status_from_statuses([]) is None)

chk("A3 聚合函数仍只有一份实现（aggregate_order_status 内部调用纯函数）",
    "return aggregate_status_from_statuses([r[\"status\"] for r in rows])"
    in open(os.path.join(DST, "app.py"), encoding="utf-8").read())


# ═══════════════════ B. 缩略图存在性：新索引 vs 老 glob 逐 id 对拍 ═══════════════════
print("=== B. _preview_tier_exists 与老 glob 语义逐 id 对拍 ===")

PDIR = os.path.join(TMP, "previews")
os.makedirs(PDIR, exist_ok=True)
for nm in [
    "abc123_1700000000_p.webp",      # p 档
    "abc123_1700000000_t.jpg",       # t 档
    "def456_1700000001_p2_p.webp",   # 第 2 页的 p 档
    "ghi789_1700000002.jpg",         # 老缓存（无档位字母）—— 老 glob 也不认
    "ghi789_1700000002_p2.jpg",      # 老缓存的第 2 页 —— 老 glob 也不认
    "jkl000_9.webp",                 # 命名不合法 —— 两边都不认
    "meta.json",
]:
    open(os.path.join(PDIR, nm), "wb").write(b"x")

backend.PREVIEW_DIR = PDIR
backend._PREVIEW_INDEX_CACHE["stamp"] = None
backend._PREVIEW_INDEX_CACHE["ids"] = frozenset()

IDS = ["abc123", "def456", "ghi789", "jkl000", "nope", "abc12", "", "abc123!@#"]
_mismatch = []
for fid in IDS:
    old = bool(backend._preview_tier_files(fid))
    new = backend._preview_tier_exists(fid)
    if old != new:
        _mismatch.append((fid, old, new))
chk("B1 8 个 file_id（含空串/非法字符/前缀）新旧判定逐个一致", not _mismatch,
    f"abc123={backend._preview_tier_exists('abc123')} "
    f"ghi789={backend._preview_tier_exists('ghi789')} " +
    ("" if not _mismatch else f"不一致 {_mismatch}"))
chk("B2 老缓存命名（无档位字母）不算有缩略图 —— 与老 glob 同口径",
    backend._preview_tier_exists("ghi789") is False)
chk("B2b 多页缓存 `<stem>_p2_p.webp` 也**不算**首页缩略图（老 glob 的正则同样不认）",
    backend._preview_tier_exists("def456") is False)

_first = backend._preview_id_index()
_second = backend._preview_id_index()
chk("B3 目录没变时命中缓存（不重复 listdir）", _first is _second)

open(os.path.join(PDIR, "newid_1700000003_p.webp"), "wb").write(b"y")
_mtime = os.stat(PDIR).st_mtime_ns
os.utime(PDIR, ns=(_mtime + 10_000_000, _mtime + 10_000_000))
chk("B4 目录变了自动重建索引（新文件立刻可见）",
    backend._preview_tier_exists("newid") is True)

backend.PREVIEW_DIR = os.path.join(TMP, "no-such-dir")
chk("B5 目录不存在：返回 False 且不抛，索引返回 None（调用方退回老口径）",
    backend._preview_tier_exists("abc123") is False
    and backend._preview_id_index() is None)

backend.PREVIEW_DIR = PDIR
backend._PREVIEW_INDEX_CACHE["stamp"] = None
backend._PREVIEW_INDEX_CACHE["ids"] = frozenset()


# ═══════════════════ C. 接口源码口径 + nginx gzip 守卫 ═══════════════════
print("=== C. 接口源码口径与 nginx gzip 配置守卫 ===")

SRC = open(os.path.join(DST, "app.py"), encoding="utf-8").read()
_body_start = SRC.index("def admin_statistics_revenue():")
_body_end = SRC.index("@app.route", _body_start)
# 只看可执行代码：注释里会提到「去掉了 LEFT JOIN … / GROUP BY o.id」，那不是代码
BODY = "\n".join(ln for ln in SRC[_body_start:_body_end].splitlines()
                 if not ln.lstrip().startswith("#"))

chk("C1 订单查询不再 LEFT JOIN order_files（纯开销）",
    "LEFT JOIN order_files" not in BODY)
chk("C2 订单查询不再 GROUP BY o.id",
    "GROUP BY o.id" not in BODY)
chk("C3 逐单 N+1 聚合已换成批量纯函数",
    "aggregate_status_from_statuses(" in BODY and "aggregate_order_status(conn" not in BODY)
chk("C4 has_preview 仍走同一份判定（_has_preview_for 或 _preview_tier_exists）",
    "_preview_tier_exists(of_row[\"file_id\"])" in BODY)

NGINX = os.path.join(BACKEND, "nginx-production-hn-space.conf")
_nginx = open(NGINX, encoding="utf-8").read()
_api_block = _nginx[_nginx.index("location /api/ {"):]
_api_block = _api_block[:_api_block.index("}")]
chk("C5 生产 nginx 配置：/api/ 开 gzip application/json（本轮公网 8 s → 0.5~1.7 s 的来源）",
    "gzip on;" in _api_block and "gzip_types application/json;" in _api_block,
    "gzip_comp_level 6" if "gzip_comp_level 6;" in _api_block else "缺 comp_level")

print()
shutil.rmtree(TMP, ignore_errors=True)
print("收支清算取数性能验收（2026-11-19）")
print("✅ 全绿" if ok_all else "❌ 有失败")
sys.exit(0 if ok_all else 1)
