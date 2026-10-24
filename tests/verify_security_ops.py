# -*- coding: utf-8 -*-
"""验收 · S3-04 安全运营（审计日志之外的半个）：凭据轮换日历 + CVE 扫描 + 季度报告/发布门

依据：docs/安全加固修复计划_2026-10-23.md §S3-04（DS-24）、§11.4（季度报告 + 演练记录）、
决策表 D-1 / D-3 / D-8。

交付物：
  · `ops/credential_rotation.py` + `ops/credential_rotation.json`
    —— 只存元数据（**绝不含秘密值**）；`--check` 有 overdue/never → exit 1；
       `--mark` 原子写（临时文件 + os.replace）+ 写前 `.bak-<ts>` + `O_CREAT|O_EXCL` 文件锁；
       未知 id / 坏日期 / 负 interval / 缺字段 → exit 2；
  · `ops/cve_scan.py` —— pip-audit 包装：0 干净 / 3 有漏洞 / 2 工具缺失（SKIP，不谎报绿）/ 1 参数解析；
  · `ops/quarterly_ops_report.py` —— 汇总证据生成季度报告 + `--release-gate` 发布门；
  · `docs/安全运营手册_2026-10-24.md`、`docs/安全运营季度报告_2026-Q4.md`。

本脚本：
  A. 静态：登记表 schema/无秘密、原子写+备份+锁、CVE 退出码语义、季度报告章节与发布门判据、
     手册章节、季度报告章节、三个 CLI 的 --help。
  B. 行为：临时 registry（过期 → exit 1；--mark 后 → 0；未知 id/坏日期/坏 schema/秘密备注 → 2）；
     假 pip-audit（有漏洞 → 3 且报告含 CVE 编号；空 vulns → 0；命令不存在 → 2 且报告标 SKIP）；
     合成证据（全新鲜）→ 发布门 exit 0 且报告含全部章节；逐项破坏证据 → exit 1 且阻断项点名；
     伪造审计统计 CLI → 报告带数字。
  C. 变异自证：① 去掉发布门的 critical 判断 ⇒ 关键凭据过期场景的断言必须转红；
     ② 把 cve_scan 的「有漏洞 exit 3」改成 0 ⇒ 有漏洞场景必须转红。

用法：python tests/verify_security_ops.py     （exit 0 = 全部通过）
"""
import atexit
import datetime as dt
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(REPO, "mobile_apps", "printer-backend")
OPS = os.path.join(BACKEND, "ops")
PY = sys.executable

CRED = os.path.join(OPS, "credential_rotation.py")
REG = os.path.join(OPS, "credential_rotation.json")
CVE = os.path.join(OPS, "cve_scan.py")
QREP = os.path.join(OPS, "quarterly_ops_report.py")
REQ = os.path.join(BACKEND, "requirements.txt")
STAGING = os.path.join(REPO, "tests", "staging", "results", "99_run_all.json")
DOC_MANUAL = os.path.join(REPO, "docs", "安全运营手册_2026-10-24.md")
DOC_Q4 = os.path.join(REPO, "docs", "安全运营季度报告_2026-Q4.md")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _sec_harness as H  # noqa: E402

H.install_cleanup_hook()

TODAY = dt.date.today().isoformat()
REQUIRED_IDS = ["printer_global_token", "device_tokens", "login_salt", "download_salt",
                "finance_module_passwords", "admin_passwords", "ssh_deploy_key",
                "cos_subaccount_key", "apk_keystore", "backup_gpg_key", "tls_cert"]

_TMPS = []


def _mkdir(prefix="hn_s304_"):
    d = tempfile.mkdtemp(prefix=prefix)
    _TMPS.append(d)
    return d


def _cleanup():
    for d in _TMPS:
        shutil.rmtree(d, ignore_errors=True)
    _TMPS.clear()


atexit.register(_cleanup)


def _read(path):
    with open(path, "r", encoding="utf-8", newline="") as f:
        return f.read()


def _env():
    e = dict(os.environ)
    e["PYTHONPATH"] = ""
    e["PYTHONUTF8"] = "1"
    return e


def run(args, cwd=REPO, timeout=240):
    return subprocess.run([PY] + list(args), cwd=cwd, env=_env(), capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=timeout)


def run_py(script, args, cwd=REPO, timeout=240):
    return run([script] + list(args), cwd=cwd, timeout=timeout)


# ============================================================
# 夹具：登记表 / 证据
# ============================================================

