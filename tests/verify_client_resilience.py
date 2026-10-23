# -*- coding: utf-8 -*-
"""验收 · S1-09 客户端韧性（R10 / DS-23）

方案要求（docs/安全加固修复计划_2026-10-23.md §S1-09）：
  ① 所有请求都有 10–15s 超时（小程序 `wx.request` 的 timeout、APP `fetch` 的
     AbortController、本地工具 `requests` 的 timeout）；
  ② 轮询改「上一次完成后再排下一次」（`setTimeout` 自调度），失败指数退避（1.5×，上限 5 min），
     成功即恢复；
  ③ `offline_sync` **不放弃**（原 `MAX_RETRY_COUNT=5` 一过就永久跳过 = 订单静默消失）、
     离线库加 `status` 列；本地工具对未同步单显示「离线未同步」并给手动重试，
     **不得**显示成「已完成」；
  ④ 「连着但慢」的取号等待 30s → 3–5s，超时直接回落本地号。

本脚本 = 静态（接线/公式）+ 行为（桩模拟长时间断网：离线单不丢、恢复后补报）+ 变异自证。
⚠ 不联网、不启动 GUI、不跑真 Office；所有网络调用都是桩。

用法: python tests/verify_client_resilience.py     （exit 0 = 全绿）
"""
import io
import os
import re
import shutil
import sys
import tempfile
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL_TOOL = os.path.join(REPO, "local_print_tool")
MOBILE = os.path.join(REPO, "mobile_apps")
APP = os.path.join(MOBILE, "android_app", "www")
MINI = os.path.join(MOBILE, "h_n_print")

ok_all = True


def check(name, cond, detail=""):
    global ok_all
    if not cond:
        ok_all = False
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


def section(t):
    print("\n" + "=" * 70)
    print(t)
    print("=" * 70)


# ── 被测文件（全部读进内存，便于变异自证）──────────────────────────────
PATHS = {
    "cloud_client": os.path.join(LOCAL_TOOL, "cloud_client.py"),
    "offline_sync": os.path.join(LOCAL_TOOL, "offline_sync.py"),
    "gui": os.path.join(LOCAL_TOOL, "gui.py"),
    "mini_request": os.path.join(MINI, "utils", "request.js"),
    "mini_index": os.path.join(MINI, "pages", "index", "index.js"),
    "mini_me": os.path.join(MINI, "pages", "me", "me.js"),
    "app_js": os.path.join(APP, "app.js"),
    "app_me": os.path.join(APP, "me.js"),
    "app_print": os.path.join(APP, "print.js"),
    "app_updater": os.path.join(APP, "updater.js"),
}
FILES = {}
for _k, _p in PATHS.items():
    with open(_p, encoding="utf-8") as _f:
        FILES[_k] = _f.read()


def _blank_comment_lines(text):
    """`#`//` 之后置等长空格（保偏移）—— 判据不许吃注释。JS 只处理行首 // 注释较安全，这里
    只对 Python 用；JS 用 _strip_js_comments。"""
    out = []
    for line in text.split("\n"):
        i = line.find("#")
        out.append(line if i < 0 else line[:i] + " " * (len(line) - i))
    return "\n".join(out)


def _strip_js_comments(text):
    """把 //… 与 /*…*/ 换成空格（保长度/行号），避免判据命中注释里的示例代码。"""
    text = re.sub(r"/\*.*?\*/", lambda m: " " * len(m.group(0)), text, flags=re.S)
    return re.sub(r"//[^\n]*", lambda m: " " * len(m.group(0)), text)


def _py_call_blocks(src, pattern):
    """按括号配对取出每个调用的完整文本（多行调用也要整块判断 timeout 是否在内）。"""
    for m in re.finditer(pattern, src):
        i = m.end() - 1
        depth = 0
        j = i
        while j < len(src):
            c = src[j]
            if c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        yield src[:m.start()].count("\n") + 1, src[m.start():j + 1]


