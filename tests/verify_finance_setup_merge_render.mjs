/* 验收 · 收支清算「首次设置按钮」与「本地/云端合并」两处 UI 逻辑（2026-10-23 用户反馈）
 *
 * 用户原话：「这个首次设置什么批量设置的这个功能有问题……一直不消失」
 *          「这个云端订单合并功能，打开就会弹，但是又没解锁，这个逻辑也有问题」
 *
 * 现场证据（本机 %APPDATA%\HN打印工具\logs\finance_audit.log，每次打开结算页记两条）：
 *   {"event":"write_denied","path":"/api/proxy/finance/config","code":"finance_session_required"}
 *   {"event":"write_denied","path":"/api/local/data",         "code":"finance_session_required"}
 * 两个根因：
 *   ① 「首次设置/批量重置未设置模块」按钮是**无条件渲染**的：7 个模块全配好了它照样常驻，
 *      点下去只回一句「所有模块都已设置密码」⇒ 看起来"永远关不掉"。
 *   ② 本地/云端合并分支**绕过会话门禁**直接写：页面刚打开、什么都没解锁就弹「是否合并」，
 *      点「合并」两个写请求都被 403（合并其实一点没做），下次打开照旧再弹。
 *      而且 hasRealData() 把 config.passwords（服务端每次写本机文件都会回填的凭据镜像）
 *      当成"本地有业务数据"⇒ 就算没有一条收支记录也会一直判成"两边都有数据"。
 *
 * 做法：本地 HTTP 桩（静态文件取真实 settlement.html，/api/** 全打桩，绝不外网），
 *       Playwright 真浏览器逐场景断言：
 *   A 现场复现：本机只剩凭据镜像 + 云端有业务数据
 *     · 打开**不弹**合并框；**一条写请求都不发**（不再制造 write_denied 噪音）；
 *     · 设置页（解锁后）**没有**「首次设置」按钮。
 *   B 真有离线数据（未解锁）
 *     · 仍不弹框、不发写请求，只给一条「解锁后可合并」的非阻断提示；
 *     · 解锁「设置」后**才**弹合并框 → 点「合并」两个写请求都带 X-Finance-Session；
 *     · 本机重复副本被清成空业务数据；重新打开**不再问**。
 *   C 还有模块没设密码（needs_setup 非空）
 *     · 首载弹设置框 → 点「稍后设置」→ 关闭后不再重弹（旧行为 400ms 后自己弹回来）；
 *     · 设置页出现「为 N 个未设置密码的模块批量设置」入口并列出模块名。
 *   D 反向自证（--old）：把 settlement.html 换回修改前版本（git show HEAD:…），
 *     场景 A 必须**变红**（弹出合并框 + 两次 403 写入），证明上面的判据有判别力。
 *
 * 用法：node tests/verify_finance_setup_merge_render.mjs [--old]
 * exit 0 = 全部通过；截图落在 截图展示/财务设置与合并/。
 */
import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';

const require_ = createRequire(import.meta.url);
const PLAYWRIGHT = 'C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright';
const { chromium } = require_(PLAYWRIGHT);

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.join(HERE, '..');
const FINANCE = path.join(ROOT, 'local_print_tool', 'finance');
const OUT_DIR = path.join(ROOT, '截图展示', '财务设置与合并');
const OLD_MODE = process.argv.includes('--old');

/* 反向自证：从 git HEAD 取修改前的那份（本文件所在 HEAD 尚未包含本轮修复） */
let HTML_PATH = path.join(FINANCE, 'settlement.html');
if (OLD_MODE) {
  const tmp = path.join(os.tmpdir(), 'settlement_old_' + Date.now() + '.html');
  const raw = execFileSync('git', ['-C', ROOT, 'show', 'HEAD:local_print_tool/finance/settlement.html'],
    { encoding: 'utf8', maxBuffer: 64 * 1024 * 1024 });
  if (!/_saveToServer\(STORE, true\)/.test(raw)) {
    console.error('反向验证前置失败：HEAD 里的 settlement.html 已经是修后版本，无法自证');
    process.exit(2);
  }
  fs.writeFileSync(tmp, raw, 'utf8');
  HTML_PATH = tmp;
  console.log('反向自证模式：使用 HEAD 的 settlement.html（修改前）');
}

