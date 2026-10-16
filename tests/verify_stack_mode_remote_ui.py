# -*- coding: utf-8 -*-
"""攒批模式「移动端远程开关」的静态接线验收（小程序 + Android APP）

背景：本地打印工具有「🖐 攒批模式」（多任务合并翻纸：多个订单的第 1 趟先堆成一叠，
人工翻一次纸再统一打第 2 趟）。本脚本钉住**移动端那一半**——两端设备列表要显示每台设备的
攒批状态，且**只有管理员/超管**能远程开关；能不能切（繁忙：正在打印 / 等人工翻纸打第二趟 /
有排队待打的请求）由服务端算好，前端只做前置提示 + 请求转发。

接口契约（字段名固定，后端见 mobile_apps/printer-backend/app.py）：

  · GET  /api/claiming_devices 每台设备新增 4 字段：
      stack_mode     true | false | null（null = 该设备从未上报，状态未知）
      stack_busy     true | false
      stack_can_toggle 服务端已算好「当前能否切换」
      stack_reason   不能切换时的原因文案（能切换时为空串）
  · POST /api/printer/stack_mode  body {client_id, enable}
      **一律 HTTP 200**，用 body 的 success 判断；失败时 message 即拦截原因。

**适用性前提（2026-10 用户要求）**：攒批（多任务合并翻纸）只对「无自动双面器、双面需人工翻纸」
的机型有意义 —— 两端设备列表**只对 `auto_duplex === false` 的设备**渲染攒批徽标与远程开关
（支持自动双面 / 能力未确认的连区块都不渲染），点击入口另有兜底。判据见 ⑧。

本脚本是**纯静态断言**（读源码字符串，不启动任何运行时），判据全部落在真实源码上；
末尾的「变异自证」把源码里的关键标记抹掉再跑同一条判据，必须转假 —— 证明判据不是恒真。

用法: python tests/verify_stack_mode_remote_ui.py   （exit 0 = 全部通过）
"""
import io
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

P_MINI_JS = os.path.join(REPO, "mobile_apps", "h_n_print", "pages", "index", "index.js")
P_MINI_WXML = os.path.join(REPO, "mobile_apps", "h_n_print", "pages", "index", "index.wxml")
P_MINI_WXSS = os.path.join(REPO, "mobile_apps", "h_n_print", "pages", "index", "index.wxss")
P_APP_JS = os.path.join(REPO, "mobile_apps", "android_app", "www", "app.js")
P_APP_CSS = os.path.join(REPO, "mobile_apps", "android_app", "www", "styles.css")

ok_all = True


def check(name, cond, detail=""):
    global ok_all
    if not cond:
        ok_all = False
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def span(src, start, op="{", cl="}"):
    """从 src[start]（应为 op 字符）起做括号配平，返回含括号的片段；失败返回 ''。

    跳过引号内的字面量（'...' / "..." / `...`），避免字符串里的括号打乱计数。
    只用于本脚本要断言的几段短代码，够用即可。
    """
    if start < 0 or start >= len(src) or src[start] != op:
        return ""
    depth = 0
    i = start
    while i < len(src):
        c = src[i]
        if c in "\"'`":
            q = c
            i += 1
            while i < len(src) and src[i] != q:
                if src[i] == "\\":
                    i += 1
                i += 1
        elif c == op:
            depth += 1
        elif c == cl:
            depth -= 1
            if depth == 0:
                return src[start:i + 1]
        i += 1
    return ""


def fn_body(src, anchor):
    """截出 anchor 所指函数体（从函数头后面第一个 '{' 配平到闭合），避免断言误命中别处的同名片段。"""
    i = src.find(anchor)
    if i < 0:
        return ""
    brace = src.find("{", i)
    if brace < 0:
        return ""
    return span(src, brace)


def obj_after(text, marker):
    """取出 marker 之后第一个 '{...}' 对象字面量（请求体）。"""
    i = text.find(marker)
    if i < 0:
        return ""
    brace = text.find("{", i + len(marker))
    if brace < 0:
        return ""
    return span(text, brace)


# ==================== 判据（全部以「源码文本」为输入，便于变异自证） ====================

def has_stack_fields(src):
    return all(k in src for k in ("stack_mode", "stack_can_toggle", "stack_reason"))


def has_endpoint(src):
    return "/api/printer/stack_mode" in src


def has_badges(src):
    return "攒批中" in src and "攒批未开" in src


def has_admin_role(src):
    return "'admin'" in src and "'super_admin'" in src


def has_dark_override(src, *classes):
    return all((".theme-dark ." + c) in src for c in classes)


