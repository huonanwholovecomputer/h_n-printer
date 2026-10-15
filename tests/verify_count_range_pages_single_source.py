# -*- coding: utf-8 -*-
"""验收 · 🟡-D5a 小程序「页码范围 → 有效页数」三份口径 → 单一模块导出

现场（D5 第 8 条）：`pages/index/index.js` 有一份权威 `countPagesInRange`（A1 给它补过
区间收窄上限），而 `pages/me/me.js` 与 `pages/user-orders/user-orders.js` 各带一份
**复制粘贴的旧副本** `_countRangePages`，注释还写着"口径对齐后端 _count_pages_in_range"
—— 实测**并不对齐**，四处偏差：

  ① 空白**替换成逗号**（权威实现与后端都是**删除**）⇒ "1 - 3" 变成 "1,-,3"，
     只认第 1、3 两页，而后端按 1-3 三页计费；
  ② `parseInt` 宽松解析（后端 `int()` 严格）⇒ "3a" / "3.5" / "1e2" 前端各认成 1~3 页，
     后端判整段无效 → 退回**全份**计费；
  ③ 段内 `split('-')` **全切**（后端 `split('-', 1)` 只切一刀）⇒ "1-2-3" 前端认成 1-2；
  ④ 区间迭代**不收窄**到 `total` ⇒ "1-999999999" 会真的循环十亿次（A1 只修了 index.js）。

**实测影响面（诚实记录）**：`/api/orders` **恒**回传 `effective_pages`/`sheets`
（`app.py:5880` 两支都填），所以 `_enrichFileStat` 的兜底分支**当前不可达** ⇒ 危害是
**潜伏**的：一旦有别的入口吃不到该字段（或有人把这两个函数再复制到下一个页面），
显示页数/张数就会**静默算错**；而旧副本对超长区间**无收窄**，真被走到就是页面卡死。
故本条按 D5 给的修法办：**删掉两份副本，改由 `utils/pages.js` 统一导出**。

本脚本：
  A. 单一来源（小程序侧该实现**只允许一处**，且必须在 `utils/pages.js`）
  B. 实跑逐组比对（后端 `_count_pages_in_range` vs 模块 / index.js / me.js / user-orders.js）
  C. 超长区间**不得无界循环**（每个目标一个子进程 + 硬超时；旧副本必然超时）
  D. 对拍（把模块换回旧口径 → B 必红；把 me.js 换回内联副本 → A 必红；每条判据都要被变异打红过）
  E. 已知口径差异 PIN（全角数字；已由 verify_pricing_consistency.py 的 F1 判为**不可达**，
     此处只把"两端实现本身仍不同"钉在案，防止下次被当成新发现）

用法：python tests/verify_count_range_pages_single_source.py     （exit 0 = 全部通过）
"""
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MINI = os.path.join(REPO, "mobile_apps", "h_n_print")
BACKEND = os.path.join(REPO, "mobile_apps", "printer-backend")

PAGES_REL = "utils/pages.js"                      # 唯一实现所在（相对 MINI）
PAGES_ABS = os.path.join(MINI, *PAGES_REL.split("/"))
CONSUMERS = ["pages/index/index.js", "pages/me/me.js", "pages/user-orders/user-orders.js"]

bad = 0


def chk(name, cond, detail=""):
    global bad
    if not cond:
        bad += 1
    print("[%s] %s%s" % ("PASS" if cond else "FAIL", name,
                         ("  -- " + detail) if (not cond and detail) else ""))


# ============================================================
# 判据（集中一处，供 A 节实跑 + D 节对拍复用）
# ============================================================

# 该实现的**所有历史名字**：出现任何一个函数定义都算"又长出一份口径"
RANGE_IMPL_NAMES = ("countPagesInRange", "_countRangePages", "countRangePages",
                    "_countRangePage")
KEYS = ("SINGLE_IMPL", "IMPL_LOCATION", "OLD_NAME", "NOT_REQUIRED", "NO_EXPORT",
        "LEFTOVER_RANGEINT")


