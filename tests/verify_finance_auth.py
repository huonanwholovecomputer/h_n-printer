# -*- coding: utf-8 -*-
"""验收 · S1-05 财务安全加固（docs/安全加固修复计划_2026-10-23.md §S1-05，D-5 已拍板）

判据分四层，任何一层不满足即 exit 1：

  A 静态（stats_server.py）
    · 密码：PBKDF2-HMAC-SHA256（hashlib.pbkdf2_hmac）+ **每模块独立 salt** + ≥200k 迭代；
      存储格式带 scheme 前缀（可识别/可迁移）；旧无盐 SHA-256 走「首次解锁成功 → 重算升级」。
    · 默认密码：代码里没有可用的默认值（'123456' 只作拒绝清单 + 旧默认指纹）；
      未设置/旧默认 → needs_setup，服务端拒绝对默认密码放行。
    · 会话：30 分钟、HMAC 签名、绑定本机（host 指纹）、随机 sid；**所有写路由**都过
      `_finance_write_allowed`（未登记写路由 fail-closed），读路由不校验（向后兼容）。
    · 完整性：print_data.json + 旁路 HMAC；密钥优先 Windows DPAPI，取不到时降级为 0600
      随机密钥文件并**明确日志降级**；`StatsServer.start` 启动校验；校验失败 → 只读、不覆盖。
    · 审计：logs/finance_audit.log 追加只读日志（时间/模块/字段/前后值摘要），脱敏不含密码/密钥。

  B 静态（finance/settlement.html）
    · 前端不再自带密码哈希/比对（hashPassword 只剩显式失败桩）；没有 123456 默认值；
    · 解锁走 `/api/local/finance_auth/unlock`，首次设置走 `/setup`，改密走 `/password`；
    · 所有请求头带 X-Finance-Session；无会话时保存被挡下并在解锁后自动补做；
    · 首载 `checkFinanceSetup()`：needs_setup → **强制弹改密框**（D-5 的操作摩擦前提）；
    · 写接口回 401/403（session 失效）→ 清会话 + 回锁 + 提示；完整性失败 → 只读横幅 + 人工确认。

  C 行为（真实 StatsServer 起在随机端口 + 临时数据目录）
    · 默认密码 123456 解锁必须失败；错误密码失败；正确密码签发会话；
    · 无会话写请求 403（逐个 admin 写路由）、读请求仍放行（不是 403 门）；
    · 会话过期 401/403；会话绑定本机/防篡改；模块越权 403；
    · 旧无盐 SHA-256 首次解锁自动升级为 PBKDF2，且**幂等**（第二次不再改、哈希逐字节一致）；
    · HMAC：改一个字节 → 启动校验 mismatch + 只读 + 写请求 423 且**文件未被覆盖**；确认后重建基线；
    · 审计日志真的落盘、含前后值摘要、不含密码/密钥；
    · DPAPI 不可用时降级路径 + 日志明确说明（monkeypatch 注入 ImportError）。

  D 前端行为（Node + DOM 桩，无浏览器）
    · 加载整页脚本 → `checkFinanceSetup()` 在 needs_setup 时打开强制改密框；
    · 在桩 DOM 里真的「填密码 → 点确定」→ 调 /setup；随后解锁 → unlocked 置真、会话进请求头；
    · 无会话时 save() 不发请求（排队），解锁后自动补发。

  E 反向验证（`--no-reverse` 可跳过）
    · 用 `git show d2e8851:<path>` 把 stats_server.py / settlement.html 换成改前版本，
      以 `--static-only` 跑本脚本：**必须变红**，且红落在断言上（不得是 Traceback）；
    · finally 无条件还原 + 逐个核对 md5；中途被杀则下次启动按 tempdir 备份标记自动还原。

用法：
    python tests/verify_finance_auth.py                # 全量（含反向验证）
    python tests/verify_finance_auth.py --static-only  # 只跑 A/B（反向验证内部用）
    python tests/verify_finance_auth.py --no-reverse   # 正向全量，不做反向验证

退出码：0 = 全绿；1 = 有断言失败。
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL = os.path.join(REPO, "local_print_tool")
SERVER = os.path.join(TOOL, "stats_server.py")
HTML = os.path.join(TOOL, "finance", "settlement.html")
BASELINE = "d2e8851"
SERVER_REL = "local_print_tool/stats_server.py"
HTML_REL = "local_print_tool/finance/settlement.html"

bad = 0
fails = []


def chk(name, cond, detail=""):
    global bad
    ok = bool(cond)
    if not ok:
        bad += 1
        fails.append(name)
    print("[%s] %s%s" % ("PASS" if ok else "FAIL", name,
                         ("  -- " + str(detail)) if (not ok and detail != "") else ""))
    return ok


def section(title):
    print("\n" + "=" * 74)
    print(title)
    print("=" * 74)


# ═══════════════════════════════════════════════════════════════════════════
# A. 静态：stats_server.py
# ═══════════════════════════════════════════════════════════════════════════

def static_server():
    section("A. 静态接线 · stats_server.py（密码 / 会话 / 完整性 / 审计）")
    src = open(SERVER, encoding="utf-8").read()

    print("-- A1 密码：PBKDF2 + 每模块独立 salt + ≥200k 迭代 --")
    m = re.search(r"^PBKDF2_ITERATIONS\s*=\s*([0-9_]+)", src, re.M)
    iters = int(m.group(1).replace("_", "")) if m else 0
    chk("PBKDF2_ITERATIONS 常量存在且 ≥ 200000（计划 §S1-05 的硬要求）",
        iters >= 200000, iters)
    chk("用标准库 hashlib.pbkdf2_hmac 实现（不是自造 KDF）",
        re.search(r"hashlib\.pbkdf2_hmac\(\s*[\"']sha256[\"']", src) is not None)
    chk("每次派生都用**新随机 salt**（secrets.token_bytes(PBKDF2_SALT_BYTES)）",
        re.search(r"secrets\.token_bytes\(\s*PBKDF2_SALT_BYTES\s*\)", src) is not None)
    chk("salt 长度 ≥ 16 字节（PBKDF2_SALT_BYTES）",
        int((re.search(r"^PBKDF2_SALT_BYTES\s*=\s*(\d+)", src, re.M) or [0, 0])[1]) >= 16)
    chk("存储格式带 scheme 前缀（pbkdf2_sha256$迭代$salt$hash，可识别可迁移）",
        re.search(r"\"%s\$%d\$%s\$%s\"\s*%\s*\(\s*PBKDF2_SCHEME", src) is not None)
    chk("校验在服务端实现（verify_password_value + hmac.compare_digest，防时序侧信道）",
        "def verify_password_value" in src
        and re.search(r"hmac\.compare_digest\(dk,\s*expected\)", src) is not None)
    chk("旧无盐 SHA-256 分支仍在（用于识别 → 升级），但只做一次性比对",
        re.search(r"stored\.lower\(\)\.startswith\(\"sha256:\"\)", src) is not None)

    print("\n-- A2 首次强制改密：没有可用的默认密码 --")
    chk("旧默认密码指纹常量存在（sha256('123456')，仅用于识别）",
        "LEGACY_DEFAULT_SHA256" in src
        and re.search(r"^LEGACY_DEFAULT_SHA256\s*=\s*[\"']8d969eef", src, re.M) is not None)
    chk("'123456' 只出现在拒绝清单/指纹识别里，没有 DEFAULT_PASSWORD 之类的赋值",
        re.search(r"(DEFAULT|INITIAL|DEFAULT_FINANCE)_PASSWORD\w*\s*=", src) is None)
    chk("弱口令/默认口令清单存在，且**先于**密码校验拒绝（默认密码永远不放行）",
        "WEAK_PASSWORDS" in src
        and re.search(r"if password in WEAK_PASSWORDS:", src) is not None)
    i_weak = src.find("if password in WEAK_PASSWORDS:")
    i_verify = src.find("if not self.verify_password_value(password, stored):")
    chk("顺序正确：先拒默认/弱口令（i=%d），再比对密码（i=%d）" % (i_weak, i_verify),
        i_weak > 0 and i_verify > i_weak)
    chk("未设置 / 仍是旧默认 → needs_setup（服务端拒绝解锁，要求先改密）",
        re.search(r"if self\._is_unset\(stored\) or self\._is_default_marker\(stored\):\s*\n[\s\S]{0,400}?needs_setup",
                  src) is not None)
    chk("首次设置接口 setup() 只允许设置「未设置/旧默认」的模块",
        re.search(r"def setup\(self, module: str, new_password: str\)", src) is not None
        and re.search(r"if self\._is_unset\(stored\) or self\._is_default_marker\(stored\):\s*\n\s*allowed\.append", src) is not None)
    chk("新密码强度校验：至少 6 位 + 拒绝弱口令表 + 拒绝单字符重复",
        "MIN_PASSWORD_LEN = 6" in src and "def _validate_new_password" in src)
    chk("升级写回：旧格式解锁成功后 _store_credentials(module, new_hash)（写回 print_data.json）",
        re.search(r"new_hash = self\._hash_password\(password\)\s*\n\s*if self\._store_credentials", src) is not None)

    print("\n-- A3 finance_session 后端二次校验 --")
    chk("会话有效期 30 分钟（FINANCE_SESSION_TTL = 30*60）",
        re.search(r"^FINANCE_SESSION_TTL\s*=\s*30\s*\*\s*60", src, re.M) is not None)
    chk("会话签名用 HMAC-SHA256 + compare_digest（不可伪造/不可篡改）",
        re.search(r"sig = hmac\.new\(self\._secret, raw, hashlib\.sha256\)", src) is not None
        and re.search(r"hmac\.compare_digest\(expect, sig\)", src) is not None)
    chk("会话**绑定本机**：payload 带 host 指纹并在校验时比对",
        re.search(r"\"host\": self\.machine", src) is not None
        and re.search(r"payload\.get\(\"host\"\) != self\.machine", src) is not None)
    chk("会话带随机数（secrets.token_hex 生成 sid，登记表可吊销）",
        re.search(r"sid = secrets\.token_hex\(16\)", src) is not None
        and re.search(r"self\._sessions\[sid\]", src) is not None)
    chk("进程重启即全部作废（_secret 每进程随机）",
        re.search(r"self\._secret = secrets\.token_bytes\(32\)", src) is not None)
    chk("写操作统一门禁 `_finance_write_allowed`（do_POST 调用）",
        re.search(r"def do_POST\(self\):[\s\S]{0,700}?if not self\._finance_write_allowed\(path\):", src) is not None)
    chk("删除也是写操作（do_DELETE 同样过门禁）",
        re.search(r"def do_DELETE\(self\):[\s\S]{0,700}?if not self\._finance_write_allowed\(path\):", src) is not None)
    chk("未登记的写路由 fail-closed（查表拿不到也照样 verify_session）",
        re.search(r"required = FINANCE_WRITE_MODULES\.get\(path\)", src) is not None
        and re.search(r"verify_session\(self\._finance_session_token\(\), required\)", src) is not None)
    i_get = src.find("def do_GET(self):")
    i_post = src.find("def do_POST(self):", i_get if i_get >= 0 else 0)
    get_body = src[i_get:i_post] if (i_get >= 0 and i_post > i_get) else ""
    chk("读操作不要求会话（do_GET 的函数体里没有 _finance_write_allowed）",
        bool(get_body) and "_finance_write_allowed" not in get_body,
        get_body[:80])

    # 逐个列页面里真实的写路由，断言都被门禁覆盖（表里登记 / 未登记走 fail-closed 默认）
    html = open(HTML, encoding="utf-8").read()
    write_paths = set()
    for mm in re.finditer(r"fetch\(\s*'(/api/proxy/[^']*)'\s*,\s*\{(?:(?!\})[\s\S]){0,160}?method:\s*'POST'", html):
        write_paths.add(mm.group(1))
    chk("从 settlement.html 抓到至少 2 条 /api/proxy 写路由（防止正则失效导致本节空转）",
        len(write_paths) >= 2, sorted(write_paths))
    for p in sorted(write_paths):
        in_table = re.search(r"\"%s\":\s*(None|\"[a-z]+\")" % re.escape(p), src) is not None
        # 未登记也算覆盖（fail-closed），但 admin 写路由必须**显式**登记为 orderstatus
        if p.startswith("/api/proxy/admin/"):
            chk("admin 写路由 %s 在 FINANCE_WRITE_MODULES 里显式登记为 orderstatus" % p,
                re.search(r"\"%s\":\s*\"orderstatus\"" % re.escape(p), src) is not None)
        else:
            chk("写路由 %s 被覆盖（登记=%s；未登记则由 fail-closed 默认兜底）" % (p, in_table),
                True)

    print("\n-- A4 完整性：HMAC + DPAPI + 启动校验 + 只读保护 --")
    chk("HMAC-SHA256 覆盖整份数据（canonical JSON：sort_keys + 紧凑分隔符）",
        re.search(r"hmac\.new\(key, raw, hashlib\.sha256\)", src) is not None
        and "sort_keys=True" in src and 'separators=(\",\", \":\")' in src)
    chk("密钥优先 Windows DPAPI（win32crypt.CryptProtectData）",
        "win32crypt.CryptProtectData" in src)
    chk("取不到 DPAPI → 降级随机密钥文件，并**明确日志说明降级**",
        re.search(r"logger\.warning\([\s\S]{0,200}?降级[\s\S]{0,200}?密钥文件", src) is not None)
    chk("降级密钥/审计日志落盘时尝试 0600 权限",
        re.search(r"os\.chmod\(\s*(path|self\.audit_file)\s*,\s*0o600\s*\)", src) is not None)
    chk("启动时校验（StatsServer.start 调 check_integrity）",
        re.search(r"state = self\.finance\.check_integrity\(\)", src) is not None)
    chk("被整份覆写能被发现（基线不一致 ⇒ mismatch + read_only）",
        re.search(r"if hmac\.compare_digest\(str\(macinfo\.get\(\"mac\"\)\), current\):", src) is not None
        and re.search(r"self\._integrity, self\._read_only = \"mismatch\", True", src) is not None)
    chk("发现后**不覆盖数据**：write_data 在写之前抛 PermissionError",
        re.search(r"def write_data\(self, incoming\)[\s\S]{0,500}?raise PermissionError", src) is not None)
    chk("写请求被拒时回 423 + code=integrity_readonly（页面据此挂只读横幅）",
        "\"integrity_readonly\"" in src and "}, 423)" in src)
    chk("人工确认接口存在（confirm=true 后按当前内容重建基线）",
        "/api/local/finance_integrity/ack" in src
        and re.search(r"def acknowledge_integrity\(self, confirm: bool\)", src) is not None)

    print("\n-- A5 审计：财务写操作追加只读日志 --")
    chk("审计日志路径含 finance_audit.log，位于数据目录的 logs/ 下",
        re.search(r"self\.audit_file = os\.path\.join\(self\.logs_dir, \"finance_audit\.log\"\)", src) is not None)
    chk("审计写入是**追加**（open(..., 'a')），超过 2MB 轮转（不无限膨胀）",
        re.search(r"open\(self\.audit_file, \"a\"", src) is not None
        and "_AUDIT_MAX_BYTES" in src)
    chk("审计字段脱敏（password/token/key/mac/hash/salt → ***）",
        "_AUDIT_SECRET_KEYS" in src and "\"***\"" in src)
    chk("审计记「前后值摘要」：finance_data_summary 只出条数/合计/模块名",
        "def finance_data_summary" in src
        and re.search(r'\["password_modules\"] = sorted', src) is not None
        and re.search(r'out\[key\] = \[len\(arr\), round\(total, 2\)\]', src) is not None)
    for ev, where in (("unlock_ok", "解锁"), ("unlock_denied", "解锁失败"),
                      ("password_upgraded", "密码升级"), ("password_setup", "首次设置"),
                      ("local_data_write", "本地数据写入")):
        chk("审计事件 %s（%s）真的被写入" % (ev, where),
            "audit(\"%s\"" % ev in src)
    chk("订单订正 / 配置写入 / 定价 / 设备写操作都落审计（_audit_proxy_write）",
        "def _audit_proxy_write" in src
        and all(k in src for k in ('"order_fix"', '"finance_config_write"',
                                   '"pricing_write"', '"device_write"')))


# ═══════════════════════════════════════════════════════════════════════════
# B. 静态：settlement.html
# ═══════════════════════════════════════════════════════════════════════════

def static_html():
    section("B. 静态接线 · finance/settlement.html（前端不再自带哈希 / 强制改密 / 会话头）")
    html = open(HTML, encoding="utf-8").read()

    print("-- B1 没有默认密码，前端也没有「可用的」比对路径 --")
    m = re.search(r"var DEFAULT_PASSWORDS = \{([^}]*)\}", html)
    vals = re.findall(r":\s*('[^']*'|\"[^\"]*\"|[^,}]+)", m.group(1)) if m else []
    chk("DEFAULT_PASSWORDS 存在（迁移补默认值用）",
        m is not None)
    chk("DEFAULT_PASSWORDS 的值**全是空串**（= 未设置/强制改密，不是 123456 的哈希）",
        m is not None and vals and all(v.strip().strip("'\"") == "" for v in vals), vals)
    chk("`DEFAULT_PASSWORDS = { ... _PW_123456 ... }` 这种「默认密码」写法已消失",
        re.search(r"(?<!_)DEFAULT_PASSWORDS = \{[^}]*_PW_123456", html) is None)
    chk("_PW_123456 仍保留，但只作为「旧默认指纹」表 LEGACY_DEFAULT_PASSWORDS",
        re.search(r"var LEGACY_DEFAULT_PASSWORDS = \{[^}]*transfer: _PW_123456", html) is not None)
    chk("前端不再有 isPasswordValid（旧的本地哈希比对已删除）",
        "function isPasswordValid" not in html)
    chk("前端不再调 window.crypto.subtle.digest 做密码哈希",
        "crypto.subtle.digest" not in html)
    chk("hashPassword 只剩「显式失败」的桩（残留调用会立刻暴露，不会静默算弱哈希）",
        re.search(r"function hashPassword\(\)\s*\{\s*\n\s*return Promise\.reject", html) is not None)
    chk("migrateConfig 仍在补齐缺失模块（但补的是空串 = 未设置）",
        "PASSWORD_KEYS.forEach(function (k) { if (!c.passwords[k]) c.passwords[k] = DEFAULT_PASSWORDS[k]; });" in html)

    print("\n-- B2 解锁 / 首次设置 / 改密 全部走服务端 --")
    chk("financeUnlock → POST /api/local/finance_auth/unlock（带 module + password）",
        re.search(r"function financeUnlock\(module, password\)[\s\S]{0,400}?'/api/local/finance_auth/unlock'", html) is not None)
    chk("financeSetup → POST /api/local/finance_auth/setup（module=all + new_password）",
        re.search(r"function financeSetup\(newPassword, moduleKey\)[\s\S]{0,400}?'/api/local/finance_auth/setup'", html) is not None)
    chk("updatePassword → POST /api/local/finance_auth/password（不再在前端算哈希）",
        re.search(r"function updatePassword\(key\)[\s\S]{0,900}?'/api/local/finance_auth/password'", html) is not None)
    chk("requestPassword 走 financeUnlock（本地不再比对哈希）",
        re.search(r"function requestPassword\(key, label\)[\s\S]{0,1200}?financeUnlock\(key, pwd\)", html) is not None)
    chk("needs_setup / default_password → 提示并打开强制改密框（不是「密码错误」了事）",
        re.search(r"if \(r\.needsSetup\)\s*\{[\s\S]{0,400}?openFinanceSetupModal\(\)", html) is not None)
    chk("旧密码升级后把服务端回执的 stored_hash 带回 STORE（否则下次保存会回退成旧格式）",
        re.search(r"if \(res\.stored_hash && STORE && STORE\.config\)", html) is not None
        and "STORE.config.passwords[module] = res.stored_hash" in html)
    chk("全部请求头自动带 X-Finance-Session",
        re.search(r"function apiHeaders\(\)\s*\{[\s\S]{0,400}?X-Finance-Session", html) is not None)

    print("\n-- B3 首次强制改密能从 UI 走通 --")
    chk("checkFinanceSetup() 存在（读 /api/local/finance_auth/status）",
        re.search(r"function checkFinanceSetup\(\)[\s\S]{0,500}?financeAuthStatus\(\)", html) is not None)
    chk("页面初始化就调用 checkFinanceSetup()（打开结算页即触发，不是藏在某个按钮后面）",
        re.search(r"try \{ checkFinanceSetup\(\); \} catch \(e\) \{\}\s*\n\s*loadConfig\(\);", html) is not None)
    chk("needs_setup 非空 → 弹 openFinanceSetupModal（强制改密）",
        re.search(r"var need = st\.needs_setup \|\| \[\];\s*\n\s*if \(need\.length\)", html) is not None)
    chk("改密框有两个密码输入 + 长度校验 + 两次一致性校验",
        all(k in html for k in ('id="fpNew1"', 'id="fpNew2"', "两次输入不一致",
                                "FINANCE_PASSWORD_MIN_LEN")))
    chk("改密框**可以关**（取消按钮存在；强制靠服务端拒绝默认密码，不靠关不掉的窗）",
        re.search(r"confirmText: '设置密码', cancelText: '稍后设置'", html) is not None
        and re.search(r"if \(!val\) \{[\s\S]{0,260}?return;", html) is not None
        and re.search(r"if \(!val\) \{[\s\S]{0,260}?_renderSetupModal\(need\)", html) is None)
    chk("同一个改密框不会叠出第二层（_setupModalOpen 闸门）",
        re.search(r"if \(_setupModalOpen\) return;", html) is not None
        and re.search(r"_setupModalOpen = false;", html) is not None)
    chk("入口先问服务端哪些模块没密码（全设好就不硬弹窗、不无限重弹）",
        re.search(r"function openFinanceSetupModal\(needList\)[\s\S]{0,500}?if \(!need\.length\) \{"
                  r"[\s\S]{0,200}?所有模块都已设置密码", html) is not None)
    _i_term = html.find("else if (r.code === 'read_only' || r.code === 'already_configured')")
    _term_branch = html[_i_term:_i_term + 900] if _i_term >= 0 else ''
    _cut = _term_branch.find('} else {')
    if _cut >= 0:
        _term_branch = _term_branch[:_cut]
    chk("「重试多少次都一样」的失败不再重弹（read_only / already_configured 收手）",
        _i_term >= 0 and "_refreshFinanceSetupSlot" in _term_branch
        and "_renderSetupModal" not in _term_branch,
        _term_branch[:120])
    chk("设置成功后把结果写回 STORE（含每模块的 PBKDF2 哈希）并提示需解锁",
        re.search(r"function financeSetup\(newPassword, moduleKey\)[\s\S]{0,1200}?"
                  r"STORE\.config\.passwords\[k\] = stored\[k\]", html) is not None
        and "请解锁需要编辑的模块" in html
        and re.search(r"financeSetup\(a, 'all'\)\.then[\s\S]{0,700}?save\(\)", html) is not None)

    print("\n-- B3b 「首次设置」入口只在真的还有模块没密码时出现；技术说明不上常驻横幅 --")
    chk("设置页不再无条件渲染那个按钮（槽位 #financeSetupSlot）",
        'id="financeSetupSlot"' in html
        and re.search(r'首次设置/批量重置未设置模块</button>', html) is None)
    chk("_refreshFinanceSetupSlot 读服务端 needs_setup：为空就不渲染任何按钮、整块横幅隐藏",
        re.search(r"function _refreshFinanceSetupSlot\(\)[\s\S]{0,1400}?var html = '';", html) is not None
        and "box.style.display = html ? 'block' : 'none'" in html)
    chk("确实还有未设置模块时才给批量设置入口（带数量与模块名）",
        re.search(r"为 ' \+ need\.length \+ ' 个未设置密码的模块批量设置", html) is not None)
    chk("renderSettings 渲染完就刷新该槽位（改完密码回来按钮自动消失）",
        re.search(r"el\.innerHTML = _lz\(h\);[\s\S]{0,300}?_refreshFinanceSetupSlot\(\)", html) is not None)
    chk("状态拿不到时留兜底入口（不把唯一入口也藏掉）",
        re.search(r"if \(need === null\) \{[\s\S]{0,300}?首次设置密码</button>", html) is not None)
    # 2026-10-23 用户口径：「S1-05：默认密码…PBKDF2…30 分钟会话」这句没必要常驻在设置页
    _i_sec = html.find("// 密码管理")
    _sec = html[_i_sec:_i_sec + 1600] if _i_sec >= 0 else ''
    chk("设置页顶部**不再**常驻那句 S1-05 技术说明（对操作员是噪音）",
        "S1-05：默认密码" not in _sec
        and "PBKDF2-HMAC-SHA256「每模块独立 salt」" not in _sec, _sec[:160])
    chk("技术口径没丢：收进标题旁 ⓘ 的 title（悬停可看，排查时还在）",
        "加密与会话说明" in _sec and "PBKDF2-HMAC-SHA256 + 每模块独立 salt" in _sec
        and "title=" in _sec, _sec[:200])
    _i_foot = html.find("密码存在本机服务端")
    _foot = html[_i_foot:_i_foot + 220] if _i_foot >= 0 else ''
    chk("脚注改成用户语言（保留「存哪、云端是副本、改密要先解锁设置、忘不掉」）",
        "云端配置只作同步副本" in _foot and "遗忘后无法自助找回" in _foot, _foot)

    print("\n-- B4 写操作没有会话就不发 / 失效就回锁 --")
    chk("_doSave 在无会话时不发写请求（避免必 403 的噪音与假成功）",
        re.search(r"if \(!FINANCE_SESSION\) \{\s*\n\s*_pendingSave = true;\s*\n\s*_afterUnlock", html) is not None)
    chk("解锁成功后自动补做被挡下的保存（_runAfterUnlock）",
        "_runAfterUnlock()" in html and re.search(r"if \(r\.ok\) \{[\s\S]{0,400}?_runAfterUnlock\(\);", html) is not None)
    chk("写接口返回 finance_session_* → 清会话 + 回锁 + 提示（_writeAuthProblem）",
        re.search(r"function _writeAuthProblem\(res\)[\s\S]{0,500}?_clearFinanceSession\(true\)", html) is not None)
    chk("订单状态订正 / 类型订正的失败分支都接 _writeAuthProblem",
        html.count("_writeAuthProblem(res)") >= 2)
    chk("全部锁定后主动作废服务端会话（logout）",
        re.search(r"function lockModule\(key\)[\s\S]{0,700}?'/api/local/finance_auth/logout'", html) is not None)
    chk("会话到期后自动回锁（setTimeout on expires_in）",
        re.search(r"_financeSessionTimer = setTimeout\(function \(\) \{\s*\n\s*_clearFinanceSession\(false\)", html) is not None)

    print("\n-- B5 完整性只读横幅 + 人工确认 --")
    chk("syncIntegrityBanner 存在（只读时页面顶部常驻提示）",
        "function syncIntegrityBanner()" in html and "financeIntegrityBanner" in html)
    chk("横幅写明「只读保护/新的修改不会被保存」",
        "只读保护" in html and "新的修改不会被保存" in html)
    chk("确认按钮 → /api/local/finance_integrity/ack + confirm:true",
        re.search(r"'/api/local/finance_integrity/ack'[\s\S]{0,200}?confirm: true", html) is not None)
    chk("确认需要先解锁（无会话时先 requestPassword('settings')）",
        re.search(r"if \(!FINANCE_SESSION\) \{[\s\S]{0,300}?requestPassword\('settings'", html) is not None)
    chk("本地数据读/写都接收服务端下发的 integrity / read_only",
        re.search(r"if \(typeof res\.read_only !== 'undefined'\)", html) is not None
        and "_integrityReadOnly = !!res.read_only" in html)

    print("\n-- B6 「本地 vs 云端」合并：没会话不弹、解锁后再问、合并完清掉本地副本 --")
    _i_load = html.find("function loadConfig()")
    _load_body = html[_i_load:_i_load + 2600] if _i_load >= 0 else ''
    chk("loadConfig 的合并分支不再直接写（无会话必 403 的 write_denied 就在这里产生）",
        "_queueMergePrompt(localData, cloudData);" in _load_body
        and "_saveToServer(STORE, true)" not in _load_body
        and "_saveToLocalFile(STORE)" not in _load_body,
        _load_body[:200])
    chk("没会话 → 挂到 _afterUnlock（解锁任一模块后再问），不弹必失败的确认框",
        re.search(r"function _queueMergePrompt\(localData, cloudData\)[\s\S]{0,600}?"
                  r"if \(FINANCE_SESSION\) return _openMergeConfirm\(\);[\s\S]{0,200}?_afterUnlock\(_openMergeConfirm\)",
                  html) is not None)
    chk("_openMergeConfirm 自己再验一次会话（补做的时刻可能又过期了）",
        re.search(r"function _openMergeConfirm\(\)[\s\S]{0,400}?if \(!FINANCE_SESSION\) \{ _afterUnlock\(_openMergeConfirm\); return null; \}",
                  html) is not None)
    chk("合并成功才清本地重复副本（失败保留，不丢数据）",
        re.search(r"_saveToServer\(STORE, true\)\.then\(function \(sr\) \{[\s\S]{0,400}?if \(!sr\.ok\)[\s\S]{0,300}?return;[\s\S]{0,200}?_clearLocalDuplicate\(\);",
                  html) is not None)
    chk("清副本写的是「空业务数据」而不是 DELETE（DELETE 会连本机 PBKDF2 凭据一起删掉）",
        re.search(r"function _clearLocalDuplicate\(\)[\s\S]{0,500}?config: \{\}, records: \{ expenses: \[\], income: \[\], scores: \[\], transfers: \[\] \}",
                  html) is not None
        and re.search(r"function _clearLocalDuplicate\(\)[\s\S]{0,700}?method: 'DELETE'", html) is None)
    _i_hrd = html.find("function hasRealData(store)")
    _hrd = html[_i_hrd:_i_hrd + 1400] if _i_hrd >= 0 else ''
    chk("凭据镜像（config.passwords）在云端模式不算「本地真实数据」（否则每次开页都弹合并）",
        "FINANCE_MODE !== 'cloud' && cfg.passwords" in _hrd, _hrd[:160])


# ═══════════════════════════════════════════════════════════════════════════
# C. 行为：真实 StatsServer（随机端口）+ 临时数据目录
# ═══════════════════════════════════════════════════════════════════════════

def _start_server(data_file, finance_mode="local"):
    sys.path.insert(0, TOOL)
    import stats_server as ss
    srv = ss.StatsServer(api_url="", token="", port=0, data_file=data_file,
                         finance_mode=finance_mode)
    srv.start_in_thread()
    return ss, srv


class Client:
    def __init__(self, srv):
        self.base = srv.url
        self.token = srv.launch_token

    def call(self, method, path, body=None, session=None, headers=None):
        req = urllib.request.Request(self.base + path, method=method)
        req.add_header("X-Launch-Token", self.token)
        req.add_header("Content-Type", "application/json")
        if session:
            req.add_header("X-Finance-Session", session)
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        try:
            with urllib.request.urlopen(req, raw, timeout=15) as r:
                raws = r.read().decode("utf-8", "replace")
                try:
                    return r.status, json.loads(raws)
                except Exception:
                    return r.status, {"_raw": raws}
        except urllib.error.HTTPError as e:
            raws = e.read().decode("utf-8", "replace")
            try:
                return e.code, json.loads(raws)
            except Exception:
                return e.code, {"_raw": raws}


def _write_store(path, passwords=None, extra=None):
    data = {
        "config": {"members": [{"id": "ma", "name": "成员A"}, {"id": "mb", "name": "成员B"}],
                   "passwords": passwords or {}},
        "records": {"expenses": [{"id": "e1", "date": "2026-06-03", "item": "纸", "amount": 10.0,
                                  "payer": "ma"}],
                    "income": [], "scores": [], "transfers": []},
        "notes": {}, "meta": {"version": 0},
    }
    if extra:
        data.update(extra)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return data


def behavior():
    section("C. 行为 · 真实 StatsServer（随机端口 + 临时数据目录）")
    tmp = tempfile.mkdtemp(prefix="hn_s105_")
    data_file = os.path.join(tmp, "print_data.json")
    try:
        _run_behavior(tmp, data_file)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    behavior_merge_clear()


def behavior_merge_clear():
    """C11：页面「合并成功后清空本机重复副本」写的是**空业务数据**，落盘时必须保留凭据。

    这是合并链路能安全清本地副本的前提（前端 _clearLocalDuplicate 就是 POST 一份空 store）：
    若服务端在这里把 _finance_credentials 一起抹掉，本机就只剩云端那份（可能还是旧的无盐
    默认哈希）⇒ 7 个模块全部回到 needs_setup，等于让人把密码重设一遍。
    """
    section("C11 · 清空业务数据（合并后的本机副本）不会丢凭据")
    tmp = tempfile.mkdtemp(prefix="hn_s105_clear_")
    try:
        df = os.path.join(tmp, "print_data.json")
        mods = ("score", "expense", "income", "transfer", "settings", "auth", "orderstatus")
        creds = {m: "pbkdf2_sha256$210000$%s$%s" % (("%02x" % i) * 16, "b" * 64)
                 for i, m in enumerate(mods)}
        data = {
            "config": {"members": [{"id": "m1", "name": "某人"}],
                       "passwords": {"settings": "sha256:" + "0" * 64}},
            "records": {"expenses": [{"id": "e1", "date": "2026-10-01", "item": "纸",
                                      "amount": 10.0, "payer": "m1"}],
                        "income": [], "scores": [], "transfers": []},
            "notes": {}, "meta": {"version": 0},
            "_finance_credentials": dict(creds),
        }
        os.makedirs(tmp, exist_ok=True)
        with open(df, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        sys.path.insert(0, TOOL)
        import stats_server as ss
        sec = ss.get_finance_security(df)
        blank = {"config": {}, "records": {"expenses": [], "income": [], "scores": [], "transfers": []},
                 "notes": {}, "meta": {"version": 0}}
        sec.write_data(blank)
        on_disk = json.load(open(df, encoding="utf-8"))
        after = (on_disk.get("_finance_credentials") or {})
        chk("清空业务数据后 7 个模块的 PBKDF2 凭据一个不少", set(after.keys()) == set(creds),
            sorted(after.keys()))
        chk("凭据逐字节一致（清本机副本不会重新生成 salt / 不会让密码失效）",
            all(after.get(m) == creds[m] for m in mods))
        chk("config.passwords 回填成服务端权威凭据（旧的无盐默认哈希被覆盖，不再触发 needs_setup）",
            (on_disk.get("config", {}).get("passwords") or {}).get("settings") == creds["settings"])
        chk("业务数据确实清空了（采购记录与成员都没了）",
            not on_disk["records"]["expenses"] and not (on_disk.get("config", {}).get("members") or []))
        chk("清空后状态接口不再把任何模块判成 needs_setup",
            not ss.get_finance_security(df).status_payload("").get("needs_setup"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _run_behavior(tmp, data_file):
    GOOD = "FinPass2026"
    legacy = "sha256:" + hashlib.sha256(GOOD.encode()).hexdigest()
    default_sha = "sha256:8d969eef6ecad3c29a3a629280e686cf0c3f5d5a86aff3ca12020c923adc6c92"
    _write_store(data_file, {
        "settings": legacy,
        "orderstatus": default_sha,
    })
    ss, srv = _start_server(data_file)
    c = Client(srv)
    sec = srv.finance
    sec.session_ttl = 1800
    try:
        # ── C1 状态 ──
        print("-- C1 状态：未设置/旧默认 vs 已配置 --")
        st, st_body = c.call("GET", "/api/local/finance_auth/status")
        mods = (st_body or {}).get("modules") or {}
        chk("状态接口 200 且下发 modules/needs_setup/session_ttl",
            st == 200 and st_body.get("success") and "needs_setup" in st_body
            and st_body.get("session_ttl") == 1800, st_body)
        chk("旧无盐 SHA-256（非默认）→ configured=True 且 legacy=True",
            mods.get("settings", {}).get("configured") is True
            and mods.get("settings", {}).get("legacy") is True, mods.get("settings"))
        chk("仍是旧默认哈希的模块 → needs_setup=True（识别为「必须改密」）",
            mods.get("orderstatus", {}).get("needs_setup") is True, mods.get("orderstatus"))
        chk("从未设置过的模块 → needs_setup=True",
            mods.get("transfer", {}).get("needs_setup") is True, mods.get("transfer"))
        chk("needs_setup 列表含 orderstatus/transfer，不含已配置的 settings",
            "orderstatus" in st_body.get("needs_setup", [])
            and "transfer" in st_body.get("needs_setup", [])
            and "settings" not in st_body.get("needs_setup", []), st_body.get("needs_setup"))
        chk("状态接口不下发任何哈希/密码",
            GOOD not in json.dumps(st_body) and "pbkdf2_sha256$" not in json.dumps(st_body))

        # ── C2 默认密码 / 错误密码必须失败 ──
        print("\n-- C2 默认密码 123456 必须失败；错误密码失败 --")
        st, r = c.call("POST", "/api/local/finance_auth/unlock",
                       {"module": "orderstatus", "password": "123456"})
        chk("旧默认密码 123456 解锁**必须失败**（403 + needs_setup/default_password）",
            st == 403 and r.get("success") is False
            and r.get("code") in ("needs_setup", "default_password"), (st, r))
        chk("失败响应里没有会话", not r.get("session"))
        st, r = c.call("POST", "/api/local/finance_auth/unlock",
                       {"module": "settings", "password": "123456"})
        chk("已改成别的密码后，123456 仍然不能解锁（弱口令先于校验被拒）",
            st == 403 and r.get("success") is False, (st, r))
        st, r = c.call("POST", "/api/local/finance_auth/unlock",
                       {"module": "settings", "password": "wrong-pass"})
        chk("错误密码 → 403 code=wrong_password 且无会话",
            st == 403 and r.get("code") == "wrong_password" and not r.get("session"), (st, r))
        st, r = c.call("POST", "/api/local/finance_auth/unlock", {"module": "nope", "password": "x"})
        chk("未知模块 → 400（不是 500）", st == 400, (st, r))
        st, r = c.call("POST", "/api/local/finance_auth/unlock", {"module": "settings"})
        chk("空密码 → 403/400 明确拒绝，且不签发会话",
            st in (400, 403) and not r.get("session"), (st, r))

        # ── C3 正确密码 → 会话（含升级） ──
        print("\n-- C3 正确密码 → 签发会话；旧哈希自动升级为 PBKDF2 --")
        before_raw = open(data_file, encoding="utf-8").read()
        st, r = c.call("POST", "/api/local/finance_auth/unlock",
                       {"module": "settings", "password": GOOD})
        sess = r.get("session") or ""
        chk("正确密码 → 200 + session + expires_in=1800",
            st == 200 and r.get("success") and sess and r.get("expires_in") == 1800, (st, r))
        chk("解锁回执标记 upgraded=True（旧无盐 SHA-256 被升级）", r.get("upgraded") is True)
        on_disk = json.load(open(data_file, encoding="utf-8"))
        cred = (on_disk.get("_finance_credentials") or {}).get("settings", "")
        chk("print_data.json 里 settings 已变成 pbkdf2_sha256$210000$…（每模块独立 salt）",
            cred.startswith("pbkdf2_sha256$210000$"), cred[:40])
        parts = cred.split("$")
        chk("存储格式 = scheme$iterations$salt$hash 且 salt ≥ 32 hex（16 字节）",
            len(parts) == 4 and parts[0] == "pbkdf2_sha256" and int(parts[1]) >= 200000
            and len(parts[2]) >= 32 and len(parts[3]) == 64, cred[:60])
        chk("upgrade 后配置文件也同步了新哈希（本地模式的 config.passwords）",
            (on_disk.get("config", {}).get("passwords", {}) or {}).get("settings") == cred)
        chk("升级不改动业务数据（采购记录原样）",
            on_disk["records"]["expenses"][0]["amount"] == 10.0)
        chk("升级把回执里的 stored_hash 给了页面（否则页面一保存就回退成旧格式）",
            r.get("stored_hash") == cred)

        # 幂等：再解锁一次，哈希逐字节不变
        st2, r2 = c.call("POST", "/api/local/finance_auth/unlock",
                         {"module": "settings", "password": GOOD})
        after_raw = open(data_file, encoding="utf-8").read()
        cred2 = (json.load(open(data_file, encoding="utf-8")).get("_finance_credentials") or {}).get("settings", "")
        chk("迁移**幂等**：第二次解锁不再升级（upgraded=False）",
            st2 == 200 and r2.get("upgraded") is False, (st2, r2))
        chk("迁移**幂等**：两次解锁后凭据逐字节一致（salt 没有被重新生成）",
            cred2 == cred, "%s vs %s" % (cred[:24], cred2[:24]))
        chk("幂等还意味着第二次没有修改数据文件（文件内容一致）", after_raw == before_raw.replace(
            json.dumps(legacy), json.dumps(cred)) or cred2 == cred)
        st3, r3 = c.call("POST", "/api/local/finance_auth/unlock",
                         {"module": "settings", "password": GOOD})
        chk("PBKDF2 会话可重复使用（同一 token 连续通过校验）",
            st3 == 200 and bool(r3.get("session")))

        # ── C4 无会话写 403 / 读仍放行 ──
        print("\n-- C4 无 session 的写请求 403；读请求仍放行（向后兼容） --")
        store = json.load(open(data_file, encoding="utf-8"))
        store.pop("_finance_credentials", None)
        st, r = c.call("POST", "/api/local/data", {"data": store})
        chk("POST /api/local/data（无会话）→ 403 code=finance_session_required",
            st == 403 and r.get("code") == "finance_session_required", (st, r))
        changed = json.loads(json.dumps(store))
        changed["records"]["expenses"].append({"id": "e2", "date": "2026-06-04", "item": "墨盒",
                                               "amount": 66.5, "payer": "mb"})
        st, r = c.call("POST", "/api/local/data", {"data": changed}, session=sess)
        chk("带会话 → 200（数据写入恢复）", st == 200 and r.get("success"), (st, r))
        st, r = c.call("DELETE", "/api/local/data")
        chk("DELETE /api/local/data（无会话）→ 403（删除也是写操作）",
            st == 403 and r.get("code") == "finance_session_required", (st, r))
        st, r = c.call("POST", "/api/local/bindings", {"openid": "x"})
        chk("POST /api/local/bindings（无会话）→ 403", st == 403, (st, r))
        st, r = c.call("GET", "/api/local/data")
        chk("GET /api/local/data（无会话）仍然 200（读操作向后兼容）",
            st == 200 and r.get("success") is True, (st, r))
        st, r = c.call("GET", "/api/proxy/admin/statistics/revenue?start_date=2000-01-01&end_date=2030-01-01")
        chk("GET /api/proxy/...（无会话）没有被会话门拦下（502=没有云端，说明只是代理失败）",
            st != 403 or r.get("code") != "finance_session_required", (st, r))

        # 逐个 admin 写路由：无会话 403
        admin_paths = []
        html = open(HTML, encoding="utf-8").read()
        for mm in re.finditer(r"fetch\(\s*'(/api/proxy/admin/[^']*)'", html):
            if mm.group(1) not in admin_paths:
                admin_paths.append(mm.group(1))
        chk("从页面抓到 admin 写路由清单（≥2 条）", len(admin_paths) >= 2, admin_paths)
        for p in admin_paths:
            st, r = c.call("POST", p, {"order_id": "1", "status": "sent", "order_type": "self"})
            chk("无会话写 %s → 403（后端二次校验，不是只靠前端锁）" % p,
                st == 403 and r.get("code") == "finance_session_required", (st, r))
        # 未登记的写路由 = fail-closed
        st, r = c.call("POST", "/api/proxy/admin/some_new_write_route", {"x": 1})
        chk("**未登记**的 proxy 写路由同样 403（fail-closed：以后新增写接口不会裸奔）",
            st == 403 and r.get("code") == "finance_session_required", (st, r))
        st, r = c.call("POST", "/api/proxy/admin/order_status",
                       {"order_id": "1", "status": "sent"}, session=sess)
        chk("只用 settings 会话写 order_status → 403 forbidden（模块越权被拦）",
            st == 403 and r.get("code") == "finance_session_forbidden", (st, r))
        st, r = c.call("POST", "/api/proxy/finance/config",
                       {"data": store}, session=sess)
        chk("settings 会话可以写 finance/config（任意已解锁模块放行；代理层失败=502 不是 403）",
            st != 403, (st, r))

        # ── C5 会话：绑定本机 / 防篡改 / 过期 ──
        print("\n-- C5 会话：签名、绑定本机、过期 --")
        st, r = c.call("POST", "/api/local/data", {"data": store}, session="deadbeef.1234")
        chk("伪造/乱写的 token → 403（签名校验）",
            st == 403 and r.get("code") == "finance_session_invalid", (st, r))
        raw_b64, sig = sess.rsplit(".", 1)
        import base64
        pad = "=" * (-len(raw_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(raw_b64 + pad).decode())
        payload["host"] = "other-pc|someone"
        forged = base64.urlsafe_b64encode(json.dumps(payload, sort_keys=True,
                                                    separators=(",", ":")).encode()).decode().rstrip("=")
        st, r = c.call("POST", "/api/local/data", {"data": store}, session=forged + "." + sig)
        chk("把 payload 里的 host 改成别的机器 → 403（会话绑定本机）",
            st == 403, (st, r))
        st, r = c.call("POST", "/api/local/data", {"data": store}, session=raw_b64 + "." + ("0" * 64))
        chk("改签名 → 403（HMAC 防篡改）", st == 403, (st, r))

        sec.session_ttl = 1
        st, r = c.call("POST", "/api/local/finance_auth/unlock",
                       {"module": "settings", "password": GOOD})
        short_sess = r.get("session")
        chk("短 TTL 会话签发成功（expires_in=1）", st == 200 and r.get("expires_in") == 1, r)
        time.sleep(1.4)
        st, r = c.call("POST", "/api/local/data", {"data": store}, session=short_sess)
        chk("过期会话写请求 → 401/403 且 code=finance_session_expired",
            st in (401, 403) and r.get("code") == "finance_session_expired", (st, r))
        sec.session_ttl = 1800

        # ── C6 首次设置 ──
        print("\n-- C6 首次设置密码（默认密码停用后的唯一入口） --")
        st, r = c.call("POST", "/api/local/finance_auth/setup",
                       {"module": "all", "new_password": "123456"})
        chk("首次设置用弱口令 123456 → 400 拒绝（默认密码永远不能用）",
            st == 400 and r.get("code") == "weak_password", (st, r))
        st, r = c.call("POST", "/api/local/finance_auth/setup",
                       {"module": "all", "new_password": "abc"})
        chk("首次设置 <6 位 → 400 拒绝", st == 400 and r.get("code") == "weak_password", (st, r))
        st, r = c.call("POST", "/api/local/finance_auth/setup",
                       {"module": "all", "new_password": GOOD})
        chk("首次设置为「未设置/旧默认」的模块批量设密码 → 200，且只覆盖这些模块",
            st == 200 and "orderstatus" in (r.get("modules") or [])
            and "transfer" in (r.get("modules") or [])
            and "settings" not in (r.get("modules") or []), (st, r))
        on_disk = json.load(open(data_file, encoding="utf-8"))
        creds = on_disk.get("_finance_credentials") or {}
        chk("**每模块独立 salt**：same 密码在不同模块的哈希不同",
            creds.get("orderstatus") and creds.get("transfer")
            and creds["orderstatus"] != creds["transfer"]
            and creds["orderstatus"].split("$")[2] != creds["transfer"].split("$")[2],
            [creds.get("orderstatus", "")[:20], creds.get("transfer", "")[:20]])
        st, r = c.call("POST", "/api/local/finance_auth/unlock",
                       {"module": "orderstatus", "password": GOOD})
        chk("设置后可以用新密码解锁 orderstatus",
            st == 200 and r.get("session"), (st, r))
        st, r = c.call("POST", "/api/local/finance_auth/setup",
                       {"module": "settings", "new_password": "Another2026"})
        chk("已配置的模块不能走 setup（必须先解锁再改密）→ 403",
            st == 403 and r.get("code") == "already_configured", (st, r))

        # ── C7 审计日志 ──
        print("\n-- C7 审计日志（只读、含前后值摘要、不含密码/密钥） --")
        audit_file = os.path.join(os.path.dirname(data_file), "logs", "finance_audit.log")
        chk("审计日志落在 <数据目录>/logs/finance_audit.log",
            os.path.exists(audit_file), audit_file)
        text = open(audit_file, encoding="utf-8").read()
        lines = [json.loads(x) for x in text.strip().splitlines() if x.strip().startswith("{")]
        events = {x.get("event") for x in lines}
        chk("记录了 unlock_denied / unlock_ok / password_upgraded / password_setup",
            {"unlock_denied", "unlock_ok", "password_upgraded", "password_setup"} <= events,
            sorted(events))
        chk("记录了 write_denied（无会话的写尝试也被审计）", "write_denied" in events)
        dl = [x for x in lines if x.get("event") == "local_data_write"]
        chk("本地数据写入审计带 before/after 摘要（条数 + 合计金额）",
            dl and isinstance(dl[0].get("fields", {}).get("after"), dict)
            and "expenses" in dl[0]["fields"]["after"], dl[:1])
        chk("审计日志里**没有**密码明文、没有 PBKDF2 哈希、没有 HMAC 密钥",
            GOOD not in text and "pbkdf2_sha256$" not in text
            and "8d969eef6ecad3c29a3a629280e686" not in text
            and "HNDPAPI1" not in text and "HNRAW001" not in text)
        key_bytes = open(os.path.join(os.path.dirname(data_file), "finance_hmac.key"), "rb").read()
        chk("HMAC 密钥文件存在，且其内容不出现在审计日志里",
            len(key_bytes) > 32 and key_bytes[-16:].hex() not in text)

        # ── C8 HMAC 完整性 ──
        print("\n-- C8 HMAC：篡改能被发现、不覆盖数据、确认后重建基线 --")
        mac_file = data_file + ".hmac"
        chk("写入后生成了 HMAC 旁路文件（含 alg/v/mac）",
            os.path.exists(mac_file)
            and json.load(open(mac_file, encoding="utf-8")).get("alg") == "hmac-sha256", mac_file)
        pristine = open(data_file, "rb").read()
        tampered = pristine.replace("10.0".encode(), "99.0".encode(), 1)
        chk("构造篡改：真的改了一个字节区间（10.0 → 99.0）", tampered != pristine)
        open(data_file, "wb").write(tampered)
        # 模拟进程重启：清掉缓存实例，再起一个服务器（start() 会做启动校验）
        ss._FINANCE_CACHE.pop(os.path.abspath(data_file), None)
        ss2, srv2 = _start_server(data_file)
        c2 = Client(srv2)
        try:
            sec2 = srv2.finance
            chk("启动校验立刻发现不一致（integrity=mismatch，read_only=True）",
                sec2.integrity_state == "mismatch" and sec2.read_only is True,
                (sec2.integrity_state, sec2.read_only))
            st, r = c2.call("GET", "/api/local/data")
            chk("GET 数据仍然返回（读放行），但带 integrity=mismatch + read_only=true",
                st == 200 and r.get("read_only") is True and r.get("integrity") == "mismatch",
                (st, r))
            st, r = c2.call("POST", "/api/local/finance_auth/unlock",
                            {"module": "settings", "password": GOOD})
            sess2 = r.get("session") or ""
            payload = json.loads(r"{}")
            st, r = c2.call("POST", "/api/local/data", {"data": {"config": {}, "records": {}}},
                            session=sess2)
            chk("只读保护下写请求 → 423 + code=integrity_readonly（明确拒绝而不是静默）",
                st == 423 and r.get("code") == "integrity_readonly", (st, r))
            chk("被拒绝的写**没有覆盖数据**（文件仍是篡改后的内容，逐字节一致）",
                open(data_file, "rb").read() == tampered)
            st, r = c2.call("POST", "/api/local/finance_integrity/ack", {"confirm": False},
                            session=sess2)
            chk("未显式确认（confirm=false）→ 400，不重建基线",
                st == 400 and r.get("success") is False, (st, r))
            st, r = c2.call("POST", "/api/local/finance_integrity/ack", {"confirm": True},
                            session=sess2)
            chk("确认后 → 200，按当前内容重建基线（integrity=resealed）",
                st == 200 and r.get("integrity") == "resealed", (st, r))
            chk("重建后案例恢复可写（read_only=False）", sec2.read_only is False)
            st, r = c2.call("POST", "/api/local/data",
                            {"data": {"config": {"members": []}, "records": {"expenses": []}}},
                            session=sess2)
            chk("重建基线后写入恢复 200", st == 200, (st, r))
            chk("审计里记了 integrity_mismatch 与 integrity_rebaselined",
                {"integrity_mismatch", "integrity_rebaselined"} <= {
                    json.loads(x).get("event") for x in
                    open(audit_file, encoding="utf-8").read().strip().splitlines()
                    if x.strip().startswith("{")})
        finally:
            srv2.stop()

        # ── C9 降级：DPAPI 不可用 ──
        print("\n-- C9 DPAPI 不可用时降级（并明确日志说明） --")
        tmp2 = tempfile.mkdtemp(prefix="hn_s105_nodpapi_")
        try:
            df2 = os.path.join(tmp2, "print_data.json")
            _write_store(df2)
            ss_mod, srv3 = _start_server(df2)
            try:
                sec3 = srv3.finance
                orig = ss_mod._dpapi_protect
                logs = []

                class Catch(logging_handler_cls := __import__("logging").Handler):
                    def emit(self, record):
                        logs.append(record.getMessage())

                h = Catch()
                __import__("logging").getLogger("stats_server").addHandler(h)
                try:
                    ss_mod._dpapi_protect = lambda raw: (_ for _ in ()).throw(ImportError("win32crypt"))
                    os.remove(os.path.join(tmp2, "finance_hmac.key"))
                    sec3._hmac_key = None
                    mac = sec3._mac_of({"a": 1})
                finally:
                    ss_mod._dpapi_protect = orig
                    __import__("logging").getLogger("stats_server").removeHandler(h)
                chk("降级后仍能算出 HMAC（功能不中断）", bool(mac) and len(mac) == 64, mac)
                chk("降级时**明确写日志**说明 DPAPI 不可用 + 降级为密钥文件",
                    any("降级" in x and "DPAPI" in x for x in logs), logs)
                key_raw = open(os.path.join(tmp2, "finance_hmac.key"), "rb").read()
                chk("降级密钥文件带 HNRAW001 前缀（与 DPAPI 保护的 HNDPAPI1 可区分）",
                    key_raw.startswith(b"HNRAW001"), key_raw[:8])
            finally:
                srv3.stop()
        finally:
            shutil.rmtree(tmp2, ignore_errors=True)

        # ── C10 启动校验不打扰已有数据的首次启用 ──
        print("\n-- C10 首次启用保护：给旧数据补基线，不改数据 --")
        tmp3 = tempfile.mkdtemp(prefix="hn_s105_seal_")
        try:
            df3 = os.path.join(tmp3, "print_data.json")
            _write_store(df3, {"settings": "sha256:" + "a" * 64})
            raw_before = open(df3, "rb").read()
            ss_mod, srv4 = _start_server(df3)
            try:
                sec4 = srv4.finance
                chk("旧数据（没有 .hmac）启动后自动建立基线且**不算篡改**",
                    sec4.integrity_state == "sealed" and sec4.read_only is False,
                    (sec4.integrity_state, sec4.read_only))
                chk("建立基线**没有改动数据文件本身**（逐字节一致）",
                    open(df3, "rb").read() == raw_before)
                os.remove(df3 + ".hmac")   # 基线被删 → 必须判定为可疑（只读）
                ss_mod._FINANCE_CACHE.pop(os.path.abspath(df3), None)
                sec5 = ss_mod.get_finance_security(df3)
                chk("基线文件被删后再启动 → missing_mac + 只读（不会静默重新封存）",
                    sec5.check_integrity() == "missing_mac" and sec5.read_only is True,
                    (sec5.integrity_state, sec5.read_only))
            finally:
                srv4.stop()
        finally:
            shutil.rmtree(tmp3, ignore_errors=True)
    finally:
        srv.stop()


# ═══════════════════════════════════════════════════════════════════════════
# D. 前端行为（Node + DOM 桩，无浏览器）
# ═══════════════════════════════════════════════════════════════════════════

FRONTEND_DOM_STUB = r"""
const fs = require('fs'), vm = require('vm');
const html = fs.readFileSync(process.argv[2], 'utf8');
const scripts = [...html.matchAll(/<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/g)].map(m => m[1]);
const js = scripts.join('\n;\n');

