# -*- coding: utf-8 -*-
"""验收 · 结算页「口径卫生」（2026-10 批次 C2：F2 / F3 / F7 / F8）

与既有脚本的分工：
  - verify_actual_transfer.py        —— 现金口径 / 实际盈亏 / 转账归属（本脚本不重复）
  - verify_actual_transfer_render.js —— 收支清算标签页的渲染冒烟
  - 本脚本 —— 结算页四条**跨模块口径**缺陷：
      F2  非成员归属流水「单向半消失」：仍被按 N 人均摊、却无人承接
      F3  导入净化只覆盖 4 条卫生规则里的 2 条（日期不校验 ⇒ 'NaN年NaN月' 幽灵月份）
      F7  趋势图「收益」读后端 o.revenue/o.receivable，与 KPI 的 _orderMoney().record 不同源
      F8  delMonth 不删该月的实际转账 ⇒ 月份删不掉、残留转账继续决定实际盈亏

用法：python tests/verify_settlement_hygiene.py
exit 0 = 全部通过。
"""
import io
import os
import re
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


src = open(HTML, encoding="utf-8").read()

# ══════════════════════════════════════════════════════════════
# 1) 静态接线
# ══════════════════════════════════════════════════════════════
print("=== 静态接线 ===")

check("normDateStr 严格校验且补零（isDateStr 基于它）",
      "function normDateStr(v) {" in src and "function isDateStr(v) { return normDateStr(v) !== ''; }" in src
      and "return y + '-' + String(mo).padStart(2, '0') + '-' + String(d).padStart(2, '0');" in src)

check("sanitizeStore 的流水净化覆盖「日期 / 归属成员 / 金额」",
      "var OWNER_KEY = { expenses: 'payer', income: 'memberId', scores: 'memberId' };" in src
      and "if (!isDateStr(r.date)) {" in src
      and "r.date = normDateStr(r.date);" in src
      and "if (!owner || !memberWhitelist[owner]) {" in src
      and "r.amount = Math.round((+r.amount || 0) * 100) / 100;" in src
      and "r.score = +r.score || 0;" in src)

check("转账净化补齐后两条卫生规则（0 元 / 非成员收付方）",
      "if (t.amount === 0) {" in src
      and "if (!memberWhitelist[t.from] || !memberWhitelist[t.to]) {" in src)

check("allMonthKeys 只收合法月份键（挡住幽灵月份，覆盖所有载入路径）",
      "var mk = monthKeyOf(r.date);" in src and "if (isMonthKey(mk)) set[mk] = true;" in src
      and "if (r.date) set[monthKeyOf(r.date)] = true;" not in src)

check("calcMonth 只结算「付款人仍是成员」的采购，并汇总 orphanPayerExp",
      "var settledExps = exps.filter(function (e) { return known[e.payer]; });" in src
      and "settledExps.forEach(function (e) { paid[e.payer] = (paid[e.payer] || 0) + parseCents(e.amount); });" in src
      and "orphanPayerExp: orphanPayerExp," in src
      and "exps.forEach(function (e) { paid[e.payer] =" not in src)

check("采购明细把未纳入结算的行标出来（不静默吞钱）",
      "var orphan = !_expMemberSet[e.payer];" in src and "未纳入结算" in src
      and "其中：已删除成员垫付" in src)

check("delMonth 连带删除该月的实际转账",
      "if (t.month !== key) return true;" in src and "droppedTransfers++;" in src
      and "含 ' + droppedTransfers + ' 笔实际转账" in src)

check("趋势图两处「收益」都走 _orderMoney（与 KPI 同源，分 → 元）",
      src.count("revenue += _orderMoney(o, true).record / 100;") == 2
      and "((o.revenue || 0) + (o.receivable || 0))" not in src,
      f"{src.count('revenue += _orderMoney(o, true).record / 100;')} 处")

# 全文件不得再有「读订单级后端收益字段」的代码（注释里提及不算）
_code = re.sub(r"/\*[\s\S]*?\*/", "", src)
_code = re.sub(r"//[^\n]*", "", _code)
check("全文件零 o.revenue / o.receivable 读取（只剩注释在解释历史）",
      "o.revenue" not in _code and "o.receivable" not in _code)

# ══════════════════════════════════════════════════════════════
# 2) 函数级实测（Node VM 内跑真实的 calcMonth / sanitizeStore / delMonth / 趋势聚合）
# ══════════════════════════════════════════════════════════════
print("\n=== 函数级实测（真实函数，非复刻）===")

