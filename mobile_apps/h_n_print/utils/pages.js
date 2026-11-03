// utils/pages.js — 「页码范围 → 有效页数 / 页集合 / 计费张数」的**唯一实现**（小程序侧）
//
// 为什么必须只有一份：这些函数的返回值直接决定「提交前显示价」与「订单里显示的张数」，
// 必须与后端 `app.py::_parse_page_range_set / _count_pages_in_range / _charge_units`
// （以及本地工具 `printer_config._parse_range_parts` / `manual_duplex.charge_units`）
// **严格同口径**，否则顾客看到的价与实收价会对不上。
// 历史上它在本目录外被复制了三份（pages/index/index.js、pages/me/me.js、
// pages/user-orders/user-orders.js），其中两份副本没跟上修复，同一输入算出不同页数
// （实测 41 组输入里 11 组发散）。**请勿再在页面里写第二份**——守卫脚本：
// `tests/verify_count_range_pages_single_source.py`。
//
// 旧副本的三处偏差（已随副本删除，勿在新代码里复现）：
//   1. 把空白替换成逗号（后端是**删除**）→ "1 - 3" 只认第 1、3 两页，后端按 3 页计费；
//   2. 用 parseInt 解析（后端 int() 严格）→ "3a" 认成第 3 页，后端判整段无效退回**全份**；
//   3. 段内 '-' 用 split('-') 全切（后端 split('-', 1) 只切一刀）→ "1-2-3" 认成 1-2。
// 另注：两端对「Unicode 十进制数字」的处理**仍不一致**（Python int() 认全角 '３'、
// JS 的 ASCII 正则不认），已作为 🟡-D5b 记录在 docs/修复计划_2026-10.md，尚未统一。
//
// 2026-11-02 新增（契约 `hn_simplex_blank_contract.md`）：
//   · pageRangeSet —— 页码范围 → 页集合（对齐后端 `_parse_page_range_set`）。
//     「封面单独打印」必须判断 X ⊆ S，只有集合不够、要页；页面里不许再写解析。
//   · chargeUnits —— S 按「页是否属于 X」切连续段算张数（对齐后端 `_charge_units` /
//     本地 `manual_duplex.charge_units`，契约第 2 节）。
//   · rangePages —— 页集合压回 "1,2,16-23" 形式的字符串（提交/回显用）。

/* 对齐 Python `int()`：允许前后空白，其余必须是十进制整数；否则返回 null。 */
function _rangeInt(s) {
  const t = String(s).trim()
  if (!/^[+-]?[0-9]+$/.test(t)) return null
  const v = Number(t)
  return Number.isSafeInteger(v) ? v : null
}

/* 页码范围字符串 → 页集合（`total` 未知时传 0）。

   与后端 `app.py::_parse_page_range_set` **逐条对齐**：
     · 空 / 纯空白 → **空集合**（注意：不是"全部页"，全份口径由 countPagesInRange 补）；
     · 去空格、`、，；` → `,`；
     · 每段 `split('-', 1)` 只切一刀，两半都要能 `int()`；否则**整段忽略**；
     · `a < b` → `[max(a,1), min(b,total)]`；
     · `a > b` 且 `a` 长度 > 1 → 智能拆分：`'23-4'` → `{2,3,4}`；
     · 越界页忽略。 */
function pageRangeSet(rangeStr, total) {
  const out = new Set()
  if (rangeStr === undefined || rangeStr === null || !String(rangeStr).trim()) return out
  const totalN = Number(total) || 0
  String(rangeStr).replace(/ /g, '').replace(/[、，；]/g, ',').split(',').forEach(rawPart => {
    const part = (rawPart || '').trim()
    if (!part) return
    const dash = part.indexOf('-')
    if (dash >= 0) {
      const a = part.slice(0, dash)
      const b = part.slice(dash + 1)     // 只切一刀，对齐后端 split('-', 1)
      const start = _rangeInt(a)
      const end = _rangeInt(b)
      if (start === null || end === null) return
      if (start < end) {
        // 收窄迭代区间：结果与「全区间遍历 + 逐个过滤」完全一致，但防超长范围（如 1-999999999）卡死
        for (let p = Math.max(1, start); p <= Math.min(end, totalN); p++) out.add(p)
      } else if (start > end && a.trim().length > 1) {
        const prefix = _rangeInt(a.trim().slice(0, -1))
        const last = _rangeInt(a.trim().slice(-1))
        if (prefix !== null && last !== null && prefix < end) {
          for (let p = Math.max(last, 1); p <= Math.min(end, totalN); p++) out.add(p)
          if (prefix >= 1 && prefix <= totalN) out.add(prefix)
        }
      }
    } else {
      const n = _rangeInt(part)
      if (n !== null && n >= 1 && n <= totalN) out.add(n)
    }
  })
  return out
}

