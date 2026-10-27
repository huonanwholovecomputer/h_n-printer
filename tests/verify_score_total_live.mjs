/* 验收 · v7.8「服务贡献评分」改子项得分后立即重算总分（2026-10-27 用户需求）
 *
 * 需求原话：「收支清算中，服务贡献评分中的项发生更新之后，总分没有及时动态更新重算，
 *            加个修改子项得分之后立即重算总分的逻辑」
 *
 * 缺陷 / 修法：
 *   旧 `updScore()` 只写 STORE 再 save()，为了「连续录入不失焦」刻意不整表重绘 ⇒
 *   成员标题上的「总分 N」徽标一直停在打开页面（或上次切页）时的旧值，
 *   必须切走再切回「记账」才更新。现在改分后由 `refreshScoreTotals()` 只就地改徽标文本
 *   （口径 = `memberScoreTotal`，与 calcMonth / renderEntry 同一份封顶求和），不重建 DOM。
 *
 * 为什么必须真浏览器：这条 bug 的本质是「渲染后没再读数据」，静态 grep 只能证明
 * "代码里写了调用"，证明不了"徽标当场变了、且输入框没被重建"。
 *
 * 做法：本地 HTTP 服务（静态文件来自 local_print_tool/finance/，/api/** 全部打桩，
 * 绝不发外网请求）→ Playwright 打开 settlement.html → 解锁「记账」→ 逐场景断言：
 *   ① 改分之前总分是 5 / 1（基线，由云端配置渲染出来）；
 *   ② **改一个子项得分后，同一帧内（dispatchEvent 返回前）该成员总分已变**（核心判据）；
 *   ③ 单项封顶照旧生效（填 999、上限 10 ⇒ 只加 10）；
 *   ④ 只重算被改的成员；改「项目名」不影响总分；
 *   ⑤ 徽标节点与其它行的输入框**都没被重建**（不整表重绘 ⇒ 连续录入不失焦）；
 *   ⑥ 切到「收支清算」：KPI「服务总分」= Σ封顶后得分，收入分配表逐人得分一致；
 *   ⑦ 切回「记账」：徽标仍是新值（重绘后口径一致，不是只改了 DOM 文本）；
 *   ⑧ 云端真的收到保存（900 与 4 都写进去了）；
 *   ⑨ 全程零 JS 异常。
 *   ⑩ **变异自证**：把 refreshScoreTotals() 那一行删掉再跑同一套断言 —— ② 必须转红
 *      （证明 ② 抓的就是这次修的东西，而不是随随便便哪个变化）。
 *
 * 产物：截图存 截图展示/服务评分总分即时重算/。
 * 用法：node tests/verify_score_total_live.mjs
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
const FINANCE = path.join(HERE, '..', 'local_print_tool', 'finance');
const HTML_PATH = path.join(FINANCE, 'settlement.html');
const OUT_DIR = path.join(HERE, '..', '截图展示', '服务评分总分即时重算');
fs.mkdirSync(OUT_DIR, { recursive: true });

const SRC = fs.readFileSync(HTML_PATH, 'utf8');
const CUR = new Date();
const CUR_MONTH = `${CUR.getFullYear()}-${String(CUR.getMonth() + 1).padStart(2, '0')}`;
const D1 = `${CUR_MONTH}-05`, D2 = `${CUR_MONTH}-06`;

const SCORE_CAP = 10;   // 刻意不是默认 15：顺带证明「上限」取自配置、不是写死
const STORE = {
  config: {
    investRate: 0.05, scoreCap: SCORE_CAP, passwords: {},
    members: [{ id: 'ma', name: '霍楠', color: '#4472C4' }, { id: 'mb', name: '姚懿祥', color: '#548235' }],
    orderPrinterMap: {}, printers: [],
  },
  records: {
    expenses: [], income: [], transfers: [],
    scores: [
      { id: 's1', date: D1, memberId: 'ma', item: '技术支持', score: 2 },
      { id: 's2', date: D2, memberId: 'ma', item: '值班', score: 3 },
      { id: 's3', date: D2, memberId: 'mb', item: '客服', score: 1 },
    ],
  },
  notes: {}, meta: { version: 2 },
};

const json = (res, obj) => { res.writeHead(200, { 'Content-Type': 'application/json; charset=utf-8' }); res.end(JSON.stringify(obj)); };

function makeHtml(mutate) {
  if (!mutate) return SRC;
  if (mutate === 'no-live-refresh') {
    /* 变异：删掉「改分后立即重算」那一行（= 修前行为），其余一切不变 */
    const out = SRC.replace(/\n[ \t]*if \(field === 'score'\) refreshScoreTotals\(\);/, '');
    if (out === SRC) throw new Error('变异失败：没找到 refreshScoreTotals() 的调用点（源码格式变了？）');
    return out;
  }
  throw new Error('未知变异: ' + mutate);
}