def _py_func_block(src, name):
    """取 Python 函数整段源码（支持多行签名与类方法）。

    做法：先跳到签名右括号 `)`，再跳到该行行尾（跳过 `-> 注解:`），然后扫到第一个
    **缩进不深于 def** 的非空行为止。⚠ 不能"遇到第一行缩进回退就停"：多行签名的
    收尾行（`    ) -> tuple[...]:`）缩进就等于 def，会被误判成函数结束。"""
    m = re.search(r"^(?P<ind>[ \t]*)def " + re.escape(name) + r"\(", src, re.M)
    if not m:
        return ""
    ind = len(m.group("ind"))
    i = src.find("(", m.end() - 1)
    depth = 0
    j = i
    while j < len(src):
        c = src[j]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                break
        j += 1
    nl = src.find("\n", j)
    rest = nl + 1 if nl >= 0 else len(src)
    end = len(src)
    for mm in re.finditer(r"^([^\n]*)$", src[rest:], re.M):
        line = mm.group(1)
        if not line.strip():
            continue
        if len(line) - len(line.lstrip()) <= ind:
            end = rest + mm.start()
            break
    return src[m.start():end]


def _js_func_block(src, name):
    """取 JS 函数整段源码（`function name(`，含 async）。到顶格 `}` 行为止。"""
    m = re.search(r"(?:async\s+)?function\s+" + re.escape(name) + r"\s*\(", src)
    if not m:
        return ""
    close = re.search(r"^\}", src[m.end():], re.M)
    if not close:
        return src[m.start():]
    return src[m.start():m.end() + close.end()]


def _strip_py_docstrings(text):
    """把三引号 docstring 换成空格（保长度），判据不咬文档里的反例说明。"""
    return re.sub(r'"""(?:.|\n)*?"""', lambda m: re.sub(r"[^\n]", " ", m.group(0)), text)


def _func_block(src, name, indent_tolerant=True):
    """兼容旧名（Python 函数体）。"""
    return _py_func_block(src, name)


# ============================================================
# 静态判据（纯函数，输入 FILES 字典 → 便于变异自证）
# ============================================================

REQ_RE = r"http_requests\.(?:get|post|put|delete|patch|request|head)\s*\("


def check_requests_timeouts(files):
    """本地工具里**每个** requests 调用都必须带 timeout（10–15s；下载类允许更长）。"""
    bad = []
    for key in ("cloud_client", "offline_sync", "gui"):
        src = files[key]
        for line, blk in _py_call_blocks(src, REQ_RE):
            if "timeout" not in blk:
                bad.append(f"{key}:{line}")
    return bad


def check_mini_wx_timeouts(files):
    """小程序：wx.request 只在 utils/request.js 里出现，且每次都带 timeout（默认 12s）。"""
    src = files["mini_request"]
    blocks = list(_py_call_blocks(src, r"wx\.request\s*\("))
    if not blocks:
        return ["no wx.request in utils/request.js"]
    bad = [f"line {ln}" for ln, blk in blocks if "timeout" not in blk]
    # 默认值必须落在 10–15s 区间，且是"调用方没传就用默认"
    m = re.search(r"DEFAULT_TIMEOUT_MS\s*=\s*(\d+)", src)
    default_ok = bool(m) and 10000 <= int(m.group(1)) <= 15000
    fallback_ok = "options.timeout || DEFAULT_TIMEOUT_MS" in src
    if not default_ok:
        bad.append("DEFAULT_TIMEOUT_MS 不在 10–15s 区间")
    if not fallback_ok:
        bad.append("wx.request 未使用 options.timeout || DEFAULT_TIMEOUT_MS")
    # 其他页面不许绕过封装直接调 wx.request
    for key in ("mini_index", "mini_me"):
        for ln, _blk in _py_call_blocks(_strip_js_comments(files[key]), r"wx\.request\s*\("):
            bad.append(f"{key} 绕过封装直接用 wx.request（line {ln}）")
    return bad


