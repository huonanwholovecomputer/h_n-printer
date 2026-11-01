# -*- coding: utf-8 -*-
"""验收 · v7.12「订单 → 打印机」按本地监视的队列名归属（2026-11-01 用户需求）

用户原话：「本地监视监视不到使用了哪台打印机吗？监视不到的话就直接默认是本机使用最多的
打印机吧」（附结算页「订单 → 打印机」表截图：外部监视单 EXT-… 的打印机列是黄字「未识别」）。

事实澄清（这条验收盯的就是它）：Windows spooler 的 EnumJobs 里 pPrinterName 就是打印队列名，
print_watch 一直在记（账本 external_prints.json 的 printer 字段）—— 之前认不出机型，是因为
stats_server 没把这个字段放进订单行、结算页的归属阶梯也就无从比对，最后只剩「设备绑定 /
已知机型 / 单机兜底」，两台机在役时就全部落到「未识别」。

判据五组：
  A. 后端（stats_server）：订单行带 printer_name（原样队列名）；顶层 default_printer
     = 本机账本里页数最多的那台（按页数加权、跳过空队列名、跳过虚拟打印机、只算已处理）。
  B. 结算页静态接线：兜底机型变量来自 /api/local/external_prints；归属阶梯里队列名优先于
     「设备所有者 → 已知机型」；外部单的行键安全派生（以前 raw id 含冒号 ⇒ safeId 拒绝 ⇒
     解锁态也拿不到下拉，人工指定对它根本不可用）。
  C. Node VM 跑**真实的** printerResolutionForOrder / printerNameForOrder / printerPickerHTML：
     · 队列名精确命中 / 带 "(Copy 1)" 后缀的宽松命中；
     · 没读到队列名 ⇒ 归 default_printer，并标「推断」；
     · 队列名对不上台账任何一台（另一台物理机）⇒ 不归属、不冒充、照实显示队列名 + 未登记；
     · 云订单「接单设备绑定」优先级不变（零回归）；
     · 人工指定（含外部单派生键）优先级最高；
     · 外部单解锁态真的有下拉（不再是「这张单没有订单号，无法人工指定机型」的哑标记）。
  D. 变异自证：把「队列名优先」「兜底机型」「外部行安全键」任一处改坏，对应断言必须转红。
  E. 文案：新增 UI 文案里不许出现 markdown 星号（硬规矩 8）。

用法：python tests/verify_order_printer_from_watch.py   （exit 0 = 全部通过）
"""
import io
import json
import os
import re
import subprocess
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL_TOOL = os.path.join(REPO, "local_print_tool")
sys.path.insert(0, LOCAL_TOOL)

import print_watch as pw            # noqa: E402
import stats_server as ss           # noqa: E402

HTML = os.path.join(LOCAL_TOOL, "finance", "settlement.html")
src = open(HTML, encoding="utf-8").read()

ok_all = True
PASSED = 0
FAILED = 0


def check(name, cond, detail=""):
    global ok_all, PASSED, FAILED
    if cond:
        PASSED += 1
    else:
        ok_all = False
        FAILED += 1
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))
    return cond


def section(title):
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


def ledger_item(job_id=1, printer="HP LaserJet 1020", pages=3, duplex=pw.DUPLEX_ON,
                status=pw.STATUS_CONFIRMED, virtual=False, detected_at="2026-11-01 18:58:44"):
    """造一条账本记录（用账本的真实数据模型 ExternalPrint，不另写假对象）。"""
    return pw.ExternalPrint(job_id=job_id, printer=printer, document="测试.pdf", user="顾客",
                            pages=pages, pages_source=pw.SOURCE_CONFIRMED, duplex=duplex,
                            color=pw.COLOR_MONO, copies=1, size_bytes=1024,
                            detected_at=detected_at, printer_virtual=bool(virtual),
                            status=status)


# ══════════════════════════════════════════════════════════════════
section("A. 后端：订单行带队列名 + 兜底机型（stats_server.most_used_printer）")
# ══════════════════════════════════════════════════════════════════
check("A1 订单行的 printer_name 就是账本里的队列名（原样，不做映射）",
      ss._external_order_row(ledger_item(7, printer="HP LaserJet 1020", pages=3), 0.4, "张三")
      .get("printer_name") == "HP LaserJet 1020", "")
