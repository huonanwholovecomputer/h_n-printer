/* ui_diff.mjs — 小程序 vs Android APP 逐元素盘点（一次性审计工具）
 *
 * 用法:  node tools/ui_diff.mjs [输出路径]
 *
 * 提取维度:
 *   - WXML/HTML: <text>/<button>/<label>/<title> 文本 + placeholder/title/aria-label 属性
 *   - JS: 字符串字面量（含模板字符串）中的中文文案
 * 比较逻辑: 小程序有而 APP 疑似缺失 / APP 有而小程序没有（可能残留或 APP 特有）
 */
import { readFileSync, writeFileSync, mkdirSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const ROOT = resolve(__dirname, '..', '..');
const MP = (p) => resolve(ROOT, 'h_n_print', p);
const APP = (p) => resolve(ROOT, 'android_app', 'www', p);
const OUT = process.argv[2] || resolve('C:/Users/Administrator/.codex/visualizations/2026/08/12/019ff495-9242-7a43-b9b1-45125c7cb2f9/ui-sync-report.md');

const PAGES = [
  {
    name: '提交打印（pages/index）',
    mini: [['wxml', MP('pages/index/index.wxml')], ['js', MP('pages/index/index.js')]],
    app: [['html', APP('index.html')], ['js', APP('app.js')], ['js', APP('print.js')]],
  },
  {
    name: '我（pages/me）',
    mini: [['wxml', MP('pages/me/me.wxml')], ['js', MP('pages/me/me.js')]],
    app: [['html', APP('index.html')], ['js', APP('app.js')], ['js', APP('me.js')]],
  },
  {
    name: '历史授权用户（pages/authorized-users）',
    mini: [['wxml', MP('pages/authorized-users/authorized-users.wxml')], ['js', MP('pages/authorized-users/authorized-users.js')]],
    app: [['html', APP('index.html')], ['js', APP('app.js')], ['js', APP('me.js')]],
  },
  {
    name: '我的任务（pages/user-orders）',
    mini: [['wxml', MP('pages/user-orders/user-orders.wxml')], ['js', MP('pages/user-orders/user-orders.js')]],
    app: [['html', APP('index.html')], ['js', APP('app.js')], ['js', APP('me.js')]],
  },
];

const hasCJK = (s) => /[\u4e00-\u9fff]/.test(s);

function stripJsComments(src) {
  src = src.replace(/\/\*[\s\S]*?\*\//g, '');
  // 行注释：保留 http(s):// 前缀
  src = src.replace(/(https?:)?\/\/[^\n]*/g, (m, pre) => (pre ? m : ''));
  return src;
}

function extractJsStrings(src) {
  const out = [];
  const re = /(?:`(?:[^`\\]|\\.)*`|"(?:[^"\\\n]|\\.)*"|'(?:[^'\\\n]|\\.)*')/g;
  let m;
  while ((m = re.exec(src))) {
    let s = m[0].slice(1, -1);
    s = s.replace(/\\[nrt]/g, ' ');
    s = s.replace(/\{\{[^}]*\}\}/g, 'X');
    s = s.replace(/\$\{[^}]*\}/g, 'X');
    s = s.replace(/\s+/g, ' ').trim();
    if (s && s.length <= 80 && hasCJK(s)) out.push(s);
  }
  return out;
}

function extractMarkupStrings(src) {
  const out = [];
  src = src.replace(/<!--[\s\S]*?-->/g, '');
  const tagRe = /<(text|button|label|title)\b[^>]*>([\s\S]*?)<\/\1>/gi;
  let m;
  while ((m = tagRe.exec(src))) {
    const inner = m[2]
      .replace(/<[^>]+>/g, ' ')
      .replace(/\{\{[^}]*\}\}/g, 'X')
      .replace(/\s+/g, ' ')
      .trim();
    if (inner && inner.length <= 80 && hasCJK(inner)) out.push(inner);
  }
  const attrRe = /\s(placeholder|title|aria-label|label)="([^"]*)"/gi;
  while ((m = attrRe.exec(src))) {
    let v = m[2].replace(/\{\{[^}]*\}\}/g, 'X').replace(/\s+/g, ' ').trim();
    if (v && v.length <= 80 && hasCJK(v)) out.push(v);
  }
  return out;
}

function extract(kind, src) {
  if (kind === 'js') return extractJsStrings(stripJsComments(src));
  return extractMarkupStrings(src);
}

function read(p) {
  return readFileSync(p, 'utf8');
}

function compare(miniItems, appItems) {
  // miniItems/appItems: {src: 'WXML'|'JS'|'HTML', text: string}
  const miniSet = new Set(miniItems.map((i) => i.text));
  const appSet = new Set(appItems.map((i) => i.text));
  const appJoined = [...appSet].join('\n');
  const miniJoined = [...miniSet].join('\n');

  const dedupeBy = (items) => {
    const seen = new Set();
    const out = [];
    for (const i of items) {
      const k = `${i.src}\u0000${i.text}`;
      if (!seen.has(k)) {
        seen.add(k);
        out.push(i);
      }
    }
    return out;
  };
  return {
    miniSet,
    appSet,
    missing: dedupeBy(miniItems.filter((i) => !appSet.has(i.text) && !(i.text.length >= 4 && appJoined.includes(i.text)))),
    extra: dedupeBy(appItems.filter((i) => !miniSet.has(i.text) && !(i.text.length >= 4 && miniJoined.includes(i.text)))),
  };
}

function renderItems(items, mini) {
  if (!items.length) return '（无）\n';
  return (
    items
      .map((i) => `- [${i.src}] ${i.text}`)
      .join('\n') + '\n'
  );
}

const lines = [];
lines.push('# 小程序 → Android APP 逐元素同步盘点报告');
lines.push('');
lines.push(`生成时间：${new Date().toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai' })}`);
lines.push('');
lines.push('> 说明：脚本只提取中文文案元素（text/button/标题/占位符 + JS 字符串），用于找差异线索；');
lines.push('> 结构、样式、交互逻辑仍需人工复核。误报会出现在结果里，请以人工复核为准。');
lines.push('');

const summaryRows = [];

for (const page of PAGES) {
  const miniItems = [];
  const appItems = [];
  for (const [kind, p] of page.mini) {
    for (const t of extract(kind, read(p))) {
      miniItems.push({ src: kind === 'wxml' ? 'WXML' : 'JS', text: t });
    }
  }
  for (const [kind, p] of page.app) {
    for (const t of extract(kind, read(p))) {
      appItems.push({ src: kind === 'html' ? 'HTML' : 'JS', text: t });
    }
  }
  const { miniSet, appSet, missing, extra } = compare(miniItems, appItems);

  lines.push(`## ${page.name}`);
  lines.push('');
  lines.push(`- 小程序元素：${miniSet.size} 个（提取 ${miniItems.length} 条）`);
  lines.push(`- APP 元素：${appSet.size} 个（提取 ${appItems.length} 条）`);
  lines.push(`- 小程序有、APP 疑似缺失：${missing.length} 条`);
  lines.push(`- APP 有、小程序没有：${extra.length} 条`);
  lines.push('');
  lines.push('### 小程序有、APP 疑似缺失（重点核对）');
  lines.push('');
  lines.push(renderItems(missing, true));
  lines.push('### APP 有、小程序没有（可能残留 / APP 特有）');
  lines.push('');
  lines.push(renderItems(extra, false));
  lines.push('---');
  lines.push('');

  summaryRows.push(`| ${page.name} | ${miniSet.size} | ${appSet.size} | ${missing.length} | ${extra.length} |`);
}

const header = [
  '| 页面 | 小程序元素 | APP 元素 | 疑似缺失 | APP 独有 |',
  '|------|-----------|---------|---------|---------|',
  ...summaryRows,
];
const report =
  lines.slice(0, 4).join('\n') +
  '\n\n## 汇总\n\n' +
  header.join('\n') +
  '\n\n' +
  lines.slice(4).join('\n');

mkdirSync(dirname(OUT), { recursive: true });
writeFileSync(OUT, report, 'utf8');
console.log(`报告已生成: ${OUT}`);
