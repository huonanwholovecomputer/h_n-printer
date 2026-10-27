/* eslint-disable */
/**
 * 订单轮询「子文件配对」验收（D5 第 6 条）
 *
 * 线上形状（`pages/me/me.js` 的 `_pollOrdersSilent`）：增量更新子文件状态时按**数组下标**
 * 配对 —— `n.files[j].status !== o.files[j].status`，从不比对 `file_id`。
 * 子文件集合一旦变化（增删、顺序变化），第 j 行的新状态就被写到**另一个文件**的行上：
 * 界面显示"A 已完成 / B 进行中"，而实际正好相反（用户据此判断该取哪份纸，会拿错）。
 * 另一处：子文件同步被嵌在 `if (n.status !== o.status)` 里 —— 聚合口径
 * （failed > printing > queued > sent）下"某文件变了、父状态没变"（如 已sent + queued ⇒ 父仍 queued）
 * 时，那一行的状态永远不更新。
 *
 * 本脚本用**真实 me.js**（vm + 桩 Component/request/wx）驱动 `_pollOrdersSilent`，断言：
 *  ① 同集合同顺序、父状态变 → 照旧增量更新（回归）；
 *  ② 子文件**顺序互换** → 必须走全量刷新，**不得**按下标写错行；
 *  ③ 子文件**增删** → 必须走全量刷新；
 *  ④ 同 id、子文件状态变而**父状态不变** → 仍要按 file_id 更新到正确的下标；
 *  ⑤ 完全没变 → 不 setData（不无谓渲染）。
 *
 * 用法：node tests/verify_me_poll_file_pairing.js
 */
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const REPO = path.resolve(__dirname, '..');
const ME_JS = path.join(REPO, 'mobile_apps', 'h_n_print', 'pages', 'me', 'me.js');

let failures = 0;
function check(name, cond, detail) {
  if (!cond) failures++;
  console.log(`[${cond ? 'PASS' : 'FAIL'}] ${name}` + (detail ? `  -- ${detail}` : ''));
}
function section(t) { console.log('\n================ ' + t + ' ================'); }

/** 装真实 utils 模块（失败关闭：读不到或没导出就抛，别静默放行）。 */
function loadModule(rel, exportName) {
  const src = fs.readFileSync(path.join(REPO, 'mobile_apps', 'h_n_print', rel), 'utf8');
  const m = { exports: {} };
  new Function('module', 'exports', 'require', src)(m, m.exports, (dep) => {
    throw new Error('真实模块 ' + rel + ' 意外 require 了 ' + dep);
  });
  if (exportName && typeof m.exports[exportName] !== 'function') {
    throw new Error(rel + ' 未导出 ' + exportName);
  }
  return m.exports;
}

const CONFIG_MOD = { CONFIG: { BASE_URL: 'https://example.invalid' } };
const PAGES_MOD = loadModule('utils/pages.js', 'countPagesInRange');
// 头像裁剪几何（me.js 2026-10 起 require 的模块）：**真实加载**，文件缺失/语法错直接抛，
// 不做"空对象桩" —— 否则 me.js 一旦依赖它的导出就会被静默放过。
const AVATAR_CROP_MOD = loadModule('utils/avatar_crop.js');

/** 载入真实 me.js，返回 { methods, captured: {lastOpts} } */
function loadMe() {
  const src = fs.readFileSync(ME_JS, 'utf8');
  const captured = { lastOpts: null, component: null };
  let exportsObj = null;
  const sandbox = {
    console,
    setTimeout: () => 0,
    clearTimeout: () => {},
    setInterval: () => 0,
    clearInterval: () => {},
    module: { exports: {} },
    exports: {},
    require: (dep) => {
      if (dep.endsWith('utils/config')) return CONFIG_MOD;
      if (dep.endsWith('utils/pages')) return PAGES_MOD;
      if (dep.endsWith('utils/avatar_crop')) return AVATAR_CROP_MOD;
      if (dep.endsWith('utils/request')) {
        return { request: (opts) => { captured.lastOpts = opts; } };
      }
      throw new Error('shim 未提供的 require: ' + dep);
    },
    wx: {
      getStorageSync: (k) => (k === 'token' ? 'tok' : ''),
      getWindowInfo: () => ({ windowWidth: 375, windowHeight: 800, safeArea: { bottom: 800 } }),
      getSystemInfoSync: () => ({ windowWidth: 375, windowHeight: 800, safeArea: { bottom: 800 } }),
      setStorageSync: () => {},
      showToast: () => {},
      createSelectorQuery: () => ({ select: () => ({ boundingClientRect: () => ({ exec: () => {} }) }) }),
      nextTick: (fn) => fn && fn(),
    },
    getApp: () => ({ globalData: {} }),
    Component: (obj) => { captured.component = obj; },
    Page: (obj) => { captured.component = obj; },
  };
  sandbox.global = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(src, sandbox, { filename: 'me.js' });
  const comp = captured.component;
  if (!comp || !comp.methods || typeof comp.methods._pollOrdersSilent !== 'function') {
    throw new Error('me.js 未暴露 methods._pollOrdersSilent（夹具需同步更新）');
  }
  return { methods: comp.methods, captured };
}

function makeCtx(methods, orders, page) {
  const patches = [];
  const ctx = {
    data: { orders, ordersCurrentPage: 1, ordersPerPage: 10 },
    setData: (patch) => { patches.push(patch); },
    patches,
  };
  Object.keys(methods).forEach((k) => { ctx[k] = methods[k]; });
  return ctx;
}

