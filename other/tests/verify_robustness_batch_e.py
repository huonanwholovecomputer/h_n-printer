# -*- coding: utf-8 -*-
"""批次 E（🟡 15 条）验收：静默降级 / 静默吞错 / 静默丢账，一律要留痕或有补救出口。

这批问题都不是崩溃，而是「出了事没人知道」：
  🟡1  结算页 save() 只有 250ms 防抖、无卸载兜底、多标签页互不知情 ⇒ 关页丢最后一笔
  🟡2  revenue 的 `_file_range_stats` 少传 simplex_pages ⇒ 封面单独打印的单少算张数
  🟡3  `_start_print_attempts` 只在内存、先计数后发送 ⇒ 重启重刷、发送失败也烧额度
  🟡4  首页（封面）没打出来只写一行日志，整批照常按成功收尾
  🟡5  台账写失败退回「按失败处理」⇒ 云端重推、整单再出一遍
  🟡6  出纸核对没排空时，翻纸提示仍说「应有 N 张，数够再翻」（没退路）
  🟡7  离线缓存命中只验 `os.path.isfile` ⇒ 0 字节 / 半截 PDF 也当缓存
  🟡8  配置读坏静默回默认值（价格/打印机全变回默认，界面无声）
  🟡10 没有接单设备时，排队超时原因一律写「排队超时」
  🟡11 `recover_stale_printing_tasks` 无条件把 printing 翻回 queued（重启撞上正在打的双面）
  🟡12 Excel 版式解析失败只 logger.debug ⇒ 缩放/区域静默失效
  🟡13 主题设置非原子写、坏文件永不重建
  🟡E1 `_abandon_jobs` 把 task_id 当 order_id（会误标别人的整单）
  🟡E2 队列丢单/换号补报失败被全吞

用法: python other/tests/verify_robustness_batch_e.py   （exit 0 = 全部通过）
"""
import io
import json
import os
import re
import shutil
import sys
import tempfile
import traceback
from datetime import datetime, timedelta

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LOCAL_TOOL = os.path.join(REPO, "local_print_tool")
SRC_BACKEND = os.path.join(REPO, "mobile_apps", "printer-backend")

ok_all = True
_bad = 0


def check(name, cond, detail=""):
    global ok_all, _bad
    if not cond:
        ok_all = False
        _bad += 1
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


def read(path):
    with open(path, "r", encoding="utf-8", newline="") as f:
        return f.read()


def read_json(path):
    """读不到 / 不是 JSON 都返回 None —— 让断言变红而不是让脚本崩掉。

    变异自证时最容易踩的就是这里：把写盘改坏以后脚本会 FileNotFoundError，
    看上去"没有 FAIL 行"，其实是整段没跑完。
    """
    try:
        return json.loads(read(path))
    except Exception:
        return None


GUI_SRC = read(os.path.join(LOCAL_TOOL, "gui.py"))
THEME_SRC = read(os.path.join(LOCAL_TOOL, "theme_manager.py"))
CLOUD_SRC = read(os.path.join(LOCAL_TOOL, "cloud_client.py"))
OFFLINE_SRC = read(os.path.join(LOCAL_TOOL, "offline_sync.py"))
SETTLE_SRC = read(os.path.join(LOCAL_TOOL, "finance", "settlement.html"))
APP_SRC = read(os.path.join(SRC_BACKEND, "app.py"))

_tempdirs = []


def tmpprefix(prefix):
    d = tempfile.mkdtemp(prefix=prefix)
    _tempdirs.append(d)
    return d


print("\n=== 🟡E1 放弃通知只认 order_id（不许把 task_id 当 order_id）===")


def _no_taskid_abandon():
    """用 AST 查：真代码里不许出现 abandon_order_to_server(<x>.task_id)。

    不能直接对源码做正则 —— 说明这个坑的 docstring 里就写着旧写法
    （`abandon_order_to_server(job.task_id)`），正则会把它当成真代码命中。
    """
    import ast

    bad = []
    tree = ast.parse(GUI_SRC)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
        if name != "abandon_order_to_server":
            continue
        for arg in node.args:
            if isinstance(arg, ast.Attribute) and arg.attr == "task_id":
                bad.append(getattr(node, "lineno", 0))
    return not bad

check("①-a 定义了 `_abandon_cloud_job`",
      "def _abandon_cloud_job(self, job) -> bool:" in GUI_SRC)
