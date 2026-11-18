/* eslint-disable */
/**
 * 「封面单独打印」开了却没生效 · 验收（2026-11-18，订单 HN20261118-0017）
 *
 * 现场：顾客在小程序里给两个封皮文件打开了「封面单独打印」，但生产库 order_files 里
 * 这两个文件的 simplex_pages 是空串，服务端也没打回（status=sent、reject_reason 空），
 * 本机按整份双面出了纸。根因是**客户端**的一个静默黑洞：
 *   · 开关只是布尔（simplexEnabled），打开时不预填任何页；
 *   · 输入框留空的行没有 error，提交前的校验只拦「有 error 的行」；
 *   · 于是「开了但没填页」→ 提交体 simplex_pages: ''（契约里空串 = 关闭）→ 静默不生效。
 * 全库 `select count(*) ... where simplex_pages <> ''` 长期为 0，就是这么来的。
 *
 * 本脚本钉住两端（小程序 + APP）修好后的口径：
 *   ① 开关打开且没有页 ⇒ 立刻预填封面第 1 页，并马上过一遍校验；
 *   ② 提交前「开着但一行合法页都没有」必须拦下，不许静默发空串；
 *   ③ 提交体合并逻辑只有一份（小程序收进 _simplexMergedPages），拦截与提交同判据；
 *   ④ 后端 submit 日志逐文件回显 page_range / simplex_pages（下次一眼能分清是谁丢了）。
 *
 * 用法：node other/tests/verify_simplex_toggle_guard.js   （exit 0 = 全绿）
 */
const fs = require('fs');
const path = require('path');

const REPO = path.resolve(__dirname, '..', '..');
const MP_JS = path.join(REPO, 'mobile_apps', 'h_n_print', 'pages', 'index', 'index.js');
const APP_JS = path.join(REPO, 'mobile_apps', 'android_app', 'www', 'print.js');
const BACKEND = path.join(REPO, 'mobile_apps', 'printer-backend', 'app.py');
const PAGES_MOD = path.join(REPO, 'mobile_apps', 'h_n_print', 'utils', 'pages.js');

let failures = 0;
function check(name, cond, detail) {
  if (!cond) failures++;
  console.log(`[${cond ? 'PASS' : 'FAIL'}] ${name}` + (detail ? '  -- ' + detail : ''));
}

const mp = fs.readFileSync(MP_JS, 'utf8');
const app = fs.readFileSync(APP_JS, 'utf8');
const backend = fs.readFileSync(BACKEND, 'utf8');

/* 从源码里抠一个对象方法（缩进 4 空格、到配对的右花括号为止） */
function extractMethod(src, name) {
  const start = src.indexOf('\n    ' + name + '(');
  if (start < 0) return '';
  const braceOpen = src.indexOf('{', start);
  if (braceOpen < 0) return '';
  let depth = 0;
  for (let i = braceOpen; i < src.length; i++) {
    const c = src[i];
    if (c === '{') depth++;
    else if (c === '}') {
      depth--;
      if (depth === 0) return src.slice(start, i + 1);
    }
  }
  return '';
}