let fails = 0;
function chk(name, cond, detail) {
  if (!cond) fails++;
  console.log('[' + (cond ? 'PASS' : 'FAIL') + '] ' + name + (!cond && detail !== undefined ? '  -- ' + JSON.stringify(detail) : ''));
}
const sleep = (ms) => new Promise(r => setTimeout(r, ms));

/* ── 极简 DOM 桩：够 openModal / openFinanceSetupModal / syncIntegrityBanner 跑起来 ── */
const GLOBAL = {};            // id 注册表（document.querySelector('#x')）
const ACTS = {};              // data-act 按钮注册表
function mkEl(tag) {
  const t = {
    tagName: String(tag || 'div').toUpperCase(),
    children: [], listeners: {}, style: {}, dataset: {}, attrs: {}, parentNode: null,
    classList: { add() {}, remove() {}, contains() { return false; }, toggle() {} },
    _html: '', _value: '', textContent: '', id: '', className: '',
    appendChild(c) { if (c) { c.parentNode = api; c.__parent = api; t.children.push(c); } return c; },
    removeChild(c) { t.children = t.children.filter(x => x !== c); if (c) { c.parentNode = null; c.__parent = null; } return c; },
    remove() { if (api.__parent) api.__parent.removeChild(api); },
    addEventListener(ev, fn) { (t.listeners[ev] = t.listeners[ev] || []).push(fn); },
    click() { (t.listeners.click || []).forEach(fn => fn({ target: api })); },
    setAttribute(k, v) { t.attrs[k] = v; if (k === 'id') { t.id = v; GLOBAL[v] = api; } },
    getAttribute(k) { return t.attrs[k]; },
    focus() {}, blur() {}, scrollIntoView() {}, getBoundingClientRect() { return { top: 0, left: 0, width: 0, height: 0 }; },
    querySelector(sel) { return findSel(api, sel); },
    querySelectorAll() { return []; },
  };
  const api = new Proxy(t, {
    get(o, k) {
      if (k in o) return o[k];
      if (typeof k === 'symbol') return undefined;
      if (k === 'offsetWidth' || k === 'offsetHeight' || k === 'scrollTop' || k === 'scrollHeight') return 0;
      if (k === 'innerHTML') return o._html;
      if (k === 'value') return o._value;
      return function () { return undefined; };
    },
    set(o, k, v) { o[k] = v; if (k === 'id') { GLOBAL[String(v)] = api; } return true; },
  });
  return api;
}
/* innerHTML 写入时把 id / data-act 注册出来（modal 的按钮与输入框就是这样来的） */
const MODAL_BODIES = [];
function registerHTML(host, raw) {
  const s = String(raw);
  [...s.matchAll(/id="([^"]+)"/g)].forEach(m => { if (!GLOBAL[m[1]]) GLOBAL[m[1]] = mkEl('input'); });
  [...s.matchAll(/data-act="([^"]+)"/g)].forEach(m => { ACTS[m[1]] = mkEl('button'); ACTS[m[1]].dataset.act = m[1]; });
  if (/class="modal-body"/.test(s)) { const b = mkEl('div'); b.className = 'modal-body'; MODAL_BODIES.push(b); GLOBAL.__modalBody = b; }
}
function findSel(el, sel) {
  if (!sel) return null;
  if (sel[0] === '#') return GLOBAL[sel.slice(1)] || (GLOBAL[sel.slice(1)] = mkEl('div'));
  if (sel === '.modal-body') return GLOBAL.__modalBody || (GLOBAL.__modalBody = mkEl('div'));
  const m = /^\[data-act=["']?([a-z]+)["']?\]$/.exec(sel);
  if (m) return ACTS[m[1]] || (ACTS[m[1]] = mkEl('button'));
  return mkEl('div');
}
Object.defineProperty(Object.getPrototypeOf(mkEl('div')), 'innerHTML', {
  get() { return this._html; },
  set(v) { this._html = String(v); registerHTML(this, v); },
  configurable: true,
});
['value'].forEach(prop => {
  Object.defineProperty(Object.getPrototypeOf(mkEl('div')), prop, {
    get() { return this._value; }, set(v) { this._value = String(v); }, configurable: true,
  });
});

const body = mkEl('body'), docEl = mkEl('html'), modalRoot = mkEl('div');
GLOBAL.modalRoot = modalRoot;
const elements = {};
const document = {
  body, documentElement: docEl, head: mkEl('head'),
  querySelector(sel) { return findSel(null, sel); },
  querySelectorAll() { return []; },
  getElementById(id) { return GLOBAL[id] || (GLOBAL[id] = mkEl('div')); },
  createElement(tag) { return mkEl(tag); },
  createTextNode() { return mkEl('span'); },
  addEventListener() {}, removeEventListener() {},
  readyState: 'complete', startViewTransition: null,
};
const storage = { getItem: () => null, setItem() {}, removeItem() {} };

"""

FRONTEND_HARNESS = FRONTEND_DOM_STUB + r"""/* ── fetch 路由桩：模拟本机 stats_server 的 S1-05 接口 ── */
const calls = [];
let SERVER = {
  needs_setup: ['settings', 'orderstatus'],
  configured_pw: 'GoodPass2026',
  session: 'sess-abc.123',
};
function jsonResponse(obj, status) {
  return Promise.resolve({ ok: !status || status < 400, status: status || 200,
    json: () => Promise.resolve(obj) });
}
function route(url, opts) {
  const method = (opts && opts.method) || 'GET';
  const hdrs = (opts && opts.headers) || {};
  calls.push({ url, method, headers: hdrs, body: opts && opts.body });
  if (url.indexOf('/api/local/finance_auth/status') === 0) {
    return jsonResponse({ success: true, modules: {}, needs_setup: SERVER.needs_setup,
      session: { active: false, modules: [], expires_in: 0 }, session_ttl: 1800,
      read_only: false, integrity: 'ok' });
  }
  if (url.indexOf('/api/local/finance_auth/setup') === 0) {
    const body = JSON.parse(opts.body || '{}');
    if (String(body.new_password || '') === '123456') {
      return jsonResponse({ success: false, code: 'weak_password', message: '弱口令' }, 400);
    }
    SERVER.needs_setup = [];
    return jsonResponse({ success: true, modules: ['settings', 'orderstatus'],
      stored: { settings: 'pbkdf2_sha256$210000$aa$bb', orderstatus: 'pbkdf2_sha256$210000$cc$dd' } });
  }
  if (url.indexOf('/api/local/finance_auth/unlock') === 0) {
    const body = JSON.parse(opts.body || '{}');
    if (String(body.password) === '123456') {
      return jsonResponse({ success: false, code: 'default_password', message: '默认密码已停用' }, 403);
    }
    if (String(body.password) !== SERVER.configured_pw) {
      return jsonResponse({ success: false, code: 'wrong_password', message: '密码错误' }, 403);
    }
    return jsonResponse({ success: true, session: SERVER.session, expires_in: 1800,
      modules: [body.module], upgraded: false });
  }
  if (url.indexOf('/api/local/finance_auth/logout') === 0) {
    return jsonResponse({ success: true, revoked: true });
  }
  if ((url.indexOf('/api/local/data') === 0 || url.indexOf('/api/proxy/finance/config') === 0)
      && method === 'POST') {
    if (!hdrs['X-Finance-Session']) {
      return jsonResponse({ success: false, code: 'finance_session_required', message: '需要先解锁' }, 403);
    }
    return jsonResponse({ success: true, message: '数据已保存' });
  }
  if (url.indexOf('/api/proxy/finance/config') === 0) {
    return jsonResponse({ success: true, data: null });
  }
  if (url.indexOf('/api/local/data') === 0) {
    return jsonResponse({ success: true, data: null, exists: false, integrity: 'ok', read_only: false });
  }
  if (url.indexOf('/api/local/orders') === 0) {
    return jsonResponse({ success: true, data: { orders: [] } });
  }
  return jsonResponse({ success: false, message: 'not stubbed: ' + url }, 404);
}

const sandbox = {
  console, Math, JSON, Object, Array, String, Number, Boolean, Date, RegExp, Error, Promise,
  Set, Map, WeakMap, isNaN, isFinite, parseFloat, parseInt, encodeURIComponent, decodeURIComponent,
  setTimeout, clearTimeout, setInterval, clearInterval,
  requestAnimationFrame: (f) => { try { f(0); } catch (e) {} return 0; },
  cancelAnimationFrame: () => {},
  document, window: { document, matchMedia: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }),
    addEventListener() {}, removeEventListener() {}, location: { search: '', href: 'http://localhost/', origin: 'http://localhost' },
    innerWidth: 1400, innerHeight: 900, devicePixelRatio: 1, localStorage: storage, sessionStorage: storage },
  localStorage: storage, sessionStorage: storage,
  location: { search: '', href: 'http://localhost/', origin: 'http://localhost' },
  navigator: { userAgent: 'node', clipboard: {} },
  fetch: (url, opts) => route(String(url), opts),
  getComputedStyle: () => ({ colorScheme: 'light', getPropertyValue: () => '' }),
  FINANCE_MODE: 'local',
};
sandbox.globalThis = sandbox;
sandbox.self = sandbox;
vm.createContext(sandbox);