def _blank_comment_lines(text):
    """整行注释置空 —— 判据吃代码不吃注释（说明里会引用旧函数名）。"""
    return "\n".join("" if ln.lstrip().startswith("#") or ln.lstrip().startswith("//") else ln
                     for ln in text.splitlines())


def audit_static(files):
    """静态判据：files = {相对路径: 源码}。返回违规列表（空 = 合规），每条带 key。"""
    v = []
    code = {rel: _blank_comment_lines(src) for rel, src in files.items()}

    # ① 实现只允许一处
    defs = []
    for rel, src in code.items():
        for m in re.finditer(r"(?m)^[ \t]*function\s+(\w+)\s*\(", src):
            if m.group(1) in RANGE_IMPL_NAMES:
                defs.append((rel, m.group(1)))
    if len(defs) != 1:
        v.append("SINGLE_IMPL: 小程序侧该实现应只有一份，实际 %d 份：%s" % (len(defs), defs))
    # ② 且必须落在 utils/pages.js，名字必须是 countPagesInRange
    elif defs[0][0] != PAGES_REL or defs[0][1] != "countPagesInRange":
        v.append("IMPL_LOCATION: 唯一实现应在 %s 且名为 countPagesInRange，实际在 %s（%s）"
                 % (PAGES_REL, defs[0][0], defs[0][1]))
    # ③ 旧名字不得残留（含调用）
    for rel, src in code.items():
        if "_countRangePages" in src:
            v.append("OLD_NAME: %s 仍残留旧函数名 _countRangePages（副本没清干净）" % rel)
    # ④ 三个消费方都必须从 utils/pages 取，不得自带实现
    for rel in CONSUMERS:
        if not re.search(r"require\(\s*['\"][^'\"]*utils/pages(?:\.js)?['\"]\s*\)", code.get(rel, "")):
            v.append("NOT_REQUIRED: %s 未 require utils/pages（还在自带一份口径）" % rel)
    # ⑤ 模块本身必须导出
    mod = code.get(PAGES_REL, "")
    if "module.exports" not in mod or "countPagesInRange" not in mod:
        v.append("NO_EXPORT: %s 没有 module.exports 出 countPagesInRange" % PAGES_REL)
    # ⑥ index.js 里那份配套的 _rangeInt 也必须一起搬走（留半份最容易被再复制）
    if re.search(r"(?m)^[ \t]*function\s+_rangeInt\s*\(", code.get("pages/index/index.js", "")):
        v.append("LEFTOVER_RANGEINT: pages/index/index.js 还留着 _rangeInt（应随实现一起搬进模块）")
    return v


def load_tree():
    """{相对路径: 源码} —— 小程序侧全部 .js。"""
    out = {}
    for root, dirs, names in os.walk(MINI):
        dirs[:] = [d for d in dirs if d not in ("node_modules", "__pycache__", ".git")]
        for n in names:
            if n.endswith(".js"):
                p = os.path.join(root, n)
                out[os.path.relpath(p, MINI).replace("\\", "/")] = \
                    open(p, encoding="utf-8").read()
    return out


# ============================================================
# 实跑器材：Node VM 里把「模块 + 三个页面」各自跑起来，比对同一组输入
# ============================================================

