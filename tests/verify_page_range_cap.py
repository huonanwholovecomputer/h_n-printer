# -*- coding: utf-8 -*-
"""验收脚本 · 页码范围解析必须夹紧到已知边界（2026-10 审查 🔴-1）

事故形状：`_parse_page_range_set` / `_parse_range_parts` / `RangeListWidget._parse_range`
以及两端前端的同一份复制实现，都是「先建全区间集合、再逐个过滤」。
`1-999999999` 于是变成 10 亿次迭代：

  · 后端 —— 实测 1e7 = 198ms，线性外推 1e9 ≈ **20 秒/次**；而 `workers=1/threads=16`
    且循环发生在 `INSERT INTO orders`（已持 SQLite 唯一写者锁）之后 ⇒ 单个已登录请求
    即可占满线程、把其他写者拖到 `busy_timeout=30s` 后 `database is locked`。
  · 本地工具 —— 同一份循环跑在**主线程**（经 calc_cost）⇒ 界面卡 20 秒；
    `gui.py:_parse_range` 更狠：`set(range(1, 1e9))` ≈ **30+ GB**（表 + int 对象）→ MemoryError。
  · 两端前端 —— 1e6 区间 115ms，Set 无上限 ⇒ 1e9 ≈ 115 秒 + 内存爆。

本脚本钉三件事（缺一不可）：
  ① **结果不变**：所有"页数在合理范围"的输入，新实现的集合与旧实现**逐元素相同**
     （合法性判定、重叠检测、有效页数全部不受影响）；
  ② **夹紧生效**：超长区间不再空转（耗时上限）且不再越界（结果被夹到 [1, total]）；
  ③ **判据不变**：越界输入仍被判为「超出总页数」（收窄不能把非法输入洗成合法）。

四处实现全部覆盖：后端 app.py（AST 抽取真实函数体，不 import Flask）、
本地 printer_config + gui、APP print.js、小程序 index.js。
每处的"旧实现"用**各自忠实的**参考复刻（三者的分隔符归一化本来就不一样，
不能拿同一份参考去比 —— 那会把既有差异误报成本次改动引入的差异）。

    python tests/verify_page_range_cap.py     # exit 0 = 全绿
"""
import ast
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL_TOOL = os.path.join(REPO, "local_print_tool")
BACKEND = os.path.join(REPO, "mobile_apps", "printer-backend")
APP_JS = os.path.join(REPO, "mobile_apps", "android_app", "www", "print.js")
MP_JS = os.path.join(REPO, "mobile_apps", "h_n_print", "pages", "index", "index.js")

# 沙箱：不碰真实用户数据
_SANDBOX = tempfile.mkdtemp(prefix="hn_range_cap_")
os.environ["APPDATA"] = _SANDBOX
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, LOCAL_TOOL)
os.chdir(LOCAL_TOOL)

ok_all = True


def check(name, cond, detail=""):
    global ok_all
    if not cond:
        ok_all = False
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


# ============================================================
# 三份「旧实现」的忠实参考（只在声明的页码很小时执行，否则它们自己会卡死）
# ============================================================

# 「旧实现」参考的安全上限：参考实现本身就是**无上限**的那份代码，
# 拿它去跑超长区间会把本脚本自己变成内存炸弹（1e9 → 30+ GB，实测踩过）。
# 因此所有参考实现在执行前先扫一遍声明的页码跨度，超限**直接拒跑**而不是硬跑。
_REF_SPAN_GUARD = 5000


def _guard_span(text: str) -> None:
    for part in (text or "").replace("、", ",").replace("，", ",").replace("；", ",").split(","):
        part = part.strip()
        if not part:
            continue
        nums = []
        for tok in part.split("-", 1):
            tok = tok.strip()
            if tok.lstrip("+-").isdigit():
                nums.append(abs(int(tok)))
        if nums and max(nums) > _REF_SPAN_GUARD:
            raise RuntimeError(
                f"参考实现拒绝执行超长区间 {text!r}（声明页码 > {_REF_SPAN_GUARD}）")


