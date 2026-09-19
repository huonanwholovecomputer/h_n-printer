# -*- coding: utf-8 -*-
"""验收 · 收支清算「实际转账」模块（独立密码 + 现金口径实际盈亏 + 历史记录口径）

背景：原有的「经营损益 / 历史记录净额」算的是**账面口径**（该赚该亏多少），
与钱有没有真的转无关。真实场景里 2026-06 成员A垫了 1125.77、只代收 101.80，
若成员B / 成员C不按建议转账，亏损就全压在成员A身上 —— 账面却显示他 +862.36。

本脚本验证：
1. 全部内联 <script> 通过 `node --check` 语法校验。
2. 静态接线：transfer 独立密码模块、编辑入口、历史列口径、数据迁移与净化。
3. 函数级实测（Node VM 内跑真实 calcMonth + monthFromRecords）：
   - 截图的 2026-06 数据复现出 应转出/入 = +760.56 / -572.56 / -188.00
   - 未转账时实际盈亏 = 成员A -1023.97 / 成员B +348.50 / 成员C -124.60
   - 全额转账后实际盈亏 == 应得盈亏，差额=0
   - 部分转账按比例落在收款方头上
   - 任意转账组合下「团队总盈亏」恒定 = -800.07（转账是内部钱，不改变总账）
   - 非法记录（自转账 / 已删除成员 / 跨月日期）不污染结算
"""
import io
import os
import re
import subprocess
import sys
import tempfile
import shutil

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

# ---------- 1) 内联 <script> 语法校验 ----------
print("--- 语法校验 ---")
scripts = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", src, re.S)
check("抽到内联 script 块", len(scripts) > 0, f"{len(scripts)} 块")
tmpdir = tempfile.mkdtemp(prefix="hn_transfer_")
for i, s in enumerate(scripts):
    p = os.path.join(tmpdir, f"s{i}.js")
    open(p, "w", encoding="utf-8").write(s)
    r = subprocess.run(["node", "--check", p], capture_output=True, text=True, encoding="utf-8")
    check(f"script#{i} node --check", r.returncode == 0, (r.stderr or "").strip()[:300])

# ---------- 2) 静态接线 ----------
print("\n--- 独立密码模块 ---")
check("PASSWORD_KEYS 含 transfer", "PASSWORD_KEYS = ['score', 'expense', 'income', 'transfer', 'settings', 'auth']" in src)
check("DEFAULT_PASSWORDS 含 transfer", re.search(r"transfer: _PW_123456", src) is not None)
check("MODULE_NAMES 含 transfer", re.search(r"transfer: '实际转账'", src) is not None)
check("unlocked 初始状态含 transfer", "var unlocked = { score: false, expense: false, income: false, settings: false, auth: false, transfer: false }" in src)
check("resetData 重置后含 transfer",
      "unlocked = { score: false, expense: false, income: false, settings: false, auth: false, transfer: false };" in src)
check("设置页密码管理遍历 PASSWORD_KEYS（可设独立密码）",
      src.count("PASSWORD_KEYS.forEach(function (k) { if (!c.passwords[k])") >= 1
      and "PASSWORD_KEYS.forEach(function (k) {" in src)
check("迁移时补齐缺失的 transfer 密码（老数据不会无密码可用）",
      "PASSWORD_KEYS.forEach(function (k) { if (!c.passwords[k]) c.passwords[k] = DEFAULT_PASSWORDS[k]; });" in src)

print("\n--- 编辑入口 / 锁定 ---")
check("实际转账区块用 lockBadge('transfer')", "lockBadge('transfer')" in src)
check("实际转账区块用 lockBarHTML('transfer')", "lockBarHTML('transfer')" in src)
check("未解锁时输入框 disabled", "var dT = unlocked.transfer ? '' : ' disabled';" in src)
check("所有写操作都先校验 unlocked.transfer",
      src.count("if (!unlocked.transfer) { toast(") >= 3,
      f"{src.count('if (!unlocked.transfer) { toast(')} 处")
