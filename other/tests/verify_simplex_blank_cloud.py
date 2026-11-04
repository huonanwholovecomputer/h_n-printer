# -*- coding: utf-8 -*-
"""云端「封面单独打印」/「排除末尾空白页」验收（后端口径 + 与本地工具逐格对齐）

只读真实源码，不重新实现口径：
  1. 后端 `_charge_units` / `calculate_price` / `_file_range_stats`（真实 app.py）
  2. 本地 `manual_duplex.charge_units` / `count_sheets` / `printer_config.calc_cost`
  3. `_plan_order_files` 的校验打回（真实 sqlite 提交计划）
  4. 迁移：order_files 两列存在
  5. `on_page_range_truncated` 截断后重算价格（排除空白页不收钱）

用法：python tests/verify_simplex_blank_cloud.py
"""
import itertools
import io
import os
import shutil
import sqlite3
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BACKEND = os.path.join(REPO, "mobile_apps", "printer-backend")
LOCAL = os.path.join(REPO, "local_print_tool")

SP, DP = 0.3, 0.4          # 线上现行单价（/api/pricing 实测）
failures = []


def check(name, cond, detail=""):
    if not cond:
        failures.append(name)
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


tmp = tempfile.mkdtemp(prefix="hn_simplex_cloud_")
dst = os.path.join(tmp, "printer-backend")
shutil.copytree(BACKEND, dst, ignore=shutil.ignore_patterns(
    "__pycache__", "orders.db", "users.db", "uploads", "logs"))
sys.path.insert(0, dst)
# 后端 `DATABASE = "orders.db"` 是**相对 cwd** 的 ⇒ 整个测试都在临时副本里跑，
# 绝不能把 orders.db / users.db / devices.json / uploads 落到仓库根（那样会污染真实库）。
os.chdir(dst)
import app as backend  # noqa: E402

DB_FILE = os.path.join(dst, "orders.db")

sys.path.insert(0, LOCAL)
import manual_duplex as md  # noqa: E402
import printer_config as local_cfg  # noqa: E402

backend._load_paper_prices = lambda: (SP, DP)


# ============================================================
# ① 纸张拆分：后端 vs 本地（逐格）
# ============================================================
print("== ① 张数/计价口径：后端 _charge_units vs 本地 manual_duplex.charge_units ==")
cases = []
for total in (1, 2, 5, 8, 16, 31, 40):
    pages = list(range(1, total + 1))
    for sx in ((), (1,), (1, 2), (1, 16, 17, 18, 19, 20, 21, 22, 23),
               tuple(range(2, total + 1, 2))):
        sx = tuple(p for p in sx if p <= total)
        cases.append((total, "", sx))
        cases.append((total, "2-31" if total >= 31 else "1-%d" % total, sx))

bad = []
for total, prange, sx in cases:
    sxp = ",".join(str(p) for p in sx)
    b_dup, b_single = backend._charge_units(total, prange, sxp)
    l_pages = backend._selected_page_list(total, prange)
    l_dup, l_single = md.charge_units(l_pages, set(sx))
    if (b_dup, b_single) != (l_dup, l_single):
        bad.append((total, prange, sx, (b_dup, b_single), (l_dup, l_single)))
check("①a %d 组张数拆分与本地工具逐格一致" % len(cases), not bad, bad[:3])
check("①b 31 页 1,16-23 单面 = 11 双面张 + 9 单面张",
      backend._charge_units(31, "", "1,16-23") == (11, 9),
      backend._charge_units(31, "", "1,16-23"))
check("①c 31 页纯双面 = 15/1（与老口径一致）",
      backend._charge_units(31, "", "") == (15, 1), backend._charge_units(31, "", ""))

