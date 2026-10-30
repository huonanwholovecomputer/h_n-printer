# -*- coding: utf-8 -*-
"""验收 · v7.10「硒鼓按所属打印机摊销」（2026-10-30 用户需求 A）

需求原话：「硒鼓是按打印机买的 —— 例如 10 月 22 日买的那支鼓是 HP LaserJet MFP M232-M237 的，
装在 HP LaserJet 1020 上根本不能用，所以不能拿 1020 打的页数去摊这支鼓。
采购支出要加一列标明这支鼓属于哪台打印机。」
用户选定方案 A：加「所属打印机」列，且该笔硒鼓**只按这台打印机本月页数摊销**；
未指定打印机的耗材仍按全队总页数（旧数据零回归）。

判据：
  A. 静态接线 —— expense.printer_id 的读写/归一、采购表「所属打印机」列、updExpPrinter、
     切类型时清掉归属、consumableAmort 的分桶与文案。
  B. 纯函数实测（Node VM 跑**真实的** consumableAmort）：
     · printer_id 有值 ⇒ 只用该机逐日页数（pagesByDayPrinter），绝不掺全队；
     · 未指定 ⇒ 用全队 pagesByDay（旧口径，零回归）；
     · 两台机各自的鼓先进先出互不串；
     · 打印机被删 ⇒ 回落按全队页数摊 + 明确标记（不摊成 0）；
     · A4纸不受 printer_id 影响（仍按全队逐日张数 sheetsByDay）；
     · 真实算例：¥128.98 / 3000 页的 M232 鼓，本月 1020 打 1000 页、M232 打 500 页。
  C. 变异自证 —— 把「单机桶回落到全队页数」「打印机被删回落」改坏，对应断言必须转红。

用法：python tests/verify_toner_printer_attr.py
exit 0 = 全部通过。
"""
import io
import os
import subprocess
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HTML = os.path.join(REPO, "local_print_tool", "finance", "settlement.html")
ok_all = True


def check(name, cond, detail=""):
    global ok_all
    if not cond:
        ok_all = False
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


def section(t):
    print("\n=== " + t + " ===")


src = open(HTML, encoding="utf-8").read()

# ══════════════════════════════════════════════════════════════
# A. 静态接线
# ══════════════════════════════════════════════════════════════
section("A. 静态接线（printer_id 读写 / 采购表新列 / 分桶算法）")

check("expense 新字段 printer_id：migrateConfig 归一（safeId 校验，非法或空就删掉）",
      "if (e.printer_id != null && e.printer_id !== '') {" in src
      and "var _pid = safeId(e.printer_id);" in src
      and "if (_pid) e.printer_id = _pid; else delete e.printer_id;" in src
      and "} else delete e.printer_id;" in src)
check("导入净化 sanitizeStore 同样保留/归一 printer_id",
      "var _ip = safeId(r.printer_id);" in src and "if (_ip) r.printer_id = _ip; else delete r.printer_id;" in src)
check("monthFromRecords 把 printer_id 带进计算用的 expenses（否则 calcMonth 看不到归属）",
      "expenses[expenses.length - 1].printer_id = safeId(r.printer_id);" in src)
check("expensePrinterId 是读归属的唯一入口（一律 safeId）",
      "function expensePrinterId(e) { return e ? safeId(e.printer_id) : ''; }" in src)
check("printerNameOf 辅助存在（台账改名/被删时台账里查不到）",
      "function printerNameOf(id) {" in src)
check("采购表在「数量」后新增「所属打印机」列头（仅耗材列存在时）",
      "(hasConsumable ? '<th>数量</th><th>所属打印机</th>' : '')" in src)
check("行内归属格：硒鼓给下拉（未指定 + 各台机），A4纸恒为 —",
      "function expPrinterCellHTML(e, eid, kind, disabled) {" in src
      and "未指定（全队页数）" in src
      and "只有耗材需要指定" in src and "A4纸按全队用纸量摊" in src)
check("updExpPrinter：safeId 校验、空则删除字段、保存后重绘表格",
      "function updExpPrinter(id, val) {" in src
      and "if (pid) r.printer_id = pid; else delete r.printer_id;" in src
      and "save(); renderEntry();" in src)
check("切类型到 A4纸 / 普通采购时清掉 printer_id（不让旧值以后被捡起来用）",
      "if (r.kind !== 'toner') delete r.printer_id;" in src)
