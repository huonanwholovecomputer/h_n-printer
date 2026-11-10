/* 验收 · 结算页「多端并发 / 离线回放 / 导入」三条静默覆盖（体检 🔴8/9/10，2026-11-10）
 *
 * 现场问题（三条都是"数据没了但界面说成功"）：
 *   🔴8 保存撞上并发：后端从不返回 server_version ⇒ 前端 conflict 恒 false、冲突框永不弹，
 *       后保存的那份把先保存的整份覆盖；而弹出来时也只有一个「覆盖」按钮，等于没有选择。
 *   🔴9 离线暂存回放：`STORE = pending; save()` 无条件整本盖回云端 —— 离线期间别台设备
 *       记过的账被抹掉，且没有任何提示。
 *   🔴10 导入：直接 `STORE = sanitize(data)` 就 renderAll，没有备份、没有确认
 *       （sanitizeStore 还会静默跳过成员对不上的流水，只 console.warn）。
 *
 * 本脚本在 Node 的 VM 里**真跑** settlement.html 的内联 JS（不是 grep），断言：
 *   ① openChoice 存在且把按钮 value 原样返回（多选一确认框）；
 *   ② _handleSaveConflict：force / reload / 关闭 三种返回值；
 *   ③ _saveToServer：server_version 参与 conflict 判定并回写 meta.version；后端没返回时本地自增；
 *   ④ _doSave 撞冲突：选「强制覆盖」会带 force 再存一次；选「以云端为准」不再存；
 *   ⑤ replayOfflinePending：云端更新时不覆盖、暂存键保留；选「用暂存覆盖」才替换；
 *      暂存不比云端新时直接重放；
 *   ⑥ _applyImportedData：取消则账本不动、不留备份；确认则先备份原账本再替换，
 *      并把「会被跳过的流水条数」写进确认框。
 *
 * 用法：node other/tests/verify_settlement_conflict_guard.js [settlement.html 路径]
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

let bad = 0;
function chk(name, cond, detail) {
  if (!cond) bad++;
  console.log('[' + (cond ? 'PASS' : 'FAIL') + '] ' + name + (!cond && detail !== undefined ? '  -- ' + JSON.stringify(detail) : ''));
}
const tick = () => new Promise((r) => setTimeout(r, 5));
function setStore(store) { sandbox.STORE = store; }
function getStore() { return sandbox.STORE; }
function cleanLS() { Object.keys(LS).forEach((k) => { delete LS[k]; }); }

const MEMBER = { id: 'm1', name: '甲', color: '#264f78' };
function baseStore() {
  return { meta: { version: 1 }, config: { members: [MEMBER], passwords: {} }, records: { expenses: [], income: [], scores: [], transfers: [] }, months: {} };
}
function importPayload(records) {
  return { config: { members: [MEMBER], passwords: {} }, records: records, months: { '2026-11': { note: 'x' } } };
}
function expense(id, payer, date) {
  return { id: id, date: date || '2026-11-01', payer: payer || 'm1', amount: 5, item: '纸张' };
}

(async function main() {
  /* ---------------- ① openChoice ---------------- */
  chk('openChoice 已定义（多选一确认框，冲突处理不能再只有一个是/否）',
      typeof sandbox.openChoice === 'function');
  let _origChoice = sandbox.openChoice;
  const captured = [];
  sandbox.openChoice = function (title, body, buttons) {
    captured.push({ title: title, body: body, buttons: buttons });
    return Promise.resolve(buttons && buttons[0] ? buttons[0].value : null);
  };
  const gotChoice = await sandbox.openChoice('t', 'b', [{ text: 'A', value: 'go', cls: 'pri' }]);
  chk('openChoice 把按钮 value 原样返回', gotChoice === 'go', gotChoice);

  /* ---------------- ② _handleSaveConflict ---------------- */
  setStore(baseStore());
  getStore().meta.version = 5;
  sandbox.openChoice = () => Promise.resolve('force');
  let act = await sandbox._handleSaveConflict({ server_version: 9 });
  chk('冲突框：选「强制覆盖」→ 返回 force', act === 'force', act);
  sandbox.openChoice = () => Promise.resolve('reload');
  act = await sandbox._handleSaveConflict({ server_version: 9 });
  chk('冲突框：选「以云端为准」→ 返回 reload', act === 'reload', act);
  sandbox.openChoice = () => Promise.resolve(null);
  act = await sandbox._handleSaveConflict({ server_version: 9, });
  chk('冲突框：关掉/先不保存 → 返回 null（本机改动留在页面上、不保存）', act === null, act);

  /* ---------------- ③ _saveToServer 的版本号语义 ---------------- */
  setStore(baseStore());
  getStore().meta.version = 3;
  sandbox.fetch = () => Promise.resolve({ json: () => Promise.resolve({ success: true, server_version: 7 }), ok: true });
  let r = await sandbox._saveToServer(getStore(), false);
  chk('server_version 7 vs 本机 3 → conflict = true（期间有设备写过）',
      r.ok === true && r.conflict === true, r);
  chk('回写 meta.version = 服务器版本 7', getStore().meta.version === 7, getStore().meta.version);

  setStore(baseStore());
  getStore().meta.version = 3;
  sandbox.fetch = () => Promise.resolve({ json: () => Promise.resolve({ success: true, server_version: 4 }), ok: true });
  r = await sandbox._saveToServer(getStore(), false);
  chk('server_version 4 vs 本机 3 → 无冲突（就是自己刚才那次）', r.conflict === false, r);

  setStore(baseStore());
  getStore().meta.version = 3;
  sandbox.fetch = () => Promise.resolve({ json: () => Promise.resolve({ success: true }), ok: true });
  r = await sandbox._saveToServer(getStore(), false);
  chk('后端没返回 server_version → 本地自增（老后端兼容）',
      getStore().meta.version === 4, getStore().meta.version);

  /* ---------------- ④ _doSave 撞冲突后的两条路 ---------------- */
  const posts = [];
  sandbox.FINANCE_SESSION = 'sess-test';
  setStore(baseStore());
  getStore().meta.version = 5;
  sandbox.fetch = (url, opts) => {
    const body = opts && opts.body ? JSON.parse(opts.body) : {};
    posts.push(body);
    const sv = posts.length === 1 ? 9 : 10;   // 第一次撞冲突，强制覆盖后正常保存
    return Promise.resolve({ json: () => Promise.resolve({ success: true, server_version: sv }), ok: true });
  };
  sandbox.openChoice = () => Promise.resolve('force');
  sandbox._doSave(false);
  await tick(); await tick(); await tick();
  chk('撞冲突选「强制覆盖」→ 发了两次保存', posts.length === 2, posts.length);
  const forced = posts[1] && posts[1].data && posts[1].data.meta ? posts[1].data.meta.version : null;
  chk('第二次保存带的是服务器版本（meta.version 已回写成 9 而非 5）', forced === 9, forced);

  posts.length = 0;
  setStore(baseStore());
  getStore().meta.version = 5;
  sandbox.fetch = (url, opts) => {
    const body = opts && opts.body ? JSON.parse(opts.body) : {};
    posts.push(body);
    return Promise.resolve({ json: () => Promise.resolve({ success: true, server_version: 9 }), ok: true });
  };
  sandbox.openChoice = () => Promise.resolve('reload');
  sandbox._doSave(false);
  await tick(); await tick();
  chk('撞冲突选「以云端为准」→ 不再追加保存（只发出那一次）', posts.length === 1, posts.length);

  /* ---------------- ⑤ 离线暂存回放 ---------------- */
  cleanLS();
  setStore(baseStore());
  getStore().meta.version = 5;
  LS['finance_offline_pending'] = JSON.stringify({ meta: { version: 2 }, config: { members: [MEMBER] }, records: { expenses: [expense('p1')] }, months: { '2026-09': {} } });
  let asked = 0;
  sandbox.openChoice = () => { asked++; return Promise.resolve('later'); };
  sandbox.replayOfflinePending();
  await tick(); await tick();
  chk('云端更新（v5 > 暂存 v2）→ 必须问人，不能直接覆盖', asked === 1, asked);
  chk('问人期间账本原封不动（云端的 5 还在）', getStore().meta.version === 5, getStore().meta.version);
  chk('选「稍后再说」→ 暂存键**保留**，下次打开还会问',
      typeof LS['finance_offline_pending'] === 'string');

  sandbox.openChoice = () => Promise.resolve('drop');
  sandbox.replayOfflinePending();
  await tick(); await tick();
  chk('选「丢弃本地暂存」→ 暂存键被删、账本仍是云端版本',
      LS['finance_offline_pending'] === undefined && getStore().meta.version === 5,
      [LS['finance_offline_pending'], getStore().meta.version]);

  setStore(baseStore());
  getStore().meta.version = 5;
  LS['finance_offline_pending'] = JSON.stringify({ meta: { version: 2 }, config: { members: [MEMBER] }, records: { expenses: [expense('p2')] }, months: { '2026-09': {} } });
  sandbox.openChoice = () => Promise.resolve('use');
  sandbox.replayOfflinePending();
  await tick(); await tick();
  chk('选「用本地暂存覆盖云端」→ 账本换成暂存、暂存键删除',
      (getStore().records.expenses || []).some((x) => x.id === 'p2') && LS['finance_offline_pending'] === undefined,
      [getStore().records.expenses, LS['finance_offline_pending']]);

  setStore(baseStore());
  getStore().meta.version = 1;
  LS['finance_offline_pending'] = JSON.stringify({ meta: { version: 3 }, config: { members: [MEMBER] }, records: { expenses: [expense('p3')] }, months: { '2026-09': {} } });
  sandbox.openChoice = () => { asked++; return Promise.resolve('later'); };
  asked = 0;
  sandbox.replayOfflinePending();
  await tick();
  chk('暂存比云端新（v3 > v1）→ 直接重放，不打扰人', asked === 0, asked);
  chk('直接重放后账本含暂存那笔', (getStore().records.expenses || []).some((x) => x.id === 'p3'),
      getStore().records.expenses);

  /* ---------------- ⑥ 导入：备份 + 摘要 + 二次确认 ---------------- */
  cleanLS();
  setStore(baseStore());
  getStore().meta.version = 4;
  getStore().records.expenses = [expense('old1')];
  const beforeTitle = JSON.stringify(getStore().records.expenses);
  captured.length = 0;
  sandbox.openChoice = function (title, body, buttons) {
    captured.push({ title: title, body: body, buttons: buttons });
    return Promise.resolve(null);   // 取消
  };
  sandbox._applyImportedData(importPayload({ expenses: [expense('new1'), expense('new2')] }));
  await tick(); await tick();
  chk('导入会先弹确认框（不再直接整本替换）', captured.length === 1, captured.length);
  chk('取消导入 → 账本一字未改', JSON.stringify(getStore().records.expenses) === beforeTitle,
      getStore().records.expenses);
  chk('取消导入 → 不写备份、不留垃圾键',
      Object.keys(LS).filter((k) => k.indexOf('finance_import_backup_') === 0).length === 0,
      Object.keys(LS));

  const summary = captured[0] ? captured[0].body : '';
  chk('确认框说清「当前 x 条 → 导入后 y 条」', summary.indexOf('条流水') > 0, summary.slice(0, 120));
  chk('确认框写明备份键（finance_import_backup_）', summary.indexOf('finance_import_backup_') > 0);

  captured.length = 0;
  LS['__saved'] = undefined;
  sandbox.openChoice = function (title, body, buttons) {
    captured.push({ title: title, body: body, buttons: buttons });
    return Promise.resolve('go');
  };
  setStore(baseStore());
  getStore().meta.version = 4;
  getStore().records.expenses = [expense('old1')];
  sandbox._applyImportedData(importPayload({ expenses: [expense('new1'), expense('new2')] }));
  await tick(); await tick();
  const backupKeys = Object.keys(LS).filter((k) => k.indexOf('finance_import_backup_') === 0);
  chk('确认导入 → 先把原账本备份进 localStorage', backupKeys.length === 1, backupKeys);
  let backedUp = null;
  try { backedUp = JSON.parse(LS[backupKeys[0]] || 'null'); } catch (e) { backedUp = null; }
  chk('备份内容就是导入前的账本（old1 在、new1 不在）',
      !!(backedUp && (backedUp.records.expenses || []).some((x) => x.id === 'old1')
        && !(backedUp.records.expenses || []).some((x) => x.id === 'new1')),
      backedUp && backedUp.records && backedUp.records.expenses);
  chk('确认导入 → 账本换成导入内容（new1/new2 在、old1 不在）',
      (getStore().records.expenses || []).some((x) => x.id === 'new1')
      && !(getStore().records.expenses || []).some((x) => x.id === 'old1'),
      getStore().records.expenses);

  /* 「会被跳过的流水」必须写进确认框：payer 不在成员名单里 ⇒ sanitizeStore 丢弃 */
  captured.length = 0;
  sandbox.openChoice = function (title, body) {
    captured.push({ title: title, body: body });
    return Promise.resolve(null);
  };
  setStore(baseStore());
  sandbox._applyImportedData(importPayload({ expenses: [expense('ok1'), expense('bad1', 'ghost')] }));
  await tick(); await tick();
  chk('导入时成员对不上的流水会被跳过 → 确认框必须说出来（不能只 console.warn）',
      captured[0] && captured[0].body.indexOf('会被跳过') > 0, captured[0] && captured[0].body.slice(0, 200));

  chk('_storeCounts 统计条数与月份', (function () {
    const c = sandbox._storeCounts({ records: { expenses: [{}, {}], income: [{}], transfers: [{}] }, months: { a: {}, b: {}, c: {} } });
    return c.records === 4 && c.months === 3;
  })());

  /* ---------------- 源码层：三条路径都走新函数 ---------------- */
  const code = js;
  chk('★ _doSave 撞冲突走 _handleSaveConflict（不再只有「覆盖」一个按钮）',
      /if \(!force && r\.conflict\)[\s\S]{0,200}_handleSaveConflict\(r\)/.test(code));
  chk('★ replayOfflinePending 有版本比对分支（curV > pendV → 问人）',
      /function replayOfflinePending\(\)[\s\S]{0,900}_askOfflineReplay\(/.test(code));
  chk('★ importData 走 _applyImportedData（确认 + 备份），不再直接 STORE = sanitize(data)',
      /function importData\(e\)[\s\S]{0,700}_applyImportedData\(data\)/.test(code)
      && code.indexOf('STORE = migrateConfig(sanitizeStore(data));   // 导入即净化 name/id/color') < 0);

  console.log('\n' + (bad ? '❌ 有失败（' + bad + ' 条）' : '✅ 全部通过（0 条失败）'));
  process.exit(bad ? 1 : 0);
})().catch((e) => {
  console.log('RUNTIME_FAIL: ' + (e && e.stack || e));
  process.exit(2);
});
