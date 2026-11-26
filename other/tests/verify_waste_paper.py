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
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
HTML = os.path.join(HERE, '..', '..', 'local_print_tool', 'finance', 'settlement.html')

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


def js_func(name, text=None):
    """取某个函数定义的源码切片（按花括号配对；取不到返回 ''）"""
    text = src if text is None else text
    at = text.find('function ' + name + '(')
    if at < 0:
        return ''
    i = text.find('{', at)
    depth = 0
    for j in range(i, len(text)):
        if text[j] == '{':
            depth += 1
        elif text[j] == '}':
            depth -= 1
            if depth == 0:
                return text[at:j + 1]
    return ''


def stats_server_body(func_name='_extract_proxy_body'):
    """本机代理 stats_server.py 里某个函数的源码切片"""
    p = os.path.join(HERE, '..', '..', 'local_print_tool', 'stats_server.py')
    try:
        with open(p, encoding='utf-8') as fh:
            s = fh.read()
    except OSError:
        return ''
    at = s.find('def ' + func_name + '(')
    if at < 0:
        return ''
    nxt = s.find('\ndef ', at + 1)
    return s[at:nxt if nxt > 0 else len(s)]


def proxy_passes_server_version(text=None):
    """本机代理是否把云端回执里的 server_version 透传给页面（v7.32 冲突检测的前提）"""
    body = stats_server_body() if text is None else text
    return 'out["server_version"] = data["server_version"]' in body


def waste_wiring(text=None):
    """v7.32「废纸不再被重画吞掉」的四条接线判据（静态；变异自证也复用它）"""
    text = src if text is None else text
    snap_at = text.find('var _wSnap = _wasteFocusSnapshot();')
    calc_at = text.find('var r = calcMonthWithUsage(currentKey);', snap_at) if snap_at >= 0 else -1
    restore_at = text.find('_wasteFocusRestore(_wSnap);', snap_at) if snap_at >= 0 else -1
    return {
        # ① 输入即落库：oninput + onblur 都在
        'oninput': ('oninput="updWastePaper(' in text
                    and 'onblur="_wasteFlushSoon()"' in text),
        # ② 每格带成员 ID（重画后按 data-mid 找回）
        'data_mid': 'data-mid="\' + esc(mem.id) + \'"' in text and 'class="inp-waste' in text,
        # ③ 重画前先快照（且必须早于 calcMonthWithUsage —— 废纸值影响用量）
        'snapshot_before_calc': snap_at >= 0 and calc_at > snap_at and calc_at - snap_at < 400,
        # ④ 重画后还原焦点
        'restore_after_render': restore_at > snap_at,
        # ⑤ updWastePaper 自己不再同步重画（那正是抢焦点的来源）
        'no_sync_render': 'renderDash' not in js_func('updWastePaper', text),
        # ⑥ 快照里先落库，否则重画一换 DOM 就丢了
        'snapshot_commits': 'var ok = updWastePaper(mid, val, el);' in js_func('_wasteFocusSnapshot', text),
    }

def waste_lock_wiring(text=None):
    """v7.33「废纸格的锁」的五条接线判据（静态；变异自证复用）"""
    text = src if text is None else text
    return {
        # ① 标题右侧那枚 🔒（.st-lock，与订单明细「状态」列同一个控件同一个点击手感）
        'lock_btn': ("<h2>🧻 用纸统计' + _wasteLockHTML() + " in text
                     and "' <button type=\"button\" class=\"st-lock' + (on ? ' on' : '')" in text
                     and 'event.stopPropagation();toggleWasteLock()' in text),
        # ② 锁定态只读，点格子就是解锁入口（用户："提示要解锁，可我没看到在哪解锁"）
        'locked_readonly': ('readonly title="🔒 已锁定：点这里' in text
                            and 'onclick="wasteAskUnlock()"' in text),
        # ③ 写入口自己再查一次锁（绕过 readonly / 旧缓存直接调也不许落库）
        'write_guard': 'if (_wasteLocked()) { wasteAskUnlock(); return false; }' in text,
        # ④ 复用「采购支出」那个密码，不新开密码模块（否则每次加载都弹「首次设置密码」）
        'reuse_expense': "requestPassword('expense', MODULE_NAMES.expense," in text,
        # ⑤ 会话失效时一起回锁；会话恢复（expense 已解锁）时一起放开
        'session_alias': ('unlocked.waste = false;' in text
                          and 'if (unlocked.expense) unlocked.waste = true;' in text),
    }


