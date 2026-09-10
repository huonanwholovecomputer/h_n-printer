# -*- coding: utf-8 -*-
"""审计修复验收 · 本地打印工具侧（🔴2 / 🔴3 / 🔴4 / 🔴5 / 🔴7 / 🔴8）

尽量用「真实类/真实函数 + 桩依赖」跑行为，而不是只做字符串匹配。
"""
import io
import os
import shutil
import sys
import tempfile
import threading
import traceback
from datetime import datetime, timedelta

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LOCAL_TOOL = os.path.join(REPO, "local_print_tool")
sys.path.insert(0, LOCAL_TOOL)
os.chdir(LOCAL_TOOL)

ok_all = True


def check(name, cond, detail=""):
    global ok_all
    if not cond:
        ok_all = False
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


def src_of(p):
    return open(p, encoding="utf-8").read()


# ============================================================
# 🔴2 SumatraPDF -print-settings
# ============================================================
print("=== 🔴2 SumatraPDF 降级参数（官方 token）===")
from pdf_printer import build_sumatra_print_settings, _sumatra_page_tokens, _print_via_sumatra
import inspect

pdf_src = src_of(os.path.join(LOCAL_TOOL, "pdf_printer.py"))

cases = [
    (dict(copies=3, duplex="on", duplex_mode="long-edge", page_range="1-5", total_pages=10), "1-5,3x,duplexlong"),
    (dict(copies=1, duplex="on", duplex_mode="short-edge", page_range="", total_pages=10), "1x,duplexshort"),
    (dict(copies=2, duplex="off", duplex_mode="long-edge", page_range="", total_pages=10), "2x,simplex"),
    (dict(copies=1, duplex="off", duplex_mode="long-edge", page_range="1、3、5-7", total_pages=10), "1,3,5-7,1x,simplex"),
    (dict(copies=4, duplex="on", duplex_mode="long-edge", page_range="2-3,9", total_pages=12), "2-3,9,4x,duplexlong"),
]
for kw, expect in cases:
    got = build_sumatra_print_settings(**kw)
    check(f"settings({kw['copies']}份/{kw['duplex']}/{kw['page_range'] or '全部'}) == {expect}",
          got == expect, f"got={got!r}")

settings_probe = build_sumatra_print_settings(copies=3, duplex="on", page_range="1-5", total_pages=10)
check("输出不含非法 `copies=` 键", "copies=" not in settings_probe)
check("输出不含非法 `duplex=` 键", "duplex=" not in settings_probe)
check("输出不含非法 `range=` 键", "range=" not in settings_probe)
check("双面用官方 `duplexlong`/`duplexshort`",
      "duplexlong" in settings_probe and
      "duplexshort" in build_sumatra_print_settings(duplex="on", duplex_mode="short-edge"))
check("页码是裸 token（不再是 range=1-5）", settings_probe.split(",")[0] == "1-5")

# 页码范围必须由 _parse_page_range 压缩生成（保证一定合法）
check("页码 token 压缩正确：0-based [0,1,2,4] → '1-3,5'", _sumatra_page_tokens([0, 1, 2, 4]) == "1-3,5")
check("页码 token 压缩正确：单页 [2] → '3'", _sumatra_page_tokens([2]) == "3")
check("页码范围非法/超限时退回全打印（不产生非法 token）",
      build_sumatra_print_settings(page_range="abc", total_pages=5).startswith("1-5,"),
      build_sumatra_print_settings(page_range="abc", total_pages=5))

sumatra_body = inspect.getsource(_print_via_sumatra)
check("_print_via_sumatra 内不再调用 _get_printer_devmode（系统级 DEVMODE 污染已移除）",
      "_get_printer_devmode" not in sumatra_body)
check("_get_printer_devmode 仍被 GDI 主路径使用（未变成死代码）",
      pdf_src.count("_get_printer_devmode(") >= 2)
check("命令带 -silent（防 SumatraPDF 错误弹窗阻塞子进程）", '"-silent"' in sumatra_body or "'-silent'" in sumatra_body)
check("命令仍带 -print-to / -print-settings 及 PDF 路径",
      '"-print-to"' in sumatra_body and '"-print-settings"' in sumatra_body)


# ============================================================
# 🔴3 _save_config 后台线程不读 UI 控件
# ============================================================
print("\n=== 🔴3 _save_config 线程安全 ===")
from PySide6.QtCore import QThread
from gui import MainWindow

