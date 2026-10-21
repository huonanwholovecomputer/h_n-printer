/* eslint-disable */
/**
 * 「范围输入框正在输入时页数返回」验收（小程序为主 + APP 静态接线）
 *
 * 线上现象（2026-10-21 现场截图）：上传 doc/docx 后卡片进入"页数未知"态（黄色警告 + 多行
 * 范围输入）。等待本地打印工具转换期间用户点了范围输入框、正在打字，页数恰好在这时返回 ⇒
 * **输入框的占位符与「全部页 / 已选 …」摘要按钮画在同一位置**（重叠）。
 *
 * 根因（本脚本钉住的第一件事）：
 *   微信小程序 `<input>` **只在 focus 时表现为原生组件**。原生组件渲染在 WebView 之上，
 *   不参与父级的 max-height / overflow / opacity 裁剪。页数一到就把 `.range-inputs-state`
 *   收成 max-height:0，原生输入框却留在原地 ⇒ 与下方（此时已上移）的摘要按钮重叠。
 *
 * 修法（两个互补的部分，缺一都会让现象回来）：
 *   ① `_blurRangeInputs()`：页数确认时若该文件的范围输入还聚焦着，先把它失焦
 *      （`focus="{{item.rangeFocusLine === li}}"` 置 -1）+ `wx.hideKeyboard()`，原生层退回
 *      同层渲染后才吃得到裁剪；返回值并进同一批 setData。
 *   ② 布局闸门 `rangeEditing`：`index.wxml` 里**输入区收起**与**摘要按钮显示**都多带一个
 *      `!item.rangeEditing`，而 `rangeEditing` **只由真正的 bindblur 清**。于是"输入框在屏上"
 *      与"页数选择器在屏上"在数据层面互斥 —— 就算 ① 在某些基础库上失焦没生效，界面也只是
 *      停在输入态，**不会**出现重叠。
 *
 * 反向自证：把 wxml 里的 `!item.rangeEditing` 删掉、或让 `_blurRangeInputs` 顺手把
 * rangeEditing 也清了，下面的断言都会转红（脚本里各有一处变异用例）。
 *
 * 用法：node tests/verify_range_input_focus_blur.js   （exit 0 = 全部通过）
 */
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const REPO = path.resolve(__dirname, '..');
const MP_JS = path.join(REPO, 'mobile_apps', 'h_n_print', 'pages', 'index', 'index.js');
const MP_WXML = path.join(REPO, 'mobile_apps', 'h_n_print', 'pages', 'index', 'index.wxml');
const APP_JS = path.join(REPO, 'mobile_apps', 'android_app', 'www', 'print.js');

// utils/pages.js 是页码范围口径的唯一实现（index.js require 它）——装真的，失败关闭
const __PAGES_EXPORTS = (() => {
  const src = fs.readFileSync(
    path.join(REPO, 'mobile_apps', 'h_n_print', 'utils', 'pages.js'), 'utf8');
  const m = { exports: {} };
  new Function('module', 'exports', src)(m, m.exports);
  if (typeof m.exports.countPagesInRange !== 'function') {
    throw new Error('utils/pages.js 未导出 countPagesInRange');
  }
  return m.exports;
})();

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
  const box = {
    requests: [],          // 桩 request 收到的调用（含 success/fail，用于喂页数）
    hideKeyboard: 0,       // wx.hideKeyboard 调用次数
  };
  const sandbox = {
    console: { log() {}, warn: console.warn, error: console.error },
    JSON, Math, Date, Object, Array, String, Number, Boolean, Error, Promise, Set, Map,
    parseInt, parseFloat, isNaN,
    setTimeout, clearTimeout, setInterval, clearInterval,
    require: (p) => {
      if (p.indexOf('utils/pages') >= 0) return __PAGES_EXPORTS;
      if (p.indexOf('utils/config') >= 0) return { CONFIG: { BASE_URL: 'https://example.test' } };
      if (p.indexOf('utils/request') >= 0) {
        return { request: (opts) => { box.requests.push(opts); } };
      }
      throw new Error('unexpected require: ' + p);
    },
    Page: (cfg) => { pageConfig = cfg; },
    Component: (cfg) => { pageConfig = cfg; },
    getApp: () => ({ globalData: { isDarkMode: false, themeMode: 'light' } }),
    wx: {
      getStorageSync: () => 'tok', setStorageSync() {}, removeStorageSync() {},
      setBackgroundColor() {}, switchTab() {}, showToast() {}, showLoading() {}, hideLoading() {},
      hideKeyboard: () => { box.hideKeyboard++; },
      getWindowInfo: () => ({ windowWidth: 375, windowHeight: 800 }),
      getSystemInfoSync: () => ({ windowWidth: 375, windowHeight: 800, platform: 'devtools' }),
      createSelectorQuery: () => ({
        select: () => ({ boundingClientRect() { return this; }, scrollOffset() { return this; } }),
        selectAll: () => ({ boundingClientRect() { return this; } }),
        in: function () { return this; }, exec(cb) { cb && cb([[], null, { scrollTop: 0 }]); },
      }),
      nextTick: (fn) => setTimeout(fn, 0),
      uploadFile: () => ({ abort() {}, onProgressUpdate() {} }),
      chooseMessageFile: () => {},
    },
  };
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
  page._uploadTimers = {};
  page._pollTimers = {};
  page._fileUidSeq = 0;
  page.data.userRole = 'admin';
  return { page, box };
}

