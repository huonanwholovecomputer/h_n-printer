// utils/price_copy.js — 「复制价格 / 复制详细价格」文案的**唯一实现**（小程序侧）
//
// 为什么必须只有一份：这两个入口的文本要跟**本地打印工具**（`local_print_tool/gui.py`
// 的 Ctrl+C / Ctrl+Shift+C）逐字一致，用户会把复制出来的文字直接粘进微信/表格里对账。
// 历史上「我」页有一份、其它页面没有，于是「管理管理员 → 某人任务」子视图压根没得复制
// （2026-11-09 用户提的现场问题）。现在两处共用本文件：
//   · `pages/me`            —— 「我」页订单列表 + 「近 N 条订单」行
//   · `pages/user-orders`   —— 管理入口进来的「某人任务 / 某来源任务」子视图
// 守卫脚本：`other/tests/verify_price_copy_popconfirm.js`（跨端逐字一致 + 本文件外不得有第二份）。
//
// 口径：
//   简略单行：`{订单号} — ¥{金额}`（没有订单号就只写金额）
//   详细多行：订单号 / -×14 / 计费明细 / -×14 / 每个文件 3 行 / [附加服务费] / -×14 / 💰合计
// 金额一律取**实收**（orders.total_price / order_files.total_price）：设备回报页数后后端会重算并
// 回写这两处，前端不再自己套单价；算式（`3张×0.40 = ¥1.20`）只用来解释实收，
// 复算价与实收对不上（历史单价 / 人工改价 / 首页混排）就只写金额，绝不编一条与实收不一致的算式。

// 页码范围口径的**唯一实现**（勿在本文件再写第二份）——见 utils/pages.js 顶部注释
const { countPagesInRange } = require('./pages')

const PRICE_DASH = '-'.repeat(14)

// 单价兜底：与 pricing.json 权威值一致；`/api/pricing` 拉不到时才用（不阻塞复制）
const FALLBACK_PRICE_RULES = { simplex_price: 0.30, duplex_price: 0.40 }

function money(n) {
  return (Math.round((Number(n) || 0) * 100) / 100).toFixed(2)
}

// 单面页/双面页的摊纸张数（与后端 manual_duplex.charge_units 同口径）：
// 指定了「单面页」的那些页各占 1 张，其余页两页一张，奇数页最后一张占 1 张。
function chargeUnitsOf(eff, simplexPages) {
  const n = Number(eff) || 0
  if (simplexPages && String(simplexPages).trim()) {
    const set = {}
    String(simplexPages).split(',').forEach((part) => {
      const seg = String(part).trim()
      if (!seg) return
      const mm = seg.split('-')
      const a = parseInt(mm[0], 10)
      const b = mm.length > 1 ? parseInt(mm[1], 10) : a
      if (!isFinite(a) || !isFinite(b)) return
      for (let i = a; i <= b; i++) set[i] = true
    })
    let simplex = 0
    for (let i = 1; i <= n; i++) if (set[i]) simplex++
    const rest = Math.max(0, n - simplex)
    return { pairs: simplex + Math.floor(rest / 2), remainder: rest % 2 }
  }
  return { pairs: Math.floor(n / 2), remainder: n % 2 }
}

/* 复原 calc_cost 的算式（local_print_tool/printer_config.py:536-583）：
   - 单面：`{eff}张×{sp}`；双面：`{pairs}张×{dp}`（+`+{rem}张×{sp}`）
   - copies>1 时整体包成 `({formula})×{copies}份` */
function calcCostFormula(f, eff, rules) {
  const sp = Number(rules && rules.simplex_price) || FALLBACK_PRICE_RULES.simplex_price
  const dp = Number(rules && rules.duplex_price) || FALLBACK_PRICE_RULES.duplex_price
  const copies = Number(f.copies) || 1
  const pages = Number(eff) || 0
  if (pages <= 0) return { cost: 0, formula: '' }
  let cost = 0
  let formula = ''
  if (f.duplex === 'on') {
    const units = chargeUnitsOf(pages, f.simplex_pages)
    const parts = []
    if (units.pairs > 0) { parts.push(units.pairs + '张×' + dp.toFixed(2)); cost += units.pairs * dp }
    if (units.remainder > 0) { parts.push(units.remainder + '张×' + sp.toFixed(2)); cost += units.remainder * sp }
    formula = parts.join('+')
  } else {
    formula = pages + '张×' + sp.toFixed(2)
    cost = pages * sp
  }
  cost = cost * copies
  if (copies > 1) formula = '(' + formula + ')×' + copies + '份'
  return { cost: cost, formula: formula }
}

