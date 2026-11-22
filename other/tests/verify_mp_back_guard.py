# -*- coding: utf-8 -*-
"""小程序「安卓物理返回键」接管（utils/back_guard.js）的静态验收

## 为什么要有这个脚本

2026-11-20 用户报的现场问题：安卓上按系统返回键，小程序**直接退出**，而 App 是
「先收弹窗 → 再退出」。要求两端同一套返回策略。

小程序**没有**拦截物理返回键的 API（wx.onBackPress 不存在）。唯一可用的口子是
`<page-container>`，官方文档原文：

    页面内存在该容器时，当用户进行返回操作，关闭该容器不关闭页面。
    返回操作包括三种情形，右滑手势、安卓物理返回键和调用 navigateBack 接口。

于是每页末尾挂一只**隐形守卫容器**：有弹层在 → show=true → 用户按返回时微信只关容器、
不动页面 → `beforeleave` 里收掉最上层弹层，再把守卫重新挂上；弹层收完了 → show=false →
下一次返回才落回「关页面 / 退出小程序」。

这套东西全是**静态约定**（页面之间没有运行期互相校验），最容易在后续改动里静默失配：
  · wxml 的 show 表达式与 js 的 BACK_LAYERS 是两处清单 —— 只改一处，新弹层就永远收不掉；
  · 收层方法名写错（改名/搬走）—— 按返回时什么都关不掉，还看不出来；
  · `closing` 标志笔误 —— 连按返回会重复触发同一层的收尾逻辑；
  · 删掉 `backGuardHold` 递归闸 —— 一次返回连着关两层弹层（代码关容器同样触发 beforeleave）；
  · 收完不重挂 —— 第二层就漏过去，直接退小程序（微信不会自己把容器重新打开）。
所以这些约定必须由脚本钉住，不能靠人眼。

## 判据

① 四个页面（index / me / user-orders / recent-orders）**各恰好一个** `<page-container>`
   守卫，且在 wxml 最末尾（后面只剩空行/注释）；`authorized-users`、`bind` 无弹层，不得有
② 守卫属性齐全：`show="{{!backGuardHold && (…)}"`、`duration="0"`、`z-index` ≤ 100
   （必须压在 .modal-mask=1000 之下）、`overlay="{{false}}"`、`pointer-events: none`、
   `bind:beforeleave="onBackGuardLeave"`，且内含 `.back-guard-sentinel` 占位
③ wxml show 表达式里的标志集合 **== 且顺序 ==** 该页 js `BACK_LAYERS` 的 flag 序列
   （两处清单必须同一份；顺序即「谁的层级更高、先收谁」）
④ `BACK_LAYERS` 里每个 `close` 方法在该页 js 里真实存在；每个 `closing`（若写了）在 data 里声明
⑤ 每页 data 里有 `backGuardHold: false`，methods 里有 `onBackGuardLeave()` 且
   `return backGuard.leave(this, BACK_LAYERS)`
⑥ `utils/back_guard.js` 三条硬闸都在：`_flag` 支持嵌套路径（`pv.visible`）、
   `if (page.data.backGuardHold) return false` 在最前、`if (closed) remount(page)`
⑦ 页面级「手风琴」不参与返回拦截：show 表达式里不得出现 `showUrgencyPicker` / `showDeliveryPicker`
   （那是页内展开，不是弹层，与 App 一致）
⑧ app.wxss 有 `.back-guard-sentinel`（1rpx + 透明 + pointer-events:none）
⑨ 变异自证：删守卫 / 删一个标志 / 改错收层方法名 / 删 closing 声明 / 去掉递归闸 / 
   把守卫 z-index 提到弹层之上 —— 每条都必须让判据打红

用法: python other/tests/verify_mp_back_guard.py   （exit 0 = 全部通过）
"""
import io
import os
import re
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MP = os.path.join(REPO, "mobile_apps", "h_n_print")

PAGES = [
    ("index", "pages/index/index"),
    ("me", "pages/me/me"),
    ("user-orders", "pages/user-orders/user-orders"),
    ("recent-orders", "pages/recent-orders/recent-orders"),
]
NO_GUARD_PAGES = ["pages/authorized-users/authorized-users", "pages/bind/bind"]