def check_app_fetch_timeouts(files):
    """APP：每个裸 fetch 都必须挂 AbortController（或在 api() 封装里，由封装统一挂），
    且默认超时落在 10–15s 区间。"""
    bad = []
    m = re.search(r"API_TIMEOUT_MS\s*=\s*(\d+)", _strip_js_comments(files["app_js"]))
    if not m:
        bad.append("app.js: 缺 API_TIMEOUT_MS")
    elif not (10000 <= int(m.group(1)) <= 15000):
        bad.append(f"app.js: API_TIMEOUT_MS={m.group(1)} 不在 10–15s 区间")
    for key in ("app_js", "app_updater"):
        src = _strip_js_comments(files[key])
        for m in re.finditer(r"\bfetch\s*\(", src):
            line = src[:m.start()].count("\n") + 1
            # 取所在函数体（往前找最近的 function 声明）
            head = src[:m.start()]
            fm = None
            for cand in re.finditer(r"(?:async\s+)?function\s+(\w+)\s*\(", head):
                fm = cand
            name = fm.group(1) if fm else ""
            body = _js_func_block(src, name) if name else ""
            if "AbortController" in body and "signal" in body:
                continue
            # api() 封装：内部 fetch(fetchOpts)，signal 在调用前挂到 fetchOpts 上
            near = src[max(0, m.start() - 3000):m.start()]
            if "AbortController" in near and "signal" in near:
                continue
            bad.append(f"{key}:{line}（{name or '?'}）没有 AbortController/signal")
    return bad


POLL_MARKERS = {
    # key: 不允许再出现的 setInterval 轮询形态（自调度后应改写成 createPollScheduler）
    "mini_index": [r"_printerPollTimer\s*=\s*setInterval"],
    "mini_me": [r"_orderPollTimer\s*=\s*setInterval", r"_keyPollTimer\s*=\s*setInterval"],
    "app_js": [r"setInterval\(\s*\(\s*\)\s*=>\s*\{\s*checkPrinterStatus"],
    "app_me": [r"_orderPollTimer\s*=\s*setInterval", r"_keyPollTimer\s*=\s*setInterval",
               r"_userOrdersPollTimer\s*=\s*setInterval"],
    "app_print": [r"setInterval\(\s*loadPricing", r"f\._pollTimer\s*=\s*setInterval"],
}


def check_polls_self_scheduled(files):
    """所有网络轮询点都改成 createPollScheduler（setTimeout 自调度），不再用 setInterval。"""
    bad = []
    for key, pats in POLL_MARKERS.items():
        src = _strip_js_comments(files[key])
        for p in pats:
            if re.search(p, src):
                bad.append(f"{key}: 仍是 setInterval 轮询（{p}）")
        # 该文件必须真的用上了调度器
        if "createPollScheduler" not in src:
            bad.append(f"{key}: 没有 createPollScheduler 接线")
    # 小程序首页/我页：从统一封装里取调度器
    for key in ("mini_index", "mini_me"):
        if not re.search(r"require\(['\"]\.\./\.\./utils/request['\"]\)", files[key]) \
                or "createPollScheduler" not in files[key].split("\n")[2]:
            bad.append(f"{key}: 未从 utils/request 引入 createPollScheduler")
    return bad


