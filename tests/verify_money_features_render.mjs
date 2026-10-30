/* 验收 · 收支清算 v7.10 两个钱相关需求的**真实渲染**（需求 A 硒鼓所属打印机 / 需求 B 废纸）
 *
 * 为什么必须真浏览器：这两条需求的落点都是「渲染出来的表格 + 可编辑的输入框 + 保存链路」：
 *   ① 「所属打印机」列只有在有耗材行时才出现（表头/列数/单元格文案是渲染结果）；
 *   ② 废纸格是 contenteditable 之外的 <input>，改完要经 onchange → updWastePaper → save()
 *      → POST /api/proxy/finance/config 真正落库，再重绘成「正常 N + 废 M 张」；
 *   ③ 切换月份要看对应月的人工值（不是内存里的旧值）；
 *   ④ 非法输入必须不落库、弹错、恢复有效值。
 * 这些都只有真渲染 + 真请求能回答。
 *
 * 做法：起本地 HTTP 服务（静态文件来自 local_print_tool/finance/，/api/** 全部打桩，
 * 绝不发外网请求），Playwright 打开 settlement.html，按场景逐条断言：
 *   A 基线：用纸统计 / 长期耗材摊销 两个区块的数字、废纸预估与人工值、接口带了 status；
 *   B 编辑废纸：改 → 落库 → 重绘；清空 → 删格；填 0 → 与留空不同；非法输入 → 不落库；
 *   C 采购支出表：「所属打印机」列只在耗材行出现，硒鼓行是下拉且选中了台账里的机器；
 *   D 切换月份：11 月读 11 月的人工值/预估，切回 10 月值复原。
 *
 * 用法：node tests/verify_money_features_render.mjs
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
const ROOT = path.join(HERE, '..');
const FINANCE = path.join(ROOT, 'local_print_tool', 'finance');
const HTML_PATH = process.argv[2] || path.join(FINANCE, 'settlement.html');
const OUT_DIR = path.join(ROOT, '截图展示', 'v7.10_钱相关需求');
const STAMP = new Date().toISOString().replace(/[:.]/g, '-').replace('T', '_').slice(0, 19);

let bad = 0, total = 0;
function chk(name, cond, detail) {
  total++;
  if (!cond) bad++;
  console.log('[' + (cond ? 'PASS' : 'FAIL') + '] ' + name + (!cond && detail !== undefined ? '  -- ' + detail : ''));
}

/* ── 夹具 ───────────────────────────────────────────────────────────────── */
const P1020 = { id: 'p1020', name: 'HP LaserJet 1020', model: 'HP LaserJet 1020',
                client_id: 'c1020', price: 0, life_pages: 0, life_mode: 'estimated',
                status: 'active', billing: 'team', owner: 'shared' };
const PM232 = { id: 'pm232', name: 'HP LaserJet MFP M232-M237', model: 'HP LaserJet MFP M232-M237',
                client_id: 'cm232', price: 0, life_pages: 0, life_mode: 'estimated',
                status: 'active', billing: 'team', owner: 'shared' };

const STORE = {
  config: {
    investRate: 0.05, scoreCap: 15, passwords: {}, memberBindings: {},
    members: [{ id: 'ma', name: '张三', color: '#4472C4' },
              { id: 'mb', name: '李四', color: '#ED7D31' }],
    printers: [P1020, PM232],
    purchaseBasis: 'equal',
    /* 10 月：李四填了 8 张人工废纸，张三留空（用预估） */
    waste_paper: { '2026-10': { mb: 8 } },
  },
  records: {
    expenses: [
      /* 硒鼓，指定给 M232（需求 A 的核心数据） */
      { id: 'e2', date: '2026-10-22', item: 'M232 硒鼓', kind: 'toner',
        units: 3000, amount: 128.98, payer: 'ma', printer_id: 'pm232' },
      /* A4纸：不分机器 */
      { id: 'e1', date: '2026-10-01', item: 'A4纸一箱', kind: 'paper', units: 1000, amount: 100, payer: 'ma' },
      /* 普通采购：不该有「所属打印机」下拉 */
      { id: 'e3', date: '2026-10-05', item: '文件夹', kind: 'other', amount: 20, payer: 'mb' },
      /* 11 月：只为让月份下拉里有 11 月 */
      { id: 'e4', date: '2026-11-02', item: '11月A4纸', kind: 'paper', units: 500, amount: 50, payer: 'ma' },
    ],
    income: [], scores: [], transfers: [],
  },
  notes: {}, meta: { version: 1 },
};

