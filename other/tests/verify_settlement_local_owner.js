/* 验收 · 结算页 v7.19「本地来源订单调整所属人」（Node + VM 内真实执行 settlement.html 内联 JS）
 *
 * 背景（2026-11-09 用户反馈）：订单明细的「用户」列只有云端单能改，来源=本地 的订单整列
 * 只读，而现场大量订单是本地自打的（owner_name 是管理员/成员名）。本脚本钉住四件事：
 *   #1 可编辑判据：解锁后本地单可改；外部监视单 v7.20 起也可改（写本地账本
 *      manual_owner）；未解锁 / 非十进制云端 id 仍不可改；
 *   #2 本地单候选所属人 = 当前所属人（置顶）→ 成员名单 → 数据里出现过的其他归属人；
 *   #3 提交：本地单 body 只带 owner_name（绝不带 openid，否则后端会把本地单判成云端单），
 *      成功后本地把 owner_name 落到订单对象上、留痕字段跟着写；
 *   #4 云端单行为不回归：body 仍是 openid，成功后改 openid/nickname。
 *
 * 用法：node other/tests/verify_settlement_local_owner.js [settlement.html 路径]
 * exit 0 = 全部通过。
 */
const fs = require('fs'), vm = require('vm'), path = require('path');
const HTML_PATH = process.argv[2] || path.join(__dirname, '..', '..', 'local_print_tool', 'finance', 'settlement.html');
const html = fs.readFileSync(HTML_PATH, 'utf8');
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
const LS = {};
const storage = {
  getItem: (k) => (Object.prototype.hasOwnProperty.call(LS, k) ? LS[k] : null),
  setItem: (k, v) => { LS[k] = String(v); },
  removeItem: (k) => { delete LS[k]; },
};
const windowProto = {
  matchMedia: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }),
  addEventListener: () => {},
  removeEventListener: () => {},
  location: { search: '', hash: '', href: 'http://localhost/', origin: 'http://localhost' },
  innerWidth: 1400, innerHeight: 900, devicePixelRatio: 1,
  localStorage: storage, sessionStorage: storage, document: document,
};
const REQS = [];
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
  fetch: (url, opt) => {
    REQS.push({ url: String(url), body: (opt && opt.body) || '' });
    return Promise.resolve({ json: () => Promise.resolve(sandbox.__RESP || { success: true }) });
  },
  getComputedStyle: () => ({ colorScheme: 'light', getPropertyValue: () => '' }),
  FINANCE_MODE: 'cloud',
};
sandbox.globalThis = sandbox;
sandbox.self = sandbox;
/* Chart.js 桩：renderCloud 会画图，Node 里没有 Chart 全局（v7.20 起本脚本会触发整页重绘） */
sandbox.Chart = function Chart() {
  return { data: { datasets: [], labels: [] }, options: {}, update() {}, destroy() {},
           resize() {}, draw() {} };
};
vm.createContext(sandbox);
try {
  vm.runInContext(js, sandbox, { filename: 'settlement.js' });
} catch (e) {
  console.log('LOAD_FAIL: ' + e.message);
  process.exit(2);
}
console.log('LOAD_OK');

let bad = 0;
function chk(name, cond, detail) {
  if (!cond) bad++;
  console.log('[' + (cond ? 'PASS' : 'FAIL') + '] ' + name + (!cond && detail !== undefined ? '  -- ' + detail : ''));
}
const run = (code) => vm.runInContext(code, sandbox);

/* ==================== 注入数据 ==================== */
const ORDERS = [
  { order_id: 1, order_number: 'HN20261109-0001', source: 'local', openid: 'local', owner_name: '甲成员',
    status: 'sent', total_price: 1.00, files_count: 1, created_at: '2026-11-09 10:00:00', files: [] },
  { order_id: 2, order_number: 'HN20261109-0002', source: 'local', openid: 'local', owner_name: '外部人',
    status: 'sent', total_price: 1.00, files_count: 1, created_at: '2026-11-09 09:00:00', files: [] },
  { order_id: 3, order_number: 'HN20261109-0003', source: 'wechat', openid: 'wx-abc', nickname: '小王',
    status: 'sent', total_price: 1.00, files_count: 1, created_at: '2026-11-09 08:00:00', files: [] },
  { order_id: 4, order_number: 'HN20261109-0004', source: 'external', openid: 'local', owner_name: '甲成员',
    status: 'sent', total_price: 1.00, files_count: 1, created_at: '2026-11-09 07:00:00', files: [] },
];
sandbox.__ORDERS = ORDERS;
run(`
  STORE = { config: { members: [{ id: 'ma', name: '甲成员', color: '#4472C4' },
                               { id: 'mb', name: '乙成员', color: '#ED7D31' }],
                      memberBindings: [], passwords: {} },
            records: { expenses: [], income: [], scores: [], transfers: [] }, notes: {}, meta: {} };
  _cloudData = { orders: __ORDERS,
                 assignable_users: [{ openid: 'wx-abc', nickname: '小王' },
                                    { openid: 'wx-def', nickname: '小李' }] };
  _cloudStatusFilter = 'all'; _scopeMembers = null; _scopeSources = null;
  _ordersPage = 1; _ordersPerPage = 20; _cloudExpanded = {}; _ordersSearch = '';
  unlocked.orderstatus = false;
`);

