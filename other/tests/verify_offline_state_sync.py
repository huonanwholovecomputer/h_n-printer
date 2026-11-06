#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""验收：本地订单库一变，界面立刻跟上（2026-11-06 用户第 1 条需求）。

用户现场：右下角「📴 离线未同步 N」的文案不跟着自动同步走 —— 后台同步线程把单子
上报成功后什么都不通知界面，角标要等 60 秒定时器才变；「📴 离线未同步订单」明细窗
更是要手点「刷新」。要求：**只要本地订单发生云端同步（或换号、清库、落单），
所有与本地订单同步相关的显示状态一起同步。**

本脚本钉住：
  ① `OfflineSync` 有变更回调钩子，且在 save/sync/retry/rename/mark_synced/clear_all
     这些"界面数字会变"的写库动作之后真的触发；
  ② 回调抛异常不会打断业务（通知失败不该影响上报）；
  ③ 回调可以被清掉（`set_change_callback(None)`）；
  ④ gui.py 接线：`_offlineStateChanged` 信号 → `_on_offline_state_changed` →
     刷角标 + 刷明细窗，且注册发生在 `if not self._config.cloud_enabled:` 之前
     （本地模式落单也要立刻看见）；
  ⑤ 变异自证：把 `_notify_change()` 从 sync 里抽掉，静态判据必须变红。