// 逐文件有效页数：页面预处理过就用它（effectivePages），后端给了 effective_pages 就用它，
// 都没有才按页码范围现算（口径仍是 utils/pages 那一份）。
function effectivePagesOf(f) {
  if (typeof f.effectivePages === 'number') return f.effectivePages
  if (typeof f.effective_pages === 'number') return f.effective_pages
  return countPagesInRange(f.page_range, f.page_count)
}

// 订单实收合计（两位小数）
function orderTotal(o) {
  return Math.round((Number(o && o.total_price) || 0) * 100) / 100
}

// 单行：`{订单号} — ¥{金额}`
function priceLine(o) {
  const amount = '¥' + orderTotal(o).toFixed(2)
  const no = String((o && o.order_number) || '').trim()
  return no ? (no + ' — ' + amount) : amount
}

// 详细多行（与 local_print_tool/gui.py::_on_copy_detail 同结构）
function priceDetailLines(o, rules) {
  const lines = []
  const no = String(o.order_number || '').trim()
  if (no) {
    lines.push('订单号: ' + no)
    lines.push(PRICE_DASH)
  }
  lines.push('计费明细')
  lines.push(PRICE_DASH)
  const total = orderTotal(o)
  let baseTotal = 0
  let itemNo = 0
  ;(o.files || []).forEach((f) => {
    itemNo++
    const amount = Math.round((Number(f.total_price) || 0) * 100) / 100
    baseTotal += amount
    const copies = Number(f.copies) || 1
    const eff = effectivePagesOf(f)
    lines.push(itemNo + '. ' + (f.original_name || f.file_name || '未知文件'))
    lines.push('   ' + copies + 'x ' + (f.duplex === 'on' ? '双面' : '单面') + ' 范围:' + (f.page_range || '全部'))
    const calc = calcCostFormula(f, eff, rules)
    // 算式复算的价与实收一致才给算式；对不上只写实收金额（复制出去的数字必须就是页面上的数字）
    if (calc.formula && Math.abs(calc.cost - amount) <= 0.005) {
      lines.push('   ' + calc.formula + ' = ¥' + amount.toFixed(2))
    } else {
      lines.push('   ¥' + amount.toFixed(2))
    }
  })
  // 附加服务费 = 实收 − 文件小计（派送 + 加急 + 首页）。只在这一单真的勾了附加服务、
  // 且残差与「文件小计 × 派送百分比 + 加急 + 首页」对得上时才写这一行：
  // 人工改过价的历史单如果把差额写成「附加服务费」，等于替操作员编了一条不存在的收费理由。
  const extra = Math.round((total - baseTotal) * 100) / 100
  const extrasEnabled = !!(Number(o.delivery_enabled) || Number(o.urgency_price) > 0 || Number(o.cover_page))
  const deliveryCost = Number(o.delivery_enabled)
    ? Math.round(baseTotal * (Number(o.delivery_percentage) || 0)) / 100
    : 0
  const expectedExtra = Math.round((deliveryCost
    + (Number(o.urgency_price) || 0)
    + (Number(o.cover_page) ? (Number(o.cover_page_price) || 0) * Number(o.cover_page) : 0)) * 100) / 100
  if (extra > 0 && extrasEnabled && Math.abs(extra - expectedExtra) <= 0.011) {
    lines.push('附加服务费: ¥' + extra.toFixed(2))
  }
  lines.push(PRICE_DASH)
  lines.push('💰合计: ¥' + total.toFixed(2))
  return lines
}

// 单价取 /api/pricing（pricing.json 权威，随后端改价生效）；拉不到用兜底常量，不阻塞复制。
// 依赖注入 request/CONFIG：本文件不直接碰 wx.request，页面与验收脚本都能用。
function createPriceRulesGetter(request, CONFIG) {
  let cache = null
  return function getPriceRules(cb) {
    if (cache) { cb(cache); return }
    const fallback = {
      simplex_price: FALLBACK_PRICE_RULES.simplex_price,
      duplex_price: FALLBACK_PRICE_RULES.duplex_price,
    }
    request({
      url: CONFIG.BASE_URL + '/api/pricing',
      method: 'GET',
      success: (res) => {
        let rules = fallback
        if (res.statusCode === 200 && res.data && res.data.success && res.data.pricing) {
          const p = res.data.pricing
          rules = {
            simplex_price: p.simplex_price != null ? Number(p.simplex_price) : FALLBACK_PRICE_RULES.simplex_price,
            duplex_price: p.duplex_price != null ? Number(p.duplex_price) : FALLBACK_PRICE_RULES.duplex_price,
          }
        }
        cache = rules
        cb(rules)
      },
      fail: () => { cache = fallback; cb(fallback) },
    })
  }
}

module.exports = {
  PRICE_DASH,
  FALLBACK_PRICE_RULES,
  money,
  chargeUnitsOf,
  calcCostFormula,
  effectivePagesOf,
  orderTotal,
  priceLine,
  priceDetailLines,
  createPriceRulesGetter,
}