function ord(id, who, status, day, cid, files, price) {
  return { order_id: id, order_number: 'HN2026' + day.slice(0, 2) + day.slice(3, 5) + '-00' + id,
           source: 'local', openid: 'local', nickname: '', owner_name: who,
           status: status, total_price: price, created_at: '2026-' + day + ' 10:00:00',
           files_count: files.length, total_page_count: files.reduce((a, f) => a + f.total_pages, 0),
           total_sheet_count: files.reduce((a, f) => a + f.total_sheets, 0),
           received_client: cid, received_label: '', files: files };
}
/* 10 月订单（**全部排在硒鼓采购日 10-22 之后**：老口径「采购日之前的用量不算这批」，
   排在前面会让本月摊销变成 0，验收就测不到按机摊销了）：
   · 张三 sent 3 张（1020 打，6 页）
   · 李四 sent 2 张（M232 打，4 页）
   · 张三 failed 5 张（没产出 ⇒ 废纸预估 5）
   · 李四 canceled 3 张（人工值 8 覆盖掉）
   11 月订单：张三 sent 4 张、李四 failed 6 张（11 月预估 6，没人填人工值） */
const ORDERS_10 = [
  ord(1, '张三', 'sent', '10-23', 'c1020', [{ status: 'sent', total_sheets: 3, total_pages: 6 }], 1.8),
  ord(2, '李四', 'sent', '10-24', 'cm232', [{ status: 'sent', total_sheets: 2, total_pages: 4 }], 1.2),
  ord(3, '张三', 'failed', '10-25', 'cm232', [{ status: 'failed', total_sheets: 5, total_pages: 10 }], 3.0),
  ord(4, '李四', 'canceled', '10-26', 'c1020', [{ status: 'canceled', total_sheets: 3, total_pages: 6 }], 1.8),
];
const ORDERS_11 = [
  ord(5, '张三', 'sent', '11-03', 'c1020', [{ status: 'sent', total_sheets: 4, total_pages: 8 }], 2.4),
  ord(6, '李四', 'failed', '11-04', 'cm232', [{ status: 'failed', total_sheets: 6, total_pages: 12 }], 3.6),
];

/* ── 桩服务 ─────────────────────────────────────────────────────────────── */
const posts = [];       // 保存请求体（断言「有没有真的落库」）
const revenueHits = []; // 用量查询（断言 status 参数把失败终态单也拉回来了）
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
    /* 解锁：真页面里 save() 没有 finance_session 会把写操作挂起（_pendingSave），
       所以渲染验收必须先拿到会话，否则「改废纸 → 落库」根本不会发请求。 */
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
      const status = u.searchParams.get('status') || '';
      revenueHits.push({ start, status });
      const month = start.slice(0, 7);
      const orders = month === '2026-11' ? ORDERS_11 : ORDERS_10;
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
fs.mkdirSync(OUT_DIR, { recursive: true });

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1600, height: 1200 }, deviceScaleFactor: 1 });
const pageErrors = [];
const externalReq = [];
page.on('pageerror', (e) => pageErrors.push(e.message));
page.on('request', (r) => { if (!r.url().startsWith(BASE) && !r.url().startsWith('data:')) externalReq.push(r.url()); });