check("A2 账本没记队列名 ⇒ printer_name 是空字符串（不是 None，前端按 '' 判空）",
      ss._external_order_row(ledger_item(8, printer=""), 0.4, "张三").get("printer_name") == "", "")
_py = open(os.path.join(LOCAL_TOOL, "stats_server.py"), encoding="utf-8").read()
check("A3 静态：build_external_prints_payload 用 most_used_printer(items) 填 default_printer",
      "def most_used_printer(items) -> dict:" in _py
      and '"default_printer": most_used_printer(items),' in _py, "")

_mixed = [ledger_item(11, printer="HP LaserJet 1020", pages=20),
          ledger_item(12, printer="HP LaserJet 1020", pages=5),
          ledger_item(13, printer="HP LaserJet MFP M232-M237", pages=9),
          ledger_item(14, printer="", pages=50),                     # 空队列名不投票
          ledger_item(15, printer="Microsoft Print to PDF", pages=99, virtual=True),  # 虚拟机不投票
          ledger_item(16, printer="HP LaserJet 1020", pages=99, status=pw.STATUS_NEW)]  # 非已处理不投票
_dflt = ss.most_used_printer(_mixed)
check("A4 按页数加权：1020 合计 25 页 > M232 的 9 页 ⇒ 兜底机型是 1020",
      _dflt == {"name": "HP LaserJet 1020", "pages": 25, "orders": 2}, json.dumps(_dflt, ensure_ascii=False))
check("A5 空队列名 / 虚拟打印机 / 未确认记录都不投票（不然 50 页的 PDF 会顶掉真机）",
      _dflt.get("pages") == 25 and _dflt.get("orders") == 2, json.dumps(_dflt, ensure_ascii=False))
check("A6 账本为空 ⇒ 空字典（没有可兜底的机型时前端不硬凑）",
      ss.most_used_printer([]) == {} and ss.most_used_printer(None) == {}, "")
_tie = [ledger_item(21, printer="A", pages=10), ledger_item(22, printer="B", pages=10),
        ledger_item(23, printer="B", pages=0)]
check("A7 页数打平 → 订单多的优先（B 两单 > A 一单）",
      ss.most_used_printer(_tie)["name"] == "B", json.dumps(ss.most_used_printer(_tie), ensure_ascii=False))
_payload = ss.build_external_prints_payload(_mixed, 0.3, 0.4, "张三")
_expect_rows = sorted(it.printer for it in _mixed if it.status == pw.STATUS_CONFIRMED)
check("A8 响应体的 default_printer 只喂兜底、不改明细：每行带的还是它自己的队列名",
      _payload["default_printer"]["name"] == "HP LaserJet 1020"
      and sorted(o["printer_name"] for o in _payload["orders"]) == _expect_rows,
      json.dumps([o["printer_name"] for o in _payload["orders"]], ensure_ascii=False))

# ══════════════════════════════════════════════════════════════════
section("B. 结算页静态接线：兜底机型来源 / 归属阶梯 / 外部单行键")
# ══════════════════════════════════════════════════════════════════