def ref_filtered(range_str: str, total_pages: int, normalize: bool) -> set:
    """「全区间遍历 + 逐个过滤」的旧语义。normalize=True 复刻后端（归一 、，；与空白），
    False 复刻 printer_config._parse_range_parts（只按 ',' 切，不归一化）。"""
    pages = set()
    if not range_str or not range_str.strip():
        return pages
    _guard_span(range_str)
    raw = range_str.strip()
    if normalize:
        raw = raw.replace("、", ",").replace("，", ",").replace("；", ",").replace(" ", "")
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            try:
                a, b = part.split("-", 1)
                start, end = int(a), int(b)
                if start < end:
                    for p in range(start, end + 1):
                        if 1 <= p <= total_pages:
                            pages.add(p)
                elif start > end and len(a) > 1:
                    prefix = int(a[:-1])
                    last = int(a[-1])
                    if prefix < end:
                        for p in range(last, end + 1):
                            if 1 <= p <= total_pages:
                                pages.add(p)
                        if 1 <= prefix <= total_pages:
                            pages.add(prefix)
            except ValueError:
                pass
        else:
            try:
                p = int(part)
                if 1 <= p <= total_pages:
                    pages.add(p)
            except ValueError:
                pass
    return pages


def ref_gui(text: str, total_pages: int):
    """改前 gui.RangeListWidget._parse_range 的忠实复刻（单行输入，返回 None 或集合）。"""
    text = (text or "").strip()
    if not text:
        return None
    _guard_span(text)
    try:
        if "-" in text:
            a, b = text.split("-", 1)
            start, end = int(a), int(b)
            if 1 <= start < end:
                return set(range(start, end + 1))
            return None
        v = int(text)
        return {v} if v >= 1 else None
    except ValueError:
        return None


# 小输入：声明的页码都不大，旧实现不会卡死
SMALL_CASES = [
    "", "   ", "1", "0", "-1", "5", "abc", "1-5", "1-3", "2-7", "1-1", "2-2",
    "3-1", "23-4", "1,3,5-7", "1-2,4,6-8", "1 - 3", "1、2", "1，2；3", "1-100",
    "1-9,5", "12-20", "9-10", "1-200", "100-200", "199-200",
]
# gui 版接收的是**单行**输入（多段由控件拆行），只用单行 case 比
GUI_CASES = [c for c in SMALL_CASES if "," not in c and "、" not in c and "，" not in c
             and "；" not in c]

TOTALS = [0, 1, 2, 3, 7, 10, 12, 50, 200]

# 超长区间：(输入, total, 期望的"夹到 [1,total] 后"集合 或 None=不比集合)
HUGE_CASES = [
    ("1-999999999", 10, set(range(1, 11))),
    ("1-1000000000", 3, set(range(1, 4))),
    ("999999999", 10, set()),                  # 单页越界 → 空集
    ("1-999999999,3", 10, set(range(1, 11))),
    ("50-999999999", 10, set()),               # 整段落在总页数之外 → 空集
    ("5-999999999", 10, set(range(5, 11))),
]
HUGE_GUI = [
    ("1-999999999", 10, set(range(1, 11))),
    ("1-1000000000", 3, set(range(1, 4))),
    ("999999999", 10, set()),                  # 单页：不夹紧（无循环），只被 total 过滤掉
    ("50-999999999", 10, set()),
]

print("=" * 70)
print("① 后端 app.py::_parse_page_range_set（AST 抽取真实函数体，不 import Flask）")

src = open(os.path.join(BACKEND, "app.py"), encoding="utf-8").read()
tree = ast.parse(src)
target = next((n for n in tree.body
               if isinstance(n, ast.FunctionDef) and n.name == "_parse_page_range_set"), None)
check("后端找到 _parse_page_range_set 定义", target is not None)
ns: dict = {}
exec(compile(ast.Module(body=[target], type_ignores=[]), "<app.py>", "exec"), ns)
backend_parse = ns["_parse_page_range_set"]
check("后端实现可执行", callable(backend_parse))

bad = []
for total in TOTALS:
    for case in SMALL_CASES:
        try:
            got = backend_parse(case, total)
        except Exception as e:  # noqa: BLE001
            bad.append(f"{case!r} total={total} 抛 {type(e).__name__}: {e}")
            continue
        want = ref_filtered(case, total, normalize=True)
        if got != want:
            bad.append(f"{case!r} total={total} 得 {sorted(got)} 期望 {sorted(want)}")
check("后端：小输入结果与改前**逐元素相同**", not bad, "; ".join(bad[:4]))

slow = []
for case, total, want in HUGE_CASES:
    t0 = time.perf_counter()
    got = backend_parse(case, total)
    dt = time.perf_counter() - t0
    if dt > 0.05:
        slow.append(f"{case!r} {dt*1000:.0f}ms")
    if want is not None and got != want:
        slow.append(f"{case!r} 得 {sorted(got)} 期望 {sorted(want)}")