def base_registry():
    with open(REG, "r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path, data):
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def make_registry(root, overrides=None, drop_field=None, interval=None,
                  name="reg.json"):
    """复制真实登记表并把所有条目设为"今天轮换过"，再按 overrides 造欠账。"""
    reg = base_registry()
    for it in reg["items"]:
        it["last_rotated"] = TODAY
    for iid, date in (overrides or {}).items():
        hit = [it for it in reg["items"] if it["id"] == iid]
        if not hit:
            raise AssertionError("overrides 里的 id 不在登记表：%s" % iid)
        hit[0]["last_rotated"] = date
    if drop_field:
        for it in reg["items"]:
            it.pop(drop_field, None)
    if interval is not None:
        reg["items"][0]["interval_days"] = interval
    path = os.path.join(root, name)
    write_json(path, reg)
    return path


def _iso_days_ago(days):
    return (dt.datetime.now().astimezone()
            - dt.timedelta(days=days)).isoformat(timespec="seconds")


def make_evidence(root, reg_overrides=None, cve_status="CLEAN", cve_age=0,
                  ha_age=0, ha_ok=True, rto=12.5, rpo=1.0, staging="ok",
                  run_all="pass"):
    """合成一套证据，返回 (paths dict)。默认全部新鲜/通过。"""
    os.makedirs(root, exist_ok=True)
    reports = os.path.join(root, "reports")
    os.makedirs(reports, exist_ok=True)
    paths = {"root": root, "reports": reports}
    paths["registry"] = make_registry(root, overrides=reg_overrides)

    cve = {
        "ok": cve_status == "CLEAN",
        "status": cve_status,
        "generated_at": _iso_days_ago(cve_age),
        "requirements": REQ,
        "requirements_sha256": hashlib.sha256(_read(REQ).encode("utf-8")).hexdigest(),
        "tool": {"cmd": ["pip-audit"], "version": "pip-audit 2.9.0"},
        "summary": {"dependencies": 12, "vulnerable_packages": 0, "vulns": 0},
        "vulns": [],
        "skip_reason": "合成证据：SKIP" if cve_status == "SKIP" else "",
    }
    paths["cve"] = os.path.join(reports, "cve-20261024T000000Z.json")
    write_json(paths["cve"], cve)

    ha = {
        "ok": ha_ok,
        "generated_at": _iso_days_ago(ha_age),
        "rto_min": rto,
        "rpo_min": rpo,
        "targets": {"rto_min": 30, "rpo_min": 5},
        "warm_standby": True,
    }
    paths["ha"] = os.path.join(reports, "ha_20261024T000000Z.json")
    write_json(paths["ha"], ha)

    staging_data = json.loads(_read(STAGING))
    if staging == "failed":
        for s in staging_data.get("scripts") or []:
            if str(s.get("script", "")).startswith("02"):
                s["rc"] = 1
        staging_data["failed"] = ["02_pool_saturation.py"]
    paths["staging"] = os.path.join(root, "staging.json")
    write_json(paths["staging"], staging_data)

    log = os.path.join(root, "run_all.log")
    if run_all == "pass":
        body = ("[ 1/40] PASS  tests/verify_x.py  (1.0s)\n" + "-" * 78 +
                "\n结果：PASS 40 / 40  （SKIP 6，已知预期红 0）   用时 320.0s\n")
    elif run_all == "partial":
        body = ("[40/40] FAIL  tests/verify_y.py  (1.0s)\n" + "-" * 78 +
                "\n结果：PASS 39 / 40  （SKIP 6，已知预期红 0）   用时 320.0s\n\n"
                "未通过：\n  [FAIL] tests/verify_y.py\n")
    else:
        body = ""
    if body:
        with open(log, "w", encoding="utf-8", newline="\n") as f:
            f.write(body)
    paths["run_all"] = log if body else ""

    paths["out"] = os.path.join(root, "q.md")
    return paths


def report_args(ev, out=None, audit_query=None, gate=True, period="2026-Q4"):
    args = ["--period", period,
            "--rotation-registry", ev["registry"],
            "--reports-dir", ev["reports"],
            "--staging-result", ev["staging"],
            "--out", out or ev["out"]]
    if ev.get("run_all"):
        args += ["--run-all-log", ev["run_all"]]
    if audit_query:
        args += ["--audit-query", audit_query]
    if gate:
        args.append("--release-gate")
    return args


# ============================================================
# A. 静态接线
# ============================================================
print("=== A. 静态接线（工具 / 登记表 / 文档）===")
cred_src = _read(CRED) if os.path.isfile(CRED) else ""
cve_src = _read(CVE) if os.path.isfile(CVE) else ""
q_src = _read(QREP) if os.path.isfile(QREP) else ""

H.chk("三个工具与登记表都存在",
      all(os.path.isfile(p) for p in (CRED, REG, CVE, QREP)),
      [p for p in (CRED, REG, CVE, QREP) if not os.path.isfile(p)])

reg = base_registry() if os.path.isfile(REG) else {"items": []}
reg_ids = [it.get("id") for it in reg.get("items") or []]
H.chk("★ 登记表覆盖全部必需凭据（11 项）+ schema 字段齐全",
      all(i in reg_ids for i in REQUIRED_IDS) and all(
          all(k in it for k in ("id", "name", "owner", "interval_days", "last_rotated",
                                "method", "critical", "evidence", "note"))
          for it in reg.get("items") or []),
      "缺少：%s" % [i for i in REQUIRED_IDS if i not in reg_ids])

# 秘密值扫描：登记表**绝不能**含凭据原文
secret_pats = [re.compile(r"(?i)(token|password|passwd|secret|api[_-]?key|private[_-]?key"
                          r"|authorization|access[_-]?key)\s*[:=]\s*\S{4,}"),
               re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
               re.compile(r"(?i)\bssh-(rsa|ed25519|ecdsa)\s+AAAA"),
               re.compile(r"\b[0-9a-fA-F]{32,}\b"),
               re.compile(r"\bAKID[0-9A-Za-z]{10,}\b")]
secret_hits = []
for it in reg.get("items") or []:
    for k, v in it.items():
        if isinstance(v, str) and any(p.search(v) for p in secret_pats):
            secret_hits.append("%s.%s" % (it.get("id"), k))
H.chk("★ 登记表里没有任何秘密值（token/密码/私钥/长 hex 一处都不许有）",
      not secret_hits, secret_hits)

H.chk("★ 原子写 + 写前备份 + 跨平台文件锁 三件套都在源码里",
      "os.replace(tmp, path)" in cred_src and ".bak-" in cred_src
      and "O_CREAT" in cred_src and "O_EXCL" in cred_src
      and "tempfile.mkstemp" in cred_src,
      {"os.replace": "os.replace(tmp, path)" in cred_src, ".bak-": ".bak-" in cred_src,
       "O_EXCL": "O_EXCL" in cred_src, "mkstemp": "tempfile.mkstemp" in cred_src})

H.chk("凭据 CLI 覆盖 --check/--list/--mark/--report/--registry/--warn-only",
      all(f in cred_src for f in ('"--check"', '"--list"', '"--mark"', '"--report"',
                                  '"--registry"', '"--warn-only"')))

H.chk("★ cve_scan 的 pip-audit 调用契约（-r / --format json / --progress-spinner off）",
      all(s in cve_src for s in ('"-r"', '"--format"', '"json"',
                                 '"--progress-spinner"', '"off"')))
H.chk("★ CVE 语义三档显式（CLEAN/VULNS/SKIP）+ SKIP 措辞「未完成扫描，不得当作通过」",
      "STATUS_CLEAN" in cve_src and "STATUS_VULNS" in cve_src and "STATUS_SKIP" in cve_src
      and "未完成扫描，不得当作通过" in cve_src
      and 'report["exit_code"] = 3' in cve_src and 'report["exit_code"] = 2' in cve_src)
H.chk("CVE 报告带 requirements SHA-256（不是「扫了个空」）",
      "sha256_file" in cve_src and "requirements_sha256" in cve_src
      and "hashlib.sha256" in cve_src)

H.chk("★ 季度报告用 subprocess 调审计 CLI（不直接 import 未落地的模块）",
      "subprocess.run(" in q_src and "import audit_query" not in q_src
      and '"--stats"' in q_src and '"--json"' in q_src
      and "审计模块未部署/未落地" in q_src)
H.chk("★ 发布门判据齐备（critical / overdue / never / CVE 30 天 / HA 90 天 / staging / run_all）",
      all(k in q_src for k in ("critical_bad", "CVE_MAX_AGE_DAYS = 30",
                               "HA_MAX_AGE_DAYS = 90", "staging", "run_all",
                               "关键凭据未就绪", "SKIP（未完成扫描")))
H.chk("季度报告按「缺失」降级而不是编数字（章节关键词齐全）",
      "缺失" in q_src and all(s in q_src for s in
                              ("## 1. 摘要", "## 2. 凭据轮换", "## 3. CVE",
                               "## 4. 高可用", "## 5. 审计", "## 6. DDoS",
                               "## 7. 发布前全量回归", "## 8. 发布门",
                               "## 9. 未做 / 人工清单", "## 10. 下季度动作")))
H.chk("季度报告从同目录 import 轮换模块（sys.path 处理），不复制判断逻辑",
      "credential_rotation" in q_src and "sys.path.insert(0, HERE)" in q_src)

for name, path in (("安全运营手册", DOC_MANUAL), ("首个季度报告", DOC_Q4)):
    H.chk("%s存在：%s" % (name, os.path.basename(path)), os.path.isfile(path))

if os.path.isfile(DOC_MANUAL):
    manual = _read(DOC_MANUAL)
    need = ["日", "周", "月", "季", "CVE", "pip-audit", "轮换", "审计",
            "tabletop", "16", "凭据泄漏", "磁盘", "orders.db",
            "99_run_all", "run_all", "证据", "90", "责任人", "WAF", "SIEM"]
    miss = [k for k in need if k not in manual]
    H.chk("安全运营手册覆盖：节奏表/CVE 订阅/轮换用法/审计入口/四场景/压测/发布清单/保留/边界",
          not miss, miss)

if os.path.isfile(DOC_Q4):
    q4 = _read(DOC_Q4)
    need = ["## 1. 摘要", "## 2. 凭据轮换", "## 3. CVE", "## 4. 高可用", "## 5. 审计",
            "## 6. DDoS", "## 7. 发布前全量回归", "## 8. 发布门",
            "## 9. 未做 / 人工清单", "## 10. 下季度动作", "RTO", "RPO", "三档",
            "2026-Q4", "缺失"]
    miss = [k for k in need if k not in q4]
    H.chk("首个季度报告含全部章节 + 真实证据路径 + 如实标「缺失」", not miss, miss)

for name, script in (("credential_rotation", CRED), ("cve_scan", CVE),
                     ("quarterly_ops_report", QREP)):
    p = run_py(script, ["--help"], timeout=120)
    H.chk("%s --help 可跑（exit 0）" % name, p.returncode == 0 and "usage" in p.stdout.lower(),
          "rc=%s stderr=%s" % (p.returncode, (p.stderr or "")[:200]))


# ============================================================
# B. 行为 · 凭据轮换
# ============================================================
print("\n=== B. 行为 · 凭据轮换日历 ===")
root1 = _mkdir()
reg_ok = make_registry(root1, name="fresh.json")
p = run_py(CRED, ["--check", "--registry", reg_ok])
H.chk("全新鲜登记表 --check → exit 0（PASS）",
      p.returncode == 0 and "PASS" in p.stdout, "rc=%s" % p.returncode)

# 按需/外部管理策略：APK keystore（换了会破坏覆盖升级）与 TLS（certbot 自动续期）
# 即使 last_rotated=null 也不该算欠账、更不该永久卡发布门。
reg_pol = make_registry(root1, overrides={"apk_keystore": None, "tls_cert": None},
                        name="policy.json")
p = run_py(CRED, ["--check", "--registry", reg_pol])
H.chk("★ 按需轮换（APK keystore）/ 自动续期（TLS）从不判欠账：--check exit 0",
      p.returncode == 0 and "按需/自动" in p.stdout
      and "关键凭据欠账" not in p.stdout, "rc=%s" % p.returncode)

overdue_date = (dt.date.today() - dt.timedelta(days=200)).isoformat()
reg_overdue = make_registry(root1, overrides={"printer_global_token": overdue_date},
                            name="overdue.json")
p = run_py(CRED, ["--check", "--registry", reg_overdue])
H.chk("★ 造一个过期项 → --check exit 1 且点名 id + 过期天数",
      p.returncode == 1 and "printer_global_token" in p.stdout
      and "过期" in p.stdout and "FAIL" in p.stdout, "rc=%s" % p.returncode)
H.chk("--check 表格含「到期日」列（发布/人工都要看哪天到期）",
      "到期日" in p.stdout and "过期天数" in p.stdout)

p = run_py(CRED, ["--check", "--registry", reg_overdue, "--warn-only"])
H.chk("--warn-only 时同样欠账也 exit 0（只提醒不拦）",
      p.returncode == 0 and "warn-only" in p.stdout, "rc=%s" % p.returncode)

reg_never = make_registry(root1, overrides={"device_tokens": None}, name="never.json")
p = run_py(CRED, ["--check", "--registry", reg_never])
H.chk("last_rotated=null ⇒ 显示「从未轮换」且 exit 1（诚实呈现欠账）",
      p.returncode == 1 and "从未轮换" in p.stdout, "rc=%s" % p.returncode)

# --mark：过期 → 今天 → 转绿；同时验证备份与原子写
root2 = _mkdir()
reg_mark = make_registry(root2, overrides={"printer_global_token": overdue_date},
                         name="mark.json")
before_check = run_py(CRED, ["--check", "--registry", reg_mark])
p = run_py(CRED, ["--mark", "printer_global_token", "--date", TODAY,
                  "--registry", reg_mark])
after_check = run_py(CRED, ["--check", "--registry", reg_mark])
mark_data = json.loads(_read(reg_mark))
item = [it for it in mark_data["items"] if it["id"] == "printer_global_token"][0]
backups = [f for f in os.listdir(root2) if ".bak-" in f]
tmps = [f for f in os.listdir(root2) if ".tmp-" in f]
H.chk("★ --mark 后 --check 由 exit 1 转为 exit 0（写入真的生效）",
      before_check.returncode == 1 and p.returncode == 0
      and after_check.returncode == 0 and item["last_rotated"] == TODAY,
      "before=%s mark=%s after=%s last=%s"
      % (before_check.returncode, p.returncode, after_check.returncode,
         item["last_rotated"]))
H.chk("★ 写前生成 .bak-<ts> 备份、且不残留 .tmp- 临时文件",
      bool(backups) and not tmps, {"backups": backups, "tmps": tmps})

before_bytes = _read(reg_mark)
p = run_py(CRED, ["--mark", "no_such_credential", "--registry", reg_mark])
H.chk("未知 id → exit 2，且登记表一个字节都没动",
      p.returncode == 2 and _read(reg_mark) == before_bytes,
      "rc=%s" % p.returncode)

for bad in ("2026-13-01", "2026-2-3", "tomorrow"):
    p = run_py(CRED, ["--mark", "tls_cert", "--date", bad, "--registry", reg_mark])
    H.chk("坏日期 %r → exit 2" % bad, p.returncode == 2, "rc=%s" % p.returncode)

p = run_py(CRED, ["--mark", "tls_cert", "--note",
                  "cloud_token=abcdef0123456789", "--registry", reg_mark])
H.chk("★ --note 疑似含秘密 → exit 2（登记表绝不落秘密）",
      p.returncode == 2 and "秘密" in (p.stdout + p.stderr), "rc=%s" % p.returncode)

reg_neg = make_registry(root2, interval=-5, name="neg.json")
p = run_py(CRED, ["--check", "--registry", reg_neg])
H.chk("负 interval → exit 2 且报 interval_days",
      p.returncode == 2 and "interval_days" in (p.stdout + p.stderr),
      "rc=%s" % p.returncode)

reg_missing = make_registry(root2, drop_field="evidence", name="missing.json")
p = run_py(CRED, ["--check", "--registry", reg_missing])
H.chk("缺字段 → exit 2 且报缺 evidence",
      p.returncode == 2 and "evidence" in (p.stdout + p.stderr),
      "rc=%s" % p.returncode)

p = run_py(CRED, ["--list", "--registry", reg_ok])
H.chk("--list 逐条列方式/证据（不只是 id）",
      p.returncode == 0 and "方式：" in p.stdout and "证据：" in p.stdout)
p = run_py(CRED, ["--report", "md", "--registry", reg_ok])
H.chk("--report md 输出 markdown 表",
      p.returncode == 0 and p.stdout.lstrip().startswith("**凭据轮换状态**")
      and "| ID |" in p.stdout)

p = run_py(CRED, ["--check"])
H.chk("真实登记表 --check 可跑（exit 0/1，且列出全部 11 个 id —— 现状是欠账）",
      p.returncode in (0, 1) and all(i in p.stdout for i in REQUIRED_IDS),
      "rc=%s" % p.returncode)


# ============================================================
# B. 行为 · CVE 扫描
# ============================================================
print("\n=== B. 行为 · CVE 扫描（假 pip-audit 注入）===")
root_cve = _mkdir()
fake_vuln = os.path.join(root_cve, "fake_vuln.py")
with open(fake_vuln, "w", encoding="utf-8", newline="\n") as f:
    f.write('import sys, json\n'
            'if "--version" in sys.argv:\n'
            '    print("pip-audit 2.9.0"); sys.exit(0)\n'
            'print(json.dumps({"dependencies": [\n'
            '  {"name": "flask", "version": "3.1.3", "vulns": [\n'
            '    {"id": "CVE-2026-1111", "fix_versions": ["3.1.4"],\n'
            '     "aliases": ["GHSA-test-0000"], "description": "fake"}]},\n'
            '  {"name": "pypdf", "version": "6.13.2", "vulns": []}],\n'
            '  "fixes": []}))\n')
fake_clean = os.path.join(root_cve, "fake_clean.py")
with open(fake_clean, "w", encoding="utf-8", newline="\n") as f:
    f.write('import sys, json\n'
            'if "--version" in sys.argv:\n'
            '    print("pip-audit 2.9.0"); sys.exit(0)\n'
            'print(json.dumps({"dependencies": [{"name": "flask", "version": "3.1.3",\n'
            '  "vulns": []}], "fixes": []}))\n')

reports_a = os.path.join(root_cve, "reports_a")
p = run_py(CVE, ["--pip-audit-cmd", "%s %s" % (PY, fake_vuln),
                 "--report-dir", reports_a])
jsons = sorted(f for f in os.listdir(reports_a) if f.endswith(".json")) if os.path.isdir(reports_a) else []
cve_rep = json.loads(_read(os.path.join(reports_a, jsons[-1]))) if jsons else {}
mds = sorted(f for f in os.listdir(reports_a) if f.endswith(".md")) if jsons else []
md_text = _read(os.path.join(reports_a, mds[-1])) if mds else ""
H.chk("★ 假 pip-audit 报一个漏洞 → exit 3，报告含 CVE 编号与修复版本",
      p.returncode == 3 and cve_rep.get("status") == "VULNS"
      and cve_rep.get("vulns", [{}])[0].get("id") == "CVE-2026-1111"
      and "CVE-2026-1111" in md_text and "3.1.4" in md_text
      and "| 包 | 版本 | 漏洞 ID | 修复版本" in md_text,
      "rc=%s status=%s" % (p.returncode, cve_rep.get("status")))
H.chk("CVE 报告 JSON 带 requirements SHA-256（与真实文件一致）",
      cve_rep.get("requirements_sha256") == hashlib.sha256(
          _read(REQ).encode("utf-8")).hexdigest(),
      cve_rep.get("requirements_sha256"))

reports_b = os.path.join(root_cve, "reports_b")
p = run_py(CVE, ["--pip-audit-cmd", "%s %s" % (PY, fake_clean),
                 "--report-dir", reports_b])
jsons_b = sorted(f for f in os.listdir(reports_b) if f.endswith(".json")) if os.path.isdir(reports_b) else []
cve_clean = json.loads(_read(os.path.join(reports_b, jsons_b[-1]))) if jsons_b else {}
H.chk("假 pip-audit 无漏洞 → exit 0 且 status=CLEAN",
      p.returncode == 0 and cve_clean.get("status") == "CLEAN"
      and cve_clean.get("ok") is True, "rc=%s" % p.returncode)

reports_c = os.path.join(root_cve, "reports_c")
p = run_py(CVE, ["--pip-audit-cmd", "hn-no-such-pip-audit-xyz",
                 "--report-dir", reports_c])
jsons_c = sorted(f for f in os.listdir(reports_c) if f.endswith(".json")) if os.path.isdir(reports_c) else []
cve_skip = json.loads(_read(os.path.join(reports_c, jsons_c[-1]))) if jsons_c else {}
mds_c = sorted(f for f in os.listdir(reports_c) if f.endswith(".md")) if jsons_c else []
skip_md = _read(os.path.join(reports_c, mds_c[-1])) if mds_c else ""
H.chk("★ 工具不存在 → exit 2（SKIP 语义），报告写明「未完成扫描，不得当作通过」",
      p.returncode == 2 and cve_skip.get("status") == "SKIP"
      and cve_skip.get("ok") is False and "未完成扫描，不得当作通过" in skip_md,
      "rc=%s status=%s" % (p.returncode, cve_skip.get("status")))

p = run_py(CVE, ["--offline-report", os.path.join(reports_a, jsons[-1])])
H.chk("--offline-report 只重渲染已有 JSON：有漏洞的仍 exit 3（不因离线而变绿）",
      p.returncode == 3, "rc=%s" % p.returncode)

p = run_py(CVE, ["--pip-audit-cmd", "%s %s" % (PY, fake_clean),
                 "--requirements", os.path.join(root_cve, "nope.txt"),
                 "--report-dir", os.path.join(root_cve, "reports_d")])
H.chk("requirements 不存在 → exit 1（参数/IO 错误，不是 SKIP 也不是绿）",
      p.returncode == 1, "rc=%s" % p.returncode)


# ============================================================
# B. 行为 · 季度报告 + 发布门
# ============================================================
print("\n=== B. 行为 · 季度报告 + 发布门 ===")
root_q = _mkdir()
ev = make_evidence(os.path.join(root_q, "ok"))
missing_audit = os.path.join(root_q, "no_audit_query.py")
p = run_py(QREP, report_args(ev, audit_query=missing_audit))
md = _read(ev["out"]) if os.path.isfile(ev["out"]) else ""
missing_sections = [s for s in ("## 1. 摘要", "## 2. 凭据轮换", "## 3. CVE",
                                "## 4. 高可用", "## 5. 审计", "## 6. DDoS",
                                "## 7. 发布前全量回归", "## 8. 发布门",
                                "## 9. 未做 / 人工清单", "## 10. 下季度动作")
                     if s not in md]
H.chk("★ 合成证据齐全 → --release-gate exit 0 且报告含全部 10 个章节",
      p.returncode == 0 and not missing_sections,
      {"rc": p.returncode, "missing": missing_sections})
ev_pol = make_evidence(os.path.join(root_q, "policy"),
                       reg_overrides={"apk_keystore": None, "tls_cert": None})
p = run_py(QREP, report_args(ev_pol))
md_pol = _read(ev_pol["out"]) if os.path.isfile(ev_pol["out"]) else ""
H.chk("★ 发布门不被「按需/自动」项卡住（否则 APK keystore 永远无法放行）",
      p.returncode == 0 and "按需/自动" in md_pol, "rc=%s" % p.returncode)
H.chk("报告里 RTO/RPO 实测 vs 目标都落在表里（不是只写一句话）",
      "RTO" in md and "RPO" in md and "12.5" in md and "30" in md and "5" in md)
H.chk("报告仍诚实标注缺失项（审计模块不存在 ⇒ 「缺失」并进人工清单）",
      "审计模块未部署/未落地" in md and "未做 / 人工清单" in md)

# 伪造审计 CLI：报告要带上真实数字
fake_audit = os.path.join(root_q, "audit_query.py")
with open(fake_audit, "w", encoding="utf-8", newline="\n") as f:
    f.write('import json, sys\n'
            'print(json.dumps({"ok": True, "files": 7, "lines": 123, "denied": 2,\n'
            '  "by_action": {"file_download": 100, "file_page": 23},\n'
            '  "by_result": {"ok": 121, "denied": 2},\n'
            '  "since": "2026-10-01", "until": "2026-10-24"}))\n')
ev2 = make_evidence(os.path.join(root_q, "audit"))
p = run_py(QREP, report_args(ev2, audit_query=fake_audit))
md2 = _read(ev2["out"]) if os.path.isfile(ev2["out"]) else ""
H.chk("★ 审计统计走 subprocess 契约（--stats --json）并把数字写进报告",
      p.returncode == 0 and "文件 7 个" in md2 and "行 123" in md2
      and "file_download" in md2, "rc=%s" % p.returncode)

# ① 关键凭据过期
ev3 = make_evidence(os.path.join(root_q, "cred_bad"),
                    reg_overrides={"printer_global_token": overdue_date})
p = run_py(QREP, report_args(ev3, audit_query=missing_audit))
md3 = _read(ev3["out"]) if os.path.isfile(ev3["out"]) else ""
H.chk("★ 关键凭据过期 → 发布门 exit 1，阻断项点名 printer_global_token",
      p.returncode == 1 and "printer_global_token" in md3
      and "关键凭据未就绪" in md3, "rc=%s" % p.returncode)

# ② 关键凭据从未轮换
ev4 = make_evidence(os.path.join(root_q, "cred_never"),
                    reg_overrides={"device_tokens": None})
p = run_py(QREP, report_args(ev4, audit_query=missing_audit))
md4 = _read(ev4["out"]) if os.path.isfile(ev4["out"]) else ""
H.chk("关键凭据「从未轮换」同样阻断（不只是过期才拦）",
      p.returncode == 1 and "device_tokens" in md4 and "从未轮换" in md4,
      "rc=%s" % p.returncode)

# ③ CVE 有漏洞
ev5 = make_evidence(os.path.join(root_q, "cve_vuln"), cve_status="VULNS")
json_files = [f for f in os.listdir(ev5["reports"]) if f.startswith("cve-")]
write_json(os.path.join(ev5["reports"], json_files[0]), {
    "ok": False, "status": "VULNS", "generated_at": _iso_days_ago(0),
    "requirements_sha256": "x" * 64, "tool": {"version": "pip-audit 2.9.0"},
    "summary": {"dependencies": 12, "vulnerable_packages": 1, "vulns": 1},
    "vulns": [{"package": "flask", "version": "3.1.3", "id": "CVE-2026-1111",
               "fix_versions": ["3.1.4"], "aliases": []}]})
p = run_py(QREP, report_args(ev5, audit_query=missing_audit))
md5 = _read(ev5["out"]) if os.path.isfile(ev5["out"]) else ""
H.chk("★ CVE 有漏洞 → 发布门 exit 1，阻断项点名 CVE 与漏洞数",
      p.returncode == 1 and "CVE：最新报告有" in md5 and "CVE-2026-1111" in md5,
      "rc=%s" % p.returncode)

# ④ CVE 报告过期（40 天前）
ev6 = make_evidence(os.path.join(root_q, "cve_stale"), cve_age=40)
p = run_py(QREP, report_args(ev6, audit_query=missing_audit))
md6 = _read(ev6["out"]) if os.path.isfile(ev6["out"]) else ""
H.chk("CVE 干净报告超过 30 天 → 发布门 exit 1（新鲜度也是判据）",
      p.returncode == 1 and "CVE：最新干净报告已 40 天" in md6, "rc=%s" % p.returncode)

# ⑤ CVE SKIP
ev7 = make_evidence(os.path.join(root_q, "cve_skip"), cve_status="SKIP")
p = run_py(QREP, report_args(ev7, audit_query=missing_audit))
md7 = _read(ev7["out"]) if os.path.isfile(ev7["out"]) else ""
H.chk("★ CVE SKIP（没扫成）→ 发布门 exit 1，绝不当作通过",
      p.returncode == 1 and "SKIP" in md7 and "不得当作通过" in md7,
      "rc=%s" % p.returncode)

# ⑥ HA 报告过期（100 天前）
ev8 = make_evidence(os.path.join(root_q, "ha_stale"), ha_age=100)
p = run_py(QREP, report_args(ev8, audit_query=missing_audit))
md8 = _read(ev8["out"]) if os.path.isfile(ev8["out"]) else ""
H.chk("HA 演练报告超过 90 天 → 发布门 exit 1",
      p.returncode == 1 and "HA 演练：最新报告已 100 天" in md8, "rc=%s" % p.returncode)

# ⑦ HA ok=false
ev9 = make_evidence(os.path.join(root_q, "ha_bad"), ha_ok=False)
p = run_py(QREP, report_args(ev9, audit_query=missing_audit))
md9 = _read(ev9["out"]) if os.path.isfile(ev9["out"]) else ""
H.chk("HA 报告 ok=false → 发布门 exit 1",
      p.returncode == 1 and "HA 演练：最新报告 ok=false" in md9, "rc=%s" % p.returncode)

# ⑧ RTO 超标
ev10 = make_evidence(os.path.join(root_q, "rto_bad"), rto=45.0)
p = run_py(QREP, report_args(ev10, audit_query=missing_audit))
md10 = _read(ev10["out"]) if os.path.isfile(ev10["out"]) else ""
H.chk("RTO 实测 45 min > 目标 30 min → 发布门 exit 1（ok=true 也必须拦）",
      p.returncode == 1 and "RTO 实测 45 min > 目标 30 min" in md10,
      "rc=%s" % p.returncode)

# ⑨ staging 失败
ev11 = make_evidence(os.path.join(root_q, "staging_bad"), staging="failed")
p = run_py(QREP, report_args(ev11, audit_query=missing_audit))
md11 = _read(ev11["out"]) if os.path.isfile(ev11["out"]) else ""
H.chk("★ staging 有失败脚本 → 发布门 exit 1，阻断项点名脚本",
      p.returncode == 1 and "02_pool_saturation.py" in md11, "rc=%s" % p.returncode)

# ⑩ run_all 未 PASS
ev12 = make_evidence(os.path.join(root_q, "runall_bad"), run_all="partial")
p = run_py(QREP, report_args(ev12, audit_query=missing_audit))
md12 = _read(ev12["out"]) if os.path.isfile(ev12["out"]) else ""
H.chk("run_all 只 PASS 39/40 → 发布门 exit 1（解析汇总行而不是看退出码脸色）",
      p.returncode == 1 and "run_all 未 PASS" in md12, "rc=%s" % p.returncode)

# ⑪ run_all 日志缺失
ev13 = make_evidence(os.path.join(root_q, "runall_missing"), run_all="")
p = run_py(QREP, report_args(ev13, audit_query=missing_audit))
md13 = _read(ev13["out"]) if os.path.isfile(ev13["out"]) else ""
H.chk("run_all 证据缺失 → 发布门 exit 1（缺证据不能当 PASS）",
      p.returncode == 1 and "run_all 证据缺失" in md13, "rc=%s" % p.returncode)

# ⑫ 缺报告（无 cve / 无 ha）时降级为「缺失」而不是崩
ev14 = make_evidence(os.path.join(root_q, "empty_reports"))
shutil.rmtree(ev14["reports"])
os.makedirs(ev14["reports"])
p = run_py(QREP, report_args(ev14, audit_query=missing_audit))
md14 = _read(ev14["out"]) if os.path.isfile(ev14["out"]) else ""
H.chk("证据目录为空 → 报告仍生成（exit 1），CVE/HA 两节写「缺失」而不是崩",
      p.returncode == 1 and "**缺失**" in md14 and "CVE：没有扫描报告" in md14
      and "HA 演练：没有报告" in md14, "rc=%s" % p.returncode)

# ⑬ 非 --release-gate 时只生成报告不拦
ev15 = make_evidence(os.path.join(root_q, "noflag"),
                    reg_overrides={"printer_global_token": overdue_date})
p = run_py(QREP, report_args(ev15, audit_query=missing_audit, gate=False))
H.chk("不带 --release-gate 时：有阻断也 exit 0（报告照出，门由 CI 决定）",
      p.returncode == 0 and os.path.isfile(ev15["out"]), "rc=%s" % p.returncode)

# ⑭ 坏 --period
p = run_py(QREP, ["--period", "2026-Q9"])
H.chk("坏 --period → exit 2", p.returncode == 2, "rc=%s" % p.returncode)


# ============================================================
# C. 变异自证
# ============================================================
print("\n=== C. 变异自证（改回去必须红）===")


def mutate(src_path, pairs, out_dir, extra_copy=None, out_name=None):
    os.makedirs(out_dir, exist_ok=True)
    dst = os.path.join(out_dir, out_name or os.path.basename(src_path))
    src = _read(src_path)
    for old, new in pairs:
        if old not in src:
            raise AssertionError("变异锚点未命中：%r" % old[:100])
        src = src.replace(old, new, 1)
    with open(dst, "w", encoding="utf-8", newline="\n") as f:
        f.write(src)
    if extra_copy:
        shutil.copy2(extra_copy, out_dir)
    return dst


# C1：去掉发布门的「关键凭据」判断 ⇒ 关键凭据过期场景必须转绿（证明断言抓得住）
mut_q_dir = os.path.join(_mkdir(), "ops")
mut_q = mutate(QREP, [('        crit = rot.get("critical_bad") or []',
                       '        crit = []  # MUTATION：去掉关键凭据判断')],
               mut_q_dir, extra_copy=CRED,
               out_name="quarterly_ops_report.py")
ev_m = make_evidence(os.path.join(root_q, "mut_c1"),
                     reg_overrides={"printer_global_token": overdue_date})
p_real = run_py(QREP, report_args(ev_m, audit_query=missing_audit,
                                 out=os.path.join(ev_m["root"], "real.md")))
p_mut = run_py(mut_q, report_args(ev_m, audit_query=missing_audit,
                                 out=os.path.join(ev_m["root"], "mut.md")))
H.chk("★ 变异①：删掉发布门的关键凭据判断 ⇒ 过期场景由 exit 1 变 exit 0（判据是活的）",
      p_real.returncode == 1 and p_mut.returncode == 0,
      {"real": p_real.returncode, "mutated": p_mut.returncode})

# C2：把 cve_scan 的「有漏洞 exit 3」改成 0 ⇒ 有漏洞场景必须转绿
mut_c_dir = os.path.join(_mkdir(), "ops")
mut_cve = mutate(CVE, [('        report["exit_code"] = 3',
                        '        report["exit_code"] = 0  # MUTATION')],
                 mut_c_dir, out_name="cve_scan.py")
reports_r = os.path.join(root_cve, "reports_real_c2")
reports_m = os.path.join(root_cve, "reports_mut_c2")
p_real = run_py(CVE, ["--pip-audit-cmd", "%s %s" % (PY, fake_vuln),
                      "--requirements", REQ, "--report-dir", reports_r])
p_mut = run_py(mut_cve, ["--pip-audit-cmd", "%s %s" % (PY, fake_vuln),
                         "--requirements", REQ, "--report-dir", reports_m])
H.chk("★ 变异②：cve_scan 的「有漏洞 exit 3」改成 0 ⇒ 有漏洞场景由 3 变 0（判据是活的）",
      p_real.returncode == 3 and p_mut.returncode == 0,
      {"real": p_real.returncode, "mutated": p_mut.returncode})

# C3：去掉 SKIP 语义（工具缺失按 0 返回）⇒ 工具缺失场景必须转绿
mut_cve2 = mutate(CVE, [('        report["exit_code"] = 2\n        return report',
                         '        report["exit_code"] = 0  # MUTATION\n        return report')],
                  os.path.join(_mkdir(), "ops"), out_name="cve_scan.py")
p_mut2 = run_py(mut_cve2, ["--pip-audit-cmd", "hn-no-such-pip-audit-xyz",
                           "--requirements", REQ,
                           "--report-dir", os.path.join(root_cve, "reports_mut_c3")])
H.chk("★ 变异③：把 SKIP 的 exit 2 改成 0 ⇒ 工具缺失场景由 2 变 0（SKIP 不可被吞）",
      p_mut2.returncode == 0, {"mutated": p_mut2.returncode})

# C4：把原子写的 os.replace 改成直接覆盖写 ⇒ 静态判据必须能红
mut_cred = mutate(CRED, [("        os.replace(tmp, path)",
                          "        shutil.copyfile(tmp, path)  # MUTATION")],
                  os.path.join(_mkdir(), "ops"), out_name="credential_rotation.py")
mut_src = _read(mut_cred)
H.chk("★ 变异④：把 os.replace 换成普通覆盖写 ⇒ 静态「原子写」判据转红",
      "os.replace(tmp, path)" not in mut_src,
      "变异后仍含 os.replace(tmp, path)（判据抓不住）")


# ============================================================
H.finish()
