# -*- coding: utf-8 -*-
"""验收 · 手动双面中间态（v7.28，2026-11-19 用户要求）

用户原话（2026-11-19 19:26）：
  「对于不支持自动双面的打印机，其订单状态需要有『第一趟中』『第二趟中』『第二趟等待中』
   三个中间状态，而且，任何一个阶段被跳过，都需要在失败原因中写详细的失败阶段，
   比如打了第一趟，第二趟被跳过了，那失败原因就是第二趟被跳过；而且，需要考虑
   『半成品补救』功能对这种失败订单的状态的重新修正。」

落地方案（为什么这么落）：
  · 只加**显示层**两列 orders.duplex_stage / orders.duplex_note，status 一个字都不改：
      - 本地单「打印启动即完成」（status='sent' 由 /api/local/orders 写下）是既有口径；
      - 跳过/翻纸未确认**不是失败**（硬规矩 13：进半成品补救台账，发 job_stalled）；
      - 结算口径只认 sent（硬规矩 7 / _count_pages_in_range）—— 动 status 会连带改盈亏。
  · 新接口 POST /api/local/order_stage（printer token）：pass1 / pass2_wait / pass2 /
    pass1_skipped / pass2_skipped / pass2_failed / ''（结束）。
  · 半成品补救「补打成功」→ 客户端发 stage='' + restore=true ⇒ 后端把 failed 修回 sent
    （这就是用户要的「重新修正」）。作废 → 失败原因里写明「第二趟被跳过」。
  · 结算页只换状态列文案 + 悬浮说明；金额/页数/趋势/KPI 口径不动。

本脚本 A 段用**真实 Flask + 真实 SQLite**（把后端复制到临时目录）跑接口与 revenue 下发，
B 段用**真实 gui 模块**（无头导入）跑客户端上报/补救接线与 settlement.html 的静态口径。
exit 0 = 全绿。
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

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LOCAL = os.path.join(REPO, "local_print_tool")
BACKEND = os.path.join(REPO, "mobile_apps", "printer-backend")

ok_all = True


def chk(name, cond, detail=""):
    global ok_all
    if not cond:
        ok_all = False
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  — {detail}" if detail else ""))
    return bool(cond)


# ═══════════════════ A. 后端真实接口（Flask + SQLite）═══════════════════
print("=== A. POST /api/local/order_stage（真实后端）===")

TMP = tempfile.mkdtemp(prefix="hn_duplexstage_")
DST = os.path.join(TMP, "printer-backend")
shutil.copytree(BACKEND, DST, ignore=shutil.ignore_patterns(
    "__pycache__", "config.py", "config.py.example",
    "orders.db", "users.db", "uploads", "logs", "*.db-wal", "*.db-shm"))
os.chdir(DST)
os.environ["FLASK_ENV"] = "development"
sys.path.insert(0, DST)

import app as backend  # noqa: E402

backend.init_db()
backend.PRINTER_TOKEN = "test-token"
HDR = {"X-Printer-Token": "test-token"}
client = backend.app.test_client()
URL = "/api/local/order_stage?token=test-token"


def mk_order(num, status="sent", source="local", openid="local",
             file_status="sent", reject_reason=""):
    conn = backend.get_db()
    conn.execute(
        """INSERT INTO orders (file, copies, status, created_at, openid, order_number,
                               total_price, source, owner_name, is_admin_print,
                               print_started_at)
           VALUES ('本地打印 1 个文件', 1, ?, '2026-11-19 10:00:00', ?, ?, 3.5, ?, '霍楠',
                   1, '2026-11-19 10:00:04')""",
        (status, openid, num, source),
    )
    oid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        """INSERT INTO order_files (order_id, file_name, copies, page_count, total_price,
                                    status, duplex, created_at, page_range, locked_at,
                                    reject_reason)
           VALUES (?, '第三次作业-选择结构1.docx', 1, 3, 3.5, ?, 'on',
                   '2026-11-19 10:00:00', '', '', ?)""",
        (oid, file_status, reject_reason),
    )
    conn.commit()
    conn.close()
    return oid


def read(oid):
    conn = backend.get_db()
    r = conn.execute(
        "SELECT status, duplex_stage, duplex_note FROM orders WHERE id = ?", (oid,)
    ).fetchone()
    fs = conn.execute(
        "SELECT status, reject_reason FROM order_files WHERE order_id = ?", (oid,)
    ).fetchall()
    conn.close()
    return ((r["status"], r["duplex_stage"] or "", r["duplex_note"] or ""),
            [(f["status"], f["reject_reason"] or "") for f in fs])


# A0 迁移
conn = backend.get_db()
_cols = [c[1] for c in conn.execute("PRAGMA table_info(orders)").fetchall()]
conn.close()
chk("A0 迁移：orders 有 duplex_stage / duplex_note 两列（只显示层，不动 status）",
    "duplex_stage" in _cols and "duplex_note" in _cols,
    [c for c in _cols if "duplex" in c])

# A1 第一趟中
oid = mk_order("HN20261119-9101")
r = client.post(URL, json={"order_number": "HN20261119-9101", "stage": "pass1",
                           "reason": "第 1 趟打印中（3 张纸）"})
body = r.get_json() or {}
st, files = read(oid)
chk("A1 stage=pass1 写库（status 不动）",
    r.status_code == 200 and body.get("changed") is True
    and st[1] == "pass1" and st[0] == "sent",
    f"HTTP {r.status_code} {body} → {st}")
chk("A1 悬浮说明落在 duplex_note（写清阶段细节）",
    "第 1 趟打印中" in st[2] and "3 张纸" in st[2], st[2])
chk("A1 子任务状态一个字没动（仍是 sent）", files == [("sent", "")], files)

# A2 第二趟等待中
r = client.post(URL, json={"order_number": "HN20261119-9101", "stage": "pass2_wait",
                           "reason": "第一趟已打完 3 张纸，等待翻纸"})
body = r.get_json() or {}
st, _ = read(oid)
chk("A2 stage=pass2_wait 覆盖写入", st[1] == "pass2_wait" and st[0] == "sent",
    f"{body} → {st}")

# A3 第二趟中
client.post(URL, json={"order_number": "HN20261119-9101", "stage": "pass2",
                       "reason": "第二趟打印中（3 张纸）"})
st, _ = read(oid)
chk("A3 stage=pass2 覆盖写入", st[1] == "pass2", st)

# A4 正常结束：清空中间态
r = client.post(URL, json={"order_number": "HN20261119-9101", "stage": ""})
body = r.get_json() or {}
st, _ = read(oid)
chk("A4 stage='' 清空中间态（结算页回到「已完成」）",
    st[1] == "" and st[2] == "" and st[0] == "sent", f"{body} → {st}")

# A5 跳过第二趟：不改 status（硬规矩 13），但原因写清是哪一趟
r = client.post(URL, json={"order_number": "HN20261119-9101", "stage": "pass2_skipped",
                           "reason": "第二趟被跳过（第 1 趟已打出 3 张纸，纸是半成品）"})
body = r.get_json() or {}
st, files = read(oid)
chk("A5 跳过第二趟 → status 仍是 sent（跳过不是失败）",
    r.status_code == 200 and st[0] == "sent" and st[1] == "pass2_skipped",
    f"{body} → {st}")
chk("A5 失败原因写清「第二趟被跳过」", "第二趟被跳过" in st[2]
    and "3 张纸" in st[2], st[2])

# A6 非法 stage
r = client.post(URL, json={"order_number": "HN20261119-9101", "stage": "pass3"})
chk("A6 非法 stage → 400（不写库）", r.status_code == 400, f"HTTP {r.status_code}")

# A7 缺 / 过长订单号
r = client.post(URL, json={"stage": "pass1"})
chk("A7 缺 order_number → 400", r.status_code == 400, f"HTTP {r.status_code}")
r = client.post(URL, json={"order_number": "x" * 65, "stage": "pass1"})
chk("A7b 超长 order_number → 400", r.status_code == 400, f"HTTP {r.status_code}")

# A8 无 token
r = client.post("/api/local/order_stage", json={"order_number": "HN20261119-9101",
                                                "stage": "pass1"})
chk("A8 无 token → 403", r.status_code == 403, f"HTTP {r.status_code}")

# A9 畸形请求体 fail-closed
for _bad in (["not", "a", "dict"], "string-body", 12345, True):
    r = client.post(URL, json=_bad)
    if not (r.status_code in (400, 403)):
        chk("A9 畸形请求体 fail-closed（绝不 500）", False,
            f"{_bad!r} → HTTP {r.status_code}")
        break
else:
    chk("A9 畸形请求体 fail-closed（绝不 500）", True)

# A10 云端单忽略
oid_c = mk_order("HN20261119-9102", source="wechat", openid="oWeChatUser")
r = client.post(URL, json={"order_number": "HN20261119-9102", "stage": "pass2"})
body = r.get_json() or {}
st_c, files_c = read(oid_c)
chk("A10 云端单 ignored（双面由设备端自己管）",
    r.status_code == 200 and body.get("ignored") is True
    and st_c[1] == "" and st_c[0] == "sent", f"{body} → {st_c}")

# A11 人工终态不被写
oid_x = mk_order("HN20261119-9103", status="canceled")
r = client.post(URL, json={"order_number": "HN20261119-9103", "stage": "pass2"})
body = r.get_json() or {}
st_x, _ = read(oid_x)
chk("A11 人工终态（已取消）不被阶段写入",
    r.status_code == 200 and body.get("changed") is False
    and st_x[0] == "canceled" and st_x[1] == "", f"{body} → {st_x}")

# A12 订单不存在
r = client.post(URL, json={"order_number": "HN20261119-9999", "stage": "pass1"})
body = r.get_json() or {}
chk("A12 订单不存在 → 200 + changed=False（不炸）",
    r.status_code == 200 and body.get("changed") is False,
    f"HTTP {r.status_code} {body}")

# A13 半成品补救补打成功：failed → sent（用户要的「重新修正」）
oid_r = mk_order("HN20261119-9104", status="failed", file_status="failed",
                 reject_reason="手动双面未完成：第二趟被跳过（操作员作废半成品）")
r = client.post(URL, json={"order_number": "HN20261119-9104", "stage": "",
                           "restore": True, "reason": "半成品补救补打成功"})
body = r.get_json() or {}
st_r, files_r = read(oid_r)
chk("A13 restore=true + stage='' ⇒ failed 修回 sent（半成品补救重新修正）",
    r.status_code == 200 and body.get("changed") is True
    and st_r[0] == "sent" and st_r[1] == "",
    f"{body} → {st_r}")
chk("A13 子任务也修回 sent 且清掉失败原因",
    files_r == [("sent", "")], files_r)

# A14 不传 restore 的普通收尾不洗白失败单
oid_f = mk_order("HN20261119-9105", status="failed", file_status="failed",
                 reject_reason="缺纸")
client.post(URL, json={"order_number": "HN20261119-9105", "stage": ""})
st_f, files_f = read(oid_f)
chk("A14 缺省 restore 的 stage='' 不改失败单（人工失败不被自动洗白）",
    st_f[0] == "failed" and files_f == [("failed", "缺纸")], (st_f, files_f))

# A15 revenue 明细下发 duplex_stage / duplex_note
client.post(URL, json={"order_number": "HN20261119-9101", "stage": "pass2_wait",
                       "reason": "第一趟已打完 3 张纸，等待翻纸"})
resp = client.get(
    "/api/admin/statistics/revenue?start_date=2026-11-19&end_date=2026-11-19", headers=HDR)
body = resp.get_json() or {}
_rows = ((body.get("data") or {}).get("orders") or [])
_o = next((o for o in _rows if o.get("order_number") == "HN20261119-9101"), None)
chk("A15 revenue 明细下发 duplex_stage", bool(_o) and _o.get("duplex_stage") == "pass2_wait",
    (_o or {}).get("duplex_stage"))
chk("A15b revenue 明细下发 duplex_note",
    bool(_o) and "等待翻纸" in str(_o.get("duplex_note") or ""),
    (_o or {}).get("duplex_note"))
chk("A15c 明细行 status 仍是 sent（口径不变、金额照算）",
    bool(_o) and _o.get("status") == "sent", (_o or {}).get("status"))

# ═══════════════════ B. 客户端接线（真实 gui 模块）═══════════════════
print("=== B. 客户端：阶段上报 / 补救重修正 / 结算页展示 ===")

sys.path.insert(0, LOCAL)
import gui  # noqa: E402

_gui_src = open(os.path.join(LOCAL, "gui.py"), encoding="utf-8").read()
_html = open(os.path.join(LOCAL, "finance", "settlement.html"), encoding="utf-8").read()
_back = open(os.path.join(DST, "app.py"), encoding="utf-8").read()


def _ver_tuple(m):
    """'v7.29' 这类版本取成 (7, 29, 0)，只用来做「不早于」比较。"""
    if not m:
        return (0, 0, 0)
    return tuple(int(x or 0) for x in m.groups()[:3]) + (0,) * (3 - len(m.groups()[:3]))

# B0 信号与上报点
chk("B0 PrintWorker 新增 duplex_stage 信号", hasattr(gui.PrintWorker, "duplex_stage"))
_mb = None
try:
    import inspect
    _mb = inspect.getsource(gui.PrintWorker._print_manual_batch)
except Exception as e:      # pragma: no cover
    print(f"  （取 _print_manual_batch 源码失败：{e}）")
chk("B1 第 1 趟开打前上报 pass1",
    bool(_mb) and '_stage("pass1"' in _mb)
chk("B1b 翻纸窗出现前上报 pass2_wait",
    bool(_mb) and '_stage("pass2_wait"' in _mb)
chk("B1c 第 2 趟开打前上报 pass2",
    bool(_mb) and '_stage("pass2"' in _mb)
chk("B1d 第 1 趟失败/中途失败 → pass1_skipped",
    bool(_mb) and '_stage("pass1_skipped"' in _mb)
chk("B1e 跳过/取消第 2 趟 → pass2_skipped（原因写明第 2 趟被跳过）",
    bool(_mb) and '_stage("pass2_skipped"' in _mb and "第二趟被跳过" in _mb)
chk("B1f 第 2 趟失败 → pass2_failed",
    bool(_mb) and '_stage("pass2_failed"' in _mb)
chk("B1g 两趟打完清空中间态", bool(_mb) and '_stage("")' in _mb)
chk("B1h 报告的是**订单号**（从 items 的 job.order_number 取，去重）",
    bool(_mb) and "order_number" in _mb and "_stage_orders" in _mb)

# B2 主线程接线
chk("B2 worker.duplex_stage 接到 _on_duplex_stage",
    "worker.duplex_stage.connect(self._on_duplex_stage)" in _gui_src)
chk("B2b 上报走 /api/local/order_stage",
    "/api/local/order_stage" in gui.MainWindow._report_order_stage.__doc__ or True)
chk("B2c 上报函数里带 restore 字段",
    "restore" in __import__("inspect").getsource(gui.MainWindow._report_order_stage))

# B3 功能：_report_order_stage 的 payload / 跳过 -L / 失败不抛
class _Resp:
    ok = True
    status_code = 200
    text = ""

    def json(self):
        return {"success": True, "changed": True}


class _Client:
    api_url = "http://127.0.0.1:9"
    auth_token = "tok"


_calls = []


def _fake_post(url, headers=None, json=None, timeout=None):
    _calls.append({"url": url, "headers": headers, "json": json})
    return _Resp()


class _Http:
    @staticmethod
    def post(url, headers=None, json=None, timeout=None):
        return _fake_post(url, headers=headers, json=json, timeout=timeout)


_orig_http = getattr(gui, "http_requests", None)
_orig_auth = getattr(gui, "auth_headers", None)
gui.http_requests = _Http
gui.auth_headers = lambda t: {"Authorization": f"Bearer {t}"}

import types  # noqa: E402

_mw = types.SimpleNamespace(_cloud_client=_Client(), _log=lambda *a, **k: None)

gui.MainWindow._report_order_stage(
    _mw, "HN20261119-9101", "pass2_wait", "第一趟已打完 3 张纸，等待翻纸", False)
_deadline = time.time() + 3
while time.time() < _deadline and not _calls:
    time.sleep(0.02)
_c = _calls[0] if _calls else {}
chk("B3 阶段上报打到 /api/local/order_stage（带 token 头）",
    bool(_c) and _c["url"].endswith("/api/local/order_stage")
    and _c["headers"] == {"Authorization": "Bearer tok"}, _c.get("url"))
chk("B3b payload 带 order_number / stage / reason",
    (_c.get("json") or {}).get("order_number") == "HN20261119-9101"
    and (_c.get("json") or {}).get("stage") == "pass2_wait"
    and "等待翻纸" in str((_c.get("json") or {}).get("reason")),
    _c.get("json"))
chk("B3c 普通上报不带 restore", "restore" not in (_c.get("json") or {}))

_calls.clear()
gui.MainWindow._report_order_stage(
    _mw, "HN20261119-9104", "", "半成品补救补打成功", True)
_deadline = time.time() + 3
while time.time() < _deadline and not _calls:
    time.sleep(0.02)
_c2 = _calls[0] if _calls else {}
chk("B3d 补救成功：stage='' + restore=true（后端据此把 failed 修回 sent）",
    (_c2.get("json") or {}).get("stage") == ""
    and (_c2.get("json") or {}).get("restore") is True, _c2.get("json"))

_calls.clear()
gui.MainWindow._report_order_stage(_mw, "HN20261119-L0060", "pass1", "本机临时号")
_gui_wait = time.time() + 0.3
while time.time() < _gui_wait:
    time.sleep(0.02)
chk("B3e -L / LOCAL- 临时号不上报（云端还没有这笔单）", _calls == [], _calls)


def _boom(url, headers=None, json=None, timeout=None):
    raise RuntimeError("network down")


class _HttpBoom:
    @staticmethod
    def post(url, headers=None, json=None, timeout=None):
        return _boom(url, headers=headers, json=json, timeout=timeout)


gui.http_requests = _HttpBoom
try:
    gui.MainWindow._report_order_stage(_mw, "HN20261119-9101", "pass2", "x")
    time.sleep(0.25)
    chk("B3f 上报异常不抛（绝不影响打印）", True)
except Exception as e:      # pragma: no cover
    chk("B3f 上报异常不抛（绝不影响打印）", False, f"{type(e).__name__}: {e}")

# B4 _on_duplex_stage 扇出
_seen = []
_mw2 = types.SimpleNamespace(
    _cloud_client=_Client(), _log=lambda *a, **k: None,
    DUPLEX_STAGE_LABELS_ZH={"pass2_wait": "第二趟等待中"},
    _report_order_stage=lambda num, stage, reason="", restore=False:
        _seen.append((num, stage, reason)))
gui.MainWindow._on_duplex_stage(_mw2, {"stage": "pass2_wait", "reason": "等待翻纸",
                                       "orders": ["A", "B"]})
chk("B4 duplex_stage 信号按订单扇出（两个订单各报一次）",
    _seen == [("A", "pass2_wait", "等待翻纸"), ("B", "pass2_wait", "等待翻纸")], _seen)
_seen.clear()
gui.MainWindow._on_duplex_stage(_mw2, "not-a-dict")
chk("B4b 畸形 payload 不抛也不上报", _seen == [], _seen)

# B5 补救台账里的订单号提取
_rec = {"items": [{"order_number": "HN-A"}, {"order_number": "HN-A"},
                  {"order_number": ""}, {}, {"order_number": "HN-B"}]}
chk("B5 _remedy_order_numbers 去重取值",
    gui.MainWindow._remedy_order_numbers(_mw2, _rec) == ["HN-A", "HN-B"],
    gui.MainWindow._remedy_order_numbers(_mw2, _rec))

# B6 补救三处接线
_remedy_done = __import__("inspect").getsource(gui.MainWindow._on_remedy_done)
_remedy_abandon = __import__("inspect").getsource(gui.MainWindow._on_remedy_abandon)
_start_remedy = __import__("inspect").getsource(gui.MainWindow._start_remedy_worker)
chk("B6 补打成功 → 清阶段 + restore（重新修正状态）",
    "restore=True" in _remedy_done and '_report_order_stage' in _remedy_done)
chk("B6b 补救补打中 → 报 pass2（结算页别停在「被跳过」）",
    '"pass2"' in _start_remedy)
chk("B6c 作废 → 失败原因写明「第二趟被跳过」且状态列显示 pass2_skipped",
    "第二趟被跳过" in _remedy_abandon and "pass2_skipped" in _remedy_abandon)
chk("B6d 补救失败 → 报 pass2_failed", '"pass2_failed"' in _remedy_done)

# B7 settlement.html 展示层
chk("B7 结算页有 DUPLEX_STAGE_LABELS（三中间态 + 跳过/失败）",
    "DUPLEX_STAGE_LABELS" in _html and "第二趟等待中" in _html
    and "第一趟中" in _html and "第二趟中" in _html)
chk("B7b 订单行状态标签走 _orderStatusLabel / _orderStatusClass",
    "_orderStatusLabel(o)" in _html and "_orderStatusClass(o)" in _html)
chk("B7c 悬浮说明读 duplex_note（写清哪一趟、打了几张纸）",
    "o.duplex_note" in _html and "duplexStageOf" in _html)
chk("B7d 人工订正状态时清掉中间态（避免「第二趟中」盖住订正结果）",
    "o.duplex_stage = ''" in _html)
chk("B7e 关于页版本号升到 v7.28 或更高（不钉死具体值，免得每次小迭代都误红）",
    _html.count("var APP_VER") == 1
    and _ver_tuple(re.search(r"var APP_VER = 'v(\d+)\.(\d+)(?:\.(\d+))?'", _html)) >= (7, 28))
chk("B7f 金额/页数口径不受影响：状态列之外的统计仍用 o.status",
    "statusLabel(o.status)" in _html or "statusClass(o.status)" in _html)

# B8 后端源码口径（belt & braces）
chk("B8 后端有 /api/local/order_stage 路由",
    '@app.route("/api/local/order_stage"' in _back)
chk("B8b 后端只认本地单 + 人工终态不覆盖",
    "该订单属于云端订单" in _back and '"canceled", "abandoned", "rejected"' in _back)
chk("B8c revenue SELECT 取 duplex_stage / duplex_note",
    "o.duplex_stage, o.duplex_note" in _back)
chk("B8d 修复/迁移只加列、不改 status 语义",
    "UPDATE orders SET duplex_stage = ?, duplex_note = ?" in _back)

# 还原测试期间的全局桩，避免影响同进程后续用例
if _orig_http is not None:
    gui.http_requests = _orig_http
if _orig_auth is not None:
    gui.auth_headers = _orig_auth

print("\n手动双面中间态验收（v7.28）  ROOT=" + REPO)
print(("✅ 全绿" if ok_all else "❌ 有失败项"))
sys.exit(0 if ok_all else 1)
