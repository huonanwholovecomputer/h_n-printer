#!/usr/bin/env node
/**
 * 收支清算三张环形图的「图例行」——验收（2026-10-28 用户要求：三个都同步）
 *
 * 现场两幕：
 *   ① 环形图「订单构成」只有小程序 / APP / 本地三个扇区，明细里却有来源「外部监视」的行
 *      ⇒ 根因：_sourceAgg() 只认 wechat/app/local，其余一律折成 wechat。
 *   ② 修完①后外部监视仍"看不见"：1 单 / 449 单 = 0.22% ⇒ 扇形只 0.8°，引线标签插件按
 *      MIN_ARC≈5.2° 跳过小扇区 ⇒ 图上什么都看不到。
 *      ⇒ 补一行图例（色块 + 名称 + 单数，与扇区同色）；用户随后要求**三张图都加**：
 *        订单状态分布（小状态同样画不出标签）、免费打印占比。
 *
 * 本脚本跑**真实源码**（从 settlement.html 抽出 _sourceAgg / _renderSourceDonut /
 * _donutLegendHTML / _sourceLegendHTML / _statusLegendHTML / _selfRatioLegendHTML /
 * _SOURCE_SEGS，node vm 里执行），末尾八条变异自证。
 *
 * 用法: node tests/verify_source_donut_external.js   （exit 0 = 全部通过）
 */
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const REPO = path.dirname(__dirname);
const HTML = path.join(REPO, 'local_print_tool', 'finance', 'settlement.html');
const src = fs.readFileSync(HTML, 'utf8');

let okAll = true;
function check(name, cond, detail) {
  if (!cond) okAll = false;
  console.log('[' + (cond ? 'PASS' : 'FAIL') + '] ' + name + (detail ? '  -- ' + detail : ''));
}
function section(t) { console.log('\n' + '='.repeat(68) + '\n' + t + '\n' + '='.repeat(68)); }

/* 抽顶层函数（用缩进 0 的 } 收尾）。 */
function extract(name) {
  const re = new RegExp('function ' + name + '\\([^)]*\\) \\{[\\s\\S]*?\\n\\}');
  const m = src.match(re);
  if (!m) throw new Error('找不到函数 ' + name);
  return m[0];
}
/* 抽顶层 var 数组（_SOURCE_SEGS）。 */
function extractVar(name) {
  const re = new RegExp('var ' + name + ' = \\[[\\s\\S]*?\\n\\];');
  const m = src.match(re);
  if (!m) throw new Error('找不到 var ' + name);
  return m[0];
}

const STATUS_LABEL_STUB = {
  sent: '已完成', canceled: '已取消', failed: '失败', abandoned: '放弃打印',
  rejected: '被打回', queued: '排队中', printing: '打印中', waiting: '等待中'
};
const STATUS_COLOR_STUB = {
  sent: '#22c55e', canceled: '#9ca3af', failed: '#ef4444', abandoned: '#a855f7',
  rejected: '#f59e0b', queued: '#3b82f6', printing: '#06b6d4', waiting: '#eab308'
};

/* 受控沙箱：只提供被测代码用到的最小依赖。 */
function run(sources) {
  const donutCalls = [];
  const sandbox = {
    console: console,
    _orderPages: function (o) { return Number(o && o.pages) || 0; },
    _orderMoney: function (o) { return { record: Math.round((Number(o && o.amount) || 0) * 100), free: 0 }; },
    _cloudCharts: {},
    _leaderLabelPlugin: { id: 'leader' },
    _mixToWhite: function (c) { return c; },
    document: { getElementById: function () { return { getContext: function () { return {}; } }; } },
    Chart: function (ctx, cfg) { donutCalls.push(cfg); },
    statusLabel: function (s) { return STATUS_LABEL_STUB[s] || s; },
    _statusColor: function (s) { return STATUS_COLOR_STUB[s] || '#9ca3af'; },
  };
  vm.createContext(sandbox);
  vm.runInContext(sources.filter(Boolean).join('\n'), sandbox);
  return { sandbox: sandbox, donutCalls: donutCalls };
}

const ORDERS = [
  { source: 'wechat', files: [1, 2], pages: 5, amount: 1.5 },
  { source: 'app', files: [1], pages: 2, amount: 0.6 },
  { source: 'local', files: [1], pages: 1, amount: 0.3 },
  { source: 'external', files: [1, 1, 1], pages: 1, amount: 0.3 },
  { source: 'external', files: [1], pages: 3, amount: 0.9 },
];

