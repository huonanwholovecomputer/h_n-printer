# -*- coding: utf-8 -*-
"""验收脚本 · 归属不得跨标签页串（冻结页 / 云端订单页只展示、不回写）

线上现场（2026-11-18 22:07，本机 PID 41480）：
  标签页 1/2/3 挂着本地预留单 HN20261118-0055/0056/0057（云端登记归属「霍楠」，
  备注分别 李科延 / 宁孜澎 / 李宝乐），三页都已打完（frozen=True）。
  22:02:33 与 22:03:28 两张微信云端单（HN20261118-0058/0059，下单人「闫承翰」）落地到
  新标签页 4/5，`_cloud_new_tab_should_focus()` 因为前台页已冻结而把焦点切到新页 ⇒
  共享的归属下拉被渲染成「闫承翰」。
  之后操作员依次点回标签页 1/2/3，三页**都显示「闫承翰」**（而云端与配置里都是「霍楠」）。

两个成因，本脚本各钉一条：
  ① 显示串页：`_sync_tab_settings_to_ui()` 在 `tab.frozen or cloud_locked` 分支里
     **只禁用控件、提前 return**，不刷新共享控件 ⇒ 下拉停在上一次渲染的标签页上。
  ② 落盘串页：`_update_owner_combo_items()` 无条件把兜底值（candidates[0]）写回标签页，
     不看该页是否冻结/云端订单 ⇒ 成员名单一变（含瞬时为空、顺序变化）就把已经出账的
     历史归属改写成第一个成员。

判定口径：
  · 切到冻结页/云端订单页 ⇒ 下拉显示**本页**归属；
  · 冻结页/云端订单页的 owner_name 在任何名单变化下都不被改写（只展示）；
  · 可编辑页仍保留「归属为空 → 落到第一个成员」的建单兜底（这条语义不能被顺手改掉）；
  · 云端模式断线时，本地空名单不得把 admin_names 清空（否则可编辑页归属会被清空、
    重连后又兜底成第一个成员）；纯本地部署仍严格采用本地名单。

    python other/tests/verify_owner_lock_isolation.py     # exit 0 = 全绿
"""
import io
import json
import os
import re
import shutil
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LOCAL_TOOL = os.path.join(REPO, "local_print_tool")

_SANDBOX = tempfile.mkdtemp(prefix="hn_owner_lock_")
os.environ["APPDATA"] = _SANDBOX                 # 用户数据目录 → 沙箱（别碰真实配置）
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, LOCAL_TOOL)
os.chdir(LOCAL_TOOL)

import paths  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

app = QApplication.instance() or QApplication([])

import gui as _gui  # noqa: E402
import converter as _converter  # noqa: E402

# 引擎探测会真的 Dispatch Word/WPS COM（离屏测试里既慢又有副作用）→ 一律桩成"都可用"
_converter.get_available_engines = lambda: {"word": True, "wps": True, "libreoffice": True}
_gui.QMessageBox.question = staticmethod(lambda *a, **k: _gui.QMessageBox.Yes)
_gui.QMessageBox.warning = staticmethod(lambda *a, **k: _gui.QMessageBox.Ok)
_gui.QMessageBox.information = staticmethod(lambda *a, **k: _gui.QMessageBox.Ok)
_gui.QMessageBox.critical = staticmethod(lambda *a, **k: _gui.QMessageBox.Ok)


# ── 出网拦截：任何 http_requests 调用都不发真请求 ─────────────────────────────
class _Resp:
    def __init__(self, payload):
        self._payload = payload
        self.ok = True
        self.status_code = 200
        self.text = json.dumps(payload, ensure_ascii=False)

    def json(self):
        return self._payload


class _HttpRecorder:
    def __init__(self):
        self.posts = []
        self.gets = []

    def get(self, url, params=None, timeout=None, **kw):
        self.gets.append({"url": str(url), "params": params})
        return _Resp({"success": True})

    def post(self, url, params=None, json=None, timeout=None, **kw):
        self.posts.append({"url": str(url), "json": json})
        return _Resp({"success": True})


HTTP = _HttpRecorder()
_gui.http_requests = HTTP          # 必须早于 MainWindow

from gui import MainWindow  # noqa: E402
from printer_config import PrintJob  # noqa: E402

ok_all = True


def check(name, cond, detail=""):
    global ok_all
    if not cond:
        ok_all = False
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


class _FakeClient:
    """只实现归属刷新路径读到的属性；云端当作已连接。"""

    def __init__(self, api_url="https://example.test", auth_token="tok"):
        self.api_url = api_url
        self.auth_token = auth_token
        self.token = auth_token
        self.client_id = "pc-test-owner"
        self.last_printer_state = {}
        self.take_orders = False

    def is_connected(self):
        return True

    def __getattr__(self, name):
        def _noop(*a, **k):
            return None
        return _noop


def mkjob(name, order_number="", order_id=0, task_id=0, remark="", sent=True):
    j = PrintJob(file_path=os.path.join(_SANDBOX, name), page_count=2)
    j.order_number = order_number
    j.order_id = order_id
    j.task_id = task_id
    j.remark = remark
    j.sent = sent
    return j