/* 打开页面 → 拿会话（否则 save() 被挂起，永远不发请求）→ 切到指定月份 → 等「用纸统计」渲染完 */
async function openDash(month) {
  const want = month || '2026-10';
  await page.goto(BASE + '/settlement.html', { waitUntil: 'domcontentloaded' });
  await page.waitForFunction(() => typeof window.financeUnlock === 'function', null, { timeout: 20000 });
  await page.evaluate(() => window.financeUnlock('expense', '123456'));
  await page.waitForFunction(() => !!window.FINANCE_SESSION, null, { timeout: 10000 });
  /* 页面启动时把 currentKey 取成「最新有记录的月」= 2026-11，这里显式切到要验的月 */
  await page.evaluate((k) => { window.switchTab('dash'); window.onSelMonth(k); }, want);
  await page.waitForFunction((k) => window.currentKey === k && document.querySelector('#paperStatsSection'), want, { timeout: 20000 });
  await page.waitForFunction(() => {
    const sec = document.querySelector('#paperStatsSection');
    return sec && sec.querySelectorAll('tbody tr').length >= 2 && !sec.querySelector('.loading-spin');
  }, null, { timeout: 20000 });
  /* 摊销区块要多等一拍（它要等「最早采购月 → 本月」全部月份的用量） */
  await page.waitForTimeout(600);
}

/* 切 tab 走 switchTab()，不经鼠标：页面在保存/加载期间会盖一层 #blockOverlay 挡点击 */
async function goTab(name) {
  await page.evaluate((n) => {
    const ov = document.querySelector('#blockOverlay');
    if (ov) ov.classList.remove('show');
    window.switchTab(n);
  }, name);
  await page.waitForTimeout(300);
}
async function goMonth(key) {
  await page.evaluate((k) => { window.switchTab('dash'); window.onSelMonth(k); }, key);
  await page.waitForFunction((k) => window.currentKey === k && document.querySelector('#paperStatsSection'), key, { timeout: 20000 });
  await page.waitForFunction(() => {
    const sec = document.querySelector('#paperStatsSection');
    return sec && sec.querySelectorAll('tbody tr').length >= 2 && !sec.querySelector('.loading-spin');
  }, null, { timeout: 20000 });
  await page.waitForTimeout(600);
}
/* 等保存请求真的发出（save() 有 250ms 防抖，再走 _saveToServer） */
async function waitPostsAtLeast(n, timeoutMs) {
  const t0 = Date.now();
  while (posts.length < n && Date.now() - t0 < (timeoutMs || 8000)) await page.waitForTimeout(100);
  return posts.length;
}

const dashSnap = () => page.evaluate(() => {
  const sec = document.querySelector('#paperStatsSection');
  const rows = [...(sec ? sec.querySelectorAll('tbody tr') : [])].map((tr) => {
    const tds = [...tr.children];
    const inp = tr.querySelector('input.inp-waste');
    return {
      who: (tds[0] ? tds[0].textContent.trim() : ''),
      normal: (tds[1] ? tds[1].textContent.trim() : ''),
      value: inp ? inp.value : null,
      placeholder: inp ? inp.getAttribute('placeholder') : null,
      total: (tds[3] ? tds[3].textContent.trim() : ''),
      src: (tds[4] ? tds[4].textContent.trim() : ''),
    };
  });
  const amort = document.querySelector('#consumableSection');
  const amThs = [...(amort ? amort.querySelectorAll('thead th') : [])].map((t) => t.textContent.trim());
  const amRows = [...(amort ? amort.querySelectorAll('tbody tr') : [])].map((tr) => [...tr.children].map((td) => td.textContent.trim()));
  return {
    title: sec ? sec.querySelector('h2').textContent.trim() : '',
    rows,
    foot: sec ? [...sec.querySelectorAll('tr.tfoot td')].map((td) => td.textContent.trim()) : [],
    memo: sec ? (sec.lastElementChild ? sec.lastElementChild.textContent : '') : '',
    amThs, amRows,
    toasts: [...document.querySelectorAll('#toastWrap .toast')].map((t) => t.textContent.trim()),
    currentKey: window.currentKey,
  };
});

