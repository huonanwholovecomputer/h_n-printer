# -*- coding: utf-8 -*-
"""验收 · 收支清算「📐 结算公式」说明块（v7.3，2026-10-25）

被验对象：`local_print_tool/finance/settlement.html` 里「收支清算」标签页新增的可折叠公式块。

判据（三组）：
  A. 静态接线 —— 块在 renderDash 里被插入、位置在「🔄 资金重分配」之前、默认收起、
     就地刷新保留展开态、CSS 用原生 <details> 那套。
  B. 口径纪律 —— `settleFormulaHTML()` **只做展示**：函数体内不得出现第二份计算
     （Math.round / parseCents / apportionCents / calcMonth）。页面数字只有一个来源，
     否则两套口径必然漂移（这正是 v7.2 页数/张数出事的根因）。
  C. 真实渲染（Node VM 里跑真实 renderDash + 真实 calcMonth）——
     · 团队总式的「本月代入」三个数取的都是 calcMonth 的返回值；
     · ΣN / ΣB / ΣC / Π 四处实算与 calcMonth 逐项对得上，且自检标记为「通过」；
     · 公式块的 ΣC 与「💰 实际盈亏」表合计行显示**同一个数**；
     · 零值不渲染成「−¥0.00」/「+¥0.00」。

变异自证（必须能红）：把 `settleFormulaHTML(r, false)` 改成 true、把 ΣN 的累加改坏、
把团队盈亏的正负号改坏 —— 三种改法各有一组断言必须当场转红。

用法：python tests/verify_settle_formula.py
exit 0 = 全部通过。
"""
import io
import os
import re
import subprocess
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HTML = os.path.join(REPO, "local_print_tool", "finance", "settlement.html")
ok_all = True


def check(name, cond, detail=""):
    global ok_all
    if not cond:
        ok_all = False
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


def section(t):
    print("\n=== " + t + " ===")


src = open(HTML, encoding="utf-8").read()

# ══════════════════════════════════════════════════════════════
# A. 静态接线
# ══════════════════════════════════════════════════════════════
section("A. 静态接线（renderDash / 就地刷新 / CSS）")

check("settleFormulaHTML(r, open) 存在（两个入参：数据 + 展开态）",
      "function settleFormulaHTML(r, open) {" in src)

check("renderDash 把块插进「收支清算」（默认收起，套一层 #dashFormula 便于就地刷新）",
      "h += '<div id=\"dashFormula\">' + settleFormulaHTML(r, false) + '</div>';" in src)

i_formula = src.find("h += '<div id=\"dashFormula\">'")
i_redist = src.find("'<div class=\"settle-box\"><h2>🔄 资金重分配")
check("位置：在「🔄 资金重分配」之前（先讲算式，再看转账指令）",
      0 <= i_formula < i_redist, f"formula@{i_formula} redist@{i_redist}")

check("就地刷新：改实际转账后跟着重算，并保留展开态",
      "var f = $('#dashFormula');" in src
      and "document.querySelector('#settleFormula[open]')" in src
      and "f.innerHTML = settleFormulaHTML(r, keepOpen);" in src)

check("用原生 <details> + <summary>（无需 JS 记状态、刷新默认收起、键盘可达）",
      "var h = '<details class=\"formula-box\" id=\"settleFormula\"' + (open ? ' open' : '') + '>';" in src
      and "'<summary>📐 结算公式 <span class=\"hint\">" in src)

check("CSS：折叠箭头 / 隐藏默认三角 / 展开分隔线",
      ".formula-box > summary::-webkit-details-marker { display: none; }" in src
      and ".formula-box[open] > summary::after { content: '收起 ▴'; }" in src
      and ".formula-box[open] > summary { border-bottom: 1px solid var(--border);" in src)

check("关于页版本号与更新日志都有 v7.3 段（版本号跟随最新，v7.3 的历史条目保留）",
      ">v7.9</div>" in src and "v7.3：📐 结算公式说明块" in src)

# ══════════════════════════════════════════════════════════════
# B. 口径纪律：说明块里不许有第二份计算
# ══════════════════════════════════════════════════════════════
section("B. 口径纪律（说明块只做展示，不算第二份账）")

i_fn = src.find("function settleFormulaHTML(r, open) {")
i_fn_end = src.find("\n}", i_fn)
fn_body = src[i_fn:i_fn_end + 2]
check("函数体抽取成功（防止下面的判据空转）", i_fn > 0 and len(fn_body) > 500, f"{len(fn_body)} 字符")

for bad, why in [("Math.round", "自己取整"),
                 ("parseCents(", "自己换算分"),
                 ("calcMonth(", "自己算一遍月结")]:
    check(f"函数体内不出现 {bad}（{why}）", bad not in fn_body)

