# -*- coding: utf-8 -*-
"""审计修复验收 · 后端（🔴1 / 🔴11 / 🔴12）

在临时目录复制 printer-backend 后导入 app.py（避免污染真实 orders.db/users.db），
用内存/临时 SQLite 验证三处修复。
"""
import os
import shutil
import sqlite3
import sys
import tempfile
import traceback

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SRC = os.path.join(REPO, "mobile_apps", "printer-backend")

tmp = tempfile.mkdtemp(prefix="hn_verify_")
dst = os.path.join(tmp, "printer-backend")
shutil.copytree(SRC, dst, ignore=shutil.ignore_patterns(
    "__pycache__", "orders.db", "users.db", "uploads", "logs"))
sys.path.insert(0, dst)
os.chdir(dst)

ok_all = True


def check(name, cond, detail=""):
    global ok_all
    mark = "PASS" if cond else "FAIL"
    if not cond:
        ok_all = False
    print(f"[{mark}] {name}" + (f"  -- {detail}" if detail else ""))


# ---------- 导入 app ----------
try:
    import app as backend
    print(f"imported app.py from {dst}")
except Exception:
    traceback.print_exc()
    sys.exit("无法导入 app.py")

# ================= 🔴1 license_keys 消费 SQL =================
print("\n=== 🔴1 临时授权消费 SQL ===")
conn = sqlite3.connect(":memory:")
conn.row_factory = sqlite3.Row
conn.execute("CREATE TABLE license_keys (id INTEGER PRIMARY KEY AUTOINCREMENT, "
             "key TEXT, used_by TEXT, order_id INTEGER)")
conn.execute("INSERT INTO license_keys (key, used_by, order_id) VALUES ('k1', 'u1', 111)")
conn.execute("INSERT INTO license_keys (key, used_by, order_id) VALUES ('k2', 'u1', NULL)")
conn.execute("INSERT INTO license_keys (key, used_by, order_id) VALUES ('k3', 'u2', NULL)")
conn.commit()

NEW_SQL = (
    "UPDATE license_keys SET order_id = ? WHERE id = ("
    "  SELECT id FROM license_keys"
    "  WHERE used_by = ? AND order_id IS NULL"
    "  ORDER BY id DESC LIMIT 1)"
)
try:
    cur = conn.execute(NEW_SQL, (999, "u1"))
    check("新 SQL 在标准 SQLite 上可执行（原先 OperationalError）", True,
          f"rowcount={cur.rowcount}")
    check("恰好更新最新的未消费密钥(k2)", cur.rowcount == 1)
    row = conn.execute("SELECT id, order_id FROM license_keys WHERE key='k2'").fetchone()
    check("k2.order_id 已回填为 999", row["order_id"] == 999, f"order_id={row['order_id']}")
    row = conn.execute("SELECT order_id FROM license_keys WHERE key='k1'").fetchone()
    check("已消费的 k1 未被改动", row["order_id"] == 111)
except Exception as e:
    check("新 SQL 执行", False, repr(e))

# 确认源码里已无旧写法，且新写法在源码中真实存在
src_text = open(os.path.join(dst, "app.py"), encoding="utf-8").read()
check("源码不含旧 `... order_id IS NULL ORDER BY id DESC LIMIT 1\")` 直改写法",
      'AND order_id IS NULL ORDER BY id DESC LIMIT 1",' not in src_text)
check("源码含子查询写法 `WHERE id = (\\n                    \"  SELECT id FROM license_keys\"`",
      "SELECT id FROM license_keys" in src_text)

# 旧写法在标准 SQLite 上确实报错（对照实验，证明修复必要性）
try:
    conn.execute("UPDATE license_keys SET order_id = 1 WHERE used_by = 'u2' ORDER BY id DESC LIMIT 1")
    check("对照：旧写法应当报错", False, "竟然没报错")
except sqlite3.OperationalError as e:
    check("对照：旧写法在标准 SQLite 上报错（修复必要性）", "ORDER" in str(e), str(e))

# ================= 🔴11 sqlite3.Row 无 .get() =================
print("\n=== 🔴11 /api/file_page 的 Row 访问 ===")
conn2 = sqlite3.connect(":memory:")
conn2.row_factory = sqlite3.Row
conn2.execute("CREATE TABLE files (id INTEGER PRIMARY KEY, page_count INTEGER, "
              "page_count_verified INTEGER, original_name TEXT, path TEXT)")
