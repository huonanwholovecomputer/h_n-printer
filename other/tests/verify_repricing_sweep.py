# -*- coding: utf-8 -*-
"""改价失败的子任务不能永久停在估价（needs_repricing 标记 + 定时补算）。

对应体检报告 `docs/健壮性体检_2026-11-10.md` 的：
  🔴11 `_sync_subtask_config()` 的改价异常被 `except: print + rollback()` 吞掉，而
       `_recalc_prices_for_file` 的 SQL 跳过终态 ⇒ 这一单永远停在估价（账单与纸张不符）

每条断言都要求「实现被改坏就会红」：
  · 迁移里删掉 needs_repricing 列 → ① 红
  · except 分支不写标记 → ⑤ 红
  · `resync_pending_prices()` 不清标记 / 不重算父单 → ②③④ 红
  · 调度器不再注册补算任务 → ⑥ 红

跑法：
    PYTHONPATH= PYTHONIOENCODING=utf-8 PYTHONUTF8=1 \
        C:/Users/Administrator/AppData/Local/Programs/Python/Python312/python.exe \
        other/tests/verify_repricing_sweep.py
"""
import os
import sys
import shutil
import sqlite3
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BACKEND_DIR = os.path.join(REPO, "mobile_apps", "printer-backend")

_ok = []
_fail = []


def check(name, cond, detail=""):
    if cond:
        _ok.append(name)
        print(f"  [PASS] {name}")
    else:
        _fail.append(name)
        print(f"  [FAIL] {name}" + (f"  ← {detail}" if detail else ""))


TMP = tempfile.mkdtemp(prefix="hn_repricing_")
DST = os.path.join(TMP, "printer-backend")
shutil.copytree(
    BACKEND_DIR, DST,
    ignore=shutil.ignore_patterns("__pycache__", "orders.db", "users.db", "uploads",
                                  "logs", "venv", "*.bak-*", "*.pyc"))
sys.path.insert(0, DST)
os.chdir(DST)

import app as backend  # noqa: E402

backend.init_db()
DB = backend.DATABASE if isinstance(getattr(backend, "DATABASE", None), str) else None
if not DB:
    DB = getattr(backend, "DB_PATH", None) or os.path.join(DST, "orders.db")


def db():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


SRC = open(os.path.join("app.py"), "r", encoding="utf-8").read()

print("── ① 静态：列、标记、补算函数、调度器都在 ──")
check("①-a 迁移里有 needs_repricing 列",
      "ALTER TABLE order_files ADD COLUMN needs_repricing" in SRC)
check("①-b except 分支会写标记",
      "needs_repricing = 1" in SRC)
check("①-c 有 resync_pending_prices()", "def resync_pending_prices()" in SRC)
check("①-d 调度器注册了周期补算",
      'id="resync_prices"' in SRC and "resync_pending_prices" in SRC)


def insert_order_and_file(oid, fid, budget_total, real_page_count, duplex, page_range="",
                          copies=1, needs=1):
    c = db()
    _ocols = [r[1] for r in c.execute("PRAGMA table_info(orders)").fetchall()]
    _fcols = [r[1] for r in c.execute("PRAGMA table_info(order_files)").fetchall()]
    ov = {"id": oid, "order_number": f"HN20261110-{oid:04d}", "status": "sent",
          "total_price": budget_total, "page_count": real_page_count, "schedule_mode": "now",
          "created_at": "2026-11-10 15:00:00", "delivery_enabled": 0,
          "delivery_percentage": 0, "urgency_price": 0, "cover_page": 0,
          "cover_page_price": 0}
    fv = {"id": fid, "order_id": oid, "file_id": f"file-{fid}", "status": "sent",
          "copies": copies, "duplex": duplex, "page_range": page_range,
          "page_count": real_page_count, "total_price": budget_total,
          "needs_repricing": needs, "created_at": "2026-11-10 15:00:00"}
    # 表的 NOT NULL 列比这里写的多（orders.file 等）—— 给没指定的补类型默认值，
    # 免得测试被建表约束卡住（关心的是价格列，不是每一列）。
    for _r in c.execute("PRAGMA table_info(orders)").fetchall():
        _col, _typ, _notnull = _r[1], (_r[2] or "").upper(), _r[3]
        if _notnull and _col not in ov and _r[4] is None:
            ov[_col] = 0 if ("INT" in _typ or "NUM" in _typ or "REAL" in _typ) else ""
    c.execute("INSERT OR REPLACE INTO orders (%s) VALUES (%s)" % (
        ",".join(k for k in ov if k in _ocols),
        ",".join("?" for k in ov if k in _ocols)),
        tuple(ov[k] for k in ov if k in _ocols))
    for _r in c.execute("PRAGMA table_info(order_files)").fetchall():
        _col, _typ, _notnull = _r[1], (_r[2] or "").upper(), _r[3]
        if _notnull and _col not in fv and _r[4] is None:
            fv[_col] = 0 if ("INT" in _typ or "NUM" in _typ or "REAL" in _typ) else ""
    c.execute("INSERT OR REPLACE INTO order_files (%s) VALUES (%s)" % (
        ",".join(k for k in fv if k in _fcols),
        ",".join("?" for k in fv if k in _fcols)),
        tuple(fv[k] for k in fv if k in _fcols))
    c.commit()
    c.close()


