/* 验收 · 收支清算「实际转账」渲染级冒烟测试（Node + VM 内真实执行 settlement.html 的渲染函数）
 *
 * 与 verify_actual_transfer.py 的分工：
 *   - .py  —— 静态接线 + calcMonth 算法级（金额口径、守恒、脏数据）
 *   - 本文件 —— 真实加载整页脚本、注入截图数据后调用 renderDash()/renderHistory()，
 *              断言产出的 HTML（数字对不对、锁定态是否真的禁用了输入、旧口径是否绝迹）。
 *              DOM 用 Proxy 桩替代（无需 jsdom / 浏览器）。
 *
 * 用法：node tests/verify_actual_transfer_render.js [settlement.html 路径]
 * exit 0 = 全部通过。
 */
const fs = require('fs'), vm = require('vm');
const html = fs.readFileSync(process.argv[2], 'utf8');
const scripts = [...html.matchAll(/<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/g)].map(m => m[1]);
const js = scripts.join('\n;\n');

function symbolQ(k) { return typeof k === 'symbol'; }
function stub() {
  const store = {};
  return new Proxy(function () {}, {
    get(t, k) {
      if (k === 'innerHTML' || k === 'textContent' || k === 'value') return store[k] || '';
      if (k === 'classList') return { add() {}, remove() {}, contains() { return false; }, toggle() {} };
      if (k === 'style') return {};
      if (k === 'dataset') return {};
      if (k === 'children') return [];
      if (k === 'files') return [];
      if (k === Symbol.toPrimitive) return () => '';
      if (k === 'toString' || k === 'valueOf') return () => '';
      if (symbolQ(k)) return undefined;
      return stub();
    },
    set(t, k, v) { store[k] = v; return true; },
    apply() { return stub(); },
  });
}
function proxied(fn) {
  return new Proxy({}, {
    get(t, k) {
      if (k === Symbol.toPrimitive) return () => '';
      if (k === 'toString' || k === 'valueOf') return () => '';
      if (symbolQ(k) || k === 'then' || k === 'toJSON') return undefined;
      if (fn[k]) return fn[k];
      return stub();
    },
    set() { return true; },
  });
}
const els = {};
const documentProto = {
  querySelector: (s) => (els[s] = els[s] || stub()),
  querySelectorAll: () => [],
  getElementById: (id) => (els['#' + id] = els['#' + id] || stub()),
  createElement: () => stub(),
  createTextNode: () => stub(),
  addEventListener: () => {},
  removeEventListener: () => {},
  readyState: 'complete',
  documentElement: stub(),
  head: stub(),
  body: stub(),
};
const document = proxied(documentProto);
const storage = { getItem: () => null, setItem() {}, removeItem() {} };
const windowProto = {
  matchMedia: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }),
  addEventListener: () => {},
  removeEventListener: () => {},
  location: { search: '', href: 'http://localhost/', origin: 'http://localhost' },
  innerWidth: 1400, innerHeight: 900, devicePixelRatio: 1,
  localStorage: storage, sessionStorage: storage, document: document,
};
const sandbox = {
  console, Math, JSON, Object, Array, String, Number, Boolean, Date, RegExp, Error, Promise,
  Set, Map, WeakMap, isNaN, isFinite, parseFloat, parseInt, encodeURIComponent, decodeURIComponent,
  setTimeout, clearTimeout, setInterval, clearInterval,
  requestAnimationFrame: (f) => { try { f(0); } catch (e) {} return 0; },
  cancelAnimationFrame: () => {},
  document: document,
  window: proxied(windowProto),
  localStorage: storage,
  sessionStorage: storage,
  location: windowProto.location,
  navigator: { userAgent: 'node', clipboard: {} },
  fetch: () => Promise.resolve({ json: () => Promise.resolve({ success: false }), ok: true }),
  getComputedStyle: () => ({ colorScheme: 'light', getPropertyValue: () => '' }),
  FINANCE_MODE: 'cloud',
};
sandbox.globalThis = sandbox;
sandbox.self = sandbox;
vm.createContext(sandbox);
try {
  vm.runInContext(js, sandbox, { filename: 'settlement.js' });
} catch (e) {
  console.log('LOAD_FAIL: ' + e.message);
  process.exit(2);
}
console.log('LOAD_OK');