const LEGEND_FNS = ['_donutLegendHTML', '_sourceLegendHTML', '_statusLegendHTML', '_selfRatioLegendHTML'];
function sourcesOf(mut) {
  const one = function (name) { return mut ? mut(extract(name)) : extract(name); };
  return {
    segs: mut ? mut(extractVar('_SOURCE_SEGS')) : extractVar('_SOURCE_SEGS'),
    agg: one('_sourceAgg'),
    donut: one('_renderSourceDonut'),
    legends: LEGEND_FNS.map(one).join('\n'),
  };
}
function aggOf(mut) {
  const s = sourcesOf(mut);
  return run([s.segs, s.agg, s.donut]).sandbox._sourceAgg(ORDERS);
}
function paint(mut, rs) {
  const s = sourcesOf(mut);
  const h = run([s.segs, s.agg, s.donut]);
  h.sandbox._renderSourceDonut('cloud-chart-source', rs || h.sandbox._sourceAgg(ORDERS));
  return h.donutCalls[0] || null;
}
function legends(mut) {
  const s = sourcesOf(mut);
  return run([s.segs, s.legends]).sandbox;
}

section('① _sourceAgg：外部监视自成一段，不再折进「小程序」');
const rs = aggOf(null);
check('★ 外部监视计入自己的一段（external_orders = 2）', rs.external_orders === 2, String(rs.external_orders));
check('★ 小程序只算自己的 1 单（旧写法会把外部 2 单也算进来 → 3）', rs.wechat_orders === 1, String(rs.wechat_orders));
check('APP / 本地各 1 单（没被动过）', rs.app_orders === 1 && rs.local_orders === 1,
      'app=' + rs.app_orders + ' local=' + rs.local_orders);
check('文件数与页数也按来源分开统计', rs.external_files === 4 && rs.external_pages === 4,
      'files=' + rs.external_files + ' pages=' + rs.external_pages);
check('四来源订单数之和 = 全部订单数（中心「总订单」不再丢单）',
      rs.wechat_orders + rs.app_orders + rs.local_orders + rs.external_orders === ORDERS.length,
      String(rs.wechat_orders + rs.app_orders + rs.local_orders + rs.external_orders));

section('② 环形图：外部监视必须有扇区（颜色与来源筛选同色系）');
const cfg = paint(null);
const labels = (cfg && cfg.data && cfg.data.labels) || [];
const data = (cfg && cfg.data && cfg.data.datasets && cfg.data.datasets[0].data) || [];
const colors = (cfg && cfg.data && cfg.data.datasets && cfg.data.datasets[0].backgroundColor) || [];
const idx = labels.findIndex(function (l) { return l.indexOf('外部监视') === 0; });
check('★ 画出一个「外部监视 2单」扇区', idx >= 0 && labels[idx].indexOf('2单') > 0, JSON.stringify(labels));
check('扇区数值 = 外部订单数', data[idx] === 2, JSON.stringify(data));
check('颜色 = #0d9488（与 _SOURCE_OPTIONS / 明细徽标同色系）', colors[idx] === '#0d9488', String(colors[idx]));
check('四个来源四个扇区（一个都不许静默消失）', labels.length === 4, JSON.stringify(labels));

section('③ 悬停提示：能报出该来源的文件数 / 页数');
const tipLabel = cfg.options.plugins.tooltip.callbacks.label(
  { dataIndex: idx, label: labels[idx], parsed: data[idx] });
check('外部监视的 tooltip 带「文件 4」与「4页」',
      tipLabel.indexOf('文件 4') > 0 && tipLabel.indexOf('4页') > 0, tipLabel);

section('④ 中心「总订单」把外部监视算进去（页面模板里的表达式）');
function lineWith(marker) {
  return src.split('\n').find(function (l) { return l.indexOf(marker) >= 0; }) || '';
}
check('★ 中心总数含 rs.external_orders（否则 449 ≠ 各扇区之和）',
      lineWith('cloud-chart-source').indexOf('(rs.external_orders || 0)') > 0,
      lineWith('cloud-chart-source').slice(0, 100) + '…');

section('⑤ 订单构成的图例（现场：外部监视 1 单 = 0.22%，扇形画不出来）');
const LEGEND_RS = {
  wechat_orders: 71, app_orders: 5, local_orders: 372, external_orders: 1,
  wechat_files: 90, app_files: 6, local_files: 400, external_files: 1,
  wechat_pages: 120, app_pages: 8, local_pages: 500, external_pages: 1
};
const legend = legends(null)._sourceLegendHTML(LEGEND_RS);
check('★ 图例列出「外部监视 1单」',
      legend.indexOf('外部监视 <b class="dl-num">1</b>单') > 0, legend.slice(0, 240));