PROBE_JS = r'''
const fs=require('fs'),vm=require('vm');
const PAGES_SRC=fs.readFileSync(process.argv[2],'utf8');
const TARGETS=JSON.parse(fs.readFileSync(process.argv[3],'utf8'));
const CASES=JSON.parse(fs.readFileSync(process.argv[4],'utf8'));

// ---- 模块（utils/pages.js）当 CommonJS 跑一遍，取它的导出 ----
let MOD_EXPORTS={}, MOD_ERR=null;
try{ const m={exports:{}}; new Function('module','exports',PAGES_SRC)(m,m.exports); MOD_EXPORTS=m.exports; }
catch(e){ MOD_ERR=String(e.message); }

function EL(){return {innerHTML:'',textContent:'',value:'',style:{},dataset:{},classList:{add(){},remove(){},toggle(){},contains(){return false}},querySelector:()=>null,querySelectorAll:()=>[],appendChild(){},addEventListener(){},removeEventListener(){},getBoundingClientRect:()=>({height:0,width:0,top:0,left:0}),parentNode:null,children:[],focus(){},blur(){}};}
const B={console:{log(){},warn(){},error(){}},JSON,Math,Date,Object,Array,String,Number,Boolean,Error,TypeError,RangeError,Promise,Set,Map,WeakMap,WeakSet,Symbol,parseInt,parseFloat,isNaN,isFinite,setTimeout,clearTimeout,setInterval,clearInterval,RegExp,Function,ArrayBuffer,Uint8Array,decodeURIComponent,encodeURIComponent};
function newCtx(){
  const m=Object.assign({},B);
  m.global=m; m.globalThis=m;
  m.getApp=()=>({globalData:{isDarkMode:false,themeMode:'light',_pageRegistry:[],userInfo:null}});
  m.getCurrentPages=()=>[];
  m.Page=c=>{m.__cfg=c}; m.Component=c=>{m.__cfg=c}; m.Behavior=c=>{m.__cfg=c}; m.App=c=>{m.__cfg=c};
  m.wx={getStorageSync:()=>'',setStorageSync(){},removeStorageSync(){},setBackgroundColor(){},showToast(){},hideToast(){},showLoading(){},hideLoading(){},showModal(){},stopPullDownRefresh(){},switchTab(){},navigateTo(){},redirectTo(){},reLaunch(){},navigateBack(){},nextTick:f=>setTimeout(f,0),
    setNavigationBarColor(){},onWindowResize(){},offWindowResize(){},createAnimation:()=>({step(){return this},export(){return {}}}),previewImage(){},setClipboardData(){},
    getSystemInfoSync:()=>({windowWidth:375,windowHeight:800,platform:'devtools'}),getWindowInfo:()=>({windowWidth:375,windowHeight:800}),
    createSelectorQuery:()=>({selectAll(){return this},select(){return this},boundingClientRect(){return this},exec(cb){cb&&cb([[]])}}),
    uploadFile:()=>({abort(){},onProgressUpdate(){}})};
  m.require=p=>{
    const s=String(p);
    if(s.indexOf('utils/pages')>=0) return MOD_EXPORTS;
    if(s.indexOf('config')>=0) return {CONFIG:{BASE_URL:''}};
    return {request:()=>{}};
  };
  vm.createContext(m);
  return m;
}
function resolveFn(m){
  if(vm.runInContext('typeof countPagesInRange',m)==='function') return 'countPagesInRange';
  if(vm.runInContext('typeof _countRangePages',m)==='function') return '_countRangePages';
  return null;
}
function evalCases(m,fnName){
  return CASES.map(([pr,total])=>{
    try{ return vm.runInContext(fnName+'('+JSON.stringify(pr)+','+Number(total)+')',m); }
    catch(e){ return 'ERR:'+e.message; }
  });
}
const out={};
out['@@module']={fn:'exports.countPagesInRange',err:MOD_ERR,
  vals:(typeof MOD_EXPORTS.countPagesInRange==='function')
        ? CASES.map(([pr,total])=>{ try{return MOD_EXPORTS.countPagesInRange(pr,total);}catch(e){return 'ERR:'+e.message;} })
        : CASES.map(()=>'ERR:not-exported')};
for(const t of TARGETS){
  const m=newCtx();
  let err=null;
  try{ vm.runInContext(fs.readFileSync(t.file,'utf8'),m,{filename:t.name}); }
  catch(e){ err=String(e.message); }
  const fnName=err?null:resolveFn(m);
  out[t.name]={fn:fnName,err:err,vals:(fnName?evalCases(m,fnName):CASES.map(()=>'ERR:no-fn'))};
}
process.stdout.write(JSON.stringify(out));
'''