vm.runInContext(`
  STORE = {
    config: { investRate: 0.05, scoreCap: 15, passwords: {}, members: [
      { id: 'ma', name: '成员A', color: '#4472C4' },
      { id: 'mb', name: '成员B', color: '#548235' },
      { id: 'mc', name: '成员C', color: '#ED7D31' } ] },
    records: {
      expenses: [
        { id:'e1', date:'2026-06-03', item:'打印机', amount:1016.10, payer:'ma' },
        { id:'e2', date:'2026-06-05', item:'喷头', amount:109.67, payer:'ma' },
        { id:'e3', date:'2026-06-07', item:'订书机', amount:19.80, payer:'mb' },
        { id:'e4', date:'2026-06-20', item:'A4纸第3批', amount:25.70, payer:'mb' },
        { id:'e5', date:'2026-06-12', item:'A4纸第1批', amount:89.00, payer:'mc' },
        { id:'e6', date:'2026-06-15', item:'A4纸第2批', amount:57.60, payer:'mc' } ],
      income: [
        { id:'i1', date:'2026-06-08', memberId:'ma', amount:101.80 },
        { id:'i2', date:'2026-06-09', memberId:'mb', amount:394.00 },
        { id:'i3', date:'2026-06-11', memberId:'mc', amount:22.00 } ],
      scores: [
        { id:'s1', date:'2026-06-10', memberId:'ma', item:'打印', score:15 },
        { id:'s2', date:'2026-06-10', memberId:'ma', item:'推广', score:15 },
        { id:'s3', date:'2026-06-10', memberId:'ma', item:'值班', score:4 },
        { id:'s4', date:'2026-06-10', memberId:'mb', item:'打印', score:15 },
        { id:'s5', date:'2026-06-10', memberId:'mb', item:'推广', score:15 },
        { id:'s6', date:'2026-06-10', memberId:'mb', item:'值班', score:12 },
        { id:'s7', date:'2026-06-10', memberId:'mc', item:'打印', score:15 },
        { id:'s8', date:'2026-06-10', memberId:'mc', item:'推广', score:9 } ],
      transfers: [ { id:'t1', month:'2026-06', from:'mb', to:'ma', amount:300.00, note:'先转一半' } ]
    },
    notes: {}, meta: { version: 0 }
  };
  currentKey = '2026-06';
  unlocked.transfer = true;
  renderDash();
`, sandbox);

let bad = 0;
function chk(name, cond, detail) {
  if (!cond) bad++;
  console.log(`[${cond ? 'PASS' : 'FAIL'}] ${name}` + (!cond && detail ? '  -- ' + detail : ''));
}
const out = els['#tab-dash'].innerHTML || '';
console.log('RENDER_OK, dash length =', out.length);