def dark_after_base(src, cls):
    """深色覆盖必须写在浅色基类规则**之后**（否则被基类同权重规则压过去，覆盖形同没写）。"""
    base = src.find("." + cls + " {")
    dark = src.find(".theme-dark ." + cls)
    return base >= 0 and dark >= 0 and dark > base


def blank(src, needle):
    """把 needle 全部替换成必然不存在的标记 —— 变异自证用。"""
    return src.replace(needle, "ZZ_不存在的标记_ZZ")


# ---- 适用性判据（只给「需人工翻纸」的机型渲染攒批区块；2026-10 用户要求）----

def mini_gate_on_manual_flip(src):
    """小程序：整个攒批区块（徽标 + 开关）被 auto_duplex === false 的 <block> 包住。"""
    return ('<block wx:if="{{item.auto_duplex === false}}">' in src
            and 'wx:if="{{item.auto_duplex === false}}"' in src)


def app_gate_on_manual_flip(badge, btn, handler):
    """APP：徽标 / 开关控件 / 点击入口三处都先筛「需人工翻纸」。"""
    return all("d.auto_duplex !== false" in body and "return" in body
               for body in (badge, btn)) and "d.auto_duplex !== false" in handler


def mini_js_gate_on_manual_flip(fn):
    return "device.auto_duplex !== false" in fn and "不需要攒批模式" in fn


# ==================== 读源码 ====================
try:
    mini_js = read(P_MINI_JS)
    mini_wxml = read(P_MINI_WXML)
    mini_wxss = read(P_MINI_WXSS)
    app_js = read(P_APP_JS)
    app_css = read(P_APP_CSS)
except OSError as e:
    sys.exit(f"读取源码失败: {e}")

# 两端各自的「攒批开关」函数体（请求体断言只在函数内做，避免误命中别处）
mini_fn = fn_body(mini_js, "onToggleStackMode(e) {")
app_fn = fn_body(app_js, "async function toggleStackMode(clientId) {")
# 小程序就地写回 helper
mini_apply = fn_body(mini_js, "_applyStackMode(index, clientId, data) {")
# 小程序提示 helper（长 reason 走 modal 防 toast 截断）
mini_tip = fn_body(mini_js, "_showStackTip(msg) {")
# APP 管理员判定 + 开关控件渲染
app_isadmin = fn_body(app_js, "function isAdminRole() {")
app_btn = fn_body(app_js, "function stackToggleBtnHtml(d) {")
app_badge = fn_body(app_js, "function stackBadgeHtml(d) {")

# ==================== ① 新增 4 字段的消费 ====================
print("=== ① 攒批字段（stack_mode / stack_can_toggle / stack_reason）===")
check("小程序 JS 消费三字段", has_stack_fields(mini_js))
check("小程序 WXML 消费 stack_mode + stack_can_toggle",
      "stack_mode" in mini_wxml and "stack_can_toggle" in mini_wxml)
check("APP app.js 消费三字段", has_stack_fields(app_js))
check("两端都判定 stack_can_toggle === false（禁用）+ 用 stack_reason 做提示",
      "stack_can_toggle === false" in mini_js and "stack_reason" in mini_fn
      and "stack_can_toggle === false" in app_js and "stack_reason" in app_fn)
check("两端都读取 stack_busy（与服务端状态保持一致）",
      "stack_busy" in mini_apply and "stack_busy" in app_fn)

# ==================== ② POST /api/printer/stack_mode ====================
print("\n=== ② POST /api/printer/stack_mode 调用点 ===")
mini_body = obj_after(mini_fn, "data:")          # 注：marker 不含 '{'，由 obj_after 自己定位第一个对象字面量
app_body = obj_after(app_fn, "JSON.stringify(")
check("小程序有 stack_mode 端点且方法为 POST", has_endpoint(mini_js) and "method: 'POST'" in mini_fn)
check("小程序请求体同时含 client_id 与 enable",
      "client_id" in mini_body and "enable" in mini_body, mini_body.replace("\n", " "))
check("APP 有 stack_mode 端点且方法为 POST", has_endpoint(app_js) and "method: 'POST'" in app_fn)
check("APP 请求体同时含 client_id 与 enable",
      "client_id" in app_body and "enable" in app_body, app_body.replace("\n", " "))
check("两端都走 Bearer 鉴权（与项目其它用户接口一致）",
      "'Authorization': 'Bearer '" in mini_fn and "api(" in app_fn)
check("小程序用 wx.showToast（icon:'none'）提示结果",
      "wx.showToast(" in mini_fn and "icon: 'none'" in mini_fn)
