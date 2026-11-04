/* 验收 · 收支清算「跨区块恒等式」（v7.11，2026-10-30 用户点「开始做 A」）
 *
 * 为什么必须真渲染：A 组修的 6 条毛病全是「同一个量在两个区块各算一遍，算出两个数」，
 * 只有在真页面上把两块 DOM 的数字取出来对比才抓得到。纯静态读过源码是抓不到的
 * （verify_settle_formula.py 已经证明：ΣW ≡ 0，逐人写错但总和凑巧相等时自检永远绿）。
 *
 * 钉住的 6 条（对应 settlement.html 的 v7.11 改动）：
 *   A1 「实际盈亏 · 应得盈亏」列 == 「经营损益 · 本月盈亏」列 == calcMonth 的 bookPL
 *      （旧写法在 realPLSectionHTML 里现场重算 R−A−D，漏了损耗费 W ⇒ 有收损耗费机器的月份对不上）
 *   A2 ΣB == ΣC == Π，且公式块的自检徽标必须是 ✅（含新增的逐人 bookClash 核对）
 *   A3 公式块的 ΣB 取自 r.bookPL，不再现场重算（源码级 + 文本级双重）
 *   A4 「用纸统计 · 合计」的「正常用纸」必须含「未归属」灰行，合计 == usage.sheets
 *      （旧写法只在灰行显示、不进合计 ⇒ 合计 < 实际计入量，A4 摊销按 u.sheets 算就与它对不上）
 *   A5 「打印机折旧 · 本口径页数」合计必须排除父订单非正常用量终态的单
 *      （旧写法只筛子文件 sent，不筛父订单 ⇒ 已取消/已拒绝的单照样计页、计提折旧）
 *   A6 「垫付清算」第三列表头跟本月**生效**的口径（r.useUsage / r.basisFallback），不读配置项
 *      （选了「按用量」但当月 0 页 ⇒ calcMonth 已回退均摊，表头还写「按用量」就是把均摊说成按用量）
 *   A7 实际转账合法性只有一个判据 _validTransfers()（chips 的「已转」与 calcMonth 的 sent/recv 同源）
 *
 * 夹具刻意造出「损耗费 ≠ 0」的月份（一台 team 机 + 一台 billing='wear' 的机），
 * 否则 ΣW ≡ 0 会让 A1/A3 的旧写法照样对得上 —— 那样验收就是假绿。
 *
 * 用法：node tests/verify_settlement_invariants.mjs [settlement.html 路径]
 * exit 0 = 全部通过。
 */
import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';

const require_ = createRequire(import.meta.url);
const { chromium } = require_('C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.join(HERE, '..', '..');
const FINANCE = path.join(ROOT, 'local_print_tool', 'finance');
const HTML_PATH = process.argv[2] || path.join(FINANCE, 'settlement.html');

let bad = 0, total = 0;
function chk(name, cond, detail) {
  total++;
  if (!cond) bad++;
  console.log('[' + (cond ? 'PASS' : 'FAIL') + '] ' + name + (!cond && detail !== undefined ? '  -- ' + detail : ''));
}

/* 取某个顶层函数在源码里的整段实现（从 `function name(` 到下一个顶层 function）——
   「这个块里还剩什么」这类结构断言用它，比全文件 indexOf 准（同名文案可能别处也有）。 */
function sliceFn(source, name) {
  const i = source.indexOf('function ' + name + '(');
  if (i < 0) return '';
  const j = source.indexOf('\nfunction ', i + 5);
  return source.slice(i, j < 0 ? source.length : j);
}

/* ── 夹具 ────────────────────────────────────────────────────────────────
   3 个成员（取整余数才有意义）；两台机器：
     pteam 团队共担（有折旧 D，且未归属 10 页 ⇒ orphanCost 单列）
     pwear billing='wear'、owner=ma（使用者付损耗费给 ma ⇒ W ≠ 0，这是 A1 的关键） */
const PTEAM = { id: 'pteam', name: 'HP LaserJet 1020', model: 'HP LaserJet 1020',
                client_id: 'c1020', price: 400, life_pages: 4000, life_mode: 'estimated',
                status: 'active', billing: 'team', owner: 'shared', measured: false };
const PWEAR = { id: 'pwear', name: 'HP LaserJet MFP M232-M237', model: 'HP LaserJet MFP M232-M237',
                client_id: 'cm232', price: 500, life_pages: 5000, life_mode: 'estimated',
                status: 'active', billing: 'wear', owner: 'ma', measured: false };

const STORE = {
  config: {
    investRate: 0.05, scoreCap: 15, passwords: {}, memberBindings: [],
    members: [{ id: 'ma', name: '张三', color: '#4472C4' },
              { id: 'mb', name: '李四', color: '#ED7D31' },
              { id: 'mc', name: '王五', color: '#A5A5A5' }],
    printers: [PTEAM, PWEAR],
    purchaseBasis: 'usage',   /* A6：10 月用量 > 0 ⇒ 生效；11 月用量 0 ⇒ 已回退 */
    waste_paper: {},
  },
  records: {
    expenses: [
      /* 普通采购（kind='other'，不进耗材摊销，避免把 A4/硒鼓摊销搅进来） */
      { id: 'e1', date: '2026-10-01', item: '打印纸箱 + 装订耗材', kind: 'other', amount: 300, payer: 'ma' },
      /* 11 月：只为让月份下拉里有 11 月，且该月**没有任何订单** ⇒ 用量 0 ⇒ 采购分摊回退均摊 */
      { id: 'e2', date: '2026-11-02', item: '11 月杂项', kind: 'other', amount: 100, payer: 'mb' },
    ],
    income: [
      { id: 'i1', date: '2026-10-03', memberId: 'ma', amount: 90 },
      { id: 'i2', date: '2026-10-03', memberId: 'mb', amount: 60 },
      { id: 'i3', date: '2026-10-03', memberId: 'mc', amount: 30 },
    ],
    scores: [],
    transfers: [
      /* 合法：李四 → 张三 ¥100（A7 的「已转」只该认这一条） */
      { id: 't1', month: '2026-10', from: 'mb', to: 'ma', amount: 100, note: '' },
      /* 垃圾记录：自转 / 已删成员 / 0 元 —— calcMonth 的 sent/recv 与 chips 都必须无视它们 */
      { id: 't2', month: '2026-10', from: 'ma', to: 'ma', amount: 50, note: '' },
      { id: 't3', month: '2026-10', from: 'mzzz', to: 'ma', amount: 30, note: '' },
      { id: 't4', month: '2026-10', from: 'mc', to: 'ma', amount: 0, note: '' },
    ],
  },
  notes: {}, meta: { version: 1 },
};

function ord(id, who, status, day, cid, files, price, openid) {
  return { order_id: id, order_number: 'HN2026' + day.slice(0, 2) + day.slice(3, 5) + '-00' + id,
           source: 'local', openid: openid || 'local', nickname: '', owner_name: who,
           status: status, total_price: price, created_at: '2026-' + day + ' 10:00:00',
           files_count: files.length, total_page_count: files.reduce((a, f) => a + f.total_pages, 0),
           total_sheet_count: files.reduce((a, f) => a + f.total_sheets, 0),
           received_client: cid, received_label: '', files: files };
}
/* 2026-10 订单：
   o1 张三 sent 1020 100 页 / 50 张（team 机 ⇒ 张三背折旧）
   o2 李四 sent M232 200 页 / 100 张（wear 机 ⇒ 李四付损耗费给张三）
   o3 王五 sent M232 100 页 / 50 张（同上）
   o4 外部客户 sent 1020 10 页 / 5 张（成员名单外 ⇒ unattributedSheets=5，A4 用）
   o5 张三 **已取消** 但子文件仍是 sent 的 8 页 / 4 张（A5 的关键：旧写法照样计页） */
const ORDERS_10 = [
  ord(1, '张三', 'sent', '10-23', 'c1020', [{ status: 'sent', total_sheets: 50, total_pages: 100 }], 30),
  ord(2, '李四', 'sent', '10-23', 'cm232', [{ status: 'sent', total_sheets: 100, total_pages: 200 }], 60),
  ord(3, '王五', 'sent', '10-23', 'cm232', [{ status: 'sent', total_sheets: 50, total_pages: 100 }], 30),
  ord(4, '外部客户', 'sent', '10-24', 'c1020', [{ status: 'sent', total_sheets: 5, total_pages: 10 }], 3, 'nobody'),
  ord(5, '张三', 'canceled', '10-25', 'c1020', [{ status: 'sent', total_sheets: 4, total_pages: 8 }], 2.4),
];

/* ── 桩服务（绝不发外网请求） ───────────────────────────────────────────── */
const posts = [];
const json = (res, obj) => { res.writeHead(200, { 'Content-Type': 'application/json; charset=utf-8' }); res.end(JSON.stringify(obj)); };

const server = http.createServer((req, res) => {
  const u = new URL(req.url, 'http://127.0.0.1');
  const p = decodeURIComponent(u.pathname);
  if (p.startsWith('/api/')) {
    if (p === '/api/health') return json(res, { success: true, cloud: true });
    if (p === '/api/proxy/finance/config') {
      if (req.method === 'POST') {
        let b = '';
        req.on('data', (c) => { b += c; });
        return req.on('end', () => {
          try { posts.push(JSON.parse(b)); } catch (e) { posts.push({ raw: b }); }
          json(res, { success: true, server_version: posts.length + 1 });
        });
      }
      return json(res, { success: true, data: STORE });
    }
    if (p === '/api/local/finance_auth/status') {
      return json(res, { success: true, modules: {}, needs_setup: [],
                         session: { active: false, modules: [], expires_in: 0 },
                         session_ttl: 1800, read_only: false, integrity: 'ok' });
    }
    if (p === '/api/local/finance_auth/unlock') {
      let b = '';
      req.on('data', (c) => { b += c; });
      return req.on('end', () => {
        let mod = 'expense';
        try { mod = JSON.parse(b).module || 'expense'; } catch (e) {}
        json(res, { success: true, code: 'ok', message: '',
                    session: 'test-session-' + mod, expires_in: 1800,
                    modules: ['expense', 'income', 'transfer', 'settings', 'score', 'auth',
                              'orderstatus', 'orderprinter'],
                    stored_hash: null });
      });
    }
    if (p === '/api/proxy/pricing') {
      return json(res, { success: true, data: { simplex_price: 0.3, duplex_price: 0.4,
        cover_page_price: 0.1, delivery_percentages: {}, urgency_prices: {} } });
    }
    if (p === '/api/proxy/admin/statistics/months') {
      return json(res, { success: true, data: { months: [{ month: '2026-10' }, { month: '2026-11' }] } });
    }
    if (p === '/api/proxy/admin/statistics/revenue') {
      const start = u.searchParams.get('start_date') || '';
      const month = start.slice(0, 7);
      const orders = month === '2026-10' ? ORDERS_10 : [];
      return json(res, { success: true, data: { period: {}, revenue_summary: {}, by_user: [], orders } });
    }
    if (p === '/api/local/orders') return json(res, { success: true, data: { orders: [] } });
    if (p === '/api/local/data') return json(res, { success: true, data: null });
    if (p === '/api/local/bindings') return json(res, { success: true, data: {} });
    if (p === '/api/local/external_prints') return json(res, { success: true, orders: [], count: 0, skipped: 0,
      simplex_price: 0.3, duplex_price: 0.4, owner_name: '' });
    return json(res, { success: true, data: null });
  }
  const file = p === '/' ? 'settlement.html' : p.replace(/^\//, '');
  const fp = file === 'settlement.html' ? HTML_PATH : path.join(FINANCE, file);
  if (!fs.existsSync(fp) || fs.statSync(fp).isDirectory()) { res.writeHead(404); return res.end('not found'); }
  const ext = path.extname(fp);
  res.writeHead(200, { 'Content-Type': ext === '.html' ? 'text/html; charset=utf-8'
    : ext === '.js' ? 'application/javascript; charset=utf-8' : 'application/octet-stream' });
  res.end(fs.readFileSync(fp));
});

const port = await new Promise((r) => server.listen(0, '127.0.0.1', () => r(server.address().port)));
const BASE = 'http://127.0.0.1:' + port;

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1600, height: 1200 }, deviceScaleFactor: 1 });
const pageErrors = [];
const externalReq = [];
page.on('pageerror', (e) => pageErrors.push(e.message));
page.on('request', (r) => { if (!r.url().startsWith(BASE) && !r.url().startsWith('data:')) externalReq.push(r.url()); });