check('★ 四个来源都在图例里（一个都不许静默消失）',
      ['小程序', 'APP', '本地', '外部监视'].every(function (n) { return legend.indexOf(n) > 0; }),
      legend.slice(0, 240));
check('图例色块与扇区同色（外部监视 #0d9488）', legend.indexOf('background:#0d9488') > 0);
check('图例是 .donut-legend 容器（样式挂在它上面）', legend.indexOf('class="donut-legend"') > 0);
check('0 单的来源不占位置（只列有单的）', legend.indexOf('<b class="dl-num">0</b>') < 0);

section('⑥ 另外两张图的图例（用户要求三个都同步）');
const L = legends(null);
const statusLegend = L._statusLegendHTML({ sent: 410, canceled: 10, failed: 2, abandoned: 1, rejected: 0 });
check('★ 订单状态分布：列出全部有单状态与单数',
      statusLegend.indexOf('已完成 <b class="dl-num">410</b>单') > 0
      && statusLegend.indexOf('已取消 <b class="dl-num">10</b>单') > 0
      && statusLegend.indexOf('失败 <b class="dl-num">2</b>单') > 0
      && statusLegend.indexOf('放弃打印 <b class="dl-num">1</b>单') > 0, statusLegend.slice(0, 260));
check('★ 状态图例按单数降序（与环形图同一排序，否则顺序对不上扇区）',
      statusLegend.indexOf('已完成') < statusLegend.indexOf('已取消')
      && statusLegend.indexOf('已取消') < statusLegend.indexOf('失败')
      && statusLegend.indexOf('失败') < statusLegend.indexOf('放弃打印'));
check('0 单的状态不占位置（被打回 0 单）', statusLegend.indexOf('被打回') < 0, statusLegend.slice(0, 120));
check('状态图例色块来自 _statusColor（与扇区同色）', statusLegend.indexOf('background:#9ca3af') > 0);

const selfLegend = L._selfRatioLegendHTML(35, 450);
check('★ 免费打印占比：免费打印 35单 + 实际订单 415单',
      selfLegend.indexOf('免费打印 <b class="dl-num">35</b>单') > 0
      && selfLegend.indexOf('实际订单 <b class="dl-num">415</b>单') > 0, selfLegend.slice(0, 200));
check('两段颜色与扇区一致（#007AFF / #FF9500）',
      selfLegend.indexOf('background:#007AFF') > 0 && selfLegend.indexOf('background:#FF9500') > 0);
check('免费打印为 0 时只列实际订单',
      L._selfRatioLegendHTML(0, 10).indexOf('免费打印') < 0
      && L._selfRatioLegendHTML(0, 10).indexOf('实际订单') > 0);
check('三段图例共用同一个实现（_donutLegendHTML）',
      extract('_sourceLegendHTML').indexOf('_donutLegendHTML') > 0
      && extract('_statusLegendHTML').indexOf('_donutLegendHTML') > 0
      && extract('_selfRatioLegendHTML').indexOf('_donutLegendHTML') > 0);