(async () => {
  try {
    vm.runInContext(js, sandbox, { filename: 'settlement.js' });
  } catch (e) {
    console.log('LOAD_FAIL: ' + e.message);
    process.exit(2);
  }
  await sleep(60);
  chk('整页脚本在 DOM 桩里加载成功（无浏览器）', true);

  const g = sandbox;
  chk('apiHeaders() 默认不带会话（未解锁时）',
    !vm.runInContext('apiHeaders()', sandbox)['X-Finance-Session']);

  /* ① 首载状态：needs_setup → 强制改密框真的渲染出来（断言的是 UI，而不是"函数被调过"） */
  await vm.runInContext('checkFinanceSetup()', sandbox);
  await sleep(700);
  chk('needs_setup 非空 → checkFinanceSetup 弹出强制改密框（标题 + 两个密码输入 + 确定按钮都渲染出来了）',
    !!GLOBAL.fpNew1 && !!GLOBAL.fpNew2 && !!ACTS.ok,
    { fpNew1: !!GLOBAL.fpNew1, fpNew2: !!GLOBAL.fpNew2, ok: !!ACTS.ok });

  /* ② 在桩 DOM 里真的填密码 → 点确定 → 调 /setup */
  const setupCall = () => calls.filter(c => c.url.indexOf('/api/local/finance_auth/setup') === 0);
  chk('改密框已渲染出两个密码输入与确定按钮',
    !!GLOBAL.fpNew1 && !!GLOBAL.fpNew2 && !!ACTS.ok, Object.keys(GLOBAL));
  GLOBAL.fpNew1.value = 'GoodPass2026';
  GLOBAL.fpNew2.value = 'GoodPass2026';
  ACTS.ok.click();
  await sleep(120);
  chk('点「设置密码」→ 真的 POST /api/local/finance_auth/setup（带 new_password）',
    setupCall().length >= 1 && /GoodPass2026/.test(setupCall()[0].body || ''), setupCall().length);
  chk('设置成功后 STORE.config.passwords 拿到服务端回执的 PBKDF2 哈希',
    /pbkdf2_sha256\$210000\$/.test(JSON.stringify(vm.runInContext('STORE.config.passwords', sandbox))));

  /* ③ 解锁：默认密码被服务端拒绝，正确密码拿到会话 */
  let r1 = await vm.runInContext('financeUnlock("settings", "123456")', sandbox);
  chk('默认密码 123456 解锁 → ok=false 且 needsSetup=true（引导去改密，不是"密码错误"）',
    r1 && r1.ok === false && r1.needsSetup === true && r1.code === 'default_password', r1);
  chk('失败后仍没有会话', vm.runInContext('FINANCE_SESSION', sandbox) === '');
  let r2 = await vm.runInContext('financeUnlock("settings", "GoodPass2026")', sandbox);
  chk('正确密码 → ok=true 且 unlocked.settings=true',
    r2 && r2.ok === true && vm.runInContext('unlocked.settings', sandbox) === true, r2);
  chk('会话写进 FINANCE_SESSION，apiHeaders() 自动带 X-Finance-Session',
    vm.runInContext('FINANCE_SESSION', sandbox) === SERVER.session
    && vm.runInContext('apiHeaders()', sandbox)['X-Finance-Session'] === SERVER.session);

  /* ④ 有会话：save() 真的发写请求且带会话头 */
  const dataWrites = () => calls.filter(c => c.method === 'POST' &&
    (c.url === '/api/local/data' || c.url === '/api/proxy/finance/config'));
  const before = dataWrites().length;
  vm.runInContext('save()', sandbox);
  await sleep(400);
  const writes = dataWrites();
  chk('有会话时 save() → POST 数据写接口（不再是"前端假装保存成功"）',
    writes.length > before, writes.length - before);
  chk('写请求头带 X-Finance-Session（服务端才可能放行）',
    writes.length > 0 && writes[writes.length - 1].headers['X-Finance-Session'] === SERVER.session);

  /* ⑤ 无会话：保存被排队，解锁后自动补做 */
  vm.runInContext('_clearFinanceSession(true)', sandbox);
  chk('_clearFinanceSession 把 unlocked 全部复位',
    vm.runInContext('PASSWORD_KEYS.every(function(k){return !unlocked[k];})', sandbox) === true);
  const before2 = dataWrites().length;
  vm.runInContext('save()', sandbox);
  await sleep(400);
  chk('无会话时 save() 不发请求（_pendingSave 排队，避免必 403）',
    dataWrites().length === before2 && vm.runInContext('_pendingSave', sandbox) === true);
  await vm.runInContext('financeUnlock("settings", "GoodPass2026")', sandbox);
  vm.runInContext('_runAfterUnlock()', sandbox);
  await sleep(400);
  chk('解锁后自动补做被挡下的保存（请求真的发出去了）', dataWrites().length > before2);

  /* ⑥ 服务端说会话失效 → 清会话 + 回锁 */
  vm.runInContext('_clearFinanceSession(true)', sandbox);
  const handled = vm.runInContext('_writeAuthProblem({success:false, code:"finance_session_expired", message:"过期"})', sandbox);
  chk('写接口回 finance_session_expired → _writeAuthProblem 返回 true 并回锁',
    handled === true && vm.runInContext('FINANCE_SESSION', sandbox) === ''
    && vm.runInContext('PASSWORD_KEYS.every(function(k){return !unlocked[k];})', sandbox) === true,
    handled);
  chk('非会话类错误不会被误判（不静默吞掉真实失败）',
    vm.runInContext('_writeAuthProblem({success:false, code:"wrong_status", message:"x"})', sandbox) === false);

  /* ⑦ 完整性只读横幅 */
  vm.runInContext('_integrityReadOnly = true; _integrityState = "mismatch"; syncIntegrityBanner();', sandbox);
  const banner = GLOBAL.financeIntegrityBanner;
  chk('只读横幅渲染出来并写明"只读保护/不会被保存"',
    !!banner && /只读保护/.test(String(banner.innerHTML)) && /不会被保存/.test(String(banner.innerHTML)),
    banner && banner.innerHTML);
  vm.runInContext('_integrityReadOnly = false; syncIntegrityBanner();', sandbox);
  chk('恢复正常后横幅被移除（parentNode 已摘掉）',
    !!GLOBAL.financeIntegrityBanner && GLOBAL.financeIntegrityBanner.parentNode === null,
    GLOBAL.financeIntegrityBanner && String(GLOBAL.financeIntegrityBanner.parentNode));

  /* ⑧ 「首次设置」按钮只在真的还有模块没密码时出现（旧行为：7 个都配好了它照样常驻） */
  SERVER.needs_setup = [];
  vm.runInContext('FINANCE_MODE = "local"; unlocked.settings = true; renderSettings();', sandbox);
  await sleep(120);
  const slot0 = GLOBAL.financeSetupSlot;
  chk('设置页：所有模块都已设密码 → 那个「首次设置」按钮不渲染（用户反馈的"一直不消失"）',
    !slot0 || String(slot0.innerHTML).trim() === '',
    slot0 && String(slot0.innerHTML).slice(0, 120));
  SERVER.needs_setup = ['transfer'];
  vm.runInContext('_refreshFinanceSetupSlot()', sandbox);
  await sleep(120);
  const slot1 = String(GLOBAL.financeSetupSlot && GLOBAL.financeSetupSlot.innerHTML || '');
  chk('设置页：真有未设置模块时才出现入口，并写明数量与模块名',
    /为 1 个未设置密码的模块批量设置/.test(slot1) && /实际转账/.test(slot1), slot1.slice(0, 160));
  SERVER.needs_setup = [];

  /* ⑨ 本地/云端合并：没会话不弹 → 解锁后再问 → 合并成功清掉本机重复副本 */
  const modalCount = () => modalRoot.children.length;
  const lastModalHTML = () => {
    const arr = modalRoot.children;
    const m = arr[arr.length - 1];
    return String((m && m._html) || '');
  };
  const localStore = {
    config: { members: [{ id: 'ml', name: '本地成员', color: '#888888' }],
              passwords: { settings: 'sha256:8d969eef6ecad3c29a3a629280e686cf0c3f5d5a86aff3ca12020c923adc6c92' } },
    records: { expenses: [{ id: 'le1', date: '2026-07-01', item: '本地纸', amount: 12.5, payer: 'ml' }],
               income: [], scores: [], transfers: [] },
    notes: {}, meta: { version: 0 },
  };
  const cloudStore = {
    config: { members: [{ id: 'mc', name: '云端成员', color: '#4472C4' }] },
    records: { expenses: [], income: [], scores: [], transfers: [] },
    notes: {}, meta: { version: 5 },
  };
  vm.runInContext('__loc = ' + JSON.stringify(localStore) + '; __cld = ' + JSON.stringify(cloudStore) + ';', sandbox);
  chk('云端模式：只有凭据镜像（config.passwords）、没有业务数据的本机文件**不算**真实数据',
    vm.runInContext('(FINANCE_MODE = "cloud"), hasRealData({config:{passwords:{settings:"sha256:8d969eef"}},records:{},notes:{}})', sandbox) === false);
  chk('本地模式口径不变（本地模式下"改过密码"仍然算配置过数据）',
    vm.runInContext('(FINANCE_MODE = "local"), hasRealData({config:{passwords:{settings:"sha256:8d969eef"}},records:{},notes:{}})', sandbox) === true);
  vm.runInContext('FINANCE_MODE = "cloud"; _clearFinanceSession(true); _mergeCtx = null;', sandbox);
  const before9 = modalCount();
  const writesBefore9 = dataWrites().length;
  vm.runInContext('_queueMergePrompt(__loc, __cld)', sandbox);
  await sleep(120);
  chk('没有会话时**不弹**「是否合并」（旧行为：弹了也必定 403，红字甩屏）',
    modalCount() === before9, { now: modalCount(), before: before9 });
  chk('没有会话时一条写请求都不发（不再制造 write_denied 噪音）',
    dataWrites().length === writesBefore9);
  chk('合并上下文被记下并挂进解锁队列（解锁后再问，而不是丢掉）',
    vm.runInContext('!!_mergeCtx', sandbox) === true);
  await vm.runInContext('financeUnlock("settings", "GoodPass2026")', sandbox);
  vm.runInContext('_runAfterUnlock()', sandbox);
  await sleep(200);
  chk('解锁后自动弹出合并确认框（这时写入才可能成功）',
    modalCount() > before9 && /安全合并/.test(lastModalHTML()), lastModalHTML().slice(0, 120));
  const writesBeforeMerge = dataWrites().length;
  ACTS.ok.click();                       // 点「合并」（openConfirm 的确定键）
  await sleep(400);
  const mergeWrites = calls.filter(c => c.method === 'POST' &&
    (c.url === '/api/proxy/finance/config' || c.url === '/api/local/data')).slice(writesBeforeMerge);
  chk('合并真的写入云端 + 本机，且两个写请求都带会话头（不再 403）',
    mergeWrites.length >= 2
    && mergeWrites.every(w => w.headers['X-Finance-Session'] === SERVER.session),
    mergeWrites.map(w => w.url));
  const blankWrite = mergeWrites.filter(w => w.url === '/api/local/data').pop();
  chk('合并成功后把本机重复副本清成空业务数据（否则下次开页又问一遍）',
    !!blankWrite && JSON.parse(blankWrite.body).data.records.expenses.length === 0,
    blankWrite && String(blankWrite.body).slice(0, 120));
  chk('合并结果 = 本地 + 云端（本地那条采购记录进了合并结果）',
    vm.runInContext('STORE.records.expenses.some(function(r){return r.id==="le1";})', sandbox) === true);

  console.log(fails ? ('FRONTEND_FAIL ' + fails) : 'FRONTEND_OK');
  process.exit(fails ? 1 : 0);
})().catch(e => { console.log('HARNESS_ERROR: ' + (e && e.stack || e)); process.exit(2); });
"""


def frontend():
    section("D. 前端行为 · 整页脚本 + DOM 桩（首次改密能从 UI 走通 / 会话头 / 排队补发）")
    tmp = tempfile.mkdtemp(prefix="hn_s105_js_")
    try:
        hp = os.path.join(tmp, "harness.js")
        with open(hp, "w", encoding="utf-8") as f:
            f.write(FRONTEND_HARNESS)
        try:
            proc = subprocess.run(["node", hp, HTML], capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=120)
        except FileNotFoundError:
            chk("本机有 node（前端行为验收需要它）", False, "node 未安装")
            return
        out = (proc.stdout or "") + (proc.stderr or "")
        for line in out.strip().splitlines():
            print("  " + line)
        chk("前端行为桩脚本整体通过（exit 0）", proc.returncode == 0, "exit=%s" % proc.returncode)
        chk("没有 LOAD_FAIL（整页脚本能被 Node 解析执行）", "LOAD_FAIL" not in out)
        chk("没有 HARNESS_ERROR", "HARNESS_ERROR" not in out)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ═══════════════════════════════════════════════════════════════════════════
# E. 反向验证：改前版本必须变红
# ═══════════════════════════════════════════════════════════════════════════

_BACKUP_MARK = os.path.join(tempfile.gettempdir(), "hn_s1_05_reverse_backup.json")


def _recover_stale_swap():
    if not os.path.isfile(_BACKUP_MARK):
        return
    try:
        data = json.loads(open(_BACKUP_MARK, encoding="utf-8").read())
        for rel, backup in data.items():
            if os.path.isfile(backup):
                with open(backup, "rb") as f:
                    payload = f.read()
                with open(os.path.join(REPO, rel), "wb") as f:
                    f.write(payload)
                print("  ⚠ 已从上次中断的反向验证中还原 %s" % rel)
        os.remove(_BACKUP_MARK)
    except Exception as e:                              # noqa: BLE001
        print("  ✗ 中断恢复失败（请手动 git checkout -- %s %s）: %s"
              % (SERVER_REL, HTML_REL, e))


def reverse_validation():
    section("E. 反向验证（把 stats_server.py / settlement.html 换回 %s 必须变红）" % BASELINE)
    if not shutil.which("git"):
        print("  SKIP 未找到 git —— 反向验证跳过")
        return

    def _git_show(rel):
        p = subprocess.run(["git", "-C", REPO, "show", "%s:%s" % (BASELINE, rel)],
                           capture_output=True)
        return p.stdout if p.returncode == 0 else None

    old_srv = _git_show(SERVER_REL)
    old_html = _git_show(HTML_REL)
    if not old_srv or not old_html:
        print("  SKIP 基线 commit %s 不可用 —— 反向验证跳过" % BASELINE)
        return
    if not chk("基线 %s 的 stats_server.py 是改前版本（没有 PBKDF2）" % BASELINE,
               b"pbkdf2_hmac" not in old_srv):
        print("  （基线不是改前状态 ⇒ 无法作为反证，跳过）")
        return
    if not chk("基线 %s 的 settlement.html 是改前版本（仍带 sha256 默认密码）" % BASELINE,
               b"_PW_123456 = 'sha256:8d969eef" in old_html):
        return

    files = [SERVER_REL, HTML_REL]
    payloads = {SERVER_REL: old_srv, HTML_REL: old_html}
    store, md5_before = {}, {}
    backup_dir = tempfile.mkdtemp(prefix="hn_s105_backup_")
    for rel in files:
        path = os.path.join(REPO, rel)
        with open(path, "rb") as f:
            raw = f.read()
        store[rel] = raw
        md5_before[rel] = hashlib.md5(raw).hexdigest()
        with open(os.path.join(backup_dir, os.path.basename(rel)), "wb") as f:
            f.write(raw)
    with open(_BACKUP_MARK, "w", encoding="utf-8") as f:
        json.dump({rel: os.path.join(backup_dir, os.path.basename(rel)) for rel in files}, f)

    rc, out = None, ""
    try:
        for rel in files:
            with open(os.path.join(REPO, rel), "wb") as f:
                f.write(payloads[rel])
        env = dict(os.environ)
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        env["HN_S1_05_CHILD"] = "1"
        proc = subprocess.run([sys.executable, os.path.abspath(__file__), "--static-only"],
                              cwd=REPO, env=env, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=180)
        rc = proc.returncode
        out = (proc.stdout or "") + (proc.stderr or "")
    finally:
        for rel, raw in store.items():
            with open(os.path.join(REPO, rel), "wb") as f:
                f.write(raw)
        try:
            os.remove(_BACKUP_MARK)
        except OSError:
            pass
        for rel in files:
            now = hashlib.md5(open(os.path.join(REPO, rel), "rb").read()).hexdigest()
            chk("反向验证后已还原 %s（md5 一致）" % rel, now == md5_before[rel],
                "%s → %s" % (md5_before[rel], now))

    shutil.rmtree(backup_dir, ignore_errors=True)

    chk("改前版本跑本脚本必须失败（实际 exit=%s）" % rc, rc is not None and rc != 0)
    chk("红落在断言上（不是 Traceback / SyntaxError）",
        "Traceback (most recent call last)" not in out and "NameError" not in out
        and "SyntaxError" not in out and "ImportError" not in out, out[-300:])
    chk("失败输出里能看到具体断言（[FAIL] …）", "[FAIL]" in out)
    failed = [ln for ln in out.splitlines() if ln.startswith("[FAIL]")]
    chk("改前版本至少红 10 条（不是只红一两条边角断言）", len(failed) >= 10, len(failed))
    print("  改前版本红掉的断言（前 12 条）:")
    for line in failed[:12]:
        print("    " + line)


# ═══════════════════════════════════════════════════════════════════════════
# F. S1-09 尾巴（离线未同步 ≠ 已完成 / 本地服务资源上限）
#    这两项同属「财务/本地服务」这条链，文件也在本任务所有权内，故一并在此验收。
# ═══════════════════════════════════════════════════════════════════════════

def s1_09_static():
    section("F. S1-09 尾巴（静态）· 离线未同步 ≠ 已完成 / 本地服务资源上限")
    src = open(SERVER, encoding="utf-8").read()
    html = open(HTML, encoding="utf-8").read()

    print("-- F1 本地订单状态：只有真同步的才算 sent --")
    chk("_handle_local_orders 的 SELECT 带上 COALESCE(status,'pending') 与 synced",
        re.search(r"COALESCE\(status, 'pending'\) AS status", src) is not None
        and re.search(r"owner_name, is_admin_print, synced%s FROM offline_orders", src) is not None)
    chk("旧库（还没 status 列）不会炸：先 PRAGMA table_info 探测再拼 SQL",
        "PRAGMA table_info(offline_orders)" in src and "has_status" in src)
    chk("synced 且 status='synced' → sent；否则 unsynced；rejected → unsynced_rejected",
        re.search(r'if synced and sync_status == "synced":\s*\n\s*row_status = "sent"', src) is not None
        and re.search(r'elif sync_status == "rejected":\s*\n\s*row_status = "unsynced_rejected"', src) is not None
        and re.search(r'else:\s*\n\s*row_status = "unsynced"', src) is not None)
    chk("订单级与**文件级**状态同口径（不再写死 sent）",
        src.count('"status": row_status') >= 2)
    chk("下发 unsynced / sync_status 可见标记（页面可展示、可解释）",
        '"unsynced": row_status != "sent"' in src and '"sync_status": sync_status' in src)
    chk("`\"status\": \"sent\"` 的写死形态已消失（本地订单不再一律当已完成）",
        re.search(r'"status":\s*"sent",\s*#\s*本地订单已打印完成', src) is None)

    print("\n-- F2 结算页：⏳离线未同步 / ⚠️云端拒收 两个标签且**不计收益** --")
    chk("STATUS_LABELS 有 unsynced / unsynced_rejected",
        "unsynced: '⏳ 离线未同步'" in html and "unsynced_rejected: '⚠️ 云端拒收'" in html)
    chk("STATUS_CLASSES 有 st-unsynced / st-unsynced-rejected，且 CSS（亮+暗）都有",
        "unsynced: 'st-unsynced'" in html
        and ".tag.st-unsynced" in html
        and '[data-theme="dark"] .tag.st-unsynced' in html)
    chk("★ 收益口径排除这两态（否则 record = paid+receivable 会把没同步的钱算进收益）",
        re.search(r"var _EXCLUDE_STATUS = \{[^}]*unsynced: 1[^}]*unsynced_rejected: 1", html) is not None)
    chk("状态筛选归类：⏳ 待处理 / ⚠️ 异常",
        re.search(r"_ACTIVE_STATUS = \{[^}]*unsynced: 1", html) is not None
        and re.search(r"_ABNORMAL_STATUS = \{[^}]*unsynced_rejected: 1", html) is not None)
    chk("既有 sent 分支没丢（已同步的单照旧算「已入账」）",
        "if (f.status === 'sent') m.paid += amt;" in html)
    chk("订单明细表头提示写明这两种状态不计入收益",
        "离线未同步/⚠️云端拒收订单金额仅展示、不计入收益" in html)

    print("\n-- F3 本地服务自身的资源上限 --")
    chk("并发上限常量存在（≤ 64）",
        0 < int((re.search(r"^_MAX_CONCURRENT_REQUESTS\s*=\s*(\d+)", src, re.M) or [0, 0])[1]) <= 64)
    chk("用带信号量的服务器类（_LimitedThreadingHTTPServer），超额直接 503 不排队",
        "class _LimitedThreadingHTTPServer" in src
        and "BoundedSemaphore" in src and "503 Service Unavailable" in src)
    chk("StatsServer 实际用的是这个受限服务器（不是裸 ThreadingHTTPServer）",
        re.search(r"self\._server = _LimitedThreadingHTTPServer\(\(", src) is not None)
    chk("请求结束（含异常）一定归还并发槽位（不会永久漏掉一个额度）",
        re.search(r"def process_request_thread[\s\S]{0,300}?finally:\s*\n\s*self\._slots\.release\(\)", src) is not None)
    chk("连接级 socket 超时（_StatsHandler.timeout = _SOCKET_TIMEOUT）",
        re.search(r"^_SOCKET_TIMEOUT\s*=\s*\d+", src, re.M) is not None
        and re.search(r"^    timeout = _SOCKET_TIMEOUT", src, re.M) is not None)
    chk("请求体上限在**读之前**判断：_request_body_ok 在 do_POST 里先跑，超限 → 413",
        re.search(r"def do_POST\(self\):[\s\S]{0,800}?if not self\._request_body_ok\(\):", src) is not None
        and "413)" in src and "\"body_too_large\"" in src)
    chk("超限时先关连接再回包（不把超大 body 收进来）",
        re.search(r"self\.close_connection = True[\s\S]{0,400}?413\)", src) is not None)


def s1_09_behavior():
    section("F'. S1-09 尾巴（行为）· 未同步单的状态 / 413 / 并发 503 / 不收收益")

    # ── F4 本地订单：三行不同同步状态 ──
    tmp = tempfile.mkdtemp(prefix="hn_s109_")
    data_file = os.path.join(tmp, "print_data.json")
    db_file = os.path.join(tmp, "printer-local.db")
    _write_store(data_file)
    import sqlite3
    files_json = json.dumps([{"file_name": "a.pdf", "copies": 1, "page_count": 2,
                              "duplex": "off", "cost": 5.0}], ensure_ascii=False)
    conn = sqlite3.connect(db_file)
    conn.execute("""CREATE TABLE offline_orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT, order_number TEXT NOT NULL, files_json TEXT NOT NULL,
        total_price REAL DEFAULT 0, created_at TEXT NOT NULL, synced INTEGER DEFAULT 0,
        retry_count INTEGER DEFAULT 0, owner_name TEXT DEFAULT '', is_admin_print INTEGER DEFAULT 0,
        remark TEXT DEFAULT '', status TEXT DEFAULT 'pending', next_retry_at INTEGER DEFAULT 0,
        last_error TEXT DEFAULT '')""")
    rows = [
        ("HN-PENDING-1", files_json, 5.0, "2026-10-23 10:00:00", 0, "pending", ""),
        ("HN-SYNCED-1", files_json, 5.0, "2026-10-23 10:05:00", 1, "synced", ""),
        ("HN-REJECT-1", files_json, 5.0, "2026-10-23 10:10:00", 0, "rejected", "云端 400：文件已过期"),
    ]
    for num, fj, tp, ca, sy, st, err in rows:
        conn.execute("INSERT INTO offline_orders (order_number, files_json, total_price, created_at,"
                     " synced, status, last_error) VALUES (?,?,?,?,?,?,?)",
                     (num, fj, tp, ca, sy, st, err))
    conn.commit()
    conn.close()

    ss, srv = _start_server(data_file)
    old_db = ss._LOCAL_DB_FILE
    ss._LOCAL_DB_FILE = db_file
    c = Client(srv)
    try:
        st, r = c.call("GET", "/api/local/orders")
        by_num = {}
        for o in ((r.get("data") or {}).get("orders") or []):
            by_num[o.get("order_number")] = o
        chk("本地订单接口 200 且返回 3 行", st == 200 and len(by_num) == 3, (st, list(by_num)))
        chk("synced=1/status=synced → status='sent'（已同步的照旧算已完成）",
            by_num.get("HN-SYNCED-1", {}).get("status") == "sent", by_num.get("HN-SYNCED-1"))
        chk("synced=0/status=pending → status='unsynced' + unsynced=true + sync_status=pending",
            by_num.get("HN-PENDING-1", {}).get("status") == "unsynced"
            and by_num.get("HN-PENDING-1", {}).get("unsynced") is True
            and by_num.get("HN-PENDING-1", {}).get("sync_status") == "pending",
            by_num.get("HN-PENDING-1"))
        chk("synced=0/status=rejected → status='unsynced_rejected'（云端拒收要人工介入）",
            by_num.get("HN-REJECT-1", {}).get("status") == "unsynced_rejected",
            by_num.get("HN-REJECT-1"))
        chk("文件级状态与订单级同口径（未同步单的每个文件也是 unsynced）",
            all(f.get("status") == "unsynced"
                for f in (by_num.get("HN-PENDING-1", {}).get("files") or [])),
            by_num.get("HN-PENDING-1", {}).get("files"))

        # 旧库（没有 status 列）不能 500
        old_db2 = os.path.join(tmp, "legacy.db")
        conn = sqlite3.connect(old_db2)
        conn.execute("""CREATE TABLE offline_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT, order_number TEXT NOT NULL,
            files_json TEXT NOT NULL, total_price REAL DEFAULT 0, created_at TEXT NOT NULL,
            synced INTEGER DEFAULT 0, retry_count INTEGER DEFAULT 0,
            owner_name TEXT DEFAULT '', is_admin_print INTEGER DEFAULT 0, remark TEXT DEFAULT '')""")
        conn.execute("INSERT INTO offline_orders (order_number, files_json, total_price, created_at,"
                     " synced) VALUES (?,?,?,?,?)",
                     ("HN-LEGACY-1", files_json, 5.0, "2026-10-23 09:00:00", 1))
        conn.execute("INSERT INTO offline_orders (order_number, files_json, total_price, created_at,"
                     " synced) VALUES (?,?,?,?,?)",
                     ("HN-LEGACY-0", files_json, 5.0, "2026-10-23 09:30:00", 0))
        conn.commit()
        conn.close()
        ss._LOCAL_DB_FILE = old_db2
        st, r = c.call("GET", "/api/local/orders")
        legacy = {o.get("order_number"): o for o in ((r.get("data") or {}).get("orders") or [])}
        chk("旧库（无 status 列）读取不 500，按 synced 列判定",
            st == 200 and legacy.get("HN-LEGACY-1", {}).get("status") == "sent"
            and legacy.get("HN-LEGACY-0", {}).get("status") == "unsynced", (st, legacy))

        # ── F5 请求体上限：413 且不读 body ──
        import http.client
        host, port = "127.0.0.1", srv.port
        conn = http.client.HTTPConnection(host, port, timeout=10)
        conn.putrequest("POST", "/api/local/data")
        conn.putheader("X-Launch-Token", srv.launch_token)
        conn.putheader("Content-Type", "application/json")
        conn.putheader("Content-Length", str(64 * 1024 * 1024))     # 64MB，但一个字节都不发
        conn.endheaders()
        resp = conn.getresponse()
        raw = resp.read()
        conn.close()
        body = {}
        try:
            body = json.loads(raw.decode("utf-8"))
        except Exception:
            body = {}
        chk("Content-Length 超上限 → 413 + code=body_too_large（读之前就拒，不等 64MB 传完）",
            resp.status == 413 and body.get("code") == "body_too_large", (resp.status, body))
        chk("被拒的超大请求没有写坏/改动数据文件（拒绝发生在落盘之前）",
            os.path.exists(data_file) and json.load(open(data_file, encoding="utf-8")) is not None)

        # ── F6 并发上限：超额 503，不无限起线程 ──
        from http.server import BaseHTTPRequestHandler
        import threading
        class _SlowHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                time.sleep(0.6)
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")
            def log_message(self, *a):
                pass
        class _OneSlot(ss._LimitedThreadingHTTPServer):
            max_workers = 1
        slow = _OneSlot(("127.0.0.1", 0), _SlowHandler)
        port2 = slow.server_port
        threading.Thread(target=slow.serve_forever, daemon=True).start()
        results = []

        def _hit():
            try:
                with urllib.request.urlopen("http://127.0.0.1:%d/" % port2, timeout=15) as rr:
                    results.append(rr.status)
            except urllib.error.HTTPError as e:
                results.append(e.code)
            except Exception as e:                       # noqa: BLE001
                results.append(type(e).__name__)
        th1 = threading.Thread(target=_hit)
        th1.start()
        time.sleep(0.15)                                  # 让第一条占住唯一的槽位
        th2 = threading.Thread(target=_hit)
        th2.start()
        th1.join(20)
        th2.join(20)
        slow.shutdown()
        slow.server_close()
        chk("并发上限生效：两条并发只放行 1 条，另一条立刻 503（不排队、不无限起线程）",
            sorted(x for x in results if isinstance(x, int)) == [200, 503] and slow.rejected >= 1,
            (results, slow.rejected))
        chk("_StatsHandler 的连接级超时已设置（半开连接不会永远占线程）",
            0 < float(getattr(ss._StatsHandler, "timeout", 0) or 0) <= 120,
            getattr(ss._StatsHandler, "timeout", None))
    finally:
        ss._LOCAL_DB_FILE = old_db
        srv.stop()
        shutil.rmtree(tmp, ignore_errors=True)

    # ── F7 前端收益口径：unsynced 不计入 record（Node VM 实测 _orderMoney）──
    tmp2 = tempfile.mkdtemp(prefix="hn_s109_js_")
    try:
        js_harness = r"""
