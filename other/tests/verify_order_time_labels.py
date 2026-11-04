# -*- coding: utf-8 -*-
"""验收脚本 · 订单「创建时间 / 打印发起时间」（task-4 #7，GUI 集成，offscreen）

需求（用户 2026-11）：
1. 标签页工具条右侧显示两个**只读**时间：「订单创建：YYYY-MM-DD HH:MM:SS」与
   「打印发起：…」；没有值时显示「—」；窄窗口时允许退化（省略秒 → 只留日期）但不能出界。
2. 创建时间 = 本标签页订单**第一次生成订单号**的时刻，存 `TabSettings.created_at`，
   持久化、重启后仍在；**一经写入不可变** —— 复制合计、换号补报、离线补报、重打都不得改写。
3. 打印发起时间 = 第一次真正开始打印的时刻，存 `TabSettings.started_at`，同样不可覆盖。
4. 修掉旧 bug：预留上报（复制合计）曾拿 `PrintJob.finished_at` 当 created_at，
   导致同一张订单的时间随每次上报漂移。

    python tests/verify_order_time_labels.py     # exit 0 = 全绿
"""
import io
import os
import re
import sys
import tempfile
import time
import types

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LOCAL_TOOL = os.path.join(REPO, "local_print_tool")

_SANDBOX = tempfile.mkdtemp(prefix="hn_ordertime_")
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

from gui import MainWindow  # noqa: E402
from printer_config import PrintJob, TabSettings  # noqa: E402

SRC = io.open(os.path.join(LOCAL_TOOL, "gui.py"), encoding="utf-8").read()
ok_all = True


def check(name, cond, extra=""):
    global ok_all
    if not cond:
        ok_all = False
        print(f"❌ {name}" + (f"  [{extra}]" if extra else ""))
    else:
        print(f"✅ {name}")


# ══════════════════════════════════════════════════════════════════════════
print("\n=== ① 纯函数：时间文案的三档退化 ===")
check("无值 ⇒ 单个「前缀 + —」，不伪造时间",
      _gui._order_time_variants("订单创建：", "") == ["订单创建： —"],
      str(_gui._order_time_variants("订单创建：", "")))
_v = _gui._order_time_variants("订单创建：", "2026-11-03 14:25:30")
check("有值 ⇒ 完整 / 省略秒 / 仅日期 三档",
      _v == ["订单创建： 2026-11-03 14:25:30", "订单创建： 2026-11-03 14:25",
             "订单创建： 2026-11-03"], str(_v))
check("_now_stamp() 是 19 字符的本地时间戳",
      re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", _gui._now_stamp()) is not None,
      _gui._now_stamp())

print("\n=== ② 控件：窄了一点就退化，不会撑破布局 ===")
_lbl = _gui._TimeVariantsLabel()
_lbl.set_variants(_v)
check("set_variants 后默认显示完整值", _lbl.full_text() == _v[0], _lbl.full_text())
_lbl.resize(60, 18)
_after = _lbl.text()
check("压到 60px 后显示的是更短的档（或省略号），且不超长",
      len(_after) <= len(_v[0]) and _after.startswith("订单创建："), _after)
_lbl.deleteLater()

# ══════════════════════════════════════════════════════════════════════════
print("\n=== ③ 真窗口：两个标签显示「—」，写入后显示时间 ===")
win = MainWindow(config_path=paths.config_path())
tab_key = win._current_tab
tab = win._config.tabs[tab_key]
tab.jobs = [PrintJob(file_path=r"C:\tmp\a.pdf", page_count=2)]
win._set_current_jobs(tab.jobs)
win._refresh_tab_display()

check("标签页工具条上有「订单创建」「打印发起」两个只读标签",
      hasattr(win, "_tab_created_label") and hasattr(win, "_tab_started_label"))
check("初始没有值 ⇒ 两个标签都显示「—」（不显示假时间）",
      win._tab_created_label.full_text().endswith("—")
      and win._tab_started_label.full_text().endswith("—"),
      f"{win._tab_created_label.full_text()} / {win._tab_started_label.full_text()}")
check("文案里没有 markdown 星号",
      "**" not in win._tab_created_label.toolTip()
      and "**" not in win._tab_started_label.toolTip())

stamp = win._mark_order_created(tab_key)
win._refresh_tab_display()
check("写入创建时间后标签立刻显示该时间",
      stamp and stamp in win._tab_created_label.full_text(),
      win._tab_created_label.full_text())
check("创建时间落进 TabSettings.created_at（持久化来源）", tab.created_at == stamp)

print("\n=== ④ 不可覆盖：再标记一次不改写（复制合计/重打/换号都走这条路） ===")
again = win._mark_order_created(tab_key, when="1999-01-01 00:00:00")
check("created_at 已有值时原样返回，不覆盖", again == stamp and tab.created_at == stamp,
      f"{again} vs {stamp}")

started = win._mark_print_started(tab_key)
win._refresh_tab_display()
check("打印发起时间写入并显示", started and started in win._tab_started_label.full_text(),
      win._tab_started_label.full_text())
again2 = win._mark_print_started(tab_key, when="1999-01-01 00:00:00")
check("started_at 同样不可覆盖（重打不刷新）", again2 == started and tab.started_at == started,
      f"{again2} vs {started}")

print("\n=== ⑤ 持久化：存盘再加载，时间还在 ===")
win._save_config()
win2 = MainWindow(config_path=paths.config_path())
tab2 = win2._config.tabs.get(tab_key)
check("重新加载配置后 created_at 仍在", tab2 is not None and tab2.created_at == stamp,
      getattr(tab2, "created_at", None))