GUARD_ZINDEX_MAX = 100          # .modal-mask = 1000，守卫必须压在最下面
FORBIDDEN_FLAGS = ["showUrgencyPicker", "showDeliveryPicker"]


def p(rel):
    return os.path.join(MP, rel.replace("/", os.sep))


def read(path):
    with io.open(path, "r", encoding="utf-8", newline="") as f:
        return f.read()


def load_texts():
    t = {"js": {}, "wxml": {}, "guard": read(os.path.join(MP, "utils", "back_guard.js")),
         "wxss": read(os.path.join(MP, "app.wxss"))}
    for _name, rel in PAGES:
        t["js"][rel] = read(p(rel + ".js"))
        t["wxml"][rel] = read(p(rel + ".wxml"))
    t["wxml_noguard"] = {rel: read(p(rel + ".wxml")) for rel in NO_GUARD_PAGES}
    return t


# ── 解析 ──────────────────────────────────────────────────────────────────
def guard_block(wxml):
    """取 wxml 里守卫容器整块文本（`<page-container …>…</page-container>`），没有则 None。"""
    idx = wxml.find("<page-container")
    if idx < 0:
        return None
    end = wxml.find("</page-container>", idx)
    if end < 0:
        return wxml[idx:]
    return wxml[idx:end + len("</page-container>")]


def show_flags(block):
    m = re.search(r'show="\{\{!backGuardHold && \((.*?)\)\}\}"', block, re.S)
    if not m:
        return None
    return [x.strip() for x in m.group(1).split("||") if x.strip()]


def back_layers(js):
    m = re.search(r"const BACK_LAYERS = \[(.*?)\n\]", js, re.S)
    if not m:
        return None
    body = m.group(1)
    items = []
    for mm in re.finditer(r"\{([^}]*)\}", body):
        seg = mm.group(1)
        f = re.search(r"flag:\s*'([^']+)'", seg)
        c = re.search(r"close:\s*'([^']+)'", seg)
        cl = re.search(r"closing:\s*'([^']+)'", seg)
        if f and c:
            items.append((f.group(1), c.group(1), cl.group(1) if cl else None))
    return items


def data_has(js, field):
    return re.search(r"^\s{2,}" + re.escape(field) + r":", js, re.M) is not None


def method_def(js, name):
    return re.search(r"^\s+" + re.escape(name) + r"\s*\(", js, re.M) is not None