const lastPost = () => {
  const b = posts[posts.length - 1];
  return b && b.data ? b.data : null;
};
const wasteOf = (month, mid) => {
  const d = lastPost();
  const wp = d && d.config && d.config.waste_paper;
  return wp && wp[month] && Object.prototype.hasOwnProperty.call(wp[month], mid) ? wp[month][mid] : null;
};
const setWaste = async (memberName, val) => {
  await page.evaluate(({ name, v }) => {
    const rows = [...document.querySelectorAll('#paperStatsSection tbody tr')];
    const tr = rows.find((r) => (r.children[0] || {}).textContent.trim() === name);
    const inp = tr.querySelector('input.inp-waste');
    inp.value = v;
    inp.dispatchEvent(new Event('change', { bubbles: true }));
  }, { name: memberName, v: val });
  /* save() → 250ms 防抖 → POST；等够再断言 */
  await page.waitForTimeout(1200);
};

/* ══════════════════════════════════════════════════════════════
   A. 基线：用纸统计 + 长期耗材摊销
   ══════════════════════════════════════════════════════════════ */
console.log('\n════ A. 基线渲染 ════');
await openDash('2026-10');
let snap = await dashSnap();
const rowOf = (s, who) => s.rows.find((r) => r.who === who) || {};
const zhang = rowOf(snap, '张三'), li = rowOf(snap, '李四');

chk('A1 页面出现「🧻 用纸统计」区块', snap.title.indexOf('用纸统计') >= 0, snap.title);
chk('A2 张三留空 ⇒ 废纸格是空的、placeholder = 预估 5',
    zhang.value === '' && zhang.placeholder === '5', JSON.stringify(zhang));
chk('A3 张三合计文案 = 「正常 3 + 废 5 张」（用户原话 xx+xx张）',
    zhang.total === '正常 3 + 废 5 张', zhang.total);
chk('A4 张三废纸来源标「预估」', zhang.src === '预估', zhang.src);
chk('A5 李四填了 8 ⇒ value=8、合计「正常 2 + 废 8 张」、来源「人工」',
    li.value === '8' && li.total === '正常 2 + 废 8 张' && li.src === '人工', JSON.stringify(li));
chk('A6 合计行：正常 5 + 废 13 张（3+2 正常 / 5+8 废纸）',
    (snap.foot[0] || '') === '合计' && (snap.foot[3] || '').replace(/\s+/g, ' ') === '正常 5 + 废 13 张',
    JSON.stringify(snap.foot));
chk('A7 说明文案写明废纸计入本月用纸量、A4纸摊销按它算',
    snap.memo.indexOf('计入本月用纸量') >= 0 && snap.memo.indexOf('A4 纸摊销') >= 0);
chk('A8 用量接口带上了 status 参数（否则拉不到「已取消/放弃打印」的失败单）',
    revenueHits.length > 0 && revenueHits.some((h) => /canceled/.test(h.status) && /failed/.test(h.status)),
    JSON.stringify(revenueHits.slice(0, 2)));

/* 长期耗材摊销（需求 A 的展示层） */
chk('A9 摊销表头新增「所属打印机」列', snap.amThs.indexOf('所属打印机') >= 0, JSON.stringify(snap.amThs));
const tonerRow = snap.amRows.find((r) => r[0] && r[0].indexOf('硒鼓') >= 0) || [];
chk('A10 硒鼓行的归属格写明「🖨 HP LaserJet MFP M232-M237 / 按该机本月 4 页摊」',
    (tonerRow[1] || '').indexOf('HP LaserJet MFP M232-M237') >= 0 && (tonerRow[1] || '').indexOf('按该机本月 4 页摊') >= 0,
    JSON.stringify(tonerRow.slice(0, 2)));
chk('A11 本月摊销按「这台机 4 页」算：¥128.98 × 4 / 3000 = ¥0.17',
    (tonerRow[8] || '').indexOf('0.17') >= 0, tonerRow[8]);
chk('A12 摊销行文案写明按哪台机摊',
    (await page.evaluate(() => document.body.innerText)).indexOf('硒鼓摊销（按 HP LaserJet MFP M232-M237 本月 4 页）') >= 0,
    '未在页面文案里找到按机摊销行');