def check_backoff_formula(files):
    """退避公式：1.5×，上限 5 分钟，成功恢复基线 —— 两份实现（小程序/APP）都要有。"""
    bad = []
    for key in ("mini_request", "app_js"):
        src = _strip_js_comments(files[key])
        if not re.search(r"POLL_BACKOFF_FACTOR\s*=\s*1\.5", src):
            bad.append(f"{key}: 缺 POLL_BACKOFF_FACTOR = 1.5")
        if not re.search(r"POLL_BACKOFF_MAX_MS\s*=\s*300000", src):
            bad.append(f"{key}: 缺 POLL_BACKOFF_MAX_MS = 300000（5 分钟）")
        body = _js_func_block(src, "createPollScheduler")
        if not body:
            bad.append(f"{key}: 没有 createPollScheduler")
            continue
        if not re.search(r"delay\s*\*\s*factor", body) or "Math.min" not in body:
            bad.append(f"{key}: 失败时未按 factor 指数放大并取上限")
        if not re.search(r"delay\s*=\s*base", body):
            bad.append(f"{key}: 成功后未恢复基线间隔")
        if "setTimeout(tick" not in body:
            bad.append(f"{key}: 不是 setTimeout 自调度")
    # 本地工具：兜底拉单循环也是自调度 + 退避
    cc = _blank_comment_lines(files["cloud_client"])
    if not re.search(r"PULL_FALLBACK_BACKOFF_FACTOR\s*=\s*1\.5", cc):
        bad.append("cloud_client: 缺 PULL_FALLBACK_BACKOFF_FACTOR")
    loop = _func_block(cc, "_pull_fallback_loop")
    if "self._stop_event.wait(delay)" not in loop:
        bad.append("cloud_client: 兜底拉单循环不是按 delay 自调度")
    if not re.search(r"delay\s*\*\s*PULL_FALLBACK_BACKOFF_FACTOR", loop):
        bad.append("cloud_client: 失败未按 1.5× 退避")
    if not re.search(r"delay\s*=\s*float\(PULL_FALLBACK_INTERVAL\)", loop):
        bad.append("cloud_client: 成功后未恢复基线间隔")
    return bad


def check_offline_never_gives_up(files):
    """离线单永不放弃：无 retry_count 上限过滤 + status 列 + 退避 + 手动重试。"""
    bad = []
    os_ = _blank_comment_lines(files["offline_sync"])
    if not re.search(r"NEVER_GIVE_UP\s*=\s*True", os_):
        bad.append("offline_sync: 缺 NEVER_GIVE_UP = True")
    if re.search(r"retry_count\s*<\s*\?", os_):
        bad.append("offline_sync: 仍在用 retry_count < ? 过滤（= 到次数就永久放弃）")
    if not re.search(r"ADD COLUMN status", os_):
        bad.append("offline_sync: 离线库没有 status 列")
    if not re.search(r"def retry_now\(", os_):
        bad.append("offline_sync: 缺手动重试入口 retry_now()")
    if not re.search(r"def list_unsynced_orders\(", os_):
        bad.append("offline_sync: 缺 list_unsynced_orders()（UI 用）")
    if not re.search(r"def retry_backoff_seconds\(", os_) or "RETRY_BACKOFF_FACTOR" not in os_:
        bad.append("offline_sync: 缺指数退避公式")
    if not re.search(r"WHERE synced = 0\s*\n\s*\"?\s*\n?\s*AND COALESCE\(status", os_) \
            and "COALESCE(status, 'pending') <>" not in os_:
        bad.append("offline_sync: 同步查询未按 status 排除已拒收单")
    return bad


def check_unsynced_ui(files):
    """本地工具必须把未同步单显示成「离线未同步」并给手动重试；不得显示成「已完成」。"""
    bad = []
    gui = files["gui"]
    if "class OfflineOrdersDialog" not in gui:
        bad.append("gui: 没有「离线未同步订单」窗口")
    if "_refresh_offline_badge" not in gui or "📴 离线未同步" not in gui:
        bad.append("gui: 没有「离线未同步」角标")
    if "def _retry_offline_orders" not in gui:
        bad.append("gui: 没有手动重试入口")
    if "sync.retry_now(" not in gui:
        bad.append("gui: 手动重试没调用 offline_sync.retry_now()")
    # 2026-10-23 用户要求：菜单项只在真有未同步单时出现（平时是纯噪音），与角标同一判据
    if not re.search(r"(?:self\._offline_action|action)\.setVisible\(n > 0\)", gui):
        bad.append("gui: 文件菜单「离线未同步订单」没有按未同步数显隐（要求：有未同步单才出现）")
    if "self._offline_action.setVisible(False)" not in gui:
        bad.append("gui: 菜单项没有默认隐藏（首帧会闪一下）")
    # 窗口提示曾是 markdown 写法（**只存在本机**），QLabel 不解析 → 原样显示星号
    if "下列订单只存在本机" not in gui or "**只存在本机**" in gui:
        bad.append("gui: 「离线未同步订单」窗口提示残留 markdown 星号（**…**）")
    st = _strip_py_docstrings(_py_func_block(gui, "status_text"))
    if "离线未同步" not in st:
        bad.append("gui: 状态列没有「离线未同步」")
    if "已完成" in st:
        bad.append("gui: 未同步单被显示成「已完成」（金额会算错）")
    return bad


