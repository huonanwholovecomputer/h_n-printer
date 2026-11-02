# -*- coding: utf-8 -*-
"""验收 · 移动端「提交打印」页入场动画接线（2026-11-01 用户反馈）

现场（用户截图指的那一行）：打印页上「🔒 文件保留 遵从服务器管理（15天后自动删除）」这一行
**没有入场动画** —— 上面几张卡片逐个淡入上浮，它和提交按钮却是"一开始就摆在那儿"。
根因：入场是"白名单驱动"的两段式——
  · 元素静态带 `card-preload`（opacity:0）→ 到点由 JS 把类换成 `entrance-play`（播 CSS 动画）；
  · 白名单是 `PRINT_ENTRANCE`（APP，print.js）/ `_scheduleEntrance()` 的 steps（小程序，index.js）。
这一行当初是"低存在感入口"，两段都没进白名单 ⇒ 永远不播，也永远是可见的（因为没带 card-preload）。

判据（两端各一组）：
  A. 这一行确实进了入场表，且排在管理员开关之后、提交按钮之前；
  B. **一致性**：表里每个 id 都在 HTML 里存在、且都带 `card-preload`（否则那张卡片永远不出现，
     这是本 bug 的镜像错误）；HTML 里带 `card-preload` 的元素也都在表里（否则它永远 opacity:0）；
  C. 时刻单调递增、清理超时 > 最后一个槽位 + 动画时长（免得清早了在切 tab 时重放）。
  D. 变异自证：把两端的改动各自改回修复前形态，对应判据必须变红。

用法：python tests/verify_mobile_print_entrance.py     （exit 0 = 全部通过）
"""
import io
import os
import re
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_HTML = os.path.join(REPO, "mobile_apps", "android_app", "www", "index.html")
APP_JS = os.path.join(REPO, "mobile_apps", "android_app", "www", "print.js")
WX_WXML = os.path.join(REPO, "mobile_apps", "h_n_print", "pages", "index", "index.wxml")
WX_JS = os.path.join(REPO, "mobile_apps", "h_n_print", "pages", "index", "index.js")

ANIM_SEC = 0.5          # cardFadeIn / entrance-play 的持续时间（两端同一份 CSS 值）

ok_all = True
results = []


def check(name, cond, detail=""):
    global ok_all
    if not cond:
        ok_all = False
    results.append((name, bool(cond), detail))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))
    return bool(cond)


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


# ═══════════════ APP（android_app/www）═══════════════

def parse_entries(js):
    """从 print.js 抠出 PRINT_ENTRANCE 表 → [(id, 秒)]。"""
    m = re.search(r"const PRINT_ENTRANCE = \[(.*?)\n\];", js, re.S)
    if not m:
        return None
    return [(i, float(t)) for i, t in re.findall(r"\['([A-Za-z0-9_]+)',\s*([0-9.]+)\]", m.group(1))]


def tag_line_of(html, el_id):
    """返回含 id="..." 的那一行（这些卡片都是单行开标签）。"""
    for line in html.splitlines():
        if f'id="{el_id}"' in line:
            return line
    return ""


def audit_app(html, js):
    """APP 端判据。返回 [(判据名, 是否通过, 细节)]。"""
    out = []

    def add(name, cond, detail=""):
        out.append((name, bool(cond), detail))

    entries = parse_entries(js)
    if entries is None:
        add("print.js 能解析出入场表 PRINT_ENTRANCE", False, "没找到 const PRINT_ENTRANCE = [...]")
        return out
    add("print.js 能解析出入场表 PRINT_ENTRANCE", True, f"{len(entries)} 条")
    ids = [i for i, _ in entries]

    # A. 文件保留那一行进了表，且位置在管理员开关之后 / 提交按钮之前
    add("APP：privacyEntryRow 在入场表里（这一行不再「裸奔」）",
        "privacyEntryRow" in ids, str(ids))
    if "privacyEntryRow" in ids and "adminPrintSection" in ids and "submitArea" in ids:
        add("APP：入场顺序 = 管理员开关 → 文件保留 → 提交按钮",
            ids.index("adminPrintSection") < ids.index("privacyEntryRow") < ids.index("submitArea"),
            str(ids[ids.index("adminPrintSection"):ids.index("submitArea") + 1]))

    # B. 一致性：表 ↔ HTML（双向）
    for el_id in ids:
        line = tag_line_of(html, el_id)
        if not line:
            add(f"APP：入场元素 id=\"{el_id}\" 在 index.html 里存在", False, "index.html 里没有这个 id")
            continue
        add(f"APP：入场元素 id=\"{el_id}\" 在 index.html 里存在", True)
        add(f"APP：id=\"{el_id}\" 带 card-preload（否则永远可见、动画形同虚设）",
            "card-preload" in line, line.strip()[:110])
    preload_ids = [m.group(1) for m in re.finditer(r'<[^>]*class="[^"]*card-preload[^"]*"[^>]*id="([A-Za-z0-9_]+)"', html)]
    stray = [i for i in preload_ids if i not in ids]
    add("APP：HTML 里带 card-preload 的元素都在入场表里（否则永远 opacity:0）",
        not stray, str(stray))

    # C. 时刻单调 + 注释计数 + 清理时机
    times = [t for _, t in entries]
    add("APP：入场时刻单调不减", all(b >= a for a, b in zip(times, times[1:])), str(times))
    mc = re.search(r"\}, (\d+)\);\s*\n\}", js)
    cleanup_ms = int(mc.group(1)) if mc else -1
    add("APP：清理入场类的超时 > 最后槽位 + 动画时长（清早了会在切 tab 时重放）",
        cleanup_ms >= (max(times) + ANIM_SEC) * 1000, f"{cleanup_ms}ms vs 需要 ≥ {(max(times) + ANIM_SEC) * 1000:.0f}ms")
    return out