gui_src = src_of("gui.py")
check("源码含主线程判定 _is_gui_thread", "def _is_gui_thread" in gui_src)
check("_save_config 内含 sync_ui 自动判定分支",
      "sync_ui = self._is_gui_thread()" in gui_src)
check("落盘被 _config_save_lock 串行化", "with self._config_save_lock:" in gui_src)
check("__init__ 初始化了 _config_save_lock", "self._config_save_lock = threading.Lock()" in gui_src)


class _StubConfig:
    def __init__(self):
        self.saved = 0

    def save(self, path):
        self.saved += 1


class _StubWindow:
    """只保留 _save_config 依赖的最小面。"""
    _is_gui_thread = MainWindow._is_gui_thread
    _save_config = MainWindow._save_config

    def __init__(self, same_thread: bool):
        self._config = _StubConfig()
        self._config_path = os.path.join(tempfile.gettempdir(), "hn_verify_never_written.json")
        self._config_save_lock = threading.Lock()
        self.sync_ui_calls = 0
        self._fake_thread = QThread.currentThread() if same_thread else object()

    def _sync_ui_to_config(self):
        # 真实实现会读 QComboBox 等控件；这里只记账
        self.sync_ui_calls += 1

    def thread(self):
        return self._fake_thread


main_like = _StubWindow(same_thread=True)
main_like._save_config()
check("主线程调用 → 会同步 UI（保持旧行为）", main_like.sync_ui_calls == 1 and main_like._config.saved == 1,
      f"sync={main_like.sync_ui_calls} saved={main_like._config.saved}")

bg_like = _StubWindow(same_thread=False)
bg_like._save_config()
check("后台线程调用 → 不读 UI 控件，但仍落盘",
      bg_like.sync_ui_calls == 0 and bg_like._config.saved == 1,
      f"sync={bg_like.sync_ui_calls} saved={bg_like._config.saved}")

bg_explicit = _StubWindow(same_thread=True)
bg_explicit._save_config(sync_ui=False)
check("显式 sync_ui=False → 即使主线程也不读 UI",
      bg_explicit.sync_ui_calls == 0 and bg_explicit._config.saved == 1)

# 真实并发：多线程同时落盘，验证不抛异常且计数正确
conc = _StubWindow(same_thread=False)
threads = [threading.Thread(target=conc._save_config) for _ in range(32)]
[t.start() for t in threads]
[t.join() for t in threads]
check("32 线程并发落盘无异常且全部写出", conc._config.saved == 32 and conc.sync_ui_calls == 0,
      f"saved={conc._config.saved} sync={conc.sync_ui_calls}")

# 后台调用点是否都显式标注（可读性）
for n in ("self._save_config(sync_ui=not from_thread)",
          "self._save_config(sync_ui=False)   # 后台线程（owner-name-sync）→ 不读 UI 控件"):
    check(f"后台调用点已显式标注: {n[:46]}…", n in gui_src)


# ============================================================
# 🔴4 预约状态机两条旁路
# ============================================================
print("\n=== 🔴4 预约状态机 ===")


class _StubCloud:
    def __init__(self):
        self.failed = []

    def report_fail(self, task_id, reason):
        self.failed.append((task_id, reason))


class _StubTask:
    def __init__(self, tid):
        self.task_id = tid


class _StubScheduledWin:
    """只保留 _fire_scheduled_print 依赖的最小面。"""
    _fire_scheduled_print = MainWindow._fire_scheduled_print
    _report_scheduled_failure = MainWindow._report_scheduled_failure

    def __init__(self):
        self._scheduled_orders = {}
        self._worker = None            # 打印机空闲 → 直接走到「标签页无文件」分支
        self._cloud_client = _StubCloud()
        self.logs = []
        self.timers = []
        self.cleaned = []
        self.order_ids = [11, 22]

    def _log(self, msg):
        self.logs.append(msg)

    def _tab_jobs(self, tab_key):
        return []                      # 标签页里没有文件（被删/被手动打完）

    def _start_scheduled_print_timer(self, order_id, target_ts):
        self.timers.append((order_id, target_ts))

    def _cleanup_scheduled_order(self, order_id, reason=""):
        self.cleaned.append((order_id, reason))
        self._scheduled_orders.pop(order_id, None)

    def _start_print_worker(self, jobs, tab_key=""):
        return True