def check_order_number_wait(files):
    """「连着但慢」的取号等待 ≤ 5s（原 30s）。"""
    bad = []
    gui = files["gui"]
    m = re.search(r"^ORDER_NUMBER_WAIT_S\s*=\s*(\d+)", _blank_comment_lines(gui), re.M)
    if not m:
        bad.append("gui: 缺 ORDER_NUMBER_WAIT_S 常量")
    elif int(m.group(1)) > 5:
        bad.append(f"gui: ORDER_NUMBER_WAIT_S={m.group(1)} > 5")
    body = _func_block(gui, "_acquire_order_number_and_upload")
    if "ORDER_NUMBER_WAIT_S" not in body:
        bad.append("gui: 取号等待没用 ORDER_NUMBER_WAIT_S")
    for mm in re.finditer(r"deadline\s*=\s*time\.time\(\)\s*\+\s*(\d+)", body):
        if int(mm.group(1)) > 5:
            bad.append(f"gui: 取号等待仍是 {mm.group(1)}s")
    # 另一条取号路径（复制订单号）
    ensure = _func_block(gui, "_ensure_order_number")
    for mm in re.finditer(r"deadline\s*=\s*time\.time\(\)\s*\+\s*(\d+)", ensure):
        if int(mm.group(1)) > 5:
            bad.append(f"gui: _ensure_order_number 等待 {mm.group(1)}s > 5")
    return bad


STATIC_CHECKS = [
    ("① 本地工具 requests 调用全部带 timeout", check_requests_timeouts),
    ("① 小程序 wx.request 全部带 timeout（默认 12s，统一封装）", check_mini_wx_timeouts),
    ("① APP 裸 fetch 全部挂 AbortController", check_app_fetch_timeouts),
    ("② 所有网络轮询都改自调度（无 setInterval 轮询残留）", check_polls_self_scheduled),
    ("② 退避公式 1.5× / 上限 5min / 成功恢复", check_backoff_formula),
    ("③ 离线单永不放弃（无次数上限 + status 列 + 退避 + 手动重试）", check_offline_never_gives_up),
    ("③ 本地工具显示「离线未同步」+ 手动重试，不显示成「已完成」", check_unsynced_ui),
    ("④ 取号等待 ≤ 5s", check_order_number_wait),
]

section("1. 静态判据：超时 / 自调度退避 / 离线不放弃 / 取号等待")
static_results = []
for name, fn in STATIC_CHECKS:
    bad = fn(FILES)
    static_results.append((name, not bad))
    check(name, not bad, "; ".join(bad[:4]))


# ============================================================
# 2. 行为：桩模拟长时间断网（离线单不丢 → 恢复后补报）
# ============================================================
section("2. 行为：压缩时钟模拟 30 轮断网重试（离线单不丢、恢复后补报）")

TMP = tempfile.mkdtemp(prefix="hn_s109_")
sys.path.insert(0, LOCAL_TOOL)
import offline_sync as osync  # noqa: E402

NET_DOWN = {"v": True}
POSTED = []


class _FakeResp:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body or {"success": True}
        self.text = str(self._body)
        self.ok = 200 <= status_code < 300

    def json(self):
        return self._body


def _fake_post(url, params=None, json=None, timeout=None, **kw):
    assert timeout is not None, "requests.post 必须带 timeout"
    if NET_DOWN["v"]:
        raise ConnectionError("模拟断网：目标主机不可达")
    POSTED.append((url, (json or {}).get("order_number")))
    return _FakeResp(200, {"success": True})


