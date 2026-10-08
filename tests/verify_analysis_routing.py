# -*- coding: utf-8 -*-
"""页数分析 / 打印任务 的「设备归属」验收

背景（线上反馈）：用户在小程序里选了**离线的接单设备 A**，另有一台**在线设备 B**，
上传 Word 后发现这个文件被 B 拿去下载+转 PDF 数页。用户担心：
  ① 页数分析是不是随便挑一台在线设备（甚至抢了本该 A 处理的文件）？
  ② 打印任务会不会也被 B 抢走？
  ③ A 重新上线后，这个文件要重新从云端获取吗？

代码事实（本脚本逐条钉住）：
  · 页数分析是**文件级**的（files.page_count + MD5 索引），不是订单级；但"发给谁"有成本
    （要整份下载 + 转 PDF），所以现在按优先级选：调用方指定（前端当前选中的接单设备 /
    刚上线的设备）→ 该文件**活跃订单的目标设备**（orders.target_client）→ 任一在线接单设备。
  · 打印任务严格按订单目标设备分发：目标离线 → 保持 queued 等它上线，绝不推给其它在线设备。
  · A 上线后：若页数仍未知（page_count=0）→ on_connect 补推给 A 自己分析；若页数已由 B
    分析好，则 A 直接复用服务端的页数，打印时再按需从云端下载 + 本地转 PDF（各自缓存）。

用法: python tests/verify_analysis_routing.py   （exit 0 = 全部通过）
"""
import io
import os
import shutil
import sys
import tempfile
import traceback
from datetime import datetime

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(REPO, "mobile_apps", "printer-backend")

ok_all = True


def check(name, cond, detail=""):
    global ok_all
    if not cond:
        ok_all = False
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


tmp = tempfile.mkdtemp(prefix="hn_analysis_")
dst = os.path.join(tmp, "printer-backend")
shutil.copytree(SRC, dst, ignore=shutil.ignore_patterns(
    "__pycache__", "orders.db", "users.db", "uploads", "logs", "venv"))
sys.path.insert(0, dst)
os.chdir(dst)

try:
    import app as backend
    print(f"imported app.py from {dst}")
except Exception:
    traceback.print_exc()
    sys.exit("无法导入 app.py")

# 临时目录里的 orders.db/users.db 由 init_db() 建表（含 target_client 等迁移列）
backend.init_db()

# ==================== 桩：在线设备 / 接单设备 / socketio ====================
CLAIMING = ["A", "B"]
ONLINE = set()


def _set_online(ids):
    ONLINE.clear()
    ONLINE.update(ids)
    backend.printer_clients.clear()
    for cid in ids:
        backend.printer_clients[cid] = {
            "sid": f"sid-{cid}", "heartbeat": datetime.now(), "connected_at": datetime.now(),
        }


class FakeSocketIO:
    def __init__(self):
        self.sent = []

    def emit(self, event, payload=None, to=None):
        self.sent.append({"event": event, "payload": payload, "to": to})


fake_sio = FakeSocketIO()
backend.socketio = fake_sio
backend.get_active_clients = lambda: [c for c in CLAIMING if c in ONLINE]
backend.get_claiming_device_ids = lambda: list(CLAIMING)
backend.printer_clients_lock = backend.printer_clients_lock  # 真实锁即可

# ==================== 造数据：一个 docx 文件 + 一个指向 A 的活跃订单 ====================
docx_path = os.path.join(tmp, "报告.docx")
with open(docx_path, "wb") as f:
    f.write(b"PK\x03\x04fake-docx")

