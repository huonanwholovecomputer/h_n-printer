# -*- coding: utf-8 -*-
"""选定与后端 `round(x, 2)` 完全同口径的 JS 取整实现

后端派送费 = round(文件费 × pct / 100, 2)。前端要用同口径补上这一步，
但 JS 没有 Python 的 round（半偶 + 对二进制精确值正确舍入），
所以先对「线上真实可达的输入」做差分，挑出 0 不一致的候选实现。
"""
import io, json, os, shutil, subprocess, sys, tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(REPO, "mobile_apps", "printer-backend")

tmp = tempfile.mkdtemp(prefix="hn_round_")
dst = os.path.join(tmp, "pb")
shutil.copytree(BACKEND, dst, ignore=shutil.ignore_patterns(
    "__pycache__", "orders.db", "users.db", "uploads", "logs"))
sys.path.insert(0, dst)
os.chdir(dst)
import app as backend
os.chdir(REPO)
backend._load_paper_prices = lambda: (0.3, 0.4)

# 线上可达的文件费（单/双面 × 页数 × 份数）
bases = set()
for pc in range(1, 41):
    for copies in range(1, 6):
        for duplex in ("on", "off"):
            v = round(backend.calculate_price(pc, duplex, "") * copies, 2)
            if v > 0:
                bases.add(v)
bases = sorted(bases)
pcts = [0.0, 5.0, 10.0, 15.0, 20.0]
cases = [[b, p] for b in bases for p in pcts]
print(f"可达文件费 {len(bases)} 种，派送档位 {pcts} → {len(cases)} 组")

# 后端（真实 Python）期望值，用「分」表示
expected = [int(round(round(b * p / 100, 2) * 100)) for b, p in cases]

probe = os.path.join(tmp, "r.js")
open(probe, "w", encoding="utf-8").write(r'''
const cases = JSON.parse(require('fs').readFileSync(process.argv[2], 'utf8'));
// 候选 A：Math.round（半上）
const A = (x) => Math.round(x * 100) / 100;
// 候选 B：模拟 Python round 的半偶
function B(x) {
  const v = x * 100, f = Math.floor(v), d = v - f;
  let r;
  if (d > 0.5) r = f + 1; else if (d < 0.5) r = f; else r = (f % 2 === 0) ? f : f + 1;
  return r / 100;
}
// 候选 C：toFixed（十进制字符串四舍五入，主流实现为半上/半远离零）
const C = (x) => Number(x.toFixed(2));
// 候选 D：BigInt 精确十进制展开 + 半偶（完全复刻 Python round 的语义）
function D(x) {
  if (!isFinite(x) || x === 0) return 0;
  const neg = x < 0;
  const ax = Math.abs(x);
  const buf = new DataView(new ArrayBuffer(8));
  buf.setFloat64(0, ax);
  const hi = buf.getBigUint64(0);
  const expBits = Number((hi >> 52n) & 0x7ffn);
  let mant = hi & 0xfffffffffffffn;
  let exp;
  if (expBits === 0) { exp = -1074; } else { mant |= 0x10000000000000n; exp = expBits - 1075; }
  // 精确值 = mant * 2^exp ；要舍入到 1e-2 → 计算 round(mant * 2^exp * 100) / 100
  let num = mant, den = 1n;
  if (exp >= 0) num <<= BigInt(exp); else den <<= BigInt(-exp);
  num *= 100n;                       // 目标：整数分
  let q = num / den, r2 = num % den;
  const twice = r2 * 2n;
  if (twice > den || (twice === den && (q % 2n) === 1n)) q += 1n;
  const cents = Number(q);
  return (neg ? -cents : cents) / 100;
}
const out = { A: [], B: [], C: [], D: [] };
for (const [b, p] of cases) {
  const x = (b * p) / 100;           // 与后端同顺序：base * pct / 100（左结合）
  out.A.push(Math.round(Math.round(x * 100) / 100 * 100));
  out.B.push(Math.round(B(x) * 100));
  out.C.push(Math.round(C(x) * 100));
  out.D.push(Math.round(D(x) * 100));
}
process.stdout.write(JSON.stringify(out));
''')
cf = os.path.join(tmp, "c.json")
open(cf, "w", encoding="utf-8").write(json.dumps(cases))
r = subprocess.run(["node", probe, cf], capture_output=True, text=True, encoding="utf-8")
got = json.loads(r.stdout)

for name, key in (("Math.round(x*100)/100", "A"), ("半偶模拟", "B"), ("toFixed(2)", "C"),
                  ("BigInt 精确 + 半偶（复刻 Python round）", "D")):
    bad = [(cases[i], expected[i], got[key][i]) for i in range(len(cases)) if got[key][i] != expected[i]]
    print(f"候选 {name:22s} 不一致 {len(bad)}/{len(cases)} 组"
          + (f"  例: 文件费{'' if not bad else bad[0][0][0]} 派送{bad[0][0][1]}% → 后端 {bad[0][1]}分 vs JS {bad[0][2]}分" if bad else "  ✅ 完全一致"))

shutil.rmtree(tmp, ignore_errors=True)
