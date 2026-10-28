# -*- coding: utf-8 -*-
"""移动端「我」页内容布局顺序的静态验收（小程序 me.wxml + Android APP index.html）

## 为什么要有这个脚本

「我」页原来是**按"功能出现的历史顺序"堆出来**的：身份卡 → 生成许可密钥 → 账号绑定 →
（访客/普通用户的许可区块）→ 服务器存储 → 防滥用设置 → 历史授权用户 → 管理管理员 →
已临时授权的普通用户 → 我的打印任务。问题：

  1. **「我的打印任务」被压在最底部** —— 全页唯一的高频内容（顾客查进度、管理员看自己下的单）
     要滑过 8~9 个区块才够得着（线上实测超管 121 单，第一屏全是管理面板）；
  2. **个人内容与管理工具混在一起** —— 「账号绑定 / 检查更新」是个人设置，却夹在管理区块中间；
  3. **两端顺序不一致** —— 访客/普通用户的许可区块，小程序排在存储统计之前，APP 排在所有管理
     区块之后（同一个角色在两个端看到的页面结构不一样，同源逻辑却不同款）。

重排原则（本脚本钉住的就是它）：

    我是谁（身份卡） → 我的状态（许可：访客验证密钥 / 普通用户打印许可）
    → 我的东西（我的打印任务） → 我的设置（账号绑定、检查更新）
    —— 以上个人区全角色可见，且**永远在管理区之前** ——
    → 管理区（仅 admin/super：生成许可密钥 → 已临时授权用户 → 管理管理员
      → 历史授权用户 → 服务器存储 → 防滥用设置）

（2026-10-28 用户要求：管理区的「本地打印任务」卡片是早期测试残留，两端入口都已删除；
 该页只保留「历史授权用户」一张角色入口卡片。）

## 判据

① 两端区块顺序 == 设计顺序（顺序表写死在这里；**若确属有意重排，同步更新本表**）
② 「我的打印任务」不再垫底：它必须在**全部**管理区块之前（这是本次重排的核心）
③ 个人区（身份卡/许可/我的任务/账号绑定/检查更新）在管理区之前；两端一致
④ 树深度：个人区块都是容器直接子级；APP 的管理区块仍在 #adminSections 内（+1 层）
⑤ 角色门控没被一起搬走（小程序 wx:if / APP me.js 的 setBlock·updateRoleActions 接线）
⑥ 每个区块的关键标记恰好出现一次（防搬漏/搬重）＋ APP 我页 id 集合不增不减
⑦ 小程序的失效 `animation-delay` 已清掉（那是死代码：me 页没有任何元素挂 animation，
   其数值却编码着旧顺序，留着会把后人带偏）

用法: python tests/verify_me_layout_order.py   （exit 0 = 全部通过）
"""
import io
import os
import re
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
P_MP = os.path.join(REPO, "mobile_apps", "h_n_print", "pages", "me", "me.wxml")
P_APP = os.path.join(REPO, "mobile_apps", "android_app", "www", "index.html")
P_APP_JS = os.path.join(REPO, "mobile_apps", "android_app", "www", "me.js")

# ── 小程序：区块锚点（唯一注释行）与设计顺序 ──────────────────────────────
MP_ANCHORS = [
    ("card", "    <!-- 用户信息卡片（右侧许可密钥徽章可点击展开详情） -->"),
    ("guest", "    <!-- 访客：验证许可密钥 -->"),
    ("userlic", "    <!-- 普通用户 / 临时授权：已获得许可 -->"),
    ("orders", "    <!-- 我的打印任务 -->"),
    ("bind", "    <!-- 账号绑定入口（管理功能收进独立页面，不在首页常驻展示） -->"),
    ("keys", "    <!-- 管理员：生成许可密钥 -->"),
    ("tempusers", "    <!-- 管理员/超级管理员：已临时授权的普通用户（位于管理管理员下方；管理员同样位置） -->"),
    ("admins", "    <!-- 超级管理员：管理员列表 -->"),
    ("authusers", "    <!-- 管理员/超级管理员：历史授权用户 -->"),
    ("storage", "    <!-- 管理员：存储统计 -->"),
    ("security", "    <!-- 管理员：防滥用设置（DDoS 防护阈值） -->"),
]
MP_DESIGN = ["card", "guest", "userlic", "orders", "bind",
             "keys", "tempusers", "admins", "authusers", "storage", "security"]