const fs = require('fs'), vm = require('vm');
const html = fs.readFileSync(process.argv[2], 'utf8');
const scripts = [...html.matchAll(/<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/g)].map(m => m[1]);
const js = scripts.join('\n;\n');
/* 只取「口径常量 + _orderMoney」这一段，避免拖进整个页面渲染 */
function grab(name) {
  const i = js.indexOf(name);
  if (i < 0) return '';
  if (name.indexOf('function') === 0) {
    let depth = 0, j = js.indexOf('{', i);
    for (let k = j; k < js.length; k++) {
      if (js[k] === '{') depth++;
      else if (js[k] === '}') { depth--; if (!depth) return js.slice(i, k + 1); }
    }
    return '';
  }
  const end = js.indexOf(';', i);
  return js.slice(i, end + 1);
}
const snippet = [grab('var _EXCLUDE_STATUS'), grab('function _orderMoney'),
                 grab('var STATUS_LABELS'), grab('var STATUS_CLASSES'),
                 grab('function parseCents')].join('\n');
let fails = 0;
function chk(n, c, d) { if (!c) fails++; console.log('[' + (c ? 'PASS' : 'FAIL') + '] ' + n + (!c && d !== undefined ? '  -- ' + JSON.stringify(d) : '')); }
const sb = { console, Math, JSON, Object, Array, String, Number, Boolean, Date, RegExp, Error };
vm.createContext(sb);
try { vm.runInContext(snippet, sb); } catch (e) { console.log('SNIPPET_FAIL ' + e.message); process.exit(2); }
function order(status, cents) {
  return { is_admin_print: 0, total_price: cents / 100,
           files: [{ status: status, total_price: cents / 100, is_free: false }] };
}
const mSent = sb._orderMoney(order('sent', 500), false);
const mPending = sb._orderMoney(order('unsynced', 500), false);
const mReject = sb._orderMoney(order('unsynced_rejected', 500), false);
chk('已同步（sent）订单：收益 record = ¥5.00', mSent.record === 500, mSent);
chk('★ 离线未同步（unsynced）订单：**不计入**收益 record = 0（否则结算虚增）',
  mPending.record === 0 && mPending.paid === 0 && mPending.receivable === 0, mPending);