check("采购表空行 colspan 随新增列改成 8",
      "(hasConsumable ? 8 : 6)" in src)
check("consumableAmort：lot 带 printerId（只有硒鼓才带归属），分桶 + 单机只读 pagesByDayPrinter",
      "printerId: kind === 'toner' ? safeId(e.printer_id) : ''," in src
      and "var per = u.pagesByDayPrinter || {};" in src
      and "var gone = !!pid && !printerById(pid);" in src)
check("单机口径**不**回落全队页数（注释里写明这条禁令）",
      "绝不回落到全队 pagesByDay" in src)
check("摊销行文案体现口径：按某台机摊销 / 未指定（旧文案）/ 原打印机已删除",
      "'摊销（按 ' + pName + ' 本月 '" in src
      and "原所属打印机已删除，按全队" in src
      and "label + '摊销（本月 ' + tm.units + ' ' + unit + '）'" in src)
check("单机桶摊不掉的量单独记 uncoveredPrinter（不混进全队 uncovered）",
      "uncoveredPrinter[scope] = self.uncoveredPrinter[scope] || {};" in src)
check("区块显式提示：按机器摊销明细 / 单机无库存 / 原打印机已删除",
      "🖨 按机器摊销（只吃「这台机」本月页数）" in src
      and "没有对应这台机的硒鼓库存" in src
      and "已按全队页数" in src)
check("版本说明 v7.10 提到硒鼓归属（版本历史块）",
      ">v7.10</div>" in src and "硒鼓按所属打印机摊销" in src)

# ══════════════════════════════════════════════════════════════
# B/C. Node VM 侧
# ══════════════════════════════════════════════════════════════

