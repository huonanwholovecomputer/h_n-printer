/* eslint-disable */
/**
 * 移动端弹层「操作行整行宽」· 静态验收（小程序 wxss + APP css + 弹窗清单）
 *
 * 病根（机制）：.modal-sheet 是 align-items:center 的纵向 flex 容器，每个子项在交叉轴
 *   收缩为**内容宽**。按钮 .btn-primary{width:100%} 的百分比对父级求值 —— 父级若是一层
 *   width:auto 的中间 view，那 100% 就只是「100% 的文案宽」⇒ 长文案撑开、短文案塌成窄条
 *   （现场：「我已阅读并同意」vs「完成」44×62）。
 *   ⇒ 凡是「应当整行宽」的操作行容器，必须显式 width:100%（与 .modal-buttons 同一口径）。
 *
 * 本脚本只做静态断言（小程序无法无头渲染 wxss；几何另有 verify_modal_button_width_render.mjs
 * 用真实规则复刻量宽度）。运行：node tests/verify_modal_button_width.js
 */
const fs = require('fs');
const path = require('path');

const REPO = path.resolve(__dirname, '..');
const MP = path.join(REPO, 'mobile_apps', 'h_n_print');
const APP = path.join(REPO, 'mobile_apps', 'android_app', 'www');

let failures = 0;
function check(name, cond, detail) {
  if (!cond) failures++;
  console.log('[' + (cond ? 'PASS' : 'FAIL') + '] ' + name + (detail ? '  -- ' + detail : ''));
}
const read = (p) => fs.readFileSync(p, 'utf8');

/* 取某选择器第一条规则的声明块（选择器按整行/逗号分隔精确匹配，避免子串误命中） */
function decl(css, selector) {
  const lines = css.split('\n');
  for (let i = 0; i < lines.length; i++) {
    const sels = lines[i].split(',').map((s) => s.trim().replace(/{$/, '').trim());
    if (!sels.includes(selector)) continue;
    let body = '';
    for (let j = i + 1; j < lines.length; j++) {
      body += lines[j] + '\n';
      if (lines[j].includes('}')) return body;
    }
    return body;
  }
  return null;
}

const appWxss = read(path.join(MP, 'app.wxss'));
const idxWxss = read(path.join(MP, 'pages', 'index', 'index.wxss'));
const meWxss = read(path.join(MP, 'pages', 'me', 'me.wxss'));
const appCss = read(path.join(APP, 'styles.css'));

/* ---------- 1) 隐私弹层：操作行 + 保留期行必须整行宽（本次修复点） ---------- */
console.log('\n--- 1) 隐私弹层操作行（本次修复）---');
const cases = [
  ['小程序 index.wxss', idxWxss, '.privacy-sheet-actions'],
  ['小程序 index.wxss', idxWxss, '.privacy-retention-row'],
  ['APP styles.css', appCss, '.privacy-sheet-actions'],
  ['APP styles.css', appCss, '.privacy-retention-row'],
];
for (const [where, css, sel] of cases) {
  const d = decl(css, sel);
  check(where + ' ' + sel + ' 存在且显式 width:100%', !!d && /width:\s*100%/.test(d),
    d ? d.replace(/\s+/g, ' ').trim().slice(0, 80) : '**规则不存在**');
}

/* ---------- 2) 按钮仍跟随容器（width:100%，不是被写死的像素宽） ---------- */
console.log('\n--- 2) 按钮宽度来源 ---');
for (const [where, css] of [['小程序 index.wxss', idxWxss], ['APP styles.css', appCss]]) {
  const d = decl(css, '.privacy-agree-btn');
  check(where + ' .privacy-agree-btn{width:100%}（跟随容器，未改硬编码宽度）',
    !!d && /width:\s*100%/.test(d) && !/width:\s*\d+(px|cqw|rpx)/.test(d),
    d ? d.replace(/\s+/g, ' ').trim() : '**规则不存在**');
}
const actsAppCss = decl(appCss, '.privacy-sheet-actions') || '';
const actsIdxCss = decl(idxWxss, '.privacy-sheet-actions') || '';
check('操作行没被写成固定像素/cqw 宽度（不是逐个按钮硬顶）',
  !/width:\s*\d+(px|cqw|rpx)/.test(actsAppCss) && !/width:\s*\d+(px|rpx)/.test(actsIdxCss));

/* ---------- 3) 家族口径没有被改坏 ---------- */
console.log('\n--- 3) 弹层家族既有口径 ---');
const sheetDecl = decl(appWxss, '.modal-sheet');
check('.modal-sheet 仍是 align-items:center（未整族改成 stretch：.title-medium 无 text-align，会左对齐标题）',
  !!sheetDecl && /align-items:\s*center/.test(sheetDecl),
  sheetDecl ? sheetDecl.replace(/\s+/g, ' ').trim().slice(0, 120) : '**缺失**');
const mbDecl = decl(appWxss, '.modal-buttons');
check('.modal-buttons 仍 width:100%（两按钮等宽的正解范例仍在）',
  !!mbDecl && /width:\s*100%/.test(mbDecl));
check('app.wxss 记下了这条陷阱（防复发：注释含「收缩成内容宽」与 width:100%）',
  /收缩成\**内容宽/.test(appWxss) && /操作行/.test(appWxss));

/* ---------- 4) 375rpx 既定设计（有意 50% 屏宽）一处未动 ---------- */
console.log('\n--- 4) 375rpx 既定设计（本轮不动）---');
const count375 = (css) => (css.match(/width:\s*375rpx/g) || []).length;
check('index.wxss 仍 2 处 width:375rpx（725 行 .btn-primary / 1216 行 .submit-area）', count375(idxWxss) === 2, '实际 ' + count375(idxWxss));
check('me.wxss 仍 3 处 width:375rpx（498 / 912 / 1066）', count375(meWxss) === 3, '实际 ' + count375(meWxss));

/* ---------- 5) 弹窗清扫清单：任何弹层内的「操作/工具行」容器都必须整行宽 ---------- */
console.log('\n--- 5) 弹窗清扫清单（modal-sheet 内所有 xxx-actions / xxx-row 容器）---');

/* 按缩进找出每个 <view class="...modal-sheet..."> ... </view> 块（wxml 缩进是稳定的） */
function sheetBlocks(src) {
  const lines = src.split('\n');
  const out = [];
  for (let i = 0; i < lines.length; i++) {
    const m = lines[i].match(/^(\s*)<view[^>]*class="[^"]*modal-sheet/);
    if (!m) continue;
    const indent = m[1];
    const closeRe = new RegExp('^' + indent.replace(/ /g, ' ') + '<\/view>');
    let end = lines.length - 1;
    for (let j = i + 1; j < lines.length; j++) {
      if (closeRe.test(lines[j])) { end = j; break; }
    }
    out.push({ startLine: i + 1, text: lines.slice(i, end + 1).join('\n') });
  }
  return out;
}