_m = re.search(r"\n    def _abandon_cloud_job\(self, job\) -> bool:(.*?)\n    def ", GUI_SRC, re.S)
_helper = _m.group(1) if _m else ""
check("①-b helper 只在 order_id>0 时发 abandon",
      "oid > 0" in _helper and "abandon_order_to_server(oid)" in _helper)
check("①-c helper 对「只有 task_id」的情形显式跳过并解释原因",
      "task_id" in _helper and "跳过" in _helper)
check("①-d 全仓没有把 job.task_id 塞进 abandon_order_to_server", _no_taskid_abandon())
check("①-e 五处以上调用点已改用 helper",
      GUI_SRC.count("_abandon_cloud_job(") >= 6,
      f"{GUI_SRC.count('_abandon_cloud_job(')} 处")

print("\n=== 🟡E2 队列丢单 / 换号补报失败要看得见 ===")
check("②-a 状态队列满了会 emit 提示（不只写 logger）",
      "状态同步队列已满" in CLOUD_SRC and "status_message.emit" in CLOUD_SRC)
check("②-b 离线队列丢单留一次性提示接口",
      "take_queue_drop_note" in OFFLINE_SRC and "_queue_drop_note" in OFFLINE_SRC)
check("②-c GUI 会消费这个提示（日志 + 状态栏）",
      "take_queue_drop_note()" in GUI_SRC and "离线队列已满" in GUI_SRC)
check("②-d 换号补报失败不再 `except Exception: pass` 全吞",
      "换号补报失败" in GUI_SRC and "换号补报异常" in GUI_SRC)

print("\n=== 🟡5 台账写失败不再「按失败处理」===")
check("⑤-a 有 stall-fallback 留存路径", "stall-fallback-" in GUI_SRC)
check("⑤-b 台账写失败改成返回带 id 的空记录（→ job_stalled）",
      re.search(r'self\._stalled_record = \{\s*\n?\s*"id": ""', GUI_SRC) is not None)
check("⑤-c finally 里按 _keep_merged_pdf 决定是否删除合批 PDF",
      "_keep_merged_pdf" in GUI_SRC and
      re.search(r'if not getattr\(self, "_keep_merged_pdf", False\)', GUI_SRC) is not None)
check("⑤-d 空 id 的半成品不会写 remedy_pending（否则闸门会挡住重打）",
      "没拿到半成品记录" in GUI_SRC)

print("\n=== 🟡4 首页没打出来要看得见 ===")
check("④-a PrintWorker 有 cover_failed 信号", "cover_failed = Signal(str)" in GUI_SRC)
check("④-b 三处失败点都会 emit",
      GUI_SRC.count("self.cover_failed.emit(") >= 3,
      f"{GUI_SRC.count('self.cover_failed.emit(')} 处")
check("④-c 主窗接线并落地提示方法",
      "worker.cover_failed.connect(self._on_cover_failed)" in GUI_SRC
      and "def _on_cover_failed(self, msg: str)" in GUI_SRC)
check("④-d 收尾时会提到「首页未打出」而不是宣称全部成功",
      "首页未打出" in GUI_SRC and "打印完成（缺首页）" in GUI_SRC)

print("\n=== 🟡7 离线缓存要验 %PDF 头 ===")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["APPDATA"] = tmpprefix("hn_batch_e_appdata_")
sys.path.insert(0, LOCAL_TOOL)
os.chdir(LOCAL_TOOL)
try:
    import gui  # noqa: PLC0415
    _gui_imported = True
except Exception:
    traceback.print_exc()
    _gui_imported = False

check("⑦-a gui 提供了 `_pdf_cache_usable`",
      hasattr(gui, "_pdf_cache_usable") if _gui_imported else False)
if _gui_imported:
    _d = tmpprefix("hn_pdfcache_")
    _zero = os.path.join(_d, "zero.pdf")
    open(_zero, "wb").close()
    _txt = os.path.join(_d, "text.pdf")
    with open(_txt, "w", encoding="utf-8") as f:
        f.write("这不是 PDF" * 400)
    _halfpdf = os.path.join(_d, "half.pdf")
    with open(_halfpdf, "wb") as f:
        f.write(b"%PD")
        f.write(b"x" * 2000)
    _good = os.path.join(_d, "good.pdf")
    with open(_good, "wb") as f:
        f.write(b"%PDF-1.4\n")
        f.write(b"x" * 2000)
    check("⑦-b 0 字节文件不算可用", gui._pdf_cache_usable(_zero) is False)
    check("⑦-c 文本文件（不是 PDF）不算可用", gui._pdf_cache_usable(_txt) is False)
    check("⑦-d 头不是 %PDF- 的残文件不算可用", gui._pdf_cache_usable(_halfpdf) is False)
    check("⑦-e 真 PDF 算可用", gui._pdf_cache_usable(_good) is True)
    check("⑦-f 不存在的路径不算可用", gui._pdf_cache_usable(os.path.join(_d, "none.pdf")) is False)
