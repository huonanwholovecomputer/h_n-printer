# -*- coding: utf-8 -*-
"""验收脚本 · 「复制」与「复制计费明细」的云端预留同步必须同口径

线上反馈（2026-10）：本地打印工具里点「📋 复制」（合计）会把「预留订单」同步到云端
（文件明细 + 价格 + 备注 + 附加服务），但点「📋 复制计费明细」（Ctrl+Shift+C）只复制文本、
**不**同步 → 云端订单还是旧数据/空占位：文件列表和价格都没更新。

根因：`_on_copy_total` 里有 `self._upload_reserved_order(order_number)`，`_on_copy_detail` 没有。

    python tests/verify_copy_reserved_sync.py     # exit 0 = 全绿
"""
import io
import json
import os
import shutil
import sys
import tempfile
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL_TOOL = os.path.join(REPO, "local_print_tool")

_SANDBOX = tempfile.mkdtemp(prefix="hn_copy_reserved_")
os.environ["APPDATA"] = _SANDBOX                 # 用户数据目录 → 沙箱
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, LOCAL_TOOL)
os.chdir(LOCAL_TOOL)

import paths          # noqa: E402
from printer_config import calc_cost  # noqa: E402

ok_all = True


def check(name, cond, detail=""):
    global ok_all
    if not cond:
        ok_all = False
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


# ── 造一份 5 页 PDF（双面 5 页 = 2张×0.40 + 1张×0.30 = ¥1.10）──
from reportlab.lib.pagesizes import A4  # noqa: E402
from reportlab.pdfgen import canvas  # noqa: E402

PDF = os.path.join(_SANDBOX, "报告.pdf")
_c = canvas.Canvas(PDF, pagesize=A4)
for _ in range(5):
    _c.drawString(72, 720, "hello copy reserved")
    _c.showPage()
_c.save()

from PySide6.QtWidgets import QApplication  # noqa: E402

app = QApplication.instance() or QApplication([])

import gui as _gui  # noqa: E402


# 关闭窗口时若有未打印任务会弹确认框（offscreen 下无人可答会死等）→ 自动回「是」
_gui.QMessageBox.question = staticmethod(lambda *a, **k: _gui.QMessageBox.Yes)
_gui.QMessageBox.warning = staticmethod(lambda *a, **k: _gui.QMessageBox.Ok)
_gui.QMessageBox.information = staticmethod(lambda *a, **k: _gui.QMessageBox.Ok)


# ── 出网拦截：所有 http_requests 调用都落到记录器（不发真请求）──
class _Resp:
    def __init__(self, payload, ok=True):
        self._payload = payload
        self.ok = ok
        self.text = json.dumps(payload, ensure_ascii=False)

    def json(self):
        return self._payload


class _HttpRecorder:
    """假 http_requests：GET 取号返回固定订单号，POST 全记录。"""

    def __init__(self):
        self.posts = []
        self.gets = []
        self.next_order_number = "HN20261008-9001"

    def get(self, url, params=None, timeout=None, **kw):
        self.gets.append({"url": url, "params": params})
        if str(url).endswith("/api/next_order_number"):
            return _Resp({"success": True, "order_number": self.next_order_number})
        return _Resp({"success": True})

    def post(self, url, params=None, json=None, timeout=None, **kw):
        self.posts.append({"url": url, "params": params, "json": json})
        return _Resp({"success": True})


HTTP = _HttpRecorder()
_gui.http_requests = HTTP          # 必须早于 MainWindow（8s 后的 _sync_pricing 也走它）

from gui import MainWindow  # noqa: E402

cfg_path = paths.config_path()
win = MainWindow(config_path=cfg_path)
win.show()

# 成员（无成员时两个复制处理器都会提前返回）
win._config.admin_names = ["乙成员"]
_tab = win._config.tabs.get(win._current_tab)
if _tab is not None:
    _tab.owner_name = "乙成员"


class _FakeClient:
    """只实现复制/退出路径用到的接口（is_connected / api_url / token / client_id）。
    其它方法（如 closeEvent 里的 abandon_reserved_order）一律吞掉并记录，避免误发请求。"""

    def __init__(self, connected=True):
        self.api_url = "https://example.test"
        self.token = "tok"
        self.client_id = "pc-test"
        self._connected = connected
        self.calls = []
        # 退出/建标签页路径会读的属性（不能落进 __getattr__ 的 no-op 分支）
        self.last_printer_state = {}
        self.take_orders = False

    def is_connected(self):
        return self._connected

    def __getattr__(self, name):
        def _noop(*a, **k):
            self.calls.append((name, a))
        return _noop


FAKE = _FakeClient(connected=True)
win._cloud_client = FAKE


def wait_posts(count, timeout=6.0):
    deadline = time.time() + timeout
    while len(HTTP.posts) < count and time.time() < deadline:
        app.processEvents()
        time.sleep(0.02)
    app.processEvents()
    return len(HTTP.posts) >= count


