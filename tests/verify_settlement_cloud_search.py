# -*- coding: utf-8 -*-
"""验收 · 结算页「云端订单明细」四项交互（2026-11 批次）

覆盖任务 #6 / #4 / #5 / #9 里**能靠源码结构钉住**的部分（行为细节见同批
tests/verify_settlement_order_ux.js，那里用 Node+vm 真跑页面函数）：

  #6  删除「本月结论」卡：不再有 #dashVerdict 锚点 / dashVerdictHTML 函数 / 「本月结论」文案
  #4  订单明细搜索：输入即筛、可清空、命中高亮（转义后包 mark.hit）、只筛已加载订单，
      不改变 KPI / 图表 / 页脚统计口径
  #5  价格气泡：金额可点、两种复制入口、单行 `订单号 — ¥x.xx`、详细复制 14 短横线分节
  #9  状态 tooltip：标记人 / status_reason / 通用文案三支；timeout=超时；订正白名单不变

用法：python tests/verify_settlement_cloud_search.py
exit 0 = 全部通过。
"""
import io
import os
import re
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HTML = os.path.join(REPO, "local_print_tool", "finance", "settlement.html")
ok_all = True


def check(name, cond, detail=""):
    global ok_all
    if not cond:
        ok_all = False
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


def body(fn_name):
    """取一个函数的源码片段（从 function 名开始，按大括号配平）。"""
    m = re.search(r"function\s+" + re.escape(fn_name) + r"\s*\(", src)
    if not m:
        return ""
    i = src.find("{", m.end())
    if i < 0:
        return ""
    depth = 0
    for j in range(i, len(src)):
        c = src[j]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return src[m.start(): j + 1]
    return src[m.start():]


src = open(HTML, encoding="utf-8").read()

# ══════════════════════════════════════════════════════════════
# #6 删除「本月结论」卡
# ══════════════════════════════════════════════════════════════
print("=== #6 删除本月结论卡 ===")

check("renderDash 不再挂 #dashVerdict 锚点", 'id="dashVerdict"' not in src)
check("dashVerdictHTML 函数已删除（不留死函数）", not re.search(r"function\s+dashVerdictHTML\s*\(", src))
verdict_lines = [ln.strip() for ln in src.splitlines() if "本月结论" in ln]
check("「本月结论」文案已从页面移除（只在删除说明注释里出现）",
      all(ln.startswith(("/*", "*", "//")) for ln in verdict_lines), verdict_lines)
check("_refreshDashSummary 不再重绘 #dashVerdict", "#dashVerdict" not in body("_refreshDashSummary"))
check("删卡后 ustate 仍保留给状态条 / 入场动画",
      "var ustate = usageStateOfMonth(currentKey);" in src
      and "dashBannerHTML(r, m, ustate)" in src
      and "playSettledEntrance(" in src)
check("KPI 读数带仍在（删卡不影响 KPI）", "h += dashKpiHTML(r, totalPL, basisLabel, amortSub);" in src)

# ══════════════════════════════════════════════════════════════
# #4 订单明细搜索
# ══════════════════════════════════════════════════════════════
print("=== #4 订单明细搜索 ===")

cur = body("_currentOrderList")
check("搜索只叠加在 _currentOrderList（分页 / 列表唯一出口）",
      "_ordersSearchQuery()" in cur and "_orderSearchHit(o, q)" in cur)
check("搜索不污染口径链：_cloudFilteredOrders 里没有搜索词",
      "_ordersSearch" not in body("_cloudFilteredOrders"))

hit = body("_orderSearchHit")
check("命中判据 = 备注 + 文件名（任务指定的两个字段）",
      "o.remark" in hit and "file_name" in hit and "toLowerCase" in hit)

hl = body("_hlText")
check("高亮逐段转义后才包 mark.hit（防 XSS，只高亮命中片段）",
      "esc(" in hl and '<mark class="hit">' in hl)

check("备注行用 _hlText 渲染", "_hlText(o.remark" in src)
check("文件名用 _hlText 渲染", "_hlText(f.file_name" in src)

check("搜索框输入即筛 + 清空按钮 + 占位文案",
      'id="ordersSearchInput"' in src and 'oninput="onOrdersSearch(this.value)"' in src
      and 'onclick="onOrdersSearch(' in src and "清空搜索" in src and "搜文件名 / 备注" in src)
check("搜索词变化后回到第 1 页", "_ordersPage = 1;" in body("onOrdersSearch"))
check("重绘后恢复输入框焦点与光标",
      "selectionStart" in body("onOrdersSearch") and "focus()" in body("onOrdersSearch"))
check("两套底纹（浅色 / 深色）都定义了",
      "mark.hit" in src and "#fff3b0" in src and "rgba(255,235,130,.55)" in src)

shared = body("_renderCloudShared") or body("renderCloud")
check("页脚统计仍用 filteredOrders（搜索不改变统计口径）",
      "filteredOrders.length" in shared and "_ordersSearch" not in shared)

# ══════════════════════════════════════════════════════════════
# #5 价格气泡与两种复制
# ══════════════════════════════════════════════════════════════
print("=== #5 价格气泡与复制格式 ===")

check("金额格与文件金额格都可点（price-copy + openPriceMenu）",
      src.count('class="price-copy"') >= 2 and "openPriceMenu(event" in src)
check("气泡是 fixed 定位、有层级、点外部关闭",
      "#priceMenu { position: fixed" in src
      and "document.addEventListener('click', _priceMenuOutside, true)" in body("openPriceMenu"))