# 文案里可以「提到」apportionCents()（④ 取整规则要写明这处分摊走哪个函数），
# 但绝不能真的带参数调用它 —— 那才是自己算第二份账。
check("函数体内不调用 apportionCents(…（只允许文案里出现函数名）",
      re.search(r"apportionCents\(\s*[A-Za-z0-9_]", fn_body) is None)

check("金额全部经 RMB()/r.xxx 取用（与表格同源）",
      fn_body.count("RMB(") >= 6 and "r.totalInc" in fn_body and "r.actualPL" in fn_body)

# ══════════════════════════════════════════════════════════════
# C. 真实渲染（Node VM）
# ══════════════════════════════════════════════════════════════
section("C. 真实渲染（真实 renderDash + 真实 calcMonth）")

HARNESS = r"""
const fs = require('fs'), vm = require('vm');
const html = fs.readFileSync(process.argv[2], 'utf8');
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
  documentElement: stub(), head: stub(), body: stub(),
};
const document = proxied(documentProto);
const storage = { getItem: () => null, setItem() {}, removeItem() {} };
const windowProto = {
  matchMedia: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }),
  addEventListener: () => {}, removeEventListener: () => {},
  location: { search: '', href: 'http://localhost/', origin: 'http://localhost' },
  innerWidth: 1400, innerHeight: 900, devicePixelRatio: 1,
  localStorage: storage, sessionStorage: storage, document: document,
};
const sandbox = {
  console, Math, JSON, Object, Array, String, Number, Boolean, Date, RegExp, Error, Promise,
  Set, Map, WeakMap, isNaN, isFinite, parseFloat, parseInt, encodeURIComponent, decodeURIComponent,
  setTimeout, clearTimeout, setInterval, clearInterval,
  requestAnimationFrame: (f) => { try { f(0); } catch (e) {} return 0; },
  cancelAnimationFrame: () => {},
  document, window: proxied(windowProto), localStorage: storage, sessionStorage: storage,
  location: windowProto.location, navigator: { userAgent: 'node', clipboard: {} },
  fetch: () => Promise.resolve({ json: () => Promise.resolve({ success: false }), ok: true }),
  getComputedStyle: () => ({ colorScheme: 'light', getPropertyValue: () => '' }),
  FINANCE_MODE: 'cloud',
};
sandbox.globalThis = sandbox; sandbox.self = sandbox;
vm.createContext(sandbox);
try { vm.runInContext(js, sandbox, { filename: 'settlement.js' }); }
catch (e) { console.log('LOAD_FAIL: ' + e.message); process.exit(3); }

vm.runInContext(`
  STORE = {
    config: { investRate: 0.05, scoreCap: 15, passwords: {}, members: [
      { id: 'ma', name: '成员A', color: '#4472C4' },
      { id: 'mb', name: '成员B', color: '#548235' },
      { id: 'mc', name: '成员C', color: '#ED7D31' } ] },
    records: {
      expenses: [
        { id:'e1', date:'2026-06-03', item:'打印机', amount:1016.10, payer:'ma' },
        { id:'e2', date:'2026-06-05', item:'喷头', amount:109.67, payer:'ma' },
        { id:'e3', date:'2026-06-07', item:'订书机', amount:19.80, payer:'mb' },
        { id:'e4', date:'2026-06-20', item:'A4纸第3批', amount:25.70, payer:'mb' },
        { id:'e5', date:'2026-06-12', item:'A4纸第1批', amount:89.00, payer:'mc' },
        { id:'e6', date:'2026-06-15', item:'A4纸第2批', amount:57.60, payer:'mc' } ],
      income: [
        { id:'i1', date:'2026-06-08', memberId:'ma', amount:101.80 },
        { id:'i2', date:'2026-06-09', memberId:'mb', amount:394.00 },
        { id:'i3', date:'2026-06-11', memberId:'mc', amount:22.00 } ],
      scores: [
        { id:'s1', date:'2026-06-10', memberId:'ma', item:'打印', score:15 },
        { id:'s2', date:'2026-06-10', memberId:'ma', item:'推广', score:15 },
        { id:'s3', date:'2026-06-10', memberId:'ma', item:'值班', score:4 },
        { id:'s4', date:'2026-06-10', memberId:'mb', item:'打印', score:15 },
        { id:'s5', date:'2026-06-10', memberId:'mb', item:'推广', score:15 },
        { id:'s6', date:'2026-06-10', memberId:'mb', item:'值班', score:12 },
        { id:'s7', date:'2026-06-10', memberId:'mc', item:'打印', score:15 },
        { id:'s8', date:'2026-06-10', memberId:'mc', item:'推广', score:9 } ],
      transfers: [ { id:'t1', month:'2026-06', from:'mb', to:'ma', amount:300.00, note:'先转一半' } ]
    },
    notes: {}, meta: { version: 0 }
  };
  currentKey = '2026-06';
  unlocked.transfer = true;
  renderDash();
`, sandbox);

const out = els['#tab-dash'].innerHTML || '';
if (!out) { console.log('RENDER_EMPTY'); process.exit(4); }

/* 用真实 calcMonth 现算一遍「应有的数字」，再回头核对页面口径 */
const R = vm.runInContext(`
  (function () {
    var r = calcMonth(monthFromRecords('2026-06'));
    var sN = 0, sB = 0, sC = 0;
    getMembers().forEach(function (m) {
      sN += r.net[m.id]; sB += r.redist[m.id] - r.perPersonShare[m.id]; sC += r.actualPL[m.id];
    });
    var sg = function (c) { return (c > 0 ? '+' : '') + RMB(c); };
    return { ti: r.totalInc, te: r.totalExp, ip: r.investPool, sp: r.servicePool, dep: r.depTotal || 0,
             sN: sN, sB: sB, sC: sC, pl: r.totalInc - r.totalExp,
             sNtxt: sg(sN), sBtxt: sg(sB), sCtxt: sg(sC), pltxt: sg(r.totalInc - r.totalExp - (r.depTotal || 0)),
             sCcls: (sC >= 0 ? 'pos' : 'neg'),
             teamLine: 'Π = ' + RMB(r.totalInc) + ' − ' + RMB(r.totalExp)
                       + ((r.depTotal || 0) ? ' − ' + RMB(r.depTotal) : '') + ' = ' + RMB(r.investPool)
                       + ' + ' + RMB(r.servicePool) + ' − ' + RMB(r.totalExp)
                       + ((r.depTotal || 0) ? ' − ' + RMB(r.depTotal) : '') + ' = <b>'
                       + sg(r.totalInc - r.totalExp - (r.depTotal || 0)) + '</b>' };
  })()
`, sandbox);

const results = [];
const P = (n, c, d) => results.push([n, !!c, d || '']);

/* —— 结构 —— */
P('公式块已渲染', out.indexOf('id="settleFormula"') >= 0);
P('默认收起（<details> 不带 open）', /<details class="formula-box" id="settleFormula">/.test(out),
  (out.match(/<details class="formula-box"[^>]*>/) || [''])[0]);
P('五个小节齐全', ['① 团队层', '② 成员层', '③ 三条恒等式', '④ 取整规则', '⑤ 页面上每张表']
  .every((t) => out.indexOf(t) >= 0));
P('位置：公式块在「🔄 资金重分配」之前',
  out.indexOf('id="settleFormula"') < out.indexOf('🔄 资金重分配'));
P('成员层 10 步都列了（v7.5 加入损耗费一步）',
  (out.match(/<tr><td>(?:[1-9]|10)<\/td>/g) || []).length === 10,
  String((out.match(/<tr><td>(?:[1-9]|10)<\/td>/g) || []).length));

/* —— 团队总式：三个数都来自 calcMonth —— */
P('团队总式「本月代入」与 calcMonth 完全一致（Σ代收 / 投资池 / 服务池 / 采购）',
  out.indexOf(R.teamLine) >= 0, R.teamLine);
P('团队盈亏 = Σ代收 − Σ采购 − Σ折旧（数值）', R.pl === R.ti - R.te - R.dep, R.pl + ' vs ' + (R.ti - R.te - R.dep));
P('P₁ + P₂ ≡ Σ代收', R.ip + R.sp === R.ti, R.ip + ' + ' + R.sp + ' vs ' + R.ti);

/* —— 恒等式自检 —— */
P('ΣN ≡ 0', R.sN === 0, String(R.sN));
P('ΣB ≡ ΣC', R.sB === R.sC, R.sB + ' vs ' + R.sC);
P('ΣC ≡ 团队盈亏 Π', R.sC === R.pl, R.sC + ' vs ' + R.pl);
P('ΣN 实算显示为 ¥0.00', out.indexOf('ΣN = ¥0.00') >= 0,
  (out.match(/ΣN = [^<]*/) || [''])[0]);
P('ΣB / ΣC / Π 三处实算逐项一致',
  out.indexOf('ΣB = ' + R.sBtxt) >= 0
  && out.indexOf('ΣC = ' + R.sCtxt) >= 0
  && out.indexOf('Π = ' + R.pltxt + '</code>') >= 0,
  R.sBtxt + ' / ' + R.sCtxt + ' / ' + R.pltxt);
P('本月自检标记为「通过」（不是红字）',
  out.indexOf('✅ 自检通过') >= 0 && out.indexOf('❌ 三处对不上') < 0);

/* —— 与「实际盈亏」表同源：同一个数出现在两处 —— */
P('公式块的 ΣC 与「💰 实际盈亏」合计行显示同一个数',
  R.sC !== 0 && out.indexOf('ΣC = ' + R.sCtxt) >= 0
  && out.indexOf('<td class="' + R.sCcls + '">' + R.sCtxt + '</td>') >= 0,
  'ΣC=' + R.sCtxt + ' / <td class="' + R.sCcls + '">');

/* —— 零值不带符号 —— */
const block = out.slice(out.indexOf('id="dashFormula"'), out.indexOf('🔄 资金重分配'));
P('块内不出现 −¥0.00 / +¥0.00（零值不带符号）',
  block.indexOf('−¥0.00') < 0 && block.indexOf('+¥0.00') < 0);
P('块切片抓取成功（判据不空转）', block.length > 1500, String(block.length));

/* —— 展开态：open=true 时带 open 属性 —— */
const opened = vm.runInContext('settleFormulaHTML(calcMonth(monthFromRecords("2026-06")), true)', sandbox);
P('open=true 时渲染出 open 属性（就地刷新可保留展开态）',
  /<details class="formula-box" id="settleFormula" open>/.test(opened));

/* —— 就地刷新接线真的会重写 #dashFormula —— */
vm.runInContext('currentTab = "dash"; _refreshDashSummary();', sandbox);
const refreshed = els['#dashFormula'].innerHTML || '';
P('_refreshDashSummary() 重写 #dashFormula（改转账后公式跟着变）',
  refreshed.indexOf('📐 结算公式') >= 0 && refreshed.indexOf('ΣC = ' + R.sCtxt) >= 0,
  String(refreshed.length) + ' 字符');

let bad = 0;
for (const [n, c, d] of results) {
  if (!c) bad++;
  console.log(`[${c ? 'PASS' : 'FAIL'}] ${n}` + (d ? '  -- ' + d : ''));
}
process.exit(bad ? 1 : 0);
"""


