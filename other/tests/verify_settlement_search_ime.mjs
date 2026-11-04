/* 验收 · 结算页「订单明细」搜索框：中文输入法（IME）能不能打进去（2026-11-04 现场）
 *
 * 现场问题（用户原话）：『云端清算中，订单的搜索功能貌似不能使用中文输入法，输入法输不进去，
 * 英文可以，复制粘贴也没问题。』
 * 根因：v7.15 的「输入即筛」在每次 input 时把 #cloud-orders 整段 outerHTML 换掉 ——
 * 搜索框**自己**也在被换的区间里。英文逐字提交，换掉也无所谓；中文输入法在 composition
 * 期间元素消失，拼音/候选就一起没了，所以只有中文打不进去、粘贴（一次性 input）正常。
 *
 * 这份脚本钉的就是「组字期间不许重绘」：用真实 DOM 派发 compositionstart → input(拼音) →
 * compositionend(中文) + input，断言
 *   ① 组字期间 #ordersSearchInput 还是**同一个节点**、仍是焦点、值没被吞；
 *   ② 组字期间列表**没有**按拼音筛（说明确实没重绘）；
 *   ③ 上屏后按中文筛出正确的一行、焦点回到搜索框、搜索词落进 _ordersSearch；
 *   ④ 英文逐字、粘贴中文、✕ 清空三条老路没被弄坏。
 *
 * 做法：本地 HTTP 服务（静态文件来自 local_print_tool/finance/，/api/** 全部打桩，不发外网请求）。
 * 用法：node other/tests/verify_settlement_search_ime.mjs [settlement.html 路径]
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

let bad = 0;
function chk(name, cond, detail) {
  if (!cond) bad++;
  console.log(`[${cond ? 'PASS' : 'FAIL'}] ${name}` + (!cond && detail !== undefined ? `  -- ${detail}` : ''));
}

/* ── 夹具：三条订单，靠**订单备注**区分（搜索只认文件名 + 备注）────────────── */
function ord(i, remark, fileName) {
  return {
    order_id: 910000 + i,
    order_number: 'HN20261104-' + String(2000 + i),
    source: 'local', openid: 'local', nickname: '', owner_name: '欧阳娜娜',
    status: 'sent', total_price: 2.4,
    created_at: '2026-11-04 09:0' + i + ':00',
    print_started_at: '2026-11-04 09:0' + i + ':30',
    wait_seconds: 12, is_admin_print: 0, files_count: 1, total_page_count: 4,
    received_client: 'DESKTOP-EJGEB1V-a6c1a1365e', received_label: '欧阳娜娜的设备 | DESKTOP-EJGEB1V-a6c1a1365e',
    remark: remark,
    files: [{ id: 'f' + i, file_name: fileName, copies: 1, page_count: 4, duplex: 'off',
              page_range: '', total_price: 2.4, status: 'sent', is_free: false }],
  };
}
const ORDERS = [
  ord(1, '发票抬头需要开票', 'fapiao.pdf'),
  ord(2, '作业本第 3 页', 'zuoye.pdf'),
  ord(3, '', 'plain.pdf'),
];