# 金额：后端 calculate_price vs 本地 calc_cost
bad_money = []
for total, prange, sx in cases:
    sxp = ",".join(str(p) for p in sx)
    b = backend.calculate_price(total, "on", prange, prices=(SP, DP), simplex_pages=sxp)
    l_cost, _f = local_cfg.calc_cost(total, 1, "on", SP, DP, prange, sxp)
    if abs(round(l_cost, 2) - b) > 1e-9:
        bad_money.append((total, prange, sx, b, round(l_cost, 2)))
check("①d %d 组金额与本地 calc_cost 一致" % len(cases), not bad_money, bad_money[:3])
check("①e 31 页 + 1,16-23 单面 = ¥7.10",
      backend.calculate_price(31, "on", "", prices=(SP, DP), simplex_pages="1,16-23") == 7.1)
check("①f 不传 simplex 时与老口径逐字相同（31 页 = ¥6.30）",
      backend.calculate_price(31, "on", "", prices=(SP, DP)) == 6.3
      and backend.calculate_price(31, "on", "2-31", prices=(SP, DP)) == 6.0)
check("①g 单面打印时 simplex 无意义（¥0.30×31 页 = 9.30）",
      abs(backend.calculate_price(31, "off", "", prices=(SP, DP), simplex_pages="1") - 9.3) < 1e-9)

# 张数统计：_file_range_stats.sheets vs 本地 count_sheets
bad_sheets = []
for total, prange, sx in cases:
    sxp = ",".join(str(p) for p in sx)
    st = backend._file_range_stats(total, 1, "on", prange, sxp)
    l_pages = backend._selected_page_list(total, prange)
    want = md.count_sheets(l_pages, set(sx))
    if st["sheets"] != want:
        bad_sheets.append((total, prange, sx, st["sheets"], want))
    if st["effective_pages"] != len(l_pages):
        bad_sheets.append((total, prange, sx, "eff", st["effective_pages"], len(l_pages)))
check("①h %d 组 sheets/effective_pages 与本地一致" % len(cases), not bad_sheets, bad_sheets[:3])

# ============================================================
# ② 校验与格式化
# ============================================================
print("== ② simplex_pages 校验（错的要打回，不是静默忽略） ==")
check("②a 合法串归一化：'1, 16-23' → '1,16-23'",
      backend._simplex_pages_checked("1, 16-23", "", 31, True) == ("1,16-23", ""))
check("②b 中文逗号/顿号/分号归一化",
      backend._simplex_pages_checked("1、3；20", "", 31, True) == ("1,3,20", ""))
check("②c 格式错 → 打回文案",
      backend._simplex_pages_checked("abc", "", 31, True)[1]
      == "「封面单独打印」页码格式不正确：abc（应为如 1-5 或 7）")
check("②d 倒序区间 23-4 → 格式错（不静默拆成 2,3-4）",
      backend._simplex_pages_checked("23-4", "", 31, True)[1].startswith("「封面单独打印」页码格式不正确"))
check("②e 超出总页数 → 打回文案",
      backend._simplex_pages_checked("1,32", "", 31, True)[1]
      == "「封面单独打印」第 32 页超出文件总页数（文件共 31 页）")
check("②f 不在页码范围内 → 打回文案",
      backend._simplex_pages_checked("1", "2-31", 31, True)[1]
      == "「封面单独打印」第 1 页不在页码范围「2-31」内")
check("②g 页数未验证时只查格式，不误判越界",
      backend._simplex_pages_checked("1,999", "", 1, False) == ("1,999", ""))
check("②h 空串 → 不启用、不打回", backend._simplex_pages_checked("   ", "", 31, True) == ("", ""))
check("②i 去重排序：'5,1,1' → '1,5'",
      backend._simplex_pages_checked("5,1,1", "", 31, True) == ("1,5", ""))
check("②j 连续页压缩：'1,2,3,16,17' → '1-3,16-17'",
      backend._format_page_selection({1, 2, 3, 16, 17}) == "1-3,16-17")