check("APP 用本项目自己的 showToast(msg, ms)（不是 wx.showToast）",
      "showToast(" in app_fn and "wx.showToast" not in app_js)

# ==================== ③ 管理员/超管角色判断 ====================
print("\n=== ③ 仅 admin / super_admin 可见可点 ===")
check("小程序 WXML 按钮被管理员的 wx:if 包住",
      "wx:if=\"{{userRole === 'admin' || userRole === 'super_admin'}}\"" in mini_wxml)
check("小程序 JS 有角色兜底（非管理员直接 return）",
      "role !== 'admin' && role !== 'super_admin'" in mini_fn)
check("APP 有 isAdminRole() 且判定 admin / super_admin", has_admin_role(app_isadmin))
check("APP 开关控件渲染处有管理员守卫（非管理员不产出 HTML，不留空壳）",
      "if (!isAdminRole()) return '';" in app_btn)
check("APP 点击入口也有管理员兜底", "if (!isAdminRole()) return;" in app_fn)

# ==================== ④ 状态徽标文案 ====================
print("\n=== ④ 状态徽标（攒批中 / 攒批未开）===")
check("小程序 WXML 同时有「🖐 攒批中」与「攒批未开」", has_badges(mini_wxml))
check("APP app.js 同时有「🖐 攒批中」与「攒批未开」", has_badges(app_js))
check("小程序用三态判定（true → 攒批中 / false → 攒批未开 / null → 不渲染）",
      "item.stack_mode === true" in mini_wxml and "wx:elif=\"{{item.stack_mode === false}}\"" in mini_wxml)
check("APP 用三态判定（null 直接返回空串 → 不渲染）",
      "d.stack_mode === true" in app_badge and "d.stack_mode === false" in app_badge
      and "return '';" in app_badge)
check("新增独立 class：device-state-stack / device-state-stack-off",
      "device-state-stack" in mini_wxml and "device-state-stack-off" in mini_wxml
      and "device-state-stack" in app_js and "device-state-stack-off" in app_js
      and ".device-state-stack" in mini_wxss and ".device-state-stack-off" in app_css)

# ==================== ⑤ 深色模式覆盖 ====================
print("\n=== ⑤ 浅色 / 深色两套主题 ===")
check("小程序 WXSS 有深色覆盖（.theme-dark .device-state-stack / .device-stack-btn）",
      has_dark_override(mini_wxss, "device-state-stack", "device-state-stack-off", "device-stack-btn",
                        "device-stack-btn-disabled"))
check("APP styles.css 有深色覆盖（.theme-dark .device-state-stack / .device-stack-btn）",
      has_dark_override(app_css, "device-state-stack", "device-state-stack-off", "device-stack-btn",
                        "device-stack-btn-disabled"))
check("小程序有深色覆盖写在浅色基类之后（否则被同权重基类压过去）",
      dark_after_base(mini_wxss, "device-state-stack") and dark_after_base(mini_wxss, "device-stack-btn"))
check("APP 有深色覆盖写在浅色基类之后（否则被同权重基类压过去）",
      dark_after_base(app_css, "device-state-stack") and dark_after_base(app_css, "device-stack-btn"))

# ==================== ⑥ 就地更新（不整表重渲染）====================
print("\n=== ⑥ 成功后就地更新那一台设备 ===")
check("小程序走精确路径 setData（claimingDevices[i].stack_mode）",
      "'claimingDevices[' + i + '].stack_mode'" in mini_apply
      and "'claimingDevices[' + i + '].stack_busy'" in mini_apply)
check("小程序按 client_id 复核下标（列表可能已被刷新）",
      "clientId" in mini_apply and "findIndex" in mini_apply)
check("小程序成功分支调用就地写回（未整表 setData claimingDevices）",
      "this._applyStackMode(" in mini_fn
      and "claimingDevices: devices" not in mini_fn)
check("APP 更新 claimingDevices 缓存后再 renderDeviceList()",
      "claimingDevices[i].stack_mode = data.stack_mode === true" in app_fn
      and "renderDeviceList();" in app_fn)
check("两端请求中禁用防重复点击",
      "if (this.data.stackTogglingId) return" in mini_fn
      and "if (stackTogglingId) return" in app_fn
      and "stackTogglingId" in mini_wxml)

# ==================== ⑦ 拦截提示文案 ====================
print("\n=== ⑦ 前置拦截提示 ===")
check("两端都有离线提示「设备离线，无法远程切换」",
      "设备离线，无法远程切换" in mini_fn and "设备离线，无法远程切换" in app_fn)