check("⑦-g 离线分支已改用新判据（不再只有 isfile）",
      "_pdf_cache_usable(cached_pdf)" in GUI_SRC)

print("\n=== 🟡8 配置读坏了要让人知道 ===")
import printer_config as pc  # noqa: PLC0415

_cfgdir = tmpprefix("hn_cfg_")
_cfgpath = os.path.join(_cfgdir, "print_config.json")
with open(_cfgpath, "w", encoding="utf-8") as f:
    f.write('{"printer_name": "坏掉的配置", ')
try:
    pc.CONFIG_LOAD_ERRORS.clear()
except Exception:
    pass
cfg = pc.PrinterConfig.load(_cfgpath)
check("⑧-a 坏配置返回默认值而不是抛异常", isinstance(cfg, pc.PrinterConfig))
check("⑧-b 坏配置被另存为 .corrupt-*",
      any(n.startswith("print_config.json.corrupt-") for n in os.listdir(_cfgdir)),
      str(os.listdir(_cfgdir)))
check("⑧-c 留了一条给界面看的说明", len(pc.CONFIG_LOAD_ERRORS) >= 1,
      str(pc.CONFIG_LOAD_ERRORS[:1]))
check("⑧-d 说明里带原文件路径与备份路径",
      any("print_config.json" in n and "corrupt" in n for n in pc.CONFIG_LOAD_ERRORS))
check("⑧-e GUI 有启动检查方法且已挂定时器",
      "def _check_config_load_errors" in GUI_SRC
      and "QTimer.singleShot(2400, self._check_config_load_errors)" in GUI_SRC)
check("⑧-f 提示文案说明「已改用默认配置」", "已改用默认配置" in GUI_SRC)

print("\n=== 🟡13 主题设置：原子写 + 坏文件重建 ===")
import theme_manager as thm  # noqa: PLC0415

_thdir = tmpprefix("hn_theme_")
_thpath = os.path.join(_thdir, "theme_settings.json")
_tm = thm.ThemeManager(_thpath)
_tm._save_setting()
check("⑨-a 保存后是合法 JSON", (read_json(_thpath) or {}).get("theme_mode") in thm.ALL_MODES)
check("⑨-b 保存后没有留下 .tmp", not os.path.exists(_thpath + ".tmp"))
check("⑨-c 写入走 tmp + os.replace + fsync（源码判据）",
      "os.replace(tmp, self._settings_path)" in THEME_SRC and "os.fsync" in THEME_SRC)
with open(_thpath, "w", encoding="utf-8") as f:
    f.write("{坏掉的 json")
_tm2 = thm.ThemeManager(_thpath)
check("⑨-d 坏文件不会让构造抛异常", isinstance(_tm2, thm.ThemeManager))
check("⑨-e 坏文件被另存为 .corrupt-*",
      any(n.startswith("theme_settings.json.corrupt-") for n in os.listdir(_thdir)),
      str(os.listdir(_thdir)))
check("⑨-e2 坏文件之后被重建成合法 JSON",
      (read_json(_thpath) or {}).get("theme_mode") in thm.ALL_MODES)

print("\n=== 🟡12 Excel 版式解析失败不再静默 ===")
check("⑩-a 不再只有 logger.debug『按不传处理』",
      'logger.debug("解析表格版式失败，按不传处理"' not in GUI_SRC)
check("⑩-b 升级成 warning 且带异常",
      "解析表格版式失败" in GUI_SRC and "logger.warning(" in GUI_SRC)
check("⑩-c 界面日志里说清「缩放/区域不会生效」",
      "缩放/区域不会生效" in GUI_SRC)

print("\n=== 🟡6 出纸没排空时翻纸提示要给出退路 ===")
check("⑪-a `_ask_operator_to_flip` 支持 warn 参数",
      'def _ask_operator_to_flip(self, plan, tray_foreign: int = 0, warn: str = "")' in GUI_SRC)
check("⑪-b 调用点把 _drain_warn 传进去",
      "_ask_operator_to_flip(plan, tray_foreign, warn=_drain_warn)" in GUI_SRC)
