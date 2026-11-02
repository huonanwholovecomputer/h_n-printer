# -*- coding: utf-8 -*-
"""验收 · 「拉取前后端日志」为什么永远 0 字节（2026-11-02 现场）

现场：本地工具「日志管理 → 拉取前后端日志」回 `✓ 拉取完成: server: 0 字节 | frontend: 0 字节`，
看着像网络/权限问题。核查服务器 `/home/printer-backend/logs/`：

    -rw-r--r-- 1 197108 197121     0 Aug  3 19:52 frontend.log
    -rw-r--r-- 1 197108 197121     0 Aug  3 19:52 server.log

两个文件都是 0 字节，不是拉取失败：
  · `server.log` 只由 `logging.getLogger("printer_server")` 的 FileHandler 写，**全仓没有一处
    往这个 logger 打日志**；gunicorn 的 access/error 日志走 stdout/stderr → journald。
  · `frontend.log` 只由 `/api/log/report` 写，而**没有任何前端在调这个接口**。

修法（后端）：`/api/log/fetch?type=server` 在文件为空时回落 `journalctl -u printer-backend`，
返回 `source="journald"`；单次最多 200KB；token 脱敏。客户端把 "0 字节" 改显示成「无记录」，
journald 来源标注出来。`frontend.log` 不回落（它确实是没人上报，不能编内容）。

    PYTHONUTF8=1 python tests/verify_log_fetch_journald.py    # exit 0 = 全绿
"""
import io
import os
import shutil
import sys
import tempfile
import types

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(REPO, "mobile_apps", "printer-backend")

_tmp = tempfile.mkdtemp(prefix="hn_logfetch_")
_dst = os.path.join(_tmp, "printer-backend")
shutil.copytree(SRC, _dst, ignore=shutil.ignore_patterns(
    "__pycache__", "orders.db", "users.db", "uploads", "logs"))
sys.path.insert(0, _dst)
os.chdir(_dst)

ok_all = True


def check(name, cond, detail=""):
    global ok_all
    if not cond:
        ok_all = False
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


try:
    import app as backend
except Exception as e:
    print(f"✗ 无法导入 app.py: {e}")
    sys.exit(1)

TOKEN = "log-fetch-token"
backend.PRINTER_TOKEN = TOKEN
backend.init_db()
client = backend.app.test_client()
H = {"X-Printer-Token": TOKEN}


def fetch(log_type, **params):
    p = {"type": log_type}
    p.update(params)
    return client.get("/api/log/fetch", headers=H, query_string=p)


# ── journalctl 替身：记录调用参数，返回预设文本 ──
class _Proc:
    def __init__(self, out, rc=0):
        self.stdout = out
        self.stderr = ""
        self.returncode = rc


_calls = []
_journal_out = ""


def _fake_run(cmd, **kw):
    _calls.append(list(cmd))
    if isinstance(_journal_out, Exception):
        raise _journal_out
    return _Proc(_journal_out)


backend.subprocess = types.SimpleNamespace(run=_fake_run)
# 服务单元的 PATH 只有 venv/bin（没有 /usr/bin）⇒ 代码必须自己解析 journalctl 绝对路径。
# 测试里替身 which 返回 /usr/bin/journalctl，验证调用参数用的是绝对路径。
backend.shutil = types.SimpleNamespace(which=lambda name: "/usr/bin/journalctl")

LOG_DIR = backend.LOG_DIR
SERVER_LOG = os.path.join(LOG_DIR, "server.log")
FRONTEND_LOG = os.path.join(LOG_DIR, "frontend.log")
os.makedirs(LOG_DIR, exist_ok=True)

print("=== 1. server.log 为空 ⇒ 回落 journald（现场就是这个状态）===")
for p in (SERVER_LOG, FRONTEND_LOG):
    open(p, "w", encoding="utf-8").close()      # 0 字节，与线上一致
_journal_out = ("2026-11-02T13:24:59+0800 VM-0-2-ubuntu gunicorn[2211]: "
                "[LINK] 已连接 DESKTOP-EJGEB1V-a6c1a1365e\n"
                "2026-11-02T13:25:01+0800 VM-0-2-ubuntu gunicorn[2211]: "
                "127.0.0.1 GET /api/log/fetch?token=SECRET123 200\n")
_calls.clear()
r = fetch("server")
d = r.get_json() or {}
check("HTTP 200", r.status_code == 200, str(r.status_code))
check("★ 文件为 0 字节时不再回「0 字节」", (d.get("size") or 0) > 0, str(d.get("size")))
check("★ 标明来源 journald", d.get("source") == "journald", str(d.get("source")))
check("内容确实是 journal 的两行",
      d.get("content", "").count("\n") == 2 and "[LINK] 已连接" in d.get("content", ""),
      repr(d.get("content", ""))[:80])
check("journalctl 只调用一次、走绝对路径、带 -u printer-backend",
      len(_calls) == 1 and _calls[0][:3] == ["/usr/bin/journalctl", "-u", "printer-backend"],
      str(_calls))
check("★ 日志里的 token 已脱敏", "SECRET123" not in d.get("content", "")
      and "token=***" in d.get("content", ""), repr(d.get("content", ""))[-60:])
check("size 与 content 字节数一致",
      d.get("size") == len(d.get("content", "").encode("utf-8")),
      f"{d.get('size')} vs {len(d.get('content', '').encode('utf-8'))}")

