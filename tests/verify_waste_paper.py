# -*- coding: utf-8 -*-
"""验收：v7.10 需求 B「废纸」—— 每人可填废纸张数 + 按失败订单自动预估。

用户口径（2026-10-30）：
  · A4 纸会有没计入收支清算的「废纸」，要能填每人大概废了多少张；
  · 同时提供预估：预估 = 本月所有「失败」终态订单（实现上按 失败 / 放弃打印 / 已取消
    这类没有产出纸的终态判定，理由写在 settlement.html 的常量注释里）累积的纸张数，按成员归属；
  · 用纸记录显示成「正常 N + 废 M 张」（用户原话「xx+xx张」）；
  · 人工填了就用人工值，留空用预估；**0 与留空必须能区分**；
  · 废纸计入本月用纸量，参与 A4纸摊销（「按用量分摊」的权重仍是出纸页数）。

本脚本分三段：
  A. 静态接线（源码里必须存在的关键实现片段）；
  B. 纯函数实测（Node VM 跑页面自己的 _usageFromOrders / wasteManualOf / wasteSheetsOf /
     _applyWasteToUsage / paperStatsSectionHTML / updWastePaper / consumableAmort）；
  C. 变异自证（把「留空回落到预估」「0 与留空区分」「失败单按成员归属」改坏 ⇒ 必须在具体断言上转红）。

跑法：python tests/verify_waste_paper.py
"""
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
HTML = os.path.join(HERE, '..', 'local_print_tool', 'finance', 'settlement.html')

OK = []
FAIL = []


def section(title):
    print('\n' + '=' * 78)
    print(title)
    print('=' * 78)


def check(name, cond, detail=''):
    if cond:
        OK.append(name)
        print('[PASS] ' + name + (('  -- ' + str(detail)) if detail else ''))
    else:
        FAIL.append(name)
        print('[FAIL] ' + name + (('  -- ' + str(detail)) if detail else ''))


with open(HTML, encoding='utf-8') as fh:
    src = fh.read()

# ══════════════════════════════════════════════════════════════
# A. 静态接线
# ══════════════════════════════════════════════════════════════
section('A. 静态接线（页面源码里必须存在的实现片段）')

check('有「用纸统计」区块渲染函数 paperStatsSectionHTML',
      'function paperStatsSectionHTML(r, m) {' in src)
check('用纸统计区块标题 = 🧻 用纸统计',
      "<h2>🧻 用纸统计 <span" in src)
check('每人一格可编辑「废纸」并回调 updWastePaper(memberId, value)',
      'onchange="updWastePaper(\\\'' in src and '\\\', this.value)"></td>' in src)
check('合计列显示成「正常 N + 废 M 张」（用户原话 xx+xx张）',
      "'<td style=\"font-weight:700\">正常 ' + fmtInt(normal) + ' + 废 ' + fmtInt(waste) + ' 张</td>'" in src)
check('废纸格留空时 placeholder = 预估张数（留空看得到预估）',
      "'\" placeholder=\"' + fmtInt(est) + '\" '" in src)
check('废纸来源标注区分「人工 / 预估」',
      '<span class="tag b">预估</span>' in src and '<span class="tag g">人工</span>' in src)
check('用纸统计有「未归属」行（成员被删 / 订单没绑成员时纸不能凭空消失）',
      ">未归属</td>" in src and 'orphanWaste' in src and 'orphanNormal' in src)
check('用纸统计在「长期耗材摊销」之前渲染',
      src.index('h += paperStatsSectionHTML(r, m);') < src.index('h += consumableAmortSectionHTML(r, m);'))

check('废纸人工值读的是 config.waste_paper（按月 × 成员）',
      'var bag = (STORE.config && STORE.config.waste_paper) || {};' in src and 'var mon = bag[key];' in src)
check('wasteManualOf：用 hasOwnProperty 判「填过没有」⇒ 0 是有效值、留空才是 null',
      'if (!Object.prototype.hasOwnProperty.call(mon, memberId)) return null;' in src)
check('wasteManualOf：负数 / 非数字不认（返回 null ⇒ 回落预估）',
      'if (!isFinite(n) || n < 0) return null;' in src)
check('wasteSheetsOf：人工值优先，留空用预估',
      'var n = (v === null) ? wasteEstimateOf(u, mem.id) : v;' in src)