const paperRow = snap.amRows.find((r) => r[0] && r[0].indexOf('A4纸') >= 0) || [];
chk('A13 A4纸行仍按全队用纸量摊（不被打印机归属污染）',
    (paperRow[1] || '').indexOf('A4纸按全队') >= 0, JSON.stringify(paperRow.slice(0, 2)));
chk('A14 没有 JS 报错', pageErrors.length === 0, JSON.stringify(pageErrors));
chk('A15 没有发外网请求', externalReq.length === 0, JSON.stringify(externalReq.slice(0, 3)));

await page.screenshot({ path: path.join(OUT_DIR, 'A_基线_用纸统计与摊销_' + STAMP + '.png'), fullPage: true });

/* ══════════════════════════════════════════════════════════════
   B. 编辑废纸：落库 / 清空 / 填 0 / 非法输入
   ══════════════════════════════════════════════════════════════ */
console.log('\n════ B. 废纸格可编辑并落库 ════');
const nPosts0 = posts.length;
await setWaste('张三', '12');
await waitPostsAtLeast(nPosts0 + 1);
chk('B1 改张三废纸 = 12 ⇒ 真的 POST 落库到 waste_paper[2026-10].ma',
    posts.length > nPosts0 && wasteOf('2026-10', 'ma') === 12, JSON.stringify(wasteOf('2026-10', 'ma')));
snap = await dashSnap();
let z2 = rowOf(snap, '张三');
chk('B2 重绘后合计 = 「正常 3 + 废 12 张」、来源变「人工」',
    z2.total === '正常 3 + 废 12 张' && z2.src === '人工', JSON.stringify(z2));
chk('B3 李四的人工值不受影响（同一次保存里仍带 8）', wasteOf('2026-10', 'mb') === 8);

await setWaste('张三', '');
await waitPostsAtLeast(nPosts0 + 2);
chk('B4 清空张三 ⇒ 这一格从 config 里删掉（不再是 12）',
    wasteOf('2026-10', 'ma') === null, JSON.stringify(wasteOf('2026-10', 'ma')));
snap = await dashSnap();
z2 = rowOf(snap, '张三');
chk('B5 清空后回到预估：placeholder=5、合计「正常 3 + 废 5 张」、来源「预估」',
    z2.value === '' && z2.placeholder === '5' && z2.total === '正常 3 + 废 5 张' && z2.src === '预估',
    JSON.stringify(z2));

await setWaste('张三', '0');
await waitPostsAtLeast(nPosts0 + 3);
chk('B6 填 0 ⇒ 落库成 0（与留空不同）', wasteOf('2026-10', 'ma') === 0, JSON.stringify(wasteOf('2026-10', 'ma')));
snap = await dashSnap();
z2 = rowOf(snap, '张三');
chk('B7 填 0 后合计「正常 3 + 废 0 张」、来源「人工」',
    z2.value === '0' && z2.total === '正常 3 + 废 0 张' && z2.src === '人工', JSON.stringify(z2));

const nPosts1 = posts.length;
/* 非法输入走**函数直调**：<input type="number"> 会把 'abc' 直接吞成空串（浏览器行为），
   那样测到的是「清空」而不是「校验」，落不到 updWastePaper 的校验分支上。 */
await page.evaluate(() => window.updWastePaper('ma', 'abc'));
await page.waitForTimeout(600);
snap = await dashSnap();
chk('B8 非法输入（文字）⇒ 弹错、不落库',
    snap.toasts.some((t) => t.indexOf('废纸张数要填非负整数') >= 0) && posts.length === nPosts1,
    JSON.stringify(snap.toasts) + ' posts=' + (posts.length - nPosts1));
chk('B9 非法输入后那一格恢复成有效值 0（不留在 abc）',
    rowOf(snap, '张三').value === '0', JSON.stringify(rowOf(snap, '张三')));