# ── 判据 ──────────────────────────────────────────────────────────────────
def evaluate(t):
    fails = []

    def bad(msg):
        fails.append(msg)

    # ⑥ back_guard.js 三条硬闸
    g = t["guard"]
    if "function _flag(" not in g or "path.indexOf('.') < 0" not in g:
        bad("⑥ utils/back_guard.js 缺少嵌套路径取值 _flag（pv.visible 这种要能取到）")
    if "if (page.data.backGuardHold) return false" not in g:
        bad("⑥ utils/back_guard.js 少了递归闸 `if (page.data.backGuardHold) return false`"
            "（代码关容器同样触发 beforeleave，没有它就一次返回连关两层）")
    m = re.search(r"function leave\(page, layers\) \{(.*?)\n\}", g, re.S)
    if not m:
        bad("⑥ utils/back_guard.js 没有 leave(page, layers)")
    else:
        body = m.group(1)
        gate = body.find("page.data.backGuardHold")
        close = body.find("closeTopLayer(")
        if gate < 0 or close < 0 or gate > close:
            bad("⑥ leave() 里递归闸必须出现在 closeTopLayer 之前")
        if "if (closed) remount(page)" not in body:
            bad("⑥ leave() 收完一层后必须 remount（微信不会自己重新打开容器，不重挂第二层就漏过去）")
    if "function remount(" not in g or g.count("backGuardHold: true") < 1:
        bad("⑥ utils/back_guard.js 缺少 remount 的 backGuardHold 假推")

    # ⑧ app.wxss 占位样式
    w = t["wxss"]
    m = re.search(r"\.back-guard-sentinel\s*\{(.*?)\}", w, re.S)
    if not m:
        bad("⑧ app.wxss 缺少 .back-guard-sentinel 占位样式")
    else:
        body = m.group(1)
        for token in ("width: 1rpx", "height: 1rpx", "pointer-events: none"):
            if token not in body:
                bad("⑧ .back-guard-sentinel 缺少 `%s`" % token)

    # ④ 三个无弹层页面不得有守卫
    for rel, text in t["wxml_noguard"].items():
        if "<page-container" in text:
            bad("① %s.wxml 没有任何弹层，不该挂返回键守卫" % rel)

    for name, rel in PAGES:
        js = t["js"][rel]
        wxml = t["wxml"][rel]

        # ① 恰好一个守卫，且在文件末尾
        cnt = wxml.count("<page-container")
        if cnt != 1:
            bad("① %s.wxml 应有且只有一个 <page-container> 守卫，实际 %d 个" % (name, cnt))
            continue
        tail = wxml[wxml.find("<page-container"):]
        # 守卫块之后只允许空白与注释
        after = tail[tail.find("</page-container>"):].replace("</page-container>", "", 1)
        stripped = re.sub(r"<!--.*?-->", "", after, flags=re.S).strip()
        if stripped:
            bad("① %s.wxml 的守卫必须放在文件最末尾（之后只允许空行/注释），后面还有内容：%r"
                % (name, stripped[:60]))

        block = guard_block(wxml)
        # ② 属性齐全
        for token in ('duration="0"', 'overlay="{{false}}"',
                      'bind:beforeleave="onBackGuardLeave"', "back-guard-sentinel"):
            if token not in block:
                bad("② %s.wxml 守卫缺少 `%s`" % (name, token))
        mz = re.search(r'z-index="(\d+)"', block)
        if not mz:
            bad("② %s.wxml 守卫必须显式写 z-index（压在 .modal-mask 之下）" % name)
        elif int(mz.group(1)) > GUARD_ZINDEX_MAX:
            bad("② %s.wxml 守卫 z-index=%s 必须在 %d 及以下（否则盖住弹层、挡住点击）"
                % (name, mz.group(1), GUARD_ZINDEX_MAX))
        mcs = re.search(r'custom-style="([^"]*)"', block)
        if not mcs or "pointer-events: none" not in mcs.group(1) or "transparent" not in mcs.group(1):
            bad("② %s.wxml 守卫 custom-style 必须同时有 background:transparent 与 pointer-events:none"
                % name)

        # ③ wxml 标志集合 == js BACK_LAYERS（且顺序一致）
        flags = show_flags(block)
        layers = back_layers(js)
        if flags is None:
            bad("② %s.wxml 守卫 show 必须写成 {{!backGuardHold && (… || …)}}" % name)
        if layers is None:
            bad("③ %s.js 缺少 `const BACK_LAYERS = [...]`" % name)
        if flags is not None and layers is not None:
            lf = [f for f, _c, _cl in layers]
            if flags != lf:
                bad("③ %s 的 show 标志与 BACK_LAYERS 不一致：\n     wxml=%s\n     js  =%s"
                    % (name, flags, lf))
            # ⑦ 手风琴不参与
            for ff in FORBIDDEN_FLAGS:
                if ff in flags:
                    bad("⑦ %s 的守卫表达式里出现了手风琴标志 %s（页内展开不是弹层，与 App 一致不收）"
                        % (name, ff))
            # ④ 收层方法真实存在 / closing 在 data 里声明
            for flag, close, closing in layers:
                if not method_def(js, close):
                    bad("④ %s.json/js 里没有收层方法 %s（flag=%s）—— 按返回时这层永远收不掉"
                        % (name, close, flag))
                if closing and not data_has(js, closing):
                    bad("④ %s 的 BACK_LAYERS 引用 closing=%s，但 data 里没有这个字段（笔误就会连按重复收）"
                        % (name, closing))
                if flag.count(".") > 1:
                    bad("④ %s 的 flag=%s 嵌套过深（_flag 只支持一层）" % (name, flag))

        # ⑤ data 闸 + methods 处理器
        if not data_has(js, "backGuardHold"):
            bad("⑤ %s.js 的 data 里缺少 backGuardHold: false（守卫 show 表达式依赖它）" % name)
        if not method_def(js, "onBackGuardLeave"):
            bad("⑤ %s.js 缺少 onBackGuardLeave()（wxml 的 bind:beforeleave 指向它）" % name)
        elif not re.search(r"onBackGuardLeave\(\)\s*\{\s*return backGuard\.leave\(this, BACK_LAYERS\)\s*\}",
                           js, re.S):
            bad("⑤ %s.js 的 onBackGuardLeave 必须只有一行 `return backGuard.leave(this, BACK_LAYERS)`" % name)
        if "require('../../utils/back_guard')" not in js:
            bad("⑤ %s.js 没有 require utils/back_guard" % name)

    return fails