let bad = 0;
let total = 0;
function chk(name, cond, detail) {
  total++;
  if (!cond) bad++;
  console.log(`[${cond ? 'PASS' : 'FAIL'}] ${name}` + (!cond && detail !== undefined ? `  -- ${JSON.stringify(detail)}` : ''));
}

/* ── 桩数据 ──────────────────────────────────────────────────────────────── */
const LEGACY_DEFAULT = 'sha256:8d969eef6ecad3c29a3a629280e686cf0c3f5d5a86aff3ca12020c923adc6c92';
const CLOUD_STORE = {
  config: {
    investRate: 0.05, scoreCap: 15,
    members: [{ id: 'm1', name: '霍楠', color: '#4472C4' },
              { id: 'm2', name: '姚懿祥', color: '#ED7D31' },
              { id: 'm3', name: '闫承翰', color: '#FFC000' }],
    passwords: { score: LEGACY_DEFAULT, expense: LEGACY_DEFAULT, income: LEGACY_DEFAULT, settings: LEGACY_DEFAULT },
  },
  records: {
    expenses: [{ id: 'e1', date: '2026-10-03', item: 'A4 纸', amount: 128.0, payer: 'm1' }],
    income: [{ id: 'i1', date: '2026-10-05', memberId: 'm1', amount: 330.0, note: '' }],
    scores: [], transfers: [],
  },
  notes: {}, meta: { version: 7 },
};
/* A：现场状态 —— 本机只剩凭据镜像（服务端 _preserve_credentials 每次写回填），业务数据全空 */
const LOCAL_CREDS_ONLY = {
  config: { scoreCap: 15, investRate: 0.05, members: [],
            passwords: { score: LEGACY_DEFAULT, expense: LEGACY_DEFAULT,
                         income: LEGACY_DEFAULT, settings: LEGACY_DEFAULT },
            memberBindings: [] },
  records: { expenses: [], income: [], scores: [], transfers: [] },
  notes: {}, meta: { version: 3 },
};
/* B：真有离线业务数据 */
const LOCAL_REAL = {
  config: { members: [{ id: 'ml', name: '离店期间的本地成员', color: '#70AD47' }], passwords: {} },
  records: { expenses: [{ id: 'le1', date: '2026-09-28', item: '离线期间的墨盒', amount: 66.5, payer: 'ml' }],
             income: [], scores: [], transfers: [] },
  notes: {}, meta: { version: 2 },
};

const TEST_PWD = 'FinPass2026';
const state = {
  local: LOCAL_CREDS_ONLY,
  needsSetup: [],
  cloud: CLOUD_STORE,
  posts: [],        // {url, hasSession, body}
  sessionSeq: 0,
};
const json = (res, obj, status = 200) => {
  res.writeHead(status, { 'Content-Type': 'application/json; charset=utf-8' });
  res.end(JSON.stringify(obj));
};
const readBody = (req) => new Promise((r) => { let b = ''; req.on('data', (c) => { b += c; }); req.on('end', () => r(b)); });
const signed = (req) => !!req.headers['x-finance-session'];