check("后端：超长区间被夹紧且 < 50ms（改前 1e9 ≈ 20s，单请求可拖垮全站）",
      not slow, "; ".join(slow))

print()
print("=" * 70)
print("② 本地 printer_config._parse_range_parts + gui.RangeListWidget._parse_range")

import printer_config as pc  # noqa: E402

bad = []
for total in TOTALS:
    for case in SMALL_CASES:
        got = pc._parse_range_parts(case, total)
        want = ref_filtered(case, total, normalize=False)   # 此函数不归一化分隔符
        if got != want:
            bad.append(f"{case!r} total={total} 得 {sorted(got)} 期望 {sorted(want)}")
check("printer_config：小输入结果与改前逐元素相同", not bad, "; ".join(bad[:4]))

slow = []
for case, total, want in HUGE_CASES:
    t0 = time.perf_counter()
    got = pc._parse_range_parts(case, total)
    dt = time.perf_counter() - t0
    if dt > 0.05:
        slow.append(f"{case!r} {dt*1000:.0f}ms")
    if want is not None and got != want:
        slow.append(f"{case!r} 得 {sorted(got)} 期望 {sorted(want)}")
check("printer_config：超长区间 < 50ms 且结果被夹紧（改前主线程卡 20s）",
      not slow, "; ".join(slow))

from PySide6.QtWidgets import QApplication  # noqa: E402

_app = QApplication.instance() or QApplication([])
import gui  # noqa: E402

R = gui.RangeListWidget
bad = []
for total in TOTALS:
    for case in GUI_CASES:
        got = R._parse_range(case, total)
        want = ref_gui(case, total)
        if want is None:
            if got is not None:
                bad.append(f"{case!r} total={total} 期望 None，得 {sorted(got)}")
            continue
        if got is None:
            bad.append(f"{case!r} total={total} 得 None，期望 {sorted(want)}")
            continue
        # 对称比较：只在 [1, total] 内比对（越界部分是新实现**故意**不再物化的，
        # 其"是否越界"的判据由下面的 verdict 检查单独钉住）。total=0 表示页数未知，
        # 此时旧实现与 `_RANGE_SPAN_CAP` 截断后的新实现都应给出同一集合。
        if total > 0:
            got_cmp = {p for p in got if p <= total}
            want_cmp = {p for p in want if p <= total}
        else:
            got_cmp, want_cmp = got, want
        if got_cmp != want_cmp:
            bad.append(f"{case!r} total={total} 得 {sorted(got_cmp)} 期望 {sorted(want_cmp)}")
check("gui：单行输入（含 None 判据）与改前一致", not bad, "; ".join(bad[:4]))

slow = []
for case, total, want in HUGE_GUI:
    t0 = time.perf_counter()
    got = R._parse_range(case, total)
    dt = time.perf_counter() - t0
    if dt > 0.05:
        slow.append(f"{case!r} {dt*1000:.0f}ms")
    if want is not None:
        got_wo = {p for p in (got or set()) if p <= total}
        if got_wo != want:
            slow.append(f"{case!r} 得 {sorted(got_wo)} 期望 {sorted(want)}")
check("gui：超长区间 < 50ms（改前 set(range(1,1e9)) ≈ 30+ GB → MemoryError）",
      not slow, "; ".join(slow))

verdicts = []


def ref_gui_declared_max(text: str):
    """旧 gui 实现"声明的最大页码"（**不建集合**，专供超长区间的判据比对）。

    `_check_all` 里判「超限」用的是 `max(pages) > total_pages`；旧实现返回的集合
    的最大元素恒等于输入的声明上界（`end` 或单页值），所以可以解析式求出，
    不必真的把集合建出来 —— 这正是本脚本要防的那种"顺手遍历"。
    """
    text = (text or "").strip()
    if not text:
        return None
    try:
        if "-" in text:
            a, b = text.split("-", 1)
            start, end = int(a), int(b)
            return end if 1 <= start < end else None
        v = int(text)
        return v if v >= 1 else None
    except ValueError:
        return None