# ═══════════════ 小程序（h_n_print）═══════════════

def audit_wx(wxml, js):
    out = []

    def add(name, cond, detail=""):
        out.append((name, bool(cond), detail))

    m = re.search(r"const steps = \[(.*?)\n      \]", js, re.S)
    steps = [(i, float(t)) for i, t in re.findall(r"\['([A-Za-z0-9_]+)',\s*([0-9.]+)\]", m.group(1))] if m else []
    add("小程序：index.js 能解析出 _scheduleEntrance 的 steps", bool(steps), f"{len(steps)} 条")
    keys = [k for k, _ in steps]

    # A. wxml 里这一行带入场表达式
    line = ""
    for ln in wxml.splitlines():
        if "privacy-entry-row" in ln and "bindtap=\"onOpenPrivacySheet\"" in ln:
            line = ln
            break
    add("小程序：wxml 的「文件保留」行带入场类表达式（card-preload / entrance-play 二态）",
        bool(line) and "entered.privacy" in line and "card-preload" in line and "entrance-play" in line,
        line.strip()[:120])

    # A2. steps 里有 privacy，且在 adminPrint 之后、submit 之前
    add("小程序：steps 里登记了 privacy 槽位", "privacy" in keys, str(keys))
    if "privacy" in keys and "adminPrint" in keys and "submit" in keys:
        add("小程序：入场顺序 = 管理员开关 → 文件保留 → 提交按钮",
            keys.index("adminPrint") < keys.index("privacy") < keys.index("submit"),
            str(keys[keys.index("adminPrint"):keys.index("submit") + 1]))

    # B. entered / entranceDelay 两个数据源都登记（缺 entered 就永远不播；entranceDelay 当前无消费方，
    #    这里只做「同形补齐」的一致性检查，免得后人看表以为漏了一项）
    add("小程序：data.entered 里登记了 privacy", bool(re.search(r"entered:\s*\{[^}]*privacy:\s*false", js, re.S)))
    add("小程序：data.entranceDelay 里也同形补齐了 privacy（无消费方，只为一致性）",
        bool(re.search(r"entranceDelay:\s*\{[^}]*\n\s*privacy:", js)))

    # C. 时刻单调 + 清理时机
    times = [t for _, t in steps]
    add("小程序：入场时刻单调不减", all(b >= a for a, b in zip(times, times[1:])), str(times))
    mc = re.search(r"this\.setData\(\{ entranceDone: true \}\)\s*\n\s*\}, (\d+)\)", js)
    cleanup_ms = int(mc.group(1)) if mc else -1
    add("小程序：清理入场类的超时 > 最后槽位 + 动画时长",
        cleanup_ms >= (max(times) + ANIM_SEC) * 1000,
        f"{cleanup_ms}ms vs 需要 ≥ {(max(times) + ANIM_SEC) * 1000:.0f}ms")
    return out


print("=== A. APP（android_app/www/index.html + print.js）===")
app_html, app_js = read(APP_HTML), read(APP_JS)
for name, cond, detail in audit_app(app_html, app_js):
    check(name, cond, detail)

print("\n=== B. 小程序（h_n_print/pages/index）===")
wx_wxml, wx_js = read(WX_WXML), read(WX_JS)
for name, cond, detail in audit_wx(wx_wxml, wx_js):
    check(name, cond, detail)

print("\n=== C. 变异自证（改回修复前形态必须变红）===")

# C1. APP：这一行去掉 card-preload（bug 的原始形态：可见但不在白名单）
mutated = app_html.replace('class="privacy-entry-row card-preload"', 'class="privacy-entry-row"')
res = audit_app(mutated, app_js)
red = [n for n, ok, _ in res if not ok]
check("变异 1：APP 去掉 card-preload ⇒ 一致性判据变红",
      mutated != app_html and any("card-preload" in n for n in red), str(red[:2]))

# C2. APP：表里删掉这一行
mutated_js = re.sub(r"\n\s*\['privacyEntryRow', 0\.55\],.*", "", app_js)
res = audit_app(app_html, mutated_js)
red = [n for n, ok, _ in res if not ok]
check("变异 2：APP 入场表删掉 privacyEntryRow ⇒ 接线判据变红",
      mutated_js != app_js and any("privacyEntryRow 在入场表里" in n for n in red), str(red[:2]))

# C3. 小程序：wxml 去掉入场表达式（回到裸 class="privacy-entry-row"）
mutated_wxml = re.sub(r'class="privacy-entry-row \{\{.*?\}\}"', 'class="privacy-entry-row"', wx_wxml)
res = audit_wx(mutated_wxml, wx_js)
red = [n for n, ok, _ in res if not ok]
check("变异 3：小程序 wxml 去掉入场表达式 ⇒ 判据变红",
      mutated_wxml != wx_wxml and any("带入场类表达式" in n for n in red), str(red[:2]))

# C4. 小程序：steps 里删掉 privacy 槽位
mutated_js = re.sub(r"\n\s*\['privacy', 1\.05\],", "", wx_js)
res = audit_wx(wx_wxml, mutated_js)
red = [n for n, ok, _ in res if not ok]
check("变异 4：小程序 steps 删掉 privacy ⇒ 槽位判据变红",
      mutated_js != wx_js and any("登记了 privacy 槽位" in n for n in red), str(red[:2]))

print()
if ok_all:
    print("全部通过 ✅（两端「文件保留」行都已进白名单，白名单 ↔ HTML 双向一致）")
    sys.exit(0)
print("有判据变红 ❌")
sys.exit(1)
