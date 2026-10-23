# -*- coding: utf-8 -*-
"""审计修复验收 · settlement.html（🔴9 XSS / 🔴10 弱哈希兜底）

1. 抽出 <script> 内容做 `node --check` 语法校验（含本轮所有改动）。
2. 在 Node 中加载密码相关函数，验证「前端不再产生任何哈希」。
3. 静态检查：内联 deleteDevice 事件已消除、白名单范式已用。

S1-05（2026-10-23，docs/安全加固修复计划_2026-10-23.md）：密码校验整体搬到本机服务端
（PBKDF2-HMAC-SHA256 + 每模块独立 salt + 210k 迭代），前端不再算哈希、也没有 isPasswordValid。
因此 🔴10 的判据从「无 Web Crypto 时不降级」翻成「前端这套哈希根本不存在、校验只走服务端」——
语义更严：弱哈希降级在结构上已经不可能发生。
"""
import io
import os
import re
import subprocess
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
HTML = os.path.join(REPO, "local_print_tool", "finance", "settlement.html")
ok_all = True


def check(name, cond, detail=""):
    global ok_all
    if not cond:
        ok_all = False
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


src = open(HTML, encoding="utf-8").read()

# ---------- 1) 抽出所有内联 <script> 并语法校验 ----------
scripts = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", src, re.S)
check("抽到内联 script 块", len(scripts) > 0, f"{len(scripts)} 块")

tmpdir = tempfile.mkdtemp(prefix="hn_settle_")
for i, s in enumerate(scripts):
    p = os.path.join(tmpdir, f"s{i}.js")
    open(p, "w", encoding="utf-8").write(s)
    r = subprocess.run(["node", "--check", p], capture_output=True, text=True, encoding="utf-8")
    check(f"script#{i} node --check", r.returncode == 0, (r.stderr or "").strip()[:300])

# 合并脚本供函数级测试（保留顺序，去掉 IIFE 的立即执行部分由下方单独处理）
merged = "\n;\n".join(scripts)
mp = os.path.join(tmpdir, "merged.js")
open(mp, "w", encoding="utf-8").write(merged)

# ---------- 2) 静态检查 ----------
print("\n--- 🔴9 内联事件注入 ---")
check("不再出现 onclick=\"deleteDevice('...') 拼接",
      'onclick="deleteDevice(\\' not in src and "onclick=\"deleteDevice(" not in src)
check("改用 data-device-delete 数据属性", 'data-device-delete="' in src)
check("渲染侧 client_id 经 safeId() 白名单", "var cidSafe = safeId(d.client_id);" in src)
check("委托侧再次 safeId() 白名单复查",
      'var cid = safeId(btn.getAttribute(\'data-device-delete\'));' in src)
check("非法 ID 的按钮被 disabled（fail-closed）",
      "设备 ID 含非法字符，已禁用删除" in src)

print("\n--- 🔴10 弱哈希兜底（S1-05 之后：前端不再有任何哈希/比对）---")
check("djb2 实现已删除（仅注释中提及）",
      not re.search(r"var h = 5381", src))
check("不再返回 'djb2:' 前缀哈希", "return Promise.resolve('djb2:'" not in src)
check("前端不再自带 SHA-256 哈希/比对（_sha256Hex / isPasswordValid / crypto.subtle.digest 全无）",
      "_sha256Hex" not in src and "function isPasswordValid" not in src
      and "crypto.subtle.digest" not in src)
check("hashPassword 只剩显式失败桩（残留调用立刻暴露，不会静默算出弱哈希）",
      re.search(r"function hashPassword\(\)\s*\{\s*\n\s*return Promise\.reject", src) is not None)
check("解锁/改密走服务端接口（PBKDF2）且失败有 .catch（不会静默失效）",
      "/api/local/finance_auth/unlock" in src
      and "/api/local/finance_auth/password" in src
      and re.search(r"function _financeAuthFetch\(url, payload\)[\s\S]{0,600}?\.catch\(", src) is not None)

# ---------- 3) 函数级行为验证（Node 内实测） ----------
print("\n--- 密码函数行为（Node 内实测）---")
harness = r"""
const fs = require('fs');
const vm = require('vm');
const code = fs.readFileSync(process.argv[2], 'utf8');

// 只取密码/解锁会话相关函数定义（避免执行整页渲染逻辑）
const start = code.indexOf('var FINANCE_SESSION = ');
const end = code.indexOf('function $(s)');
if (start < 0 || end < 0) { console.log('EXTRACT_FAIL'); process.exit(3); }
const snippet = code.slice(start, end);

function run(env) {
  const sandbox = Object.assign({ console, Promise, TextEncoder, String, Error, JSON }, env);
  vm.createContext(sandbox);
  vm.runInContext(snippet, sandbox);
  return sandbox;
}

(async () => {
  const results = [];

  // 环境 A：有 Web Crypto（老版本会在这里算出 SHA-256；S1-05 之后前端不该再产生任何哈希）
  const A = run({ window: { crypto: require('crypto').webcrypto, TextEncoder } });
  let rejA = false, msgA = '';
  try { await A.hashPassword('123456'); } catch (e) { rejA = true; msgA = String((e && e.message) || e); }
  results.push(['有 Web Crypto：hashPassword 仍然拒绝（哈希已迁到服务端，前端不产生"看起来能用"的值）',
                rejA, msgA.slice(0, 48)]);
  results.push(['有 Web Crypto：页面上没有 isPasswordValid（没有本地比对入口）',
                typeof A.isPasswordValid === 'undefined']);

  // 环境 B：无 Web Crypto（老版本会降级 djb2 / 把模块永久锁死）
  const B = run({ window: { crypto: {} } });
  let rejB = false;
  try { await B.hashPassword('123456'); } catch (e) { rejB = true; }
  results.push(['无 Web Crypto：同样拒绝（不降级 djb2、也不依赖浏览器加密 API）', rejB]);
  results.push(['无 Web Crypto：会话状态与清理函数仍可用（解锁逻辑不依赖浏览器加密 API）',
                B.FINANCE_SESSION === '' && typeof B._clearFinanceSession === 'function']);

  let fails = 0;
  for (const [n, c, d] of results) {
    if (!c) fails++;
    console.log(`[${c ? 'PASS' : 'FAIL'}] ${n}` + (d ? `  -- ${d}` : ''));
  }
  process.exit(fails ? 1 : 0);
})();
"""
hp = os.path.join(tmpdir, "harness.js")
open(hp, "w", encoding="utf-8").write(harness)
r = subprocess.run(["node", hp, mp], capture_output=True, text=True, encoding="utf-8")
out = (r.stdout or "") + (r.stderr or "")
print(out.strip())
if r.returncode != 0:
    ok_all = False

import shutil
shutil.rmtree(tmpdir, ignore_errors=True)
print("\n" + "=" * 50)
print("settlement.html 验收:", "全部通过 ✅" if ok_all else "存在失败 ❌")
sys.exit(0 if ok_all else 1)