for case, total in [("1-999999999", 10), ("50-999999999", 10), ("5-999999999", 10),
                    ("1-5", 10), ("1-3", 3), ("1-12", 10), ("200-300", 100),
                    ("999999999", 10), ("1-1", 10), ("abc", 10), ("0", 10)]:
    pages = R._parse_range(case, total)
    new_over = bool(pages) and total > 0 and max(pages) > total
    m = ref_gui_declared_max(case)
    old_pages_exist = m is not None
    old_over = old_pages_exist and total > 0 and m > total
    if old_over != new_over:
        verdicts.append(f"{case!r}@{total}: 新={new_over} 旧={old_over}")
check("gui：「超出总页数」判据与改前一致（哨兵路径未改判据）", not verdicts, "; ".join(verdicts))

big = R._parse_range("1-999999999", 0)
check("gui：页数未知时按 _RANGE_SPAN_CAP 截断（不建巨型集合）",
      big is not None and len(big) <= gui._RANGE_SPAN_CAP,
      f"len={len(big) if big else None} cap={gui._RANGE_SPAN_CAP}")

print()
print("=" * 70)
print("③ 两端前端 parseSingleRange / _parseSingleRange（node 真实求值）")

NODE = shutil.which("node") or r"C:\Users\Administrator\.workbuddy\binaries\node\versions\22.22.2-6\node.exe"
DRIVER = r"""
const fs = require('fs');
const cfg = JSON.parse(fs.readFileSync(process.argv[process.argv.length - 1], 'utf8'));
const src = fs.readFileSync(cfg.srcPath, 'utf8');

function extractFrom(marker) {
  const start = src.indexOf(marker);
  if (start < 0) throw new Error('未找到 ' + marker);
  const open = src.indexOf('{', start);
  let depth = 0;
  for (let i = open; i < src.length; i++) {
    if (src[i] === '{') depth++;
    else if (src[i] === '}') { depth--; if (depth === 0) return src.slice(start, i + 1); }
  }
  throw new Error('大括号不匹配: ' + marker);
}

const capMatch = src.match(/const RANGE_SPAN_CAP\s*=\s*(\d+)/);
const capSrc = capMatch ? capMatch[0] + ';' : '';
let fnSrc;
if (cfg.kind === 'app') {
  fnSrc = extractFrom('function parseSingleRange(text, total)');
} else {
  const raw = extractFrom('_parseSingleRange(text, total)');
  fnSrc = 'function parseSingleRange(text, total) ' + raw.slice(raw.indexOf('{'));
}
const parse = new Function(capSrc + fnSrc + '; return parseSingleRange;')();

// 参考实现：改前 parseSingleRange 的**忠实复刻**（单行输入、不归一化分隔符、
// 不按 total 过滤 —— 越界判定原本由调用方的 Math.max(...pages) > maxPages 负责）。
// ⚠ 它本身无上限：拿它跑 "1-999999999" 会把本脚本自己变成炸弹，故加跨度硬保险。
const REF_SPAN_GUARD = 5000;
function guardSpan(text) {
  String(text || '').split('-', 2).map(t => t.trim())
    .filter(t => /^[+-]?\d+$/.test(t)).forEach(t => {
      if (Math.abs(parseInt(t, 10)) > REF_SPAN_GUARD) {
        throw new Error('参考实现拒绝执行超长区间 ' + JSON.stringify(text));
      }
    });
}

function refSet(text) {
  const t = String(text || '').trim();
  if (!t) return null;
  guardSpan(t);
  if (t.indexOf('-') !== -1) {
    const seg = t.split('-');
    if (seg.length !== 2) return null;
    const start = parseInt(seg[0], 10), end = parseInt(seg[1], 10);
    if (isNaN(start) || isNaN(end)) return null;
    if (start >= 1 && start < end) {
      const pages = new Set();
      for (let p = start; p <= end; p++) pages.add(p);
      return pages;
    }
    return null;
  }
  const v = parseInt(t, 10);
  if (isNaN(v) || v < 1) return null;
  return new Set([v]);
}

const sortNum = a => [...a].sort((x, y) => x - y);

const mismatch = [];
for (const total of cfg.totals) {
  for (const c of cfg.cases) {
    const got = parse(c, total);
    const want = refSet(c);
    // 对称比较：只在 [1, total] 内比对（越界部分是新实现**故意**不再物化的）。
    // total=0（页数未知）时两边都不截断，直接比。
    const cut = s => (s === null ? null
      : sortNum(total > 0 ? [...s].filter(p => p <= total) : [...s]));
    const g = cut(got), w = cut(want);
    if (g === null && (w === null || w.length === 0)) continue;   // null ↔ 空，视为一致
    if (JSON.stringify(g) !== JSON.stringify(w)) {
      mismatch.push(`${JSON.stringify(c)}@${total} 得 ${JSON.stringify(g)} 期望 ${JSON.stringify(w)}`);
    }
  }
}

const slow = [];
for (const [c, total, wantArr] of cfg.huge) {
  const t0 = process.hrtime.bigint();
  const got = parse(c, total);
  const ms = Number(process.hrtime.bigint() - t0) / 1e6;
  if (ms > 50) slow.push(`${JSON.stringify(c)} ${ms.toFixed(0)}ms`);
  const clamped = got ? sortNum([...got].filter(p => p <= total)) : [];
  const want = sortNum(wantArr);          // 期望值由 Python 侧解析式给出，不跑参考实现
  if (JSON.stringify(clamped) !== JSON.stringify(want)) {
    slow.push(`${JSON.stringify(c)}@${total} 得 ${JSON.stringify(clamped)} 期望 ${JSON.stringify(want)}`);
  }
}

// 旧实现"声明的最大页码"：解析式求出，**不建集合**（`_check_all` 判超限用的就是它）
function declaredMax(text) {
  const t = String(text || '').trim();
  if (!t) return null;
  if (t.indexOf('-') !== -1) {
    const seg = t.split('-');
    if (seg.length !== 2) return null;
    const a = parseInt(seg[0], 10), b = parseInt(seg[1], 10);
    if (isNaN(a) || isNaN(b)) return null;
    return (a >= 1 && a < b) ? b : null;
  }
  const v = parseInt(t, 10);
  if (isNaN(v) || v < 1) return null;
  return v;
}

const verdict = [];
for (const [c, total] of cfg.verdict) {
  const got = parse(c, total);
  const over = !!got && got.size > 0 && total > 0 && Math.max(...got) > total;
  const m = declaredMax(c);
  const oldOver = m !== null && total > 0 && m > total;
  if (over !== oldOver) verdict.push(`${JSON.stringify(c)}@${total}: 新=${over} 旧=${oldOver}`);
}

console.log(JSON.stringify({ mismatch, slow, verdict }));
"""
tmp = tempfile.mkdtemp(prefix="hn_range_js_")
driver_path = os.path.join(tmp, "driver.js")
open(driver_path, "w", encoding="utf-8").write(DRIVER)