/* ==================== #1 可编辑判据 ==================== */
console.log('--- #1 可编辑判据：本地单解锁后可改，外部单/未解锁/非法 id 不可改 ---');
chk('未解锁：本地单不可改', run("_orderUserEditable('1')") === false);
run('unlocked.orderstatus = true;');
chk('解锁后：本地单可改（v7.19 放开）', run("_orderUserEditable('1')") === true);
chk('解锁后：云端单仍可改', run("_orderUserEditable('3')") === true);
chk('外部监视单也可改（v7.20：写本地账本 manual_owner）', run("_externalOrderIds['4'] = true; _orderUserEditable('4')") === true);
run("delete _externalOrderIds['4'];");
chk('非十进制、又不是外部单的 id 不可改', run("_orderUserEditable('1.0')") === false);
chk('外部单的行 key（extrow-…）也可改（前端就是这么传的）',
    run("_orderUserEditable('extrow-ext-1-2026-10-29-090000')") === true);

/* ==================== #2 候选所属人 ==================== */
console.log('--- #2 本地单候选：当前 → 成员名单 → 历史归属人 ---');
const names1 = run("_orderUserCandidates(_findCloudOrder('1')).map(function (u) { return u.name + (u.cur ? '*' : ''); })");
chk('订单 1：当前「甲成员」置顶 → 乙成员 → 外部人',
  JSON.stringify(names1) === JSON.stringify(['甲成员*', '乙成员', '外部人']), JSON.stringify(names1));
const names2 = run("_orderUserCandidates(_findCloudOrder('2')).map(function (u) { return u.name + (u.cur ? '*' : ''); })");
chk('订单 2：当前「外部人」置顶，成员名单补齐',
  JSON.stringify(names2) === JSON.stringify(['外部人*', '甲成员', '乙成员']), JSON.stringify(names2));
const cloud = run("_orderUserCandidates(_findCloudOrder('3')).map(function (u) { return u.openid; })");
chk('云端单候选仍是 openid（当前 + assignable_users）',
  JSON.stringify(cloud) === JSON.stringify(['wx-abc', 'wx-def']), JSON.stringify(cloud));

/* ==================== #3 提交：本地单带 owner_name ==================== */
console.log('--- #3 提交：本地单 body 只带 owner_name，成功后落地 owner_name ---');
sandbox.__RESP = { success: true, owner_name: '乙成员', previous_owner_name: '甲成员',
                   manual_user_by: '甲成员', manual_user_at: '2026-11-09 23:00:00' };
