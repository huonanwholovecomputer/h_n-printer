/* eslint-disable */
/**
 * 文件卡片生命周期验收（小程序）：删除卡片后，"下面的卡片挪上来"不得重播入场动画
 *
 * 线上现象：删除某张文件卡片时，它下方的卡片向上滑动之后又播了一次入场动画。
 *
 * 机制（本脚本钉住的两条）：
 *  ① wx:for 的 key 必须是**稳定身份** `_uid`，不能是 `index`。
 *    用 index 时，删除中间的卡片后，被删那张的节点会被"就地复用"给上移的卡片：
 *    节点的 class 从 `card-removing`（cardRemove 动画）变成 `card-entering`
 *    （cardExpand/cardFadeIn 动画）→ animation-name 变化 → **入场动画重播**。
 *    换成 `_uid` 后每张卡片独占自己的节点，上移只是位置变化，不会再触发动画。
 *  ② `entering` 标记必须可靠清除（按 uid 清、重印恢复路径也清）。只要卡片永久挂着
 *    `card-entering`，任何一次节点重排都会把入场动画再播一遍。
 *
 * 用真实 pages/index/index.js + 桩 wx 驱动，断言"删完卡片后列表里没有任何 entering 残留"。
 * 用法：node tests/verify_file_card_lifecycle.js
 */
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const REPO = path.resolve(__dirname, '..');
const MP_JS = path.join(REPO, 'mobile_apps', 'h_n_print', 'pages', 'index', 'index.js');
const MP_WXML = path.join(REPO, 'mobile_apps', 'h_n_print', 'pages', 'index', 'index.wxml');

let failures = 0;
function check(name, cond, detail) {
  if (!cond) failures++;
  console.log(`[${cond ? 'PASS' : 'FAIL'}] ${name}` + (detail ? `  -- ${detail}` : ''));
}
function section(t) { console.log('\n================ ' + t + ' ================'); }

function setByPath(obj, key, val) {
  const m = key.match(/^([A-Za-z0-9_$]+)((\[\d+\]|\.[A-Za-z0-9_$]+)*)$/);
  if (!m) { obj[key] = val; return; }
  const parts = m[2].match(/\[\d+\]|\.[A-Za-z0-9_$]+/g) || [];
  if (!parts.length) { obj[m[1]] = val; return; }
  let cur = obj[m[1]];
  if (cur === undefined || cur === null) { obj[m[1]] = cur = {}; }
  for (let i = 0; i < parts.length; i++) {
    const p = parts[i];
    const isLast = i === parts.length - 1;
    const k = p[0] === '[' ? Number(p.slice(1, -1)) : p.slice(1);
    if (isLast) { cur[k] = val; return; }
    cur = cur[k];
    if (cur === undefined || cur === null) throw new Error('bad path ' + key);
  }
}

function loadPage() {
  const src = fs.readFileSync(MP_JS, 'utf8');
  let pageConfig = null;
  const picked = [];
  const sandbox = {
    console: { log() {}, warn: console.warn, error: console.error },
    JSON, Math, Date, Object, Array, String, Number, Boolean, Error, Promise, Set, Map,
    parseInt, parseFloat, isNaN,
    setTimeout, clearTimeout, setInterval, clearInterval,
    require: (p) => {
      if (p.indexOf('utils/config') >= 0) return { CONFIG: { BASE_URL: 'https://example.test' } };
      if (p.indexOf('utils/request') >= 0) return { request: () => {} };
      throw new Error('unexpected require: ' + p);
    },
    Page: (cfg) => { pageConfig = cfg; },
    Component: (cfg) => { pageConfig = cfg; },
    getApp: () => ({ globalData: { isDarkMode: false, themeMode: 'light' } }),
    wx: {
      getStorageSync: () => 'tok', setStorageSync() {}, removeStorageSync() {},
      setBackgroundColor() {}, switchTab() {}, showToast() {}, showLoading() {}, hideLoading() {},
      getWindowInfo: () => ({ windowWidth: 375, windowHeight: 800 }),
      getSystemInfoSync: () => ({ windowWidth: 375, windowHeight: 800, platform: 'devtools' }),
      createSelectorQuery: () => ({
        select: () => ({ boundingClientRect() { return this; }, scrollOffset() { return this; } }),
        selectAll: () => ({ boundingClientRect() { return this; } }),
        in: function () { return this; }, exec(cb) { cb && cb([[], null, { scrollTop: 0 }]); },
      }),
      nextTick: (fn) => setTimeout(fn, 0),
      uploadFile: () => ({ abort() {}, onProgressUpdate() {} }),
      // 选择文件：同步回调，复现"选完立刻删掉一个"的真实时序
      chooseMessageFile: (o) => {
        if (picked.length && o && o.success) o.success({ tempFiles: picked.slice() });
        if (o && o.complete) o.complete();
      },
    },
  };
  sandbox.__pick = (files) => { picked.length = 0; files.forEach(f => picked.push(f)); };
  sandbox.global = sandbox;
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(src, sandbox, { filename: 'index.js' });

  const page = Object.assign({}, pageConfig, pageConfig.methods);
  page.data = JSON.parse(JSON.stringify(pageConfig.data || {}));
  page.setData = function (patch, cb) {
    Object.keys(patch || {}).forEach(k => setByPath(this.data, k, patch[k]));
    if (cb) cb();
  };
  page._scheduleMeasure = () => {};
  page.getTabBar = () => null;
  page._uploadTimers = {};    // 真实环境在 onLoad 初始化，桩里手动补齐
  page._pollTimers = {};
  page._fileUidSeq = 0;
  page.data.userRole = 'admin';    // 通过 onChooseFile 的访客拦截
  return { page, sandbox };
}