harness = r"""
const fs = require('fs');
const vm = require('vm');
const html = fs.readFileSync(process.argv[2], 'utf8');
const scripts = [...html.matchAll(/<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/g)].map(m => m[1]);
const js = scripts.join('\n;\n');

/* 抽出「数据模型/迁移 → calcMonth 结束」这一段（含 normDateStr/isDateStr/sanitizeStore/
   parseCents/allMonthKeys/monthFromRecords/calcMonth/apportionCents）。 */
const start = js.indexOf('function migrateConfig(data)');
const end = js.indexOf('/* ==================== 保存 ====================');
if (start < 0 || end < 0 || end <= start) { console.log('EXTRACT_FAIL'); process.exit(3); }
const snippet = js.slice(start, end);

/* 按「函数体到第一个顶格 }」抽单个函数 —— 本文件顶层函数一律以 `\n}` 收尾。 */
function fnBody(name) {
  const i = js.indexOf('function ' + name + '(');
  if (i < 0) throw new Error('未找到函数 ' + name);
  const e = js.indexOf('\n}', i);
  if (e < 0) throw new Error('未找到函数 ' + name + ' 的结束');
  const t = js.slice(i, e + 2);
  if (t.indexOf('return') < 0) throw new Error('函数 ' + name + ' 抽取不完整');
  return t;
}
// S1-05（2026-10-23）：排除表新增「离线未同步 / 云端拒收」后成了多行对象字面量，
// 判据放宽为跨行匹配（否则会因为换行假红）。语义不变：必须能抽到整张排除表。
const mExcl = js.match(/^var _EXCLUDE_STATUS = \{[\s\S]*?\};/m);
if (!mExcl) { console.log('EXCLUDED_STATUS_FAIL'); process.exit(3); }

const warns = [];
function makeSandbox() {
  const sb = {
    console: { log() {}, warn(...a) { warns.push(a.map(String).join(' ')); }, error() {} },
    Math, JSON, Object, Array, String, Number, Boolean, Date, RegExp, Error,
    DEFAULT_MEMBERS: [], DEFAULT_SCORE_CAP: 15, DEFAULT_RATE: 0.05,
    PASSWORD_KEYS: ['score', 'expense', 'income', 'transfer', 'settings', 'auth'],
    DEFAULT_PASSWORDS: {}, DEFAULT_PRICING: { simplex_price: 0.2, duplex_price: 0.3, cover_page_price: 0.1,
      delivery_percentages: {}, urgency_prices: {} },
    FINANCE_MODE: 'cloud', STORE: null,
  };
  vm.createContext(sb);
  vm.runInContext(snippet, sb);
  return sb;
}

function JUNE_STORE(records) {
  return {
    config: { investRate: 0.05, scoreCap: 15, passwords: {}, members: [
      { id: 'ma', name: '成员A', color: '#4472C4' },
      { id: 'mb', name: '成员B', color: '#548235' } ] },
    records: Object.assign({ expenses: [], income: [], scores: [], transfers: [] }, records || {}),
    notes: {}, meta: { version: 0 },
  };
}

const results = [];
const yuan = (c) => (c / 100).toFixed(2);
const eq = (a, b) => Math.abs(a - b) < 1e-9;
const sum = (o, ids) => ids.reduce((s, id) => s + (o[id] || 0), 0);

/* ── F2：幽灵付款人的采购 ────────────────────────────────────── */
{
  const sb = makeSandbox();
  // 只有 X（已删除成员）垫付 ¥100，A/B 两人
  sb.STORE = JUNE_STORE({ expenses: [{ id: 'e1', date: '2026-06-03', item: '纸', amount: 100, payer: 'ghost' }] });
  const r = sb.calcMonth(sb.monthFromRecords('2026-06'));
  results.push(['F2 幽灵付款人的采购不进 totalExp / paid（不再被 N 人均摊）',
    r.totalExp === 0 && r.paid.ma === 0 && r.paid.mb === 0 && r.perPerson === 0,
    'totalExp=' + yuan(r.totalExp) + ' perPerson=' + yuan(r.perPerson)]);
  results.push(['F2 但金额被记进 orphanPayerExp（钱不凭空消失）',
    r.orphanPayerExp === 10000, yuan(r.orphanPayerExp)]);
  results.push(['F2 Σ应转出/入 == 0（旧写法 Σnet = -¥100）',
    sum(r.net, ['ma', 'mb']) === 0, sum(r.net, ['ma', 'mb'])]);
  results.push(['F2 ★ Σ实际盈亏 == 团队盈亏（旧写法 ¥0.00 vs −¥100.00，差整整 100 元）',
    sum(r.actualPL, ['ma', 'mb']) === r.totalInc - r.totalExp,
    yuan(sum(r.actualPL, ['ma', 'mb'])) + ' vs ' + yuan(r.totalInc - r.totalExp)]);
  results.push(['F2 没有任何人被挂在「还应收」上',
    r.members.every(m => r.settleGap[m.id] === 0), r.members.map(m => m.id + ':' + r.settleGap[m.id]).join(' ')]);
}
{
  // 混合：幽灵垫付 ¥100 + 成员A垫付 ¥60 ⇒ 只按 ¥60 均摊
  const sb = makeSandbox();
  sb.STORE = JUNE_STORE({ expenses: [
    { id: 'e1', date: '2026-06-03', item: '幽灵', amount: 100, payer: 'ghost' },
    { id: 'e2', date: '2026-06-04', item: '成员', amount: 60, payer: 'ma' } ] });
  const r = sb.calcMonth(sb.monthFromRecords('2026-06'));
  results.push(['F2 混合场景：只按「成员垫付的部分」均摊（¥60 / 2 人）',
    r.totalExp === 6000 && r.perPerson === 3000 && r.orphanPayerExp === 10000,
    yuan(r.totalExp) + ' / ' + yuan(r.perPerson) + ' / 孤儿 ' + yuan(r.orphanPayerExp)]);
  results.push(['F2 混合场景 Σnet == 0、Σpaid == totalExp',
    sum(r.net, ['ma', 'mb']) === 0 && sum(r.paid, ['ma', 'mb']) === r.totalExp,
    sum(r.net, ['ma', 'mb']) + ' / ' + sum(r.paid, ['ma', 'mb'])]);
}

/* ── F3：导入净化 ────────────────────────────────────────────── */
{
  warns.length = 0;
  const sb = makeSandbox();
  const clean = sb.sanitizeStore({
    config: { members: [ { id: 'ma', name: 'A', color: '#111111' }, { id: 'mb', name: 'B', color: '#222222' } ] },
    records: {
      expenses: [
        { id: 'e1', date: '2026-06-03', item: '合法', amount: 12.345, payer: 'ma' },
        { id: 'e2', date: '', item: '空日期', amount: 10, payer: 'ma' },
        { id: 'e3', date: 'unknown', item: '非法日期', amount: 10, payer: 'ma' },
        { id: 'e4', date: '2026-06-31', item: '不存在的日期', amount: 10, payer: 'ma' },
        { id: 'e5', date: '2026-13-01', item: '月份越界', amount: 10, payer: 'ma' },
        { id: 'e6', date: '2026-06-05', item: '幽灵付款人', amount: 10, payer: 'ghost' },
      ],
      income: [
        { id: 'i1', date: '2026-06-05', memberId: 'mb', amount: 30.005 },
        { id: 'i2', date: '2026-6-5', memberId: 'mb', amount: 1 },
        { id: 'i3', date: '2026-06-05', memberId: 'ghost', amount: 1 },
      ],
      scores: [
        { id: 's1', date: '2026-06-05', memberId: 'ma', item: '打印', score: '3' },
        { id: 's2', date: 'nope', memberId: 'ma', item: 'x', score: 1 },
      ],
      transfers: [
        { id: 't1', month: '2026-06', from: 'ma', to: 'mb', amount: 10 },
        { id: 't2', month: '2026-06', from: 'ma', to: 'mb', amount: 0 },
        { id: 't3', month: '2026-06', from: 'ma', to: 'ghost', amount: 5 },
        { id: 't4', month: '2026-13', from: 'ma', to: 'mb', amount: 5 },
      ],
    },
    notes: {}, meta: { version: 0 },
  });
  const R = clean.records;
  results.push(['F3 采购：只留 1 条合法记录（空/非法/不存在的日期、越界月、幽灵付款人全丢）',
    R.expenses.length === 1 && R.expenses[0].id === 'e1', R.expenses.map(x => x.id).join(',')]);
  results.push(['F3 采购金额归一到两位小数',
    eq(R.expenses[0].amount, 12.35), String(R.expenses[0].amount)]);
  results.push(['F3 代收：只留 2 条（空/非法日期与幽灵成员丢弃）',
    R.income.length === 2 && R.income[0].id === 'i1' && R.income[1].id === 'i2',
    R.income.map(x => x.id).join(',')]);
  results.push(['F3 宽松日期被补零为 2026-06-05（否则 monthKeyOf 截出 "2026-6-"）',
    R.income[1].date === '2026-06-05', String(R.income[1].date)]);
  results.push(['F3 评分：只留 1 条且得分归一为数字',
    R.scores.length === 1 && R.scores[0].score === 3, JSON.stringify(R.scores)]);
  results.push(['F3 转账：0 元与非成员收付方被丢弃（只剩 t1）',
    R.transfers.length === 1 && R.transfers[0].id === 't1', R.transfers.map(x => x.id).join(',')]);
  results.push(['F3 丢弃都有 console.warn 记录（不静默改写）',
    warns.filter(w => w.indexOf('幽灵月份') >= 0).length === 5   // 采购 4 条 + 评分 1 条
    && warns.some(w => w.indexOf('非成员的流水记录') >= 0)
    && warns.some(w => w.indexOf('0 元转账') >= 0)
    && warns.some(w => w.indexOf('非成员的转账记录') >= 0),
    '共 ' + warns.length + ' 条警告，其中日期非法 ' + warns.filter(w => w.indexOf('幽灵月份') >= 0).length + ' 条']);

  sb.STORE = clean;
  const keys = sb.allMonthKeys();
  results.push(['F3 净化后月份列表无幽灵月份（只有合法 YYYY-MM + 当月）',
    keys.every(k => /^\d{4}-(0[1-9]|1[0-2])$/.test(k)) && keys.includes('2026-06'),
    keys.join(',')]);

  // 幂等：再净化一次结果不变
  const again = sb.sanitizeStore(JSON.parse(JSON.stringify(clean)));
  results.push(['F3 净化幂等（重复导入结果一致）',
    JSON.stringify(again.records) === JSON.stringify(clean.records)]);
}
{
  // 脏 date 直接塞进 STORE（模拟云端拉取路径，不经 sanitizeStore）—— allMonthKeys 也必须兜住
  const sb = makeSandbox();
  sb.STORE = JUNE_STORE({
    expenses: [{ id: 'x1', date: 'unknown', item: 'x', amount: 1, payer: 'ma' }],
    income: [{ id: 'x2', date: '', memberId: 'ma', amount: 1 }],
  });
  const keys = sb.allMonthKeys();
  const labels = keys.map(sb.labelOf);
  results.push(['F3 ★ 脏 date 直塞 STORE 时也不产生 "NaN年NaN月"（旧写法必现）',
    labels.every(l => l.indexOf('NaN') < 0) && keys.every(k => /^\d{4}-(0[1-9]|1[0-2])$/.test(k)),
    labels.join(' / ')]);
}

/* ── F8：删月份连带转账 ──────────────────────────────────────── */
{
  const sb = makeSandbox();
  vm.runInContext(mExcl[0], sb);
  vm.runInContext(fnBody('_orderMoney'), sb);
  vm.runInContext(fnBody('delMonth'), sb);
  sb.confirmUnlockSettings = (fn) => fn();
  sb.openConfirm = () => ({ then: (cb) => { cb(true); return { catch() {} }; } });
  sb.save = () => {}; sb.renderAll = () => {}; sb.toast = (m) => { sb.__toast = m; };
  sb.currentKey = '2026-06';
  sb.STORE = JUNE_STORE({
    expenses: [{ id: 'e1', date: '2026-06-03', item: '纸', amount: 10, payer: 'ma' },
               { id: 'e2', date: '2026-07-03', item: '墨', amount: 20, payer: 'ma' }],
    transfers: [{ id: 't1', month: '2026-06', from: 'ma', to: 'mb', amount: 5 },
                { id: 't2', month: '2026-07', from: 'mb', to: 'ma', amount: 7 }],
  });
  sb.STORE.notes['2026-06'] = '六月备注';
  sb.delMonth('2026-06');
  const T = sb.STORE.records.transfers;
  results.push(['F8 删月份连带删除该月实际转账（旧写法残留 t1 ⇒ 月份删不掉）',
    T.length === 1 && T[0].id === 't2', T.map(x => x.id + '@' + x.month).join(',')]);
  results.push(['F8 该月采购/备注照删，其他月不受影响',
    sb.STORE.records.expenses.length === 1 && sb.STORE.records.expenses[0].id === 'e2'
    && sb.STORE.notes['2026-06'] === undefined,
    sb.STORE.records.expenses.map(x => x.id).join(',')]);
  results.push(['F8 delMonth 后 allMonthKeys 不再含该月，残留转账不再影响实际盈亏',
    !sb.allMonthKeys().includes('2026-06') && sb.STORE.records.transfers.every(t => t.month !== '2026-06'),
    sb.allMonthKeys().join(',')]);
  results.push(['F8 toast 如实告知删掉了多少笔转账',
    typeof sb.__toast === 'string' && sb.__toast.indexOf('1 笔实际转账') >= 0, String(sb.__toast)]);
}

/* ── F7：趋势图「收益」与 KPI 同源 ───────────────────────────── */
{
  const sb = makeSandbox();
  vm.runInContext(mExcl[0], sb);
  vm.runInContext(fnBody('_orderMoney'), sb);
  /* v6.8：日趋势与逐时趋势共用同一累加/建桶函数（收益口径唯一落点），故先把它们装进沙箱 */
  vm.runInContext(fnBody('_trendEmptyBucket'), sb);
  vm.runInContext(fnBody('_trendAccumulate'), sb);
  vm.runInContext(fnBody('_computeDailyTrend'), sb);
  vm.runInContext(fnBody('_trendByBucket'), sb);
  // 只服务 pages/sheets 两列，与「收益」口径无关（v7.2：页数走 _orderPages → _filePages）
  vm.runInContext(fnBody('_filePages'), sb);
  vm.runInContext(fnBody('_orderPages'), sb);
  sb._orderSheets = () => ({ total: 0 });
  sb._fmtDate = (d) => String(d || '').substring(0, 10);

  const normal = {
    created_at: '2026-06-05 10:00:00', is_admin_print: 0,
    revenue: 999, receivable: 888,          // 后端字段：故意与 files 口径不一致
    total_price: 5.00, total_page_count: 4, files_count: 2,
    files: [{ total_price: 3.50, status: 'sent' }, { total_price: 1.20, status: 'queued' }],
  };
  const self = {   // 自打订单：金额一律不计
    created_at: '2026-06-05 11:00:00', is_admin_print: 1, revenue: 50, receivable: 50,
    total_price: 9.90, total_page_count: 1, files_count: 1,
    files: [{ total_price: 9.90, status: 'sent' }],
  };
  const dropped = {   // 整单失败：不计收益，但订单数照计
    created_at: '2026-06-06 09:00:00', is_admin_print: 0, revenue: 77, receivable: 0,
    total_price: 2.00, total_page_count: 1, files_count: 1,
    files: [{ total_price: 2.00, status: 'failed' }],
  };
  const orders = [normal, self, dropped];
  const want = sb._orderMoney(normal, true).record / 100 + sb._orderMoney(self, true).record / 100
             + sb._orderMoney(dropped, true).record / 100;

  const daily = sb._computeDailyTrend(orders);
  const d1 = daily.filter(p => p.date === '2026-06-05')[0];
  const d2 = daily.filter(p => p.date === '2026-06-06')[0];
  const total = daily.reduce((s, p) => s + p.revenue, 0);

  results.push(['F7 单日收益 = _orderMoney().record（不含后端 revenue/receivable 的 999/888）',
    d1 && eq(Math.round(d1.revenue * 100) / 100, 5.00),
    '日收益 ' + (d1 ? d1.revenue : 'n/a') + ' / 期望 5.00（后端字段会给出 1887 或 9.9 + 0.0 等错值）']);
  results.push(['F7 自打订单收益为 0',
    eq(Math.round(d1.revenue * 100) / 100, 5.00), String(d1 && d1.revenue)]);
  results.push(['F7 整单失败的订单不计收益',
    d2 && eq(d2.revenue, 0), String(d2 && d2.revenue)]);
  results.push(['F7 趋势合计 == Σ _orderMoney().record / 100（与 KPI 严格同源）',
    eq(Math.round(total * 100) / 100, Math.round(want * 100) / 100),
    total + ' vs ' + want]);

  const bucket = sb._trendByBucket(orders, 'day');
  const bTotal = bucket.reduce((s, p) => s + p.revenue, 0);
  results.push(['F7 回退保留的 _trendByBucket 与日趋势同口径',
    eq(Math.round(bTotal * 100) / 100, Math.round(want * 100) / 100), String(bTotal)]);
}

let fails = 0;
for (const [n, c, d] of results) {
  if (!c) fails++;
  console.log(`[${c ? 'PASS' : 'FAIL'}] ${n}` + (d ? '  -- ' + d : ''));
}
process.exit(fails ? 1 : 0);
"""

tmpdir = tempfile.mkdtemp(prefix="hn_hygiene_")
hp = os.path.join(tmpdir, "harness.js")
open(hp, "w", encoding="utf-8").write(harness)
r = subprocess.run(["node", hp, HTML], capture_output=True, text=True, encoding="utf-8")
print((r.stdout or "") + (r.stderr or ""))
if r.returncode != 0:
    ok_all = False

import shutil
shutil.rmtree(tmpdir, ignore_errors=True)

print("\n" + "=" * 50)
print("结算页口径卫生（C2 · F2/F3/F7/F8）验收:", "全部通过 ✅" if ok_all else "存在失败 ❌")
sys.exit(0 if ok_all else 1)