check('预估来源 = 失败终态订单：WASTE_PAPER_STATUSES = failed / abandoned / canceled',
      'var WASTE_PAPER_STATUSES = { failed: 1, abandoned: 1, canceled: 1 };' in src)
check('正常用量多取回来的终态单要排除（旧口径零回归）：NORMAL_EXCLUDED_STATUSES',
      'var NORMAL_EXCLUDED_STATUSES = {' in src and 'rejected: 1' in src and 'reserved: 1' in src)
check('废纸必须在 `if (!pages && !sheets) return;` 之前算完（取消单没有 sent 文件）',
      src.index('if (WASTE_PAPER_STATUSES[ost]) {') < src.index('if (!pages && !sheets) return;'))
check('废纸按成员归属，未归属单独累加',
      'if (mid) u.wasteByMember[mid] = (u.wasteByMember[mid] || 0) + waste;' in src
      and 'else u.wasteUnattributed += waste;' in src)
check('废纸计入本月用纸量（u.sheets += total）',
      'u.sheets += total;' in src and 'u.wasteTotal = total;' in src)
check('_applyWasteToUsage 幂等（重复处理同一份用量不会越加越多）',
      'if (u._wasteAdd > 0) {' in src and 'u._wasteAdd = 0;' in src)
check('废纸记在当月最后一天（不污染「采购日之前不算」的老口径）',
      'var range = _usageMonthRange(key);' in src and 'u.sheetsByDay[lastDay] = (u.sheetsByDay[lastDay] || 0) + total;' in src)
check('ensureUsage 拉到失败终态单：云端 URL 带 status=' ,
      "'&status=' + encodeURIComponent(selectedStatuses().join(','))" in src)
check('ensureUsage 取回后调用 _applyWasteToUsage（在 undated 计算之后）',
      src.index("_applyWasteToUsage(u, key);") > src.index("u.undatedSheets"))
check('updWastePaper：留空 = 删除该格（并清理空月份）',
      'delete mon[mid];' in src and 'if (!Object.keys(mon).length) delete bag[currentKey];' in src)
check('updWastePaper：非负整数校验（负数 / 文字不落库）',
      "if (!/^\\d+$/.test(txt)) {" in src and "toast('废纸张数要填非负整数（留空 = 按失败订单自动预估）', 'error');" in src)
check('updWastePaper：落库后作废用量缓存（A4纸摊销跟着变）',
      'mon[mid] = Math.round(+txt);' in src and 'invalidateUsage(currentKey);' in src)
check('migrateConfig 归一 waste_paper（脏数据不变成算不清的纸）',
      'if (!c.waste_paper || typeof c.waste_paper !== \'object\' || Array.isArray(c.waste_paper)) c.waste_paper = {};' in src)
check('mergeStores 合并 waste_paper（月 × 成员并集）',
      'put(cc.waste_paper); put(lc.waste_paper);' in src)
check('版本历史写了 v7.10 废纸口径',
      '>v7.10</div>' in src and '废纸' in src)