def mk_state(order_id):
    return {
        "target_ts": 0, "ready": {1: _StubTask(101)}, "pending": {},
        "timer": None, "print_timer": None, "frozen": False, "delayed_sent": False,
        "printed": False, "printed_ts": 0, "tab_key": "1", "retry_count": 0,
    }


w = _StubScheduledWin()
w._scheduled_orders[11] = mk_state(11)

# 连打 5 次：重试计数必须单调递增，绝不能被重置为 0
for _ in range(5):
    w._fire_scheduled_print(11)
retries = [w._scheduled_orders[11]["retry_count"]]
check("空标签页分支不再把 retry_count 重置为 0",
      w._scheduled_orders[11]["retry_count"] == 5, f"retry_count={w._scheduled_orders[11]['retry_count']}")
check("空标签页分支仍排 10s 后重试", len(w.timers) == 5, f"timers={len(w.timers)}")

# 跑到超限 → 必须上报失败并结束状态机（旧实现永远到不了这里）
w2 = _StubScheduledWin()
w2._scheduled_orders[22] = mk_state(22)
for _ in range(61):
    w2._fire_scheduled_print(22)
check("超过 60 次后结束状态机（不再无限空转）", 22 not in w2._scheduled_orders,
      f"remaining={list(w2._scheduled_orders)}")
check("超限时上报后端失败（不静默丢弃）",
      len(w2._cloud_client.failed) == 1 and w2._cloud_client.failed[0][0] == 101,
      str(w2._cloud_client.failed))
check("超限时清理原因可读", w2.cleaned and "重试超限" in w2.cleaned[-1][1], str(w2.cleaned))

# _auto_print_retry 重映射 + order_id 反查
class _StubTabWin:
    _remap_auto_print_retry = MainWindow._remap_auto_print_retry
    _drop_auto_print_retry_for_tab = MainWindow._drop_auto_print_retry_for_tab
    _find_tab_key_by_order_id = MainWindow._find_tab_key_by_order_id

    def __init__(self):
        self._auto_print_retry = [
            {"order_id": 1, "tab_key": "1", "task_ids": []},
            {"order_id": 2, "tab_key": "3", "task_ids": []},
            {"order_id": 3, "tab_key": "9", "task_ids": []},   # 9 已不存在
        ]
        self.logs = []

        class J:
            def __init__(self, oid):
                self.order_id = oid

        class T:
            def __init__(self, jobs):
                self.jobs = jobs

        self._config = type("C", (), {"tabs": {"1": T([J(1)]), "2": T([J(2)])}})()

    def _log(self, m):
        self.logs.append(m)


tw = _StubTabWin()
tw._remap_auto_print_retry({"1": "1", "2": "2", "3": "2"})
check("重映射后 tab_key 跟随新编号", [i["tab_key"] for i in tw._auto_print_retry] == ["1", "2", ""],
      str([i["tab_key"] for i in tw._auto_print_retry]))
check("映射不到的旧 key 被置空（绝不保留旧 key 去撞别的标签页）",
      tw._auto_print_retry[2]["tab_key"] == "")
check("按 order_id 反查标签页可用", tw._find_tab_key_by_order_id(2) == "2" and tw._find_tab_key_by_order_id(1) == "1")
check("order_id 不存在时返回空串", tw._find_tab_key_by_order_id(999) == "")
tw._drop_auto_print_retry_for_tab("2")
check("标签页删除时丢弃指向它的补打队列项",
      [i["order_id"] for i in tw._auto_print_retry] == [1, 3], str(tw._auto_print_retry))
check("源码 _renumber_tabs 内确实调用了重映射",
      "_remap_auto_print_retry(key_map)" in gui_src)
check("源码 _on_all_finished 用 _find_tab_key_by_order_id 兜底定位",
      "_find_tab_key_by_order_id(order_id)" in gui_src)
check("源码空标签页分支不再出现 `st[\"retry_count\"] = 0`", 'st["retry_count"] = 0' not in gui_src)


# ============================================================
# 🔴5 updater 供应链
# ============================================================
print("\n=== 🔴5 updater HTTPS / 版本比较 ===")
import updater
from updater import (compare_versions, parse_version, VersionFormatError,
                     validate_download_url, download_setup)

check("正常版本比较: 4.4.10 > 4.4.9", compare_versions("4.4.9", "4.4.10") is True)
check("正常版本比较: 相同版本不更新", compare_versions("4.5.13", "4.5.13") is False)
try:
    compare_versions("4.5.13", "4.5.13a")
    check("非数字段报错而非归零", False, "竟然没报错")