const STORE = {
  config: { investRate: 0.05, scoreCap: 15, passwords: {},
            members: [{ id: 'ma', name: '欧阳娜娜', color: '#4472C4' }] },
  records: { expenses: [], income: [], scores: [], transfers: [] },
  notes: {}, meta: { version: 1 },
};
const json = (res, obj) => { res.writeHead(200, { 'Content-Type': 'application/json; charset=utf-8' }); res.end(JSON.stringify(obj)); };
const server = http.createServer((req, res) => {
  const u = new URL(req.url, 'http://127.0.0.1');
  const p = decodeURIComponent(u.pathname);
  if (p.startsWith('/api/')) {
    if (p === '/api/health') return json(res, { success: true, cloud: true });
    if (p === '/api/proxy/finance/config') {
      if (req.method === 'POST') return json(res, { success: true, server_version: 2 });
      return json(res, { success: true, data: STORE });
    }
    if (p === '/api/proxy/pricing') return json(res, { success: true, data: { simplex_price: 0.3, duplex_price: 0.4, cover_page_price: 0.1, delivery_percentages: {}, urgency_prices: {} } });
    if (p === '/api/proxy/admin/statistics/months') return json(res, { success: true, data: { months: [{ month: '2026-11' }] } });
    if (p === '/api/proxy/admin/statistics/revenue') return json(res, { success: true, data: { period: {}, revenue_summary: {}, by_user: [], orders: ORDERS } });
    if (p === '/api/local/data') return json(res, { success: true, data: null });
    if (p === '/api/local/bindings') return json(res, { success: true, data: {} });
    return json(res, { success: true, data: null });
  }
  const file = p === '/' ? 'settlement.html' : p.replace(/^\//, '');
  const fp = file === 'settlement.html' ? HTML_PATH : path.join(FINANCE, file);
  if (!fs.existsSync(fp) || fs.statSync(fp).isDirectory()) { res.writeHead(404); return res.end('not found'); }
  const ext = path.extname(fp);
  res.writeHead(200, { 'Content-Type': ext === '.html' ? 'text/html; charset=utf-8' : 'application/javascript; charset=utf-8' });
  res.end(fs.readFileSync(fp));
});

const port = await new Promise((r) => server.listen(0, '127.0.0.1', () => r(server.address().port)));
const BASE = `http://127.0.0.1:${port}`;

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1440, height: 1000 }, deviceScaleFactor: 1 });
const pageErrors = [];
page.on('pageerror', (e) => pageErrors.push(e.message));