print("=== 2. server.log 有内容 ⇒ 优先读文件，不碰 journald ===")
with open(SERVER_LOG, "w", encoding="utf-8") as f:
    f.write("".join(f"file-line-{i}\n" for i in range(1, 11)))
_calls.clear()
_journal_out = "journal-line\n"
d = (fetch("server") or {}).get_json() or {}
check("来源标为 file", d.get("source") == "file", str(d.get("source")))
check("读到文件内容", "file-line-10" in d.get("content", ""), repr(d.get("content", ""))[:60])
check("没有再调 journalctl", _calls == [], str(_calls))

print("=== 3. tail=N 只回末尾 N 行（P2-14 语义不变）===")
d = (fetch("server", tail=3) or {}).get_json() or {}
_lines = [ln for ln in d.get("content", "").splitlines() if ln]
check("tail=3 ⇒ 3 行", len(_lines) == 3, str(_lines))
check("tail=3 ⇒ 取的是末尾（file-line-8..10）",
      _lines == ["file-line-8", "file-line-9", "file-line-10"], str(_lines))

print("=== 4. tail=0 只取大小、不回内容，也不回落 journald ===")
open(SERVER_LOG, "w", encoding="utf-8").close()
_calls.clear()
_journal_out = "should-not-be-used\n"
d = (fetch("server", tail=0) or {}).get_json() or {}
check("内容为空", (d.get("content") or "") == "", repr(d.get("content")))
check("没有回落 journald", _calls == [], str(_calls))

print("=== 5. journalctl 不存在 / 报错 ⇒ 老实回 0，不 500 ===")
_calls.clear()
_journal_out = FileNotFoundError("journalctl")
r = fetch("server")
d = r.get_json() or {}
check("HTTP 200（不抛 500）", r.status_code == 200, str(r.status_code))
check("回 0 字节且来源 file", d.get("size") == 0 and d.get("source") == "file",
      f"{d.get('size')}/{d.get('source')}")
_journal_out = ""
backend.subprocess = types.SimpleNamespace(
    run=lambda cmd, **kw: _Proc("", rc=1))
d = (fetch("server") or {}).get_json() or {}
check("journalctl 非零退出也回 0（不把错误当内容）",
      d.get("size") == 0 and (d.get("content") or "") == "",
      f"{d.get('size')}/{repr(d.get('content'))}")
backend.subprocess = types.SimpleNamespace(run=_fake_run)
# 服务单元的 PATH 只有 venv/bin（没有 /usr/bin）⇒ 代码必须自己解析 journalctl 绝对路径。
# 测试里替身 which 返回 /usr/bin/journalctl，验证调用参数用的是绝对路径。
backend.shutil = types.SimpleNamespace(which=lambda name: "/usr/bin/journalctl")

print("=== 5b. 机器上没有 journalctl（PATH 也没有）⇒ 回 0，不崩 ===")
backend.shutil = types.SimpleNamespace(which=lambda name: None)
_calls.clear()
d = (fetch("server") or {}).get_json() or {}
check("找不到 journalctl ⇒ 0 字节 / source=file",
      d.get("size") == 0 and d.get("source") == "file",
      f"{d.get('size')}/{d.get('source')}")
backend.shutil = types.SimpleNamespace(which=lambda name: "/usr/bin/journalctl")

print("=== 6. 超过 200KB 截断（别把内存/响应撑爆）===")
_journal_out = "".join(f"line-{i:05d}-" + "x" * 90 + "\n" for i in range(6000))
_calls.clear()
r = fetch("server", tail=6000)
d = r.get_json() or {}
check("HTTP 200", r.status_code == 200, str(r.status_code))
check("★ 响应体不超过 200KB", d.get("size", 0) <= 200 * 1024, str(d.get("size")))
check("截断后仍是完整行尾（不出现乱码/半个汉字）",
      d.get("content", "").rstrip("\n").endswith("x"), repr(d.get("content", ""))[-20:])

print("=== 7. frontend 不回落 journald（它确实是没人上报）===")
open(FRONTEND_LOG, "w", encoding="utf-8").close()
_calls.clear()
_journal_out = "journal-line\n"
d = (fetch("frontend") or {}).get_json() or {}
check("frontend 仍回 0 字节", d.get("size") == 0, str(d.get("size")))
check("frontend 未调用 journalctl", _calls == [], str(_calls))
with open(FRONTEND_LOG, "w", encoding="utf-8") as f:
    f.write("frontend-error-1\n")
d = (fetch("frontend") or {}).get_json() or {}
check("frontend 有内容时正常返回", "frontend-error-1" in d.get("content", "")
      and d.get("source") == "file", str(d.get("source")))

print("=== 8. 鉴权与参数校验没被削弱 ===")
r = client.get("/api/log/fetch", query_string={"type": "server"})
check("无 token ⇒ 403", r.status_code == 403, str(r.status_code))
r = client.get("/api/log/fetch", headers={"X-Printer-Token": "wrong-token"},
               query_string={"type": "server"})
check("错 token ⇒ 403", r.status_code == 403, str(r.status_code))
r = fetch("orders")
check("非法 type ⇒ 400", r.status_code == 400, str(r.status_code))

print("=== 9. 收尾 ===")
shutil.rmtree(_tmp, ignore_errors=True)
print("\n" + ("全部通过 ✅" if ok_all else "存在失败项 ❌"))
sys.exit(0 if ok_all else 1)