FILE_ID = "filedocx001"
conn = backend.get_db()
conn.execute(
    "INSERT OR REPLACE INTO files (id, original_name, saved_name, path, size, created_at,"
    " page_count, page_count_verified, md5, openid)"
    " VALUES (?,?,?,?,?,?,?,?,?,?)",
    (FILE_ID, "报告.docx", "报告.docx", docx_path, os.path.getsize(docx_path),
     datetime.now().strftime("%Y-%m-%d %H:%M:%S"), 0, 0, "md5docx001", "openid_test"),
)
conn.execute(
    "INSERT INTO orders (file, copies, status, created_at, openid, duplex, page_count,"
    " price_per_page, total_price, is_free, target_client, order_number)"
    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
    ("报告.docx", 1, "queued", datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "openid_test",
     "on", 0, 0.3, 0, 0, "A", "HN-TEST-0001"),
)
order_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
conn.execute(
    "INSERT INTO order_files (order_id, file_id, file_name, copies, page_count, price_per_page,"
    " total_price, is_free, status, created_at)"
    " VALUES (?,?,?,?,?,?,?,?,?,?)",
    (order_id, FILE_ID, "报告.docx", 1, 0, 0.3, 0, 0, "queued",
     datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
)
conn.commit()
conn.close()

# ==================== ① 分析设备选择优先级 ====================
print("\n=== ① _resolve_analysis_client 优先级 ===")
_set_online(["B"])   # A 离线、B 在线；订单目标设备 = A

cid, why = backend._resolve_analysis_client("B", FILE_ID)
check("调用方指定在线设备 → 用它", cid == "B" and why == "调用方指定", f"{cid} / {why}")

cid, why = backend._resolve_analysis_client("A", FILE_ID)
check("指定设备离线 → 退回订单目标设备（A 离线则继续兜底到任一在线接单设备）",
      cid == "B", f"{cid} / {why}")

cid, why = backend._resolve_analysis_client("", FILE_ID)
check("未指定 → 订单目标 A 离线 → 兜底任一在线接单设备 B",
      cid == "B" and why == "任一在线接单设备", f"{cid} / {why}")

_set_online(["A", "B"])
cid, why = backend._resolve_analysis_client("", FILE_ID)
check("订单目标 A 在线 → 用订单目标设备（谁接单谁分析）",
      cid == "A" and why == "订单目标设备", f"{cid} / {why}")

_set_online(["A"])
cid, why = backend._resolve_analysis_client("B", FILE_ID)
check("指定设备离线、订单目标 A 在线 → 用 A", cid == "A", f"{cid} / {why}")

_set_online([])
cid, why = backend._resolve_analysis_client("A", FILE_ID)
check("无在线接单设备 → 返回空（保持未分析，等设备上线补推）", cid == "" and "无在线" in why, f"{cid} / {why}")

# ==================== ② request_page_analysis 真的只推给选中的设备 ====================
print("\n=== ② request_page_analysis 推送目标 ===")
_set_online(["B"])
fake_sio.sent.clear()
backend._last_page_analysis_push.clear()
ok = backend.request_page_analysis(FILE_ID, "报告.docx", prefer_client="B")
sent = [s for s in fake_sio.sent if s["event"] == "analyze_page_count"]
check("分析请求发给了选中的设备（sid-B）", ok and len(sent) == 1 and sent[0]["to"] == "sid-B",
      f"ok={ok} to={[s['to'] for s in sent]}")
check("payload 带 file_id / download_url（设备据此下载）",
      sent and sent[0]["payload"].get("file_id") == FILE_ID and "download_url" in sent[0]["payload"])

fake_sio.sent.clear()
backend._last_page_analysis_push.clear()
_set_online([])
ok = backend.request_page_analysis(FILE_ID, "报告.docx", prefer_client="A")
check("全离线时不推送（等设备上线）", ok is False and not fake_sio.sent)

# ==================== ③ 打印任务不被非目标设备抢走 ====================
print("\n=== ③ 打印任务严格按订单目标设备分发 ===")
pushed = []
# push_print_task_to_client(sub_task_id, file_id, file_name, copies, duplex, page_range, client_id, ...)
backend.push_print_task_to_client = lambda *a, **kw: (
    pushed.append(a[6] if len(a) > 6 else kw.get("client_id")), True)[1]

_set_online(["B"])          # 目标 A 离线、非目标 B 在线
pushed.clear()
backend.process_pending_orders()
check("目标设备 A 离线：不推给在线设备 B（保持排队）", pushed == [], f"pushed={pushed}")

_set_online(["A", "B"])     # 目标 A 上线
pushed.clear()
backend.process_pending_orders()
check("目标设备 A 上线：只推给 A", pushed == ["A"], f"pushed={pushed}")

# ==================== ④ 设备上线补推：自己优先分析自己的文件 ====================
print("\n=== ④ on_connect 补推（谁上线谁分析）===")
conn = backend.get_db()
conn.execute("UPDATE files SET page_count = 0, page_count_verified = 0 WHERE id = ?", (FILE_ID,))
conn.commit()
conn.close()
_set_online(["A", "B"])
fake_sio.sent.clear()
backend._last_page_analysis_push.clear()
cid, why = backend._resolve_analysis_client("A", FILE_ID)
check("A 上线时按 prefer=A 解析：A 在线且接单 → A 自己分析（on_connect 传的就是刚上线设备）",
      cid == "A" and why == "调用方指定", f"{cid} / {why}")

print("\n" + "=" * 52)
print("页数分析/打印归属验收:", "全部通过 ✅" if ok_all else "存在失败 ❌")
shutil.rmtree(tmp, ignore_errors=True)
sys.exit(0 if ok_all else 1)