check("一键填入按钮存在", "function fillSuggestedTransfers()" in src and "按建议一键填入" in src)

print("\n--- 数据模型 / 迁移 / 净化 ---")
check("migrateConfig 补 transfers 默认值", "if (!Array.isArray(data.records.transfers)) data.records.transfers = [];" in src)
check("空 records 默认含 transfers", "data.records = { expenses: [], income: [], scores: [], transfers: [] }" in src)
check("allMonthKeys 纳入 transfers（按 month 收集）",
      "(STORE.records.transfers || []).forEach(function (r) { if (isMonthKey(r.month)) set[r.month] = true; });" in src)
check("monthFromRecords 按结算月份（month）筛选，不按日期",
      "transfers.push({ id: r.id, month: r.month, from: r.from, to: r.to, amount: r.amount, note: r.note || '' })" in src
      and "if (r.month === key) transfers.push(" in src)
check("monthRecordCount 统计 transfers（按 month）",
      "(STORE.records.transfers || []).forEach(function (r) { if (r.month === key) n++; });" in src)
check("sanitizeStore 校验转账 from/to/month/amount/note",
      "t.from = safeId(t.from); t.to = safeId(t.to);" in src and "t.month = isMonthKey(t.month) ? t.month : monthKeyOf(t.date);" in src)
check("mergeStores 合并 transfers", "transfers: unionById((localStore && localStore.records && localStore.records.transfers)" in src)
check("渲染行由 safeId 白名单过滤（防内联事件注入）",
      'var tid = safeId(t.id);' in src and 'onclick="delTransfer(' in src
      and 'delTransfer(' + "' + t.id" not in src)
check("付款人=收款人被拒绝", "付款人与收款人不能是同一人" in src)

print("\n--- 转账不带日期、绑定结算月份 ---")
check("转账记录表无「日期」列", "<thead><tr><th>付款人</th><th></th><th>收款人</th>" in src
      and '<th>日期</th><th>付款人</th>' not in src)
check("转账板块标题标出归属月份", "esc(m.label) + ' 归属 · 真正转了多少钱" in src)
check("新增记录写 month（不写 date）",
      "transfers.push({ id: uid(), month: currentKey, from: from, to: to, amount: amount, note: '' })" in src)
check("一键填入写 month（不写 date）",
      "transfers.push({ id: uid(), month: currentKey, from: t.from, to: t.to, amount: left / 100, note: '按建议填入' })" in src)
check("updTransfer 不再处理 date 字段", "field === 'date'" not in src)
check("旧版 date 记录迁移为 month（避免结算被搬到下月）",
      "if (t && !isMonthKey(t.month) && t.date) t.month = monthKeyOf(t.date);" in src and "if (t) delete t.date;" in src)
check("isMonthKey 校验 'YYYY-MM'", "function isMonthKey(v) { return /^\\d{4}-(0[1-9]|1[0-2])$/.test(String(v || '')); }" in src)
check("界面文案说明「下月付款仍算本月」", "钱即使下个月才转，也还算这个月的结算" in src)

print("\n--- 历史记录口径 ---")
check("历史表头改为「实际盈亏」", "esc(m.name) + ' 实际盈亏</th>'" in src)
check("历史表不再用旧公式 redist-perPerson+paid",
      "var net = r.redist[mem.id] - r.perPerson + (r.paid[mem.id] || 0);" not in src)
check("历史表用 actualPL 并附应得盈亏小字",
      "var aPL = r.actualPL[mem.id];" in src and "应 ' + (nPL >= 0 ? '+' : '') + RMB(nPL)" in src)
check("历史表下方有口径说明", "团队总盈亏不变" in src)