MP_REGION_START = '      <view class="container">'
MP_REGION_END = "  <!-- 昵称选择弹窗 -->"

# ── APP：区块锚点与设计顺序（HTML 注释 / 结构性标记） ──────────────────────
APP_ANCHORS = [
    ("card", "          <!-- 用户卡片 -->"),
    ("guest", "          <!-- 访客：验证许可密钥 -->"),
    ("userlic", '          <!-- 普通用户：已获得打印许可（对齐小程序"打印许可"区块） -->'),
    ("orders", "          <!-- 我的打印任务 -->"),
    ("bind", "          <!-- 账号绑定入口（管理功能收进独立子视图，不在首页常驻展示） -->"),
    ("update", "          <!-- 检查更新入口 -->"),
    ("admin_open", "          <!-- 管理员区块（各子块用 admin-collapsible 展开/收起，对齐小程序入场动画） -->"),
    ("keys", "            <!-- 生成许可密钥 -->"),
    ("tempusers", "            <!-- 已临时授权的普通用户 -->"),
    ("admins", "            <!-- 超级管理员：管理员列表 -->"),
    ("role", "            <!-- 角色入口（对齐小程序：历史授权用户） -->"),
    ("storage", "            <!-- 服务器存储 -->"),
    ("security", "            <!-- 防滥用设置 -->"),
]
APP_DESIGN = ["card", "guest", "userlic", "orders", "bind", "update",
              "admin_open", "keys", "tempusers", "admins", "role", "storage", "security"]
APP_ADMIN_BLOCKS = ["keys", "tempusers", "admins", "role", "storage", "security"]
APP_REGION_START = '<div class="page-root" id="page-me" style="display:none">'
APP_REGION_END = "<!-- ===== 子视图：历史授权用户 ===== -->"

# 每个区块的关键标记：搬漏/搬重都会被抓到
MP_MARK = {
    "card": 'class="user-card-wrap"',
    "guest": 'bindtap="onRedeemKey"',
    "userlic": "user-license-card",
    "orders": 'class="orders-section',
    "bind": 'bindtap="onGoBindPage"',
    "keys": 'class="generate-btn"',
    "tempusers": 'wx:for="{{tempUsers}}"',
    "admins": 'wx:for="{{admins}}"',
    "authusers": 'bindtap="onGoAuthorizedUsers"',
    "storage": 'bindtap="onSaveRetention"',
    "security": 'class="security-summary-card',
}
APP_MARK = {
    "card": 'class="user-card-wrap"',
    "guest": "请输入管理员提供的许可密钥以获取打印权限",
    "userlic": "您已获得打印许可",
    "orders": 'class="orders-section"',
    "bind": 'id="btnBindAccount"',
    "update": 'id="btnCheckUpdate"',
    "admin_open": 'id="adminSections"',
    "keys": 'id="adminBlockKeys"',
    "tempusers": 'id="tempUsersBlock"',
    "admins": 'id="adminsBlock"',
    "role": 'id="adminBlockAuth"',
    "storage": 'id="adminBlockStorage"',
    "security": 'id="adminBlockSecurity"',
}

# APP 我页 id 快照（搬漏/搬重/误删都会改变这个集合）
APP_ID_SNAPSHOT = {
    "activeKeyCount", "activeKeys", "adminBlockAuth", "adminBlockKeys",
    "adminBlockSecurity", "adminBlockStorage", "adminCount", "adminList", "adminSections",
    "adminsBlock", "avatarBtn", "avatarImg", "avatarInput", "bindEntryDesc", "btnAuthorizedUsers",
    "btnBindAccount", "btnCheckUpdate", "deleteAllFilesBtn", "generateKeyBtn",
    "guestSection", "keyTypeAdmin", "keyTypeRow", "keyTypeSlider", "keyTypeTemp", "licenseBadge",
    "licenseBadgeKey", "licenseBadgeValidity", "licenseDetail", "licenseDetailCreated",
    "licenseDetailCreator", "licenseDetailExpires", "licenseDetailKey", "licenseDetailStatus",
    "licenseDetailType", "licenseDetailUsed", "licenseDetailValidity", "licenseMinutesMinus",
    "licenseMinutesPlus", "licenseMinutesValue", "nicknameRow", "nicknameText", "orderCountBadge",
    "orderList", "ordersEmpty", "ordersEndHint", "ordersNext", "ordersNextBottom",
    "ordersPageNumbers", "ordersPageNumbersBottom", "ordersPageSize", "ordersPageSizeArrow",
    "ordersPageSizeText", "ordersPager", "ordersPagerBottom", "ordersPrev", "ordersPrevBottom",
    "page-me", "pageSizePicker", "redeemBtn", "redeemInput", "redeemPasteBtn", "retentionDaysMinus",
    "retentionDaysPlus", "retentionDaysValue", "retentionHint", "retentionHoursMinus",
    "retentionHoursPlus", "retentionHoursValue", "roleLabel", "saveRetentionBtn", "saveSecurityBtn",
    "scrollContentMe", "scrollTopBtn", "scroller-me", "securityArrow", "securityDetail",
    "securityItems", "securitySummary", "storageFiles", "storageSize", "tempUserCount",
    "tempUserList", "tempUsersBlock", "userLicenseCountdown", "userLicenseCreator",
    "userLicenseDetail", "userLicenseKey", "userLicenseSection",
}

