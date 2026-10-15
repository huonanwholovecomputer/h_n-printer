// utils/pages.js — 「页码范围 → 有效页数」的**唯一实现**（小程序侧）
//
// 为什么必须只有一份：这个函数的返回值直接决定「提交前显示价」与「订单里显示的张数」，
// 必须与后端 `app.py::_count_pages_in_range`（以及本地工具 `printer_config._parse_range_parts`）
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

/* 对齐 Python `int()`：允许前后空白，其余必须是十进制整数；否则返回 null。 */
function _rangeInt(s) {
  const t = String(s).trim()
  if (!/^[+-]?[0-9]+$/.test(t)) return null
  const v = Number(t)
  return Number.isSafeInteger(v) ? v : null
}

/* 有效打印页数：页码范围（如 '1-3,5'）过滤后的页数；空范围返回总页数。

   ⚠ 必须与后端 `_count_pages_in_range` / 本地 `_parse_range_parts` **严格同口径**，
   否则「提交前显示价」与「提交后实收价」会对不上（详见本文件顶部注释）。
   非空但全部无效 → 返回 total（后端同样退回全打印），保证与实收一致。 */
function countPagesInRange(pageRange, total) {
  if (!pageRange || !String(pageRange).trim()) return total
  const pages = new Set()
  String(pageRange).replace(/ /g, '').replace(/[、，；]/g, ',').split(',').forEach(rawPart => {
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
        for (let p = Math.max(1, start); p <= Math.min(end, total); p++) pages.add(p)
      } else if (start > end && a.trim().length > 1) {
        const prefix = _rangeInt(a.trim().slice(0, -1))
        const last = _rangeInt(a.trim().slice(-1))
        if (prefix !== null && last !== null && prefix < end) {
          for (let p = Math.max(last, 1); p <= Math.min(end, total); p++) pages.add(p)
          if (prefix >= 1 && prefix <= total) pages.add(prefix)
        }
      }
    } else {
      const n = _rangeInt(part)
      if (n !== null && n >= 1 && n <= total) pages.add(n)
    }
  })
  return pages.size || total
}

module.exports = { countPagesInRange }
