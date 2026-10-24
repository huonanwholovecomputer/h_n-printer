#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""cve_scan.py — S3-04 CVE 扫描（pip-audit 包装 + 留档报告 + **SKIP 语义**）

依据：docs/安全加固修复计划_2026-10-23.md §S3-04（DS-24「CVE 订阅」）、
§8.2 S2 出口「pip-audit 无高危」、§S2-07（依赖已锁版本）。

为什么不是"跑一下 pip-audit 看看"
---------------------------------
发布门要用它，所以必须能区分三种完全不同的结论，并且写进文件留档：

    CLEAN  0  扫描完成、无已知漏洞          → 可以放行
    VULNS  3  扫描完成、发现漏洞（列清单）   → 不许放行，按修复版本升级
    SKIP   2  **工具缺失/超时/环境不完备**  → **未完成扫描，不得当作通过**

第 3 种最容易被"绿"掉：本机没装 pip-audit 时 subprocess 抛 FileNotFoundError，
随手 `except: return 0` 就等于把"没扫"变成"没问题"。所以 SKIP 必须是显式的
退出码 + 报告里写死"未完成扫描，不得当作通过"，季度报告也据此判红。

用法
----
    python ops/cve_scan.py                       # 扫 ../requirements.txt，写 ops/reports/cve-<ts>.json/.md
    python ops/cve_scan.py --requirements /path/requirements.txt
    python ops/cve_scan.py --pip-audit-cmd "C:/.../python.exe C:/tmp/fake_pip_audit.py"  # 测试桩
    python ops/cve_scan.py --offline-report ops/reports/cve-20261024T120000Z.json        # 只重渲染 md

pip-audit 调用：`<cmd> -r <requirements> --format json --progress-spinner off`
（不读 stdout 的进度条；JSON 里 `dependencies[].vulns[]` 是唯一真相来源。）

退出码：0 无漏洞 ｜ 3 有漏洞 ｜ 2 工具缺失/超时（SKIP）｜ 1 参数或解析错误。
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import shlex
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_REQ = os.path.abspath(os.path.join(HERE, "..", "requirements.txt"))
DEFAULT_REPORT_DIR = os.path.join(HERE, "reports")
DEFAULT_CMD = "pip-audit"
SCAN_TIMEOUT_SEC = 300
SKIP_WORDING = "未完成扫描，不得当作通过"
STATUS_CLEAN = "CLEAN"
STATUS_VULNS = "VULNS"
STATUS_SKIP = "SKIP"
STATUS_ERROR = "ERROR"


# ────────────────────────── 小工具 ──────────────────────────

def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def now_iso() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def ts_token() -> str:
    """文件名用的 UTC 时间戳（同目录下按文件名排序 == 按时间排序）。"""
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def split_cmd(text: str) -> list:
    """把 `--pip-audit-cmd` 拆成 argv。

    Windows 上必须用 posix=False：POSIX 模式下 `C:\\path\\python.exe` 的反斜杠会被
    当成转义符吃掉（`C:pathpython.exe`）。posix=False 会保留引号，所以再手工剥一层。
    """
    text = (text or "").strip()
    if not text:
        return []
    toks = shlex.split(text, posix=(os.name != "nt"))
    out = []
    for t in toks:
        if len(t) >= 2 and t[0] == t[-1] and t[0] in "\"'":
            t = t[1:-1]
        if t:
            out.append(t)
    return out


def _child_env() -> dict:
    env = dict(os.environ)
    # 与被测系统同口径：PYTHONPATH 置空（仓库里有注入 shim，会改写 os.remove 等行为）
    env["PYTHONPATH"] = ""
    env["PYTHONUTF8"] = "1"
    return env


def _run(cmd: list, timeout: int):
    """返回 (returncode, stdout, stderr, kind)；kind ∈ ok/timeout/not_found/oserror。"""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout, env=_child_env())
        return p.returncode, p.stdout or "", p.stderr or "", "ok"
    except subprocess.TimeoutExpired:
        return None, "", "超时 %ss 未返回" % timeout, "timeout"
    except FileNotFoundError as e:
        return None, "", "命令不存在：%s" % e, "not_found"
    except OSError as e:
        return None, "", "%s: %s" % (type(e).__name__, e), "oserror"


def tool_version(cmd: list) -> str:
    """尽力取工具版本；取不到写 unknown（不因此中断扫描）。"""
    rc, out, err, kind = _run(cmd + ["--version"], 30)
    if kind != "ok" or rc != 0:
        return "unknown"
    line = (out or err or "").strip().splitlines()
    line = line[0].strip() if line else ""
    if not line or line.startswith("{") or line.startswith("["):
        return "unknown"
    return line[:120]