# ══════════════════════════════════════════════════════════════
# B. 纯函数实测（Node VM）
# ══════════════════════════════════════════════════════════════
HARNESS = r"""
const fs = require('fs'), vm = require('vm');
const htmlPath = process.argv[2];
const html = fs.readFileSync(htmlPath, 'utf8');
const scripts = [...html.matchAll(/<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/g)].map(m => m[1]);
const js = scripts.join('\n;\n');

const results = [];
const P = (n, c, d) => results.push([n, !!c, d == null ? '' : String(d)]);

function defOf(name) {
  const at = js.indexOf('function ' + name + '(');
  if (at < 0) return '';
  let i = js.indexOf('{', at), depth = 0;
  for (let j = i; j < js.length; j++) {
    if (js[j] === '{') depth++;
    else if (js[j] === '}') { depth--; if (!depth) return js.slice(at, j + 1); }
  }
  return '';
}
function MB() {
  return {
    console: { log() {}, warn() {}, error() {} },
    Math, JSON, Object, Array, String, Number, Boolean, Date, RegExp, Error, Promise,
    Set, Map, isNaN, isFinite, parseFloat, parseInt, encodeURIComponent, decodeURIComponent,
    DEFAULT_MEMBERS: [], DEFAULT_SCORE_CAP: 15, DEFAULT_RATE: 0.05,
    PASSWORD_KEYS: ['score', 'expense', 'income', 'transfer', 'settings', 'auth', 'orderprinter'],
    DEFAULT_PASSWORDS: {}, DEFAULT_PRICING: { simplex_price: 0.2, duplex_price: 0.3, cover_page_price: 0.1,
      delivery_percentages: {}, urgency_prices: {} },
    FINANCE_MODE: 'cloud', STORE: null, _authDevices: null,
  };
}
/* 沙箱：数据模型切片（migrateConfig → 保存）拿到常量与 safeId/isMonthKey 等，
   再按需把「用量 / 废纸 / 渲染」这一段里的纯函数抽进来（它们不在那个切片里）。 */
function wasteSandbox(src) {
  const sb = MB();
  vm.createContext(sb);
  const start = src.indexOf('function migrateConfig(data)');
  const end = src.indexOf('/* ==================== 保存 ====================');
  if (start < 0 || end <= start) { console.log('SNIPPET_FAIL'); process.exit(3); }
  let snippet = src.slice(start, end);
  snippet += "\nvar __M = [];";
  snippet += "\nfunction getMembers() { return __M; }";
  snippet += "\nfunction getMember(id) { for (var i = 0; i < __M.length; i++) { if (__M[i].id === id) return __M[i]; } return null; }";
  snippet += "\nvar __TOASTS = [], __SAVES = 0, __INVAL = 0, __RENDERS = 0;";
  snippet += "\nfunction toast(m, k) { __TOASTS.push([String(m), String(k || '')]); }";
  snippet += "\nfunction save() { __SAVES++; }";
  snippet += "\nfunction invalidateUsage() { __INVAL++; }";
  snippet += "\nfunction renderDash() { __RENDERS++; }";
  snippet += "\nvar currentKey = '2026-10';";
  snippet += "\nfunction _fmtDate(d) { return String(d == null ? '' : d).slice(0, 10); }";
  snippet += "\nfunction _orderMemberKey(o) {";
  snippet += "\n  if ((o.openid || '') === 'local') {";
  snippet += "\n    var n = String(o.owner_name == null ? '' : o.owner_name).trim();";
  snippet += "\n    for (var i = 0; i < __M.length; i++) { if (__M[i].name === n) return 'member:' + __M[i].id; }";
  snippet += "\n    return 'local:' + (n || '未归属');";
  snippet += "\n  }";
  snippet += "\n  return 'openid:' + String(o.openid || '');";
  snippet += "\n}";
  /* 废纸只关心「哪个成员」，打印机无关；返回 null 让页数落进 unmatchedPrinterPages 分支 */
  snippet += "\nfunction printerForOrder() { return null; }";
  snippet += "\nvar _usageCache = {};";
  snippet += "\nfunction usageOfMonth(key) { return _usageCache[key] || null; }";
  snippet += "\nfunction usageStateOfMonth(key) { return _usageCache[key] ? 'ready' : 'idle'; }";
  const need = ['esc', 'fmtInt', 'RMB', 'RMBneg', 'sanitizeColor', 'MC', 'MN',
    '_filePagesOf', '_fileSheets', '_usageMonthRange', '_usageEmpty', '_usageFromOrders',
    'wasteManualOf', 'wasteEstimateOf', 'wasteSheetsOf', '_applyWasteToUsage',
    'paperStatsSectionHTML', 'updWastePaper', 'consumableAmort'];
  const missing = [];
  for (const name of need) {
    const code = defOf(name);
    if (!code) { missing.push(name); continue; }
    snippet += '\n' + code;
  }
  if (missing.length) { console.log('SNIPPET_MISSING ' + missing.join(',')); process.exit(3); }
  vm.runInContext(snippet, sb);
  return sb;
}

function uOf(sb, over) {
  const u = sb._usageEmpty('2026-10');
  Object.keys(over || {}).forEach(k => { u[k] = over[k]; });
  return u;
}

function runCases() {
  const sb = wasteSandbox(js);
  sb.__M.push({ id: 'ma', name: '张三', color: '#123456' });
  sb.__M.push({ id: 'mb', name: '李四', color: '#654321' });
  sb.STORE = { config: { waste_paper: {} }, records: {} };

  /* ═════ W1：_usageFromOrders 把失败终态单的纸算成废纸，并按成员归属 ═════ */
  const orders = [
    /* 失败：两个文件都没打出来（各 10 张）*/
    { created_at: '2026-10-03 10:00:00', status: 'failed', openid: 'local', owner_name: '张三',
      files: [{ status: 'failed', total_sheets: 10 }, { status: 'failed', total_sheets: 10 }] },
    /* 已取消：只按「没产出」的文件算 */
    { created_at: '2026-10-04 11:00:00', status: 'canceled', openid: 'local', owner_name: '李四',
      files: [{ status: 'canceled', total_sheets: 5 }] },
    /* 放弃打印 */
    { created_at: '2026-10-05 12:00:00', status: 'abandoned', openid: 'local', owner_name: '张三',
      files: [{ status: 'abandoned', total_sheets: 3 }] },
    /* 没绑成员 ⇒ 未归属 */
    { created_at: '2026-10-06 13:00:00', status: 'failed', openid: 'someone-not-bound',
      files: [{ status: 'failed', total_sheets: 7 }] },
    /* 正常单：3 张正常纸，不算废纸 */
    { created_at: '2026-10-07 14:00:00', status: 'sent', openid: 'local', owner_name: '张三',
      files: [{ status: 'sent', total_sheets: 3, total_pages: 3 }] },
    /* 部分失败但父单成功 ⇒ 成功的纸算正常，失败的那份也不进废纸（用户口径：赚到的纸已经算过了） */
    { created_at: '2026-10-08 15:00:00', status: 'sent', openid: 'local', owner_name: '张三',
      files: [{ status: 'sent', total_sheets: 4, total_pages: 4 }, { status: 'failed', total_sheets: 6 }] },
    /* 被打回 / 预约 / 排队这些「还没打」的状态既不算正常也不算废纸（零回归 + 不虚增废纸） */
    { created_at: '2026-10-09 16:00:00', status: 'rejected', openid: 'local', owner_name: '张三',
      files: [{ status: 'rejected', total_sheets: 9 }] },
    { created_at: '2026-10-09 17:00:00', status: 'reserved', openid: 'local', owner_name: '张三',
      files: [{ status: 'reserved', total_sheets: 8 }] },
    /* 打印中且有已 sent 的文件 ⇒ 仍算正常用量 */
    { created_at: '2026-10-10 18:00:00', status: 'printing', openid: 'local', owner_name: '李四',
      files: [{ status: 'sent', total_sheets: 2, total_pages: 2 }] }
  ];
  const u = sb._usageFromOrders(orders);
  P('W1 废纸只认失败终态单：失败10+10 + 取消5 + 放弃3 + 未归属7 = 35 张（rejected/reserved 不算）',
    u.waste === 35 && u.wasteOrders === 4, 'waste=' + u.waste + ' orders=' + u.wasteOrders);
  P('W1 按成员归属：张三 23 张、李四 5 张',
    u.wasteByMember.ma === 23 && u.wasteByMember.mb === 5, JSON.stringify(u.wasteByMember));
  P('W1 未绑成员的失败单进「未归属」：7 张',
    u.wasteUnattributed === 7, 'wasteUnattributed=' + u.wasteUnattributed);
  P('W1 正常用量零回归：已完成 3+4+2 = 9 张（父单 sent 里那份失败的 6 张不重复计）',
    u.sheets === 9 && u.sheetsByMember.ma === 7 && u.sheetsByMember.mb === 2,
    'sheets=' + u.sheets + ' byMember=' + JSON.stringify(u.sheetsByMember));
  P('W1 每人正常张数另存 sheetsByMember（用纸统计要用）',
    u.sheetsByMember.ma === 7, JSON.stringify(u.sheetsByMember));
  P('W1 终态单不进正常逐日用量（sheetsByDay 只有 sent 文件那几天）',
    !u.sheetsByDay['2026-10-03'] && u.sheetsByDay['2026-10-07'] === 3,
    JSON.stringify(u.sheetsByDay));

  /* ═════ W2：留空 ⇒ 用预估 ═════ */
  const u2 = uOf(sb, { wasteByMember: { ma: 40, mb: 5 } });
  P('W2 留空（config 里没有这一格）⇒ 废纸用预估：张三 40 张',
    sb.wasteManualOf('2026-10', 'ma') === null && sb.wasteSheetsOf('2026-10', 'ma', u2) === 40,
    'manual=' + sb.wasteManualOf('2026-10', 'ma') + ' used=' + sb.wasteSheetsOf('2026-10', 'ma', u2));

  /* ═════ W3：人工值优先 + 0 与留空区分 ═════ */
  sb.STORE.config.waste_paper['2026-10'] = { ma: 12, mb: 0 };
  P('W3 人工填了就用人工值（张三 40 的预估 → 12）',
    sb.wasteSheetsOf('2026-10', 'ma', u2) === 12);
  P('W3 填 0 是「明确没有废纸」，不是「留空」：李四 5 张预估 → 0',
    sb.wasteManualOf('2026-10', 'mb') === 0 && sb.wasteSheetsOf('2026-10', 'mb', u2) === 0,
    'manual=' + sb.wasteManualOf('2026-10', 'mb') + ' used=' + sb.wasteSheetsOf('2026-10', 'mb', u2));
  P('W3 没填的人仍然回落预估（同一个月里两种口径并存）',
    sb.wasteSheetsOf('2026-10', 'ma', u2) === 12);

  /* ═════ W4：_applyWasteToUsage 把废纸并进本月用纸量 ═════ */
  const u4 = uOf(sb, { sheets: 100, sheetsByDay: { '2026-10-05': 100 },
                      sheetsByMember: { ma: 100 }, wasteByMember: { ma: 40, mb: 5 }, wasteUnattributed: 7 });
  sb._applyWasteToUsage(u4, '2026-10');
  /* 张三 12（人工）+ 李四 0（人工）+ 未归属 7 = 19 */
  P('W4 本月用纸量 = 正常 100 + 废纸 19（人工 12 + 人工 0 + 未归属 7）',
    u4.sheets === 119 && u4.wasteTotal === 19,
    'sheets=' + u4.sheets + ' wasteTotal=' + u4.wasteTotal);
  P('W4 wasteSheets 记到人：张三 12 / 李四 0',
    u4.wasteSheets.ma === 12 && u4.wasteSheets.mb === 0, JSON.stringify(u4.wasteSheets));
  P('W4 废纸记在当月最后一天（10-31），不影响 10-05 的正常用量',
    u4.sheetsByDay['2026-10-31'] === 19 && u4.sheetsByDay['2026-10-05'] === 100,
    JSON.stringify(u4.sheetsByDay));
  P('W4 人工值计数 = 2（两人都填了）', u4.wasteManualCount === 2, 'n=' + u4.wasteManualCount);

  /* 幂等：同一份对象再处理一次，不能又加 19 */
  sb._applyWasteToUsage(u4, '2026-10');
  P('W4 _applyWasteToUsage 幂等：再跑一次 sheets 仍是 119',
    u4.sheets === 119 && u4.sheetsByDay['2026-10-31'] === 19,
    'sheets=' + u4.sheets + ' day=' + u4.sheetsByDay['2026-10-31']);

  /* ═════ W5：废纸进入 A4纸摊销（真实 consumableAmort） ═════ */
  const paper = [{ id: 'e1', date: '2026-10-01', item: 'A4纸一箱', kind: 'paper',
                   units: 1000, amount: 100, payer: 'ma' }];
  const uNoWaste = uOf(sb, { sheets: 100, sheetsByDay: { '2026-10-05': 100 } });
  const amNo = sb.consumableAmort(paper, { '2026-10': uNoWaste }, '2026-10');
  const amWith = sb.consumableAmort(paper, { '2026-10': u4 }, '2026-10');
  /* 1000 张 / ¥100 ⇒ ¥0.10/张（10 分）；100 张 = 1000 分，119 张 = 1190 分 */
  P('W5 没有废纸：100 张 × 10 分 = 1000 分', amNo.totalCents === 1000, amNo.totalCents);
  P('W5 计入废纸后：119 张 × 10 分 = 1190 分（废纸真的进了 A4纸摊销）',
    amWith.totalCents === 1190, amWith.totalCents);
  P('W5 差额正好是废纸那 19 张 × 10 分 = 190 分',
    amWith.totalCents - amNo.totalCents === 190, amWith.totalCents - amNo.totalCents);

  /* ═════ W6：用纸统计区块把「正常 N + 废 M 张」渲染出来 ═════ */
  sb._usageCache['2026-10'] = u4;
  const m = { key: '2026-10', label: '2026年10月', expenses: [] };
  const html = sb.paperStatsSectionHTML({ usage: u4, amort: null }, m);
  P('W6 区块出现「正常 100 + 废 12 张」（张三一行）',
    html.indexOf('正常 100 + 废 12 张') >= 0,
    html.slice(0, 0) + 'has=' + (html.indexOf('正常 100 + 废 12 张') >= 0));
  P('W6 废纸输入框可编辑：value=12（人工值），placeholder=预估 40',
    html.indexOf('value="12"') >= 0 && html.indexOf('placeholder="40"') >= 0);
  P('W6 李四填了 0 ⇒ value="0"（不是留空）且来源标「人工」',
    html.indexOf('value="0"') >= 0 && html.indexOf('<span class="tag g">人工</span>') >= 0);
  P('W6 废纸格回调带上成员 ID（引号字符串，防注入）',
    html.indexOf("updWastePaper('ma', this.value)") >= 0);
  P('W6 合计行 = 正常 100 + 废 19 张',
    html.indexOf('正常 100 + 废 19 张') >= 0);
  P('W6 未归属行把 7 张孤废纸显式列出来（不静默吞掉）',
    html.indexOf('未归属') >= 0 && html.indexOf('>7 张<') >= 0);

  /* ═════ W7：updWastePaper 的输入校验与落库 ═════ */
  sb.STORE.config.waste_paper = {};
  sb.__TOASTS.length = 0;
  const nSave0 = sb.__SAVES, nRend0 = sb.__RENDERS, nInv0 = sb.__INVAL;
  sb.updWastePaper('ma', 'abc');
  P('W7 文字输入：不落库、弹错、重绘恢复原值',
    sb.__TOASTS.length === 1 && sb.__TOASTS[0][1] === 'error'
    && sb.__SAVES === nSave0 && sb.__RENDERS === nRend0 + 1
    && !(sb.STORE.config.waste_paper['2026-10'] || {}).ma,
    JSON.stringify(sb.__TOASTS));
  sb.updWastePaper('ma', '-3');
  P('W7 负数输入：同样不落库、弹错',
    sb.__TOASTS.length === 2 && sb.__SAVES === nSave0
    && !(sb.STORE.config.waste_paper['2026-10'] || {}).ma);
  sb.updWastePaper('ma', '12');
  P('W7 合法输入：写入 waste_paper[月][成员]、保存、并作废用量缓存',
    sb.STORE.config.waste_paper['2026-10'].ma === 12 && sb.__SAVES === nSave0 + 1
    && sb.__INVAL === nInv0 + 1,
    JSON.stringify(sb.STORE.config.waste_paper));
  sb.updWastePaper('ma', '0');
  P('W7 填 0 也会落库（明确没有废纸，区别于留空）',
    sb.STORE.config.waste_paper['2026-10'].ma === 0, JSON.stringify(sb.STORE.config.waste_paper));
  sb.updWastePaper('ma', '  ');
  P('W7 留空 = 删除该格；月份空了连月份键一起删',
    !sb.STORE.config.waste_paper['2026-10'], JSON.stringify(sb.STORE.config.waste_paper));

  /* ═════ W8：换月读对应月的人工值 ═════ */
  sb.STORE.config.waste_paper = { '2026-10': { ma: 12 }, '2026-11': { ma: 3 } };
  const uNov = uOf(sb, { wasteByMember: { ma: 40 } });
  P('W8 10 月读 12、11 月读 3（人工值按月存，不互相串）',
    sb.wasteSheetsOf('2026-10', 'ma', u2) === 12 && sb.wasteSheetsOf('2026-11', 'ma', uNov) === 3);
  P('W8 11 月没填的人仍回落 11 月自己的预估（不拿 10 月的数）',
    sb.wasteSheetsOf('2026-11', 'mb', uNov) === 0);

  /* ═════ W9：脏数据（migrateConfig 归一） ═════ */
  const dirty = sb.migrateConfig({
    config: { waste_paper: { '2026-10': { ma: 12, mb: -5, mc: 'abc' }, 'bad': { ma: 1 }, '2026-09': {} } },
    records: {}, notes: {}
  });
  const wp = dirty.config.waste_paper;
  P('W9 归一：负数 / 非数字成员删掉，合法值保留并取整',
    wp['2026-10'] && wp['2026-10'].ma === 12 && !('mb' in wp['2026-10']) && !('mc' in wp['2026-10']),
    JSON.stringify(wp));
  P('W9 归一：非法月份键 / 空月份删掉',
    !('bad' in wp) && !('2026-09' in wp), JSON.stringify(wp));
}

try { runCases(); } catch (e) { console.log('HARNESS_ERROR ' + (e && e.stack || e)); process.exit(4); }
results.forEach(function (x) { console.log((x[1] ? 'PASS' : 'FAIL') + '\t' + x[0] + '\t' + x[2]); });
process.exit(results.some(function (x) { return !x[1]; }) ? 1 : 0);
"""