const wxmlFiles = [];
(function walk(dir) {
  for (const name of fs.readdirSync(dir)) {
    const p = path.join(dir, name);
    const st = fs.statSync(p);
    if (st.isDirectory()) { if (name !== 'node_modules') walk(p); }
    else if (name.endsWith('.wxml')) wxmlFiles.push(p);
  }
})(MP);

/* 设计上「就是内容宽」的容器（登记理由，不是漏网） */
const BY_DESIGN = {
  'privacy-sheet-link-row': '文本链行（居中显示即设计，不是按钮行）',
  'myfile-row': '列表行（父级 scroll-view 已占满，行自身 flex 铺满）',
};
const allWxss = [appWxss, idxWxss, meWxss].join('\n')
  + fs.readdirSync(path.join(MP, 'pages')).reduce((acc, d) => {
    const f = path.join(MP, 'pages', d, d + '.wxss');
    return acc + (fs.existsSync(f) ? read(f) : '');
  }, '');

let scanned = 0;
const inventory = [];
for (const f of wxmlFiles) {
  const rel = path.relative(MP, f).replace(/\\/g, '/');
  for (const blk of sheetBlocks(read(f))) {
    scanned++;
    const tokens = new Set();
    const re = /class="([^"]+)"/g;
    let mm;
    while ((mm = re.exec(blk.text))) {
      for (const t of mm[1].split(/\s+/)) {
        if (/(^|-)actions$/.test(t) || /(^|-)row$/.test(t) || t === 'modal-buttons') tokens.add(t);
      }
    }
    const verdicts = [];
    for (const t of tokens) {
      const d = decl(allWxss, '.' + t);
      const full = !!d && /width:\s*100%/.test(d);
      let verdict;
      if (full) verdict = '整行宽';
      else if (BY_DESIGN[t]) verdict = '内容宽（' + BY_DESIGN[t] + '）';
      else verdict = '**缺 width:100%**';
      if (!full && !BY_DESIGN[t]) failures++;
      verdicts.push(t + ' → ' + verdict);
    }
    inventory.push(rel + ':' + blk.startLine + '  ' + (verdicts.join('；') || '仅直接子按钮（.btn-primary 自带 width:100%）'));
  }
}
check('扫到全部 modal-sheet 弹层（index 11 + me 4 = 15 个）', scanned === 15, '实际 ' + scanned + ' 个');
for (const line of inventory) console.log('  ' + line);

/* ---------- 6) 变异自证：把修复摘掉，静态断言必须转红 ---------- */
console.log('\n--- 6) 变异自证（断言本身不是劝退性空转）---');
const mutated = idxWxss.replace(/(\.privacy-sheet-actions\s*\{[^}]*?)\n\s*width:\s*100%;/, '$1');
const stillFixed = /\.privacy-sheet-actions\s*\{[^}]*?width:\s*100%/.test(mutated);
check('把 .privacy-sheet-actions 的 width:100% 摘掉后，第 1 节的判据必须不成立（⇒ 该断言能红）', !stillFixed);
const mutatedApp = appCss.replace(/(\.privacy-agree-btn\s*\{[^}]*?)width:\s*100%;/, '$1');
check('把 .privacy-agree-btn 的 width:100% 摘掉后，第 2 节的判据必须不成立', !/\.privacy-agree-btn\s*\{[^}]*?width:\s*100%/.test(mutatedApp));

console.log('\n' + '='.repeat(60));
console.log('弹层操作行整行宽 · 静态验收:', failures === 0 ? '全部通过' : '存在 ' + failures + ' 项失败');
process.exit(failures ? 1 : 0);