# ══════════════════════════════════════════════════════════════
# A. 静态接线
# ══════════════════════════════════════════════════════════════
section('A. 静态接线（页面源码里必须存在的实现片段）')

check('有「用纸统计」区块渲染函数 paperStatsSectionHTML',
      'function paperStatsSectionHTML(r, m) {' in src)
check('用纸统计区块标题 = 🧻 用纸统计（v7.33 起标题右侧还挂 _wasteLockHTML() 那枚 🔒）',
      "<h2>🧻 用纸统计" in src and '_wasteLockHTML() + ' in src)
check('每人一格可编辑「废纸」并回调 updWastePaper(memberId, value, el)',
      'oninput="updWastePaper(' in src and "', this.value, this)\" " in src
      and 'onblur="_wasteFlushSoon()"></td>' in src)
check('废纸格带 data-mid（重画后靠它找回同一个成员那一格）',
      'data-mid="\' + esc(mem.id) + \'"' in src and 'class="inp-waste' in src)
check('废纸格失焦后收尾（onblur = _wasteFlushSoon，重算 A4 纸摊销）',
      'onblur="_wasteFlushSoon()"' in src)
check('合计列带就地刷新标记 js-wsum（v7.32：逐格输入时只换这一格，不整页重画抢焦点；'
      '「正常 N + 废 M 张」的文案本身由 W6 实测渲染字符串钉住）',
      'class="js-wsum"' in src and '"js-wover"' in src)
check('脚注带就地刷新标记 js-wmemo / js-wtw / js-wfoot（同上，中文文案交给 W6）',
      'class="js-wmemo"' in src and 'class="js-wtw"' in src and 'class="js-wfoot"' in src)
check('废纸格留空时 placeholder = 预估张数（留空看得到预估）',
      "'\" placeholder=\"' + fmtInt(est) + '\" '" in src)
check('废纸来源标注区分「人工 / 预估」',
      '<span class="tag b">预估</span>' in src and '<span class="tag g">人工</span>' in src)
check('用纸统计有「未归属」行（成员被删 / 订单没绑成员时纸不能凭空消失）',
      ">未归属</td>" in src and 'orphanWaste' in src and 'orphanNormal' in src)
# v7.12：明细区整批折成 secFold 折叠块，原来钉的「h += paperStatsSectionHTML(r, m);」直排行
# 已经不存在了 —— 顺序判据改成看两个折叠块的先后（paper 必须排在 amort 之前）。
check('用纸统计在「长期耗材摊销」之前渲染',
      src.index("h += secFold('paper',") < src.index("h += secFold('amort',"))

# ── v7.33：废纸格的锁（照订单明细「状态」列那把 🔒 做） ──────────────────
_WL = waste_lock_wiring()
check('★ v7.33 ①「用纸统计」标题右侧有 .st-lock 的 🔒/🔓（与订单明细状态列同一个控件、同一句提示结构）',
      _WL['lock_btn'])
check('★ v7.33 ② 锁定态格子 = readonly + 点一下弹密码框（用户："提示要解锁，可我没看到在哪解锁"）',
      _WL['locked_readonly'])
check('★ v7.33 ③ 写入口自己先查锁（绕过 readonly / 控制台直调也不落库）',
      _WL['write_guard'])
check('★ v7.33 ④ 复用「采购支出」密码，不新开密码模块（新模块会让每次加载都弹「首次设置密码」）',
      _WL['reuse_expense'])
check('★ v7.33 ⑤ 会话到期一起回锁；expense 已解锁时会话恢复即一并放开',
      _WL['session_alias'])
check('★ v7.33 ⑥ PASSWORD_KEYS 里没有 waste（后端 FINANCE_MODULES 不认识它 ⇒ 不能当密码模块用）',
      "'waste'" not in re.search(r"var PASSWORD_KEYS = \[([^\]]*)\]", src).group(1))
check('★ v7.33 ⑦ 解锁弹窗支持补充说明行（hint 参数；说明"与采购支出共用密码"）',
      'function requestPassword(key, label, hint) {' in src
      and "requestPassword('expense', MODULE_NAMES.expense," in src)

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
      'mon2[mid] = n;' in js_func('updWastePaper')
      and 'invalidateUsage(currentKey);' in js_func('_wasteFlushSoon'))