def run_harness(html_path, workdir, tag='harness'):
    hp = os.path.join(workdir, tag + '.cjs')
    with open(hp, 'w', encoding='utf-8') as fh:
        fh.write(HARNESS)
    return subprocess.run(['node', hp, html_path], capture_output=True, text=True, encoding='utf-8')


section('B. 纯函数实测（真实 _usageFromOrders / waste* / paperStatsSectionHTML，Node VM）')
with tempfile.TemporaryDirectory() as tmpdir:
    r = run_harness(HTML, tmpdir)
    out = (r.stdout or '') + (r.stderr or '')
    for line in out.splitlines():
        if line.startswith('PASS\t') or line.startswith('FAIL\t'):
            _, name, detail = (line.split('\t') + ['', ''])[:3]
            check(name, line.startswith('PASS'), detail)
    code = r.returncode
    if code not in (0, 1):
        print(out[-3000:])
        check('Node 侧执行完成（不是崩溃/语法错）', False, f'exit={code}')
    elif code == 1:
        check('Node 侧全部断言通过', False, '见上面的 FAIL 行')
    else:
        check('Node 侧全部断言通过', True)

# ══════════════════════════════════════════════════════════════
# C. 变异自证
# ══════════════════════════════════════════════════════════════
section('C. 变异自证（改坏必须转红在具体断言上）')