/* 页面里用的小工具：金额文本 → 整数分（容忍 ¥ / 千分位 / U+2212 负号 / 后面的「盈利」标签） */
const PAGE_HELPERS = `
window.__cents = function (s) {
  var m = String(s == null ? '' : s).match(/[-\\u2212]?\\d[\\d,]*(?:\\.\\d+)?/);
  if (!m) return null;
  var t = m[0].replace(/\\u2212/g, '-').replace(/,/g, '');
  return Math.round(parseFloat(t) * 100);
};
window.__section = function (needle) {
  var all = Array.prototype.slice.call(document.querySelectorAll('.section'));
  for (var i = 0; i < all.length; i++) {
    var h2 = all[i].querySelector('h2');
    if (h2 && (h2.textContent || '').indexOf(needle) >= 0) return all[i];
  }
  return null;
};
window.__rows = function (sec) {
  if (!sec) return [];
  /* 合计行 <tr class="tfoot"> 也在 tbody 里 —— 必须排除，否则「逐行求和」会把合计自己加进去 */
  return Array.prototype.slice.call(sec.querySelectorAll('tbody tr')).filter(function (tr) {
    return !tr.classList.contains('tfoot');
  }).map(function (tr) {
    return Array.prototype.slice.call(tr.children).map(function (td) { return (td.textContent || '').trim(); });
  });
};
window.__tfoot = function (sec) {
  if (!sec) return null;
  var tr = sec.querySelector('tbody tr.tfoot');
  return tr ? Array.prototype.slice.call(tr.children).map(function (td) { return (td.textContent || '').trim(); }) : null;
};
true;
`;

async function openDash(month) {
  const want = month || '2026-10';
  await page.goto(BASE + '/settlement.html', { waitUntil: 'domcontentloaded' });
  await page.waitForFunction(() => typeof window.financeUnlock === 'function', null, { timeout: 20000 });
  await page.evaluate(() => window.financeUnlock('expense', '123456'));
  await page.waitForFunction(() => !!window.FINANCE_SESSION, null, { timeout: 10000 });
  await page.evaluate(PAGE_HELPERS);
  await page.evaluate((k) => { window.switchTab('dash'); window.onSelMonth(k); }, want);
  await page.waitForFunction((k) => window.currentKey === k && document.querySelector('#paperStatsSection'), want, { timeout: 20000 });
  await page.waitForFunction(() => {
    const sec = document.querySelector('#paperStatsSection');
    return sec && sec.querySelectorAll('tbody tr').length >= 3 && !sec.querySelector('.loading-spin');
  }, null, { timeout: 20000 });
  await page.waitForTimeout(800);
}
async function goMonth(key) {
  await page.evaluate((k) => { window.switchTab('dash'); window.onSelMonth(k); }, key);
  await page.waitForFunction((k) => window.currentKey === k && document.querySelector('#paperStatsSection'), key, { timeout: 20000 });
  await page.waitForFunction(() => {
    const sec = document.querySelector('#paperStatsSection');
    return sec && sec.querySelectorAll('tbody tr').length >= 3 && !sec.querySelector('.loading-spin');
  }, null, { timeout: 20000 });
  await page.waitForTimeout(800);
}