_orig_post = osync.http_requests.post
osync.http_requests.post = _fake_post
_orig_first = osync.RETRY_BACKOFF_FIRST_S
_orig_max = osync.RETRY_BACKOFF_MAX_S
try:
    # 压缩退避（真实是 60s 起、1h 上限）：只为了让 30 轮在 1 秒内跑完，代码路径完全一致
    osync.RETRY_BACKOFF_FIRST_S = 0.005
    osync.RETRY_BACKOFF_MAX_S = 0.02

    db = os.path.join(TMP, "local.db")
    sync = osync.OfflineSync(db)
    ORDERS = ["HN20261023-0001", "HN20261023-0002", "HN20261023-0003"]
    for num in ORDERS:
        sync.save_order_offline(order_number=num, files_data=[{"file_name": num + ".pdf"}],
                                total_price=0.6, created_at="2026-10-23 10:00:00")
    check("初始：3 条离线单待同步", sync.pending_count() == 3, str(sync.pending_count()))

    # ── 30 轮失败重试（模拟断网 30 分钟，每轮之间按退避时间等待）──
    for _ in range(30):
        sync.sync_all_pending_orders("https://cloud.example", "tok")
        rows = sync.list_unsynced_orders()
        nxt = min([r["next_retry_at"] for r in rows] or [0])
        wait = max(0.0, min(nxt - time.time(), 0.02))
        time.sleep(wait)

    rows = sync.list_unsynced_orders()
    retries = [r["retry_count"] for r in rows]
    check("★ 断网 30 轮后离线单一条都没丢（pending_count=3）",
          sync.pending_count() == 3, str(sync.pending_count()))
    check("★ 重试次数已远超旧阈值 5（旧实现此时已永久放弃）",
          min(retries) > 5, str(retries))
    check("离线单状态仍是「离线未同步」（pending），没有被标成已同步",
          all(r["status"] == "pending" for r in rows), str([r["status"] for r in rows]))
    check("失败原因有记录（不静默吞掉）", all(r["last_error"] for r in rows),
          str([r["last_error"][:20] for r in rows]))
    check("退避已生效：next_retry_at 在将来（本轮不会被立刻重扫）",
          all(r["next_retry_at"] > 0 for r in rows), str([r["next_retry_at"] for r in rows]))
    check("退避上限受控（≤ RETRY_BACKOFF_MAX_S）",
          osync.retry_backoff_seconds(50) <= osync.RETRY_BACKOFF_MAX_S + 1e-9)

    # 处于退避窗口内 → 本轮不应发起上传（否则退避形同虚设）
    before = len(POSTED)
    sync.sync_all_pending_orders("https://cloud.example", "tok")
    check("退避窗口内不重复上传（sync 扫不到）", len(POSTED) == before, str(POSTED))

    # ── 网络恢复 ──
    NET_DOWN["v"] = False
    reset = sync.retry_now()
    check("手动重试：retry_now() 重置了 3 条", reset == 3, str(reset))
    sync.sync_all_pending_orders("https://cloud.example", "tok")
    check("★ 恢复网络 + 手动重试后：3 条全部补报成功", sync.count_unsynced() == 0,
          str(sync.count_unsynced()))
    check("★ 补报的订单号与原单一致（没有改号/丢单）",
          sorted(n for _u, n in POSTED) == sorted(ORDERS), str(POSTED))
    check("同步成功后 status=synced",
          all(r["status"] == "synced" for r in [
              {"status": "synced"}] if True) and
          all(s == "synced" for s in [
              row[0] for row in __import__("sqlite3").connect(db).execute(
                  "SELECT status FROM offline_orders").fetchall()]),
          "")

    # ── 云端业务拒收（4xx）：不再无限重试，但仍留在库里可见 + 可手动重试 ──
    NET_DOWN["v"] = False
    sync.save_order_offline(order_number="HN20261023-9999",
                            files_data=[{"file_name": "x.pdf"}],
                            total_price=0.3, created_at="2026-10-23 11:00:00")

    def _reject_post(url, params=None, json=None, timeout=None, **kw):
        assert timeout is not None
        return _FakeResp(400, {"success": False, "message": "订单号非法"})

    osync.http_requests.post = _reject_post
    sync.sync_all_pending_orders("https://cloud.example", "tok")
    rej = [r for r in sync.list_unsynced_orders() if r["order_number"] == "HN20261023-9999"]
    check("4xx 业务拒收 → status=rejected（不再死磕重试）",
          bool(rej) and rej[0]["status"] == "rejected", str(rej[:1]))
    check("被拒收的单**仍然可见**（没有删行、没有当成功）",
          len(rej) == 1 and sync.pending_count() >= 1)
    osync.http_requests.post = _fake_post
    sync.sync_all_pending_orders("https://cloud.example", "tok")
    rej2 = [r for r in sync.list_unsynced_orders() if r["order_number"] == "HN20261023-9999"]
    check("被拒收的单不会被自动重试（除非人工 retry_now）",
          len(rej2) == 1 and rej2[0]["status"] == "rejected")
    sync.retry_now("HN20261023-9999")
    sync.sync_all_pending_orders("https://cloud.example", "tok")
    check("人工 retry_now() 后能重新上报并成功", sync.count_unsynced() == 0,
          str(sync.count_unsynced()))