# 用例：覆盖四类偏差 + 正常口径。**刻意不含超长区间**（旧副本会卡死，C 节单独用子进程测）。
RANGE_CASES = [
    # 空 / 纯空白
    ("", 10), (None, 10), ("   ", 10), ("\t", 10),
    # 单页
    ("1", 10), ("5", 10), ("10", 10), ("0", 10), ("-1", 10),
    # 常规区间
    ("1-3", 10), ("2-4", 10), ("1-10", 10), ("3-3", 10),
    # ① 空白（旧副本把空白换成逗号 → 发散）
    ("1 - 3", 10), ("1 3", 10), ("1 -3", 10), ("1- 3", 10),
    (" 1-3 ", 10), ("1 - 3, 5", 10), ("1\t-\t3", 10),
    # 中文分隔
    ("1、3、5-7", 10), ("1，3；5", 10), ("1、3", 10),
    # ② 严格整数（parseInt 宽松 → 发散）
    ("3a", 10), ("3.5", 10), ("1e2", 10), ("0x10", 10), ("+3", 10), ("-3", 10), ("1-", 10),
    # ③ 段内多刀（split('-') 全切 → 发散）
    ("1-2-3", 10), ("1-2-3-4", 10),
    # 倒序智能拆分（23-4 → 2、3、4）
    ("23-4", 30), ("23-4", 5), ("53-2", 60), ("5-1", 10),
    # 越界 / 重复 / 全无效
    ("1-9999", 10), ("8-12", 10), ("1-3,2-4", 10), (",,", 10), ("-", 10),
]

TMP = tempfile.mkdtemp(prefix="hn_range_")
PROBE = os.path.join(TMP, "probe.js")
open(PROBE, "w", encoding="utf-8").write(PROBE_JS)
PAGES_SRC_FILE = os.path.join(TMP, "pages_src.js")
TARGETS_FILE = os.path.join(TMP, "targets.json")
CASES_FILE = os.path.join(TMP, "cases.json")
open(CASES_FILE, "w", encoding="utf-8").write(json.dumps(RANGE_CASES))


def run_probe(pages_src, targets, cases_file=CASES_FILE, timeout=60):
    """跑一次 VM 器材：pages_src = utils/pages.js 的源码文本；targets = [(name, file)]。
    返回 (parsed_dict, elapsed_seconds, raw_stderr)。"""
    open(PAGES_SRC_FILE, "w", encoding="utf-8").write(pages_src)
    open(TARGETS_FILE, "w", encoding="utf-8").write(
        json.dumps([{"name": n, "file": f} for n, f in targets]))
    t0 = time.time()
    r = subprocess.run(["node", PROBE, PAGES_SRC_FILE, TARGETS_FILE, cases_file],
                       capture_output=True, text=True, encoding="utf-8", timeout=timeout)
    dt = time.time() - t0
    if r.returncode != 0 or not r.stdout.strip():
        raise RuntimeError("probe 失败 rc=%s stderr=%s" % (r.returncode, (r.stderr or "")[:400]))
    return json.loads(r.stdout), dt, r.stderr


# ---- 后端权威实现（临时副本里 import，防污染真实库）----
_btmp = tempfile.mkdtemp(prefix="hn_range_be_")
_bdst = os.path.join(_btmp, "pb")
shutil.copytree(BACKEND, _bdst, ignore=shutil.ignore_patterns(
    "__pycache__", "config.py", "config.py.example", "orders.db", "users.db",
    "uploads", "logs", "*.db-wal", "*.db-shm"))
_cwd = os.getcwd()
os.chdir(_bdst)
os.environ["FLASK_ENV"] = "development"
sys.path.insert(0, _bdst)
import app as backend          # noqa: E402
os.chdir(_cwd)
sys.path.remove(_bdst)


def be_pages(rng, total):
    return backend._count_pages_in_range(rng, total)


# ============================================================
# A. 单一来源（静态）
# ============================================================
print("=== A. 单一来源（静态判据 %d 条）===" % len(KEYS))
TREE = load_tree()
# 模块缺失时**不许崩**（否则反向验证的红会落在 KeyError 而不是修复点）：退化成空串，
# 让 B 节如实报"模块没导出实现"，A 节如实报 NO_EXPORT。
PAGES_SRC = TREE.get(PAGES_REL, "")
violations = audit_static(TREE)
chk("★ 静态判据全绿", not violations, "；".join(violations))
chk("   %s 存在且真的实现了函数" % PAGES_REL, os.path.exists(PAGES_ABS),
    "文件不存在：%s" % PAGES_ABS)