HUGE_JS = [
    ["1-999999999", 10, list(range(1, 11))],
    ["1-1000000000", 3, list(range(1, 4))],
    ["999999999", 10, []],
    ["1-999999999,3", 10, list(range(1, 11))],
    ["50-999999999", 10, []],
    ["5-999999999", 10, list(range(5, 11))],
]
VERDICT_JS = [[c, t] for c, t, _ in HUGE_JS[:3]] + [["1-5", 10], ["1-3", 3],
                                                    ["1-12", 10], ["200-300", 100],
                                                    ["999999999", 10], ["1-1", 10]]

for label, path, kind in (("APP print.js", APP_JS, "app"),
                          ("小程序 index.js", MP_JS, "mp")):
    cfg_path = os.path.join(tmp, f"cfg_{kind}.json")
    json.dump({"srcPath": path, "kind": kind, "cases": SMALL_CASES,
               "totals": TOTALS, "huge": HUGE_JS, "verdict": VERDICT_JS},
              open(cfg_path, "w", encoding="utf-8"))
    r = subprocess.run([NODE, driver_path, cfg_path], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=120)
    if r.returncode != 0:
        check(f"{label}：node 求值成功", False, (r.stdout + r.stderr)[-500:])
        continue
    data = json.loads(r.stdout.strip().splitlines()[-1])
    check(f"{label}：小输入结果与改前逐元素相同", not data["mismatch"],
          "; ".join(data["mismatch"][:4]))
    check(f"{label}：超长区间 < 50ms 且结果被夹紧", not data["slow"], "; ".join(data["slow"]))
    check(f"{label}：「超出总页数」判据与改前一致", not data["verdict"], "; ".join(data["verdict"]))

shutil.rmtree(tmp, ignore_errors=True)
shutil.rmtree(_SANDBOX, ignore_errors=True)

print()
print("=" * 70)
print("页码范围夹紧验收: " + ("全部通过 ✅" if ok_all else "有失败 ❌"))
sys.exit(0 if ok_all else 1)