def main():
    t = load_texts()
    fails = evaluate(t)

    if fails:
        print("── 失败 %d 项 ──" % len(fails))
        for f in fails:
            print("  [FAIL] " + f)
    else:
        print("[PASS] ① 四个页面各一个守卫且在文件末尾；无弹层页面没有守卫")
        print("[PASS] ② 守卫属性齐全（z-index 压在 .modal-mask 之下、透明、不挡点击）")
        print("[PASS] ③ 守卫 show 标志与 JS BACK_LAYERS 同集合同顺序")
        print("[PASS] ④ 每个收层方法真实存在、closing 字段都在 data 里")
        print("[PASS] ⑤ 每页 data.backGuardHold + onBackGuardLeave 接线")
        print("[PASS] ⑥ back_guard.js 三条硬闸（嵌套取值/递归闸/收完重挂）")
        print("[PASS] ⑦ 手风琴（urgency/delivery）不参与返回拦截")
        print("[PASS] ⑧ .back-guard-sentinel 占位样式")

    # ── 变异自证 ──
    print("\n-- 变异自证（每条都必须把判据打红）--")
    muts = []

    def mut(name, fn):
        t2 = {"js": dict(t["js"]), "wxml": dict(t["wxml"]), "guard": t["guard"],
              "wxss": t["wxss"], "wxml_noguard": dict(t["wxml_noguard"])}
        fn(t2)
        muts.append((name, t2))

    def drop_guard(t2):
        t2["wxml"]["pages/index/index"] = re.sub(r"<page-container.*?</page-container>\s*$", "",
                                                 t2["wxml"]["pages/index/index"], flags=re.S)

    def drop_flag(t2):
        t2["wxml"]["pages/me/me"] = t2["wxml"]["pages/me/me"].replace("cropVisible || ", "", 1)

    def break_close(t2):
        t2["js"]["pages/user-orders/user-orders"] = t2["js"]["pages/user-orders/user-orders"].replace(
            "close: 'onClosePricePop'", "close: 'onClosePricePopTypo'", 1)

    def break_closing(t2):
        t2["js"]["pages/index/index"] = t2["js"]["pages/index/index"].replace(
            "closing: 'rangePickerClosing'", "closing: 'rangePickerClosingTypo'", 1)

    def drop_gate(t2):
        t2["guard"] = t2["guard"].replace("if (page.data.backGuardHold) return false", "", 1)

    def drop_remount(t2):
        t2["guard"] = t2["guard"].replace("if (closed) remount(page)", "", 1)

    def raise_z(t2):
        t2["wxml"]["pages/recent-orders/recent-orders"] = t2["wxml"][
            "pages/recent-orders/recent-orders"].replace('z-index="1"', 'z-index="99999"', 1)

    def guard_not_last(t2):
        t2["wxml"]["pages/index/index"] += "\n<view class=\"tail-after-guard\"></view>\n"

    def accordion_in(t2):
        t2["wxml"]["pages/index/index"] = t2["wxml"]["pages/index/index"].replace(
            "showPreview || ", "showUrgencyPicker || showPreview || ", 1)

    mut("删掉 index 页的守卫容器", drop_guard)
    mut("从 me 页守卫表达式里删掉一个弹层标志", drop_flag)
    mut("把收层方法名改错（模拟方法改名/搬走）", break_close)
    mut("把 closing 标志写错（模拟字段笔误）", break_closing)
    mut("去掉 back_guard.js 的递归闸", drop_gate)
    mut("收完不重挂守卫（微信不会自己重新打开容器）", drop_remount)
    mut("把守卫 z-index 提到弹层之上", raise_z)
    mut("在守卫之后又加内容（守卫不在末尾）", guard_not_last)
    mut("把手风琴标志塞进守卫表达式", accordion_in)

    bad = 0
    for name, t2 in muts:
        got = len(evaluate(t2)) > 0
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