for _c in CONSUMERS:
    chk("   %s require 了 %s" % (_c, PAGES_REL),
        bool(re.search(r"require\(\s*['\"][^'\"]*utils/pages(?:\.js)?['\"]\s*\)",
                       _blank_comment_lines(TREE.get(_c, "")))))

# ============================================================
# B. 实跑逐组比对
# ============================================================
print("\n=== B. 实跑比对（后端 vs 四个目标，%d 组输入）===" % len(RANGE_CASES))
TARGETS = [("index.js", os.path.join(MINI, "pages", "index", "index.js")),
           ("me.js", os.path.join(MINI, "pages", "me", "me.js")),
           ("user-orders.js", os.path.join(MINI, "pages", "user-orders", "user-orders.js"))]
result, _, _ = run_probe(PAGES_SRC, TARGETS)

for name in ["@@module", "index.js", "me.js", "user-orders.js"]:
    e = result.get(name, {})
    chk("   %s 解析到实现（%s）" % (name, e.get("fn")), e.get("fn") not in (None, "None"),
        "err=%s fn=%r" % (e.get("err"), e.get("fn")))

_div = []
NAMES = ["@@module", "index.js", "me.js", "user-orders.js"]
# 只比对"真的解析出了实现"的目标（模块未导出时它自己那条断言已经会红），
# 但**必须四个都可比**——否则"全都取不到值"会让发散列表为空、变成假绿。
_comparable = [n for n in NAMES if not str(result[n]["vals"][0]).startswith("ERR")]
for i, (rng, total) in enumerate(RANGE_CASES):
    be = be_pages(rng, total)
    got = {n: result[n]["vals"][i] for n in _comparable}
    if any(g != be for g in got.values()):
        _div.append((rng, total, be, got))
chk("★ 四目标与后端**逐组一致**（%d 组）" % len(RANGE_CASES),
    not _div and len(_comparable) == len(NAMES),
    "发散 %d 组%s" % (len(_div), "" if len(_comparable) == len(NAMES)
                    else "；且 %s 取不到值" % [n for n in NAMES if n not in _comparable]))
if _div:
    print("     发散明细（只列前 12 组）：")
    for rng, total, be, got in _div[:12]:
        print("       %-14r total=%-3d 后端=%-3s " % (rng, total, be)
              + " ".join("%s=%s" % (k, v) for k, v in got.items()))
    if len(_div) > 12:
        print("       …共 %d 组" % len(_div))

# ============================================================
# C. 超长区间不得无界循环（每目标一个子进程 + 硬超时）
# ============================================================
print("\n=== C. 超长区间（1-9999999999）不得无界循环 ===")
_huge_file = os.path.join(TMP, "huge.json")
open(_huge_file, "w", encoding="utf-8").write(json.dumps([["1-9999999999", 10]]))
_cases = [("@@module", PAGES_REL)] + [(n, f) for n, f in TARGETS]
for _n, _f in _cases:
    _tg = [] if _n == "@@module" else [(_n, _f)]
    try:
        _res, _dt, _ = run_probe(PAGES_SRC, _tg, cases_file=_huge_file, timeout=20)
        _v = _res["@@module" if _n == "@@module" else _n]["vals"][0]
        chk("   %s 超长区间立即返回（%.0f ms，结果 %s）" % (_n, _dt * 1000, _v),
            _v == 10 and _dt < 8, "耗时 %.1fs，结果 %r" % (_dt, _v))
    except subprocess.TimeoutExpired:
        chk("   %s 超长区间立即返回" % _n, False,
            "20s 内没跑完 ⇒ 区间迭代没收窄到 total（页面会卡死）")

# ============================================================
# D. 对拍（喂坏输入，判据/比对必须报）
# ============================================================
print("\n=== D. 对拍 ===")
_fired = []