console.log('\n=== ① 小程序：开关打开就预填单面页 ===');
check('①a 有默认页常量', /const SIMPLEX_DEFAULT_PAGES = '1'/.test(mp));
check('①b 打开分支会写 simplexLines（预填默认页）',
      /\.simplexLines'\] = \[\{ value: SIMPLEX_DEFAULT_PAGES, error: '' \}\]/.test(mp));
check('①c 只在「一行都没有」时预填（有页不覆盖顾客输入）',
      /else if \(!\(file\.simplexLines \|\| \[\]\)\.some\(l => \(l\.value \|\| ''\)\.trim\(\)\)\)/.test(mp));
check('①d 预填后立刻过校验',
      /if \(on\) this\._normalizeAndValidateSimplexLines\(fileIndex\)/.test(mp));

console.log('\n=== ② 小程序：开着没填页 ⇒ 提交前拦下 ===');
check('②a 有 _simplexEnabledButEmpty 方法', /\n    _simplexEnabledButEmpty\(file\) \{/.test(mp));
check('②b onSubmit 里真的调了它',
      /if \(this\._simplexEnabledButEmpty\(f\)\) \{/.test(mp));
const onSubmitAt = mp.indexOf('onSubmit(');
const guardAt = mp.indexOf('if (this._simplexEnabledButEmpty(f))');
const submitCallAt = mp.indexOf('doSubmit(', guardAt);
check('②c 拦截排在 onSubmit 内、doSubmit 之前',
      onSubmitAt > 0 && guardAt > onSubmitAt && submitCallAt > guardAt);
check('②d 拦截给的是可读 toast（点名文件 + 没填页）',
      /showToast\(\{ title: `"\$\{f\.name\}" 已打开「封面单独打印」但没填单面页（如 1）`/.test(mp));

console.log('\n=== ③ 小程序：提交体与拦截共用同一份合并逻辑 ===');
check('③a 有 _simplexMergedPages 方法', /\n    _simplexMergedPages\(file\) \{/.test(mp));
check('③b 提交体改用它（不再内联一份）',
      /const simplexPages = \(f\.simplexEnabled && duplex === 'on'\) \? this\._simplexMergedPages\(f\) : ''/.test(mp));
check('③c 拦截也用同一份（不自造第二套判据）',
      /return !this\._simplexMergedPages\(file\)/.test(mp));

console.log('\n=== ④ APP：同一套加固 ===');
check('④a 有默认页常量', /const SIMPLEX_DEFAULT_PAGES = '1';/.test(app));
check('④b 打开且没页时预填并校验',
      /sf\.simplexLines = \[\{ value: SIMPLEX_DEFAULT_PAGES, error: '' \}\];\s*\n\s*_normalizeAndValidateSimplexLines\(sf, false\);/.test(app));
const appBlockAt = app.indexOf('simplexDisabledFor(f)) continue;');
const appEmptyAt = app.indexOf("if (!f.simplexPages) {", appBlockAt);
const appSubmitAt = app.indexOf('const scheduleErr = validateSchedule();', appBlockAt);
check('④c 提交校验里拦住「开着却没页」',
      appBlockAt > 0 && appEmptyAt > appBlockAt && appEmptyAt < appSubmitAt);

console.log('\n=== ⑤ 真算一遍：开着没填页会不会被拦 ===');
const pagesMod = require(PAGES_MOD);
const mergedSrc = extractMethod(mp, '_simplexMergedPages');
const emptySrc = extractMethod(mp, '_simplexEnabledButEmpty');
const duplexSrc = extractMethod(mp, '_effectiveDuplex');
check('⑤a 三个方法都能从源码抠出来',
      !!mergedSrc && !!emptySrc && !!duplexSrc,
      `merged=${mergedSrc.length} empty=${emptySrc.length} duplex=${duplexSrc.length}`);
if (mergedSrc && emptySrc && duplexSrc) {
  const env = new Function('pageRangeSet', 'rangePages',
    `return {${mergedSrc},${emptySrc},${duplexSrc}};`)(pagesMod.pageRangeSet, pagesMod.rangePages);
  const mk = (over) => Object.assign({ simplexEnabled: true, pageCount: 3, duplex: 'on',
                                        simplexLines: [{ value: '1', error: '' }] }, over);

  check('⑤b 开了 + 填了 1 ⇒ 合并成 "1"', env._simplexMergedPages(mk({})) === '1',
        JSON.stringify(env._simplexMergedPages(mk({}))));
  check('⑤c 开了 + 填了 2、3 ⇒ 合并成 "2-3"（连续页压缩）',
        env._simplexMergedPages(mk({ simplexLines: [{ value: '2', error: '' }, { value: '3', error: '' }] })) === '2-3',
        JSON.stringify(env._simplexMergedPages(mk({ simplexLines: [{ value: '2', error: '' }, { value: '3', error: '' }] }))));
  check('⑤d 开了 + 只有空行 ⇒ 得空串（这就是现场那个黑洞）',
        env._simplexMergedPages(mk({ simplexLines: [{ value: '', error: '' }] })) === '');
  check('⑤e 开了 + 空行 ⇒ 判定为「该拦」', env._simplexEnabledButEmpty(mk({ simplexLines: [{ value: '', error: '' }] })) === true);
  check('⑤f 开了 + 有页 ⇒ 不拦', env._simplexEnabledButEmpty(mk({})) === false);
  check('⑤g 没开 ⇒ 不拦（没开就不该报错）',
        env._simplexEnabledButEmpty(mk({ simplexEnabled: false })) === false);
  check('⑤h 单面/1 页（生效双面 off）⇒ 不拦（正契约为 ea 无意义）',
        env._simplexEnabledButEmpty(mk({ simplexLines: [{ value: '', error: '' }], singlePage: true })) === false);
  check('⑤i 行里有 error 的页不算数',
        env._simplexMergedPages(mk({ simplexLines: [{ value: '9', error: '超出页数' }] })) === '');
}

console.log('\n=== ⑥ 后端：submit 日志逐文件回显 page_range / simplex_pages ===');
check('⑥a 有诊断行标记', /\[SUBMIT-FILES\]/.test(backend));
const submitLogAt = backend.indexOf('[SUBMIT] openid=');
const diagAt = backend.indexOf('[SUBMIT-FILES]');
check('⑥b 排在 [SUBMIT] 摘要之后', submitLogAt > 0 && diagAt > submitLogAt);
check('⑥c 回显三个关键字段',
      /st\.get\("page_range", ""\)/.test(backend) &&
      /st\.get\("simplex_pages", ""\)/.test(backend) &&
      /st\.get\("file_name"\) or st\.get\("file_id"\)/.test(backend));

console.log('');
if (failures) {
  console.log(`❌ 有 ${failures} 条失败`);
  process.exit(1);
}
console.log('✅ 全部通过');
process.exit(0);
