# -*- coding: utf-8 -*-
"""针对「页码范围含空格」分歧的最小可读复现（用四端真实实现）"""
import io, json, os, shutil, subprocess, sys, tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(REPO, "mobile_apps", "printer-backend")
LOCAL = os.path.join(REPO, "local_print_tool")
PRINT_JS = os.path.join(REPO, "mobile_apps", "android_app", "www", "print.js")
INDEX_JS = os.path.join(REPO, "mobile_apps", "h_n_print", "pages", "index", "index.js")

tmp = tempfile.mkdtemp()
dst = os.path.join(tmp, "printer-backend")
shutil.copytree(BACKEND, dst, ignore=shutil.ignore_patterns("__pycache__", "orders.db", "users.db", "uploads", "logs"))
sys.path.insert(0, dst)
os.chdir(dst)
import app as backend
os.chdir(REPO)
sys.path.insert(0, LOCAL)
import printer_config as local_cfg
backend._load_paper_prices = lambda: (0.3, 0.4)

probe = os.path.join(tmp, "p.js")
open(probe, "w", encoding="utf-8").write(r'''
const fs=require('fs'),vm=require('vm');
const EL=()=>({innerHTML:'',textContent:'',value:'',style:{},dataset:{},classList:{add(){},remove(){},toggle(){},contains(){return false}},querySelector:()=>null,querySelectorAll:()=>[],appendChild(){},addEventListener(){},getBoundingClientRect:()=>({height:0}),parentNode:null,children:[]});
const B={console:{log(){},warn(){},error(){}},JSON,Math,Date,Object,Array,String,Number,Boolean,Error,Promise,Set,Map,parseInt,parseFloat,isNaN,setTimeout,clearTimeout,setInterval,clearInterval,FormData:class{append(){}},localStorage:{getItem:()=>null},navigator:{userAgent:'n'}};
const a=Object.assign({},B,{XMLHttpRequest:class{constructor(){this.upload={}}open(){}setRequestHeader(){}abort(){}send(){}},state:{token:''},BASE_URL:'',api:()=>Promise.resolve({}),ensureLogin:()=>Promise.resolve(true),showToast(){},openModal(){},closeModal(){},esc:s=>String(s==null?'':s),escHtml:s=>String(s==null?'':s),sanitizeColor:c=>c,measureAll(){},scheduleMeasureSoon(){},renderPricing(){},getApp:()=>({globalData:{}})});
a.window=a;a.globalThis=a;a.document={getElementById:()=>EL(),querySelector:()=>null,querySelectorAll:()=>[],createElement:()=>EL(),addEventListener(){},body:EL()};
vm.createContext(a);vm.runInContext(fs.readFileSync(process.argv[2],'utf8'),a,{filename:'print.js'});
const ps=vm.runInContext('printState',a);ps.simplexPrice=0.3;ps.duplexPrice=0.4;
let cfg=null;
const m=Object.assign({},B,{require:p=>p.indexOf('utils/config')>=0?{CONFIG:{BASE_URL:''}}:{request:()=>{}},Page:c=>{cfg=c},Component:c=>{cfg=c},getApp:()=>({globalData:{isDarkMode:false,themeMode:'light',_pageRegistry:[]}}),wx:{getStorageSync:()=>'',setStorageSync(){},removeStorageSync(){},setBackgroundColor(){},showToast(){},switchTab(){},getWindowInfo:()=>({windowWidth:375,windowHeight:800}),createSelectorQuery:()=>({selectAll:()=>({boundingClientRect(){return this}}),exec(cb){cb&&cb([[]])}}),uploadFile:()=>({abort(){},onProgressUpdate(){}}),nextTick:f=>setTimeout(f,0),getSystemInfoSync:()=>({windowWidth:375,windowHeight:800})}});
m.global=m;m.globalThis=m;vm.createContext(m);vm.runInContext(fs.readFileSync(process.argv[3],'utf8'),m,{filename:'index.js'});
const page=Object.assign({},cfg,cfg.methods);page.data={simplexPrice:0.3,duplexPrice:0.4};m.__p=page;
const cases=JSON.parse(fs.readFileSync(process.argv[4],'utf8'));
process.stdout.write(JSON.stringify(cases.map(([pc,copies,duplex,pr])=>({
  ap:vm.runInContext(`countPagesInRange(${JSON.stringify(pr)},${pc})`,a),
  ac:vm.runInContext(`calcCost(${pc},${copies},${JSON.stringify(duplex)},${JSON.stringify(pr)}).cost`,a),
  mp:vm.runInContext(`countPagesInRange(${JSON.stringify(pr)},${pc})`,m),
  mc:vm.runInContext(`__p._calcCost(${pc},${copies},${JSON.stringify(duplex)},${JSON.stringify(pr)}).cost`,m),
}))));
''')

cases = [[3, 1, "on", "1 - 3"], [3, 1, "on", "1-3"], [5, 2, "off", "1 - 3"], [2, 1, "on", "1 - 3"], [10, 1, "on", "2 - 4"]]
cf = os.path.join(tmp, "c.json")
open(cf, "w", encoding="utf-8").write(json.dumps(cases))
r = subprocess.run(["node", probe, PRINT_JS, INDEX_JS, cf], capture_output=True, text=True, encoding="utf-8")
js = json.loads(r.stdout)

print("页码范围 | 总页数 | 份数 | 双面 ‖ 后端/本地 有效页数→金额 ‖ APP 有效页数→金额 ‖ 小程序")
print("-" * 108)
for (pc, copies, duplex, pr), j in zip(cases, js):
    be_p = backend._count_pages_in_range(pr, pc)
    be_c = round(backend.calculate_price(pc, duplex, pr) * copies, 2)
    lo_c, _ = local_cfg.calc_cost(pc, copies, duplex, 0.3, 0.4, pr)
    mark = "  ← 不一致" if (be_c != j["ac"] or j["ac"] != j["mc"]) else ""
    print(f"{pr!r:10s} | {pc:6d} | {copies:4d} | {duplex:4s} ‖ {be_p}页 → ¥{be_c:.2f} ‖ {j['ap']}页 → ¥{j['ac']:.2f} ‖ {j['mp']}页 → ¥{j['mc']:.2f}{mark}")

shutil.rmtree(tmp, ignore_errors=True)