function orderWith(id, status, files) {
  return { id, status, files, total_price: 1, print_started_at: '', received_label: '' };
}

function drive(ordersOld, ordersNew) {
  const { methods, captured } = loadMe();
  const ctx = makeCtx(methods, ordersOld);
  ctx._pollOrdersSilent.call(ctx);
  if (!captured.lastOpts || typeof captured.lastOpts.success !== 'function') {
    throw new Error('_pollOrdersSilent 未通过 utils/request 发请求（接线变了？）');
  }
  captured.lastOpts.success({ statusCode: 200, data: { success: true, orders: ordersNew } });
  return ctx.patches;
}

function patchKeys(patches) {
  return patches.flatMap((p) => Object.keys(p));
}

section('① 同集合同顺序、父状态变化 → 照旧增量更新（回归）');
{
  const oldO = [orderWith(1, 'queued', [{ file_id: 11, status: 'queued' }, { file_id: 12, status: 'queued' }])];
  const newO = [orderWith(1, 'printing', [{ file_id: 11, status: 'printing' }, { file_id: 12, status: 'queued' }])];
  const keys = patchKeys(drive(oldO, newO));
  check('父状态增量更新', keys.includes('orders[0].status'), JSON.stringify(keys));
  check('子文件按 id 更新到正确下标（file 11 → files[0]）',
    keys.includes('orders[0].files[0].status'), JSON.stringify(keys));
  check('未变化的子文件（file 12）不写入', !keys.includes('orders[0].files[1].status'), JSON.stringify(keys));
  check('没有走全量刷新', !keys.includes('orders'), JSON.stringify(keys));
}

section('② 子文件顺序互换 → 必须全量刷新，不得按下标写错行 ← 本条修复点');
{
  const oldO = [orderWith(1, 'queued', [{ file_id: 11, status: 'queued' }, { file_id: 12, status: 'queued' }])];
  // 服务端把两条子文件换了顺序，且 file 12 变成 sent
  const newO = [orderWith(1, 'printing', [{ file_id: 12, status: 'sent' }, { file_id: 11, status: 'queued' }])];
  const keys = patchKeys(drive(oldO, newO));
  check('★ 走全量刷新（orders 整体替换）', keys.includes('orders'), JSON.stringify(keys));
  check('★ 不再按下标写子文件状态（改前会把 file 12 的 sent 写到 file 11 的行上）',
    !keys.some((k) => k.includes('.files[')), JSON.stringify(keys));
}

section('③ 子文件增删 → 必须全量刷新');
{
  const oldO = [orderWith(1, 'queued', [{ file_id: 11, status: 'queued' }])];
  const newO = [orderWith(1, 'queued', [{ file_id: 11, status: 'queued' }, { file_id: 13, status: 'queued' }])];
  const keys = patchKeys(drive(oldO, newO));
  check('★ 新增子文件 → 全量刷新', keys.includes('orders'), JSON.stringify(keys));
  check('不按下标写子文件状态', !keys.some((k) => k.includes('.files[')), JSON.stringify(keys));
}

section('④ 父状态不变、子文件状态变了 → 仍要更新（且下标正确）← 第二处修复点');
{
  // 聚合口径：一个 sent + 一个 queued ⇒ 父仍是 queued（status 没变）
  const oldO = [orderWith(1, 'queued', [{ file_id: 11, status: 'queued' }, { file_id: 12, status: 'queued' }])];
  const newO = [orderWith(1, 'queued', [{ file_id: 11, status: 'sent' }, { file_id: 12, status: 'queued' }])];
  const keys = patchKeys(drive(oldO, newO));
  check('★ 父状态没变也要同步子文件（改前嵌在父状态分支里 ⇒ 永远不更新）',
    keys.includes('orders[0].files[0].status'), JSON.stringify(keys));
  check('变化的那个文件写到自己的下标（file 11 → files[0]）',
    keys.includes('orders[0].files[0].status') && !keys.includes('orders[0].files[1].status'),
    JSON.stringify(keys));
  check('父状态不变 → 不写 status 字段', !keys.includes('orders[0].status'), JSON.stringify(keys));
}

section('⑤ 完全没变 → 不 setData（不无谓渲染）');
{
  const same = [orderWith(1, 'queued', [{ file_id: 11, status: 'queued' }])];
  const patches = drive(same, JSON.parse(JSON.stringify(same)));
  check('无任何 setData', patches.length === 0, JSON.stringify(patches));
}

section('⑥ 静态判据：配对必须看 file_id');
{
  const src = fs.readFileSync(ME_JS, 'utf8');
  const code = src.replace(/\/\/[^\n]*/g, '');
  check('★ 源码里有 oldById[..] / file_id 配对逻辑', /oldById\s*\[/.test(code) && /file_id/.test(code));
  check('★ 已无按下标配对子文件的旧写法（n.files[j] 与 o.files[j] 直接比 status）',
    !/o\.files\[j\]\.status\s*!==/.test(code) && !/n\.files\[j\]\.status\s*!==/.test(code));
  check('子文件集合不一致时置 changed（全量刷新）', /newIds\s*!==\s*oldIds/.test(code));
}

console.log('\n' + (failures ? `存在失败项 ❌ (${failures})` : '全部通过 ✅'));
process.exit(failures ? 1 : 0);