const wait = (ms) => new Promise(r => setTimeout(r, ms));
const anyEntering = (page) => page.data.selectedFiles.filter(f => f.entering).map(f => f.name);

(async () => {
  /* ---------------- 静态：wx:key 必须是稳定 _uid ---------------- */
  section('静态：列表 key 用稳定身份');
  const wxml = fs.readFileSync(MP_WXML, 'utf8');
  check('wx:for 的 wx:key 是 _uid（不是 index）',
        /wx:for="\{\{selectedFiles\}\}"/.test(wxml) && /wx:key="_uid"/.test(wxml)
        && !/wx:key="index"/.test(wxml));

  /* ---------------- 添加后立刻删除：不许留下 entering ---------------- */
  section('添加 → 立刻删中间一张 → 等待动画结束后无 entering 残留');
  const { page, sandbox } = loadPage();
  sandbox.__pick([
    { name: 'A.pdf', size: 1000, path: '/tmp/A.pdf' },
    { name: 'B.pdf', size: 1000, path: '/tmp/B.pdf' },
    { name: 'C.pdf', size: 1000, path: '/tmp/C.pdf' },
  ]);
  page.onChooseFile();
  check('三个卡片进入列表且都带 entering（入场中）',
        page.data.selectedFiles.length === 3 && page.data.selectedFiles.every(f => f.entering),
        JSON.stringify(page.data.selectedFiles.map(f => `${f.name}:${f.entering}`)));

  // 立刻删掉中间那张（此时三张卡的 entering 都还没被清）
  page.onRemoveFile({ currentTarget: { dataset: { index: 1 } } });
  await wait(1000);   // 600ms 后 splice + 800ms 的 entering 清除都已完成

  check('删除后列表为 [A, C]',
        page.data.selectedFiles.length === 2
        && page.data.selectedFiles[0].name === 'A.pdf'
        && page.data.selectedFiles[1].name === 'C.pdf',
        page.data.selectedFiles.map(f => f.name).join(','));
  check('删除完成后没有任何卡片残留 entering（残留就会在节点重排时重播入场动画）',
        anyEntering(page).length === 0, JSON.stringify(anyEntering(page)));
  check('被删的 B 不再出现在列表中',
        !page.data.selectedFiles.some(f => f.name === 'B.pdf'));

  /* ---------------- 删第一张：其余卡片的 entering 也必须被清 ---------------- */
  section('添加 → 删第一张 → 其余卡片 entering 同样清干净');
  const { page: p2, sandbox: s2 } = loadPage();
  s2.__pick([
    { name: 'D.pdf', size: 1000, path: '/tmp/D.pdf' },
    { name: 'E.pdf', size: 1000, path: '/tmp/E.pdf' },
  ]);
  p2.onChooseFile();
  p2.onRemoveFile({ currentTarget: { dataset: { index: 0 } } });
  await wait(1000);
  check('删首位后剩余卡片无 entering 残留',
        p2.data.selectedFiles.length === 1 && p2.data.selectedFiles[0].name === 'E.pdf'
        && anyEntering(p2).length === 0,
        JSON.stringify(p2.data.selectedFiles.map(f => `${f.name}:${f.entering}`)));

  /* ---------------- 重印恢复：entering 必须被清（旧实现从不清） ---------------- */
  section('重印恢复：entering 不再永久挂着');
  const { page: p3 } = loadPage();
  p3._restoreReprintFiles({
    duplex: 'on',
    files: [
      { file_name: 'F.docx', file_id: 'F1', size: 4096, copies: 1, page_count: 0, page_range: '' },
      { file_name: 'G.pdf', file_id: 'F2', size: 2048, copies: 1, page_count: 3, page_range: '' },
    ],
  });
  check('恢复后先播入场动画（entering=true）',
        p3.data.selectedFiles.length === 2 && p3.data.selectedFiles.every(f => f.entering),
        JSON.stringify(p3.data.selectedFiles.map(f => `${f.name}:${f.entering}`)));
  await wait(1000);
  check('动画结束后 entering 全部清除（否则之后删卡片会重播入场动画）',
        anyEntering(p3).length === 0, JSON.stringify(anyEntering(p3)));

  console.log('\n' + '='.repeat(52));
  console.log('文件卡片生命周期验收:', failures === 0 ? '全部通过 ✅' : `存在 ${failures} 项失败 ❌`);
  process.exit(failures ? 1 : 0);
})();