try {
  /* ══ 0. 夹具自检：先证明这套数据真的能区分「旧写法」和「新写法」 ══ */
  await openDash('2026-10');
  const base = await page.evaluate(() => {
    const r = window.calcMonthWithUsage('2026-10');
    const u = window.usageOfMonth('2026-10') || {};
    let wearAbs = 0;
    Object.keys(r.wearNet || {}).forEach((k) => { wearAbs += Math.abs(r.wearNet[k] || 0); });
    return {
      bookPL: r.bookPL, actualPL: r.actualPL, wearNet: r.wearNet, wearAbs: wearAbs,
      depTotal: r.depTotal || 0, totalInc: r.totalInc, totalExp: r.totalExp,
      useUsage: r.useUsage, basisFallback: r.basisFallback,
      sheets: u.sheets, wasteTotal: u.wasteTotal || 0,
      unattributedSheets: u.unattributedSheets || 0,
      pagesByPrinter: u.pagesByPrinter || {},
    };
  });
  chk('夹具自检：本月损耗费 W 不全为 0（否则 A1/A3 的旧写法照样绿）', base.wearAbs > 0, base.wearNet);
  chk('夹具自检：本月有团队折旧 D > 0', base.depTotal > 0, base.depTotal);
  chk('夹具自检：有「未归属」正常用纸 5 张（A4 用）', base.unattributedSheets === 5, base.unattributedSheets);
  chk('夹具自检：生效口径是按用量（A6 的正向）', base.useUsage === true && base.basisFallback === false,
      { useUsage: base.useUsage, basisFallback: base.basisFallback });

  /* ══ 1. A1：「经营损益 · 本月盈亏」== 「实际盈亏 · 应得盈亏」== calcMonth 的 bookPL ══ */
  const plSnap = await page.evaluate(() => {
    const inv = window.__section('经营损益');
    const real = document.querySelector('#dashRealPL');
    return {
      invRows: window.__rows(inv), invFoot: window.__tfoot(inv),
      realRows: window.__rows(real), realFoot: window.__tfoot(real),
      members: window.getMembers().map((m) => ({ id: m.id, name: m.name })),
    };
  });
  chk('A1 两块表都渲染出了 3 个成员行',
      plSnap.invRows.length === 3 && plSnap.realRows.length === 3,
      { inv: plSnap.invRows.length, real: plSnap.realRows.length });
  const cash = (s) => { const m = String(s || '').match(/[-−]?\d[\d,]*(?:\.\d+)?/); return m ? Math.round(parseFloat(m[0].replace('−', '-').replace(/,/g, '')) * 100) : null; };
  let a1ok = true, a1detail = [];
  plSnap.members.forEach((mem, i) => {
    const want = base.bookPL[mem.id] || 0;
    const invCell = cash(plSnap.invRows[i] && plSnap.invRows[i][5]);
    const realCell = cash(plSnap.realRows[i] && plSnap.realRows[i][7]);
    if (invCell !== want || realCell !== want) {
      a1ok = false;
      a1detail.push({ who: mem.name, inv: invCell, real: realCell, bookPL: want });
    }
  });
  chk('A1 「经营损益 · 本月盈亏」与「实际盈亏 · 应得盈亏」逐人都等于 calcMonth 的 bookPL（含损耗费）',
      a1ok, JSON.stringify(a1detail));

  /* ══ 2. A2：ΣB == ΣC == Π，且区块合计行一致 ══ */
  const sumBook = Object.keys(base.bookPL).reduce((a, k) => a + (base.bookPL[k] || 0), 0);
  const sumActual = Object.keys(base.actualPL).reduce((a, k) => a + (base.actualPL[k] || 0), 0);
  const teamPL = base.totalInc - base.totalExp - base.depTotal;
  chk('A2 ΣbookPL == ΣactualPL == Π（I − E − D）、且 Σ损耗费 ≡ 0',
      sumBook === sumActual && sumActual === teamPL
      && Object.keys(base.wearNet).reduce((a, k) => a + (base.wearNet[k] || 0), 0) === 0,
      { sumBook, sumActual, teamPL });
  chk('A2 「经营损益」合计行的「本月盈亏」== ΣB，且「实际盈亏」合计行的「应得盈亏」== ΣB',
      cash(plSnap.invFoot && plSnap.invFoot[5]) === sumBook
      && cash(plSnap.realFoot && plSnap.realFoot[7]) === sumBook,
      { invFoot: plSnap.invFoot, realFoot: plSnap.realFoot, sumBook });

  /* ══ 3. A3：公式块自检 ✅ + ΣB 取自 bookPL（文本 + 源码） ══ */
  const formula = await page.evaluate(() => {
    const box = document.querySelector('#dashFormula');
    const txt = box ? (box.textContent || '') : '';
    const m = txt.match(/ΣB = (\S+)/);
    return { hasOk: /✅ 自检通过/.test(txt), hasBad: /❌/.test(txt), sigmaB: m ? m[1] : null, len: txt.length };
  });
  chk('A3 公式块渲染出内容且自检徽标是 ✅（没有 ❌ 三处对不上）',
      formula.len > 500 && formula.hasOk && !formula.hasBad, { len: formula.len, hasOk: formula.hasOk, hasBad: formula.hasBad });
  chk('A3 公式块「ΣB」文本 == ΣbookPL', cash(formula.sigmaB) === sumBook, { sigmaB: formula.sigmaB, sumBook });

  const src = fs.readFileSync(HTML_PATH, 'utf8');
  chk('A3 源码：ΣB 直接累加 r.bookPL，不再现场重算 R−A−D',
      src.indexOf('sumBook += b;') > 0 && src.indexOf('sumBook += (r.redist') < 0,
      { hasBookPL: src.indexOf('sumBook += b;') > 0, hasOld: src.indexOf('sumBook += (r.redist') >= 0 });
  chk('A3 源码：被累加的 b 就是 r.bookPL（不是现场重算的 R−A−D）',
      /var b = r\.bookPL\[mem\.id\]/.test(src) && !/var b = \(r\.redist/.test(src),
      { okAssign: /var b = r\.bookPL\[mem\.id\]/.test(src), oldAssign: /var b = \(r\.redist/.test(src) });
  chk('A3 源码：保留逐人核对（bookClash）—— 总和凑巧相等时也能抓到个体写错',
      /bookClash\s*\+\+/.test(src) && /bookClash === 0/.test(src));

  /* ══ 4. A4：用纸统计合计 == 成员正常 + 未归属正常 == usage.sheets ══ */
  const paper = await page.evaluate(() => {
    const sec = document.querySelector('#paperStatsSection');
    const rows = window.__rows(sec), foot = window.__tfoot(sec);
    const u = window.usageOfMonth(window.currentKey) || {};
    let memberNormal = 0, orphanNormal = 0;
    rows.forEach((r) => {
      const n = parseInt(String(r[1] || '').replace(/[^\d]/g, ''), 10) || 0;
      if (String(r[0] || '').indexOf('未归属') >= 0) orphanNormal += n; else memberNormal += n;
    });
    return { rows: rows, foot: foot, memberNormal, orphanNormal,
             sheets: u.sheets, wasteTotal: u.wasteTotal || 0 };
  });
  const tnFoot = parseInt(String((paper.foot || [])[1] || '').replace(/[^\d]/g, ''), 10) || 0;
  chk('A4 合计「正常用纸」== 成员正常 + 未归属正常（灰行也进合计）',
      tnFoot === paper.memberNormal + paper.orphanNormal,
      { tnFoot, memberNormal: paper.memberNormal, orphanNormal: paper.orphanNormal });
  chk('A4 合计「正常用纸」== usage.sheets − wasteTotal（即真正计入摊销的量）',
      tnFoot === (paper.sheets || 0) - (paper.wasteTotal || 0),
      { tnFoot, sheets: paper.sheets, wasteTotal: paper.wasteTotal });

  /* ══ 5. A6：垫付清算表头跟本月生效口径 ══ */
  const shareTh10 = await page.evaluate(() => {
    const sec = window.__section('垫付清算');
    if (!sec) return null;
    const ths = Array.prototype.slice.call(sec.querySelectorAll('thead th'));
    return ths.map((th) => (th.textContent || '').trim()).join(' | ');
  });
  chk('A6 10 月（用量 > 0）垫付清算表头写「应摊（按用量）」且不带「已回退」',
      !!shareTh10 && shareTh10.indexOf('应摊（按用量）') >= 0 && shareTh10.indexOf('回退') < 0, shareTh10);

  /* ══ 6. A5：打印机折旧「本口径页数」合计排除父订单非正常用量终态 ══ */
  await page.evaluate(() => {
    const ov = document.querySelector('#blockOverlay');
    if (ov) ov.classList.remove('show');
    window.switchTab('printers');
  });
  /* 「本口径」区间选择器默认跟**当月**走，跨月后默认就变成下个月（打桩数据只在 10 月）
     ⇒ 本口径页数恒 0，下面的 waitForFunction 必然超时。所以这里显式把口径钉到 2026-10。 */
  await page.evaluate(() => window.onScopeTimeChange('m:2026-10'));
  await page.waitForFunction(() => {
    const sec = document.querySelector('#printer-usage');
    if (!sec) return false;
    const tr = sec.querySelector('tbody tr.tfoot');
    if (!tr) return false;
    const n = parseInt(String(tr.children[5] && tr.children[5].textContent || '').replace(/[^\d]/g, ''), 10);
    return n > 0;
  }, null, { timeout: 25000 });
  const printerSnap = await page.evaluate(() => {
    const sec = document.querySelector('#printer-usage');
    const foot = window.__tfoot(sec);
    const orders = window._cloudFilteredOrders() || [];
    const excl = window.NORMAL_EXCLUDED_STATUSES || {};
    let expect = 0, canceledPages = 0, expectOrderRows = 0;
    orders.forEach((o) => {
      const isExcl = !!excl[String(o.status || '')];
      let pg = 0;
      (o.files || []).forEach((f) => { if ((f.status || '') === 'sent') pg += window._filePagesOf(f); });
      if (isExcl) { canceledPages += pg; return; }
      if (pg > 0) { expect += pg; expectOrderRows++; }
    });
    /* 明细表是区块里的第二张表（第一张是按机汇总） */
    const tables = sec ? sec.querySelectorAll('table') : [];
    const detail = tables.length > 1 ? tables[1] : null;
    let detailRows = 0, detailPages = 0;
    if (detail) {
      Array.prototype.slice.call(detail.querySelectorAll('tbody tr')).forEach((tr) => {
        const tds = tr.children;
        if (tds.length < 6) return;
        detailRows++;
        detailPages += parseInt(String(tds[5].textContent || '').replace(/[^\d]/g, ''), 10) || 0;
      });
    }
    return { foot: foot, expect: expect, canceledPages: canceledPages, orderCount: orders.length,
             detailRows: detailRows, detailPages: detailPages, expectOrderRows: expectOrderRows };
  });
  const shownPages = parseInt(String((printerSnap.foot || [])[5] || '').replace(/[^\d]/g, ''), 10) || 0;
  chk('A5 夹具确实含「父订单已取消、子文件仍 sent」的单（8 页）—— 否则这条验收是假绿',
      printerSnap.canceledPages === 8, { canceledPages: printerSnap.canceledPages, orders: printerSnap.orderCount });
  chk('A5 「本口径页数」合计 == 只统计正常用量终态（已取消/已拒绝/预约中的单不计页）',
      shownPages === printerSnap.expect && shownPages === 410,
      { shown: shownPages, expect: printerSnap.expect, canceled: printerSnap.canceledPages });
  chk('A5 「订单 → 打印机」明细也不含已取消单（行数与页数和都只算正常用量）',
      printerSnap.detailRows === printerSnap.expectOrderRows
      && printerSnap.detailPages === printerSnap.expect,
      { rows: printerSnap.detailRows, wantRows: printerSnap.expectOrderRows,
        pages: printerSnap.detailPages, wantPages: printerSnap.expect });

  /* ══ 7. A7：实际转账合法性只有一个判据 ══ */
  await page.evaluate(() => { window.switchTab('dash'); window.onSelMonth('2026-10'); });
  await page.waitForFunction(() => document.querySelector('#dashTransferGaps'), null, { timeout: 20000 });
  await page.waitForTimeout(400);
  const trSnap = await page.evaluate(() => {
    const hasFn = typeof window._validTransfers === 'function';
    const m = window.monthFromRecords('2026-10');
    const vt = hasFn ? window._validTransfers(m) : null;
    const box = document.querySelector('#dashTransferGaps');
    const txt = box ? (box.textContent || '') : '';
    const dones = [];
    const re = /已转\s*([+\-−]?¥[\d,\.]+)/g;
    let mm;
    while ((mm = re.exec(txt))) dones.push(mm[1]);
    const r = window.calcMonthWithUsage('2026-10');
    return { hasFn: hasFn, validCount: vt ? vt.length : -1,
             validAmounts: vt ? vt.map((t) => window.parseCents(t.amount)) : [],
             dones: dones, recv: r.recv, sent: r.sent, txtLen: txt.length };
  });
  chk('A7 页面暴露了统一判据 _validTransfers()（chips 与 calcMonth 同源）', trSnap.hasFn === true);
  chk('A7 _validTransfers 过滤掉自转 / 已删成员 / 0 元，只留 1 条合法转账',
      trSnap.validCount === 1 && trSnap.validAmounts[0] === 10000,
      { validCount: trSnap.validCount, validAmounts: trSnap.validAmounts });
  chk('A7 calcMonth 的 recv/sent 也只认那条合法转账（张三 +¥100）',
      (trSnap.recv.ma || 0) === 10000 && (trSnap.sent.mb || 0) === 10000
      && (trSnap.recv.mc || 0) === 0 && (trSnap.recv.mzzz || 0) === 0,
      { recv: trSnap.recv, sent: trSnap.sent });
  chk('A7 chips 的「已转」不含垃圾记录（逐条都等于合法转账和）',
      trSnap.dones.length > 0 && trSnap.dones.every((s) => {
        const m = String(s).match(/[-−]?\d[\d,]*(?:\.\d+)?/);
        const c = m ? Math.round(parseFloat(m[0].replace('−', '-').replace(/,/g, '')) * 100) : -1;
        return [0, 10000].indexOf(c) >= 0;
      }), { dones: trSnap.dones });
  chk('A7 源码：chips 的「已转」逐条累加走 _vt（= _validTransfers(m)），不再直接遍历 m.transfers',
      /_vt\.forEach\(function \(x\) \{ if \(x\.from === t\.from/.test(src)
      && !/\(m\.transfers \|\| \[\]\)\.forEach\(function \(x\) \{ if \(x\.from === t\.from/.test(src)
      && (src.match(/_validTransfers\(/g) || []).length >= 2,
      { viaVt: /_vt\.forEach\(function \(x\) \{ if \(x\.from === t\.from/.test(src),
        viaRaw: /\(m\.transfers \|\| \[\]\)\.forEach\(function \(x\) \{ if \(x\.from === t\.from/.test(src),
        calls: (src.match(/_validTransfers\(/g) || []).length });

  /* ══ 8. A6 反向：11 月没有订单 ⇒ 用量 0 ⇒ 采购分摊回退均摊，表头必须写明 ══ */
  await goMonth('2026-11');
  const shareTh11 = await page.evaluate(() => {
    const sec = window.__section('垫付清算');
    if (!sec) return null;
    const ths = Array.prototype.slice.call(sec.querySelectorAll('thead th'));
    const r = window.calcMonthWithUsage('2026-11');
    return { head: ths.map((th) => (th.textContent || '').trim()).join(' | '),
             useUsage: r.useUsage, basisFallback: r.basisFallback };
  });
  chk('A6 11 月（用量 0）calcMonth 确实回退了均摊', shareTh11 && shareTh11.useUsage === false && shareTh11.basisFallback === true,
      shareTh11 && { useUsage: shareTh11.useUsage, basisFallback: shareTh11.basisFallback });
  chk('A6 11 月表头写「应均摊（本月用量 0，已回退）」，不再假装按用量',
      !!shareTh11 && shareTh11.head.indexOf('应均摊') >= 0 && shareTh11.head.indexOf('回退') >= 0
      && shareTh11.head.indexOf('按用量') < 0, shareTh11 && shareTh11.head);

  /* ══ 10. B 组（v7.11 结构收口）：同一件事只算一遍 ══
     B 组不是改数字，是改「谁算」。判据因此分两类：
       ① 结构：重复实现真的没了、块真的搬走了（源码 + 运行时双重）；
       ② 等价：搬/合并之后数字一个都没变（结算月对照列 == calcMonth 同源数）。 */
  const B_BLOCKS = ['dashKpiHTML', 'dashBannerHTML', 'dashExpenseHTML', 'dashShareHTML', 'dashDepHTML',
                    'dashIncomeHTML', 'dashPLHTML', 'dashAdvanceHTML', 'dashRedistHTML', 'dashTransferHTML'];

  /* B1 「正常用量」只剩一个实现：_orderNormalUsage()，结算侧与打印机折旧页共用 */
  chk('B1 源码：正常用量口径只剩 _orderNormalUsage() 一处实现（定义 + 2 个调用点）',
      (src.match(/_orderNormalUsage\(/g) || []).length === 3
      && /function _orderNormalUsage\(o\) \{/.test(src),
      { calls: (src.match(/_orderNormalUsage\(/g) || []).length });
  chk('B1 源码：结算侧取数 _usageFromOrders() 不再自己遍历子文件算页',
      sliceFn(src, '_usageFromOrders').indexOf('_filePagesOf') < 0
      && sliceFn(src, '_usageFromOrders').indexOf('_orderNormalUsage') > 0);
  chk('B1 源码：打印机折旧页 printerUsageSectionHTML() 不再自己遍历子文件算页',
      sliceFn(src, 'printerUsageSectionHTML').indexOf('_filePagesOf') < 0
      && sliceFn(src, 'printerUsageSectionHTML').indexOf('_orderNormalUsage') > 0);
  const unit = await page.evaluate(() => {
    const fn = window._orderNormalUsage;
    if (typeof fn !== 'function') return null;
    const canceled = { status: 'canceled', files: [{ status: 'sent', total_sheets: 4, total_pages: 8 }] };
    const normal = { status: 'sent', files: [{ status: 'sent', total_sheets: 50, total_pages: 100 },
                                             { status: 'failed', total_sheets: 9, total_pages: 18 }] };
    const a = fn(canceled), b = fn(normal);
    return { canceled: a, normal: b };
  });
  chk('B1 _orderNormalUsage() 的判据正确：只累 sent 子文件，非正常用量只打 excluded 标记而两份实现各自早退',
      unit && unit.canceled.excluded === true && unit.canceled.pages === 8 && unit.canceled.sheets === 4
      && unit.normal.excluded === false && unit.normal.pages === 100 && unit.normal.sheets === 50,
      unit);

  /* B2 打印机折旧页并排给出「结算月」同机数字（与「收支清算 → 设备折旧」同一个数） */
  await goMonth('2026-10');
  await page.evaluate(() => window.switchTab('printers'));
  await page.waitForFunction(() => !!document.querySelector('#printer-usage table'), null, { timeout: 20000 });
  await page.evaluate(() => window.onScopeTimeChange('m:2026-10'));
  await page.waitForFunction(() => {
    const s = document.querySelector('#scopeTimeSel-p');
    const t = document.querySelector('#printer-usage table');
    return !!s && s.value === 'm:2026-10' && !!t && t.querySelectorAll('tbody tr').length >= 2;
  }, null, { timeout: 20000 });
  await page.waitForTimeout(1500);
  const b2 = await page.evaluate(() => {
    const t = document.querySelectorAll('#printer-usage table')[0];
    if (!t) return null;
    const ths = Array.prototype.slice.call(t.querySelectorAll('thead th')).map((x) => (x.textContent || '').trim());
    const iP = ths.indexOf('本口径页数'), iS = ths.indexOf('结算月页数'), iD = ths.indexOf('结算月折旧');
    const u = window.usageOfMonth('2026-10');
    const printers = window.getPrinters();
    const rows = Array.prototype.slice.call(t.querySelectorAll('tbody tr')).filter((tr) => {
      return !tr.classList.contains('tfoot');
    }).map((tr) => Array.prototype.slice.call(tr.children).map((td) => (td.textContent || '').trim()));
    const foot = t.querySelector('tbody tr.tfoot');
    return {
      ths: ths, colCount: ths.length, iP: iP, iS: iS, iD: iD,
      rows: rows, footCells: foot ? foot.children.length : 0,
      want: rows.map((r) => {
        const p = printers.filter((x) => (r[0] || '').indexOf(x.name) >= 0)[0] || null;
        const pages = (p && u && ((u.pagesByPrinter || {})[p.id] || 0)) || 0;
        return { name: r[0], id: p ? p.id : null, pages: pages,
                 dep: p ? window.printerDepCents(p, pages) : 0,
                 scopePages: r[iP], settlePages: r[iS], settleDep: r[iD], cellCount: r.length };
      }),
    };
  });
  chk('B2 打印机折旧表新增「结算月页数 / 结算月折旧」两列（8 列 → 10 列，合计行同步）',
      !!b2 && b2.colCount === 10 && b2.iS > 0 && b2.iD === b2.iS + 1 && b2.footCells === 10,
      b2 && { colCount: b2.colCount, iS: b2.iS, iD: b2.iD, footCells: b2.footCells, ths: b2.ths });
  let b2ok = true; const b2detail = [];
  /* 页数是整数（不能走 cash()，那个按「元 → 分」×100）；金额才用 cash() */
  const pagesOf = (s) => { const m = String(s || '').replace(/,/g, '').match(/\d+/); return m ? parseInt(m[0], 10) : null; };
  ((b2 && b2.want) || []).forEach((w) => {
    const sp = pagesOf(w.settlePages);
    const sd = cash(w.settleDep);
    const ok = !!w.id && sp === w.pages && Math.abs(sd || 0) === w.dep && pagesOf(w.scopePages) === w.pages;
    if (!ok) { b2ok = false; b2detail.push({ w: w, sp: sp, sd: sd, scope: pagesOf(w.scopePages) }); }
  });
  chk('B2 「结算月页数 / 结算月折旧」== calcMonth 的同机 pagesByPrinter 与 printerDepCents（不是页面另算的一套数）',
      !!b2 && b2.want.length === 2 && b2ok,
      JSON.stringify(b2detail.length ? b2detail : (b2 && b2.want)));
  chk('B2 当上方口径正好等于结算月、也不含外部来源时，「本口径页数」与「结算月页数」相等（差异只因口径不同而产生）',
      !!b2 && b2.want.length === 2 && b2.want.every((w) => pagesOf(w.scopePages) === pagesOf(w.settlePages)),
      b2 && b2.want.map((w) => ({ scope: w.scopePages, settle: w.settlePages })));

  /* B3 renderDash 只剩编排：10 个区块函数必须存在，块内容不许再 inline 回主函数 */
  const b3 = await page.evaluate((names) => {
    const miss = names.filter((n) => typeof window[n] !== 'function');
    return { miss: miss, count: names.length - miss.length };
  }, B_BLOCKS);
  chk('B3 收支清算 10 个展示区块都已拆成独立函数（KPI / 横幅 / 采购 / 分摊 / 折旧 / 收入 / 损益 / 垫付 / 重分配 / 转账）',
      b3.miss.length === 0, b3.miss);
  const rd = sliceFn(src, 'renderDash');
  /* v7.12：块调用被 secFold() 包在参数里（h += secFold(..., dashXxxHTML(r), ...)），
     所以判据从「h += block(」放宽成「block( 出现在主函数里」，仍然要求 10 个都在、且只出现一次。 */
  const calls = B_BLOCKS.filter((n) => rd.indexOf(n + '(') >= 0);
  chk('B3 主函数 renderDash() 只按顺序调用这 10 个块（调用点齐全）',
      calls.length === 10, { called: calls, miss: B_BLOCKS.filter((n) => calls.indexOf(n) < 0) });
  chk('B3 主函数里不再残留块内容（没有 <table>、没有 kpi-grid）—— 有人把块 inline 回去就会红',
      !!rd && rd.indexOf('<table') < 0 && rd.indexOf('kpi-grid') < 0 && rd.length < 7200,
      rd && { len: rd.length, hasTable: rd.indexOf('<table') >= 0, hasKpi: rd.indexOf('kpi-grid') >= 0 });
  const inBlock = B_BLOCKS.filter((n) => sliceFn(src, n).indexOf('<table') >= 0);
  chk('B3 表格内容确实搬进了块函数（采购/分摊/折旧/收入/损益/垫付/重分配/转账 8 个块各含一张表）',
      inBlock.length === 8, { withTable: inBlock });

  /* ══ 12. C 组（v7.11 口径总线）：calcMonth 直接产出展示模型 view，页面零重算 ══
     C 组的判据不是「数字对不对」（那是 A 组），而是「页面还有没有自己算」：
       ① view 六组行 + totals 必须与 calcMonth 顶层字段逐个同源（页面照抄即可）；
       ② view.redist 的三列相加必须恒等于「建议转账」用的 net（把碰巧一致变成不变量）；
       ③ 结论卡删除后，降级仍必须在状态条 / KPI 副标题里自己说出来（不许假装准）。 */
  await goMonth('2026-10');
  const c1 = await page.evaluate(() => {
    const r = window.calcMonthWithUsage('2026-10');
    const v = r.view || null;
    const ids = (r.members || []).map((m) => m.id);
    const byId = (arr) => { const o = {}; (arr || []).forEach((x) => { o[x.id] = x; }); return o; };
    const SH = byId(v && v.share), IN = byId(v && v.income), PL = byId(v && v.pl);
    const AD = byId(v && v.advance), RD = byId(v && v.redist), RL = byId(v && v.realPL);
    const bad = { transfer: [], sumCheck: [], mirror: [] };
    ids.forEach((id) => {
      const rd = RD[id] || {}, sh = SH[id] || {}, inc = IN[id] || {}, pl = PL[id] || {};
      const ad = AD[id] || {}, rl = RL[id] || {};
      if (rd.transfer !== r.net[id]) bad.transfer.push({ id: id, transfer: rd.transfer, net: r.net[id] });
      if (rd.sumCheck !== rd.transfer || rd.sumCheck !== ((rd.incomeAdj || 0) + (rd.adv || 0) + (rd.wear || 0))) {
        bad.sumCheck.push({ id: id, sumCheck: rd.sumCheck, transfer: rd.transfer });
      }
      if (sh.share !== r.perPersonShare[id]) bad.mirror.push({ id: id, k: 'share.share' });
      if (inc.redist !== r.redist[id]) bad.mirror.push({ id: id, k: 'income.redist' });
      if (pl.pl !== r.bookPL[id]) bad.mirror.push({ id: id, k: 'pl.pl' });
      if (ad.adv !== ((r.paid[id] || 0) - r.perPersonShare[id])) bad.mirror.push({ id: id, k: 'advance.adv' });
      if (rl.actual !== r.actualPL[id] || rl.book !== r.bookPL[id] || rl.gap !== r.settleGap[id]) {
        bad.mirror.push({ id: id, k: 'realPL{actual,book,gap}' });
      }
    });
    const sum = (arr, k) => (arr || []).reduce((a, x) => a + (x[k] || 0), 0);
    const t = v ? v.totals : null;
    const tBad = [];
    if (t) {
      if (t.totalPL !== sum(v.pl, 'pl')) tBad.push('totalPL');
      if (t.totalActual !== sum(v.realPL, 'actual')) tBad.push('totalActual');
      if (t.shareTotal !== sum(v.share, 'share')) tBad.push('shareTotal');
      if (t.advSum !== sum(v.advance, 'adv')) tBad.push('advSum');
      if (t.gapSum !== sum(v.realPL, 'gap')) tBad.push('gapSum');
      if (t.redistTotal !== sum(v.income, 'redist')) tBad.push('redistTotal');
      if (t.wearSum !== sum(v.pl, 'wear')) tBad.push('wearSum');
    }
    return {
      hasView: !!v, memberCount: ids.length,
      lens: v ? { share: v.share.length, income: v.income.length, pl: v.pl.length,
                  advance: v.advance.length, redist: v.redist.length, realPL: v.realPL.length } : null,
      bad: bad, tBad: tBad, totals: t, hasCaliber: !!(v && v.caliber),
    };
  });
  chk('C1 calcMonth 产出展示模型 view（6 组行 + totals + caliber），行数都等于成员数',
      c1.hasView && c1.hasCaliber && !!c1.lens && c1.memberCount === 3
      && ['share', 'income', 'pl', 'advance', 'redist', 'realPL'].every((k) => c1.lens[k] === 3),
      { hasView: c1.hasView, hasCaliber: c1.hasCaliber, lens: c1.lens });
  chk('C1 view 六组行与 calcMonth 顶层字段逐个同源（页面照抄就够，不用再算）',
      c1.bad.mirror.length === 0, c1.bad.mirror);
  chk('C1 view.redist 的「= 应转出/入」恒等于建议转账用的 net（不再现场三列相加）',
      c1.bad.transfer.length === 0, c1.bad.transfer);
  chk('C1 view.redist 的 sumCheck（三列相加）恒等于 transfer —— 把「碰巧一致」变成可断言的不变量',
      c1.bad.sumCheck.length === 0, c1.bad.sumCheck);
  chk('C1 view.totals 与逐行求和一列不差（7 个合计全部同源）',
      c1.tBad.length === 0, { bad: c1.tBad, totals: c1.totals });

  const centsOf = (s) => { const m = String(s || '').match(/[-−]?\d[\d,]*(?:\.\d+)?/); return m ? Math.round(parseFloat(m[0].replace('−', '-').replace(/,/g, '')) * 100) : null; };
  const c2 = await page.evaluate(() => {
    const box = document.querySelector('#dashVerdict');
    const card = document.querySelector('#dashVerdictCard');
    /* v7.15：结论卡已删除 —— 原卡里的两条数字校验落到仍渲染转账建议的明细区上 */
    const tabTxt = String((document.querySelector('#tab-dash') || { textContent: '' }).textContent || '').replace(/\s+/g, '');
    const txt = box ? String(box.textContent || '').replace(/\s+/g, '') : '';
    const r = window.calcMonthWithUsage('2026-10');
    const yuan = (c) => (Math.round(c) / 100).toFixed(2);
    const kpi = document.querySelector('#tab-dash .kpi-card:nth-child(3) .num');
    const kpiPL = kpi ? parseInt(kpi.getAttribute('data-count'), 10) : null;
    const foot = document.querySelector('#dashRealPL tbody tr.tfoot');
    const footActual = foot ? (foot.children[6] ? foot.children[6].textContent : '') : '';
    const vt = typeof window._validTransfers === 'function'
      ? window._validTransfers(window.monthFromRecords('2026-10')) : [];
    /* 单位陷阱：建议转账 r.transfers[].amount 已是「分」（calcMonth 里统一下来的），
       而人工录入的 m.transfers[].amount 是「元」字符串 —— 所以这里只对后者 parseCents。
       （曾经把两者混用，卡片上 ¥150.00 显示成 ¥15000.00，就是这条断言抓出来的。）
       冲抵也是按「付款人→收款人」这一对汇总后再逐笔分配：同一对可能有多笔建议（结算 + 损耗费），
       而实际转账没记「这笔是付给哪一条建议的」，每笔各自减一遍全额会让同一笔钱被抵扣两次。 */
    const pairDone = {};
    vt.forEach((x) => { const k = x.from + '|' + x.to; pairDone[k] = (pairDone[k] || 0) + (parseCents(x.amount) || 0); });
    let leftSum = 0, open = 0, suggestSum = 0;
    (r.transfers || []).forEach((t) => {
      const k = t.from + '|' + t.to;
      const pool = pairDone[k] || 0;
      const amt = t.amount || 0;
      const used = Math.min(pool, amt);
      pairDone[k] = pool - used;
      suggestSum += amt;
      if (amt - used > 1) { leftSum += amt - used; open++; }
    });
    let doneSum = 0;
    vt.forEach((x) => { doneSum += (parseCents(x.amount) || 0); });
    const amtMiss = (r.transfers || []).filter((t) => tabTxt.indexOf(yuan(t.amount || 0)) < 0).length;
    return {
      hasBox: !!box, hasCard: !!card, txt: tabTxt,
      kpiPL: kpiPL, totalPL: r.view.totals.totalPL,
      footActual: footActual, totalActual: r.view.totals.totalActual,
      leftSum: leftSum, open: open, amtMiss: amtMiss, transferCnt: (r.transfers || []).length,
      suggestSum: suggestSum, doneSum: doneSum,
      /* 「还差」两个字也出现在区块说明文案里（「已转 / 还差」会立即重算），
         所以这里只认后面紧跟金额的 chips 文案：还差 ¥x.xx */
      saysLeft: tabTxt.indexOf('还差¥') >= 0, leftTxt: yuan(leftSum),
    };
  });
  chk('C2 收支清算不再有「本月结论」卡（v7.15 删除；dashVerdictHTML 一并移除，不留死函数）',
      !c2.hasBox && !c2.hasCard && !/function\s+dashVerdictHTML\s*\(/.test(src),
      { hasBox: c2.hasBox, hasCard: c2.hasCard, txt: c2.txt });
  chk('C2 「该谁给谁」逐笔金额与建议转账一致（渲染在转账建议里，不是另抄一份数字）',
      c2.amtMiss === 0, { miss: c2.amtMiss, transfers: c2.transferCnt });
  chk('C2 「还差」合计 == 各笔未到位之和（与「实际转账」的缺口同源，同一笔钱不被抵扣两次）',
      c2.open === 0 ? c2.saysLeft === false : (c2.saysLeft && c2.txt.indexOf(c2.leftTxt) >= 0),
      { leftSum: c2.leftSum, open: c2.open, leftTxt: c2.leftTxt, saysLeft: c2.saysLeft });
  chk('C2 「还差」合计 == 建议合计 − 已转合计（按对分配，不多扣也不少扣）',
      c2.leftSum === c2.suggestSum - c2.doneSum,
      { leftSum: c2.leftSum, suggestSum: c2.suggestSum, doneSum: c2.doneSum });
  chk('C2 页面读数与 view 同源：KPI 本月盈亏 == totals.totalPL，实际盈亏合计 == totals.totalActual',
      c2.kpiPL === c2.totalPL && centsOf(c2.footActual) === c2.totalActual,
      { kpiPL: c2.kpiPL, totalPL: c2.totalPL, footActual: c2.footActual, totalActual: c2.totalActual });

  /* C3：六个表全部改吃 view（源码级）—— 有人把现场重算加回来就会红 */
  const C_VIEW_BLOCKS = ['dashShareHTML', 'dashIncomeHTML', 'dashPLHTML', 'dashAdvanceHTML', 'dashRedistHTML', 'realPLSectionHTML'];
  const cBodies = {};
  C_VIEW_BLOCKS.forEach((n) => { cBodies[n] = sliceFn(src, n); });
  chk('C3 六个表的数据来源都是 view（每个函数体里都出现 r.view 的对应数组）',
      ['v.share', 'v.income', 'v.pl', 'v.advance', 'v.redist', 'view.realPL']
        .every((k, i) => (cBodies[C_VIEW_BLOCKS[i]] || '').indexOf(k) >= 0),
      Object.keys(cBodies).map((n) => [n, (cBodies[n] || '').length]));
  chk('C3 六个表里不再出现现场重算（没有 parseCents((m.income、没有自算权重 / 合计的 getMembers().forEach）',
      C_VIEW_BLOCKS.every((n) => (cBodies[n] || '').indexOf('parseCents((m.income') < 0
                            && (cBodies[n] || '').indexOf('getMembers().forEach') < 0),
      C_VIEW_BLOCKS.filter((n) => (cBodies[n] || '').indexOf('parseCents((m.income') >= 0
                          || (cBodies[n] || '').indexOf('getMembers().forEach') >= 0));
  chk('C3 「资金重分配」直接取 row.transfer（= net），不再现场 incomeAdj + adv + wear',
      (cBodies.dashRedistHTML || '').indexOf('row.transfer') >= 0
      && (cBodies.dashRedistHTML || '').indexOf('incomeAdj + adv + wear') < 0,
      { hasRowTransfer: (cBodies.dashRedistHTML || '').indexOf('row.transfer') >= 0,
        hasInlineSum: (cBodies.dashRedistHTML || '').indexOf('incomeAdj + adv + wear') >= 0 });
  chk('C3 renderDash 不再挂结论卡锚点 #dashVerdict，dashVerdictHTML() 已删除（v7.15）',
      !/id="dashVerdict"/.test(sliceFn(src, 'renderDash') || '')
      && !/function\s+dashVerdictHTML\s*\(/.test(src),
      { inDash: /id="dashVerdict"/.test(sliceFn(src, 'renderDash') || ''), hasFn: /function\s+dashVerdictHTML\s*\(/.test(src) });
  chk('C3 结论卡删除后 _refreshDashSummary 不再引用 #dashVerdict（不留无效重绘）',
      !/#dashVerdict/.test(sliceFn(src, '_refreshDashSummary') || ''),
      (sliceFn(src, '_refreshDashSummary') || '').slice(0, 200));

  /* C4 反向：11 月用量为 0 ⇒ 页面必须自己说「已回退均摊 / 临时口径」，不许假装准（v7.15 起在状态条 + KPI 副标题） */
  await goMonth('2026-11');
  const c4 = await page.evaluate(() => {
    const txt = String((document.querySelector('#tab-dash') || { textContent: '' }).textContent || '').replace(/\s+/g, '');
    const r = window.calcMonthWithUsage('2026-11');
    return { txt: txt.slice(0, 300), fallback: r.view.caliber.basisFallback,
             useUsage: r.view.caliber.useUsage, hasBanner: txt.indexOf('本次按均摊计算') >= 0,
             hasSub: txt.indexOf('均摊（未取到用量）') >= 0 };
  });
  chk('C4 11 月（用量 0、已回退均摊）页面把降级说出来：状态条「本次按均摊计算」+ KPI 副标题「均摊（未取到用量）」',
      c4.fallback === true && c4.useUsage === false && c4.hasBanner && c4.hasSub, c4);

  /* ══ 13. v7.12 折叠版式：明细区收起成「一行摘要」，展开才是明细 ══
     判据不是"好不好看"，而是三件事：
       ① 默认全部收起、且收起态真的没有高度（否则等于没收敛）；
       ② 摘要行上的数字必须来自 view / usage，不是另算一份；
       ③ 展开只切 data-open（不重绘整页），状态记进 localStorage。 */
  await goMonth('2026-10');
  const fold = await page.evaluate(() => {
    const secs = Array.from(document.querySelectorAll('#tab-dash .cs'));
    const sums = {};
    secs.forEach((s) => {
      const n = s.querySelector('.cs-sum');
      sums[s.id.replace(/^cs-/, '')] = n ? n.textContent.replace(/\s+/g, ' ').trim() : '';
    });
    return {
      n: secs.length,
      keys: secs.map((s) => s.id.replace(/^cs-/, '')),
      open: secs.filter((s) => s.getAttribute('data-open') === '1').length,
      bodyH: secs.map((s) => Math.round(s.querySelector('.cs-body').getBoundingClientRect().height)),
      h2hidden: secs.every((s) => {
        const h = s.querySelector('.section > h2, .settle-box > h2');
        return !h || getComputedStyle(h).display === 'none';
      }),
      aria: secs.every((s) => s.querySelector('.cs-head').getAttribute('aria-expanded') === 'false'),
      count: (document.querySelector('#foldCount') || {}).textContent || '',
      hasBar: !!document.querySelector('#dashFoldBar'),
      height: document.querySelector('#tab-dash').scrollHeight,
      sums: sums,
      missingFns: ['secFold', 'foldBarHTML', 'foldAllDash', '_foldSums', '_applyFoldSums', 'foldIsOpen', 'foldSet']
        .filter((n) => typeof window[n] !== 'function'),
    };
  });
  chk('v7.12/14 十一个明细区都折成了折叠块（……/垫付/结算公式/重分配/转账/实际盈亏）',
      fold.n === 11 && fold.keys.join(',') === 'expense,paper,amort,dep,income,pl,advance,formula,redist,transfer,realpl',
      fold.keys);
  chk('v7.12 默认全部收起（收敛态才叫"抓得住重点"）', fold.open === 0 && fold.aria === true, JSON.stringify({ open: fold.open, aria: fold.aria }));
  chk('v7.12 收起态 body 高度都是 0（不是靠 overflow 视觉隐藏）', fold.bodyH.every((h) => h < 1), fold.bodyH);
  chk('v7.12 收起后整页 < 1600px（改版前 4987px：结论卡 + KPI + 10 行摘要就够看完；v7.15 删卡后只会更短）',
      fold.height > 0 && fold.height < 1600, fold.height + 'px');
  chk('v7.12 标题上移到摘要行（块内 h2 隐藏，但仍留在 DOM 里给脚本按文案查）', fold.h2hidden);
  chk('v7.12 工具条写着「已收起 N / 11 块」且与实际一致',
      fold.hasBar && /已收起 11 \/ 11 块/.test(fold.count), fold.count);
  chk('v7.12/14 每块摘要行都写了这一块最该看的数（11 块全非空）',
      Object.keys(fold.sums).length === 11 && Object.keys(fold.sums).every((k) => (fold.sums[k] || '').length > 2),
      fold.sums);
  chk('v7.12 摘要行数字与 view/usage 同源（采购总额 / 团队折旧 / 损耗费单列 / 已录转账）',
      String(fold.sums.expense || '').indexOf('300.00') >= 0 && /^团队/.test(String(fold.sums.dep || ''))
      && /损耗费/.test(String(fold.sums.dep || '')) && /已录/.test(String(fold.sums.transfer || '')),
      { expense: fold.sums.expense, dep: fold.sums.dep, transfer: fold.sums.transfer });
  chk('v7.12 折叠相关函数都在（secFold / foldBarHTML / foldAllDash / _foldSums / _applyFoldSums / foldIsOpen / foldSet）',
      fold.missingFns.length === 0, fold.missingFns);

  const toggle = await page.evaluate(async () => {
    const sec = document.querySelector('#cs-expense');
    sec.querySelector('.cs-head').click();
    await new Promise((r) => setTimeout(r, 340));
    const out = {
      open: sec.getAttribute('data-open'),
      aria: sec.querySelector('.cs-head').getAttribute('aria-expanded'),
      bodyH: Math.round(sec.querySelector('.cs-body').getBoundingClientRect().height),
      innerH: sec.querySelector('.cs-inner').scrollHeight,
      store: localStorage.getItem('finance.fold.v1'),
    };
    sec.querySelector('.cs-head').click();
    await new Promise((r) => setTimeout(r, 340));
    out.reclosed = Math.round(sec.querySelector('.cs-body').getBoundingClientRect().height);
    return out;
  });
  chk('v7.12 点开：data-open=1、aria-expanded=true、高度动画到内容高度（不是瞬移也不是裁切）',
      toggle.open === '1' && toggle.aria === 'true' && Math.abs(toggle.bodyH - toggle.innerH) < 2, toggle);
  chk('v7.12 展开状态记进 localStorage（下次打开还是你上次的样子）',
      /"expense":1/.test(toggle.store || ''), toggle.store);
  chk('v7.12 再点收起：高度回到 0', toggle.reclosed < 1, toggle.reclosed);

  await page.evaluate(() => window.foldAllDash(1));
  await page.waitForTimeout(400);
  const allOpen = await page.evaluate(() => ({
    open: Array.from(document.querySelectorAll('#tab-dash .cs')).filter((s) => s.getAttribute('data-open') === '1').length,
    count: (document.querySelector('#foldCount') || {}).textContent || '',
    height: document.querySelector('#tab-dash').scrollHeight,
  }));
  chk('v7.12「全部展开」把 11 块都摊开且计数同步', allOpen.open === 11 && /已收起 0 \/ 11 块/.test(allOpen.count), allOpen);
  chk('v7.12 展开后仍装得下全部明细（高度回到 4000px 以上，说明内容没被折叠逻辑吃掉）',
      allOpen.height > 4000, allOpen.height + 'px');
  await page.evaluate(() => window.foldAllDash(0));
  await page.waitForTimeout(320);

  chk('v7.12/14 renderDash 只用 foldBarHTML + 11 个 secFold 编排（再有人把表格 inline 回主函数会红）',
      rd.indexOf('foldBarHTML()') >= 0 && (rd.match(/secFold\(/g) || []).length === 11
      && rd.indexOf('<table') < 0 && rd.indexOf('kpi-grid') < 0,
      { secFold: (rd.match(/secFold\(/g) || []).length, len: rd.length });
  chk('v7.12 记一笔转账后摘要行跟着重算（_refreshDashSummary 里挂了 _applyFoldSums）',
      sliceFn(src, '_refreshDashSummary').indexOf('_applyFoldSums(') >= 0);

  /* ══ 14. v7.14：结算公式也是一个自写 secFold 折叠块（原生 <details> 那套已删净） ══
     它曾经是整页唯一的原生 <details>（v7.3），v7.13 还给它单独写过 WAAPI 高度动画；
     v7.14 统一到 secFold —— 一份折叠实现、一份动画、一份 localStorage 记忆。
     这里钉三件事：① DOM 是标准折叠块且默认收起；② 点开后有内容、摘要行与自检同源；
     ③ 源码里不再有第二套开关（details / _formulaAnimRun / toggle 监听）。 */
  const fx = await page.evaluate(async () => {
    const sec = document.querySelector('#cs-formula');
    if (!sec) return { ok: false };
    const wait = (ms) => new Promise((r) => setTimeout(r, ms));
    const out = { ok: true };
    out.static = {
      isCs: sec.classList.contains('cs'),
      open: sec.getAttribute('data-open'),
      aria: sec.querySelector('.cs-head').getAttribute('aria-expanded'),
      sum: ((sec.querySelector('.cs-sum') || {}).textContent || '').replace(/\s+/g, ' ').trim(),
      hasFormulaBody: !!sec.querySelector('#cs-formula-body .cs-inner .formula-body'),
      detailsLeft: document.querySelectorAll('details.formula-box, #settleFormula').length,
    };
    sec.querySelector('.cs-head').click();
    await wait(340);
    const body = sec.querySelector('.cs-body');
    out.open = {
      open: sec.getAttribute('data-open'),
      aria: sec.querySelector('.cs-head').getAttribute('aria-expanded'),
      h: Math.round(body.getBoundingClientRect().height),
      inner: sec.querySelector('.cs-inner').scrollHeight,
      store: localStorage.getItem('finance.fold.v1') || '',
      chkCount: sec.querySelectorAll('.chk').length,
    };
    const t = sec.textContent || '';
    out.text = ['① 团队层', '② 成员层', '③ 三条恒等式', '④ 取整规则', '⑤ 页面上每张表']
      .every((x) => t.indexOf(x) >= 0);
    out.ident = /✅ 自检通过|❌ 三处对不上/.test(t);
    out.steps = (t.match(/<tr><td>(?:[1-9]|10)<\/td>/g) || []).length;
    sec.querySelector('.cs-head').click();
    await wait(340);
    out.reclosed = Math.round(body.getBoundingClientRect().height);
    return out;
  });
  chk('v7.14 结算公式是标准折叠块（#cs-formula / .cs / 摘要行 / .cs-inner>.formula-body / 默认收起）',
      fx.ok && fx.static.isCs && fx.static.open === '0' && fx.static.aria === 'false'
      && fx.static.hasFormulaBody && fx.static.sum.length > 2, fx.static);
  chk('v7.14 页面里已经没有原生 <details> 公式块（details.formula-box / #settleFormula）',
      fx.ok && fx.static.detailsLeft === 0, fx.static && fx.static.detailsLeft);
  chk('v7.14 点开：高度动画到内容高度、aria 与 localStorage 同步（与其它块同一套开关）',
      fx.ok && fx.open.open === '1' && fx.open.aria === 'true'
      && Math.abs(fx.open.h - fx.open.inner) < 2 && /"formula":1/.test(fx.open.store), fx.open);
  chk('v7.14 展开后五个小节齐、恒等式自检标记在、再点收起高度归 0',
      fx.ok && fx.text === true && fx.ident === true && fx.reclosed < 1,
      { text: fx.ok && fx.text, ident: fx.ok && fx.ident, reclosed: fx.ok && fx.reclosed });

  chk('v7.14 renderDash 用 secFold(\'formula\') 编排、settleFormulaHTML 只收一个数据参数',
      src.indexOf("secFold('formula'") >= 0
      && /function settleFormulaHTML\(r\) \{/.test(src)
      && src.indexOf('function settleFormulaHTML(r, open)') < 0);
  chk('v7.14 v7.13 的 details 动画已删净（无 _formulaAnimRun / FORMULA_ANIM / toggle 监听）',
      src.indexOf('_formulaAnimRun') < 0 && src.indexOf('FORMULA_ANIM') < 0
      && src.indexOf('_isFormulaSummary') < 0 && src.indexOf("document.addEventListener('toggle'") < 0);
  const fBody = sliceFn(src, 'settleFormulaHTML');
  chk('v7.14 摘要行与块内自检同源（共用 _formulaIdent，块内不再自己累加一遍）',
      /function _formulaIdent\(r\) \{/.test(src)
      && sliceFn(src, '_foldSums').indexOf('_formulaIdent(r)') >= 0
      && fBody.indexOf('_formulaIdent(r)') >= 0
      && fBody.indexOf('Math.round') < 0
      /* 只「调用」_formulaIdent 还不够：v21 变体保留调用、又把 sumNet 现场重算了一遍，
         摘要行与块内就可能各说一套 —— 所以块内不许再出现累加/遍历成员。 */
      && fBody.indexOf('sumNet +=') < 0 && fBody.indexOf('sumBook +=') < 0
      && fBody.indexOf('getMembers().forEach') < 0);
  chk('v7.14 就地刷新：重绘同一套折叠块并同步「已收起 N / M 块」',
      /f\.innerHTML = secFold\('formula'/.test(src)
      && sliceFn(src, '_refreshDashSummary').indexOf('_syncFoldCount()') >= 0
      && src.indexOf('keepOpen') < 0);

  /* ══ 11. 页面干净：没有 JS 报错、没有外网请求 ══ */
  chk('无 JS 运行时错误（pageerror）', pageErrors.length === 0, pageErrors.slice(0, 3));
  chk('无外网请求（全程只打本地桩）', externalReq.length === 0, externalReq.slice(0, 3));
} catch (e) {
  bad++;
  console.log('[FAIL] 断言执行中断：' + (e && e.message ? e.message : e));
} finally {
  await browser.close();
  server.close();
}

console.log('\n' + '='.repeat(52));
console.log('已跑断言 ' + total + ' 项，失败 ' + bad + ' 项');
if (bad === 0) console.log('收支清算跨区块恒等式验收：全部通过 ✅');
else console.log('收支清算跨区块恒等式验收：有 ' + bad + ' 项不通过 ❌');
process.exit(bad === 0 ? 0 : 1);