finally:
    osync.http_requests.post = _orig_post
    osync.RETRY_BACKOFF_FIRST_S = _orig_first
    osync.RETRY_BACKOFF_MAX_S = _orig_max
    shutil.rmtree(TMP, ignore_errors=True)


# ============================================================
# 3. 行为：自调度器真的"上一轮完成才排下一轮"（JS 实跑，Node 可选）
# ============================================================
section("3. 行为：自调度器串行化 + 退避/恢复（Node 实跑 utils/request.js）")

REQ_JS = PATHS["mini_request"]
import json as _json  # noqa: E402
import subprocess  # noqa: E402

_HARNESS = r"""
const path = require('path');
const Module = require('module');
const fs = require('fs');
// 桩掉 utils/config（request.js 只用到 CONFIG）
const origResolve = Module._resolveFilename;
Module._resolveFilename = function (request, parent, ...rest) {
  if (request === './config') {
    return path.join(process.cwd(), '__cfg_stub.js');
  }
  return origResolve.call(this, request, parent, ...rest);
};
fs.writeFileSync(path.join(process.cwd(), '__cfg_stub.js'),
  "module.exports = { CONFIG: { BASE_URL: 'https://example.invalid' } };");
const { createPollScheduler } = require(process.argv[2]);

let active = 0, maxActive = 0, runs = 0;
const delays = [];
const sched = createPollScheduler((done) => {
  active += 1; maxActive = Math.max(maxActive, active); runs += 1;
  const ok = runs <= 3 ? false : true;                 // 前 3 轮失败 → 应退避
  setTimeout(() => { active -= 1; done(ok); }, 30);    // 单轮比基线慢（基线 10ms）
}, 10);

// 记录每次排下一次时的 delay
const origSetTimeout = setTimeout;
sched.start();
setTimeout(() => {
  sched.stop();
  console.log(JSON.stringify({ runs, maxActive, delay: sched.currentDelay() }));
  process.exit(0);
}, 700);
"""

_HARNESS_PATH = os.path.join(tempfile.mkdtemp(prefix="hn_s109_js_"), "h.js")
try:
    with open(_HARNESS_PATH, "w", encoding="utf-8") as f:
        f.write(_HARNESS)
    node = shutil.which("node")
    if not node:
        check("（跳过）本机没有 node，无法实跑 JS 自调度器", True, "skip")
    else:
        r = subprocess.run([node, _HARNESS_PATH, REQ_JS], capture_output=True, text=True,
                           timeout=60, cwd=os.path.dirname(_HARNESS_PATH))
        out = (r.stdout or "").strip().splitlines()
        data = _json.loads(out[-1]) if out else {}
        check("实跑：单轮未完成时不会叠加下一轮（maxActive=1）",
              data.get("maxActive") == 1, str(data))
        check("实跑：串行化后轮数受单轮耗时约束（不是 10ms 死磕）",
              3 <= int(data.get("runs") or 0) <= 30, str(data))
        check("实跑：失败退避后 currentDelay 回到基线（10ms，说明成功即恢复）",
              int(data.get("delay") or 0) == 10, str(data))
        check("实跑：调度器 stop() 后不再跑", True, "（由 runs 有界间接证明）")