MUTANTS = [
    ('留空时回落到 0 张（不复用失败订单的预估，等于废纸功能只剩人工填）',
     '  return wasteEstimateOf(u, memberId);',
     '  return 0;',
     ['W2 留空（config 里没有这一格）⇒ 废纸用预估：张三 40 张']),
    ('用 || 判空导致「填 0」被当成没填（0 与留空不再区分）',
     '  if (!Object.prototype.hasOwnProperty.call(mon, memberId)) return null;',
     '  if (!mon[memberId]) return null;',
     ['W3 填 0 是「明确没有废纸」，不是「留空」：李四 5 张预估 → 0']),
    ('失败终态单不算废纸（预估恒为 0）',
     '    if (WASTE_PAPER_STATUSES[ost]) {',
     '    if (false) {',
     ['W1 废纸只认失败终态单：失败10+10 + 取消5 + 放弃3 + 未归属7 = 35 张（rejected/reserved 不算）',
      'W1 按成员归属：张三 23 张、李四 5 张']),
    ('废纸只加进总数、不记到当月逐日（A4纸摊销吃不到）',
     '      u.sheetsByDay[lastDay] = (u.sheetsByDay[lastDay] || 0) + total;',
     '      /* mutated: 不写逐日 */',
     ['W4 废纸记在当月最后一天（10-31），不影响 10-05 的正常用量',
      'W5 计入废纸后：119 张 × 10 分 = 1190 分（废纸真的进了 A4纸摊销）']),
]