run("applyOrderUser('1', 1)");
setTimeout(function () {
  const req = REQS.filter(function (r) { return r.url.indexOf('/order_user') >= 0; }).pop() || {};
  let body = {};
  try { body = JSON.parse(req.body || '{}'); } catch (e) {}
  chk('本地单走 /api/proxy/admin/order_user',
    req.url === '/api/proxy/admin/order_user', req.url);
  chk('body.owner_name = 乙成员', body.owner_name === '乙成员', req.body);
  chk('body 不带 openid（否则会被后端判成云端单）', !('openid' in body), req.body);
  chk('订单对象 owner_name 已落地', ORDERS[0].owner_name === '乙成员', ORDERS[0].owner_name);
  chk('留痕 manual_user_by / manual_user_at 跟着写',
    ORDERS[0].manual_user_by === '甲成员' && !!ORDERS[0].manual_user_at,
    JSON.stringify({ by: ORDERS[0].manual_user_by, at: ORDERS[0].manual_user_at }));

  /* ==================== #4 云端单不回归 ==================== */
  console.log('--- #4 云端单：body 仍是 openid，成功后改 openid/nickname ---');
  sandbox.__RESP = { success: true, openid: 'wx-def', nickname: '小李',
                     previous_openid: 'wx-abc' };
  run("applyOrderUser('3', 1)");
  setTimeout(function () {
    const req2 = REQS.filter(function (r) { return r.url.indexOf('/order_user') >= 0; }).pop() || {};
    let body2 = {};
    try { body2 = JSON.parse(req2.body || '{}'); } catch (e) {}
    chk('云端单 body.openid = wx-def', body2.openid === 'wx-def', req2.body);
    chk('云端单 body 不带 owner_name', !('owner_name' in body2), req2.body);
    chk('订单对象 openid/nickname 已更新',
      ORDERS[2].openid === 'wx-def' && ORDERS[2].nickname === '小李',
      JSON.stringify({ openid: ORDERS[2].openid, nickname: ORDERS[2].nickname }));

    /* ==================== #5 v7.20 外部监视单的用户 / 类型订正 ==================== */
    console.log('--- #5 外部单：写 /api/local/external_prints/meta ---');
    sandbox.__EXT = {
      order_id: 'ext:4:2026-11-09 07:00:00', order_number: 'EXT-20261109-070000-4',
      source: 'external', external: true, openid: 'local', owner_name: '甲成员',
      is_admin_print: false, status: 'sent', total_price: 1.0, files_count: 1,
      created_at: '2026-11-09 07:00:00', files: [],
    };
    // 不塞进 _cloudData.orders：外部单的 id 不是数字，renderCloud 会打「非法 ID 已忽略」；
    // 这条路径本身只认传进来的订单对象（_applyExternalOrderMeta(o, …)）。
    sandbox.__RESP = { success: true, owner_name: '乙成员' };
    const _before5 = REQS.length;
    run("_applyExternalOrderMeta(__EXT, '乙成员', null)");
    setTimeout(function () {
      const req5 = REQS.slice(_before5).filter(function (r) {
        return r.url.indexOf('/external_prints/meta') >= 0; }).pop() || {};
      let b5 = {};
      try { b5 = JSON.parse(req5.body || '{}'); } catch (e) {}
      chk('外部单改用户：POST /api/local/external_prints/meta（不是云端 order_user）',
        req5.url === '/api/local/external_prints/meta', req5.url);
      chk('body 带 raw ext: order_id 与 owner_name，且不带 openid/order_type',
        b5.order_id === 'ext:4:2026-11-09 07:00:00' && b5.owner_name === '乙成员'
        && !('openid' in b5) && !('order_type' in b5), req5.body);
      chk('落地：订单 owner_name 与 manual_owner 一起更新',
        sandbox.__EXT.owner_name === '乙成员' && sandbox.__EXT.manual_owner === '乙成员',
        JSON.stringify({ o: sandbox.__EXT.owner_name, m: sandbox.__EXT.manual_owner }));

      sandbox.__RESP = { success: true, order_type: 'self' };
      const _before6 = REQS.length;
      run("_applyExternalOrderMeta(__EXT, null, 'self')");
      setTimeout(function () {
        const req6 = REQS.slice(_before6).filter(function (r) {
          return r.url.indexOf('/external_prints/meta') >= 0; }).pop() || {};
        let b6 = {};
        try { b6 = JSON.parse(req6.body || '{}'); } catch (e) {}
        chk('外部单改类型：body 只带 order_type，不带 owner_name',
          b6.order_type === 'self' && !('owner_name' in b6), req6.body);
        chk('自打落地：is_admin_print=true、manual_type=self（收益口径立刻跟着变）',
          sandbox.__EXT.is_admin_print === true && sandbox.__EXT.manual_type === 'self',
          JSON.stringify({ a: sandbox.__EXT.is_admin_print, t: sandbox.__EXT.manual_type }));

        sandbox.__RESP = { success: false, message: '账本写入失败' };
        run("_applyExternalOrderMeta(__EXT, '甲成员', null)");
        setTimeout(function () {
          chk('接口失败时订单对象不被改动（不做假落地）',
            sandbox.__EXT.owner_name === '乙成员', sandbox.__EXT.owner_name);

          console.log('\n' + (bad === 0 ? '✅ 全部通过' : '❌ ' + bad + ' 项失败'));
          process.exit(bad === 0 ? 0 : 1);
        }, 20);
      }, 20);
    }, 20);
  }, 20);
}, 20);