def _first_json(text: str):
    """从可能夹着日志的输出里取出第一个 JSON 对象（大括号配平，忽略字符串内括号）。"""
    if not text:
        return None
    start = text.find("{")
    while start != -1:
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start:i + 1])
                    except json.JSONDecodeError:
                        break
        start = text.find("{", start + 1)
    return None


# ────────────────────────── 解析 pip-audit JSON ──────────────────────────

def normalize_audit(data) -> dict:
    """把 pip-audit 的 JSON 归一成 {dependencies, vulns[], counts}。

    兼容两种形状：
      · `{"dependencies": [{"name","version","vulns":[{"id","fix_versions",...}]}]}`
      · 更老的扁平列表 `[{"name","version","vulns":[...]}]`
    """
    if isinstance(data, list):
        deps = data
    elif isinstance(data, dict):
        deps = data.get("dependencies")
        if deps is None and isinstance(data.get("vulns"), list):
            # 没见过但兜一下：顶层直接给漏洞列表
            deps = [{"name": "?", "version": "?", "vulns": data["vulns"]}]
    else:
        deps = None
    if not isinstance(deps, list):
        raise ValueError("JSON 里找不到 dependencies 数组（pip-audit --format json 契约变了？）")

    vulns = []
    vulnerable_pkgs = set()
    for dep in deps:
        if not isinstance(dep, dict):
            continue
        name = str(dep.get("name") or "?")
        version = str(dep.get("version") or "?")
        dv = dep.get("vulns") or []
        if not isinstance(dv, list):
            continue
        if dv:
            vulnerable_pkgs.add(name)
        for v in dv:
            if not isinstance(v, dict):
                continue
            fix = v.get("fix_versions") or []
            if isinstance(fix, str):
                fix = [fix]
            aliases = v.get("aliases") or []
            if isinstance(aliases, str):
                aliases = [aliases]
            vulns.append({
                "package": name,
                "version": version,
                "id": str(v.get("id") or "?"),
                "fix_versions": [str(x) for x in fix],
                "aliases": [str(x) for x in aliases],
                "description": " ".join(str(v.get("description") or "").split())[:400],
            })
    return {
        "dependencies": len(deps),
        "vulnerable_packages": len(vulnerable_pkgs),
        "vulns": vulns,
        "counts": {"dependencies": len(deps),
                   "vulnerable_packages": len(vulnerable_pkgs),
                   "vulns": len(vulns)},
    }


# ────────────────────────── 报告渲染 ──────────────────────────

def render_md(report: dict) -> str:
    st = report.get("status", STATUS_ERROR)
    badge = {STATUS_CLEAN: "✅ 干净（未发现已知漏洞）",
             STATUS_VULNS: "🔴 发现漏洞，必须升级后重新扫描",
             STATUS_SKIP: "⚪ **SKIP — %s**" % SKIP_WORDING,
             STATUS_ERROR: "⚠️ 扫描失败（解析/参数错误）"}.get(st, st)
    summary = report.get("summary") or {}
    vulns = report.get("vulns") or []
    lines = [
        "# CVE 扫描报告 — %s" % report.get("generated_at", ""),
        "",
        "- 状态：%s" % badge,
        "- 退出码：`%s`" % report.get("exit_code", ""),
    ]
    if st == STATUS_SKIP:
        lines.append("- **注意：本次没有完成扫描（工具缺失/超时/环境不完备）。"
                     "缺扫描 ≠ 无漏洞，发布门必须按红处理。**")
        if report.get("skip_reason"):
            lines.append("- SKIP 原因：%s" % report["skip_reason"])
    lines += [
        "- requirements：`%s`" % report.get("requirements", ""),
        "- requirements SHA-256：`%s`" % report.get("requirements_sha256", ""),
        "- 工具：%s" % report.get("tool", {}).get("version", "unknown"),
        "- 命令：`%s`" % " ".join(report.get("tool", {}).get("cmd") or []),
        "- 依赖数：%s ｜ 有漏洞的包：%s ｜ 漏洞数：%s"
        % (summary.get("dependencies", "?"), summary.get("vulnerable_packages", "?"),
           summary.get("vulns", len(vulns))),
        "",
    ]
    if vulns:
        lines += ["## 漏洞清单", "",
                  "| 包 | 版本 | 漏洞 ID | 修复版本 | 别名 |",
                  "|---|---|---|---|---|"]
        for v in vulns:
            lines.append("| %s | %s | %s | %s | %s |" % (
                v.get("package", ""), v.get("version", ""), v.get("id", ""),
                ", ".join(v.get("fix_versions") or []) or "无（需评估替代/隔离）",
                ", ".join(v.get("aliases") or []) or "—"))
        hard = [v for v in vulns if not (v.get("fix_versions") or [])]
        if hard:
            lines += ["", "**无修复版本的漏洞（%d）**：需要人工评估——升级到上游新版本、"
                          "移除依赖，或明确登记为已接受残余风险。" % len(hard)]
        lines += ["", "升级命令（示例，逐个包确认后执行）：", "", "```bash"]
        for v in vulns:
            if v.get("fix_versions"):
                lines.append("pip install --upgrade '%s>==%s'"
                             % (v["package"], v["fix_versions"][0]))
        lines += ["```"]
    elif st == STATUS_CLEAN:
        lines += ["未发现已知漏洞。", ""]
    else:
        lines += ["本次没有可用的漏洞清单（扫描未完成或失败）。", ""]
    err = report.get("error") or ""
    if err:
        lines += ["## 错误详情", "", "```", str(err)[:2000], "```", ""]
    lines += ["---", "",
              "扫描口径：`pip-audit -r <requirements> --format json --progress-spinner off`；"
              "漏洞结论只来自 JSON 的 `dependencies[].vulns[]`，不解析控制台文案。", ""]
    return "\n".join(lines)