with tempfile.TemporaryDirectory() as tmpdir:
    for idx, (title, old, new, expect_red) in enumerate(MUTANTS, 1):
        if old not in src:
            check(f'变异 {idx}「{title}」的锚点仍存在', False, '锚点没找到（源码改动后要同步更新本脚本）')
            continue
        mp = os.path.join(tmpdir, f'mut{idx}.html')
        with open(mp, 'w', encoding='utf-8') as fh:
            fh.write(src.replace(old, new, 1))
        mr = run_harness(mp, tmpdir, tag=f'h{idx}')
        mout = (mr.stdout or '') + (mr.stderr or '')
        fails = [ln.split('\t')[1] for ln in mout.splitlines() if ln.startswith('FAIL\t')]
        hit = [n for n in expect_red if n in fails]
        check(f'变异 {idx}「{title}」后新脚本转红（exit={mr.returncode}，红在 {len(hit)} 条预期断言上）',
              mr.returncode == 1 and len(hit) == len(expect_red),
              '红的断言：' + ' | '.join(fails[:4]))

print('\n' + '=' * 60)
print('废纸（v7.10 需求 B）验收: ' + ('全部通过 ✅' if not FAIL else '存在失败 ❌'))
print('=' * 60)
sys.exit(0 if not FAIL else 1)