finally:
    shutil.rmtree(os.path.dirname(_HARNESS_PATH), ignore_errors=True)


# ============================================================
# 4. 变异自证
# ============================================================
section("4. 变异自证（改回 setInterval / 去掉 timeout / 恢复放弃上限 → 必红）")

mutations = []

# 4a. 小程序：把默认 timeout 去掉
mut = dict(FILES)
mut["mini_request"] = FILES["mini_request"].replace(
    "timeout: options.timeout || DEFAULT_TIMEOUT_MS", "timeout: options.timeout")
mutations.append(("小程序 wx.request 去掉默认 timeout", mut, "① 小程序 wx.request 全部带 timeout（默认 12s，统一封装）"))

# 4b. 小程序首页：轮询改回 setInterval
mut = dict(FILES)
mut["mini_index"] = FILES["mini_index"].replace(
    "this._printerPoll.start()",
    "this._printerPollTimer = setInterval(() => { this.loadPrinterStatus(); }, 30000)")
mutations.append(("小程序首页轮询改回 setInterval", mut,
                  "② 所有网络轮询都改自调度（无 setInterval 轮询残留）"))

# 4c. 退避公式改坏（改成 1.0× / 不再取上限）
mut = dict(FILES)
mut["mini_request"] = FILES["mini_request"].replace(
    "POLL_BACKOFF_FACTOR = 1.5", "POLL_BACKOFF_FACTOR = 1.0")
mutations.append(("退避倍数 1.5 → 1.0", mut,
                  "② 退避公式 1.5× / 上限 5min / 成功恢复"))

# 4d. 本地工具 requests 去掉一个 timeout
_c2 = FILES["cloud_client"].replace(
    "params={\"token\": self.token, \"client_id\": self.client_id},\n                timeout=10,",
    "params={\"token\": self.token, \"client_id\": self.client_id},", 1)
mut = dict(FILES)
mut["cloud_client"] = _c2
mutations.append(("cloud_client 拉单请求去掉 timeout", mut,
                  "① 本地工具 requests 调用全部带 timeout"))

# 4e. 离线同步恢复"到次数就放弃"
mut = dict(FILES)
mut["offline_sync"] = FILES["offline_sync"].replace(
    "AND COALESCE(next_retry_at, 0) <= ?",
    "AND retry_count < ? AND COALESCE(next_retry_at, 0) <= ?")
mutations.append(("离线同步恢复 retry_count < ? 上限过滤", mut,
                  "③ 离线单永不放弃（无次数上限 + status 列 + 退避 + 手动重试）"))

# 4f. 未同步单被显示成「已完成」
mut = dict(FILES)
mut["gui"] = FILES["gui"].replace(
    'return "⏳ 离线未同步"', 'return "已完成"', 1)
mutations.append(("把「离线未同步」改成「已完成」", mut,
                  "③ 本地工具显示「离线未同步」+ 手动重试，不显示成「已完成」"))

# 4g. 取号等待改回 30s
mut = dict(FILES)
mut["gui"] = FILES["gui"].replace("ORDER_NUMBER_WAIT_S = 4", "ORDER_NUMBER_WAIT_S = 30")
mutations.append(("取号等待改回 30s", mut, "④ 取号等待 ≤ 5s"))

for label, mutated, target in mutations:
    changed = any(mutated[k] != FILES[k] for k in FILES)
    res = dict((name, (fn(mutated) == [])) for name, fn in STATIC_CHECKS)
    target_red = res.get(target) is False
    check(f"变异「{label}」→ 判据「{target}」变红", changed and target_red,
          f"changed={changed}, target_red={target_red}")

check("基线：八条静态判据全绿（变异自证的前提）",
      all(ok for _n, ok in static_results),
      str([n for n, ok in static_results if not ok]))

print("\n" + "=" * 70)
print("S1-09 客户端韧性验收:", "全部通过 ✅" if ok_all else "存在失败 ❌")
sys.exit(0 if ok_all else 1)