check("两端按钮文案都有「开启攒批」与「关闭攒批」",
      "开启攒批" in mini_wxml and "关闭攒批" in mini_wxml
      and "开启攒批" in app_btn and "关闭攒批" in app_btn)
check("繁忙/不可切换时把服务端 stack_reason 原样弹出（两端同口径）",
      "device.stack_reason || " in mini_fn and "d.stack_reason || " in app_fn)
check("小程序长拦截文案不被 toast 两行截断（>18 字走 wx.showModal 全文显示）",
      "_showStackTip(" in mini_fn and "msg.length > 18" in mini_tip
      and "wx.showModal(" in mini_tip and "showCancel: false" in mini_tip
      and "wx.showToast(" in mini_tip)
check("APP #toast 会换行（max-width:80%，长文案不被截断）",
      "max-width: 80%;" in app_css)

# ==================== ⑧ 适用性前提：只给「需人工翻纸」的机型渲染（2026-10 用户要求）============
print("\n=== ⑧ 攒批区块只对「需人工翻纸」的机型渲染 ===")
check("★ 小程序 WXML：状态徽标 + 远程开关整体被 auto_duplex === false 的 <block> 包住",
      mini_gate_on_manual_flip(mini_wxml))
check("★ 小程序 JS：点击入口兜底（非「需人工翻纸」→ 提示后直接 return，不发请求）",
      mini_js_gate_on_manual_flip(mini_fn))
check("★ APP：徽标 / 开关控件 / 点击入口三处都先筛「需人工翻纸」",
      app_gate_on_manual_flip(app_badge, app_btn, app_fn))
check("★ 设备列表标记本身没被误删（「🖐 需人工翻纸」仍在，仅作提示）",
      "需人工翻纸" in mini_wxml and "需人工翻纸" in app_js)
check("★ 攒批文案没被顺手删掉（只是被条件包住）",
      has_badges(mini_wxml) and has_badges(app_js))

# ==================== ⑨ 变异自证（判据不是恒真）====================
print("\n=== ⑨ 变异自证：抹掉关键标记 → 同一条判据必须转假 ===")
check("[变异自证] ① 端点判据：抹掉 /api/printer/stack_mode → 假",
      not has_endpoint(blank(mini_js, "/api/printer/stack_mode"))
      and not has_endpoint(blank(app_js, "/api/printer/stack_mode")))
check("[变异自证] ② 字段判据：抹掉 stack_can_toggle → 假",
      not has_stack_fields(blank(mini_js, "stack_can_toggle"))
      and not has_stack_fields(blank(app_js, "stack_can_toggle")))
check("[变异自证] ③ 角色判据：抹掉 super_admin → 假",
      not has_admin_role(blank(mini_js, "super_admin"))
      and not has_admin_role(blank(app_js, "super_admin")))
check("[变异自证] ④ 徽标判据：抹掉「攒批中」→ 假",
      not has_badges(blank(mini_wxml, "攒批中"))
      and not has_badges(blank(app_js, "攒批中")))
check("[变异自证] ⑤ 深色覆盖判据：抹掉深色块里的新 class → 假",
      not has_dark_override(blank(mini_wxss, ".theme-dark .device-stack-btn"), "device-stack-btn")
      and not has_dark_override(blank(app_css, ".theme-dark .device-state-stack"), "device-state-stack"))
check("[变异自证] ⑥ 命中函数体判据：把函数名改掉 → 取不到函数体（说明断言真的落在该函数里）",
      fn_body(mini_js, "onToggleStackModeXX(e) {") == ""
      and fn_body(app_js, "async function toggleStackModeXX(clientId) {") == ""
      and bool(mini_fn) and bool(app_fn))
check("[变异自证] ⑦ 适用性判据：抹掉 auto_duplex 前置 → 假（说明它真的在筛机型）",
      not mini_gate_on_manual_flip(
          blank(mini_wxml, '<block wx:if="{{item.auto_duplex === false}}">'))
      and not app_gate_on_manual_flip(
          fn_body(blank(app_js, "d.auto_duplex !== false"), "function stackBadgeHtml(d) {"),
          fn_body(blank(app_js, "d.auto_duplex !== false"), "function stackToggleBtnHtml(d) {"),
          fn_body(blank(app_js, "d.auto_duplex !== false"),
                  "async function toggleStackMode(clientId) {"))
      and not mini_js_gate_on_manual_flip(blank(mini_fn, "device.auto_duplex !== false")))

print("\n" + "=" * 56)
print("攒批模式移动端远程开关验收:", "全部通过 ✅" if ok_all else "存在失败 ❌")
sys.exit(0 if ok_all else 1)