except VersionFormatError as e:
    check("非数字段报错而非归零（旧实现归零成 [4,5,0] → 永远收不到更新）", True, str(e))
try:
    parse_version("4.5.13a")
    check("parse_version 拒绝非数字段", False)
except VersionFormatError:
    check("parse_version 拒绝非数字段", True)
check("旧实现确实会归零（反证）", [int(s) if s.isdigit() else 0 for s in "4.5.13a".split(".")] == [4, 5, 0],
      f"旧逻辑把 '13a' → 0，得到 {[int(s) if s.isdigit() else 0 for s in '4.5.13a'.split('.')]}，"
      f"小于 [4,5,13] → 永远判定为无更新")

for bad in ("http://evil.example.com/a.exe", "file:///C:/Windows/System32/calc.exe",
            "ftp://x/a.exe", "", "  "):
    try:
        validate_download_url(bad)
        check(f"拒绝非法下载地址 {bad[:36]!r}", False, "竟然通过")
    except ValueError:
        check(f"拒绝非法下载地址 {bad[:36]!r}", True)

check("放行 https 地址",
      validate_download_url("https://cos.example.com/a.exe") == "https://cos.example.com/a.exe")

check("download_setup 对 http:// 立即返回 None（不发请求）",
      download_setup("http://evil/a.exe", "x", tempfile.mkdtemp()) is None)
check("download_setup 对 file:// 立即返回 None（不读本机文件当安装包）",
      download_setup("file:///C:/Windows/win.ini", "x", tempfile.mkdtemp()) is None)
check("manifest 校验：fetch_update_info 内会校验 url scheme",
      "data[\"url\"] = validate_download_url" in src_of("updater.py"))

# gui 侧对版本格式错误做了显式提示（不静默当“无更新”）
check("gui 捕获 VersionFormatError 并显式提示", "except VersionFormatError as e:" in gui_src
      and "manifest_error" in gui_src)


# ============================================================
# 🔴7 PDF 缓存清理 key 失配
# ============================================================
print("\n=== 🔴7 PDF 缓存清理 key ===")
import cloud_client as cc


class _StubCC:
    _cleanup_pdf_cache = cc.CloudClient._cleanup_pdf_cache

    def __init__(self, cache_dir, hours, index):
        self._cache_dir = cache_dir
        self._CACHE_RETENTION_HOURS = hours
        self._index = dict(index)
        self._index_path = os.path.join(cache_dir, "cache_index.json")
        self._cache_cleanup_scheduled = False
        self.messages = []

        class _Sig:
            def __init__(self, outer):
                self.outer = outer

            def emit(self, m):
                self.outer.messages.append(m)

        self.status_message = _Sig(self)

    def _load_cache_index(self):
        return dict(self._index)

    def _save_cache_index(self, idx):
        self._index = dict(idx)

    def _cache_index_path(self):
        return self._index_path


d = tempfile.mkdtemp(prefix="hn_cache_")
old = (datetime.now() - timedelta(hours=100)).strftime("%Y-%m-%d %H:%M:%S")
fresh = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

# 索引 key 带后缀（真实存在的形态：图片方向后缀 / 转换引擎后缀）
keys = ["abc123", "abc123_landscape", "def456_eword", "ghi789_ewps", "fresh_key_portrait"]
files = {f"{k}.pdf": (old if not k.startswith("fresh") else fresh) for k in keys}
for name in files:
    with open(os.path.join(d, name), "wb") as f:
        f.write(b"%PDF-1.4 test")
# 一个无索引孤儿（很旧）
orphan = os.path.join(d, "orphan_no_index.pdf")
with open(orphan, "wb") as f:
    f.write(b"%PDF-1.4 orphan")
os.utime(orphan, (0, 0))

index = {k: {"created_at": files[f"{k}.pdf"], "original_name": k + ".pdf"} for k in keys}
stub = _StubCC(d, 24, index)
stub._cleanup_pdf_cache()

remaining = sorted(os.listdir(d))
check("带 _landscape 后缀的过期条目已删除（旧实现永远删不掉）",
      not os.path.exists(os.path.join(d, "abc123_landscape.pdf")))
check("带 _eword 后缀的过期条目已删除", not os.path.exists(os.path.join(d, "def456_eword.pdf")))
check("带 _ewps 后缀的过期条目已删除", not os.path.exists(os.path.join(d, "ghi789_ewps.pdf")))
check("纯 md5 过期条目已删除", not os.path.exists(os.path.join(d, "abc123.pdf")))
check("未过期条目保留", os.path.exists(os.path.join(d, "fresh_key_portrait.pdf")),
      "remaining=" + ",".join(remaining))