await page.evaluate(() => window.updWastePaper('ma', '-5'));
await page.waitForTimeout(600);
snap = await dashSnap();
chk('B10 负数同样不落库，值仍是 0',
    posts.length === nPosts1 && rowOf(snap, '张三').value === '0', JSON.stringify(rowOf(snap, '张三')));
chk('B11 编辑过程中始终没有 JS 报错', pageErrors.length === 0, JSON.stringify(pageErrors));

await page.screenshot({ path: path.join(OUT_DIR, 'B_废纸落库后_' + STAMP + '.png'), fullPage: true });

/* ══════════════════════════════════════════════════════════════
   C. 采购支出表：「所属打印机」列
   ══════════════════════════════════════════════════════════════ */
console.log('\n════ C. 采购支出「所属打印机」列 ════');
await goTab('entry');
await page.waitForFunction(() => document.querySelector('table.exp-table'), null, { timeout: 15000 });
await page.waitForTimeout(300);
const expSnap = await page.evaluate(() => {
  const tb = document.querySelector('table.exp-table');
  const ths = [...tb.querySelectorAll('thead th')].map((t) => t.textContent.trim());
  const rows = [...tb.querySelectorAll('tbody tr')].map((tr) => {
    const tds = [...tr.children];
    const sel = tr.querySelector('select[onchange*="updExpPrinter"]');
    return {
      item: (tds[1] ? (tds[1].querySelector('input') || {}).value : '') || (tds[1] ? tds[1].textContent.trim() : ''),
      kind: (tds[2] ? (tds[2].querySelector('select') || {}).value : ''),
      cellText: tds[4] ? tds[4].textContent.trim() : '',
      hasSelect: !!sel,
      selValue: sel ? sel.value : null,
      opts: sel ? [...sel.options].map((o) => o.value + '=' + o.textContent.trim()) : [],
    };
  });
  return { ths, rows, hasQtyClass: tb.className.indexOf('has-qty') >= 0 };
});
chk('C1 有耗材行 ⇒ 表头多出「所属打印机」，列数 8（日期/项目/类型/数量/所属打印机/金额/代付人/✕）',
    expSnap.ths.indexOf('所属打印机') >= 0 && expSnap.ths.length === 8, JSON.stringify(expSnap.ths));
chk('C2 表头顺序：数量之后紧跟所属打印机',
    expSnap.ths.indexOf('所属打印机') === expSnap.ths.indexOf('数量') + 1, JSON.stringify(expSnap.ths));
const tRow = expSnap.rows.find((r) => r.kind === 'toner') || {};
chk('C3 硒鼓行有下拉，且选中台账里的 M232',
    tRow.hasSelect && tRow.selValue === 'pm232', JSON.stringify(tRow));
chk('C4 下拉选项 = 未指定 + 台账两台机（1020 / M232）',
    tRow.opts && tRow.opts.length === 3 && tRow.opts[0].indexOf('未指定') >= 0
    && tRow.opts.some((o) => o.indexOf('HP LaserJet 1020') >= 0)
    && tRow.opts.some((o) => o.indexOf('HP LaserJet MFP M232-M237') >= 0), JSON.stringify(tRow.opts));
chk('C5 硒鼓格小字写明「🖨 按「M232」本月页数摊」',
    (tRow.cellText || '').indexOf('按「HP LaserJet MFP M232-M237」本月页数摊') >= 0, tRow.cellText);
const pRow = expSnap.rows.find((r) => r.kind === 'paper') || {};
chk('C6 A4纸行没有下拉，显示 — 并写明「A4纸按全队用纸量摊」',
    !pRow.hasSelect && (pRow.cellText || '').indexOf('A4纸按全队用纸量摊') >= 0, JSON.stringify(pRow));
chk('C7 普通采购行也没有下拉', !(expSnap.rows.find((r) => r.kind === 'other') || {}).hasSelect);
chk('C8 表格带 has-qty 类（数量/打印机格的小字绝对定位靠它留 padding）', expSnap.hasQtyClass);