# ---------- 3) 函数级行为验证 ----------
print("\n--- calcMonth 行为（Node VM 内实测真实函数）---")
harness = r"""
const fs = require('fs');
const vm = require('vm');
const html = fs.readFileSync(process.argv[2], 'utf8');
const scripts = [...html.matchAll(/<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/g)].map(m => m[1]);
const js = scripts.join('\n;\n');

// 抽出「数据模型/迁移 → calcMonth 结束」这一段（含 migrateConfig/sanitizeStore/getMembers/
// parseCents/RMB/allMonthKeys/monthFromRecords/calcMonth），以便连迁移逻辑一起实测
const start = js.indexOf('function migrateConfig(data)');
const end = js.indexOf('/* ==================== 保存 ====================');
if (start < 0 || end < 0 || end <= start) { console.log('EXTRACT_FAIL'); process.exit(3); }
const snippet = js.slice(start, end);

function makeSandbox() {
  const sb = {
    console, Math, JSON, Object, Array, String, Number, Boolean, Date, RegExp, Error,
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

const JUNE = '2026-06';
const MA = 'ma', MB = 'mb', MC = 'mc';

// 截图 2026年6月的真实数据
function buildStore(transferRecords) {
  const d = (day) => `${JUNE}-${String(day).padStart(2, '0')}`;
  const exp = (item, amount, payer, day) => ({ id: 'e' + item, date: d(day), item, amount, payer });
  const inc = (memberId, amount, day) => ({ id: 'i' + memberId, date: d(day), memberId, amount });
  const sc = (memberId, item, score) => ({ id: 's' + memberId + item, date: d(10), memberId, item, score });
  return {
    config: {
      investRate: 0.05, scoreCap: 15,
      members: [
        { id: MA, name: '成员A', color: '#4472C4' },
        { id: MB, name: '成员B', color: '#548235' },
        { id: MC, name: '成员C', color: '#ED7D31' },
      ],
      passwords: {},
    },
    records: {
      expenses: [
        exp('打印机', 1016.10, MA, 3), exp('喷头', 109.67, MA, 5),
        exp('订书机', 19.80, MB, 7), exp('A4纸第3批', 25.70, MB, 20),
        exp('A4纸第1批', 89.00, MC, 12), exp('A4纸第2批', 57.60, MC, 15),
      ],
      income: [inc(MA, 101.80, 8), inc(MB, 394.00, 9), inc(MC, 22.00, 11)],
      scores: [
        sc(MA, '打印', 15), sc(MA, '推广', 15), sc(MA, '值班', 4),
        sc(MB, '打印', 15), sc(MB, '推广', 15), sc(MB, '值班', 12),
        sc(MC, '打印', 15), sc(MC, '推广', 9),
      ],
      transfers: transferRecords || [],
    },
    notes: {}, meta: { version: 0 },
  };
}

function calc(transferRecords, sb) {
  sb.STORE = buildStore(transferRecords);
  const m = sb.monthFromRecords(JUNE);
  return { m, r: sb.calcMonth(m) };
}

const results = [];
const yuan = (c) => (c / 100).toFixed(2);
const eq = (a, b) => Math.abs(a - b) < 1e-9;

// ── 基线：复现截图 ──
{
  const sb = makeSandbox();
  const { r } = calc([], sb);
  results.push(['采购总额 = 1317.87', eq(r.totalExp, 131787), yuan(r.totalExp)]);
  results.push(['代收总额 = 517.80', eq(r.totalInc, 51780), yuan(r.totalInc)]);
  results.push(['人均 = 439.29', eq(r.perPerson, 43929), yuan(r.perPerson)]);
  results.push(['应得收入 175.88 / 215.23 / 126.69',
    eq(r.redist[MA], 17588) && eq(r.redist[MB], 21523) && eq(r.redist[MC], 12669),
    [r.redist[MA], r.redist[MB], r.redist[MC]].map(yuan).join(' / ')]);
  results.push(['应转出/入 760.56 / -572.56 / -188.00',
    eq(r.net[MA], 76056) && eq(r.net[MB], -57256) && eq(r.net[MC], -18800),
    [r.net[MA], r.net[MB], r.net[MC]].map(yuan).join(' / ')]);
  results.push(['建议转账 2 笔：成员B→成员A 572.56、成员C→成员A 188.00',
    r.transfers.length === 2
    && r.transfers[0].from === MB && r.transfers[0].to === MA && eq(r.transfers[0].amount, 57256)
    && r.transfers[1].from === MC && r.transfers[1].to === MA && eq(r.transfers[1].amount, 18800),
    JSON.stringify(r.transfers.map(t => [t.from, t.to, yuan(t.amount)]))]);
}

// ── 场景 A：一分钱都没转 ──
{
  const sb = makeSandbox();
  const { r } = calc([], sb);
  results.push(['A 实际盈亏 = -1023.97 / +348.50 / -124.60',
    eq(r.actualPL[MA], -102397) && eq(r.actualPL[MB], 34850) && eq(r.actualPL[MC], -12460),
    [r.actualPL[MA], r.actualPL[MB], r.actualPL[MC]].map(yuan).join(' / ')]);
  results.push(['A 成员A 实际亏得比应得盈亏多 760.56',
    eq(r.settleGap[MA], 76056), '还应收 ' + yuan(r.settleGap[MA])]);
  results.push(['A 成员B反而净拿 348.50（超付 572.56）',
    eq(r.settleGap[MB], -57256), '已超付 ' + yuan(-r.settleGap[MB])]);
  results.push(['A 合计实际盈亏仍 = -800.07（团队总亏损不变）',
    eq(r.actualPL[MA] + r.actualPL[MB] + r.actualPL[MC], -80007),
    yuan(r.actualPL[MA] + r.actualPL[MB] + r.actualPL[MC])]);
}

// ── 场景 B：按建议全额转账 ──
{
  const sb = makeSandbox();
  const { r } = calc([
    { id: 't1', month: JUNE, from: MB, to: MA, amount: 572.56, note: '' },
    { id: 't2', month: JUNE, from: MC, to: MA, amount: 188.00, note: '' },
  ], sb);
  const m = r.members;
  const pl = (id) => r.redist[id] - r.perPerson;
  results.push(['B 全额转账后 实际盈亏 == 应得盈亏（三人全部）',
    m.every(x => eq(r.actualPL[x.id], pl(x.id))),
    m.map(x => yuan(r.actualPL[x.id]) + '/' + yuan(pl(x.id))).join(' ')]);
  results.push(['B 实际盈亏 = -263.41 / -224.06 / -312.60',
    eq(r.actualPL[MA], -26341) && eq(r.actualPL[MB], -22406) && eq(r.actualPL[MC], -31260),
    [r.actualPL[MA], r.actualPL[MB], r.actualPL[MC]].map(yuan).join(' / ')]);
  results.push(['B 三人差额全为 0（已结清）',
    m.every(x => eq(r.settleGap[x.id], 0))]);
  results.push(['B 实际转账合计 = 760.56', eq(r.transferTotal, 76056), yuan(r.transferTotal)]);
}

// ── 场景 C：成员B只转 300（转不够）──
{
  const sb = makeSandbox();
  const { r } = calc([
    { id: 't1', month: JUNE, from: MB, to: MA, amount: 300.00, note: '先转一半' },
    { id: 't2', month: JUNE, from: MC, to: MA, amount: 188.00, note: '' },
  ], sb);
  results.push(['C 成员A 实际盈亏 = -535.97（成员B那 272.56 仍未到账）',
    eq(r.actualPL[MA], -102397 + 30000 + 18800) && eq(r.settleGap[MA], 27256),
    yuan(r.actualPL[MA]) + ' / 还应收 ' + yuan(r.settleGap[MA])]);
  results.push(['C 成员B实际盈亏 = 48.50（还欠 272.56）',
    eq(r.actualPL[MB], 4850) && eq(r.settleGap[MB], -27256),
    yuan(r.actualPL[MB]) + ' / 已超付 ' + yuan(-r.settleGap[MB])]);
  results.push(['C 欠款 = 建议 572.56 - 实转 300.00 = 272.56',
    eq(r.transfers[0].amount - 30000, 27256),
    '欠 ' + yuan(r.transfers[0].amount - 30000)]);
}

// ── 场景 D：任意转账组合都不改变团队总盈亏（内部转移守恒）──
{
  let allEq = true, detail = '';
  const combos = [
    [],
    [{ from: MA, to: MB, amount: 999.99 }],                                  // 反向乱转
    [{ from: MB, to: MA, amount: 572.56 }],
    [{ from: MB, to: MC, amount: 100 }, { from: MC, to: MA, amount: 288 }], // 绕道转
    [{ from: MB, to: MA, amount: 572.56 }, { from: MC, to: MA, amount: 188 }],
    [{ from: MA, to: MB, amount: 50 }, { from: MB, to: MC, amount: 50 }],
  ];
  combos.forEach((c, i) => {
    const sb = makeSandbox();
    const { r } = calc(c.map((x, j) => ({ id: 'x' + j, month: JUNE, from: x.from, to: x.to, amount: x.amount })), sb);
    const sum = r.actualPL[MA] + r.actualPL[MB] + r.actualPL[MC];
    if (!eq(sum, -80007)) { allEq = false; detail = `组合#${i} 合计 ${yuan(sum)}`; }
  });
  results.push(['D 6 种转账组合下实际盈亏合计恒为 -800.07', allEq, detail]);
}

// ── 场景 E：脏数据不污染结算 ──
{
  const sb = makeSandbox();
  const { r } = calc([
    { id: 't1', month: JUNE, from: MB, to: MB, amount: 999 },          // 自转账
    { id: 't2', month: JUNE, from: 'ghost', to: MA, amount: 500 },      // 已删除成员
    { id: 't3', month: JUNE, from: MB, to: 'ghost', amount: 500 },      // 收款人不存在
    { id: 't4', month: '2026-07', from: MB, to: MA, amount: 572.56 },   // 归属 7 月，不算进 6 月
    { id: 't5', month: JUNE, from: MB, to: MA, amount: 0 },             // 0 元
  ], sb);
  results.push(['E 自转账 / 幽灵成员 / 0 元 / 归属他月 记录均不影响 6 月结算',
    eq(r.transferTotal, 0) && eq(r.actualPL[MA], -102397) && eq(r.actualPL[MB], 34850),
    'transferTotal=' + yuan(r.transferTotal)]);
  results.push(['E 归属 7 月的记录不落在 6 月',
    sb.STORE.records.transfers.filter(x => sb.monthFromRecords('2026-07').transfers.some(y => y.id === x.id)).length === 1]);
  results.push(['E 同月记录原样返回（不按日期排序 —— 记录已无日期）', (() => {
    const sb2 = makeSandbox();
    const { m } = calc([
      { id: 'b', month: JUNE, from: MB, to: MA, amount: 10 },
      { id: 'a', month: JUNE, from: MC, to: MA, amount: 20 },
    ], sb2);
    return m.transfers.length === 2 && m.transfers[0].id === 'b' && m.transfers[1].id === 'a'
      && m.transfers.every(t => t.month === JUNE && t.date === undefined);
  })()]);
}

// ── 场景 F：迁移与净化 ──
{
  // F1 旧版 date 记录 → month（按原月份键搬过去），且 date 被删除
  const sb = makeSandbox();
  const migrated = sb.migrateConfig({
    config: { members: [
      { id: MA, name: '成员A', color: '#4472C4' },
      { id: MB, name: '成员B', color: '#548235' }] },
    records: {
      expenses: [], income: [], scores: [],
      transfers: [
        { id: 'old1', date: '2026-06-30', from: MB, to: MA, amount: 300, note: '旧版按日归月' },
        { id: 'old2', date: '2026-07-02', from: MB, to: MA, amount: 100, note: '跨月记录' },
        { id: 'new1', month: '2026-08', date: '2026-08-01', from: MB, to: MA, amount: 50, note: '已有 month' },
      ],
    },
    notes: {}, meta: { version: 0 },
  });
  const mt = migrated.records.transfers;
  results.push(['F1 旧版 date 记录迁移为 month（不丢数据、不搬到下月）',
    mt.length === 3 && mt[0].month === '2026-06' && mt[1].month === '2026-07' && mt[2].month === '2026-08',
    mt.map(t => t.id + '->' + t.month).join(' ')]);
  results.push(['F1 迁移后 date 字段被清除（有 month 时以 month 为准）',
    mt.every(t => t.date === undefined)]);
  results.push(['F1 迁移后的记录落在正确的结算月份', (() => {
    sb.STORE = migrated;
    return sb.monthFromRecords('2026-06').transfers.length === 1
      && sb.monthFromRecords('2026-07').transfers.length === 1
      && sb.monthFromRecords('2026-08').transfers.length === 1
      && sb.monthFromRecords('2026-09').transfers.length === 0;
  })()]);

  // F2 迁移幂等：再跑一次不改变结果
  const again = sb.migrateConfig(JSON.parse(JSON.stringify(migrated)));
  results.push(['F2 迁移幂等（重复启动结果一致）',
    JSON.stringify(again.records.transfers) === JSON.stringify(migrated.records.transfers)]);

  // F3 导入净化：非法月份 / 自转账 / 幽灵成员被丢弃，合法的一分不差
  const sb3 = makeSandbox();
  const clean = sb3.sanitizeStore({
    config: { members: [
      { id: MA, name: '成员A', color: '#4472C4' },
      { id: MB, name: '成员B', color: '#548235' }] },
    records: {
      expenses: [], income: [], scores: [],
      transfers: [
        { id: 'ok1', month: '2026-06', from: MB, to: MA, amount: 300.005, note: '保留' },
        { id: 'ok2', date: '2026-06-30', from: MB, to: MA, amount: 10 },          // 旧版 date，应迁移保留
        { id: 'bad1', month: '2026-13', from: MB, to: MA, amount: 10 },           // 非法月份
        { id: 'bad2', month: 'x', from: MB, to: MA, amount: 10 },                 // 非法月份
        { id: 'bad3', month: '2026-06', from: MA, to: MA, amount: 10 },           // 自转账
        { id: 'bad4', month: '2026-06', from: 'a b', to: MA, amount: 10 },        // 非法 ID（含空格）
      ],
    },
    notes: {}, meta: { version: 0 },
  });
  const ct = clean.records.transfers;
  results.push(['F3 导入净化：仅保留 2 条合法记录',
    ct.length === 2 && ct[0].id === 'ok1' && ct[1].id === 'ok2',
    ct.map(t => t.id).join(',')]);
  results.push(['F3 金额归一到两位小数', eq(ct[0].amount, 300.01), String(ct[0].amount)]);
  results.push(['F3 旧版 date 记录经净化后带上 month', ct[1].month === '2026-06', String(ct[1].month)]);
  results.push(['F3 allMonthKeys 能收集到仅含转账的月份', (() => {
    sb3.STORE = clean;
    return sb3.allMonthKeys().includes('2026-06');
  })()]);
}

let fails = 0;
for (const [n, c, d] of results) {
  if (!c) fails++;
  console.log(`[${c ? 'PASS' : 'FAIL'}] ${n}` + (d ? '  -- ' + d : ''));
}
process.exit(fails ? 1 : 0);
"""
hp = os.path.join(tmpdir, "harness.js")
open(hp, "w", encoding="utf-8").write(harness)
r = subprocess.run(["node", hp, HTML], capture_output=True, text=True, encoding="utf-8")
print((r.stdout or "") + (r.stderr or ""))
if r.returncode != 0:
    ok_all = False

# ---------- 4) 渲染级冒烟（真实执行 renderDash / renderHistory） ----------
print("\n--- 渲染级冒烟（真实 DOM 渲染，见 verify_actual_transfer_render.js）---")
render_js = os.path.join(REPO, "tests", "verify_actual_transfer_render.js")
r = subprocess.run(["node", render_js, HTML], capture_output=True, text=True, encoding="utf-8")
print((r.stdout or "") + (r.stderr or ""))
if r.returncode != 0:
    ok_all = False

shutil.rmtree(tmpdir, ignore_errors=True)
print("\n" + "=" * 50)
print("实际转账 / 实际盈亏 验收:", "全部通过 ✅" if ok_all else "存在失败 ❌")
sys.exit(0 if ok_all else 1)