def _check_mut(name, cond, expect, detail=""):
    global bad
    if not cond:
        bad += 1
    print("[%s] ★ 对拍：%s → 必须报 %s%s" % ("PASS" if cond else "FAIL", name, expect,
                                            ("  -- " + detail) if detail else ""))


def _mut_static(name, files, expect_key):
    _v = audit_static(files)
    _keys = [x.split(":")[0] for x in _v]
    _fired.extend(_keys)
    _check_mut(name, expect_key in _keys, expect_key, "实际违规=%s" % (_v[:3] or "无"))


def _copy():
    return dict(TREE)


def _repl(files, rel, pattern, repl):
    """把 files[rel] 里第一处 pattern 换成 repl；**没匹配上就返回 False**
    （改前状态下这些锚点还不存在 ⇒ 如实判红并继续，不让脚本崩在中间）。"""
    src = files.get(rel, "")
    out = re.sub(pattern, repl, src, count=1, flags=re.M)
    if out == src:
        return False
    files[rel] = out
    return True


# D1 干净源码不误报（先钉住"别乱报"）
chk("★ 对拍：干净源码 audit_static 返回空（不误报）", not audit_static(TREE))

# D2 把内联副本复制回 me.js（回归现场）
_d2 = _copy()
_ok = _repl(_d2, "pages/me/me.js",
            r"^const \{ countPagesInRange \} = require\([^\)]*\)$",
            "function countPagesInRange(r, t) { return t }")
if _ok:
    _mut_static("把 countPagesInRange 内联副本复制回 me.js", _d2, "SINGLE_IMPL")
else:
    _check_mut("把 countPagesInRange 内联副本复制回 me.js", False, "SINGLE_IMPL",
               "变异锚点缺失：me.js 里还没有 require utils/pages（尚未做单一来源）")

# D3 唯一实现被搬到别的文件（应在 utils/pages.js）
_d3 = _copy()
_ok = _repl(_d3, "utils/pages.js",
            r"(?ms)^function countPagesInRange\(.*?(?=^function |\Z)", "")
if _ok:
    _d3["pages/me/me.js"] = ("function countPagesInRange(r, t) { return t }\n"
                             + TREE["pages/me/me.js"])
    _mut_static("唯一实现被搬到 me.js（不在 utils/pages.js）", _d3, "IMPL_LOCATION")
else:
    _check_mut("唯一实现被搬到 me.js（不在 utils/pages.js）", False, "IMPL_LOCATION",
               "变异锚点缺失：utils/pages.js 不存在")

# D4 旧名字残留（又写一份 _countRangePages）
_d4 = _copy()
_d4["pages/user-orders/user-orders.js"] += "\nfunction _countRangePages(a, b) { return b }\n"
_mut_static("旧名字 _countRangePages 又长出来", _d4, "OLD_NAME")

# D5 消费方不再 require（自带口径）
_d5 = _copy()
_ok = _repl(_d5, "pages/user-orders/user-orders.js",
            r"^const \{ countPagesInRange \} = require\([^\)]*\)$", "")
if _ok:
    _mut_static("user-orders.js 不再 require utils/pages", _d5, "NOT_REQUIRED")
else:
    _check_mut("user-orders.js 不再 require utils/pages", False, "NOT_REQUIRED",
               "变异锚点缺失：user-orders.js 里还没有 require utils/pages")

# D6 模块不导出
_d6 = _copy()
_ok = _repl(_d6, "utils/pages.js", r"(?m)^module\.exports.*$", "")
if _ok:
    _mut_static("utils/pages.js 去掉 module.exports", _d6, "NO_EXPORT")
else:
    _check_mut("utils/pages.js 去掉 module.exports", False, "NO_EXPORT",
               "变异锚点缺失：utils/pages.js 不存在")

# D7 index.js 里那份 _rangeInt 没搬走
_d7 = _copy()
_d7["pages/index/index.js"] += "\nfunction _rangeInt(s) { return 1 }\n"
_mut_static("index.js 仍留着 _rangeInt", _d7, "LEFTOVER_RANGEINT")