# 小程序角色门控（重排不许把它们搬丢）
MP_GATES = [
    'wx:if="{{userRole === \'guest\'}}" class="license-section"',
    'wx:if="{{userRole === \'user\'}}" class="license-section"',
    "license-section admin-collapsible {{userRole === 'admin' ? 'admin-expanded' : ''}}",
    'wx:if="{{isSuperAdmin}}" class="license-section"',
    'wx:if="{{userRole === \'admin\'}}" class="license-section"',
    "role-actions admin-collapsible {{userRole === 'admin' ? 'admin-expanded' : ''}}",
    "storage-section admin-collapsible {{userRole === 'admin' && storageStats ? 'admin-expanded' : ''}}",
    "security-section admin-collapsible {{userRole === 'admin' && securityConfig ? 'admin-expanded' : ''}}",
]
# APP 角色门控落在 me.js（重排只动 HTML，接线不许被一起搬走）
APP_JS_GATES = [
    "setBlock('adminBlockKeys', isAdmin)",
    "setBlock('adminBlockStorage', isAdmin && !!meState.storageStats)",
    "setBlock('adminBlockSecurity', isAdmin && !!meState.securityConfig)",
    "setBlock('adminBlockAuth', isAdmin)",
    "document.getElementById('guestSection').style.display = state.role === 'guest' ? '' : 'none'",
    "userLic.style.display = state.role === 'user' ? '' : 'none'",
    "document.getElementById('adminsBlock').style.display = state.isSuperAdmin ? '' : 'none'",
    "document.getElementById('tempUsersBlock').style.display = admin ? '' : 'none'",
]


def read(path):
    with io.open(path, encoding="utf-8", newline="") as f:
        return f.read().replace("\r\n", "\n")


def region_of(text, start, end):
    i = text.index(start)
    j = text.index(end, i)
    return text[i:j], i


def order_of(region, anchors):
    """返回 [(key, 出现位置)]，按位置排序；锚点缺失/重复即抛错。"""
    pos = []
    for key, anchor in anchors:
        hits = [m.start() for m in re.finditer(re.escape(anchor), region)]
        if len(hits) != 1:
            raise AssertionError("锚点 %s (%r) 在区域内出现 %d 次" % (key, anchor, len(hits)))
        pos.append((hits[0], key))
    pos.sort()
    return [k for _, k in pos]


def depth_map(region, anchors_keys):
    """逐行统计 <view>/<div> 深度，返回 {key: 深度}（区域起点 = 0 层）。"""
    out = {}
    depth = 0
    pending = dict(anchors_keys)
    for line in region.split("\n"):
        for key, anchor in list(pending.items()):
            if anchor in line:
                out[key] = depth
                del pending[key]
        depth += len(re.findall(r"<view\b|<div\b", line)) - len(re.findall(r"</view>|</div>", line))
    return out