print("── ② 运行期：待补算的单被按实际配置重算 ──")
insert_order_and_file(7701, 77001, 99.99, 4, "off", page_range="1-4", copies=2)
_expect = round(backend.calculate_price(4, "off", "1-4", simplex_pages=None) * 2, 2)
_n = backend.resync_pending_prices()
c = db()
_row = c.execute("SELECT total_price, needs_repricing FROM order_files WHERE id=77001").fetchone()
_o = c.execute("SELECT total_price FROM orders WHERE id=7701").fetchone()
c.close()
check("②-a 扫描到了并返回补算条数", int(_n or 0) >= 1, _n)
check("②-b 子任务价格改成按实际配置算（不是估价 99.99）",
      abs(float(_row["total_price"]) - _expect) < 0.01,
      f"{_row['total_price']} vs {_expect}")
check("②-c 标记被清掉（幂等，不会每 10 分钟重算一次）",
      int(_row["needs_repricing"] or 0) == 0, _row["needs_repricing"])
check("②-d 父订单总额跟着改", abs(float(_o["total_price"]) - _expect) < 0.01,
      _o["total_price"])

print("── ③ 不误伤：没有标记的单一条都不动 ──")
insert_order_and_file(7702, 77002, 12.34, 2, "on", needs=0)
backend.resync_pending_prices()
c = db()
_r2 = c.execute("SELECT total_price, needs_repricing FROM order_files WHERE id=77002").fetchone()
c.close()
check("③-a 未标记的价格不变", abs(float(_r2["total_price"]) - 12.34) < 0.01, _r2["total_price"])
check("③-b 未标记的还是未标记", int(_r2["needs_repricing"] or 0) == 0)

print("── ④ 幂等：再扫一遍不会改动已算好的单 ──")
_now_before = float(db().execute(
    "SELECT total_price FROM order_files WHERE id=77001").fetchone()[0])
backend.resync_pending_prices()
_now_after = float(db().execute(
    "SELECT total_price FROM order_files WHERE id=77001").fetchone()[0])
check("④-a 重复扫描价格稳定", abs(_now_before - _now_after) < 0.001,
      f"{_now_before} -> {_now_after}")

print("── ⑤ 运行期：改价抛异常时打标记（而不是静默停在估价）──")
insert_order_and_file(7703, 77003, 55.55, 3, "on", needs=0)
_real_calc = backend.calculate_price


def _boom(*a, **k):
    raise RuntimeError("模拟改价失败")


backend.calculate_price = _boom
try:
    backend._sync_subtask_config(77003, copies=1, duplex="on",
                                 page_range="", page_count=3)
    _crashed = None
except Exception as e:  # noqa: BLE001
    _crashed = f"{type(e).__name__}: {e}"
finally:
    backend.calculate_price = _real_calc
c = db()
_r3 = c.execute("SELECT needs_repricing, total_price FROM order_files WHERE id=77003").fetchone()
c.close()
check("⑤-a 同步本身不抛（吞掉异常是既有语义）", _crashed is None, _crashed)
check("⑤-b 失败后打了 needs_repricing=1（还有救）",
      int(_r3["needs_repricing"] or 0) == 1, _r3["needs_repricing"])
check("⑤-c 价格暂时仍是原值（等补算）", abs(float(_r3["total_price"]) - 55.55) < 0.01,
      _r3["total_price"])
backend.resync_pending_prices()
c = db()
_r3b = c.execute("SELECT needs_repricing, total_price FROM order_files WHERE id=77003").fetchone()
c.close()
check("⑤-d 补算把它救回来了", int(_r3b["needs_repricing"] or 0) == 0
      and abs(float(_r3b["total_price"]) - 55.55) > 0.001,
      f"needs={_r3b['needs_repricing']} price={_r3b['total_price']}")

print("── ⑥ 补算不会把「一单里其它文件的钱」算丢 ──")
insert_order_and_file(7704, 77004, 10.00, 2, "on", needs=0)
insert_order_and_file(7704, 77005, 0.01, 6, "on", needs=1)
backend.resync_pending_prices()
c = db()
_sum = c.execute("SELECT COALESCE(SUM(total_price),0) s FROM order_files WHERE order_id=7704").fetchone()["s"]
_o = c.execute("SELECT total_price FROM orders WHERE id=7704").fetchone()["total_price"]
c.close()
check("⑥-a 父单 = 两个子任务之和（没有把兄弟文件的金额吞掉）",
      abs(float(_o) - float(_sum)) < 0.02, f"{_o} vs {_sum}")

print()
print(f"PASS {len(_ok)}  FAIL {len(_fail)}")
if _fail:
    print("存在失败 ❌")
    sys.exit(1)
print("全部通过 ✅")
