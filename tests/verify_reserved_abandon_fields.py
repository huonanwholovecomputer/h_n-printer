# -*- coding: utf-8 -*-
"""验收 · 「预留订单放弃」不再误盖章为「自打」、金额不再用错单价

背景（2026-10-08 现场数据 HN20261008-0025/0026）：
  · 两单是「复制价格」后的预留单，从未点开始打印，22:25 工具退出时被放弃。
  · 后端 /api/abandon_reserved_order 的 UPDATE 里有
    `is_admin_print = CASE WHEN is_admin_print = 0 THEN 1 ELSE is_admin_print END`
    —— 把「接单打印」的顾客订单强制盖章成「自打」（类型列/结算收益分类/管理员订单统计
    都按 is_admin_print 区分）。而走「超时清理」定时任务放弃的 0021~0024 不受影响，
    于是同一批 6 单里 4 单接单、2 单自打。
  · 同一处还有个金额 bug：gui.py 的放弃调用点 `calc_cost(page, copies, duplex, page_range=…)`
    漏传单价 → 用了 calc_cost 的历史默认值 0.2/0.3，把订单金额写成了 ¥3.20/¥9.20，
    而文件级金额是按本机配置 0.3/0.4 算的 ¥4.30/¥12.30（父子口径不一致）。

本脚本：
  A. 静态接线：工具 3 个放弃调用点必须显式传单价 + 归属/自打标记；cloud_client 必须上报；
     后端必须读取并以 COALESCE 回退（旧版工具仍走历史缺省）。
  B. 行为断言：把 app.py 里**真实那条 UPDATE SQL** 抽出来，在临时 SQLite 库上跑
     「旧版工具 / 新版报 0 / 新版报 1 / 归属回退 / 重复调用幂等」五种场景。
用法：python tests/verify_reserved_abandon_fields.py     （exit 0 = 全部通过）
"""
import os
import re
import sqlite3
import sys

# 控制台可能是 GBK（Windows），直接 print ✅/❌ 会 UnicodeEncodeError
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GUI = os.path.join(REPO, "local_print_tool", "gui.py")
CLIENT = os.path.join(REPO, "local_print_tool", "cloud_client.py")
APP = os.path.join(REPO, "mobile_apps", "printer-backend", "app.py")
CONFIG = os.path.join(REPO, "local_print_tool", "printer_config.py")

bad = 0


def chk(name, cond, detail=""):
    global bad
    if not cond:
        bad += 1
    print("[%s] %s%s" % ("PASS" if cond else "FAIL", name, ("  -- " + detail) if (not cond and detail) else ""))


gui = open(GUI, encoding="utf-8").read()
client = open(CLIENT, encoding="utf-8").read()
app = open(APP, encoding="utf-8").read()
cfg = open(CONFIG, encoding="utf-8").read()

print("=== A. 静态接线 ===")
# A1 每个「预留放弃」调用点都必须传**整单合计**（不是本文件的价）
#    历史 bug：循环里逐文件 calc_cost 上报 → 后端只认第一次命中 → 订单额 = 第一个文件的价
#    v4.5.18（🔴-B2）：三个标签页删除/清空入口（删除单个 / 清空全部 / 删除已完成）的
#    放弃逻辑已收敛为公共方法 `_abandon_tab_jobs`，调用点由 3 处变为 2 处
#    （公共方法 + 退出清理 closeEvent）。语义等价，断言改为「每个调用点都满足」。
sites = [m.start() for m in re.finditer(r"abandon_reserved_order\(", gui)]
chk("gui.py 预留放弃调用点 ≥2（三处标签页入口已合并为 _abandon_tab_jobs）",
    len(sites) >= 2, "实际 %d 处" % len(sites))
tabtotal = 0
for pos in sites:
    if "self._tab_total_price(" in gui[pos:pos + 400]:
        tabtotal += 1
chk("每个调用点都传整单合计 _tab_total_price(…)", tabtotal == len(sites),
    "只有 %d/%d 处" % (tabtotal, len(sites)))
chk("_tab_total_price 用本机配置单价（不落 calc_cost 的 0.2/0.3 默认值）",
    re.search(r"def _tab_total_price\(self, tab\)[\s\S]{0,1500}?self\._config\.simplex_price,"
              r" self\._config\.duplex_price", gui) is not None)