# ── v7.32：现场「填了被覆盖 / 没传回云端」的四道闸 ──────────────────────────
# 现场：用纸统计整块由 renderDash() 的 innerHTML 重建，废纸格从前只在 change（失焦）落库。
# 输入到一半时来一次重画（用量取回 / 耗材补取 / 上一次编辑自己触发）就把 input 换掉，
# 浏览器不给被移除的元素补发 change ⇒ 刚敲的数字既没进 STORE 也没 save。
_w = waste_wiring()
check('★ v7.32 ① 废纸格 oninput 即时落库（不再只等失焦）且失焦后收尾重算', _w['oninput'])
check('★ v7.32 ② 每格带 data-mid（重画后按成员找回那一格，不认行下标）', _w['data_mid'])
check('★ v7.32 ③ renderDash 重画前先快照，且早于 calcMonthWithUsage（废纸值影响用量）',
      _w['snapshot_before_calc'])
check('★ v7.32 ④ renderDash 画完按快照还原焦点与光标', _w['restore_after_render'])
check('★ v7.32 ⑤ updWastePaper 自己不再同步 renderDash（那正是抢焦点的来源）', _w['no_sync_render'])
check('★ v7.32 ⑥ 快照里先把没落库的值写进 STORE（重画一换 DOM 就读不到了）', _w['snapshot_commits'])
check('★ v7.32 ⑦ 本机代理透传 server_version（否则前端冲突检测是死代码、整本静默覆盖）',
      proxy_passes_server_version(),
      'stats_server._extract_proxy_body')
check('★ v7.32 ⑧ 没解锁时保存被挡下要出声（不再静默丢写入）',
      '_pendingSaveNotified' in js_func('_doSave') and '需要解锁后才能写入云端' in js_func('_doSave'))
check('migrateConfig 归一 waste_paper（脏数据不变成算不清的纸）',
      'if (!c.waste_paper || typeof c.waste_paper !== \'object\' || Array.isArray(c.waste_paper)) c.waste_paper = {};' in src)
check('mergeStores 合并 waste_paper（月 × 成员并集）',
      'put(cc.waste_paper); put(lc.waste_paper);' in src)