const server = http.createServer(async (req, res) => {
  const u = new URL(req.url, 'http://127.0.0.1');
  const p = decodeURIComponent(u.pathname);
  if (p.startsWith('/api/')) {
    if (p === '/api/health') return json(res, { success: true, cloud: true });
    if (p === '/api/local/finance_auth/status') {
      return json(res, { success: true, modules: {}, needs_setup: state.needsSetup.slice(),
                         session: { active: false, modules: [], expires_in: 0 },
                         session_ttl: 1800, read_only: false, integrity: 'ok' });
    }
    if (p === '/api/local/finance_auth/unlock') {
      const body = JSON.parse((await readBody(req)) || '{}');
      if (String(body.password) === '123456') {
        return json(res, { success: false, code: 'default_password', message: '默认密码已停用' }, 403);
      }
      if (String(body.password) !== TEST_PWD) {
        return json(res, { success: false, code: 'wrong_password', message: '密码错误' }, 403);
      }
      state.sessionSeq++;
      return json(res, { success: true, session: 'stub-sess-' + state.sessionSeq,
                         expires_in: 1800, modules: [String(body.module || '')], upgraded: false });
    }
    if (p === '/api/local/finance_auth/setup') {
      const body = JSON.parse((await readBody(req)) || '{}');
      const mods = state.needsSetup.slice();
      state.needsSetup = [];
      const stored = {};
      mods.forEach((m, i) => { stored[m] = 'pbkdf2_sha256$210000$' + String(i + 1).repeat(32) + '$' + 'd'.repeat(64); });
      return json(res, { success: true, modules: mods, stored });
    }
    if (p === '/api/local/finance_auth/logout') return json(res, { success: true, revoked: true });
    /* 写接口：无会话一律 403，和 stats_server 的真实门禁同口径 */
    if (req.method !== 'GET' && (p === '/api/proxy/finance/config' || p === '/api/local/data')) {
      const body = await readBody(req);
      state.posts.push({ url: p, hasSession: signed(req), body });
      if (!signed(req)) {
        return json(res, { success: false, code: 'finance_session_required',
                           message: '需要先解锁财务模块（finance_session）才能写入' }, 403);
      }
      return json(res, { success: true, message: '已保存', server_version: 8 });
    }
    if (p === '/api/proxy/finance/config') return json(res, { success: true, data: state.cloud });
    if (p === '/api/local/data') return json(res, { success: true, data: state.local, exists: !!state.local,
                                                    integrity: 'ok', read_only: false });
    if (p === '/api/proxy/pricing') return json(res, { success: true, data: { simplex_price: 0.3, duplex_price: 0.4, cover_page_price: 0.1, delivery_percentages: {}, urgency_prices: {} } });
    if (p === '/api/proxy/admin/statistics/months') return json(res, { success: true, data: { months: [{ month: '2026-10' }] } });
    if (p === '/api/proxy/admin/statistics/revenue') return json(res, { success: true, data: { period: {}, revenue_summary: {}, by_user: [], orders: [] } });
    if (p === '/api/local/bindings') return json(res, { success: true, bindings: {} });
    if (p === '/api/local/orders') return json(res, { success: true, data: { orders: [] } });
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
const BASE = `http://127.0.0.1:${port}`;
fs.mkdirSync(OUT_DIR, { recursive: true });

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1600, height: 1000 }, deviceScaleFactor: 1 });
const pageErrors = [];
page.on('pageerror', (e) => pageErrors.push(e.message));

const modalOpen = () => page.evaluate(() => document.querySelectorAll('#modalRoot .modal-backdrop').length);
const modalText = () => page.evaluate(() => [...document.querySelectorAll('#modalRoot .modal-card')]
  .map((m) => m.textContent.replace(/\s+/g, ' ').trim()));
const toasts = () => page.evaluate(() => [...document.querySelectorAll('#toastWrap .toast')].map((t) => t.textContent));
const writes = () => state.posts.filter((x) => x.url === '/api/proxy/finance/config' || x.url === '/api/local/data');
const shot = async (name) => { await page.screenshot({ path: path.join(OUT_DIR, name), fullPage: false }); };

async function unlockSettings() {
  await page.click('#tabNav button[data-tab="settings"]');
  await page.waitForTimeout(300);
  const locked = await page.evaluate(() => /设置已锁定/.test(document.querySelector('#tab-settings').textContent));
  if (locked) {
    await page.click('#tab-settings .btn.pri');
    await page.waitForSelector('#modalRoot .modal-card input[type=password]', { timeout: 5000 });
    await page.fill('#modalRoot .modal-card input[type=password]', TEST_PWD);
    await page.click('#modalRoot .modal-card [data-act=ok]');
    await page.waitForTimeout(600);
  }
  return locked;
}