def reserved_posts():
    return [p for p in HTTP.posts if str(p["url"]).endswith("/api/local_orders")]


print("=== 1. 加文件（5 页 PDF）===")
win._add_files_to_table([PDF])
jobs = win._get_current_jobs()
check("文件已加入当前标签页", len(jobs) == 1, f"n={len(jobs)}")
check("页数已解析（5 页）", bool(jobs) and jobs[0].page_count == 5,
      f"page_count={jobs[0].page_count if jobs else 'n/a'}")

base1 = sum(calc_cost(j.page_count, j.copies, j.duplex, win._config.simplex_price,
                      win._config.duplex_price, j.page_range)[0] for j in jobs)
expect1 = round(base1 + (_tab.calc_extra_total(base1, win._config) if _tab else 0.0), 2)
print(f"    文件费={base1:.2f} 应付合计={expect1:.2f} 单双面={jobs[0].duplex if jobs else '?'}")

print("=== 2. 「复制」（合计）→ 云端预留同步 ===")
HTTP.posts.clear()
win._on_copy_total()
check("复制后已向后端上报", wait_posts(1), f"posts={len(HTTP.posts)}")
posts = reserved_posts()
check("上报到 /api/local_orders", len(posts) == 1, json.dumps([p['url'] for p in HTTP.posts]))
if posts:
    body = posts[0]["json"]
    check("带打印机 token", (posts[0]["params"] or {}).get("token") == "tok")
    check("状态为 reserved", body.get("status") == "reserved", str(body.get("status")))
    check("订单号来自云端取号", body.get("order_number") == "HN20261008-9001",
          str(body.get("order_number")))
    check("携带文件明细（1 个文件）", len(body.get("files") or []) == 1,
          json.dumps(body.get("files"), ensure_ascii=False))
    check(f"价格与界面合计一致（¥{expect1:.2f}）",
          abs(float(body.get("total_price", -1)) - expect1) < 1e-6,
          f"total_price={body.get('total_price')} expect={expect1}")
    check("携带归属成员", body.get("owner_name") == "乙成员", str(body.get("owner_name")))

print("=== 3. 「复制计费明细」→ 也必须同步（本次修复点）===")
# 改一份数（1 → 2 份）模拟"文件列表/价格变了但没再点复制"的场景
jobs[0].copies = 2
win._set_current_jobs(jobs)
win._update_total_cost()
app.processEvents()
base2 = sum(calc_cost(j.page_count, j.copies, j.duplex, win._config.simplex_price,
                      win._config.duplex_price, j.page_range)[0] for j in jobs)
expect2 = round(base2 + (_tab.calc_extra_total(base2, win._config) if _tab else 0.0), 2)

HTTP.posts.clear()
_clip_before = win._copy_detail_btn.text() if win._copy_detail_btn else ""
win._on_copy_detail()
check("复制计费明细后已向后端上报（修复前为 0）", wait_posts(1), f"posts={len(HTTP.posts)}")
posts = reserved_posts()
check("上报到 /api/local_orders", len(posts) == 1, json.dumps([p['url'] for p in HTTP.posts]))
if posts:
    body = posts[0]["json"]
    check("状态仍为 reserved", body.get("status") == "reserved", str(body.get("status")))
    check("文件列表已更新（2 份）",
          (body.get("files") or [{}])[0].get("copies") == 2,
          json.dumps(body.get("files"), ensure_ascii=False))
    check(f"价格已更新为 ¥{expect2:.2f}",
          abs(float(body.get("total_price", -1)) - expect2) < 1e-6,
          f"total_price={body.get('total_price')} expect={expect2}")
else:
    check("文件列表已更新（2 份）", False, "无上报")
    check("价格已更新", False, "无上报")

clip = QApplication.clipboard().text() or ""
check("计费明细仍写入剪贴板", "计费明细" in clip and "报告.pdf" in clip,
      clip.replace("\n", " | ")[:160])
check("明细含订单号", "HN20261008-9001" in clip, clip.replace("\n", " | ")[:80])
# ③b 合计行格式（2026-10-21 用户要求）：合计行上方一条与抬头同款的分隔线 + 💰 前缀，
#     与小程序 / APP 的「💰合计」同口径。改回去立刻转红。
_lines = clip.split("\n")
_tot = next((i for i, ln in enumerate(_lines) if ln.startswith("💰合计")), -1)
check("合计行带 💰 前缀（💰合计: ¥x.xx）", _tot >= 0 and _lines[_tot].startswith("💰合计: ¥"),
      _lines[_tot] if _tot >= 0 else clip.replace("\n", " | ")[:120])
check("合计行上方紧贴 14 个半角 - 的分隔线",
      _tot >= 1 and _lines[_tot - 1] == "-" * 14,
      repr(_lines[_tot - 1]) if _tot >= 1 else "(合计行缺失)")