check("⑪-c 未排空时文案给出「少一张就点跳过」的退路", "少一张就点「跳过」" in GUI_SRC)
check("⑪-d 攒批出纸核对异常不再 `except Exception: pass` 全吞",
      "出纸核对异常" in GUI_SRC and "出纸核对线程没能启动" in GUI_SRC)

print("\n=== 🟡1 结算页：关页不丢最后一笔 + 多标签页互相提醒 ===")
check("⑫-a 注册了 pagehide / beforeunload 兜底",
      "addEventListener('pagehide', _flushBeforeUnload)" in SETTLE_SRC
      and "addEventListener('beforeunload', _flushBeforeUnload)" in SETTLE_SRC)
check("⑫-b 离开页面前把未保存改动塞进离线暂存",
      "finance_offline_pending" in SETTLE_SRC and "_saveDirty" in SETTLE_SRC)
check("⑫-c 保存成功会广播给其它标签页",
      "_announceTabSave" in SETTLE_SRC and "finance_tab_touch" in SETTLE_SRC)
check("⑫-d 监听 storage 事件并提示「另一个标签页在改」",
      "addEventListener('storage'" in SETTLE_SRC and "另一个标签页" in SETTLE_SRC)
check("⑫-e 本地模式与云端模式的成功分支都清 dirty 并广播",
      SETTLE_SRC.count("_saveDirty = false") >= 2
      and SETTLE_SRC.count("_announceTabSave();") >= 2,
      f"dirty={SETTLE_SRC.count('_saveDirty = false')} announce={SETTLE_SRC.count('_announceTabSave();')}")

print("\n=== 🟡2/3/10/11 后端（静态）===")