win = MainWindow(config_path=paths.config_path())
win.show()
app.processEvents()
win._cloud_client = _FakeClient()
win._config.admin_names = ["霍楠", "姚懿祥", "闫承翰"]

# ── 夹具：复刻 2026-11-18 22:07 的六页布局 ───────────────────────────────────
# 1/2/3：本地预留单已打完（frozen），云端登记归属「霍楠」
for _k, (_num, _remark) in {"1": ("HN20261118-0055", "李科延"),
                            "2": ("HN20261118-0056", "宁孜澎"),
                            "3": ("HN20261118-0057", "李宝乐 认定表样表+附件5 1份 双面")}.items():
    _t = win._make_new_tab()
    _t.owner_name = "霍楠"
    _t.frozen = True
    _t.jobs = [mkjob(f"local_{_k}.pdf", order_number=_num, remark=_remark)]
    win._config.tabs[_k] = _t

# 4/5：微信云端单（order_id > 0），下单人归属「闫承翰」
for _k, (_num, _oid, _tid) in {"4": ("HN20261118-0058", 1458, 501),
                               "5": ("HN20261118-0059", 1459, 502)}.items():
    _t = win._make_new_tab()
    _t.owner_name = "闫承翰"
    _t.jobs = [mkjob(f"cloud_{_k}.pdf", order_number=_num, order_id=_oid, task_id=_tid)]
    win._config.tabs[_k] = _t

win._current_tab = "1"
win._config.active_tab = "1"
win._refresh_tab_display()
app.processEvents()

combo = win._owner_combo

print("=== ① 现场复刻：云端单页 → 冻结页，归属不能停在「闫承翰」 ===")
check("夹具：标签页 1 是冻结页且非云端订单页（order_id=0）",
      win._config.tabs["1"].frozen is True and not win._is_cloud_order_tab(win._config.tabs["1"]))
check("夹具：标签页 4 是云端订单页", win._is_cloud_order_tab(win._config.tabs["4"]))
win._goto_tab("4")
app.processEvents()
check("切到云端单页 → 下拉显示「闫承翰」（先把共享控件渲染成云端归属）",
      combo.currentText() == "闫承翰", combo.currentText())

for _k in ("1", "2", "3"):
    win._goto_tab(_k)
    app.processEvents()
    _t = win._config.tabs[_k]
    check(f"★ 切到冻结页 {_k} → 下拉显示本页归属「霍楠」（不是上一页的「闫承翰」）",
          combo.currentText() == "霍楠", combo.currentText())
    check(f"冻结页 {_k} 的 owner_name 未被改写", _t.owner_name == "霍楠", _t.owner_name)
    check(f"冻结页 {_k} 的归属下拉仍是禁用的（只读展示）", combo.isEnabled() is False,
          str(combo.isEnabled()))
    check(f"冻结页 {_k} 的「管理员自行打印」仍禁用", win._admin_print_check.isEnabled() is False,
          str(win._admin_print_check.isEnabled()))

print("=== ② 冻结页归属不在成员名单里：原样展示，不被兜底成第一个成员 ===")
win._goto_tab("1")
app.processEvents()
_t1 = win._config.tabs["1"]
_t1.owner_name = "老张"          # 例如成员被删除/改名后的历史单
win._update_owner_combo_items(_t1)
check("★ 冻结页 owner_name 保持「老张」（不回写）", _t1.owner_name == "老张", _t1.owner_name)
check("下拉如实显示「老张」", combo.currentText() == "老张", combo.currentText())

print("=== ②b 非当前页的锁定页：归属为空/占位名都不许被写成第一个成员 ===")
# 这条是「回写」这条路的真正判据：下拉渲染的是 A 页、写回却落到 B 页时，
# 云端订单页/冻结页的归属会被本地成员名单污染（现场 0055/0056/0057 就是这样被改成霍楠的）。
_t5 = win._config.tabs["5"]       # 云端订单页，且**不是**当前页
_t5.owner_name = ""
win._update_owner_combo_items(_t5)
check("★ 非当前页的云端订单页归属仍为空（不被写成第一个成员）",
      _t5.owner_name == "", repr(_t5.owner_name))
_t3 = win._config.tabs["3"]       # 冻结页，且不是当前页
_t3.owner_name = "张三"           # 旧版占位名：兜底逻辑最爱改写的一种
win._update_owner_combo_items(_t3)
check("★ 非当前页的冻结页占位名归属不被改写成真实成员（只展示、不回写）",
      _t3.owner_name == "张三", repr(_t3.owner_name))
check("冻结页如实显示自己的占位名（不拿第一个成员冒充）",
      combo.currentText() == "张三", combo.currentText())
_t3.owner_name = "霍楠"
_t5.owner_name = "闫承翰"