check('版本历史写了 v7.10 废纸口径（标题版本号已改由 APP_VER 渲染，这里钉日志条目）',
      'v7.10：' in src and '废纸' in src)

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
  snippet += "\nvar __TOASTS = [], __SAVES = 0, __INVAL = 0, __RENDERS = 0, __FLUSH = 0;";
  snippet += "\nfunction toast(m, k) { __TOASTS.push([String(m), String(k || '')]); }";
  snippet += "\nfunction save() { __SAVES++; }";
  snippet += "\nfunction invalidateUsage() { __INVAL++; }";
  snippet += "\nfunction renderDash() { __RENDERS++; }";
  /* v7.32：这三个是 DOM 侧的就地刷新 / 焦点保护，Node 里没有 DOM ⇒ 只留接线桩，
     用来断言「updWastePaper 不再同步重画、且编辑后确实请求了推迟重算」。 */
  snippet += "\nfunction _wasteMarkBad() {}";
  snippet += "\nfunction _wasteRefreshRow() {}";
  snippet += "\nfunction _wasteFlushSoon() { __FLUSH++; }";
  /* v7.33：废纸格的锁 —— Node 里没有弹窗，requestPassword 只做计数桩；
     unlocked.waste 默认为真（多数用例测的是"填了会不会存"），锁相关的用例自己翻成 false。 */
  snippet += "\nvar __ASK = 0;";
  snippet += "\nvar unlocked = { waste: true, expense: true };";
  snippet += "\nvar MODULE_NAMES = { expense: '采购支出' };";
  snippet += "\nfunction requestPassword() { __ASK++; return { then: function () {} }; }";
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
    /* v7.11（B1）：正常用量口径收敛进 _orderNormalUsage()，_usageFromOrders 调它；
       它在「数据模型切片」之外，不补进来 Node 侧会以 `_orderNormalUsage is not defined` 崩。 */
    '_orderNormalUsage',
    'wasteManualOf', 'wasteEstimateOf', 'wasteSheetsOf', '_applyWasteToUsage',
    '_wasteLocked', '_wasteLockHTML', 'wasteAskUnlock',
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
  P('W6 废纸格回调带上成员 ID（引号字符串，防注入）与元素本身（标红用）',
    html.indexOf("updWastePaper('ma', this.value, this)") >= 0);
  P('W6 合计行 = 正常 100 + 废 19 张',
    html.indexOf('正常 100 + 废 19 张') >= 0);
  P('W6 未归属行把 7 张孤废纸显式列出来（不静默吞掉）',
    html.indexOf('未归属') >= 0 && html.indexOf('>7 张<') >= 0);
  P('W6 v7.33 解锁态：标题右侧是 🔓，且带 toggleWasteLock 点击（与订单明细同一个 .st-lock）',
    html.indexOf('class="st-lock on"') >= 0 && html.indexOf('🔓') >= 0
    && html.indexOf('toggleWasteLock()') >= 0);
  P('W6 v7.33 解锁态的格子不带 readonly（能直接编辑）',
    html.indexOf('readonly') < 0);

  /* ═════ W6b：v7.33 锁定态 —— 只读 + 点格子就弹密码框，且一个字节都不落库 ═════ */
  sb.unlocked.waste = false;
  sb.__TOASTS.length = 0;
  sb.__ASK = 0;
  const nSaveL = sb.__SAVES;
  const htmlL = sb.paperStatsSectionHTML({ usage: u4, amort: null }, m);
  P('W6b 锁定态：格子是 inp-waste locked + readonly（看得到历史值，改不了）',
    htmlL.indexOf('class="inp-waste locked"') >= 0 && htmlL.indexOf('readonly') >= 0);
  P('W6b 锁定态：点格子 = 解锁入口（onclick=wasteAskUnlock()，这就是"上哪输密码"的答案）',
    htmlL.indexOf('onclick="wasteAskUnlock()"') >= 0);
  P('W6b 锁定态：标题右侧是 🔒 且提示"点击输入密码解锁"',
    htmlL.indexOf('>🔒</button>') >= 0 && htmlL.indexOf('点击输入密码解锁后') >= 0);
  const rLocked = sb.updWastePaper('ma', '9');
  P('W6b 锁定态直接调写入口（在控制台 / 旧缓存里绕过 readonly）：不落库、不保存，只弹解锁框',
    rLocked === false && sb.__SAVES === nSaveL && sb.__ASK === 1
    && (sb.STORE.config.waste_paper['2026-10'] || {}).ma !== 9,
    'ask=' + sb.__ASK + ' saves=' + sb.__SAVES);
  sb.unlocked.waste = true;

  /* ═════ W7：updWastePaper 的输入校验与落库（v7.32：边打边存，不再同步重画） ═════ */
  sb.STORE.config.waste_paper = {};
  sb.__TOASTS.length = 0;
  const nSave0 = sb.__SAVES, nRend0 = sb.__RENDERS;
  const rBad = sb.updWastePaper('ma', 'abc');
  P('W7 文字输入：不落库、不写库、弹错（用户敲的字留在框里，不替他清）',
    rBad === false && sb.__TOASTS.length === 1 && sb.__TOASTS[0][1] === 'error'
    && sb.__SAVES === nSave0 && sb.__RENDERS === nRend0
    && !(sb.STORE.config.waste_paper['2026-10'] || {}).ma,
    JSON.stringify(sb.__TOASTS));
  const rNeg = sb.updWastePaper('ma', '-3');
  P('W7 负数输入：同样不落库、弹错',
    rNeg === false && sb.__TOASTS.length === 2 && sb.__SAVES === nSave0);
  P('W7 v7.32 编辑废纸不再同步 renderDash（重画会抢走正在输入的焦点 ⇒ 值被吞）',
    sb.__RENDERS === nRend0, 'renders=' + sb.__RENDERS);
  sb.updWastePaper('ma', '12');
  P('W7 合法输入：立刻写进 waste_paper[月][成员] 并保存（不等失焦）',
    sb.STORE.config.waste_paper['2026-10'].ma === 12 && sb.__SAVES === nSave0 + 1,
    JSON.stringify(sb.STORE.config.waste_paper));
  const nSave1 = sb.__SAVES;
  sb.updWastePaper('ma', '12');
  P('W7 同值重复提交不重复写库（挪光标 / 前后空格不会把保存打满）', sb.__SAVES === nSave1);
  sb.updWastePaper('ma', '0');
  P('W7 填 0 也会落库（明确没有废纸，区别于留空）',
    sb.STORE.config.waste_paper['2026-10'].ma === 0, JSON.stringify(sb.STORE.config.waste_paper));
  const nSave2 = sb.__SAVES;
  sb.updWastePaper('ma', '  ');
  P('W7 留空 = 删除该格；月份空了连月份键一起删',
    !sb.STORE.config.waste_paper['2026-10'], JSON.stringify(sb.STORE.config.waste_paper));
  P('W7 删除也要真的落盘一次', sb.__SAVES === nSave2 + 1);

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

# ══════════════════════════════════════════════════════════════
# D. 变异自证（v7.32：重画保护 / 代理透传，改坏必须转红）
# ══════════════════════════════════════════════════════════════
section('D. 变异自证（v7.32 重画保护 / 代理透传，改坏必须转红在对应判据上）')