def run_harness(html_path, workdir):
    hp = os.path.join(workdir, "harness.js")
    open(hp, "w", encoding="utf-8").write(HARNESS)
    r = subprocess.run(["node", hp, html_path], capture_output=True, text=True, encoding="utf-8")
    return r.returncode, (r.stdout or "") + (r.stderr or "")


tmpdir = tempfile.mkdtemp(prefix="hn_settle_formula_")
code, out_text = run_harness(HTML, tmpdir)
print(out_text.rstrip())
check("真实渲染：全部断言通过", code == 0, f"exit={code}")

# ── 变异自证：三种改法必须各自把对应断言弄红 ───────────────────
section("D. 变异自证（改坏 → 必须变红，证明上面的绿不是空转）")

MUTANTS = [
    ("默认收起",
     "h += '<div id=\"dashFormula\">' + settleFormulaHTML(r, false) + '</div>';",
     "h += '<div id=\"dashFormula\">' + settleFormulaHTML(r, true) + '</div>';"),
    ("ΣN ≡ 0 与自检",
     "sumNet += r.net[mem.id] || 0;",
     "sumNet += 1;"),
    ("团队盈亏",
     "var teamPL = r.totalInc - r.totalExp - depTotal;",
     "var teamPL = r.totalInc + r.totalExp;"),
]
for name, old, new in MUTANTS:
    if old not in src:
        check(f"变异 {name}：锚点是否存在（锚点没找到 ⇒ 变异自证空转）", False, old[:40])
        continue
    mp = os.path.join(tmpdir, "mutant_%d.html" % MUTANTS.index((name, old, new)))
    open(mp, "w", encoding="utf-8").write(src.replace(old, new, 1))
    mcode, mout = run_harness(mp, tmpdir)
    reds = [ln for ln in mout.splitlines() if ln.startswith("[FAIL]")]
    check(f"变异「{name}」后必须转红（拿到 {len(reds)} 条 FAIL、exit={mcode}）",
          mcode != 0 and len(reds) >= 1,
          "；".join(x[:70] for x in reds[:2]) or mout[-200:])

import shutil
shutil.rmtree(tmpdir, ignore_errors=True)

print("\n" + "=" * 50)
print("收支清算「📐 结算公式」验收:", "全部通过 ✅" if ok_all else "存在失败 ❌")
sys.exit(0 if ok_all else 1)