check("两个复制入口都挂到 copyPrice(false/true)",
      'onclick="copyPrice(false)"' in src and 'onclick="copyPrice(true)"' in src)
check("单行复制逐字 = `订单号 — ¥金额`（em dash U+2014 两侧各一空格）",
      "return num + ' \u2014 ' + RMB(_orderFinalCents(o));" in src)
check("详细复制以「订单号: X」开头 + 14 个短横线分节",
      "var PRICE_DASH = '--------------';" in src
      and "lines.push('订单号: ' + num); lines.push(PRICE_DASH);" in src
      and "lines.push('计费明细');" in src)
check("每个文件三行：序号+文件名 / 份数×单双面+范围 / 算式 = 实收金额",
      "lines.push((i + 1) + '. ' + String(f.file_name || ''));" in src
      and "(f.duplex === 'off' ? '单面' : '双面') + ' 范围:' + (f.page_range || '全部')" in src
      and "lines.push('   ' + calc.formula + ' = \u00a5' + formatCents(c));" in src)
check("附加服务费只在 >0 且能被派送/加急/首页解释时出现（不替人工改价编理由）",
      "if (extra > 0 && extrasEnabled && Math.abs(extra - expectedExtra) <= 1.1)" in src)
check("结尾 💰合计 = 订单实收（含附加服务费）",
      "lines.push('\U0001f4b0\u5408\u8ba1: \u00a5' + formatCents(total));" in src)
check("算式与本地 calc_cost 同形（{n}张×{单价} / 双面 a张×双面价+b张×单面价 / 多份包括号）",
      "parts.push(units.pairs + '张×' + dp.toFixed(2))" in src
      and "parts.push(units.remainder + '张×' + sp.toFixed(2))" in src
      and "if (copies > 1) formula = '(' + formula + ')×' + copies + '份';" in src)
check("算式复算价与实收不符时只写实收金额，不编算式",
      "if (calc.formula && Math.abs(calc.cost * 100 - c) <= 0.5)" in src)
check("金额取已定稿 total_price，老订单缺字段才退回子任务合计",
      "if (tp !== null && tp !== undefined && String(tp).trim() !== '') return parseCents(tp);" in src
      and "return _orderNominalAmount(o);" in body("_orderFinalCents"))
check("复制失败有兜底（textarea + execCommand），成功 toast",
      "document.execCommand('copy')" in body("_copyText") and "toast(" in body("_copyText"))

# ══════════════════════════════════════════════════════════════
# #9 状态 tooltip / timeout / 标记人
# ══════════════════════════════════════════════════════════════
print("=== #9 状态 tooltip 与标记人 ===")

tip = body("_statusTip")
check("tooltip 第一支：{标记人}在{时间}被标记为{状态}",
      "manual_status_by" in tip and "'在'" in tip and "'被标记为'" in tip)
check("tooltip 第二支：后端 status_reason", "status_reason" in tip)
check("tooltip 第三支：通用文案（不留空 title）", "当前状态：" in tip)
check("订单行与文件行的状态标签都带 title",
      "esc(_statusTip(o, o.status))" in src and "esc(_statusTip(f, f.status))" in src)

check("timeout = 超时，琥珀色，只做展示/筛选",
      "timeout: '超时'" in src and "timeout: 'st-timeout'" in src
      and "timeout: '#F59E0B'" in src and ".tag.st-timeout" in src)
check("timeout 计入异常分组（KPI 异常计数不丢单）",
      "+ (st.timeout || 0)" in body("_statusGroups"))
check("状态筛选下拉静态补 timeout 入口（后端没下发时也能选）",
      'if (!statusCount.timeout) h += \'<option value="timeout"' in src
      and '>超时 (0)</option>' in src)

fixable = re.search(r"var ORDER_FIXABLE_STATUSES = \[[^\]]*\];", src)
check("人工订正白名单未变（只允许终态）",
      fixable is not None and fixable.group(0)
      == "var ORDER_FIXABLE_STATUSES = ['sent', 'failed', 'abandoned', 'canceled', 'rejected'];")
check("标记人下拉用成员名单名字，不用云端昵称",
      "getMembers()" in body("_memberNames") and "nickname" not in body("_memberNames"))
check("标记人默认取上次选择，失效回退第一个",
      "MANUAL_BY_KEY" in src and "localStorage.getItem(MANUAL_BY_KEY)" in body("_manualStatusBy"))
check("订正请求体带 by（后端按 ≤32 字符校验）",
      "body: JSON.stringify({ order_id: String(oid), status: status, by: by })" in src)
check("订正成功后本地落地 manual_status_by / manual_status_at（tooltip 立即生效）",
      "o.manual_status_by = by;" in src and "o.manual_status_at = _pickManualAt(res) || _nowStamp();" in src)
check("服务器没返回时间时用本地时间兜底，不显示 undefined",
      "_pickManualAt" in src and "_nowStamp" in src and "'未知时间'" in src)

# ══════════════════════════════════════════════════════════════
# 文案规矩
# ══════════════════════════════════════════════════════════════
print("=== 文案 ===")

new_copy = [
    "搜文件名 / 备注",
    "点击复制价格",
    "复制价格",
    "复制详细价格",
    "标记人",
    "未设置标记人",
    "当前状态：",
]
check("新增 UI 文案不含 markdown 星号", all("*" not in s for s in new_copy))

print()
print("====================================================")
if ok_all:
    print("云端订单明细四项交互（结构）：全部通过 ✅")
    sys.exit(0)
else:
    print("云端订单明细四项交互（结构）：有断言不通过 ❌")
    sys.exit(1)