// 场景 1：只录了「成员B → 成员A 300」（转不够）
console.log('--- 首页（成员B只转 300）---');
chk('实际转账区块', out.includes('💸 实际转账'));
chk('已解锁徽标', out.includes('🔓 已解锁'));
chk('建议 vs 实际：还差 ¥272.56', out.includes('还差 ¥272.56'));
chk('已转金额 ¥300.00', out.includes('已转 <b>¥300.00</b>'));
chk('一键填入按钮', out.includes('按建议一键填入'));
chk('转账记录金额回填', out.includes('value="300"'));
chk('转账备注回填', out.includes('value="先转一半"'));
chk('转账表已无「日期」列', !out.includes('<th>日期</th><th>付款人</th>'));
chk('转账板块标出归属月份 2026年6月', out.includes('2026年6月 归属 · 真正转了多少钱'));
chk('说明「下月付款仍算本月」', out.includes('钱即使下个月才转，也还算这个月的结算'));
chk('无日期输入框（type="date"）', !/<input type="date"[^>]*onchange="updTransfer/.test(out));
chk('合计单元格带 id（可就地刷新）', out.includes('id="dashTransferTotal"'));
chk('实际盈亏区块存在', out.includes('💰 实际盈亏'));
chk('成员A 实际盈亏 ¥-723.97', out.includes('¥-723.97'));
chk('成员A 仍应收 ¥460.56', out.includes('还应收 ¥460.56'));
chk('成员B 实际盈亏 +¥48.50', out.includes('+¥48.50'));
chk('成员B 已超付 ¥272.56', out.includes('已超付 ¥272.56'));
chk('成员C 实际盈亏 ¥-124.60', out.includes('¥-124.60'));
chk('应得盈亏小字 ¥-263.41（成员A账面）', out.includes('>¥-263.41<'));
chk('零值不再显示 −¥0.00', !out.includes('−¥0.00') && !out.includes('+¥0.00'));
chk('团队合计实际盈亏 = 应得盈亏 ¥-800.07', out.includes('¥-800.07'));

// 场景 2：历史记录（含这笔转账）
console.log('--- 历史记录（含转账）---');
vm.runInContext('renderHistory();', sandbox);
let hist = els['#tab-history'].innerHTML || '';
chk('历史表头「成员A 实际盈亏」', hist.includes('成员A 实际盈亏'));
chk('历史 成员A 实际 ¥-723.97', hist.includes('¥-723.97'));
chk('历史 成员B 实际 +¥48.50', hist.includes('+¥48.50'));
chk('历史 小字应得 ¥-263.41', hist.includes('应 ¥-263.41'));
chk('历史不再出现旧口径 +¥862.36', !hist.includes('+¥862.36'));
chk('历史口径说明', hist.includes('团队总盈亏不变'));
chk('历史仍保留 采购总额/人均/总收入', hist.includes('¥1317.87') && hist.includes('¥439.29') && hist.includes('¥517.80'));

// 场景 3：历史记录 —— 一分钱都没转（用户反馈的原始场景）
console.log('--- 历史记录（完全没转账）---');
vm.runInContext(`
  STORE.records.transfers = [];
  renderHistory();
`, sandbox);
hist = els['#tab-history'].innerHTML || '';
chk('成员A 实际 亏 ¥-1023.97（而不是 +¥862.36）', hist.includes('¥-1023.97'), '未找到 ¥-1023.97');
chk('成员B 实际 赚 +¥348.50', hist.includes('+¥348.50'));
chk('成员C 实际 亏 ¥-124.60', hist.includes('¥-124.60'));
chk('小字应得仍为 ¥-263.41', hist.includes('应 ¥-263.41'));

// 场景 4：锁定态 —— 未解锁时输入框必须 disabled
console.log('--- 未解锁（独立密码）---');
vm.runInContext(`
  unlocked.transfer = false;
  STORE.records.transfers = [{ id:'t1', month:'2026-06', from:'mb', to:'ma', amount:300, note:'' }];
  renderDash();
`, sandbox);
const locked = els['#tab-dash'].innerHTML || '';
chk('锁定态显示「已锁定，修改需解锁」', locked.includes('已锁定，修改需解锁'));
chk('锁定态无「已解锁」徽标', !locked.includes('🔓 已解锁'));
chk('锁定态「一键填入」按钮 disabled（锁定后不可点）',
    locked.includes('fillSuggestedTransfers()" disabled'));
const BS = String.fromCharCode(39);  // 单引号（渲染出的 HTML 里是普通引号；这样写避免源码转义）
chk('锁定态转账输入框 disabled', locked.includes('updTransfer(' + BS + 't1' + BS + ',' + BS + 'amount' + BS + ',+this.value)" disabled'));
chk('锁定态删除按钮 disabled', locked.includes('delTransfer(' + BS + 't1' + BS + ')" disabled'));

process.exit(bad ? 1 : 0);
