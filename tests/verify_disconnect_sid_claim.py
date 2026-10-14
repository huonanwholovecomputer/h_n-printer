# -*- coding: utf-8 -*-
"""审计修复验收 · 🔴-2 on_disconnect 按 sid 认领

事故形状：同一 client_id 快速重连（sid A → B）后，旧连接 A 的 disconnect 仍会
按 operator_client（= client_id，无法区分 A/B）回滚该 client_id 名下所有
printing 任务，并把 B 在 printer_clients 中的注册记录一并删掉。
后果：B 正在打印的页在服务端被翻回 queued，30 秒后照常推送 ⇒ 重复打印；
且 B 从此不在注册表里（收单/保活判定失效）。

本脚本用 Flask-SocketIO 测试客户端在同一进程内构造真实连接与断开，
在临时目录副本上跑（不污染真实 orders.db / devices.json）。
"""
import os
import shutil
import sys
import tempfile

# 控制台编码兜底：断言名含中文/符号，GBK 控制台下会抛 UnicodeEncodeError
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(REPO, "mobile_apps", "printer-backend")

tmp = tempfile.mkdtemp(prefix="hn_verify_sid_")
dst = os.path.join(tmp, "printer-backend")
shutil.copytree(SRC, dst, ignore=shutil.ignore_patterns(
    "__pycache__", "orders.db", "users.db", "uploads", "logs"))
sys.path.insert(0, dst)
os.chdir(dst)

ok_all = True


def check(name, cond, detail=""):
    global ok_all
    if not cond:
        ok_all = False
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


try:
    import app as backend
    from flask_socketio import SocketIOTestClient
except Exception as exc:  # pragma: no cover
    import traceback
    traceback.print_exc()
    sys.exit(f"无法导入后端 app.py: {exc!r}")

backend.PRINTER_TOKEN = "testtok"
backend.init_db()

TOKEN = "testtok"


def connect(client_id=None):
    """建立一个打印机客户端连接，返回测试客户端对象。"""
    qs = f"token={TOKEN}"
    if client_id:
        qs += f"&client_id={client_id}"
    return SocketIOTestClient(backend.app, backend.socketio, query_string=qs)


def last_sid(client_id):
    return backend.printer_clients.get(client_id, {}).get("sid", "")


def reset_registry():
    with backend.printer_clients_lock:
        backend.printer_clients.clear()


def seed_order(order_id, spec, operator):
    """spec: [(file_id, status)]，operator_client 统一为 operator（= 领取任务的 client_id）。"""
    conn = backend.get_db_conn()
    conn.execute("INSERT INTO orders (id, file, copies, status, created_at)"
                 " VALUES (?, 'x.pdf', 1, 'printing', datetime('now'))", (order_id,))
    for fid, status in spec:
        conn.execute(
            "INSERT INTO order_files (id, order_id, file_name, copies, page_count,"
            " status, operator_client, locked_at, created_at)"
            " VALUES (?, ?, 'x.pdf', 1, 3, ?, ?, '2026-01-01 00:00:00', datetime('now'))",
            (fid, order_id, status, operator))
    conn.commit()
    conn.close()


def task_state(fid):
    conn = backend.get_db_conn()
    row = conn.execute("SELECT status, operator_client, locked_at FROM order_files WHERE id = ?",
                       (fid,)).fetchone()
    conn.close()
    return dict(row) if row else None


print("=== 前置：测试客户端能真实触发 disconnect handler ===")
reset_registry()
c0 = connect("probe")
sid0 = last_sid("probe")
check("连接后注册表记录 sid", bool(sid0), f"sid={sid0!r}")
check("测试客户端 sid 与注册表一致（request.sid 在 handler 内可用）",
      backend.printer_clients.get("probe", {}).get("sid") == sid0)
c0.disconnect()