// 一张"页数未知"的 Word 卡片（analyzing：黄色警告 + 多行范围输入在屏）
function makeFile(over) {
  return Object.assign({
    _uid: 1, name: '第一次线下作业.docx', size: 13926, path: '/tmp/a.docx', sizeDisplay: '13.6',
    fileId: 9001, uploading: false, progress: 100, failed: false, copies: 1,
    pageRange: '', rangeLines: [{ value: '', error: '' }],
    duplex: 'on', imageOrientation: 'auto',
    entering: false, removing: false,
    excelWarning: false, unsupportedFormat: false, isImage: false,
    pageCount: 0, pageCountStatus: 'analyzing', singlePage: false,
    rangeFocusLine: -1, rangeEditing: false,
  }, over || {});
}

const ev = (fileIndex, lineIndex) => ({ currentTarget: { dataset: { fileIndex, lineIndex } } });
const evInput = (fileIndex, lineIndex, value) => ({
  currentTarget: { dataset: { fileIndex, lineIndex } }, detail: { value },
});

/* 喂一次「页数已验证返回」：走真实的 _startPageCountPoll → 桩 request.success */
function feedPageCount(page, box, file, pageCount) {
  // 与生产一致：_startPageCountPoll 的 uid 来自 _fileUid()（数字，不是字符串）
  page._startPageCountPoll(file._uid, file.fileId);
  const opts = box.requests[box.requests.length - 1];
  if (!opts) throw new Error('页数轮询没有发出请求');
  opts.success({
    statusCode: 200,
    data: { success: true, page_count: pageCount, verified: true, printer_online: true },
  });
  page._stopPageCountPoll(file._uid);
}

const rangeInputTag = (wxml) => {
  const m = wxml.match(/<input\b[\s\S]*?range-line-input[\s\S]*?\/>/);
  return m ? m[0] : '';
};
const summaryBlock = (wxml) => {
  const m = wxml.match(/<block wx:if="\{\{[^"]*\}\}">\s*<view class="range-summary-trigger"/);
  return m ? m[0] : '';
};
const inputsStateTag = (wxml) => {
  const m = wxml.match(/<view class="range-inputs-state[^"]*"/);
  return m ? m[0] : '';
};

/* ---- WXML 侧判据（可变异自证）---- */
function wxmlWiringOk(wxml) {
  const input = rangeInputTag(wxml);
  const state = inputsStateTag(wxml);
  const summary = summaryBlock(wxml);
  return {
    bindfocus: /bindfocus="onRangeLineFocus"/.test(input),
    focusBound: /focus="\{\{item\.rangeFocusLine === li\}\}"/.test(input),
    inputGate: /!item\.rangeEditing/.test(state),
    summaryGate: /!item\.rangeEditing/.test(summary),
    // 警告行不参与焦点闸门（它非原生组件，页数一到就该收起）
    warnIndependent: /class="range-unknown-warn \{\{\(item\.pageCountStatus === 'confirmed'[^}]*\}\}"/.test(wxml),
  };
}