def _push_has_no_increment(src):
    """用 AST 查 `_start_print_push_due` 函数体里没有对 st["n"] 的自增。

    这个函数的 docstring 本身就写着「以前 `st["n"] += 1` 写在返回之前」，
    用字符串判据必然误判，只能看真代码。
    """
    import ast

    for node in ast.walk(ast.parse(src)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.name == "_start_print_push_due":
            for sub in ast.walk(node):
                if not isinstance(sub, ast.AugAssign):
                    continue
                tgt = sub.target
                if isinstance(tgt, ast.Subscript) \
                        and isinstance(tgt.slice, ast.Constant) \
                        and tgt.slice.value == "n":
                    return False
            return True
    return False

check("⑬-a revenue 那处 _file_range_stats 已传 simplex_pages",
      re.search(r'_file_range_stats\(of_row\["page_count"\][^)]*_row_simplex_pages\(of_row\)',
                APP_SRC, re.S) is not None)
check("⑬-b 兜底重发计数落盘：有 path/load/save 三个函数",
      all(k in APP_SRC for k in ("def _start_print_state_path(", "def _load_start_print_attempts(",
                                 "def _save_start_print_attempts(")))
_push_body = APP_SRC.split("def _start_print_push_due", 1)[1].split("\ndef ", 1)[0]
check("⑬-c `_start_print_push_due` 不再自增（只判定）", _push_has_no_increment(APP_SRC))
check("⑬-d 发送成功才调 `_mark_start_print_sent`",
      '_mark_start_print_sent(row["of_id"], time.time())' in APP_SRC)
check("⑬-e init_db 会把计数读回来",
      re.search(r"_cleanup_once_on_startup\(\)[\s\S]{0,1200}_load_start_print_attempts\(\)",
                APP_SRC) is not None)
check("⑬-f 排队超时要区分「没设备在线 / 设备不在线 / 在线没认领」",
      all(k in APP_SRC for k in ("期间没有任何接单设备在线", "期间一直不在线", "在线但一直没接这一单")))
_rec_body = APP_SRC.split("def recover_stale_printing_tasks", 1)[1].split("\ndef ", 1)[0]
check("⑬-g `recover_stale_printing_tasks` 改成委托孤儿回收（不再无条件翻 queued）",
      "recover_orphaned_printing_tasks()" in _rec_body
      and "UPDATE order_files SET status = 'queued' WHERE id = ?" not in _rec_body)

print("\n=== 后端行为：计数落盘 / 停发一次 / 超时原因 / 启动委托 ===")
_tmp = tmpprefix("hn_batch_e_backend_")
_dst = os.path.join(_tmp, "printer-backend")
shutil.copytree(SRC_BACKEND, _dst, ignore=shutil.ignore_patterns(
    "__pycache__", "orders.db", "users.db", "uploads", "logs", "venv", "*.bak-*"))
sys.path.insert(0, _dst)
os.chdir(_dst)
try:
    import app as backend  # noqa: PLC0415
    backend.init_db()
    _backend_ok = True
except Exception:
    traceback.print_exc()
    _backend_ok = False

if _backend_ok:
    try:
        backend._start_print_attempts.clear()
    except Exception:
        pass
    _due1, _ = backend._start_print_push_due(9001, 1000.0)
    check("⑭-a 第一次判定为「该发」", _due1 is True)
    check("⑭-b 判定本身不自增（还没发出去）",
          backend._start_print_attempts.get(9001, {}).get("n", 0) == 0,
          str(backend._start_print_attempts.get(9001)))
    backend._mark_start_print_sent(9001, 1000.0)
    check("⑭-c 发送成功后计数 +1",
          backend._start_print_attempts.get(9001, {}).get("n") == 1)
    _due2, _ = backend._start_print_push_due(9001, 1005.0)
    check("⑭-d 间隔没到不再重发（30 秒节奏）", _due2 is False)
    _state_path = backend._start_print_state_path()
    check("⑭-e 计数已写盘", os.path.isfile(_state_path))
    backend._start_print_attempts.clear()
    backend._load_start_print_attempts()
    check("⑭-f 重启（清内存后读回）计数还在",
          backend._start_print_attempts.get(9001, {}).get("n") == 1,
          str(backend._start_print_attempts.get(9001)))

    backend._start_print_attempts.clear()
    _now = 100000.0
    _fires = 0
    _stop_reports = 0
    for _i in range(20):
        _d, _st = backend._start_print_push_due(9002, _now)
        if _d:
            _fires += 1
            backend._mark_start_print_sent(9002, _now)
        if _st:
            _stop_reports += 1
        _now += 700.0 if _i >= 9 else 31.0
    check("⑮-a 最多发 16 次", _fires == 16, f"{_fires} 次")
    check("⑮-b 「刚停发」只报一次", _stop_reports == 1, f"{_stop_reports} 次")

    def _cols(conn, t):
        return {r[1] for r in conn.execute(f"PRAGMA table_info({t})").fetchall()}

    def _ins(conn, table, **vals):
        allowed = _cols(conn, table)
        data = {k: v for k, v in vals.items() if k in allowed}
        cur = conn.execute(
            f"INSERT INTO {table} ({', '.join(data)}) VALUES ({', '.join('?' for _ in data)})",
            tuple(data.values()))
        return cur.lastrowid

    _old = (datetime.now() - timedelta(hours=48)).strftime("%Y-%m-%d %H:%M:%S")
    conn = backend.get_db()
    try:
        backend.printer_clients.clear()
        _oid = _ins(conn, "orders", file="t.docx", copies=1, status="queued",
                    created_at=_old, total_price=1.0)
        _ofid = _ins(conn, "order_files", order_id=_oid, status="queued",
                     created_at=_old, page_count=2, copies=1, duplex="on",
                     total_price=1.0, file_name="t.docx")
        conn.commit()
    finally:
        conn.close()
    try:
        backend._SECURITY["queued_timeout_hours"] = 24
        backend.expire_stale_queued_orders()
        conn = backend.get_db()
        _row = conn.execute("SELECT status, reject_reason FROM order_files WHERE id = ?",
                            (_ofid,)).fetchone()
        conn.close()
        _reason = (_row["reject_reason"] or "") if _row else ""
        check("⑯-a 仍然超时取消（状态是对的）",
              bool(_row) and _row["status"] == backend.TIMEOUT_STATUS,
              str(tuple(_row)) if _row else "no row")
        check("⑯-b 原因写清「没有任何接单设备在线」",
              "没有任何接单设备在线" in _reason, _reason)
    except Exception:
        traceback.print_exc()
        check("⑯ 排队超时原因可读", False, "执行异常")

    _called = {"n": 0}
    _orig = backend.recover_orphaned_printing_tasks

    def _spy():
        _called["n"] += 1

    backend.recover_orphaned_printing_tasks = _spy
    try:
        _ret = backend.recover_stale_printing_tasks()
    finally:
        backend.recover_orphaned_printing_tasks = _orig
    check("⑰-a 启动清理会走孤儿回收（带在线判据）", _called["n"] == 1)
    check("⑰-b 返回值不再是「翻了多少条」", _ret == 0)

for d in _tempdirs:
    shutil.rmtree(d, ignore_errors=True)

print("\n%s（%d 条失败）" % ("❌ 有失败" if not ok_all else "✅ 全部通过", _bad))
sys.exit(0 if ok_all else 1)