# ================= 场景 A：单连接断开，三类任务正常回滚 =================
print("\n=== 场景 A：单连接断开（无替换）应正常回滚 ===")
reset_registry()
seed_order(9101, [(91011, "printing"), (91012, "downloading"), (91013, "accepted")], "solo")
ca = connect("solo")
sid_a = last_sid("solo")
check("A 已注册", sid_a != "", f"sid={sid_a!r}")
ca.disconnect()

st = task_state(91011)
check("printing 子任务回滚为 queued 且清空 operator_client",
      st["status"] == "queued" and st["operator_client"] == "", f"{st}")
check("回滚时清空 locked_at（防孤儿回收误判）", st["locked_at"] == "", f"locked_at={st['locked_at']!r}")
st = task_state(91012)
check("downloading 子任务回退 scheduled", st["status"] == "scheduled", f"{st}")
st = task_state(91013)
check("accepted 子任务标记 offline_unknown", st["status"] == "offline_unknown", f"{st}")
check("连接断开后注册表已注销", "solo" not in backend.printer_clients,
      f"registry={list(backend.printer_clients)}")

# ================= 场景 B：A→B 替换后 A 断开，不得影响 B =================
print("\n=== 场景 B：A→B 替换后旧连接 A 断开（核心回归）===")
reset_registry()
seed_order(9201, [(92011, "printing"), (92012, "downloading"), (92013, "accepted")], "demo")
a = connect("demo")
sid_a = last_sid("demo")
b = connect("demo")
sid_b = last_sid("demo")
check("同 client_id 重连后注册表指向新连接 B", sid_a != sid_b and sid_b != "",
      f"A={sid_a!r} B={sid_b!r}")

a.disconnect()
check("旧连接 A 断开后，注册表仍指向 B（未被误删）", last_sid("demo") == sid_b,
      f"registry={ {k: v.get('sid') for k, v in backend.printer_clients.items()} }")
st = task_state(92011)
check("B 名下 printing 任务未被回滚（否则 30s 后重复打印）",
      st["status"] == "printing" and st["operator_client"] == "demo", f"{st}")
st = task_state(92012)
check("B 名下 downloading 任务未被回退", st["status"] == "downloading", f"{st}")
st = task_state(92013)
check("B 名下 accepted 任务未被标记 offline_unknown", st["status"] == "accepted", f"{st}")

# ================= 场景 C：B 断开时正常回滚 + 注销 =================
print("\n=== 场景 C：当前连接 B 断开应正常回滚 ===")
b.disconnect()
check("B 断开后注册表已注销", "demo" not in backend.printer_clients,
      f"registry={list(backend.printer_clients)}")
st = task_state(92011)
check("B 断开后 printing 任务正常回滚为 queued", st["status"] == "queued", f"{st}")
st = task_state(92012)
check("B 断开后 downloading 任务回退 scheduled", st["status"] == "scheduled", f"{st}")
st = task_state(92013)
check("B 断开后 accepted 任务标记 offline_unknown", st["status"] == "offline_unknown", f"{st}")

# ================= 场景 D：不带 client_id 的兜底路径 =================
print("\n=== 场景 D：query 不带 client_id（sid 即 client_id）===")
reset_registry()
c = SocketIOTestClient(backend.app, backend.socketio, query_string=f"token={TOKEN}")
keys = list(backend.printer_clients)
check("不带 client_id 时以 sid 为键注册", len(keys) == 1, f"registry={keys}")
sid_d = keys[0] if keys else ""
seed_order(9301, [(93011, "printing")], "")
conn = backend.get_db_conn()
conn.execute("UPDATE order_files SET operator_client = ? WHERE id = 93011", (sid_d,))
conn.commit()
conn.close()
c.disconnect()
st = task_state(93011)
check("兜底路径：断开后正常回滚该 sid 名下任务", st["status"] == "queued", f"{st}")
check("兜底路径：断开后注册表已清理", sid_d not in backend.printer_clients,
      f"registry={list(backend.printer_clients)}")

print("\n" + ("全部通过" if ok_all else "存在失败项"))
shutil.rmtree(tmp, ignore_errors=True)
sys.exit(0 if ok_all else 1)