print("=== ③ 成员名单瞬时为空：冻结页/云端页归属一律不动 ===")
_saved_admins = list(win._config.admin_names)
win._config.admin_names = []
win._update_owner_combo_items(_t1)
check("★ 名单为空时冻结页归属仍是「老张」", _t1.owner_name == "老张", _t1.owner_name)
check("下拉仍显示「老张」（不显示「(无成员)」）", combo.currentText() == "老张",
      combo.currentText())
_t4 = win._config.tabs["4"]
_t4.owner_name = "外部顾客"
win._update_owner_combo_items(_t4)
check("★ 云端订单页归属也不被改写（外部顾客不在成员名单）",
      _t4.owner_name == "外部顾客", _t4.owner_name)
check("云端订单页下拉显示「外部顾客」", combo.currentText() == "外部顾客", combo.currentText())
win._config.admin_names = _saved_admins

print("=== ④ 可编辑页仍保留建单兜底：归属为空 → 落到第一个成员 ===")
_t6 = win._make_new_tab()
_t6.owner_name = ""
win._config.tabs["6"] = _t6
win._goto_tab("6")
app.processEvents()
check("★ 可编辑页归属为空 ⇒ 自动落到第一个成员（建单归属非空，语义未被改掉）",
      _t6.owner_name == "霍楠", _t6.owner_name)
check("可编辑页归属下拉是启用的", combo.isEnabled() is True, str(combo.isEnabled()))

print("=== ⑤ 冻结页归属为空（早期标签页）：显示空白，不冒充上一页的名字 ===")
_t2 = win._config.tabs["2"]
_t2.owner_name = ""
_t4.owner_name = "闫承翰"          # 复原第 ③ 节改过的云端单页归属
win._goto_tab("4")
app.processEvents()
check("先让下拉停在「闫承翰」", combo.currentText() == "闫承翰", combo.currentText())
win._goto_tab("2")
app.processEvents()
check("★ 归属为空的冻结页显示空白（不是「闫承翰」）", combo.currentText() == "",
      repr(combo.currentText()))
check("空白显示不写回（owner_name 仍为空）", _t2.owner_name == "", repr(_t2.owner_name))

print("=== ⑥ 云端模式断线：本地空名单不得清空成员名单 ===")
_orig_load = _gui.load_local_data_file
_gui.load_local_data_file = lambda: {"config": {"members": []}}
try:
    win._cloud_client = _FakeClient()
    win._config.admin_names = ["霍楠", "姚懿祥", "闫承翰"]
    win._refresh_owner_names_from_local()
    check("★ 云端是权威来源时，本地空名单被忽略（成员名单保留）",
          win._config.admin_names == ["霍楠", "姚懿祥", "闫承翰"], str(win._config.admin_names))

    win._cloud_client = None            # 纯本地部署：本地名单就是权威
    win._config.admin_names = ["霍楠", "姚懿祥", "闫承翰"]
    win._refresh_owner_names_from_local()
    check("纯本地部署仍严格采用本地名单（空 = 真的没有成员）",
          win._config.admin_names == [], str(win._config.admin_names))
finally:
    _gui.load_local_data_file = _orig_load
    win._cloud_client = _FakeClient()
    win._config.admin_names = list(_saved_admins)

print("=== ⑦ 静态接线：判据必须落在源码里（防回退）===")
GUI = open(os.path.join(LOCAL_TOOL, "gui.py"), encoding="utf-8").read()

check("`_update_owner_combo_items` 接收 tab 参数（渲染哪个页由调用方指定）",
      re.search(r"def _update_owner_combo_items\(self,\s*tab=None\)", GUI) is not None)
check("写回只在「可编辑 + 当前页」发生",
      "if tab and self._is_tab_editable(tab) and tab is self._config.tabs.get(self._current_tab):"
      in GUI)
_sync = GUI.split("def _sync_tab_settings_to_ui", 1)[1].split("def _apply_tab_settings_values", 1)[0]
check("★ 冻结/云端分支也刷新共享控件（不再提前 return 什么都不做）",
      "_apply_tab_settings_values(tab, editable=False)" in _sync
      and re.search(r"if tab\.frozen or cloud_locked:.*?_apply_tab_settings_values\(tab, editable=False\)",
                    _sync, re.S) is not None)
check("锁定分支的刷新是无条件的（不再被 `if cloud_locked:` 包住）",
      "if cloud_locked:\n                self._apply_tab_settings_values" not in _sync)
check("`_cloud_is_member_authority` 存在且被本地名单刷新用到",
      "def _cloud_is_member_authority(self) -> bool:" in GUI
      and "self._cloud_is_member_authority()" in
      GUI.split("def _refresh_owner_names_from_local", 1)[1].split("def _cloud_is_member_authority", 1)[0])
check("云端订单页/冻结页的归属仍会进入下拉候选（不被兜底覆盖）",
      "self._is_cloud_order_tab(tab) or getattr(tab, \"frozen\", False)" in GUI)

print("=== 收尾 ===")
win.close()
app.processEvents()
shutil.rmtree(_SANDBOX, ignore_errors=True)
print("\n" + ("全部通过 ✅" if ok_all else "存在失败项 ❌"))
sys.exit(0 if ok_all else 1)
