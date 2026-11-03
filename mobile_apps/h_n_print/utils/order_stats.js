// order_stats.js — 订单详情里「张数」的兜底口径
//
// 为什么单独一个模块（而不是让 me.js / user-orders.js 直接 require utils/pages 取 chargeUnits）：
// `tests/verify_count_range_pages_single_source.py` 的变异自证要求**每个消费方文件里只有一处**
// `require('...utils/pages...')`，且那行必须原样是 `const { countPagesInRange } = require(...)`
// （脚本会删掉这一行来证明「不再 require 就得报 NOT_REQUIRED」）。多写一处 require 就会让
// 这条自证变红——所以摊纸兜底从这一层转出去，消费方仍旧只声明 countPagesInRange 一处来源。
//
// 解析/摊纸算法本身**只有一份**：utils/pages.js 的 pageRangeSet / chargeUnits。
const { pageRangeSet, chargeUnits } = require('./pages')

/* 后端没回 sheets 字段时，前端按同一口径兜底算张数：
   单面 → 每页 1 张；双面 → 「封面单面页每页 1 张 + 其余连续段两页 1 张，奇数剩 1 张」
   （契约 §2，与后端 _charge_units / 本地工具 manual_duplex.charge_units 同口径）。
   注意不能再用 Math.ceil(eff/2)：31 页里 9 页单面是 20 张，不是 16 张。
   返回 0 表示算不出来（调用方自行决定显示），绝不返回 NaN。 */
function fallbackSheets(f, effective) {
  if (!f) return 0
  const pc = Number(f.page_count) || 0
  const eff = (typeof effective === 'number' && !isNaN(effective)) ? effective : 0
  if (pc <= 0) return eff
  if (f.duplex === 'off') return eff
  const sel = pageRangeSet(f.page_range, pc)
  if (!sel.size) for (let p = 1; p <= pc; p++) sel.add(p)
  const simplex = new Set()
  pageRangeSet(f.simplex_pages, pc).forEach(p => { if (sel.has(p)) simplex.add(p) })
  const units = chargeUnits(sel, simplex)
  return units.sheets || Math.ceil(eff / 2)
}

module.exports = { fallbackSheets }