HARNESS = r"""
const fs = require('fs'), vm = require('vm');
const htmlPath = process.argv[2];
const html = fs.readFileSync(htmlPath, 'utf8');
const scripts = [...html.matchAll(/<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/g)].map(m => m[1]);
const js = scripts.join('\n;\n');

const results = [];
const P = (n, c, d) => results.push([n, !!c, d == null ? '' : String(d)]);

function defOf(name) {
  const at = js.indexOf('function ' + name + '(');
  if (at < 0) return '';
  let i = js.indexOf('{', at), depth = 0;
  for (let j = i; j < js.length; j++) {
    if (js[j] === '{') depth++;
    else if (js[j] === '}') { depth--; if (!depth) return js.slice(at, j + 1); }
  }
  return '';
}
function MB() {
  return {
    console: { log() {}, warn() {}, error() {} },
    Math, JSON, Object, Array, String, Number, Boolean, Date, RegExp, Error, Promise,
    Set, Map, isNaN, isFinite, parseFloat, parseInt, encodeURIComponent, decodeURIComponent,
    DEFAULT_MEMBERS: [], DEFAULT_SCORE_CAP: 15, DEFAULT_RATE: 0.05,
    PASSWORD_KEYS: ['score', 'expense', 'income', 'transfer', 'settings', 'auth', 'orderprinter'],
    DEFAULT_PASSWORDS: {}, DEFAULT_PRICING: { simplex_price: 0.2, duplex_price: 0.3, cover_page_price: 0.1,
      delivery_percentages: {}, urgency_prices: {} },
    FINANCE_MODE: 'cloud', STORE: null, _authDevices: null,
  };
}
/* 半截沙箱：数据模型 → calcMonth 整段（含 consumableAmort）。
   打印机台账用一张测试表代替（printerById / printerNameOf 覆盖成桩），
   这样「打印机被删」可以直接用「表里没有这个 id」造出来。 */
function calcSandbox(src) {
  const sb = MB();
  vm.createContext(sb);
  const start = src.indexOf('function migrateConfig(data)');
  const end = src.indexOf('/* ==================== 保存 ====================');
  if (start < 0 || end <= start) { console.log('SNIPPET_FAIL'); process.exit(3); }
  let snippet = src.slice(start, end);
  snippet += "\nvar __P = {};";
  snippet += "\nfunction printerById(id) { return __P[String(id)] || null; }";
  snippet += "\nfunction printerNameOf(id) { var p = __P[String(id)]; return p ? String(p.name || '') : ''; }";
  snippet += "\nfunction getMember(id) { return null; }";
  snippet += "\nfunction getMembers() { return []; }";
  /* B9 的区块渲染要用的显式依赖（这些函数在「数据模型 → calcMonth」切片之外） */
  snippet += '\n' + defOf('esc');
  snippet += '\n' + defOf('fmtInt');
  snippet += '\n' + defOf('RMB');
  snippet += '\n' + defOf('RMBneg');
  snippet += '\n' + defOf('consumableAmortSectionHTML');
  snippet += "\nvar currentKey = '';";
  snippet += "\nfunction usageOfMonth(key) { return null; }";
  vm.runInContext(snippet, sb);
  return sb;
}
/* 一支鼓的采购行 */
function toner(id, date, priceYuan, pages, payer, printerId) {
  const e = { id: id, kind: 'toner', date: date, item: '硒鼓 ' + id,
              amount: priceYuan, units: pages, payer: payer || 'ma' };
  if (printerId) e.printer_id = printerId;
  return e;
}
function paper(id, date, priceYuan, sheets, payer) {
  return { id: id, kind: 'paper', date: date, item: 'A4纸 ' + id,
           amount: priceYuan, units: sheets, payer: payer || 'ma' };
}
/* 某月的用量：全队逐日页数 + 逐机逐日页数 + 全队逐日张数 */
function uMonth(pagesByDay, sheetsByDay, pagesByDayPrinter) {
  return { pagesByDay: pagesByDay || {}, sheetsByDay: sheetsByDay || {},
           pagesByDayPrinter: pagesByDayPrinter || {} };
}
function sumOf(o) { return Object.keys(o || {}).reduce((s, k) => s + (+o[k] || 0), 0); }

function runCases() {

/* ═════ B1：printer_id 有值 ⇒ 只吃该机页数 ═════ */
{
  const sb = calcSandbox(js);
  sb.__P.p1020 = { id: 'p1020', name: 'HP LaserJet 1020' };
  sb.__P.pm232 = { id: 'pm232', name: 'HP LaserJet MFP M232-M237' };
  /* 1020 打 1000 页、M232 打 500 页；M232 那支鼓 ¥128.98 / 3000 页 */
  const um = { '2026-10': uMonth({ '2026-10-10': 1000, '2026-10-12': 500 }, {},
                                { 'p1020|2026-10-10': 1000, 'pm232|2026-10-12': 500 }) };
  const e = toner('t1', '2026-10-01', 128.98, 3000, 'ma', 'pm232');
  const r = sb.consumableAmort([e], um, '2026-10');
  /* round(12898 × 500 / 3000) = round(2149.67) = 2150 */
  P('B1 M232 的鼓只按 M232 本月 500 页摊 ⇒ 2150 分（不是 6449 分）',
    r.rows.length === 1 && r.rows[0].cents === 2150 && r.rows[0].units === 500,
    JSON.stringify(r.rows));
  P('B1 未指定归属的同款鼓按全队 1500 页摊 ⇒ 6449 分',
    (function () {
      const e2 = toner('t2', '2026-10-01', 128.98, 3000, 'ma');
      const r2 = sb.consumableAmort([e2], um, '2026-10');
      return r2.rows.length === 1 && r2.rows[0].cents === 6449 && r2.rows[0].units === 1500;
    })(), '期望 6449 分');
  P('B1 摊销行文案写明「按 <机器名> 本月 500 页」',
    r.rows[0].item.indexOf('按 HP LaserJet MFP M232-M237 本月 500 页') >= 0, r.rows[0].item);
  P('B1 lots 带归属信息：scope=printer / printerId / printerName / 不是 missing',
    r.lots[0].scope === 'printer' && r.lots[0].printerId === 'pm232'
    && r.lots[0].printerName === 'HP LaserJet MFP M232-M237' && r.lots[0].printerMissing === false,
    JSON.stringify(r.lots[0]));
  P('B1 tonersByPrinter 汇总：M232 本月 500 页 / 2150 分',
    r.tonersByPrinter.pm232 && r.tonersByPrinter.pm232.monthUnits === 500
    && r.tonersByPrinter.pm232.monthCents === 2150, JSON.stringify(r.tonersByPrinter));
}

/* ═════ B2：未指定 ⇒ 全队页数（旧口径，零回归） ═════ */
{
  const sb = calcSandbox(js);
  sb.__P.p1020 = { id: 'p1020', name: 'HP LaserJet 1020' };
  const um = { '2026-10': uMonth({ '2026-10-10': 1000, '2026-10-12': 500 }, {},
                                { 'p1020|2026-10-10': 1000 }) };
  const legacy = { id: 't9', kind: 'toner', date: '2026-10-01', item: '硒鼓', amount: 300, units: 1500, payer: 'ma' };
  const r = sb.consumableAmort([legacy], um, '2026-10');
  P('B2 旧数据（没有 printer_id）：按全队 1500 页摊 ⇒ 满额 30000 分',
    r.rows.length === 1 && r.rows[0].cents === 30000 && r.rows[0].units === 1500,
    JSON.stringify(r.rows));
  P('B2 旧文案保持不变：`硒鼓摊销（本月 1500 页）`',
    r.rows[0].item === '硒鼓摊销（本月 1500 页）', r.rows[0].item);
  P('B2 旧数据不产生 printerMissing / 单机汇总',
    r.lots[0].printerMissing === false && r.lots[0].scope === 'team'
    && Object.keys(r.tonersByPrinter).length === 0, JSON.stringify(r.lots[0]));
}

/* ═════ B3：两台机各自的鼓先进先出，互不串 ═════ */
{
  const sb = calcSandbox(js);
  sb.__P.p1 = { id: 'p1', name: '机 1' };
  sb.__P.p2 = { id: 'p2', name: '机 2' };
  /* p1: 两批鼓（1000 页 ¥100 先买，2000 页 ¥200 后买）；p2: 一批 3000 页 ¥300
     本月 p1 打 1500 页、p2 打 500 页 */
  const um = { '2026-10': uMonth({ '2026-10-10': 2000 }, {},
                                { 'p1|2026-10-10': 1500, 'p2|2026-10-10': 500 }) };
  const es = [toner('a', '2026-10-01', 100, 1000, 'ma', 'p1'),
              toner('b', '2026-10-02', 200, 2000, 'mb', 'p1'),
              toner('c', '2026-10-03', 300, 3000, 'ma', 'p2')];
  const r = sb.consumableAmort(es, um, '2026-10');
  const by = {};
  r.rows.forEach(function (x) { by[x.lotId] = x; });
  /* a 用满 1000 页 ⇒ 10000；b 吃 500/2000 ⇒ round(20000×500/2000)=5000；c 吃 500/3000 ⇒ 5000 */
  P('B3 同机两台鼓先进先出：先买的 1000 页吃完，后买的只摊 500 页',
    by.a && by.a.cents === 10000 && by.b && by.b.cents === 5000, JSON.stringify(by));
  P('B3 另一台机的鼓只吃自己那 500 页（不掺 p1 的 1500 页）',
    by.c && by.c.cents === 5000 && by.c.units === 500, JSON.stringify(by.c));
  P('B3 单机汇总分别记：p1 = 1500 页 / 15000 分，p2 = 500 页 / 5000 分',
    r.tonersByPrinter.p1.monthUnits === 1500 && r.tonersByPrinter.p1.monthCents === 15000
    && r.tonersByPrinter.p2.monthUnits === 500 && r.tonersByPrinter.p2.monthCents === 5000,
    JSON.stringify(r.tonersByPrinter));
  P('B3 本月摊销合计 = 20000 分（三批之和，整数分不漂移）', r.totalCents === 20000, r.totalCents);
}

/* ═════ B4：打印机被删 ⇒ 回落按全队页数摊 + 明确标记 ═════ */
{
  const sb = calcSandbox(js);
  sb.__P.p1 = { id: 'p1', name: '机 1' };     // pm232 **不在**台账里
  const um = { '2026-10': uMonth({ '2026-10-10': 1200 }, {},
                                { 'p1|2026-10-10': 1200, 'pm232|2026-10-10': 700 }) };
  const e = toner('t1', '2026-10-01', 128.98, 3000, 'ma', 'pm232');
  const r = sb.consumableAmort([e], um, '2026-10');
  P('B4 鼓指向的打印机已删 ⇒ 按全队 1200 页摊（不是 0、也不是那台机残留的 700 页）',
    r.rows.length === 1 && r.rows[0].cents === Math.round(12898 * 1200 / 3000) && r.rows[0].units === 1200,
    JSON.stringify(r.rows));
  P('B4 行文案标出「原所属打印机已删除，按全队」',
    r.rows[0].item.indexOf('原所属打印机已删除，按全队') >= 0, r.rows[0].item);
  P('B4 lots 标 printerMissing + scope 回落 team（区块据此提示重选）',
    r.lots[0].printerMissing === true && r.lots[0].scope === 'team'
    && r.lots[0].printerRequested === 'pm232', JSON.stringify(r.lots[0]));
}

/* ═════ B5：A4纸不受 printer_id 影响 ═════ */
{
  const sb = calcSandbox(js);
  sb.__P.p1 = { id: 'p1', name: '机 1' };
  const um = { '2026-10': uMonth({ '2026-10-10': 900 },
                                { '2026-10-10': 200, '2026-10-11': 300 },
                                { 'p1|2026-10-10': 900 }) };
  const p1 = paper('p1e', '2026-10-01', 100, 1000, 'ma');
  const p2 = paper('p2e', '2026-10-01', 100, 1000, 'ma');
  p2.printer_id = 'p1';       // 就算数据里被写上了归属，A4纸也只按全队张数摊
  const r1 = sb.consumableAmort([p1], um, '2026-10');
  const r2 = sb.consumableAmort([p2], um, '2026-10');
  P('B5 A4纸仍按全队逐日张数摊：500 张 × ¥0.10 = 5000 分',
    r1.rows[0].cents === 5000 && r1.rows[0].units === 500, JSON.stringify(r1.rows));
  P('B5 A4纸即使被写了 printer_id 也不受影响（金额、单位数、lot 归属都相同）',
    r2.rows[0].cents === r1.rows[0].cents && r2.rows[0].units === r1.rows[0].units
    && r2.lots[0].printerId === '' && r2.lots[0].scope === 'team', JSON.stringify(r2.lots[0]));
  P('B5 A4纸不会被算进 tonersByPrinter（单机汇总只统计硒鼓）',
    Object.keys(r2.tonersByPrinter).length === 0, JSON.stringify(r2.tonersByPrinter));
}

/* ═════ B6：真实算例（用户给的场景） ═════ */
{
  const sb = calcSandbox(js);
  sb.__P.p1020 = { id: 'p1020', name: 'HP LaserJet 1020' };
  sb.__P.pm232 = { id: 'pm232', name: 'HP LaserJet MFP M232-M237' };
  /* 10-22 买的 M232 鼓 ¥128.98（3000 页）；本月 1020 打 1000 页、M232 打 500 页 */
  const um = { '2026-10': uMonth({ '2026-10-22': 1200, '2026-10-25': 300 }, {},
                                { 'p1020|2026-10-22': 1000, 'pm232|2026-10-25': 500 }) };
  const drum = toner('d1', '2026-10-22', 128.98, 3000, 'ma', 'pm232');
  const r = sb.consumableAmort([drum], um, '2026-10');
  P('B6 ¥128.98 / 3000 页的 M232 鼓：本月 M232 打 500 页 ⇒ 2150 分（¥21.50）',
    r.rows[0].cents === 2150, JSON.stringify(r.rows));
  const legacy = toner('d2', '2026-10-22', 128.98, 3000, 'ma');
  const r2 = sb.consumableAmort([legacy], um, '2026-10');
  P('B6 同一支鼓若「未指定」⇒ 按全队 1500 页摊 6449 分（这是需求要修掉的口径）',
    r2.rows[0].cents === 6449, JSON.stringify(r2.rows));
  P('B6 两者差额 4299 分（用户举例里"多摊了 1020 的页数"就是这部分）',
    r2.rows[0].cents - r.rows[0].cents === 4299, r2.rows[0].cents - r.rows[0].cents);
}

/* ═════ B7：单机用量超过该机鼓库存 ⇒ uncoveredPrinter（不混进全队 uncovered） ═════ */
{
  const sb = calcSandbox(js);
  sb.__P.p1 = { id: 'p1', name: '机 1' };
  const um = { '2026-10': uMonth({ '2026-10-10': 2000 }, {},
                                { 'p1|2026-10-10': 2000, 'p1020|2026-10-10': 900 }) };
  const e = toner('t1', '2026-10-01', 100, 500, 'ma', 'p1');
  const r = sb.consumableAmort([e], um, '2026-10');
  P('B7 单机桶只摊 500 页（封顶），超出的 1500 页记 uncoveredPrinter[p1]',
    r.rows[0].units === 500 && r.rows[0].cents === 10000
    && r.uncoveredPrinter.p1['2026-10'] === 1500
    && !r.uncovered.toner['2026-10'], JSON.stringify({ u: r.uncovered, up: r.uncoveredPrinter }));
  P('B7 另一台机（没买鼓）的 900 页不进任何 uncovered（没库存不等于这支鼓的责任）',
    r.uncoveredPrinter.p1020 === undefined, JSON.stringify(r.uncoveredPrinter));
}

/* ═════ B8：旧结构用量（没有 pagesByDayPrinter）不炸、也不误摊 ═════ */
{
  const sb = calcSandbox(js);
  sb.__P.p1 = { id: 'p1', name: '机 1' };
  const um = { '2026-10': { pagesByDay: { '2026-10-10': 800 }, sheetsByDay: {} } };
  const r = sb.consumableAmort([toner('t1', '2026-10-01', 100, 1000, 'ma', 'p1')], um, '2026-10');
  P('B8 旧缓存没有 pagesByDayPrinter ⇒ 单机鼓本月摊 0（不回落全队、不报错）',
    r.rows.length === 0 && r.lots.length === 1 && r.lots[0].monthUnits === 0, JSON.stringify(r.lots[0]));
  const r2 = sb.consumableAmort([toner('t2', '2026-10-01', 100, 1000, 'ma')], um, '2026-10');
  P('B8 同一份旧用量：未指定的鼓照旧按全队 800 页摊 8000 分（零回归）',
    r2.rows[0].cents === 8000, JSON.stringify(r2.rows));
}

/* ═════ B9：区块渲染（含归属列 / 提示） ═════ */
{
  const sb = calcSandbox(js);
  sb.__P.p1 = { id: 'p1', name: '机 1' };
  sb.usageOfMonth = function () { return null; };
  sb.getMembers = function () { return [{ id: 'ma', name: '成员A', color: '#4472C4' }]; };
  sb.currentKey = '2026-10';
  const um = { '2026-10': uMonth({ '2026-10-10': 1000 }, {},
                                { 'p1|2026-10-10': 400, 'pdead|2026-10-10': 0 }) };
  const es = [toner('t1', '2026-10-01', 100, 1000, 'ma', 'p1'),
              toner('t2', '2026-10-01', 100, 1000, 'ma', 'pdead')];
  const am = sb.consumableAmort(es, um, '2026-10');
  am.state = 'ready';
  const m = { key: '2026-10', label: '2026年10月', expenses: es };
  const html = sb.consumableAmortSectionHTML({ amort: am, usage: null }, m);
  P('B9 摊销表头新增「所属打印机」列', html.indexOf('<th>所属打印机</th>') >= 0);
  P('B9 单机行单元格写明「按该机本月 N 页摊」', html.indexOf('按该机本月') >= 0);
  P('B9 已删除的打印机有显式提示（要求重选，且说明已按全队摊）',
    html.indexOf('已经不在打印机台账里') >= 0 && html.indexOf('<b>已按全队页数</b>摊销') >= 0,
    'idx=' + html.indexOf('已经不在打印机台账里') + '/' + html.indexOf('<b>已按全队页数</b>摊销')
    + ' pmiss=' + JSON.stringify(am.lots.map(function (d) { return d.printerMissing; }))
    + ' tail=' + html.slice(-260));
  P('B9 按机器摊销汇总行出现（机器名 + 本月页数）',
    html.indexOf('按机器摊销（只吃「这台机」本月页数）') >= 0 && html.indexOf('机 1') >= 0);
}
}

try { runCases(); } catch (e) { console.log('HARNESS_ERROR ' + (e && e.stack || e)); process.exit(4); }
results.forEach(function (x) { console.log((x[1] ? 'PASS' : 'FAIL') + '\t' + x[0] + '\t' + x[2]); });
process.exit(results.some(function (x) { return !x[1]; }) ? 1 : 0);
"""