chk("_tab_total_price 计入附加服务费（calc_extra_total）",
    re.search(r"def _tab_total_price\(self, tab\)[\s\S]{0,1800}?calc_extra_total\(", gui) is not None)
chk("已无漏传单价的旧写法（calc_cost(..., page_range=...)）",
    not re.search(r"calc_cost\(job\.page_count, job\.copies, job\.duplex, page_range=", gui))
chk("已无「逐文件上报金额」的旧写法（放弃调用点之前不再逐文件 calc_cost）",
    all(not re.search(r"calc_cost\(job\.page_count", gui[max(0, pos - 900):pos]) for pos in sites))
chk("calc_cost 的默认单价确为历史遗留 0.2/0.3（说明必须显式传）",
    re.search(r"simplex_price: float = 0\.2,\s*\n\s*duplex_price: float = 0\.3", cfg) is not None)

# A2 每个调用点都上报归属/自打
sends = re.findall(r"abandon_reserved_order\(\s*\n?\s*job\.order_number, self\._tab_total_price\("
                   r"[^)]*\),\s*\n?\s*owner_name=", gui)
chk("每个调用点都上报 owner_name/is_admin_print", len(sends) == len(sites),
    "实际 %d 处（调用点 %d）" % (len(sends), len(sites)))

# A3 cloud_client 支持并携带这两个字段
chk("abandon_reserved_order 签名含 owner_name/is_admin_print",
    re.search(r"def abandon_reserved_order\(self, order_number: str, total_price: float = 0\.0,\s*\n\s*owner_name: str = \"\", is_admin_print=None\)", client) is not None)
chk("payload 携带 owner_name", 'payload["owner_name"] = owner_name' in client)
chk("payload 携带 is_admin_print", 'payload["is_admin_print"] = bool(is_admin_print)' in client)

# A4 后端读取并以 COALESCE 回退（旧版工具兼容）
chk("后端读取 is_admin_print", 'is_admin_print_in = data.get("is_admin_print")' in app)
chk("后端读取 owner_name", 'owner_name_in = (data.get("owner_name") or "").strip()' in app)
chk("SQL 用 COALESCE 回退 is_admin_print（保留旧版缺省行为）",
    re.search(r"is_admin_print = COALESCE\(\?,\s*\n\s*CASE WHEN is_admin_print = 0 THEN 1 ELSE is_admin_print END\)", app) is not None)
chk("SQL 用 COALESCE 回退 owner_name",
    re.search(r"owner_name = COALESCE\(NULLIF\(\?, ''\), NULLIF\(owner_name, ''\), \?\)", app) is not None)

print("=== A5. 整单合计行为（真实方法源码 + 假标签页）===")
sys.path.insert(0, os.path.join(REPO, "local_print_tool"))
from printer_config import calc_cost          # noqa: E402  真实计费函数

mt = re.search(r"    def _tab_total_price\(self, tab\) -> float:[\s\S]*?\n        return round\(total, 2\)\n", gui)
chk("能从 gui.py 抽出 _tab_total_price 真实源码", mt is not None)
if mt:
    import textwrap
    ns = {"calc_cost": calc_cost}
    exec(textwrap.dedent(mt.group(0)), ns)     # noqa: S102  故意执行真实源码
    tab_total_price = ns["_tab_total_price"]

    class _Cfg:
        simplex_price = 0.3
        duplex_price = 0.4

    class _Job:
        def __init__(self, pages, copies=1, duplex="on", page_range=""):
            self.page_count, self.copies = pages, copies
            self.duplex, self.page_range = duplex, page_range

    class _Tab:
        def __init__(self, jobs, extra=0.0):
            self.jobs, self._extra = jobs, extra

        def calc_extra_total(self, total, cfg):
            return self._extra

    class _Me:
        _config = _Cfg()

    # 复刻线上真实订单 HN20261011-0001：31页/1份/双面 + 5页/1份/双面（附加费 0）
    got = tab_total_price(_Me(), _Tab([_Job(31), _Job(5)]))
    chk("整单合计 = 6.30 + 1.10 = ¥7.40", abs(got - 7.40) < 1e-9, "%.2f" % got)
    first_only = round(calc_cost(31, 1, "on", 0.3, 0.4)[0], 2)
    chk("旧写法「只取第一个文件」会得到 ¥6.30（= 线上被写错的订单额）", abs(first_only - 6.30) < 1e-9, "%.2f" % first_only)
    chk("整单合计 ≠ 第一个文件的价（bug 已修）", abs(got - first_only) > 1e-9)
    chk("附加服务费计入整单合计（+0.10 → ¥7.50）",
        abs(tab_total_price(_Me(), _Tab([_Job(31), _Job(5)], extra=0.10)) - 7.50) < 1e-9)
    chk("空标签页/None → 0", tab_total_price(_Me(), None) == 0.0 and tab_total_price(_Me(), _Tab([])) == 0.0)