let caught = null;
try {
  /* ── A 现场复现：本机只剩凭据镜像 + 云端有业务数据 ───────────────────── */
  state.local = LOCAL_CREDS_ONLY;
  state.needsSetup = [];
  state.posts = [];
  await page.goto(BASE + '/settlement.html', { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(1600);
  let m = await modalOpen();
  let ts = await toasts();
  if (OLD_MODE) {
    /* 反向自证：旧版在这里一定弹「是否合并」，点下去两个写请求都 403 */
    chk('[反向] 旧版打开页面就弹「本地与云端均存在数据，是否合并」', m >= 1, { modalOpen: m });
    await page.click('#modalRoot .modal-card [data-act=ok]');
    await page.waitForTimeout(600);
    const w = writes();
    chk('[反向] 旧版点「合并」→ 两个写请求都因没有会话被 403',
      w.length >= 2 && w.every((x) => !x.hasSession), w.map((x) => [x.url, x.hasSession]));
    chk('[反向] 旧版弹出红字「需要先解锁财务模块（finance_session）才能写入」',
      (await toasts()).some((t) => t.includes('finance_session')), await toasts());
    console.log(`\n反向自证：旧版复现了用户报告的两个症状（共 ${total} 条），判据有判别力 ✅`);
    throw { __oldDone: true };
  }

  chk('A 打开结算页**不再弹**合并确认框（旧版：打开就弹）', m === 0, { modalOpen: m, toasts: ts });
  chk('A 打开结算页**一条写请求都不发**（旧版：两条 write_denied）', writes().length === 0,
    writes().map((x) => x.url));
  chk('A 屏幕上没有「需要先解锁财务模块」的红字', !ts.some((t) => t.includes('finance_session')), ts);
  chk('A 页面脚本无异常', pageErrors.length === 0, pageErrors.slice(0, 3));

  const wasLocked = await unlockSettings();
  chk('A 设置模块本来锁着，用密码解锁成功（走真实解锁弹窗）', wasLocked);
  const slotHTML = await page.evaluate(() => (document.getElementById('financeSetupSlot') || {}).innerHTML || '');
  chk('A 7 个模块都已设密码 → 设置页**不再显示**「首次设置/批量重置未设置模块」按钮',
    !/批量设置|首次设置/.test(slotHTML) && !/批量设置/.test(await page.evaluate(() => document.body.innerText)),
    slotHTML);
  await shot('A_全模块已设置_按钮不显示.png');

  /* ── B 真有离线数据：未解锁不弹，解锁后才问，合并完清副本 ───────────────── */
  state.local = LOCAL_REAL;
  state.cloud = CLOUD_STORE;
  state.posts = [];
  await page.goto(BASE + '/settlement.html', { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(1600);
  m = await modalOpen();
  ts = await toasts();
  chk('B 本机确有离线数据、但还没解锁 → 仍**不弹**合并框', m === 0, { modalOpen: m, toasts: ts });
  chk('B 未解锁时不发写请求（不再产生必然 403 的噪音）', writes().length === 0, writes().map((x) => x.url));
  chk('B 只给一条非阻断提示（解锁后可合并）',
    ts.some((t) => t.includes('离线收支数据')), ts);
  await shot('B1_未解锁_不弹合并框.png');

  await unlockSettings();
  await page.waitForTimeout(400);
  m = await modalOpen();
  const mtxt = (await modalText()).join(' | ');
  chk('B 解锁「设置」后**才**弹出合并确认框', m >= 1 && /安全合并/.test(mtxt), { modalOpen: m, modal: mtxt });
  await shot('B2_解锁后_合并确认框.png');

  await page.click('#modalRoot .modal-card [data-act=ok]');
  await page.waitForTimeout(900);
  const wB = writes();
  chk('B 点「合并」→ 云端配置与本机文件两个写请求都带 X-Finance-Session（不再 403）',
    wB.length >= 2 && wB.every((x) => x.hasSession), wB.map((x) => [x.url, x.hasSession]));
  const localW = wB.filter((x) => x.url === '/api/local/data').pop();
  let blankOK = false;
  try {
    const payload = JSON.parse(localW.body);
    const data = payload.data || payload;
    blankOK = (data.records.expenses || []).length === 0 && (data.records.income || []).length === 0;
  } catch (e) { /* 解析失败按失败算 */ }
  chk('B 合并成功后把本机重复副本清成空业务数据（不再每次开页都问）', blankOK, localW && localW.body.slice(0, 160));
  chk('B 合并成功提示出现', (await toasts()).some((t) => t.includes('已安全合并')), await toasts());
  await page.waitForTimeout(400);
  await shot('B3_合并完成.png');

  /* 重新打开：本机已无业务数据 → 不再问 */
  state.local = { config: { members: [], passwords: CLOUD_STORE.config.passwords },
                  records: { expenses: [], income: [], scores: [], transfers: [] }, notes: {}, meta: { version: 0 } };
  state.posts = [];
  await page.goto(BASE + '/settlement.html', { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(1600);
  chk('B 合并过一次之后，再打开**不再弹**合并框', (await modalOpen()) === 0,
    { modalOpen: await modalOpen(), toasts: await toasts() });

  /* ── C 还有模块没设密码：入口按需出现；设置框可关且不再重弹 ─────────────── */
  state.needsSetup = ['transfer'];
  state.local = LOCAL_CREDS_ONLY;
  await page.goto(BASE + '/settlement.html', { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(1200);
  chk('C needs_setup 非空 → 首载仍会把人送进「设置财务密码」框', (await modalOpen()) >= 1,
    await modalText());
  await shot('C1_首次设置弹框.png');
  await page.click('#modalRoot .modal-card [data-act=cancel]');   // 「稍后设置」
  await page.waitForTimeout(1200);
  chk('C 点「稍后设置」关掉后**不再自己弹回来**（旧版 400ms 后重弹 ⇒ 用户说"一直不消失"）',
    (await modalOpen()) === 0, { modalOpen: await modalOpen(), toasts: await toasts() });
  await page.click('#tabNav button[data-tab="settings"]');
  await page.waitForTimeout(300);
  const lockedC = await page.evaluate(() => /设置已锁定/.test(document.querySelector('#tab-settings').textContent));
  if (lockedC) {
    await page.click('#tab-settings .btn.pri');
    await page.waitForSelector('#modalRoot .modal-card input[type=password]', { timeout: 5000 });
    await page.fill('#modalRoot .modal-card input[type=password]', TEST_PWD);
    await page.click('#modalRoot .modal-card [data-act=ok]');
    await page.waitForTimeout(600);
  }
  const slotC = await page.evaluate(() => (document.getElementById('financeSetupSlot') || {}).innerHTML || '');
  chk('C 还有 1 个模块没设密码 → 设置页出现批量设置入口，并写明数量与模块名',
    /为 1 个未设置密码的模块批量设置/.test(slotC) && /实际转账/.test(slotC), slotC);
  await shot('C2_设置有未设置模块.png');
  await page.click('#financeSetupSlot button');
  await page.waitForTimeout(400);
  chk('C 点该入口能打开设置框（不是死按钮）', /设置财务密码/.test((await modalText()).join(' ')),
    await modalText());
  await page.fill('#modalRoot .modal-card input#fpNew1', 'NewPass2026');
  await page.fill('#modalRoot .modal-card input#fpNew2', 'NewPass2026');
  await page.click('#modalRoot .modal-card [data-act=ok]');
  await page.waitForTimeout(900);
  const slotAfter = await page.evaluate(() => (document.getElementById('financeSetupSlot') || {}).innerHTML || '');
  chk('C 设置成功后入口随之消失（不再常驻）', !/批量设置/.test(slotAfter), slotAfter);
  await shot('C3_设置完成后入口消失.png');
} catch (e) {
  if (!(e && e.__oldDone)) caught = e;
} finally {
  await browser.close();
  server.close();
}

if (caught) {
  console.log('\n未捕获异常：' + (caught.stack || caught.message || caught));
  bad++;
}
console.log(`\n${OLD_MODE ? '反向自证' : '验收'}：${total - bad}/${total} 通过` + (bad ? `，失败 ${bad} ❌` : ' ✅'));
process.exit(bad ? 1 : 0);