check("无索引孤儿文件被回收", not os.path.exists(orphan))
check("索引中过期条目已清除",
      set(stub._index.keys()) == {"fresh_key_portrait"}, str(sorted(stub._index.keys())))

check("源码 clear_local_cache 用索引 key 拼文件名（不再用纯 md5）",
      'os.path.join(self._cache_dir, f"{key}.pdf")' in src_of("cloud_client.py"))
check("源码仍保留纯 md5 拼名的旧写法数量为 0",
      src_of("cloud_client.py").count('f"{md5}.pdf"') == 0,
      f"count={src_of('cloud_client.py').count(chr(102)+chr(34)+'{md5}.pdf'+chr(34))}")

shutil.rmtree(d, ignore_errors=True)


# ============================================================
# 🔴8 converter COM 反初始化
# ============================================================
print("\n=== 🔴8 COM 反初始化配对 ===")
import ast

conv_src = src_of("converter.py")
tree = ast.parse(conv_src)


def _callee_name(node):
    f = node.func
    if isinstance(f, ast.Attribute):
        return f.attr
    if isinstance(f, ast.Name):
        return f.id
    return ""


calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
n_ci = sum(1 for c in calls if _callee_name(c) == "CoInitialize")
n_cu = sum(1 for c in calls if _callee_name(c) == "CoUninitialize")
n_helper = sum(1 for c in calls if _callee_name(c) == "_com_uninitialize_quietly")

# 代码级（AST）断言：注释/文档字符串里提到 com_init 不算
check("AST 中不再存在 com_init 变量（错误前提已彻底从代码移除）",
      not any(isinstance(n, ast.Name) and n.id == "com_init" for n in ast.walk(tree)))
check("源码文本中 com_init 仅出现在说明性注释里",
      all(("com_init" not in ln) or ln.lstrip().startswith("#") or "`" in ln or "P1" in ln
          for ln in conv_src.split("\n") if "com_init" in ln),
      f"出现 {conv_src.count('com_init')} 次，均在注释/文档中")
check("AST 中不再存在 `if com_init == 0` 形式的判断",
      not any(isinstance(n, ast.If) and isinstance(n.test, ast.Compare)
              and any(isinstance(s, ast.Name) and s.id == "com_init" for s in ast.walk(n.test))
              for n in ast.walk(tree)))

check(f"CoInitialize 调用数 = {n_ci}（2 处预热线程 + 5 处转换函数）", n_ci == 7, f"n_ci={n_ci}")
check(f"_com_uninitialize_quietly 调用数 = {n_helper}（5 处配对）", n_helper == 5, f"n_helper={n_helper}")
check(f"CoUninitialize 调用数 = {n_cu}（1 处 helper 内部 + 2 处预热线程）", n_cu == 3, f"n_cu={n_cu}")
check(f"配对平衡：CoInitialize({n_ci}) == 预热配对(2) + helper 配对({n_helper})",
      n_ci == 2 + n_helper, f"{n_ci} vs {2 + n_helper}")
check("反初始化统一走 _com_uninitialize_quietly（吞异常、不阻塞主流程）",
      "def _com_uninitialize_quietly()" in conv_src)

import converter

# 行为：未初始化 / 重复反初始化都不应抛异常
try:
    converter._com_uninitialize_quietly()
    converter._com_uninitialize_quietly()
    check("_com_uninitialize_quietly 重复调用不抛异常（安全兜底）", True)
except Exception as e:
    check("_com_uninitialize_quietly 重复调用不抛异常", False, repr(e))

# 真实配对：初始化 → 反初始化 → 再初始化，工作线程里也应正常
try:
    import pythoncom

    assert pythoncom.CoInitialize() is None, "前提失效：CoInitialize 返回值不再是 None"
    check("前提：pythoncom.CoInitialize() 返回 None（旧 `== 0` 判断恒 False 的根因）", True)
    converter._com_uninitialize_quietly()
    check("真实 CoInitialize → _com_uninitialize_quietly 配对成功", True)
except Exception as e:
    check("真实 COM 配对", False, repr(e))

print("\n" + "=" * 52)
print("本地工具侧验收:", "全部通过 ✅" if ok_all else "存在失败 ❌")
sys.exit(0 if ok_all else 1)