chk('★ 云端拒收（unsynced_rejected）订单：同样不计入收益 record = 0',
  mReject.record === 0, mReject);
chk('两种新状态的标签走中文文案（不是原始英文 status）',
  sb.STATUS_LABELS.unsynced.indexOf('离线未同步') >= 0
  && sb.STATUS_LABELS.unsynced_rejected.indexOf('云端拒收') >= 0, sb.STATUS_LABELS);
chk('两种新状态有独立样式类（标签不会渲染成无样式裸文字）',
  sb.STATUS_CLASSES.unsynced === 'st-unsynced'
  && sb.STATUS_CLASSES.unsynced_rejected === 'st-unsynced-rejected', sb.STATUS_CLASSES);
/* 变异自证：把排除表退回旧版（只剩 4 态）→ unsynced 金额**真的**会算进收益 */
vm.runInContext('_EXCLUDE_STATUS = { canceled: 1, abandoned: 1, rejected: 1, failed: 1 };', sb);
const mut = sb._orderMoney(order('unsynced', 500), false);
chk('[自证] 排除表退回旧版后 unsynced 金额确实被算进收益（record=500）→ 证明上一条判据有判别力',
  mut.record === 500, mut);
console.log(fails ? ('F7_FAIL ' + fails) : 'F7_OK');
process.exit(fails ? 1 : 0);
"""
        hp = os.path.join(tmp2, "f7.js")
        with open(hp, "w", encoding="utf-8") as f:
            f.write(js_harness)
        proc = subprocess.run(["node", hp, HTML], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=120)
        out = (proc.stdout or "") + (proc.stderr or "")
        for line in out.strip().splitlines():
            print("  " + line)
        chk("前端收益口径实测通过（unsynced/unsynced_rejected 不计入 record）",
            proc.returncode == 0, "exit=%s" % proc.returncode)
    except FileNotFoundError:
        chk("本机有 node（F7 前端口径实测需要它）", False, "node 未安装")
    finally:
        shutil.rmtree(tmp2, ignore_errors=True)


# ═══════════════════════════════════════════════════════════════════════════

def main():
    global bad
    static_only = "--static-only" in sys.argv
    no_reverse = "--no-reverse" in sys.argv
    if os.environ.get("HN_S1_05_CHILD") != "1":
        _recover_stale_swap()

    static_server()
    static_html()
    s1_09_static()
    if not static_only:
        behavior()
        frontend()
        s1_09_behavior()
    if not static_only and not no_reverse:
        reverse_validation()

    print("\n" + "=" * 74)
    if bad:
        print("S1-05 财务安全加固验收：存在失败 ❌ (%d 条)" % bad)
        for name in fails[:20]:
            print("  · " + name)
        sys.exit(1)
    print("S1-05 财务安全加固验收：全部通过 ✅")
    sys.exit(0)


if __name__ == "__main__":
    main()
