# -*- coding: utf-8 -*-
"""验收 · 🟡-D5 第 3 条：后台离线同步不会「焊死」启动时的地址/token

缺陷（审查报告 D5）：`OfflineSync.start_background_sync(server_url, token)` 把这两个值
**焊进闭包**，且线程已在运行就 `return`（"已在运行，忽略重复调用"），而
`stop_background_sync()` 全仓**没有任何调用点**。后果：用户在「云端连接设置」里改完
地址/token 后，**本次运行内**后台定时同步仍往旧地址上报 —— 旧云端会收到不属于它的
本地订单；反过来云端关掉后残留 token 还会继续上传。

修复后（本脚本钉住的语义）：
  A. 目标存在 `_sync_target`（单次元组赋值，CPython 原子 ⇒ 不会出现「新地址+旧 token」撕裂），
     循环**每轮**重新读取；线程已在运行时再调 start 只更新目标，**不**被忽略。
  B. 目标被清空（云端关闭）→ 不发请求（也不白耗 offline_orders.retry_count）。
  C. stop 真的停得住、之后还能重新启动。
  D. 静态接线：`_on_cloud_settings`（配置保存路径）必须同步 start/stop 后台同步，
     否则将来又会出现"改了配置但线程还打旧地址"。

用法: python tests/verify_offline_sync_target.py     （exit 0 = 全绿）
"""
import io
import os
import sys
import tempfile
import threading
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL_TOOL = os.path.join(REPO, "local_print_tool")

_SANDBOX = tempfile.mkdtemp(prefix="hn_offline_sync_")
os.environ["APPDATA"] = _SANDBOX
sys.path.insert(0, LOCAL_TOOL)
os.chdir(LOCAL_TOOL)

import offline_sync as osync          # noqa: E402

ok_all = True


def check(name, cond, detail=""):
    global ok_all
    if not cond:
        ok_all = False
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


def _blank_comment_lines(text):
    """`#` 之后置等长空格（保字符偏移）—— 判据不许吃注释（D3/D4/C4b 的老教训）。"""
    out = []
    for line in text.split("\n"):
        i = line.find("#")
        out.append(line if i < 0 else line[:i] + " " * (len(line) - i))
    return "\n".join(out)


sync = osync.OfflineSync()
calls = []
_calls_lock = threading.Lock()
INTERVAL = 0.05


def _fake_sync(url, tok):
    with _calls_lock:
        calls.append((url, tok))
    return 0


sync.sync_all_pending_orders = _fake_sync

# ==================== A. 目标每轮动态读取 ====================
print("=== A. 换目标后本次运行内就换地址（改前焊死在闭包里）===")
sync.start_background_sync("https://old.example", "tok-old", interval=INTERVAL)
time.sleep(0.35)
with _calls_lock:
    first = list(calls)
check("线程启动后按旧目标上报", len(first) >= 1
      and all(c == ("https://old.example", "tok-old") for c in first), str(first[:3]))
_thread_before = sync._sync_thread

# 模拟「用户在云端连接设置里改了地址/token」→ 保存路径会再调一次 start
sync.start_background_sync("https://new.example", "tok-new", interval=INTERVAL)
check("线程已在运行时不做「直接 return」：目标已被更新",
      getattr(sync, "_sync_target", None) == ("https://new.example", "tok-new"),
      repr(getattr(sync, "_sync_target", "<无该属性（改前）>")))
with _calls_lock:
    _before_new = len(calls)
time.sleep(0.35)
with _calls_lock:
    tail = calls[_before_new:]
check("★ 换目标后的**新一轮**上报用的是新地址（改前永远还是旧地址）",
      bool(tail) and all(c == ("https://new.example", "tok-new") for c in tail), str(tail[:3]))
check("没有起第二只线程（仍是同一只）", sync._sync_thread is _thread_before)

# ==================== B. 目标清空 → 不发请求 ====================
print("=== B. 目标清空（云端关闭）→ 不发请求，不白耗 retry_count ===")
sync.start_background_sync("", "", interval=INTERVAL)
with _calls_lock:
    _n = len(calls)
time.sleep(0.3)
with _calls_lock:
    _grew = len(calls) - _n
check("目标为空时不再发任何请求", _grew == 0, f"新增 {_grew} 次调用")

# ==================== C. stop 真的停 + 之后还能重启 ====================
print("=== C. stop 停得住、之后能重启 ===")
sync.start_background_sync("https://x.example", "tok-x", interval=INTERVAL)
time.sleep(0.25)
sync.stop_background_sync()
with _calls_lock:
    _m = len(calls)
time.sleep(0.4)          # stop 已 join 线程 ⇒ 计数不可能再涨（判据无竞态）
with _calls_lock:
    _after = len(calls)
check("stop 后线程已退出、调用数不再增长", _after == _m, f"{_m} → {_after}")

sync.start_background_sync("https://again.example", "tok-a", interval=INTERVAL)
time.sleep(0.3)
with _calls_lock:
    _restart_ok = any(c == ("https://again.example", "tok-a") for c in calls)
check("stop 之后能重新启动并用新目标上报", _restart_ok)
sync.stop_background_sync()

# ==================== D. 静态接线 ====================
print("=== D. 静态接线：配置保存路径同步后台同步目标 ===")
gui_src = open(os.path.join(LOCAL_TOOL, "gui.py"), encoding="utf-8").read()
_osync_src = open(os.path.join(LOCAL_TOOL, "offline_sync.py"), encoding="utf-8").read()

if "def _on_cloud_settings(self):" in gui_src and "def _restart_for_cloud_switch" in gui_src:
    _body_raw = gui_src.split("def _on_cloud_settings(self):", 1)[1] \
        .split("def _restart_for_cloud_switch", 1)[0]
    _body = _blank_comment_lines(_body_raw)
else:
    _body = ""
check("取到 _on_cloud_settings 函数体（判据前提）", bool(_body))
check("★ _on_cloud_settings 内调用了 start_background_sync（改完配置就换目标）",
      "start_background_sync(" in _body)
check("★ _on_cloud_settings 内调用了 stop_background_sync（云端关闭时停线程）",
      "stop_background_sync()" in _body)
check("传给 start 的是**当前**配置（cloud_api_url / cloud_token）",
      "self._config.cloud_api_url" in _body and "self._config.cloud_token" in _body)

_osync_code = _blank_comment_lines(_osync_src)
check("offline_sync 循环里每轮读 _sync_target（不在闭包里持有旧值）",
      "url, tok = self._sync_target" in _osync_code)
check("offline_sync 不再把形参直接焊进闭包（self.sync_all_pending_orders(server_url, token) 已消失）",
      "self.sync_all_pending_orders(server_url, token)" not in _osync_code)

# 判据自证：把 start/stop 调用从函数体里去掉，两条静态判据必须变红
_mut = _body.replace("self._offline_sync.start_background_sync(", "ZZ_NOT_CALLED(") \
             .replace("self._offline_sync.stop_background_sync()", "ZZ_NOT_CALLED()")
check("D 对拍：抽掉 start/stop 调用后两条判据必红",
      "start_background_sync(" not in _mut and "stop_background_sync()" not in _mut)

print("\n" + "=" * 52)
print("离线同步目标验收:", "全部通过 ✅" if ok_all else "存在失败 ❌")
shutil_rm = __import__("shutil")
shutil_rm.rmtree(_SANDBOX, ignore_errors=True)
sys.exit(0 if ok_all else 1)