def write_reports(report: dict, report_dir: str, stem: str) -> tuple:
    os.makedirs(report_dir, exist_ok=True)
    json_path = os.path.join(report_dir, stem + ".json")
    md_path = os.path.join(report_dir, stem + ".md")
    with open(json_path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
        f.write("\n")
    with open(md_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(render_md(report))
    return json_path, md_path


def unique_stem(report_dir: str, base: str) -> str:
    """同一秒内跑两次也不互相覆盖（后缀 -2/-3…，文件名排序仍等价于时间序）。"""
    stem, i = base, 1
    while os.path.exists(os.path.join(report_dir, stem + ".json")):
        i += 1
        stem = "%s-%d" % (base, i)
    return stem


# ────────────────────────── 主流程 ──────────────────────────

def scan(requirements: str, cmd: list, timeout: int = SCAN_TIMEOUT_SEC) -> dict:
    """跑一次真实扫描并返回报告 dict（不落盘）。"""
    requirements = os.path.abspath(requirements)
    report = {
        "ok": False,
        "status": STATUS_ERROR,
        "generated_at": now_iso(),
        "requirements": requirements,
        "requirements_sha256": "",
        "tool": {"cmd": list(cmd) + ["-r", requirements, "--format", "json",
                                     "--progress-spinner", "off"],
                 "version": "unknown", "returncode": None},
        "summary": {"dependencies": 0, "vulnerable_packages": 0, "vulns": 0},
        "vulns": [],
        "skip_reason": "",
        "error": "",
        "raw": None,
    }
    if not os.path.isfile(requirements):
        report["error"] = "requirements 文件不存在：%s" % requirements
        report["status"] = STATUS_ERROR
        report["exit_code"] = 1
        return report
    report["requirements_sha256"] = sha256_file(requirements)

    report["tool"]["version"] = tool_version(cmd)
    rc, out, err, kind = _run(report["tool"]["cmd"], timeout)
    report["tool"]["returncode"] = rc
    if kind in ("not_found", "timeout", "oserror"):
        report["status"] = STATUS_SKIP
        report["skip_reason"] = ("扫描工具不可用（%s）：%s"
                                 % ({"not_found": "命令不存在",
                                     "timeout": "执行超时",
                                     "oserror": "系统错误"}[kind], err))
        report["error"] = report["skip_reason"]
        report["exit_code"] = 2
        return report

    data = _first_json(out)
    if data is None:
        # 命令跑了但没给出可解析的 JSON：先看它是不是自己崩了（rc 非 0/1）
        # pip-audit 在"发现漏洞"时也可能返回 1，所以 1 不算崩溃。
        if rc not in (0, 1):
            report["status"] = STATUS_SKIP
            report["skip_reason"] = ("pip-audit 异常退出（rc=%s），未产出 JSON：%s"
                                     % (rc, (err or out or "").strip()[-500:]))
            report["error"] = report["skip_reason"]
            report["exit_code"] = 2
            return report
        report["status"] = STATUS_ERROR
        report["error"] = ("无法解析 pip-audit 的 JSON 输出（rc=%s）：%s"
                           % (rc, (out or err or "").strip()[:1500]))
        report["exit_code"] = 1
        return report

    try:
        norm = normalize_audit(data)
    except ValueError as e:
        report["status"] = STATUS_ERROR
        report["error"] = "解析失败：%s" % e
        report["exit_code"] = 1
        return report

    report["raw"] = data
    report["summary"] = norm["counts"]
    report["vulns"] = norm["vulns"]
    if norm["vulns"]:
        report["status"] = STATUS_VULNS
        report["ok"] = False
        report["exit_code"] = 3
    else:
        report["status"] = STATUS_CLEAN
        report["ok"] = True
        report["exit_code"] = 0
    return report


def _print_summary(report: dict, json_path: str = "", md_path: str = "") -> None:
    st = report.get("status")
    print("[CVE] 状态：%s" % st)
    print("[CVE] requirements：%s（sha256 %s…）"
          % (report.get("requirements"), (report.get("requirements_sha256") or "")[:12]))
    print("[CVE] 工具：%s ｜ 命令：%s"
          % (report.get("tool", {}).get("version"),
             " ".join(report.get("tool", {}).get("cmd") or [])))
    s = report.get("summary") or {}
    print("[CVE] 依赖 %s 个 ｜ 有漏洞的包 %s 个 ｜ 漏洞 %s 个"
          % (s.get("dependencies"), s.get("vulnerable_packages"), s.get("vulns")))
    for v in (report.get("vulns") or [])[:20]:
        print("  - %s %s  %s  修复：%s" % (
            v.get("package"), v.get("version"), v.get("id"),
            ", ".join(v.get("fix_versions") or []) or "无"))
    if st == STATUS_SKIP:
        print("[CVE] SKIP：%s" % report.get("skip_reason"))
        print("[CVE] ⚠ %s —— 必须换到装了 pip-audit 且能联网的机器上重跑" % SKIP_WORDING)
    if json_path:
        print("[CVE] 报告：%s" % json_path)
        print("[CVE]      ：%s" % md_path)


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001  pragma: no cover
        pass

    ap = argparse.ArgumentParser(
        description="S3-04 CVE 扫描（pip-audit 包装；工具缺失按 SKIP 退出码 2，绝不谎报绿）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="退出码：0 无漏洞；3 有漏洞；2 工具缺失/超时（SKIP）；1 参数/解析错误")
    ap.add_argument("--requirements", default=DEFAULT_REQ,
                    help="依赖清单（默认脚本上一级 requirements.txt）")
    ap.add_argument("--pip-audit-cmd", default=DEFAULT_CMD,
                    help="pip-audit 命令（默认 pip-audit；可用引号传 '绝对python 假脚本.py' 供测试）")
    ap.add_argument("--report-dir", default=DEFAULT_REPORT_DIR,
                    help="报告目录（默认 ops/reports）")
    ap.add_argument("--offline-report", default="",
                    help="只重渲染已有报告 JSON（不联网、不调用 pip-audit）")
    ap.add_argument("--timeout", type=int, default=SCAN_TIMEOUT_SEC,
                    help="扫描超时秒（默认 %d）" % SCAN_TIMEOUT_SEC)
    args = ap.parse_args(argv)

    # ── 离线重渲染 ──
    if args.offline_report:
        path = os.path.abspath(args.offline_report)
        if not os.path.isfile(path):
            print("[CVE] 错误：报告不存在：%s" % path, file=sys.stderr)
            return 1
        try:
            with open(path, "r", encoding="utf-8") as f:
                report = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            print("[CVE] 错误：报告不可解析：%s" % e, file=sys.stderr)
            return 1
        stem = os.path.splitext(os.path.basename(path))[0]
        json_path, md_path = write_reports(report, os.path.dirname(path), stem)
        _print_summary(report, json_path, md_path)
        return int(report.get("exit_code", 1))

    cmd = split_cmd(args.pip_audit_cmd)
    if not cmd:
        print("[CVE] 错误：--pip-audit-cmd 为空", file=sys.stderr)
        return 1

    report = scan(args.requirements, cmd, args.timeout)
    out_dir = os.path.abspath(args.report_dir)
    os.makedirs(out_dir, exist_ok=True)
    stem = unique_stem(out_dir, "cve-" + ts_token())
    # 先把自身路径写进报告，再一次性落盘（避免写两遍）
    report["report_json"] = os.path.join(out_dir, stem + ".json")
    report["report_md"] = os.path.join(out_dir, stem + ".md")
    json_path, md_path = write_reports(report, out_dir, stem)
    _print_summary(report, json_path, md_path)
    return int(report.get("exit_code", 1))


if __name__ == "__main__":
    sys.exit(main())