check("重新加载配置后 started_at 仍在", tab2 is not None and tab2.started_at == started,
      getattr(tab2, "started_at", None))
check("旧配置（没有这两个键）加载不报错、不伪造时间",
      TabSettings.from_dict({"jobs": []}).created_at == ""
      and TabSettings.from_dict({"jobs": []}).started_at == "")
win2.close()

# ══════════════════════════════════════════════════════════════════════════
print("\n=== ⑥ 预留上报（复制合计）用标签页创建时间，不用 finished_at ===")
posted = []


class _FakeResp:
    ok = True
    text = "ok"

    def json(self):
        return {"success": True}


class _FakeHttp:
    @staticmethod
    def post(url, headers=None, json=None, timeout=None):
        posted.append(json)
        return _FakeResp()


_orig_http = _gui.http_requests
_gui.http_requests = _FakeHttp
win._cloud_client = types.SimpleNamespace(
    client_id="cid-1", api_url="http://127.0.0.1:9", token="tok",
    is_connected=lambda: True)
tab.jobs[0].order_number = "HN20261103-9001"
tab.jobs[0].finished_at = "1999-01-01 00:00:00"      # 旧实现的错误来源
tab.remark = "验收"
try:
    win._upload_reserved_order("HN20261103-9001")
    for _ in range(50):                              # 上报在线程里跑，等它落 taped
        if posted:
            break
        time.sleep(0.02)
finally:
    _gui.http_requests = _orig_http

check("预留上报真的发出去了（测试前提）", len(posted) == 1, f"{len(posted)} 次")
_payload = posted[0] if posted else {}
check("payload.created_at 是标签页创建时间，不是 finished_at",
      _payload.get("created_at") == stamp and _payload.get("created_at") != "1999-01-01 00:00:00",
      str(_payload.get("created_at")))
check("payload 仍带 order_number / status=reserved",
      _payload.get("order_number") == "HN20261103-9001" and _payload.get("status") == "reserved")

print("\n=== ⑦ 源码口径：换号补报优先标签页时间，云端接单沿用后端时间 ===")
check("换号补报先取标签页 created_at，再退回 finished_at（顺序不能反）",
      "_tab_created = next((str(t2.created_at)" in SRC
      and "_created_at = _tab_created or next(" in SRC
      and SRC.index("_tab_created = next((str(t2.created_at)")
      < SRC.index("_created_at = _tab_created or next("))
check("预留上报不出现「拿 finished_at 直接当 created_at」的写法",
      '"created_at": next((getattr(j' not in SRC
      and 'getattr(j, "finished_at", "")), "")' not in SRC.split("_upload_reserved_order")[1]
      .split("def ")[0]
      and 'created_at = self._mark_order_created(self._current_tab)' in SRC)
check("云端接单标签页写入后端下发的 created_at",
      "_cloud_created = str(getattr(task, 'created_at', '')" in SRC
      and "tab_settings.created_at = _cloud_created" in SRC)
check("开打时同时记 created_at（兜底）与 started_at",
      "created_at = self._mark_order_created(target_key)" in SRC
      and "self._mark_print_started(target_key)" in SRC)

print("\n=== ⑧ 两个标签不挤占按钮：顺序在 stretch 之后，且不被添加到按钮区 ===")
check("时间标签加在 tab_row.addStretch() 之后（右侧、不参与按钮排布）",
      SRC.index("tab_row.addStretch()") < SRC.index("self._tab_created_label = _TimeVariantsLabel()"))
check("时间标签是只读 QLabel（没有点击/编辑入口）",
      "class _TimeVariantsLabel(QLabel)" in SRC)

win._cloud_client = None          # 收尾：关窗时 closeEvent 会问 client，桩对象没有那些方法
win.close()

print("\n=== ①⓪ 新订单新时间：清空/删光复位，撤回还原 ===")
tab = win._config.tabs[win._current_tab]      # ⑤ 之后配置对象可能已被重新加载，重新取当前页
tab.created_at = "2026-11-03 10:00:00"
tab.started_at = "2026-11-03 10:05:00"
tab.frozen = False
win._set_current_jobs([PrintJob(file_path=r"C:\tmp\a.pdf", page_count=2,
                                order_number="HN20261103-0001")])
win._rebuild_table()
win._on_clear_list()
check("清空标签页 ⇒ 创建/发起时间一起清掉（否则下一单会沿用上一单的创建时间）",
      tab.created_at == "" and tab.started_at == "",
      f"created={tab.created_at!r} started={tab.started_at!r}")
win._on_undo_clear()
check("5 秒内撤回清空 ⇒ 时间原样还原（这单没被清掉）",
      tab.created_at == "2026-11-03 10:00:00" and tab.started_at == "2026-11-03 10:05:00",
      f"created={tab.created_at!r} started={tab.started_at!r}")
win._table.selectRow(0)
win._on_remove_selected()
check("把文件全删光 ⇒ 时间复位，下一单重新计时",
      tab.created_at == "" and tab.started_at == "",
      f"created={tab.created_at!r} started={tab.started_at!r} jobs={len(tab.jobs)}")
check("Ctrl+W 清空最后一个标签页也会复位时间",
      SRC.split("tab.frozen = False")[1][:320].find("_reset_tab_order_times(key)") >= 0)

print("\n" + "=" * 78)
print("订单时间标签验收:", "全部通过 ✅" if ok_all else "存在失败 ❌")
sys.exit(0 if ok_all else 1)