def evaluate(mp_text, app_text, app_js):
    """全部判据；返回失败说明列表（空 = 全过）。"""
    fails = []

    def want(cond, msg):
        if not cond:
            fails.append(msg)

    # ① 两端顺序 == 设计顺序
    mp_region, _ = region_of(mp_text, MP_REGION_START, MP_REGION_END)
    app_region, _ = region_of(app_text, APP_REGION_START, APP_REGION_END)
    mp_order = order_of(mp_region, MP_ANCHORS)
    app_order = order_of(app_region, APP_ANCHORS)
    want(mp_order == MP_DESIGN,
         "小程序区块顺序与设计不符：%r（设计 %r；若确属有意重排，同步更新 MP_DESIGN）"
         % (mp_order, MP_DESIGN))
    want(app_order == APP_DESIGN,
         "APP 区块顺序与设计不符：%r（设计 %r；若确属有意重排，同步更新 APP_DESIGN）"
         % (app_order, APP_DESIGN))

    # ② 「我的打印任务」不再垫底：必须在全部管理区块之前
    mgmt_mp = [k for k in MP_DESIGN if k in ("keys", "tempusers", "admins", "authusers",
                                             "storage", "security")]
    for f in (mp_order, app_order):
        o = f
        if "orders" in o:
            after = [o.index(m) for m in mgmt_mp if m in o]
            want(all(o.index("orders") < a for a in after),
                 "「我的打印任务」没有排到管理区块之前：%r" % (o,))
    want(app_order.index("orders") < app_order.index("admin_open"),
         "APP「我的打印任务」没在 #adminSections 之前：%r" % (app_order,))

    # ③ 个人区在管理区之前 + 两端共有区块顺序一致
    personal_mp = ["card", "guest", "userlic", "orders", "bind"]
    want(all(mp_order.index(p) < mp_order.index("keys") for p in personal_mp),
         "小程序个人区没有整体排在管理区之前：%r" % (mp_order,))
    personal_app = ["card", "guest", "userlic", "orders", "bind", "update"]
    want(all(app_order.index(p) < app_order.index("admin_open") for p in personal_app),
         "APP 个人区没有整体排在 #adminSections 之前：%r" % (app_order,))
    mp_shared = [k for k in mp_order if k != "bind" or True]
    app_shared = [k for k in app_order if k not in ("update", "admin_open")]
    app_for_cmp = ["role" if k == "role" else k for k in app_shared]
    # role = 「历史授权用户」这一块（本地打印任务入口已于 2026-10-28 删除），按小程序口径比对
    mp_cmp = list(mp_order)
    app_cmp = []
    for k in app_for_cmp:
        if k == "role":
            app_cmp.append("authusers")
        else:
            app_cmp.append(k)
    want(mp_cmp == app_cmp,
         "两端共有的区块顺序不一致：\n  小程序 %r\n  APP    %r" % (mp_cmp, app_cmp))

    # ④ 树深度：个人区块都是容器直接子级；APP 管理区块仍在 #adminSections 内
    mp_depth = depth_map(mp_region, {k: a for k, a in MP_ANCHORS})
    want(len(set(mp_depth.values())) == 1 and mp_depth.get("card") == 1,
         "小程序区块不在同一层级（应都在 .container 直接子级）：%r" % (mp_depth,))
    app_depth = depth_map(app_region, {k: a for k, a in APP_ANCHORS})
    want(app_depth.get("card") == app_depth.get("orders") == app_depth.get("admin_open"),
         "APP 个人区块与 adminSections 不在同一层级：%r" % (app_depth,))
    want(all(app_depth.get(b) == app_depth["admin_open"] + 1 for b in APP_ADMIN_BLOCKS),
         "APP 管理区块没有全部留在 #adminSections 内：%r" % (app_depth,))

    # ⑤ 角色门控与接线没被搬丢
    for g in MP_GATES:
        want(g in mp_text, "小程序角色门控丢失：%r" % g)
    for g in APP_JS_GATES:
        want(g in app_js, "APP 角色门控/接线丢失：%r" % g)

    # ⑥ 关键标记恰好一次 + id 集合不增不减
    for k, mark in MP_MARK.items():
        n = mp_region.count(mark)
        want(n == 1, "小程序区块 %s 的关键标记出现 %d 次（应 1 次）：%r" % (k, n, mark))
    for k, mark in APP_MARK.items():
        n = app_region.count(mark)
        want(n == 1, "APP 区块 %s 的关键标记出现 %d 次（应 1 次）：%r" % (k, n, mark))
    got_ids = set(re.findall(r'id="([^"]+)"', app_region))
    want(got_ids == APP_ID_SNAPSHOT,
         "APP 我页 id 集合变化：多出 %r / 少了 %r"
         % (sorted(got_ids - APP_ID_SNAPSHOT), sorted(APP_ID_SNAPSHOT - got_ids)))

    # ⑦ 小程序失效 animation-delay 已清
    want("animation-delay" not in mp_region,
         "小程序我页区块又出现 animation-delay —— me 页没有任何元素挂 animation，"
         "该值是死代码且会把旧顺序编码进去，请删除")

    return fails