退出码 0 = 全绿。
"""
import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(os.path.dirname(_HERE))
LOCAL_TOOL = os.path.join(_REPO, "local_print_tool")

_SANDBOX = tempfile.mkdtemp(prefix="hn_offline_state_")
os.environ["APPDATA"] = _SANDBOX
sys.path.insert(0, LOCAL_TOOL)

ok_all = True


def check(name, cond, detail=""):
    global ok_all
    if not cond:
        ok_all = False
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


def section(title):
    print(f"\n=== {title} ===")


def _method_body(src: str, header: str) -> str:
    """按缩进切出一个方法体（先跳过可能跨行的签名，再切到第一个缩进 <= def 的非空行）。"""
    i = src.find(header)
    if i < 0:
        return ""
    line_start = src.rfind("\n", 0, i) + 1
    indent = len(src[line_start:i]) - len(src[line_start:i].lstrip())
    lines = src[i:].splitlines(True)
    out = [lines[0]]
    k = 0
    if not lines[0].rstrip().endswith(":"):
        # 跨行签名：找到以 ")" 开头且以 ":" 结尾的闭合行
        for k in range(1, len(lines)):
            out.append(lines[k])
            if lines[k].strip().startswith(")") and lines[k].strip().endswith(":"):
                break
    for line in lines[k + 1:]:
        if line.strip() and (len(line) - len(line.lstrip())) <= indent:
            break
        out.append(line)
    return "".join(out)


def _src(path: str) -> str:
    with open(os.path.join(LOCAL_TOOL, path), encoding="utf-8") as f:
        return f.read()


# ══════════════════════════════════════════════════════════════════
# ① OfflineSync 钩子：写库就通知（真 sqlite，不联网）
# ══════════════════════════════════════════════════════════════════
section("① OfflineSync 变更钩子：谁改库谁通知")

import offline_sync as _os  # noqa: E402

db = os.path.join(_SANDBOX, "offline_state.db")
sync = _os.OfflineSync(db_path=db)
hits = []
sync.set_change_callback(lambda: hits.append(1))

FILES = [{"file_path": r"C:\x\a.pdf", "file_name": "a.pdf", "page_count": 2,
          "copies": 1, "duplex": "on"}]

sync.save_order_offline("HN20261106-0001", FILES, 3.0, "2026-11-06 20:00:00")
check("① 落本地单 → 通知一次", len(hits) == 1, f"hits={len(hits)}")
check("① 未同步数 = 1", sync.status_summary().get("unsynced") == 1, str(sync.status_summary()))

# 网络层打桩：本轮只验证"写库成功之后有没有通知"，不在测试里发 HTTP
_uploaded = []


def _fake_upload(db_id, order_number, files_json, total_price, created_at, server_url, token,
                 owner_name="", is_admin_print=True, remark=""):
    ok = order_number != "HN20261106-0002"       # L2 当作云端业务拒收
    _uploaded.append(order_number)
    if ok:
        return True, False, ""
    return False, True, "云端拒收：金额不合法"


sync._upload_order_detail = _fake_upload
n = sync.sync_all_pending_orders("https://example.invalid", "tok")
check("① 自动同步成功 1 条", n == 1, f"n={n}")
check("★ ① 同步成功 → 立刻通知（旧代码这里只有 logger.info，界面要等 60s）",
      len(hits) == 2, f"hits={len(hits)}")
check("① 同步后未同步数归零", sync.status_summary().get("unsynced") == 0,
      str(sync.status_summary()))

# 拒收也是一次"界面数字会变"的变化（角标要显示「（1 条拒收）」）
sync.save_order_offline("HN20261106-0002", FILES, 3.0, "2026-11-06 20:05:00")
check("① 再落一单 → 通知", len(hits) == 3, f"hits={len(hits)}")
sync.sync_all_pending_orders("https://example.invalid", "tok")
_sum = sync.status_summary()
check("① 拒收单进 rejected", _sum.get("rejected") == 1, str(_sum))
check("★ ① 云端拒收 → 也通知（角标要变「（1 条拒收）」）", len(hits) == 4, f"hits={len(hits)}")

# 暂时性失败（网络类）不改未同步数 ⇒ 不必每 60s 刷一次界面
sync.save_order_offline("HN20261106-0003", FILES, 3.0, "2026-11-06 20:10:00")
_h = len(hits)
sync._upload_order_detail = lambda *a, **k: (False, False, "连接超时")
sync.sync_all_pending_orders("https://example.invalid", "tok")
check("① 纯网络失败（未同步数没变）不重复通知", len(hits) == _h, f"{_h} → {len(hits)}")

# 手动重试 / 换号 / 手工标记 / 清库
_h = len(hits)
sync.retry_now("")
check("★ ① 手动重试（重置退避）→ 通知", len(hits) > _h, f"{_h} → {len(hits)}")
_h = len(hits)
sync.rename_order_number("HN20261106-0003", "HN20261106-0099")
check("★ ① 换号 → 通知", len(hits) > _h, f"{_h} → {len(hits)}")
_h = len(hits)
sync.mark_synced("HN20261106-0099")
check("★ ① mark_synced → 通知", len(hits) > _h, f"{_h} → {len(hits)}")
_h = len(hits)
sync.clear_all()
check("★ ① 清空本地库 → 通知", len(hits) > _h, f"{_h} → {len(hits)}")

# ② 回调抛异常不影响业务；③ 可清掉
section("② 回调异常不外溢 / 可注销")


def _boom():
    raise RuntimeError("界面已经销毁")


sync.set_change_callback(_boom)
try:
    sync.save_order_offline("HN20261106-0004", FILES, 3.0, "2026-11-06 20:20:00")
    _ok = True
    _err = ""
except Exception as e:                      # noqa: BLE001
    _ok = False
    _err = f"{type(e).__name__}: {e}"
check("② 回调抛异常不影响写库", _ok, _err)
check("② 行确实写进去了", sync.find_pending_by_number("HN20261106-0004") is not None)

sync.set_change_callback(None)
_h = len(hits)
sync.clear_all()
check("③ 注销后不再回调", len(hits) == _h and sync.count_unsynced() == 0, f"hits={len(hits)}")

# ══════════════════════════════════════════════════════════════════
# ④ gui.py 接线
# ══════════════════════════════════════════════════════════════════
section("④ gui.py：信号 → 主线程刷角标 + 明细窗")

_GUI = _src("gui.py")
_INIT = _method_body(_GUI, "def _init_cloud_client(")
_SLOT = _method_body(_GUI, "def _on_offline_state_changed(")
_OS_SRC = _src("offline_sync.py")


def _wiring_ok(src: str) -> bool:
    init = _method_body(src, "def _init_cloud_client(")
    slot = _method_body(src, "def _on_offline_state_changed(")
    if "_offlineStateChanged = Signal()" not in src:
        return False
    if "_offlineStateChanged.connect(self._on_offline_state_changed)" not in src:
        return False
    if "set_change_callback(self._offlineStateChanged.emit)" not in init:
        return False
    _i_cb = init.find("set_change_callback(")
    _i_off = init.find("\n        if not self._config.cloud_enabled:")
    if not (0 <= _i_cb < _i_off):
        return False
    return ("self._refresh_offline_badge()" in slot
            and self_slot_reload(slot))


def self_slot_reload(slot: str) -> bool:
    return "self._reload_offline_dialog()" in slot


check("④ 有 _offlineStateChanged 信号", "_offlineStateChanged = Signal()" in _GUI)
check("★ ④ 注册了 OfflineSync 变更回调 → 信号 emit",
      "set_change_callback(self._offlineStateChanged.emit)" in _INIT)
_i_cb = _INIT.find("set_change_callback(")
_i_off = _INIT.find("\n        if not self._config.cloud_enabled:")
check("★ ④ 注册点在 cloud_enabled 分支之前（本地模式落单也要立刻刷新）",
      0 <= _i_cb < _i_off, f"cb@{_i_cb} off@{_i_off}")
check("④ 信号接到 _on_offline_state_changed",
      "_offlineStateChanged.connect(self._on_offline_state_changed)" in _GUI)
check("★ ④ 槽里刷角标", "self._refresh_offline_badge()" in _SLOT)
check("★ ④ 槽里刷离线订单窗", "self._reload_offline_dialog()" in _SLOT)


def _hooks_ok(src: str) -> bool:
    need = ["save_order_offline", "sync_all_pending_orders", "retry_now",
            "rename_order_number", "mark_synced", "clear_all"]
    for name in need:
        if "self._notify_change()" not in _method_body(src, f"def {name}("):
            return False
    return "_notify_change" in src


check("★ ④ offline_sync 的每个「界面数字会变」的写库动作都通知（6 处）", _hooks_ok(_OS_SRC))
check("④ 通知实现自带异常兜底", "except Exception:" in _method_body(_OS_SRC, "def _notify_change("))

# ⑤ 变异自证：抽掉 sync 里的 _notify_change() 必须变红
_MUT = _OS_SRC.replace(
    "        if synced_count > 0 or rejected_count > 0:\n            self._notify_change()", "")
check("⑤ 变异自证：sync 里不发通知时判据变红",
      (not _hooks_ok(_MUT)) and _MUT != _OS_SRC)
_MUT2 = _GUI.replace("            self._offline_sync.set_change_callback(self._offlineStateChanged.emit)",
                     "")
check("⑤ 变异自证：不注册回调时接线判据变红", not _wiring_ok(_MUT2) and _MUT2 != _GUI)

print("\n" + ("=" * 60))
print("离线同步界面状态同步验收: " + ("全部通过 ✅" if ok_all else "存在失败 ❌"))
sys.exit(0 if ok_all else 1)