/* 有效打印页数：页码范围（如 '1-3,5'）过滤后的页数；空范围返回总页数。

   ⚠ 必须与后端 `_count_pages_in_range` / 本地 `_parse_range_parts` **严格同口径**，
   否则「提交前显示价」与「提交后实收价」会对不上（详见本文件顶部注释）。
   非空但全部无效 → 返回 total（后端同样退回全打印），保证与实收一致。
   实现直接复用 pageRangeSet（同一套解析），行为已由守卫脚本 B 节逐组对拍。 */
function countPagesInRange(pageRange, total) {
  const pages = pageRangeSet(pageRange, total)
  return pages.size || total
}

/* 契约第 2 节：把 S 按「该页是否属于 X」切成连续段，算双面/单面张数。
     dup_sheets    = Σ 双面段 floor(len / 2)
     single_sheets = Σ 双面段 (len % 2) + |X|
     sheets        = dup_sheets + single_sheets
   pageSet 必须是「本次实际打印的页集合」S（空集合 → 全 0）；simplexSet 是 X。
   与本地 `manual_duplex.charge_units` / 后端 `_charge_units` 同值。 */
function chargeUnits(pageSet, simplexSet) {
  const pages = []
  ;(pageSet || []).forEach(n => { if (Number.isSafeInteger(n)) pages.push(n) })
  pages.sort((a, b) => a - b)
  const simp = (simplexSet instanceof Set) ? simplexSet : new Set(simplexSet || [])
  let dupSheets = 0
  let singleSheets = 0
  // 连续段 = 页码连续 **且** X 归属相同（契约：按"该页是否属于 X"切段）。
  // 只按页码连续切会把 [2..31] 当成一个双面段，16-23 的单面页就被算成双面张数了。
  let i = 0
  while (i < pages.length) {
    const inX = simp.has(pages[i])
    let j = i
    while (j + 1 < pages.length && pages[j + 1] === pages[j] + 1 &&
           simp.has(pages[j + 1]) === inX) j++
    const len = j - i + 1
    if (inX) {
      singleSheets += len
    } else {
      dupSheets += Math.floor(len / 2)
      singleSheets += len % 2
    }
    i = j + 1
  }
  return { dupSheets, singleSheets, sheets: dupSheets + singleSheets }
}

/* 页集合 → 压缩字符串：升序去重，连续段按长度合并（≥3 页才折成 "16-23"，
   两页的 "1,2" 保持分开——与契约示例 `1,2,16-23` 同形）。空集合 → ""。 */
function rangePages(pages) {
  const uniq = []
  ;(pages || []).forEach(n => {
    const v = Number(n)
    if (!Number.isSafeInteger(v) || v < 1) return
    if (!uniq.length || uniq[uniq.length - 1] !== v) uniq.push(v)
  })
  uniq.sort((a, b) => a - b)
  // 去重后重排（对无序输入也能正确压缩）
  const dedup = []
  uniq.forEach(v => { if (!dedup.length || dedup[dedup.length - 1] !== v) dedup.push(v) })
  const out = []
  let i = 0
  while (i < dedup.length) {
    let j = i
    while (j + 1 < dedup.length && dedup[j + 1] === dedup[j] + 1) j++
    const len = j - i + 1
    // 连续段长度 >= 2 就折成 a-b：与后端 _format_page_selection（app.py:3888）
    // 及本地 format_page_selection 同口径，避免前端回显与后端归一化后的字符串不一致
    out.push(len >= 2 ? (dedup[i] + '-' + dedup[j]) : String(dedup[i]))
    i = j + 1
  }
  return out.join(',')
}

module.exports = { countPagesInRange, pageRangeSet, chargeUnits, rangePages }