/* 每个场景一份独立的写入日志；服务端本身无状态 */
let writes = [];
let MUTATE = null;
const server = http.createServer((req, res) => {
  const u = new URL(req.url, 'http://127.0.0.1');
  const p = decodeURIComponent(u.pathname);
  if (!p.startsWith('/api/')) {
    if (p === '/' || p === '/settlement.html') {
      res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
      return res.end(makeHtml(MUTATE));
    }
    const file = path.join(FINANCE, p.replace(/^\//, ''));
    if (!fs.existsSync(file)) { res.writeHead(404); return res.end('not found'); }
    res.writeHead(200, { 'Content-Type': 'application/octet-stream' });
    return res.end(fs.readFileSync(file));
  }
  if (p === '/api/health') return json(res, { success: true, cloud: true });
  if (p === '/api/local/finance_auth/status') {
    return json(res, { success: true, modules: {}, needs_setup: [],
                       session: { active: false, modules: [], expires_in: 0 }, session_ttl: 1800,
                       read_only: false, integrity: 'ok' });
  }
  if (p === '/api/proxy/finance/config') {
    if (req.method === 'POST') {
      let body = ''; req.on('data', (c) => { body += c; });
      req.on('end', () => { writes.push(body); json(res, { success: true, server_version: 3 }); });
      return;
    }
    return json(res, { success: true, data: STORE });
  }
  if (p === '/api/proxy/pricing') return json(res, { success: true, data: { simplex_price: 0.3, duplex_price: 0.4, cover_page_price: 0.1, delivery_percentages: {}, urgency_prices: {} } });
  if (p === '/api/proxy/admin/statistics/months') return json(res, { success: true, data: { months: [{ month: CUR_MONTH }] } });
  if (p === '/api/proxy/admin/statistics/revenue') return json(res, { success: true, data: { period: {}, revenue_summary: {}, by_user: [], orders: [] } });
  if (p === '/api/proxy/printer/devices') return json(res, { success: true, devices: [] });
  if (p === '/api/local/device') return json(res, { success: true, device: { client_id: 'R7000-1', device_name: 'R7000-AHP10', take_orders: false } });
  if (p === '/api/local/data') return json(res, { success: true, data: null });
  if (p === '/api/local/bindings') return json(res, { success: true, data: {} });
  return json(res, { success: true, data: null });
});

const port = await new Promise((r) => server.listen(0, '127.0.0.1', () => r(server.address().port)));
const browser = await chromium.launch({ args: [`--explicitly-allowed-ports=${port}`] });

/* ── 场景跑法：每个场景一个全新 context（STORE / localStorage 都不串） ── */
async function runScenario(mutate, withScreenshots) {
  MUTATE = mutate;
  writes = [];
  const results = [];
  const chk = (name, cond, detail) => results.push([name, !!cond, detail === undefined ? '' : String(detail)]);

  const ctx = await browser.newContext({ viewport: { width: 1500, height: 1000 }, colorScheme: 'light', deviceScaleFactor: 1.2 });
  const page = await ctx.newPage();
  const errors = [];
  page.on('pageerror', (e) => errors.push(String(e)));
  page.on('console', (m) => { if (m.type() === 'error') errors.push('console: ' + m.text()); });

  await page.goto(`http://127.0.0.1:${port}/settlement.html`, { waitUntil: 'load' });
  await page.waitForTimeout(900);
  /* 解锁「服务评估」并进「记账」（直接翻状态位，避免走密码弹窗） */
  await page.evaluate(() => { FINANCE_SESSION = 'stub-sess'; unlocked.score = true; switchTab('entry'); });
  await page.waitForTimeout(400);

  const badge = (mid) => page.evaluate((id) => {
    const el = document.getElementById('scoreTotal-' + id);
    return el ? el.textContent.trim() : null;
  }, mid);

  /* 把「服务贡献评分」滚到视口内（截图用；也保证断言读的是真正渲染出来的东西） */
  const scrollToScores = () => page.evaluate(() => {
    const h2 = [...document.querySelectorAll('#tab-entry h2')].find((h) => h.textContent.indexOf('服务贡献评分') >= 0);
    if (!h2) return false;
    h2.scrollIntoView({ block: 'start' });
    window.scrollBy(0, -80);   // 让开顶部固定栏
    return true;
  });

  /* 改某个子项的分（真实 onchange 路径：写 DOM value 再派发 change） */
  const setScore = (sid, val) => page.evaluate(([sid, val]) => {
    const inp = [...document.querySelectorAll('input[onchange]')]
      .find((i) => (i.getAttribute('onchange') || '').indexOf("updScore('" + sid + "','score'") === 0);
    if (!inp) return { ok: false };
    inp.value = String(val);
    inp.dispatchEvent(new Event('change', { bubbles: true }));
    /* 同一个同步帧里读徽标：中间没有任何 await / 重绘机会 */
    const ma = document.getElementById('scoreTotal-ma');
    const mb = document.getElementById('scoreTotal-mb');
    return { ok: true, ma: ma ? ma.textContent.trim() : null, mb: mb ? mb.textContent.trim() : null };
  }, [sid, val]);

  const setItemName = (sid, val) => page.evaluate(([sid, val]) => {
    const inp = [...document.querySelectorAll('input[onchange]')]
      .find((i) => (i.getAttribute('onchange') || '').indexOf("updScore('" + sid + "','item'") === 0);
    if (!inp) return { ok: false };
    inp.value = String(val);
    inp.dispatchEvent(new Event('change', { bubbles: true }));
    const ma = document.getElementById('scoreTotal-ma');
    return { ok: true, ma: ma ? ma.textContent.trim() : null };
  }, [sid, val]);

  /* ① 基线 */
  const b1 = await badge('ma'), b2 = await badge('mb');
  chk('① 基线：霍楠 总分 5（2+3）、姚懿祥 总分 1 —— 徽标由云端配置渲染出来',
    b1 === '总分 5' && b2 === '总分 1', `${b1} / ${b2}`);
  await scrollToScores();
  if (withScreenshots) {
    await page.screenshot({ path: path.join(OUT_DIR, '01-改分前-总分.png'), fullPage: false });
  }

  /* 给「另一个成员的行」和一个「同成员的另一行」打标记：整表重绘会把这些节点换掉 */
  await page.evaluate(() => {
    const pick = (sid, field) => [...document.querySelectorAll('input[onchange]')]
      .find((i) => (i.getAttribute('onchange') || '').indexOf("updScore('" + sid + "','" + field + "'") === 0);
    const s2 = pick('s2', 'score'); if (s2) s2.setAttribute('data-node-mark', 's2');
    const s3 = pick('s3', 'score'); if (s3) s3.setAttribute('data-node-mark', 's3');
    const badgeEl = document.getElementById('scoreTotal-ma');
    if (badgeEl) badgeEl.setAttribute('data-node-mark', 'badge-ma');
    const nameS2 = pick('s2', 'item'); if (nameS2) nameS2.setAttribute('data-node-mark', 's2-name');
  });

  /* ② ★核心：改 s1 的得分 9 ⇒ 同一帧内 霍楠 总分变 12 */
  const r2 = await setScore('s1', 9);
  chk('② ★改子项得分后「总分」当场重算（同一帧内 5 → 12，无需切页/整表重绘）',
    r2.ok && r2.ma === '总分 12', r2.ma);
  chk('②b 只重算被改的成员（姚懿祥仍 1）', r2.mb === '总分 1', r2.mb);
  if (withScreenshots) {
    await page.screenshot({ path: path.join(OUT_DIR, '02-改分后-总分立即重算.png'), fullPage: false });
  }

  /* 造一个「正在编辑、还没提交」的另一行内容：接下来的改分若整表重绘，它就会被冲掉 */
  await page.evaluate(() => {
    const s2Name = [...document.querySelectorAll('input[onchange]')]
      .find((i) => (i.getAttribute('onchange') || '').indexOf("updScore('s2','item'") === 0);
    if (s2Name) s2Name.value = '值班（草稿）';
  });

  /* ③ 单项封顶照旧：填 999、上限 10 ⇒ 总分只加 10 */
  const r3 = await setScore('s1', 999);
  chk('③ 单项封顶照旧生效（填 999、上限 10 ⇒ 霍楠 10+3=13，不是 1002）',
    r3.ok && r3.ma === '总分 13', r3.ma);

  /* ④ 改项目名不影响总分 */
  const r4 = await setItemName('s1', '技术支持（改名）');
  chk('④ 改「项目名」不改变总分（仍 13）', r4.ok && r4.ma === '总分 13', r4.ma);

  /* ⑤ 不整表重绘：徽标与其它行输入框都是原来那几个节点 */
  const marks = await page.evaluate(() => {
    const marked = [...document.querySelectorAll('[data-node-mark]')].map((el) => el.getAttribute('data-node-mark'));
    const s2 = [...document.querySelectorAll('input[onchange]')]
      .find((i) => (i.getAttribute('onchange') || '').indexOf("updScore('s2','item'") === 0);
    return { marked: marked, s2NameValue: s2 ? s2.value : null };
  });
  chk('⑤ 改分是就地更新：徽标与其它行的输入框节点都没被重建（连续录入不会失焦）',
    marks.marked.indexOf('badge-ma') >= 0 && marks.marked.indexOf('s2') >= 0
    && marks.marked.indexOf('s3') >= 0 && marks.marked.indexOf('s2-name') >= 0,
    marks.marked.join(','));
  chk('⑤b 另一行正在编辑的内容没被重绘冲掉（s2 项目名仍是「值班（草稿）」）',
    marks.s2NameValue === '值班（草稿）', marks.s2NameValue);

  /* ⑥ 另一个成员：改 s3 的得分 4 ⇒ 姚懿祥 4，霍楠 不受影响 */
  const r6 = await setScore('s3', 4);
  chk('⑥ 改另一成员的子项：姚懿祥 1 → 4、霍楠 仍 13',
    r6.ok && r6.ma === '总分 13' && r6.mb === '总分 4', `${r6.ma} / ${r6.mb}`);

  /* ⑦ 收支清算：KPI 服务总分 = 13 + 4 = 17；收入分配表逐人得分一致 */
  await page.click('#tabNav button[data-tab="dash"]');
  await page.waitForTimeout(1000);
  const dash = await page.evaluate(() => {
    const kpi = document.querySelector('#tab-dash .kpi-card.c-orange .num');
    const tbl = [...document.querySelectorAll('#tab-dash table')].find((t) => t.textContent.indexOf('服务得分') >= 0);
    const rows = tbl ? [...tbl.querySelectorAll('tbody tr')].map((tr) => [...tr.querySelectorAll('td')].map((td) => td.innerText.trim())) : [];
    return {
      kpi: kpi ? kpi.textContent.trim() : null,
      rows: rows,
      total: tbl ? [...tbl.querySelectorAll('tr.tfoot td')].map((td) => td.innerText.trim()) : [],
    };
  });
  const rowOf = (name) => dash.rows.find((r) => r[0] === name) || [];
  chk('⑦ 收支清算 KPI「服务总分」= 17（改了分，结算侧同口径）', dash.kpi === '17', dash.kpi);
  chk('⑦b 收入分配 · 服务得分列：霍楠 13 / 姚懿祥 4',
    rowOf('霍楠')[2] === '13' && rowOf('姚懿祥')[2] === '4',
    JSON.stringify(dash.rows.map((r) => r.slice(0, 3))));
  chk('⑦c 收入分配合计行服务总分 = 17', dash.total[2] === '17', JSON.stringify(dash.total));
  if (withScreenshots) {
    await page.evaluate(() => window.scrollTo(0, 0));
    await page.waitForTimeout(200);
    await page.screenshot({ path: path.join(OUT_DIR, '03-收支清算-服务总分.png'), fullPage: false });
  }

  /* ⑧ 切回记账：徽标仍是新值（不是只改了 DOM 文本） */
  await page.click('#tabNav button[data-tab="entry"]');
  await page.waitForTimeout(500);
  const b8a = await badge('ma'), b8b = await badge('mb');
  chk('⑧ 切回「记账」重绘后徽标仍是 13 / 4（STORE 已真改，不是只刷了 DOM）',
    b8a === '总分 13' && b8b === '总分 4', `${b8a} / ${b8b}`);

  /* ⑨ 云端真的收到保存（save() 有 250ms 防抖） */
  await page.waitForTimeout(700);
  const lastWrite = writes.length ? writes[writes.length - 1] : '';
  chk('⑨ 改分已写回云端配置（s1=999、s3=4 都在最后一次保存里）',
    lastWrite.indexOf('"score":999') >= 0 && lastWrite.indexOf('"score":4') >= 0,
    lastWrite.length + ' bytes');

  chk('⑩ 全程零 JS 异常', errors.length === 0, errors.slice(0, 3).join(' | '));

  await ctx.close();
  return results;
}

/* ══════════════════════════════════════════════════════════════
   静态接线（源码文本）—— 只在正常场景前做一次
   ══════════════════════════════════════════════════════════════ */
const staticResults = [];
{
  const chk = (name, cond, detail) => staticResults.push([name, !!cond, detail === undefined ? '' : String(detail)]);
  chk('静态① renderEntry 的「总分」徽标带可定位 id（scoreTotal-<成员id>）',
    SRC.indexOf('id="scoreTotal-\' + memId + \'"') >= 0,
    SRC.indexOf('id="scoreTotal-\' + memId + \'"'));
  const updScoreBody = (() => {
    const i = SRC.indexOf('function updScore(');
    const e = SRC.indexOf('\n}', i);
    return i < 0 ? '' : SRC.slice(i, e + 2);
  })();
  chk('静态② updScore 在改得分后调用 refreshScoreTotals()（改项目名不调用）',
    /if \(field === 'score'\) refreshScoreTotals\(\);/.test(updScoreBody)
    && (updScoreBody.match(/refreshScoreTotals\(\)/g) || []).length === 1,
    updScoreBody.replace(/\s+/g, ' ').slice(0, 160));
  /* 封顶求和只能有一份实现（结算 / 徽标 / 即时重算共用 memberScoreTotal） */
  const minCount = (SRC.match(/Math\.min\(x\.score \|\| 0, /g) || []).length;
  chk('静态③ 封顶求和只有一份实现（Math.min(x.score…) 全文件 1 处，在 memberScoreTotal 里）',
    minCount === 1 && /function memberScoreTotal\(items, cap\) \{[\s\S]{0,300}?Math\.min\(x\.score \|\| 0, cap\)/.test(SRC),
    minCount + ' 处');
  const helperUses = (SRC.match(/memberScoreTotal\(/g) || []).length;
  chk('静态④ memberScoreTotal 被结算 / 徽标 / 即时重算三处复用（含定义共 ≥4 次）',
    helperUses >= 4, helperUses + ' 次');
}

/* ══════════════════════════════════════════════════════════════
   跑两遍：正常源码 + 变异（删掉即时重算）自证
   ══════════════════════════════════════════════════════════════ */
const normal = await runScenario(null, true);
const mutated = await runScenario('no-live-refresh', false);

await browser.close();
server.close();

function report(title, rows) {
  console.log('\n=== ' + title + ' ===');
  let fail = 0;
  for (const [n, c, d] of rows) {
    if (!c) fail++;
    console.log(`[${c ? 'PASS' : 'FAIL'}] ${n}` + (!c && d ? '  -- ' + d : ''));
  }
  return fail;
}

let bad = report('静态接线', staticResults);
bad += report('正常源码（真浏览器）', normal);

/* 变异自证：核心判据必须转红，且其它判据不该跟着一起塌（说明它抓的就是这一点） */
console.log('\n=== 变异自证（删掉「改分后立即重算」那一行 = 修前行为） ===');
const coreName = '② ★改子项得分后「总分」当场重算（同一帧内 5 → 12，无需切页/整表重绘）';
const coreMut = mutated.find((r) => r[0] === coreName);
const baseMut = mutated.find((r) => r[0].indexOf('① 基线') === 0);
const dashMut = mutated.find((r) => r[0].indexOf('⑦ 收支清算 KPI') === 0);
const okMut = !!coreMut && coreMut[1] === false && !!baseMut && baseMut[1] === true && !!dashMut && dashMut[1] === true;
if (!okMut) bad++;
console.log(`[${okMut ? 'PASS' : 'FAIL'}] 删掉 refreshScoreTotals() 后核心判据 ② 转红（实际 ${coreMut ? (coreMut[1] ? '仍绿' : '转红') : '缺失'}），`
  + `基线 ① 与结算侧 ⑦ 不受影响（${baseMut && baseMut[1] ? '绿' : '红'} / ${dashMut && dashMut[1] ? '绿' : '红'}）`);

console.log('\n' + '='.repeat(60));
console.log(`已跑断言 ${staticResults.length + normal.length + 1} 项，失败 ${bad} 项`);
process.exit(bad ? 1 : 0);