# D8 把模块换回**旧口径** → B 节逐组比对必须报发散（证明"单一来源"真的生效）
_OLD_IMPL = """
function _rangeInt(s) { return s }
function countPagesInRange(rangeStr, total) {
  if (!rangeStr || !String(rangeStr).trim()) return total
  const pages = new Set()
  String(rangeStr).replace(/[、，；\\s]/g, ',').split(',').forEach(part => {
    part = (part || '').trim()
    if (!part) return
    if (part.indexOf('-') >= 0) {
      const sp = part.split('-')
      const a = parseInt(sp[0], 10), b = parseInt(sp[1], 10)
      if (!isNaN(a) && !isNaN(b)) {
        if (a < b) { for (let p = a; p <= b; p++) if (p >= 1 && p <= total) pages.add(p) }
      }
    } else {
      const p = parseInt(part, 10)
      if (!isNaN(p) && p >= 1 && p <= total) pages.add(p)
    }
  })
  return pages.size || total
}
module.exports = { countPagesInRange: countPagesInRange }
"""
_old_res, _, _ = run_probe(_OLD_IMPL, TARGETS)
_old_div = [i for i, (rng, total) in enumerate(RANGE_CASES)
            if any(_old_res[n]["vals"][i] != be_pages(rng, total)
                   for n in ["@@module", "index.js", "me.js", "user-orders.js"])]
_check_mut("把模块换回旧口径（空白→逗号 / parseInt / split 全切）",
           len(_old_div) >= 3, "B 节至少 3 组发散",
           "实际发散 %d 组（%s）" % (len(_old_div),
                                     [RANGE_CASES[i][0] for i in _old_div[:5]]))

# D9 判据覆盖：每条判据都必须被某次变异打红过（否则是**死判据**）
_dead = [k for k in KEYS if k not in _fired]
chk("★ 对拍：%d 条判据全部被变异覆盖（无死判据）" % len(KEYS), not _dead,
    "从未被打红: %s" % _dead)

# ============================================================
# E. 已知发散 PIN（🟡-D5b 全角数字，未修）
# ============================================================
print("\n=== E. 已知口径差异 PIN（全角数字，**不是新发现**）===")
_uni_file = os.path.join(TMP, "uni.json")
open(_uni_file, "w", encoding="utf-8").write(json.dumps([["３", 10], ["1-３", 10], ["٣", 10]]))
_uni_res, _, _ = run_probe(PAGES_SRC, [], cases_file=_uni_file)
_uni_rows = [("３", 1, _uni_res["@@module"]["vals"][0]),
             ("1-３", 3, _uni_res["@@module"]["vals"][1]),
             ("٣", 1, _uni_res["@@module"]["vals"][2])]
print("   注：本条**不是新发现** —— tests/verify_pricing_consistency.py 的 F1 已记录同一事实，"
      "并断言它**不可达**（两个前端的行校验 parseSingleRange 都用 parseInt，全角数字"
      "直接 NaN → 判非法 → 不会写进 page_range）。")
print("   Python int() 认 Unicode 十进制数字，JS 侧 /^[+-]?[0-9]+$/ 只认 ASCII ⇒ "
      "直接调函数时两端不同：")
for _r, _b, _j in _uni_rows:
    print("     %-6r 后端=%s 页 / 小程序=%s 页" % (_r, _b, _j))
chk("★ PIN：两端「解析口径」差异与 F1 记载一致（未变）",
    [(b, j) for _, b, j in _uni_rows] == [(1, 10), (3, 10), (1, 10)],
    "实测 %s —— 若已对齐，请把本 PIN 改成「必须一致」并同步 docs 与 "
    "verify_pricing_consistency.py 的 F1" % ([(b, j) for _, b, j in _uni_rows],))

# ============================================================
os.chdir(REPO)
shutil.rmtree(TMP, ignore_errors=True)
shutil.rmtree(_btmp, ignore_errors=True)
print("\n" + "=" * 60)
print("页码范围单一来源验收:", "全部通过 ✅" if bad == 0 else "存在失败 ❌（%d 条）" % bad)
sys.exit(0 if bad == 0 else 1)