check("抬头的分隔线仍是同款（未把分隔线换成别的字符）",
      _lines[1:2] == ["-" * 14] and _lines[3:4] == ["-" * 14],
      " | ".join(repr(x) for x in _lines[:5]))
check("按钮切换为已复制", bool(win._copy_detail_btn) and "已复制" in win._copy_detail_btn.text(),
      win._copy_detail_btn.text() if win._copy_detail_btn else "")
print(f"    (复制前按钮文本={_clip_before!r})")

print("=== 4. 离线（未连接云端）→ 不上报，但明细仍可复制 ===")
FAKE._connected = False
# 模拟"离线新建订单"：清掉已分配订单号，让 _ensure_order_number 走本地 -L 分支
_jobs = win._get_current_jobs()
for _j in _jobs:
    _j.order_number = ""
win._set_current_jobs(_jobs)
HTTP.posts.clear()
win._on_copy_detail()
deadline = time.time() + 1.0
while time.time() < deadline:
    app.processEvents()
    time.sleep(0.02)
check("离线时不发起上报", len(HTTP.posts) == 0, json.dumps([p["url"] for p in HTTP.posts]))
check("离线时仍生成本地临时订单号（-L）", "-L" in (win._get_current_jobs()[0].order_number or ""),
      win._get_current_jobs()[0].order_number)
clip = QApplication.clipboard().text() or ""
check("离线明细仍写入剪贴板", "计费明细" in clip and "-L" in clip,
      clip.replace("\n", " | ")[:160])

print("=== 4b. 云端订单（带 task_id）→ 绝不能上报 /api/local_orders（会把来源改成「本地」）===")
# 现场（2026-10-18）：小程序订单在本地工具里打印完，云端那行的 source 被改成 'local'，
# 结算页来源徽标显示「本地」、订单构成/按来源对账全部错位。
# 链路：云端任务的 job 带着**云端订单号**（cloud_client 里 order_number=task.order_number）
#   → `_acquire_order_number_and_upload` 复用该号 → POST /api/local_orders（status=sent）
#   → 后端 `local_orders()` 按 order_number 命中那行云端单 → UPDATE ... source='local'。
FAKE._connected = True
_cl_jobs = win._get_current_jobs()
for _j in _cl_jobs:
    _j.order_number = "HN20261018-0023"     # 云端订单号（后端那行 openid=微信/source=wechat）
    _j.task_id = 98765                      # = 云端子任务，说明这是云端订单
    _j.order_id = 4321
win._set_current_jobs(_cl_jobs)
HTTP.posts.clear()
_num, _uploaded = win._acquire_order_number_and_upload(
    _cl_jobs,
    files_data=[{"file_name": "离散数学作业2.pdf", "copies": 1, "page_count": 1,
                 "cost": 0.3, "duplex": "off", "page_range": ""}],
    total_price=0.3, extra_fields={}, tab=win._config.tabs.get(win._current_tab),
    created_at="2026-10-18 22:00:00", online=True)
check("★ 云端订单不再上报 /api/local_orders（这是「变成本地订单」的根因）",
      len(reserved_posts()) == 0, json.dumps([p["url"] for p in HTTP.posts]))
check("★ 订单号保持云端号不变（不换号、不丢弃）", _num == "HN20261018-0023", _num)

# 反向：本地订单（自己开的号、无 task_id）必须**照旧**上报，否则「复制价格→预留」断掉
HTTP.posts.clear()
_lc_jobs = win._get_current_jobs()
for _j in _lc_jobs:
    _j.order_number = "HN20261018-0024"
    _j.task_id = 0
    _j.order_id = 0
win._set_current_jobs(_lc_jobs)
_num2, _ = win._acquire_order_number_and_upload(
    _lc_jobs,
    files_data=[{"file_name": "本地单.pdf", "copies": 1, "page_count": 1,
                 "cost": 0.3, "duplex": "off", "page_range": ""}],
    total_price=0.3, extra_fields={}, tab=win._config.tabs.get(win._current_tab),
    created_at="2026-10-18 22:00:00", online=True)
check("★ 本地订单照旧上报 /api/local_orders（零回归）",
      len(reserved_posts()) == 1, json.dumps([p["url"] for p in HTTP.posts]))
if reserved_posts():
    _b = reserved_posts()[0]["json"] or {}
    check("本地单上报用本地号 + status=sent",
          _b.get("order_number") == "HN20261018-0024" and _b.get("status") == "sent",
          f"{_b.get('order_number')}/{_b.get('status')}")

print("=== 5. 收尾 ===")
win._on_copy_total  # noqa: B018  仅引用，避免未使用告警
win.close()
app.processEvents()
shutil.rmtree(_SANDBOX, ignore_errors=True)
print("\n" + ("全部通过 ✅" if ok_all else "存在失败项 ❌"))
sys.exit(0 if ok_all else 1)