(async () => {
  const wxml = fs.readFileSync(MP_WXML, 'utf8');
  const appJs = fs.readFileSync(APP_JS, 'utf8');
  const mpJs = fs.readFileSync(MP_JS, 'utf8');

  /* ================= 静态 ①：WXML 接线 ================= */
  section('静态：范围输入框聚焦接线 + 输入区/选择器互斥闸门');
  const w = wxmlWiringOk(wxml);
  check('范围输入框绑定了 bindfocus="onRangeLineFocus"', w.bindfocus);
  check('范围输入框 focus 绑到 item.rangeFocusLine（失焦靠它反转）', w.focusBound,
        rangeInputTag(wxml).split('\n').filter(l => /focus/.test(l)).join(' | '));
  check('输入区收起条件带 !item.rangeEditing（还在输入就不收起）', w.inputGate, inputsStateTag(wxml));
  check('摘要按钮显示条件带 !item.rangeEditing（还在输入就不出选择器）', w.summaryGate, summaryBlock(wxml));
  check('黄色警告行不受焦点闸门影响（页数一到照常收起）', w.warnIndependent);

  /* 变异自证：删掉任一处闸门，判据必须转红 */
  const mutated1 = wxml.replace(
    "<view class=\"range-inputs-state {{(item.pageCountStatus === 'confirmed' && item.pageCount > 0 && !item.rangeEditing) ? 'state-collapsed' : ''}}\"",
    "<view class=\"range-inputs-state {{(item.pageCountStatus === 'confirmed' && item.pageCount > 0) ? 'state-collapsed' : ''}}\"");
  check('变异自证：输入区闸门被删 → inputGate 转红',
        !wxmlWiringOk(mutated1).inputGate);
  const mutated2 = wxml.replace(
    "<block wx:if=\"{{item.pageCountStatus === 'confirmed' && item.pageCount > 0 && !item.rangeEditing}}\">",
    "<block wx:if=\"{{item.pageCountStatus === 'confirmed' && item.pageCount > 0}}\">");
  check('变异自证：摘要闸门被删 → summaryGate 转红',
        mutated2 !== wxml && !wxmlWiringOk(mutated2).summaryGate);

  /* ================= 静态 ②：两条卡片构造路径都初始化焦点字段 ================= */
  section('静态：卡片构造路径初始化 rangeFocusLine / rangeEditing');
  const ctorCount = (mpJs.match(/rangeFocusLine: -1/g) || []).length;
  check('新增文件与重印恢复两处都带 rangeFocusLine: -1', ctorCount >= 2, 'count=' + ctorCount);
  check('两处都带 rangeEditing: false', (mpJs.match(/rangeEditing: false/g) || []).length >= 2);

  /* ================= 行为 ①：正在输入时页数返回 ================= */
  section('行为：输入中页数返回 → 先失焦、且输入区/选择器不同时上屏');
  {
    const { page, box } = loadPage();
    const file = makeFile();
    page.data.selectedFiles = [file];

    page.onRangeLineFocus(ev(0, 0));
    check('聚焦第 1 行 → rangeFocusLine=0 / rangeEditing=true',
          page.data.selectedFiles[0].rangeFocusLine === 0
          && page.data.selectedFiles[0].rangeEditing === true,
          JSON.stringify({ line: page.data.selectedFiles[0].rangeFocusLine, editing: page.data.selectedFiles[0].rangeEditing }));

    // 用户正在打字："2-3"
    page.onRangeLineInput(evInput(0, 0, '2-3'));
    check('输入内容即时写入 rangeLines（不依赖 blur 才保存）',
          page.data.selectedFiles[0].rangeLines[0].value === '2-3');

    // 页数返回（5 页）
    feedPageCount(page, box, file, 5);
    const f = page.data.selectedFiles[0];
    check('页数已确认（pageCount=5 / status=confirmed）',
          f.pageCount === 5 && f.pageCountStatus === 'confirmed');
    check('首先失焦：rangeFocusLine 被置 -1（<input focus> 反转 → 原生层退回同层渲染）',
          f.rangeFocusLine === -1, 'rangeFocusLine=' + f.rangeFocusLine);
    check('同时收了键盘：wx.hideKeyboard 恰好调用 1 次', box.hideKeyboard === 1, 'calls=' + box.hideKeyboard);
    check('布局闸门仍在输入态：rangeEditing 还保持 true（输入区不收起、摘要按钮不渲染）',
          f.rangeEditing === true,
          'rangeEditing=' + f.rangeEditing + '（若这里为 false，说明失焦失败时仍会重叠）');
    check('用户打的 2-3 没丢：pageRange=2-3（失焦不是"丢弃输入"）',
          f.pageRange === '2-3', 'pageRange=' + JSON.stringify(f.pageRange));

    // 真正的失焦事件到达（原生层确定已退出）
    page.onRangeLineBlur(ev(0, 0));
    const f2 = page.data.selectedFiles[0];
    check('bindblur 到达 → rangeEditing=false（此后才收起输入区、放出摘要按钮）',
          f2.rangeEditing === false && f2.rangeFocusLine === -1,
          JSON.stringify({ editing: f2.rangeEditing, line: f2.rangeFocusLine }));
    check('失焦后仍保留输入结果：pageRange=2-3', f2.pageRange === '2-3');
  }

  /* ================= 行为 ②：没在输入时页数返回（不该动键盘、立即收起） ================= */
  section('行为：没聚焦时页数返回 → 不动键盘、直接进网格态');
  {
    const { page, box } = loadPage();
    const file = makeFile({ _uid: 2 });
    page.data.selectedFiles = [file];
    feedPageCount(page, box, file, 7);
    const f = page.data.selectedFiles[0];
    check('无焦点 → 不调用 wx.hideKeyboard（不去抢别的输入框的键盘）', box.hideKeyboard === 0, 'calls=' + box.hideKeyboard);
    check('rangeEditing 保持 false、rangeFocusLine=-1 → 输入区立即收起、摘要按钮可见',
          f.rangeEditing === false && f.rangeFocusLine === -1 && f.pageCount === 7);
  }

  /* ================= 行为 ③：别的文件的页数返回，不该动这个文件的输入框 ================= */
  section('行为：A 在输入、B 的页数返回 → 不影响 A 的焦点');
  {
    const { page, box } = loadPage();
    const a = makeFile({ _uid: 3, name: 'A.docx', fileId: 9101 });
    const b = makeFile({ _uid: 4, name: 'B.docx', fileId: 9102 });
    page.data.selectedFiles = [a, b];
    page.onRangeLineFocus(ev(0, 0));
    feedPageCount(page, box, b, 6);
    const fa = page.data.selectedFiles[0];
    const fb = page.data.selectedFiles[1];
    check('B 确认页数后 A 仍在输入态（A.rangeEditing=true / A.rangeFocusLine=0）',
          fa.rangeEditing === true && fa.rangeFocusLine === 0,
          JSON.stringify({ editing: fa.rangeEditing, line: fa.rangeFocusLine }));
    check('B 已确认且离开输入态（B.rangeEditing=false）', fb.rangeEditing === false && fb.pageCount === 6);
    check('没有跨文件收键盘（calls=0）', box.hideKeyboard === 0, 'calls=' + box.hideKeyboard);
  }

  /* ================= 行为 ④：_blurRangeInputs 只反转 focus、不碰布局闸门 ================= */
  section('行为：_blurRangeInputs 的契约（只失焦，不清 rangeEditing）');
  {
    const { page, box } = loadPage();
    page.data.selectedFiles = [makeFile({ _uid: 5 })];
    page.onRangeLineFocus(ev(0, 0));
    const patch = page._blurRangeInputs(0);
    check('聚焦时返回的补丁只含 rangeFocusLine=-1',
          patch && Object.keys(patch).length === 1
          && patch['selectedFiles[0].rangeFocusLine'] === -1,
          JSON.stringify(patch));
    page.setData(patch);                     // 生产里由调用方并进同一批 setData
    check('补丁落地后 rangeFocusLine=-1（<input focus> 反转）',
          page.data.selectedFiles[0].rangeFocusLine === -1);
    check('rangeEditing 没有被顺手清掉（失焦失败时这就是最后一道防线）',
          page.data.selectedFiles[0].rangeEditing === true);
    const none = page._blurRangeInputs(0);   // 已置 -1，再调不该重复
    check('已失焦后再次调用返回 null（不重复收键盘）', none === null);
    check('只收了一次键盘', box.hideKeyboard === 1, 'calls=' + box.hideKeyboard);
  }

  /* ================= 行为 ⑤：行切换时旧行 blur 晚到，不许踢掉新拿到的焦点 ================= */
  section('行为：跨行切换的 blur/focus 乱序（旧行 blur 晚到）');
  {
    const { page } = loadPage();
    page.data.selectedFiles = [makeFile({ _uid: 6 })];
    page.onRangeLineInput(evInput(0, 0, '2-3'));   // 第 1 行有内容 → 自动追加第 2 行
    page.onRangeLineFocus(ev(0, 0));
    page.onRangeLineFocus(ev(0, 1));               // 用户改点第 2 行（focus 先到）
    page.onRangeLineBlur(ev(0, 0));                // 旧行的 blur 后到
    const f = page.data.selectedFiles[0];
    check('晚到的旧行 blur 不清新行的焦点标记（rangeFocusLine 仍是 1）',
          f.rangeFocusLine === 1, 'rangeFocusLine=' + f.rangeFocusLine);
    check('rangeEditing 仍为 true（还在输入，输入区不许收起）', f.rangeEditing === true,
          'rangeEditing=' + f.rangeEditing);
  }

  /* ================= 静态 ③：APP 同场景（DOM 里不会被裁剪，但焦点要挪走） ================= */
  section('静态：APP 收起输入区前先 blur 焦点');
  {
    const blurIdx = appJs.indexOf('inputsEl.contains(active)');
    const addIdx = appJs.indexOf("inputsEl.classList.add('state-collapsed')");
    check('APP normalizeAndValidateRangeLines 里有"焦点在本卡输入区→blur"',
          blurIdx > 0 && /active\.blur\(\)/.test(appJs.slice(blurIdx - 200, blurIdx + 200)));
    check('blur 发生在原地收起（classList.add state-collapsed）之前',
          blurIdx > 0 && addIdx > 0 && blurIdx < addIdx, `blur@${blurIdx} collapse@${addIdx}`);
  }

  console.log('\n' + '='.repeat(56));
  console.log('范围输入框聚焦 × 页数返回 验收:', failures === 0 ? '全部通过 ✅' : `存在 ${failures} 项失败 ❌`);
  process.exit(failures ? 1 : 0);
})();
