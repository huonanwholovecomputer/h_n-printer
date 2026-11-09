# -*- coding: utf-8 -*-
"""验收 · 智能助手调整云端订单归属（op `cloud.order_owner`，方案 A：人在场输财务密码）

为什么要有这一条：助手手里只有设备 token，而云端 `/api/admin/order_user` 认的正是设备
token —— 助手完全可以绕开财务密码直连后端。本 op 的设计就是**故意不走直连**：
必须由操作员在弹窗里输入财务密码，经本地收支清算代理（X-Launch-Token +
X-Finance-Session）落库，密码不落盘、用完即 revoke 会话。

本脚本钉住：
  A. 静态：op 已注册；走代理而不是直连云端；用 `unlock('orderstatus')` 换会话并 revoke；
     弹窗是密码模式且超时 < 60s；密码不进日志/不写配置；错误码已映射、文档已登记。
  B. 编排：假 win + 假云端响应，验证参数校验、目标用户解析（唯一/多义/不存在）、
     成功路径（POST 地址/头/体、revoke）、拒绝、密码错、本地单拒绝。

跑法：python other/tests/verify_agent_order_owner.py（退出码 0 = 全绿）
"""
import os
import re
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LOCAL_TOOL = os.path.join(REPO, "local_print_tool")
sys.path.insert(0, LOCAL_TOOL)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _sec_harness as H          # noqa: E402
import agent_ops                 # noqa: E402
from agent_api import AgentOpError  # noqa: E402


def read(path):
    with open(path, "r", encoding="utf-8", newline="") as f:
        return f.read()


def section(title):
    print("\n=== " + title + " ===")


OPS = read(os.path.join(LOCAL_TOOL, "agent_ops.py"))
API = read(os.path.join(LOCAL_TOOL, "agent_api.py"))
GUI = read(os.path.join(LOCAL_TOOL, "gui.py"))
DOC = read(os.path.join(REPO, "docs", "智能助手控制接口.md"))

# ============================================================
section("A. 静态检查")
# ============================================================

H.chk("op 已注册进 _OPS",
      '"cloud.order_owner": AgentOps.op_cloud_order_owner' in OPS)
H.chk("op 有实现函数",
      "def op_cloud_order_owner(self, params: dict) -> dict:" in OPS)
H.chk("唯一写路径走本地代理（不是直连云端 /api/admin/order_user）",
      '/api/proxy/admin/order_user' in OPS
      and '/api/admin/order_user' not in OPS)
H.chk("用财务密码换 orderstatus 会话",
      re.search(r'unlock\(\s*["\']orderstatus["\']', OPS) is not None)
H.chk("用完立即 revoke 会话",
      "revoke_session(session)" in OPS)
H.chk("弹窗是密码输入（不回显）", "setTextEchoMode(QLineEdit.Password)" in OPS)
H.chk("弹窗超时 30s，必须短于调用超时 60s",
      "APPROVAL_TIMEOUT_SEC = 30.0" in OPS
      and agent_ops.AgentOps.APPROVAL_TIMEOUT_SEC < 60.0)
H.chk("密码只在局部变量里：没有赋值给 self / 不写配置",
      not re.search(r"self\.\w*pwd\w*\s*=", OPS)
      and "save_config" not in OPS[OPS.find("def op_cloud_order_owner"):OPS.find("def _match_single_order")])
H.chk("日志不会打印密码（_log 行里没有 pwd/password 变量）",
      all(("pwd" not in line and "password" not in line)
          for line in OPS.splitlines() if "self.win._log(" in line))
H.chk("approval_denied / wrong_password → 403",
      '"approval_denied", "wrong_password"' in API and "status = 403" in API)
H.chk("needs_setup / finance_error / finance_timeout → 409",
      '"needs_setup", "finance_error",' in API and '"finance_timeout")' in API)
H.chk("GUI 抽出 _ensure_stats_server（页面与 op 共用同一个服务）",
      "def _ensure_stats_server(self):" in GUI
      and "self._ensure_stats_server()" in GUI)
H.chk("文档登记 op、密码协议与错误码",
      "cloud.order_owner" in DOC and "approval_denied" in DOC
      and "财务密码" in DOC)
H.chk("文档仍保留「只读、没有任何写路径」的旧口径修正（cloud.orders 段声明只读）",
      "**只读**：没有任何写路径" in DOC)

# ============================================================
section("B. 编排（假 win / 假云端 / 假代理）")
# ============================================================


class _Client:
    client_id = "cid-1"
    auth_token = "dev-token"
    api_url = "http://cloud.invalid"


class _Fin:
    def __init__(self):
        self.unlock_result = {"ok": True, "session": "S-1", "modules": ["orderstatus"]}
        self.unlocked = []
        self.revoked = []

    def unlock(self, module, password):
        self.unlocked.append((module, password))
        return dict(self.unlock_result)

    def revoke_session(self, token):
        self.revoked.append(token)
        return True


class _Server:
    finance_mode = "cloud"
    url = "http://127.0.0.1:59999"
    launch_token = "launch-1"

    def __init__(self):
        self.finance = _Fin()


class _Win:
    _cloud_client = _Client()

    def __init__(self):
        self.server = _Server()
        self.logs = []

    def _ensure_stats_server(self):
        return self.server

    def _log(self, text):
        self.logs.append(text)


USERS = [
    {"openid": "oA1", "nickname": "闫承翰", "role": "user"},
    {"openid": "oA2", "nickname": "姚懿祥", "role": "user"},
    {"openid": "oA3", "nickname": "承翰同学", "role": "user"},
]
ORDER = {
    "order_id": 42, "order_number": "HN20261109-0007", "owner": "姚懿祥",
    "source": "wechat", "status": "sent",
}