/* 改归属：解锁 expense 后换一台机 ⇒ 落库 printer_id（需求 A 的可编辑入口） */
const nPosts2 = posts.length;
const changed = await page.evaluate(() => {
  window.unlocked.expense = true;
  window.renderEntry();
  const tb = document.querySelector('table.exp-table');
  if (!tb) return false;
  const tr = [...tb.querySelectorAll('tbody tr')].find((r) => r.querySelector('select[onchange*="updExpPrinter"]'));
  if (!tr) return false;
  const sel = tr.querySelector('select[onchange*="updExpPrinter"]');
  sel.value = 'p1020';
  sel.dispatchEvent(new Event('change', { bubbles: true }));
  return true;
});
await waitPostsAtLeast(nPosts2 + 1);
const d2 = lastPost();
const expRow = d2 && d2.records && d2.records.expenses.find((x) => x.id === 'e2');
chk('C9 把硒鼓改指 1020 ⇒ 落库 printer_id=p1020',
    changed && posts.length > nPosts2 && expRow && expRow.printer_id === 'p1020',
    JSON.stringify(expRow && expRow.printer_id));
await goTab('dash');
await page.waitForTimeout(800);
snap = await dashSnap();
const tonerRow2 = snap.amRows.find((r) => r[0] && r[0].indexOf('硒鼓') >= 0) || [];
chk('C10 改指 1020 后摊销改用 1020 的 6 页（不是 M232 的 4 页）',
    (tonerRow2[1] || '').indexOf('按该机本月 6 页摊') >= 0
    && (await page.evaluate(() => document.body.innerText)).indexOf('硒鼓摊销（按 HP LaserJet 1020 本月 6 页）') >= 0,
    JSON.stringify(tonerRow2.slice(0, 2)));
/* 改回，避免影响后面的月度断言 */
await page.evaluate(() => {
  window.unlocked.expense = true;
  window.STORE.records.expenses.find((x) => x.id === 'e2').printer_id = 'pm232';
  window.save();
  window.renderDash();
});
await page.waitForTimeout(600);

/* ══════════════════════════════════════════════════════════════
   D. 切换月份：读对应月的人工值 / 预估
   ══════════════════════════════════════════════════════════════ */
console.log('\n════ D. 切换月份 ════');
await goMonth('2026-11');
snap = await dashSnap();
const novZhang = rowOf(snap, '张三'), novLi = rowOf(snap, '李四');
chk('D1 11 月张三：正常 4 张、废纸 0（11 月没有失败单）',
    novZhang.normal === '4 张' && novZhang.total === '正常 4 + 废 0 张', JSON.stringify(novZhang));
chk('D2 11 月李四：留空（人工值只有 10 月那份）⇒ placeholder = 11 月自己的预估 6',
    novLi.value === '' && novLi.placeholder === '6', JSON.stringify(novLi));
chk('D3 11 月李四来源「预估」（绝不能沿用 10 月的 8 张人工值）',
    novLi.src === '预估' && novLi.total === '正常 0 + 废 6 张', JSON.stringify(novLi));

await goMonth('2026-10');
snap = await dashSnap();
chk('D4 切回 10 月：李四的人工值 8 还在（按月存，不串月）',
    rowOf(snap, '李四').value === '8' && rowOf(snap, '李四').total === '正常 2 + 废 8 张',
    JSON.stringify(rowOf(snap, '李四')));

await page.screenshot({ path: path.join(OUT_DIR, 'D_切换月份_' + STAMP + '.png'), fullPage: true });

chk('E1 全流程无 JS 报错', pageErrors.length === 0, JSON.stringify(pageErrors));
chk('E2 全流程无外网请求', externalReq.length === 0, JSON.stringify(externalReq.slice(0, 3)));

await browser.close();
server.close();

console.log('\n' + '='.repeat(60));
console.log('v7.10 钱相关需求（硒鼓归属 / 废纸）渲染验收: ' + (bad === 0 ? '全部通过 ✅' : bad + ' 条失败 ❌') + '  (' + total + ' 条断言)');
console.log('截图目录: ' + OUT_DIR);
console.log('='.repeat(60));
process.exit(bad === 0 ? 0 : 1);