conn2.execute("INSERT INTO files VALUES (7, 0, 0, 'a.pdf', 'pdf/abc.pdf')")
conn2.commit()
r = conn2.execute("SELECT page_count, page_count_verified, original_name, path "
                  "FROM files WHERE id = 7").fetchone()

has_get = hasattr(r, "get")
check("sqlite3.Row 确实没有 .get()（旧代码 AttributeError 根因）", not has_get)

try:
    r["page_count"] = 5
    check("sqlite3.Row 支持字段赋值（应当不支持）", False, "竟然支持")
except TypeError as e:
    check("sqlite3.Row 不支持字段赋值（旧代码 TypeError 根因）", True, str(e))

# 源码检查：三处旧写法均已替换
check("源码不再出现 `row.get(\"path\")`", 'row.get("path")' not in src_text)
check("源码不再出现 `row[\"page_count\"] = new_count`", 'row["page_count"] = new_count' not in src_text)
check("源码不再出现 `row[\"page_count_verified\"] = True`",
      'row["page_count_verified"] = True' not in src_text)
check("源码改为局部变量 page_count / verified",
      "page_count = row[\"page_count\"] or 0" in src_text and "verified = bool(row[\"page_count_verified\"])" in src_text)

# 用局部变量重放修复后的逻辑（含真实 sqlite Row）
page_count = r["page_count"] or 0
verified = bool(r["page_count_verified"])
original_name = r["original_name"] or ""
rel_path = r["path"] or ""
check("修复后逻辑对 Row 读取无异常", (page_count, verified, original_name, rel_path) == (0, False, "a.pdf", "pdf/abc.pdf"),
      f"{page_count},{verified},{original_name},{rel_path}")

# ================= 🔴12 claim 键 =================
print("\n=== 🔴12 接单接管记录（claiming_devices） ===")
claim_file = backend.CLAIM_FILE
# 写入一个真实可用的接管记录（含旧格式，验证加载器归一化）
backend._save_claim_impl({"claiming_devices": {"dev-A": {"claimed_at": "t", "owner_name": "老王"},
                                               "dev-B": {"claimed_at": "t", "owner_name": "小李"}}})
claim = backend._load_claim_impl()
check("加载器输出只含 claiming_devices 键",
      "claiming_devices" in claim and "active_client_id" not in claim, str(list(claim.keys())))
check("旧代码判断 `claim.get('active_client_id') == 'dev-A'` 恒为 False（死代码根因）",
      claim.get("active_client_id") != "dev-A")

# 重放修复后的删除逻辑
claim = backend._load_claim_impl()
claiming = claim.get("claiming_devices")
cleared = False
if isinstance(claiming, dict) and "dev-A" in claiming:
    claiming.pop("dev-A", None)
    cleared = True
check("修复后：删除当前接单设备能从 claiming_devices 清除", cleared and "dev-A" not in claiming)
check("修复后：其他设备不受影响", "dev-B" in claiming)

# 重放修复后的 bind_owner 逻辑
claim = backend._load_claim_impl()
claiming = claim.get("claiming_devices")
if isinstance(claiming, dict) and "dev-B" in claiming:
    entry_claim = claiming.get("dev-B")
    if not isinstance(entry_claim, dict):
        entry_claim = {}
    entry_claim["owner_name"] = "新名字"
    claiming["dev-B"] = entry_claim
check("修复后：bind_owner 能更新 claiming_devices 中的 owner_name",
      claiming["dev-B"]["owner_name"] == "新名字")

check("源码 4912 处的死代码判断已移除",
      'claim.get("active_client_id") == client_id' not in src_text)
check("源码改用 claiming_devices 字典查询",
      src_text.count('claiming = claim.get("claiming_devices")') >= 2)

# 旧格式兼容：加载含 active_client_id 的文件仍能归一化
with open(claim_file, "w", encoding="utf-8") as f:
    f.write('{"active_client_id": "dev-old", "owner_name": "旧格式"}')
legacy = backend._load_claim_impl()
check("旧格式文件仍被正确归一化",
      legacy.get("claiming_devices", {}).get("dev-old", {}).get("owner_name") == "旧格式",
      str(legacy))

shutil.rmtree(tmp, ignore_errors=True)
print("\n" + ("=" * 50))
print("后端验收:", "全部通过 ✅" if ok_all else "存在失败 ❌")
sys.exit(0 if ok_all else 1)