# ============================================================
# ③ 迁移：两列落地
# ============================================================
print("== ③ order_files 迁移 ==")
backend.init_db()
conn = sqlite3.connect(DB_FILE)
conn.row_factory = sqlite3.Row
cols = [r[1] for r in conn.execute("PRAGMA table_info(order_files)").fetchall()]
check("③a order_files.simplex_pages 存在", "simplex_pages" in cols, cols[-6:])
check("③b order_files.blank_tail_exclude 存在", "blank_tail_exclude" in cols)

# ============================================================
# ④ _plan_order_files：校验打回 + 计价 + 字段
# ============================================================
print("== ④ 提交计划 _plan_order_files ==")
conn.execute("DELETE FROM files WHERE id = ?", ("f_simplex_test",))
conn.execute(
    "INSERT INTO files (id, original_name, saved_name, path, size, created_at,"
    " page_count, page_count_verified, openid)"
    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
    ("f_simplex_test", "人工智能程序设计习题.pdf", "saved_simplex_test.pdf", "",
     1024, "2026-11-02 23:00:00", 31, 1, "openid_test"),
)
conn.commit()
prices = (SP, DP)


def plan_for(**kw):
    f = {"file_id": "f_simplex_test", "file": "人工智能程序设计习题.pdf",
         "copies": 1, "duplex": "on", "page_range": ""}
    f.update(kw)
    return backend._plan_order_files(conn, [f], "openid_test", "on", "now", prices)[0]


p1 = plan_for(simplex_pages="1,16-23")
check("④a 混排计价 ¥7.10", p1["total_price"] == 7.10, p1["total_price"])
check("④b 归一化后写计划", p1["simplex_pages"] == "1,16-23", p1["simplex_pages"])
check("④c 合法时不被拒", p1["status"] == "queued", p1["status"])

p2 = plan_for(simplex_pages="1,99")
check("④d 越界 → rejected", p2["status"] == "rejected", p2["status"])
check("④e 越界文案",
      p2["reject_reason"] == "「封面单独打印」第 99 页超出文件总页数（文件共 31 页）",
      p2["reject_reason"])

p3 = plan_for(simplex_pages="1", page_range="2-31")
check("④f 不在页码范围 → rejected",
      p3["status"] == "rejected"
      and p3["reject_reason"] == "「封面单独打印」第 1 页不在页码范围「2-31」内",
      p3["reject_reason"])

p4 = plan_for(simplex_pages="2", page_range="2-31")
# 与本地工具同口径对照（不写死数字）：2-31 + 单面页 {2} ⇒ 14 张双面 + 2 个单面单位
_local4 = round(local_cfg.calc_cost(31, 1, "on", 0.3, 0.4, "2-31", "2")[0], 2)
check("④g 范围内 → 与本地 calc_cost 同价 %.2f" % _local4,
      p4["status"] == "queued" and abs(float(p4["total_price"]) - _local4) < 1e-6,
      (p4["status"], p4["total_price"]))

p5 = plan_for(simplex_pages="abc")
check("④h 格式错 → rejected", p5["status"] == "rejected", p5["status"])

p6 = plan_for(duplex="off", simplex_pages="1")
check("④i 单面打印时 simplex 被忽略（不报错、不涨价）",
      p6["status"] == "queued" and p6["simplex_pages"] == ""
      and abs(p6["total_price"] - 9.3) < 1e-9, (p6["status"], p6["total_price"]))

p7 = plan_for(blank_tail_exclude=1)
check("④j blank_tail_exclude 落计划", p7["blank_tail_exclude"] == 1, p7["blank_tail_exclude"])
p8 = plan_for()
check("④k 不传 = 0（旧客户端零变化）",
      p8["blank_tail_exclude"] == 0 and p8["simplex_pages"] == "" and p8["total_price"] == 6.3)

