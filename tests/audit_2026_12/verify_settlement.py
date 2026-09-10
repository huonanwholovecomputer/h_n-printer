# -*- coding: utf-8 -*-
"""审计修复验收 · settlement.html（🔴9 XSS / 🔴10 弱哈希兜底）

1. 抽出 <script> 内容做 `node --check` 语法校验（含本轮所有改动）。
2. 在 Node 中加载密码哈希函数，分别在「有/无 Web Crypto」两种环境下验证行为。
3. 静态检查：内联 deleteDevice 事件已消除、白名单范式已用。
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

print("\n--- 🔴10 弱哈希兜底 ---")
check("djb2 实现已删除（仅注释中提及）",
      not re.search(r"var h = 5381", src))
check("不再返回 'djb2:' 前缀哈希", "return Promise.resolve('djb2:'" not in src)
check("无 Web Crypto 时 _sha256Hex 直接 reject",
      "return Promise.reject(new Error(CRYPTO_UNAVAILABLE_MSG));" in src)
check("isPasswordValid 在无 Web Crypto 时 reject（不退回明文比对）",
      "if (!cryptoAvailable()) return Promise.reject(new Error(CRYPTO_UNAVAILABLE_MSG));" in src)
check("两个调用方都补了 .catch（不会静默失效）",
      src.count("CRYPTO_UNAVAILABLE_MSG") >= 2 and "密码保存失败" in src
      and "密码校验失败" in src)

# ---------- 3) 函数级行为验证（Node 内模拟两种环境） ----------
print("\n--- 密码函数行为（Node 内实测）---")
harness = r"""
const fs = require('fs');
const vm = require('vm');
const code = fs.readFileSync(process.argv[2], 'utf8');

// 只取到密码相关函数定义（避免执行整页渲染逻辑）
const start = code.indexOf('var CRYPTO_UNAVAILABLE_MSG');
const end = code.indexOf('function $(s)');
if (start < 0 || end < 0) { console.log('EXTRACT_FAIL'); process.exit(3); }
const snippet = code.slice(start, end);

function run(env) {
  const sandbox = Object.assign({ console, Promise, TextEncoder, String, Error }, env);
  vm.createContext(sandbox);
  vm.runInContext(snippet, sandbox);
  return sandbox;
}

(async () => {
  const results = [];

  // 环境 A：有 Web Crypto
  const A = run({ window: { crypto: require('crypto').webcrypto, TextEncoder } });
  const hA = await A.hashPassword('123456');
  results.push(['有 Web Crypto: hashPassword 返回 64 位 hex 且无 djb2 前缀',
                /^[0-9a-f]{64}$/.test(hA) && !hA.includes('djb2'), hA.slice(0, 16) + '...']);
  results.push(['有 Web Crypto: sha256: 前缀校验通过',
                await A.isPasswordValid('123456', 'sha256:' + hA) === true]);
  results.push(['有 Web Crypto: 错误密码校验为 false',
                await A.isPasswordValid('wrong', 'sha256:' + hA) === false]);
  results.push(['有 Web Crypto: 存量明文仍可校验（兼容迁移）',
                await A.isPasswordValid('oldplain', 'oldplain') === true]);

  // 环境 B：无 Web Crypto（crypto.subtle 缺失）
  const B = run({ window: { crypto: {} } });
  let rejected = false, msg = '';
  try { await B.hashPassword('123456'); } catch (e) { rejected = true; msg = e.message; }
  results.push(['无 Web Crypto: hashPassword 拒绝（不再降级 djb2）', rejected, msg.slice(0, 40)]);

  let rej2 = false;
  try { await B.isPasswordValid('123456', 'djb2:deadbeef'); } catch (e) { rej2 = true; }
  results.push(['无 Web Crypto: isPasswordValid 拒绝（含存量 djb2 记录）', rej2]);

  let ok2 = false;
  try { ok2 = await B.isPasswordValid('oldplain', 'oldplain'); } catch (e) { ok2 = false; }
  results.push(['无 Web Crypto: 不做任何降级比对（明文也拒绝）', ok2 === false]);

  let fails = 0;
  for (const [n, c, d] of results) {
    if (!c) fails++;
    console.log(`[${c ? 'PASS' : 'FAIL'}] ${n}` + (d ? '  -- ' + d : ''));
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