try {
  await page.goto(BASE + '/settlement.html', { waitUntil: 'domcontentloaded' });
  chk('页面脚本加载无语法错误', pageErrors.length === 0, pageErrors.join(' | '));
  await page.click('#tabNav button[data-tab="cloud"]');
  await page.waitForFunction(() => window._cloudData && window._cloudData.orders && document.querySelector('.order-table'), null, { timeout: 20000 });

  const rows = () => page.evaluate(() => document.querySelectorAll('tr.order-row').length);
  chk('夹具三条订单已渲染', (await rows()) === 3, String(await rows()));

  /* ── ① 组字期间不许重绘：输入框必须是同一个节点 ─────────────────────── */
  const comp = await page.evaluate(() => {
    const el = document.getElementById('ordersSearchInput');
    el.focus();
    const before = el;
    el.dispatchEvent(new CompositionEvent('compositionstart', { bubbles: true, data: '' }));
    el.value = 'fapiao';
    el.dispatchEvent(new InputEvent('input', { bubbles: true, data: 'fapiao', inputType: 'insertCompositionText' }));
    const after = document.getElementById('ordersSearchInput');
    return {
      sameNode: before === after,
      focused: document.activeElement === after,
      valueKept: after.value,
      rendered: window._ordersSearchRendered,
      renderedText: document.querySelectorAll('tr.order-row').length,
      composing: window._ordersSearchComposing,
    };
  });
  chk('组字中 _ordersSearchComposing = true', comp.composing === true, JSON.stringify(comp));
  chk('组字中搜索框还是同一个 DOM 节点（没被重绘换掉）', comp.sameNode === true, JSON.stringify(comp));
  chk('组字中焦点仍在搜索框', comp.focused === true, JSON.stringify(comp));
  chk('组字中拼音没被吞（值仍是 fapiao）', comp.valueKept === 'fapiao', comp.valueKept);
  chk('组字中列表没按拼音筛（说明确实没重绘）', comp.renderedText === 3, String(comp.renderedText));
  chk('组字中词还没画进 DOM（_ordersSearchRendered 不含 fapiao）', comp.rendered !== 'fapiao', String(comp.rendered));

  /* ── ② 上屏（compositionend + input）：按中文筛出正确的一行 ───────────── */
  await page.evaluate(() => {
    const el = document.getElementById('ordersSearchInput');
    el.value = '发票';
    el.dispatchEvent(new CompositionEvent('compositionend', { bubbles: true, data: '发票' }));
    el.dispatchEvent(new InputEvent('input', { bubbles: true, data: '发票', inputType: 'insertText' }));
  });
  await page.waitForTimeout(150);
  const after = await page.evaluate(() => {
    const el = document.getElementById('ordersSearchInput');
    return {
      rows: document.querySelectorAll('tr.order-row').length,
      value: el ? el.value : null,
      focused: document.activeElement === el,
      word: window._ordersSearch,
      rendered: window._ordersSearchRendered,
      composing: window._ordersSearchComposing,
    };
  });
  chk('上屏后按「发票」筛出 1 行', after.rows === 1, JSON.stringify(after));
  chk('上屏后搜索词落进 _ordersSearch', after.word === '发票', String(after.word));
  chk('上屏后词已画进 DOM（_ordersSearchRendered = 发票）', after.rendered === '发票', String(after.rendered));
  chk('上屏后搜索框里仍是「发票」', after.value === '发票', String(after.value));
  chk('上屏后焦点回到搜索框（可以接着打）', after.focused === true, JSON.stringify(after));
  chk('上屏后组字标记已复位', after.composing === false, String(after.composing));

  /* ── ③ 英文逐字路径没被弄坏 ──────────────────────────────────────────── */
  await page.evaluate(() => {
    const el = document.getElementById('ordersSearchInput');
    el.focus();
    el.value = 'zuoye';
    el.dispatchEvent(new InputEvent('input', { bubbles: true, data: 'zuoye' }));
  });
  await page.waitForTimeout(120);
  chk('英文「zuoye」筛出 1 行（文件名命中）', (await rows()) === 1, String(await rows()));

  /* ── ④ 粘贴中文路径（一次性 input，无组字事件）没被弄坏 ───────────────── */
  await page.evaluate(() => {
    const el = document.getElementById('ordersSearchInput');
    el.focus();
    el.value = '作业本';
    el.dispatchEvent(new InputEvent('input', { bubbles: true, data: '作业本', inputType: 'insertFromPaste' }));
  });
  await page.waitForTimeout(120);
  chk('粘贴「作业本」筛出 1 行（备注命中）', (await rows()) === 1, String(await rows()));

  /* ── ⑤ ✕ 清空：回到全部订单 ──────────────────────────────────────────── */
  await page.evaluate(() => { const b = document.querySelector('.orders-search .clr'); if (b) b.click(); });
  await page.waitForTimeout(150);
  const cleared = await page.evaluate(() => ({
    rows: document.querySelectorAll('tr.order-row').length,
    value: document.getElementById('ordersSearchInput').value,
    word: window._ordersSearch,
  }));
  chk('点 ✕ 清空后回到全部 3 条', cleared.rows === 3, JSON.stringify(cleared));
  chk('点 ✕ 清空后搜索框与 _ordersSearch 都为空', cleared.value === '' && cleared.word === '', JSON.stringify(cleared));

  /* ── ⑥ 变异自证：去掉守卫就该红（证明上面那条 sameNode 断言真能失败）── */
  const mutated = await page.evaluate(() => {
    const el = document.getElementById('ordersSearchInput');
    el.focus();
    window._ordersSearchComposing = false;          // 模拟「没有组字守卫」
    el.value = 'x';
    el.dispatchEvent(new InputEvent('input', { bubbles: true, data: 'x' }));
    return document.getElementById('ordersSearchInput') !== el;
  });
  chk('变异自证：去掉组字守卫后输入框确实会被重绘换掉（判据不是空转）',
      mutated === true, '若这里是 false，说明 sameNode 断言在空转，必须换判据');
  chk('页面全程无脚本异常', pageErrors.length === 0, pageErrors.join(' | '));
} finally {
  await browser.close();
  server.close();
}

console.log(bad === 0 ? '\n全部通过 ✅' : `\n${bad} 项失败 ❌`);
process.exit(bad === 0 ? 0 : 1);