# ============================================================
# ⑤ 截断回报 → 重算价格（排除空白页不收钱）
# ============================================================
print("== ⑤ page_range_truncated 后重算 ==")
# orders.id 是 INTEGER 主键自增、没有 order_number 列 ⇒ 不能手塞字符串 id。
cur = conn.execute(
    "INSERT INTO orders (file_id, file, copies, status, created_at, openid, duplex,"
    " page_count, total_price) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
    ("f_simplex_test", "人工智能程序设计习题.pdf", 1, "queued", "2026-11-02 23:00:00",
     "openid_test", "on", 31, 6.0),
)
oid = cur.lastrowid
conn.execute(
    "INSERT INTO order_files (order_id, file_id, file_name, copies, page_count, page_range,"
    " price_per_page, total_price, is_free, status, created_at, duplex, image_orientation,"
    " simplex_pages, blank_tail_exclude)"
    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
    (oid, "f_simplex_test", "人工智能程序设计习题.pdf", 1, 31, "", 0, 6.0, 0,
     "queued", "2026-11-02 23:00:00", "on", "auto", "1", 1),
)
conn.commit()
tid = conn.execute("SELECT id FROM order_files WHERE order_id = ?", (oid,)).fetchone()[0]
conn.close()

_orig_client = backend._get_event_client_id
_orig_own = backend._subtask_ownership
backend._get_event_client_id = lambda: "client_test"
backend._subtask_ownership = lambda task_id, client_id, conn=None: (
    True, {"status": "queued", "basis": "order_files.operator_client", "owner": client_id})
try:
    with backend.app.test_request_context("/"):
        backend.on_page_range_truncated({
            "task_id": tid, "original_range": "1-31", "effective_range": "1-28",
            "total_pages": 31,
        })
finally:
    backend._get_event_client_id = _orig_client
    backend._subtask_ownership = _orig_own

conn = sqlite3.connect(DB_FILE)
conn.row_factory = sqlite3.Row
row = conn.execute("SELECT * FROM order_files WHERE id = ?", (tid,)).fetchone()
check("⑤a 页码被改写为排除后的范围", row["page_range"] == "1-28", row["page_range"])
check("⑤b page_range_truncated 落库", row["page_range_truncated"] == 1)
# 1-28 + 单面页 {1}：1 张单面 + (2..28)=27 页 ⇒ 13 双面 + 1 单面收尾 = 5.80
_local5 = round(local_cfg.calc_cost(31, 1, "on", 0.3, 0.4, "1-28", "1")[0], 2)
check("⑤c 价格按 28 页重算（与本地同价 %.2f）" % _local5,
      abs(float(row["total_price"]) - _local5) < 1e-6, row["total_price"])
check("⑤d simplex 收敛到新范围后仍为 '1'", row["simplex_pages"] == "1", row["simplex_pages"])
order = conn.execute("SELECT total_price FROM orders WHERE id = ?", (oid,)).fetchone()
check("⑤e 父订单总额同步", abs(float(order["total_price"]) - _local5) < 1e-6,
      order["total_price"])
conn.close()

# ============================================================
# ⑥ 下发：设备端拿得到两个新选项
# ============================================================
print("== ⑥ print_task 下发字段 ==")
src = io.open(os.path.join(dst, "app.py"), encoding="utf-8").read()
check("⑥a options 带 simplex_pages", '"simplex_pages": simplex_pages or "",' in src)
check("⑥b options 带 blank_tail_exclude", '"blank_tail_exclude": int(blank_tail_exclude or 0),' in src)
check("⑥c 普通分发把行里的两个值传下去",
      'simplex_pages=_row_simplex_pages(row)' in src
      and 'blank_tail_exclude=_row_blank_tail_exclude(row)' in src)
check("⑥d submit 后即时推送也带上",
      src.count('simplex_pages=st.get("simplex_pages", "")') == 2)

print()
if failures:
    print("❌ 失败 %d 项：" % len(failures))
    for f in failures:
        print("   -", f)
    sys.exit(1)
print("✅ 全部通过")
sys.exit(0)