def js_def(name):
    """按函数名切片（花括号配平），只取满足 `function name(` 的那一份。"""
    at = src.find("function " + name + "(")
    if at < 0:
        return ""
    i = src.find("{", at)
    depth = 0
    for j in range(i, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if not depth:
                return src[at:j + 1]
    return ""


def strip_js_comments(s):
    """剥掉 JS 注释（块注释里的 `*` 不是 UI 文案；判定只针对真正会渲染出去的字符串）。"""
    s = re.sub(r"/\*.*?\*/", " ", s, flags=re.S)
    return re.sub(r"(?<!:)//[^\n]*", " ", s)


_fetch = js_def("_fetchExternalOrders")
_resolve = js_def("printerResolutionForOrder")
_picker = js_def("printerPickerHTML")
_reason = js_def("_printerReasonText")

check("B1 兜底机型变量存在且初始为空（后端没给就不兜底）",
      "var _externalDefaultPrinter = '';" in src, "")
check("B2 /api/local/external_prints 的 default_printer 灌进 _externalDefaultPrinter",
      "_externalDefaultPrinter = (dp && dp.name) ? String(dp.name) : '';" in _fetch
      and "data.default_printer" in _fetch, "")
check("B3 队列名匹配器：归一化（干掉 (Copy N) 后缀）+ 精确后再宽松包含",
      "function _queueKeyOf(s) {" in src and "function printerByQueueName(qname) {" in src
      and "replace(/\\((?:copy|副本)\\s*\\d*\\)/ig, ' ')" in src
      and "nm.length >= 8 && q.length >= 8" in src, "")
check("B4 归属阶梯只有一份实现：printerForOrder 只是取 .printer",
      "function printerResolutionForOrder(o) {" in src
      and "function printerForOrder(o) { return printerResolutionForOrder(o).printer; }" in src, "")
check("B5 队列名优先于「设备所有者 → 已知机型」（一台设备接两台机时，队列名才是那台机）",
      _resolve.find("printerByQueueName(qname)") > 0
      and _resolve.find("printerByQueueName(qname)") < _resolve.find("KNOWN_PRINTER_MODELS[owner]"),
      "queue@%d owner@%d" % (_resolve.find("printerByQueueName(qname)"),
                             _resolve.find("KNOWN_PRINTER_MODELS[owner]")))
check("B6 兜底机型只在「连队列名都没有」时才用（队列名对不上台账 ⇒ 说明是另一台机，不冒充）",
      "if (o && o.external && !qname) {" in _resolve, "")
check("B7 外部单的行键安全派生（raw id 含冒号 ⇒ safeId 会拒 ⇒ 人工指定对它不可用）",
      "function _orderKeyOf(o) {" in src and "var oid = _orderKeyOf(o);" in _picker
      and "safeId(String((o && (o.order_id != null ? o.order_id : o.id)) || ''))" not in _picker, "")
check("B8 锁定态：推断出来的归属标「推断」并说明来源（折旧摊给谁取决于它）",
      "'推断'" in _picker and "_printerReasonText(res.reason" in _picker, "")
check("B9 表尾脚注写清「本地监视队列名 → 默认设备 → 已知机型」的优先级",
      "「本地监视的打印队列名」→「默认设备」→ 已知机型表兜底" in src, "")
check("B10 新增文案里没有 markdown 星号（硬规矩 8）",
      "*" not in strip_js_comments(_reason) and "*" not in strip_js_comments(_picker)
      and "*" not in strip_js_comments(_fetch), "")

# ══════════════════════════════════════════════════════════════════
section("C. Node VM：真实的归属阶梯 / 展示名 / 打印机下拉")
# ══════════════════════════════════════════════════════════════════

HARNESS = r"""
const fs = require('fs'), vm = require('vm');
const htmlPath = process.argv[2];
const html = fs.readFileSync(htmlPath, 'utf8');
const js = [...html.matchAll(/<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/g)].map(m => m[1]).join('\n;\n');
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
function varOf(name) {
  const m = js.match(new RegExp('var ' + name + ' = ([^;]+);'));
  return m ? m[1] : '';
}
const P1 = { id: 'p1', name: 'HP LaserJet 1020', model: 'HP LaserJet 1020', status: 'active',
             price: 1200, life_pages: 3000, client_id: 'R7000-AHP10-168dbdc3d1' };
const P2 = { id: 'p2', name: 'HP LaserJet MFP M232-M237', model: 'HP LaserJet MFP M232-M237',
             status: 'active', price: 1500, life_pages: 4000, client_id: 'DESKTOP-EGEB1V-a6c1a1365e' };
function sandbox(printers, unlockedFlag) {
  const sb = {
    console: { log() {}, warn() {}, error() {} },
    Math, JSON, Object, Array, String, Number, Boolean, Date, RegExp, Error,
    STORE: { config: { printers: printers, orderPrinterMap: {} }, records: {} },
    _authDevices: [{ client_id: 'R7000-AHP10-168dbdc3d1', owner_name: '霍楠' },
                   { client_id: 'DESKTOP-EGEB1V-a6c1a1365e', owner_name: '姚懿祥' },
                   { client_id: 'UNBOUND-001', owner_name: '霍楠' }],   // 没绑打印机，只靠所有者推断
    unlocked: { orderprinter: !!unlockedFlag },
    MODULE_NAMES: { orderprinter: '打印机台账/归属' },
  };
  vm.createContext(sb);
  let snip = '';
  ['getPrinters', 'getActivePrinters', 'printerById', 'printerByClient', 'safeId', 'esc',
   '_deviceOwnerOfOrder', '_queueKeyOf', 'printerByQueueName', '_orderKeyOf',
   'printerResolutionForOrder', 'printerForOrder', '_printerReasonIsGuess',
   '_printerReasonText', 'printerNameForOrder', 'printerPickerHTML'
  ].forEach(n => { snip += '\n' + defOf(n); });
  snip += '\nvar KNOWN_PRINTER_MODELS = ' + varOf('KNOWN_PRINTER_MODELS') + ';';
  snip += '\nvar _externalDefaultPrinter = ' + varOf('_externalDefaultPrinter') + ';';
  vm.runInContext(snip, sb);
  return sb;
}
const EXT_ID = 'ext:15:2026-11-01 18:58:44';
const base = { order_id: EXT_ID, order_number: 'EXT-20261101-185844-15', source: 'external',
               external: true, created_at: '2026-11-01 18:58:44' };
const out = {};
function res(sb, o) {
  const r = sb.printerResolutionForOrder(o);
  const info = sb.printerNameForOrder(o);
  return { id: r.printer ? r.printer.id : '', name: r.printer ? r.printer.name : '',
           reason: r.reason, info: info };
}
const sb = sandbox([P1, P2], false);
sb._externalDefaultPrinter = 'HP LaserJet MFP M232-M237';   // 后端 default_printer = M232
out.decl_default = varOf('_externalDefaultPrinter');
out.queue_exact = res(sb, Object.assign({}, base, { printer_name: 'HP LaserJet 1020' }));
out.queue_suffix = res(sb, Object.assign({}, base, { printer_name: 'HP LaserJet 1020 (Copy 1)' }));
out.queue_mfp = res(sb, Object.assign({}, base, { printer_name: 'HP LaserJet MFP M232-M237' }));
out.no_name = res(sb, Object.assign({}, base, {}));
out.unregistered = res(sb, Object.assign({}, base, { printer_name: 'Brother HL-1110' }));
out.cloud_client = res(sb, { order_id: '20260101-0001', received_client: 'DESKTOP-EGEB1V-a6c1a1365e' });
out.cloud_owner = res(sb, { order_id: '20260101-0002', received_client: 'UNBOUND-001' });
out.ext_key = sb._orderKeyOf(base);
sb.STORE.config.orderPrinterMap[out.ext_key] = 'p1';
out.manual_wins = res(sb, Object.assign({}, base, { printer_name: 'HP LaserJet MFP M232-M237' }));
out.locked_queue = sb.printerPickerHTML(Object.assign({}, base, { order_id: 'ext:91:2026-11-01 10:00:00',
  printer_name: 'HP LaserJet 1020' }), { name: 'HP LaserJet 1020', registered: true });
out.locked_guess = sb.printerPickerHTML(Object.assign({}, base, { order_id: 'ext:92:2026-11-01 10:00:00' }),
  { name: 'HP LaserJet MFP M232-M237', registered: true });
out.locked_unreg = sb.printerPickerHTML(Object.assign({}, base, { order_id: 'ext:93:2026-11-01 10:00:00',
  printer_name: 'Brother HL-1110' }), { name: 'Brother HL-1110', registered: false });
sb.unlocked.orderprinter = true;
out.unlocked_ext = sb.printerPickerHTML(Object.assign({}, base, { order_id: 'ext:94:2026-11-01 10:00:00',
  printer_name: 'HP LaserJet 1020' }), { name: 'HP LaserJet 1020', registered: true });
/* 单机店铺：只有一台在役机时的两个方向 */
const sb1 = sandbox([P1], false);
sb1._externalDefaultPrinter = '';
out.single_noname = res(sb1, Object.assign({}, base, { order_id: 'ext:81:2026-11-01 10:00:00' }));
out.single_unknown_queue = res(sb1, Object.assign({}, base, { order_id: 'ext:82:2026-11-01 10:00:00',
  printer_name: 'Brother HL-1110' }));
console.log(JSON.stringify(out));
"""


_HARNESS_DIR = tempfile.mkdtemp(prefix="hn_attr_js_")
_HARNESS_PATH = os.path.join(_HARNESS_DIR, "harness.js")
with open(_HARNESS_PATH, "w", encoding="utf-8") as _fh:
    _fh.write(HARNESS)


def run_harness(html_path):
    """跑一遍 Node 沙箱，返回它吐出的 JSON（失败返回 None，由调用方判红）。"""
    p = subprocess.run(["node", _HARNESS_PATH, html_path],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    if p.returncode != 0:
        print("  [node stderr] " + (p.stderr or "")[:800])
        return None
    line = [ln for ln in (p.stdout or "").strip().splitlines() if ln.strip().startswith("{")]
    if not line:
        return None
    try:
        return json.loads(line[-1])
    except Exception as e:
        print("  [node 输出解析失败] %s: %r" % (type(e).__name__, line[-1][:300]))
        return None


facts = run_harness(HTML)
if facts is None:
    check("C0 Node 沙箱跑通（settlement.html 内联 JS 能载入）", False, "node 执行失败")
    facts = {}
else:
    check("C0 Node 沙箱跑通（settlement.html 内联 JS 能载入）", True, "")
    check("C1 兜底机型变量默认是空串（后端没给 ⇒ 前端不硬凑）", facts.get("decl_default") == "''",
          repr(facts.get("decl_default")))

    q = facts.get("queue_exact") or {}
    check("C2 队列名精确命中在役机 ⇒ 归它、reason=queue、不算「推断」",
          q.get("id") == "p1" and q.get("reason") == "queue" and (q.get("info") or {}).get("guessed") is False,
          json.dumps(q, ensure_ascii=False))
    s = facts.get("queue_suffix") or {}
    check("C3 队列名带 (Copy 1) 后缀也能对上（spooler 端口重名的常见形态）",
          s.get("id") == "p1" and s.get("reason") == "queue", json.dumps(s, ensure_ascii=False))
    m = facts.get("queue_mfp") or {}
    check("C4 另一台机的队列名精确命中另一台（两台在役不会串台）",
          m.get("id") == "p2", json.dumps(m, ensure_ascii=False))

    n = facts.get("no_name") or {}
    ni = n.get("info") or {}
    check("C5 没读到队列名 ⇒ 归 default_printer，并标「推断」+ reason=default",
          n.get("id") == "p2" and n.get("reason") == "default" and ni.get("guessed") is True,
          json.dumps(n, ensure_ascii=False))

    u = facts.get("unregistered") or {}
    ui = u.get("info") or {}
    check("C6 队列名对不上台账任何一台 ⇒ 不归属（绝不拿另一台机顶上）",
          u.get("id") == "" and u.get("reason") == "", json.dumps(u, ensure_ascii=False))
    check("C7 但名字照实显示队列名 + registered=false（未登记 ⇒ 不提折旧）",
          ui.get("name") == "Brother HL-1110" and ui.get("registered") is False,
          json.dumps(ui, ensure_ascii=False))

    cc = facts.get("cloud_client") or {}
    co = facts.get("cloud_owner") or {}
    check("C8 云订单零回归：接单设备精确绑定优先（reason=client，不算推断）",
          cc.get("id") == "p2" and cc.get("reason") == "client"
          and (cc.get("info") or {}).get("guessed") is False, json.dumps(cc, ensure_ascii=False))
    check("C9 云订单零回归：只靠设备所有者 → 已知机型（reason=owner，算推断）",
          co.get("id") == "p1" and co.get("reason") == "owner", json.dumps(co, ensure_ascii=False))

    k = facts.get("ext_key") or ""
    mm = facts.get("manual_wins") or {}
    check("C10 外部单的行键只含 [A-Za-z0-9_-]（能进内联事件、能长期存配置）",
          bool(re.fullmatch(r"[A-Za-z0-9_-]+", k)) and k.startswith("ext-"), repr(k))
    check("C11 人工指定优先级最高（连队列名也算不过它）",
          mm.get("id") == "p1" and mm.get("reason") == "manual", json.dumps(mm, ensure_ascii=False))

    lq = facts.get("locked_queue") or ""
    lg = facts.get("locked_guess") or ""
    lu = facts.get("locked_unreg") or ""
    check("C12 锁定态：队列名给出的归属只显示名字、不标「推断」（这是实测值）",
          "HP LaserJet 1020" in lq and "推断" not in lq and "<select" not in lq, lq[:160])
    check("C13 锁定态：兜底机型标「推断」并写清来源（title 里带兜底说明）",
          "推断" in lg and "兜底" in lg and "M232" in lg, lg[:220])
    check("C14 锁定态：未登记的队列名标「未登记」（不提折旧）",
          "未登记" in lu and "Brother HL-1110" in lu, lu[:200])

    ue = facts.get("unlocked_ext") or ""
    check("C15 解锁态：外部单真的有下拉（以前 raw id 被 safeId 拒 ⇒ 只剩哑标记）",
          "<select" in ue and 'value="p1"' in ue and "setOrderPrinter('ext-ext-94-2026-11-01-10-00-00'" in ue,
          ue[:260])
    check("C16 下拉里标出当前归属（队列名命中的那台是 selected）",
          'selected>HP LaserJet 1020' in ue, ue[:260])

    s1 = facts.get("single_noname") or {}
    s2 = facts.get("single_unknown_queue") or {}
    check("C17 单机店铺：没读到队列名 ⇒ 归唯一那台（reason=single）",
          s1.get("id") == "p1" and s1.get("reason") == "single", json.dumps(s1, ensure_ascii=False))
    check("C18 单机店铺：队列名说的是**另一台**机 ⇒ 绝不吞给唯一那台（单据照实显示未登记名字）",
          s2.get("id") == "" and (s2.get("info") or {}).get("name") == "Brother HL-1110",
          json.dumps(s2, ensure_ascii=False))

# ══════════════════════════════════════════════════════════════════
section("D. 变异自证：改坏任意一处，对应断言必须转红")
# ══════════════════════════════════════════════════════════════════
MUTATIONS = [
    ("拆掉「队列名优先」这一档",
     "  if (qhit) return { printer: qhit, reason: 'queue' };",
     "",
     ("queue_exact", "reason"), "queue"),
    ("拆掉「兜底机型」这一档",
     "  if (o && o.external && !qname) {",
     "  if (false) {",
     ("no_name", "reason"), "default"),
    ("外部单退回 safeId 行键（人工指定不可用）",
     "  var oid = _orderKeyOf(o);              // 行键：与归属阶梯同一把（外部单在这里也拿得到下拉）",
     "  var oid = safeId(String((o && (o.order_id != null ? o.order_id : o.id)) || ''));",
     ("unlocked_ext", "has_select"), "<select"),
]
_tmp = tempfile.mkdtemp(prefix="hn_attr_mut_")
for label, old, new, (key, field), expect in MUTATIONS:
    if src.count(old) != 1:
        check("D0 变异锚点唯一：" + label, False,
              "锚点出现 %d 次（改了实现的写法就得同步改这条变异）" % src.count(old))
        continue
    path = os.path.join(_tmp, "mut_%d.html" % abs(hash(label)))
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(src.replace(old, new))
    mf = run_harness(path) or {}
    got = mf.get(key) or {}
    if field == "has_select":
        turned_red = "<select" not in str(got)
    else:
        turned_red = got.get(field) != expect
    check("D 变异 → 断言转红：" + label, turned_red,
          json.dumps(got, ensure_ascii=False)[:200] if got else "变异版没跑出结果")

# ══════════════════════════════════════════════════════════════════
print("\n" + "=" * 78)
print("PASS %d / FAIL %d / 共 %d 条" % (PASSED, FAILED, PASSED + FAILED))
print("全部通过 ✅" if ok_all else "有失败 ❌")
print("=" * 78)
sys.exit(0 if ok_all else 1)