print("=== B. 真实 SQL 行为（临时库）===")
m = re.search(r'"""(UPDATE orders SET status = \'abandoned\'[\s\S]*?WHERE order_number = \? AND status = \'reserved\')"""', app)
chk("能从 app.py 抽出放弃预留单的 UPDATE SQL", m is not None)
if not m:
    sys.exit(1)
SQL = m.group(1)
dm = re.search(r'DEFAULT_OWNER_NAME\s*=\s*"([^"]*)"', app)
DEFAULT_OWNER = dm.group(1) if dm else "本地任务"
chk("能取到 DEFAULT_OWNER_NAME", bool(dm), DEFAULT_OWNER)


def fresh(is_admin_print, owner, status="reserved", total=0.0):
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, order_number TEXT, status TEXT,"
                 " total_price REAL, owner_name TEXT, is_admin_print INTEGER)")
    conn.execute("INSERT INTO orders (order_number, status, total_price, owner_name, is_admin_print)"
                 " VALUES ('HN-T', ?, ?, ?, ?)", (status, total, owner, is_admin_print))
    return conn


def run(conn, total_price, owner_in, admin_in, order_number="HN-T"):
    cur = conn.execute(SQL, (total_price, owner_in, DEFAULT_OWNER, admin_in, order_number))
    return cur.rowcount, conn.execute(
        "SELECT status, total_price, owner_name, is_admin_print FROM orders WHERE order_number = ?",
        (order_number,)).fetchone()


# ① 旧版工具（不带 is_admin_print / owner_name）→ 保持历史缺省行为：接单(0) 被盖章成自打(1)
c = fresh(0, "")
n, row = run(c, 3.20, "", None)
chk("① 旧版工具：接单单被补记为自打（历史行为保留，向后兼容）", n == 1 and row[3] == 1, str(row))
chk("① 旧版工具：无归属 → 补默认占位名", row[2] == DEFAULT_OWNER, str(row))

# ② 新版工具报 0（接单）→ 必须保持 0（本次修复的核心）
c = fresh(0, "乙成员")
n, row = run(c, 4.30, "乙成员", 0)
chk("② 新版工具报「接单」：is_admin_print 保持 0（不再误盖章）", n == 1 and row[3] == 0, str(row))
chk("② 金额按上报值写入（不再被 0.2/0.3 默认价覆盖）", abs(row[1] - 4.30) < 1e-9, str(row))
chk("② 归属保留成员名", row[2] == "乙成员", str(row))

# ③ 新版工具报 1（自打）→ 保持 1
c = fresh(0, "乙成员")
n, row = run(c, 9.20, "乙成员", 1)
chk("③ 新版工具报「自打」：保持 1", n == 1 and row[3] == 1, str(row))

# ④ 已 abandoned（例如被 30 分钟超时任务先放弃）→ 幂等，不改任何字段
c = fresh(0, "乙成员", status="abandoned", total=2.00)
n, row = run(c, 3.20, "乙成员", 0)
chk("④ 已放弃的订单重复调用 → 0 行受影响（幂等）", n == 0, str(row))
chk("④ 原有金额/归属/自打标记不被覆盖", abs(row[1] - 2.00) < 1e-9 and row[3] == 0, str(row))

# ⑤ 归属为空字符串时回退到原有归属（不被清空）
c = fresh(0, "甲成员")
n, row = run(c, 1.20, "", None)
chk("⑤ 未上报归属时保留原归属（不清空）", row[2] == "甲成员", str(row))

print("=" * 50)
print("预留单放弃字段验收:", "全部通过 ✅" if bad == 0 else "存在失败 ❌")
sys.exit(1 if bad else 0)