def run_harness(html_path, workdir, tag="harness"):
    hp = os.path.join(workdir, tag + ".cjs")
    open(hp, "w", encoding="utf-8").write(HARNESS)
    return subprocess.run(["node", hp, html_path], capture_output=True, text=True, encoding="utf-8")


section("B. 纯函数实测（真实 consumableAmort，Node VM）")
with tempfile.TemporaryDirectory() as tmpdir:
    r = run_harness(HTML, tmpdir)
    out = (r.stdout or "") + (r.stderr or "")
    for line in out.splitlines():
        if line.startswith("PASS\t") or line.startswith("FAIL\t"):
            _, name, detail = (line.split("\t") + ["", ""])[:3]
            check(name, line.startswith("PASS"), detail)
    code = r.returncode
    if code not in (0, 1):
        print(out[-3000:])
        check("Node 侧执行完成（不是崩溃/语法错）", False, f"exit={code}")
    elif code == 1:
        check("Node 侧全部断言通过", False, "见上面的 FAIL 行")
    else:
        check("Node 侧全部断言通过", True)

# ══════════════════════════════════════════════════════════════
# C. 变异自证
# ══════════════════════════════════════════════════════════════
section("C. 变异自证（改坏必须转红在具体断言上）")

MUTANTS = [
    ("单机桶回落到全队逐日页数（正是需求 A 要修的错误）",
     "      if (printerId) {\n        var per = u.pagesByDayPrinter || {};",
     "      if (false) {\n        var per = u.pagesByDayPrinter || {};",
     ["B1 M232 的鼓只按 M232 本月 500 页摊 ⇒ 2150 分（不是 6449 分）",
      "B1 摊销行文案写明「按 <机器名> 本月 500 页」",
      "B6 ¥128.98 / 3000 页的 M232 鼓：本月 M232 打 500 页 ⇒ 2150 分（¥21.50）",
      "B3 另一台机的鼓只吃自己那 500 页（不掺 p1 的 1500 页）"]),
    ("打印机被删时不回落（仍按那台机 → 摊成 0 页）",
     "      var gone = !!pid && !printerById(pid);",
     "      var gone = false;",
     ["B4 鼓指向的打印机已删 ⇒ 按全队 1200 页摊（不是 0、也不是那台机残留的 700 页）",
      "B4 行文案标出「原所属打印机已删除，按全队」"]),
    ("A4纸也被套上归属（拿硒鼓口径摊纸）",
     "printerId: kind === 'toner' ? safeId(e.printer_id) : '',",
     "printerId: safeId(e.printer_id),",
     ["B5 A4纸即使被写了 printer_id 也不受影响（金额、单位数、lot 归属都相同）"]),
    ("单机桶在旧缓存缺 pagesByDayPrinter 时回落到全队逐日页数（「绝不回落」的禁令被改坏）",
     "        var per = u.pagesByDayPrinter || {};",
     "        var per = u.pagesByDayPrinter || {};\n"
     "        if (!Object.keys(per).length) { Object.keys(u.pagesByDay || {}).forEach(function (d0) { per[printerId + '|' + d0] = u.pagesByDay[d0]; }); }",
     ["B8 旧缓存没有 pagesByDayPrinter ⇒ 单机鼓本月摊 0（不回落全队、不报错）"]),
]