WIRING_MUTANTS = [
    ('重画前不拍快照（回到「输入中被 innerHTML 换掉就丢值」的老路）',
     'var _wSnap = _wasteFocusSnapshot();',
     'var _wSnap = null;',
     'snapshot_before_calc'),
    ('画完不还原焦点（值还在，但光标没了 ⇒ 接着敲就落到 body 上）',
     '_wasteFocusRestore(_wSnap);',
     'void _wSnap;',
     'restore_after_render'),
    ('废纸格退回「只认 onchange」（现场那条路：失焦前被重画就丢）',
     'oninput="updWastePaper(',
     'onchange-only="updWastePaper(',
     'oninput'),
    ('废纸格不带 data-mid（重画后按行下标找，成员一增删就串行）',
     'data-mid="\' + esc(mem.id) + \'"',
     'data-row="\' + esc(mem.id) + \'"',
     'data_mid'),
    ('快照只读不落库（重画一换 DOM，刚敲的字仍然丢）',
     'var ok = updWastePaper(mid, val, el);',
     'var ok = false;',
     'snapshot_commits'),
    ('updWastePaper 里又塞回同步 renderDash（抢走正在输入的焦点）',
     '  _wasteDirty = true;\n  _wasteRefreshRow(mid);\n  return true;\n}',
     '  renderDash();\n  _wasteDirty = true;\n  _wasteRefreshRow(mid);\n  return true;\n}',
     'no_sync_render'),
]

for _idx, (_title, _old, _new, _key) in enumerate(WIRING_MUTANTS, 1):
    if _old not in src:
        check(f'v7.32 变异 {_idx}「{_title}」的锚点仍存在', False,
              '锚点没找到（源码改动后要同步更新本脚本）')
        continue
    _got = waste_wiring(src.replace(_old, _new, 1))
    check(f'v7.32 变异 {_idx}「{_title}」后判据 {_key} 转红', _got.get(_key) is False, str(_got))

_pxy = stats_server_body()
check('v7.32 变异 7：代理白名单删掉 server_version 后判据转红',
      proxy_passes_server_version(_pxy)
      and not proxy_passes_server_version(_pxy.replace('out["server_version"] = data["server_version"]', '', 1)),
      'stats_server._extract_proxy_body')

# ── v7.33：锁的变异自证（每条都要把对应判据打红，否则判据就是摆设） ──────────
LOCK_MUTANTS = [
    ('标题上不再挂 🔒（回到 v7.32：提示要解锁、页面上却没有入口）',
     "<h2>🧻 用纸统计' + _wasteLockHTML() +",
     "<h2>🧻 用纸统计' +",
     'lock_btn'),
    ('锁定态还给 readonly 但不给点击入口（看得见锁、找不到门）',
     'readonly title="🔒 已锁定：点这里（或标题右侧的 🔒）输入密码后可编辑" onclick="wasteAskUnlock()" ',
     'readonly ',
     'locked_readonly'),
    ('写入口不查锁（绕过 readonly 直接调就能改账）',
     'if (_wasteLocked()) { wasteAskUnlock(); return false; }',
     'if (false) { }',
     'write_guard'),
    ('解锁改去要一个不存在的 waste 密码模块（后端不认 ⇒ 解锁永远失败）',
     "requestPassword('expense', MODULE_NAMES.expense,",
     "requestPassword('waste', MODULE_NAMES.expense,",
     'reuse_expense'),
    ('会话恢复时不放开废纸锁（解锁了采购支出，废纸格仍然点不动）',
     'if (unlocked.expense) unlocked.waste = true;',
     'void 0;',
     'session_alias'),
]

section('D. v7.33 变异自证（把锁拆掉，对应判据必须转红）')
for _idx, (_title, _old, _new, _key) in enumerate(LOCK_MUTANTS, 1):
    if _old not in src:
        check(f'v7.33 变异 {_idx}「{_title}」的锚点仍存在', False,
              '锚点没找到（源码改动后要同步更新本脚本）')
        continue
    _got = waste_lock_wiring(src.replace(_old, _new, 1))
    check(f'v7.33 变异 {_idx}「{_title}」后判据 {_key} 转红', _got.get(_key) is False, str(_got))

print('\n' + '=' * 60)
print('废纸（v7.10 需求 B）验收: ' + ('全部通过 ✅' if not FAIL else '存在失败 ❌'))
print('=' * 60)
sys.exit(0 if not FAIL else 1)