section('⑦ 段落接线 + 图例必须在 section 里面（不是拼在后面的孤儿节点）');
const SEC = [
  { marker: 'cloud-chart-status', legend: '_statusLegendHTML(st)' },
  { marker: 'cloud-chart-source', legend: '_sourceLegendHTML(rs)' },
  { marker: 'cloud-chart-selfratio', legend: '_selfRatioLegendHTML(fagg.selfOrders || 0, fagg.orders || 0)' },
];
SEC.forEach(function (s) {
  const line = lineWith(s.marker);
  const opens = (line.match(/<div/g) || []).length;
  const closes = (line.match(/<\/div>/g) || []).length;
  const at = line.indexOf(s.legend);
  const lastClose = line.lastIndexOf('</div>');
  const delta = opens - closes;
  // 两种合法形状：
  //   delta 0 → 本行自己收掉 section ⇒ 图例必须出现在**最后一个 </div> 之前**；
  //   delta 1 → section 还没收（下一行 h += '</div>' 收）⇒ 图例天然在 section 内，
  //             但必须确认下一行确实在收尾，否则图例就跑到 section 外成了孤儿节点。
  const nextLine = src.split('\n')[src.split('\n').indexOf(line) + 1] || '';
  const insideOk = delta === 0
    ? (at > 0 && at < lastClose)
    : (delta === 1 && /^\s*h \+= '<\/div>';/.test(nextLine));
  check('★ ' + s.marker + ' 挂着 ' + s.legend, at > 0, line.slice(-80));
  check('  …且图例在 section 内部（不是拼在 section 后面的孤儿节点）', insideOk,
        'delta=' + delta + ' legendAt=' + at + ' lastClose=' + lastClose + ' next=' + JSON.stringify(nextLine.trim().slice(0, 30)));
});
check('★ 三个图例共用 .donut-legend 样式（页面里只有一份定义）',
      (src.match(/\.donut-legend \{/g) || []).length === 1
      && src.indexOf('.donut-legend .dl-dot') > 0 && src.indexOf('.donut-legend .dl-num') > 0);

section('⑧ 判据自证（改回去必须变红）');
function mutAggWhitelist(s) {
  return s.replace(
    "if (src !== 'wechat' && src !== 'app' && src !== 'local' && src !== 'external') src = 'wechat';",
    "if (src !== 'wechat' && src !== 'app' && src !== 'local') src = 'wechat';");
}
const rsOld = aggOf(mutAggWhitelist);
check('B1 白名单退回三来源（外部折成小程序）→ ① 判据变红',
      rsOld.external_orders === 0 && rsOld.wechat_orders === 3,
      'external=' + rsOld.external_orders + ' wechat=' + rsOld.wechat_orders);

function mutSegsRemove(s) {
  return s.replace(/\s*\{ key: 'external'[^}]*\},?/, '');
}
const cfgOld = paint(mutSegsRemove);
const labelsOld = (cfgOld && cfgOld.data && cfgOld.data.labels) || [];
check('B2 去掉 external 扇区 → ② 判据变红',
      labelsOld.filter(function (l) { return l.indexOf('外部监视') === 0; }).length === 0,
      JSON.stringify(labelsOld));

check('B3 中心总数去掉 external → ④ 判据变红',
      lineWith('cloud-chart-source').indexOf('(rs.external_orders || 0)') > 0
      && (function () {
        const l = lineWith('cloud-chart-source');
        return l.replace(' + (rs.external_orders || 0)', '').indexOf('rs.external_orders') < 0;
      })());

check('B4 图例函数被抽空 → ⑤ 判据变红',
      legends(function (s) { return s.replace(/return parts\.length[\s\S]*?;/, "return '';"); })
        ._sourceLegendHTML(LEGEND_RS).indexOf('外部监视') < 0);

check('B5 tooltip 退回 segs[c.dataIndex]（无单来源会错位）→ ③ 判据变红',
      (function () {
        const c = paint(function (s) {
          return s.replace('var k = keys[c.dataIndex];', 'var k = _SOURCE_SEGS[c.dataIndex].key;');
        }, { wechat_orders: 3, app_orders: 0, local_orders: 2, external_orders: 1,
             wechat_files: 3, app_files: 0, local_files: 2, external_files: 4,
             wechat_pages: 3, app_pages: 0, local_pages: 2, external_pages: 9 });
        const ls = c.data.labels;
        return c.options.plugins.tooltip.callbacks.label({ dataIndex: 2, label: ls[2], parsed: 1 })
          .indexOf('文件 4') < 0;
      })());

const BAD_LEGEND_LINE = '<div class="section"><div class="chart-wrap"></div></div> _statusLegendHTML(st)';
function legendInsideSection(line, call) {
  const at = line.indexOf(call);
  return at > 0 && at < line.lastIndexOf('</div>');
}
check('B6 图例挪到 section 外面（拼在最后一个 </div> 之后）→ ⑦ 判据变红',
      legendInsideSection(lineWith('cloud-chart-status'), '_statusLegendHTML(st)')
      && !legendInsideSection(BAD_LEGEND_LINE, '_statusLegendHTML(st)'));

const ascLegend = legends(function (x) { return x.replace('src[b] - src[a]', 'src[a] - src[b]'); })
  ._statusLegendHTML({ sent: 410, canceled: 10, failed: 2, abandoned: 1 });
check('B7 状态图例改成升序（与扇区顺序错开）→ ⑥ 判据变红',
      ascLegend.indexOf('已完成') > ascLegend.indexOf('已取消'), ascLegend.slice(0, 140));

check('B8 免费打印图例把差额算错（不减去免费单）→ ⑥ 判据变红',
      legends(function (x) { return x.replace('Math.max(0, total - self)', 'total'); })
        ._selfRatioLegendHTML(35, 450).indexOf('实际订单 <b class="dl-num">415</b>单') < 0);

console.log('\n' + '='.repeat(56));
console.log('环形图（外部监视 + 三图图例）验收:', okAll ? '全部通过 ✅' : '存在失败 ❌');
process.exit(okAll ? 0 : 1);