with tempfile.TemporaryDirectory() as tmpdir:
    for idx, (title, old, new, expect_red) in enumerate(MUTANTS, 1):
        if old not in src:
            check(f"变异 {idx}「{title}」的锚点仍存在", False, "锚点没找到（源码改动后要同步更新本脚本）")
            continue
        mp = os.path.join(tmpdir, f"mut{idx}.html")
        open(mp, "w", encoding="utf-8").write(src.replace(old, new, 1))
        mr = run_harness(mp, tmpdir, tag=f"h{idx}")
        mout = (mr.stdout or "") + (mr.stderr or "")
        fails = [ln.split("\t")[1] for ln in mout.splitlines() if ln.startswith("FAIL\t")]
        hit = [n for n in expect_red if n in fails]
        check(f"变异 {idx}「{title}」后新脚本转红（exit={mr.returncode}，红在 {len(hit)} 条预期断言上）",
              mr.returncode == 1 and len(hit) == len(expect_red),
              "红的断言：" + " | ".join(fails[:4]))

print("\n" + "=" * 60)
print("硒鼓按所属打印机摊销（v7.10 需求 A）验收: " + ("全部通过 ✅" if ok_all else "存在失败 ❌"))
print("=" * 60)
sys.exit(0 if ok_all else 1)