# ── 变异自证：把源码改坏，同一条判据必须转红 ────────────────────────────────
def swap_blocks(region, keys, anchors, ka, kb):
    """在区块序列里交换两块的位置（用于变异测试）。"""
    pos = []
    for key, anchor in anchors:
        i = region.find(anchor)
        assert i >= 0, key
        pos.append((i, key))
    pos.sort()
    starts = [i for i, _ in pos]
    order = [k for _, k in pos]
    blocks = {}
    for n, (i, key) in enumerate(pos):
        end = starts[n + 1] if n + 1 < len(starts) else len(region)
        blocks[key] = region[starts[n]:end]
    order[keys.index(ka)], order[keys.index(kb)] = order[keys.index(kb)], order[keys.index(ka)]
    return region[:starts[0]] + "".join(blocks[k] for k in order)


def main():
    mp = read(P_MP)
    app = read(P_APP)
    app_js = read(P_APP_JS)

    fails = evaluate(mp, app, app_js)
    for m in fails:
        print("[FAIL] %s" % m)
    if not fails:
        print("[PASS] ① 两端区块顺序 == 设计顺序（个人区在前、管理区在后）")
        print("[PASS] ② 「我的打印任务」已排到全部管理区块之前")
        print("[PASS] ③ 个人区整体在管理区之前，且两端共有区块顺序一致")
        print("[PASS] ④ 层级正确（个人区块同级；APP 管理区块仍在 #adminSections 内）")
        print("[PASS] ⑤ 角色门控（小程序 wx:if / APP me.js 接线）完整")
        print("[PASS] ⑥ 区块关键标记各一份，APP 我页 id 集合无增减")
        print("[PASS] ⑦ 失效 animation-delay 已清除")

    # ── 变异自证 ──
    print("\n-- 变异自证（每条都必须把判据打红）--")
    muts = []
    mp_region, off = region_of(mp, MP_REGION_START, MP_REGION_END)
    swapped = swap_blocks(mp_region, [k for k, _ in MP_ANCHORS], MP_ANCHORS, "orders", "keys")
    muts.append(("小程序：把「我的打印任务」与「生成许可密钥」换位（= 退回旧顺序的思路）",
                 mp.replace(mp_region, swapped, 1), app, app_js))
    app_region, _ = region_of(app, APP_REGION_START, APP_REGION_END)
    swapped = swap_blocks(app_region, [k for k, _ in APP_ANCHORS], APP_ANCHORS, "orders", "storage")
    muts.append(("APP：把「我的打印任务」挪到管理区之后",
                 mp, app.replace(app_region, swapped, 1), app_js))
    swapped = swap_blocks(app_region, [k for k, _ in APP_ANCHORS], APP_ANCHORS, "bind", "keys")
    muts.append(("APP：把「账号绑定」挪回 #adminSections 里面",
                 mp, app.replace(app_region, swapped, 1), app_js))
    muts.append(("小程序：抹掉访客区块的角色门控",
                 mp.replace('wx:if="{{userRole === \'guest\'}}" class="license-section"',
                            'class="license-section"', 1), app, app_js))
    muts.append(("APP：删掉我页一个 id（模拟搬漏）",
                 mp, app.replace('id="ordersPageSizeText"', 'data-size-text', 1), app_js))
    muts.append(("小程序：给区块重新加上失效的 animation-delay",
                 mp.replace(MP_ANCHORS[0][1], MP_ANCHORS[0][1], 1).replace(
                     'class="role-actions"', 'class="role-actions" style="animation-delay: 0.06s"', 1),
                 app, app_js))

    bad = 0
    for name, m_text, a_text, j_text in muts:
        f = evaluate(m_text, a_text, j_text)
        got = len(f) > 0
        print("[%s] %s" % ("PASS" if got else "FAIL", name))
        if not got:
            bad += 1
    if bad:
        print("\n有 %d 条变异没有被判据打红 —— 判据不成立" % bad)
        return 1
    if fails:
        return 1
    print("\n全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