def make_ops(order=None, users=None, password="GoodPass2026", order_hits=None):
    win = _Win()
    ops = agent_ops.AgentOps(win, "test")
    if order_hits is None:
        order_hits = [order or dict(ORDER)]
    ops.op_cloud_orders = lambda params: {"orders": list(order_hits), "total": len(order_hits)}
    ops._http_get_json = lambda url, params, token, client_id: {
        "success": True, "data": {"assignable_users": list(users if users is not None else USERS)}}
    ops._ask_finance_password = lambda o, t: password
    captured = {}

    def _post(url, payload, headers, timeout=10.0):
        captured.update({"url": url, "payload": payload, "headers": headers})
        return {"success": True, "order_id": 42, "openid": "oA1", "nickname": "闫承翰",
                "previous_openid": "oA2", "manual_user_by": "智能助手",
                "manual_user_at": "2026-11-09 19:30:00"}

    ops._http_post_json = _post
    return ops, win, captured


def code_of(fn):
    try:
        fn()
        return ""
    except AgentOpError as e:
        return e.code


# 成功：按昵称精确解析
ops, win, cap = make_ops()
res = ops("cloud.order_owner", {"order": "HN20261109-0007", "user": "闫承翰"})
H.chk("成功路径返回 previous/openid/nickname/manual_*",
      res["previous_openid"] == "oA2" and res["openid"] == "oA1"
      and res["nickname"] == "闫承翰" and res["manual_user_by"] == "智能助手", res)
H.chk("POST 到本地代理（不是云端）",
      cap["url"].endswith("/api/proxy/admin/order_user")
      and cap["url"].startswith("http://127.0.0.1:59999"), cap["url"])
H.chk("带 launch token + finance session 两个头",
      cap["headers"].get("X-Launch-Token") == "launch-1"
      and cap["headers"].get("X-Finance-Session") == "S-1", cap["headers"])
H.chk("请求体 order_id/openid/by 正确",
      cap["payload"] == {"order_id": "42", "openid": "oA1", "by": "智能助手"}, cap["payload"])
H.chk("unlock 用的是 orderstatus，密码取自弹窗",
      win.server.finance.unlocked == [("orderstatus", "GoodPass2026")],
      win.server.finance.unlocked)
H.chk("用完即 revoke 会话",
      win.server.finance.revoked == ["S-1"], win.server.finance.revoked)
H.chk("GUI 日志留痕（谁改的、从谁到谁）",
      any("助手调整订单" in s and "闫承翰" in s for s in win.logs), win.logs)

# 目标用户不存在
ops, _win, _cap = make_ops()
H.chk("目标昵称不存在 → not_found",
      code_of(lambda: ops("cloud.order_owner",
                          {"order": "HN20261109-0007", "user": "不存在的人"})) == "not_found")

# 多义：闫承翰 / 承翰同学 都命中「承翰」
ops, _win, _cap = make_ops()
H.chk("昵称模糊命中多个 → invalid（要求更精确）",
      code_of(lambda: ops("cloud.order_owner",
                          {"order": "HN20261109-0007", "user": "承翰"})) == "invalid")

# openid 精确
ops, _win, _cap = make_ops()
res = ops("cloud.order_owner", {"order": "HN20261109-0007", "openid": "oA1"})
H.chk("openid 精确解析成功", res["openid"] == "oA1")

# 参数互斥
ops, _win, _cap = make_ops()
H.chk("openid 与 user 同时给 → invalid",
      code_of(lambda: ops("cloud.order_owner",
                          {"order": "HN20261109-0007", "openid": "oA1", "user": "闫承翰"})) == "invalid")
H.chk("两个都不给 → invalid",
      code_of(lambda: ops("cloud.order_owner", {"order": "HN20261109-0007"})) == "invalid")
H.chk("order 缺失 → invalid",
      code_of(lambda: ops("cloud.order_owner", {"user": "闫承翰"})) == "invalid")

# 操作员拒绝
ops, _win, _cap = make_ops(password="")
H.chk("操作员拒绝/超时 → approval_denied",
      code_of(lambda: ops("cloud.order_owner",
                          {"order": "HN20261109-0007", "user": "闫承翰"})) == "approval_denied")

# 密码错
ops, win, _cap = make_ops()
win.server.finance.unlock_result = {"ok": False, "code": "wrong_password", "message": "密码错误"}
H.chk("财务密码错 → wrong_password",
      code_of(lambda: ops("cloud.order_owner",
                          {"order": "HN20261109-0007", "user": "闫承翰"})) == "wrong_password")
H.chk("密码错时不发写请求", not _cap)

# 未设密码
ops, win, _cap = make_ops()
win.server.finance.unlock_result = {"ok": False, "code": "needs_setup",
                                    "message": "还没有设置密码"}
H.chk("未设密码 → needs_setup", code_of(lambda: ops(
    "cloud.order_owner", {"order": "HN20261109-0007", "user": "闫承翰"})) == "needs_setup")

# 订单不存在
ops, _win, _cap = make_ops(order_hits=[])
H.chk("订单不存在 → not_found", code_of(lambda: ops(
    "cloud.order_owner", {"order": "HN99999999-0001", "user": "闫承翰"})) == "not_found")

# 本地单拒绝
ops, _win, _cap = make_ops(order={**ORDER, "source": "local"})
H.chk("本地订单（source=local）→ invalid", code_of(lambda: ops(
    "cloud.order_owner", {"order": "HN20261109-0007", "user": "闫承翰"})) == "invalid")

H.finish()
