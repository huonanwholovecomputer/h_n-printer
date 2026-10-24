#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""quarterly_ops_report.py — S3-04 安全运营季度报告 + 发布门

依据：docs/安全加固修复计划_2026-10-23.md §S3-04（DS-24）与 §11.4「季度报告 + 演练记录」。

这个脚本只做一件事：**把已有证据如实拼成一份可审计的季度报告**，并对缺证据/过期
证据给出明确的红/黄判定。它不生产证据，也不美化结论：

  · 凭据轮换：读 `ops/credential_rotation.py` 的 status（只 import，不重算规则）；
  · CVE：读 `ops/reports/cve-*.json`（最新一份；SKIP 不等于干净）；
  · 高可用：读 `ops/reports/ha_*.json`（RTO/RPO 实测 vs 目标）；
  · DDoS 三档回归：读 `tests/staging/results/99_run_all.json`；
  · run_all：解析 `--run-all-log` 的 `结果：PASS n/n` 与退出码；
  · 审计统计：**subprocess 调** `ops/audit_query.py --stats --json`（模块不存在就写「缺失」，
    绝不直接 import 一个还没落地的模块）。

证据缺失一律写「缺失」，并进「未做 / 人工清单」；`--release-gate` 时按 §S3-04 判据逐条
列阻断项并 exit 1 —— 发布门不会因为"报告生成了"就变绿。

用法
----
    python ops/quarterly_ops_report.py --period 2026-Q4
    python ops/quarterly_ops_report.py --period 2026-Q4 --release-gate
    python ops/quarterly_ops_report.py --period 2026-Q4 \
        --rotation-registry /tmp/reg.json --reports-dir /tmp/reports \
        --staging-result tests/staging/results/99_run_all.json \
        --run-all-log /tmp/run_all.log --audit-query /tmp/audit_query.py \
        --out /tmp/q.md

退出码：0 报告已生成（无 --release-gate 时恒 0）；1 发布门未通过；2 参数/IO 错误。
"""

from __future__ import annotations

import argparse
import datetime as dt
import glob
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.dirname(HERE)
REPO = os.path.dirname(os.path.dirname(BACKEND))
DEFAULT_REPORTS = os.path.join(HERE, "reports")
DEFAULT_ROTATION = os.path.join(HERE, "credential_rotation.json")
DEFAULT_STAGING = os.path.join(REPO, "tests", "staging", "results", "99_run_all.json")
DEFAULT_AUDIT_QUERY = os.path.join(HERE, "audit_query.py")

CVE_MAX_AGE_DAYS = 30          # CVE 报告新鲜度（发布门判据）
HA_MAX_AGE_DAYS = 90           # HA 演练新鲜度（发布门判据）
AUDIT_TIMEOUT_SEC = 120

SYM_OK, SYM_PART, SYM_MISS = "✅", "◐", "☐"

# 供测试与季度报告引用的「必须出现的章节」
REQUIRED_SECTIONS = (
    "## 1. 摘要",
    "## 2. 凭据轮换",
    "## 3. CVE",
    "## 4. 高可用",
    "## 5. 审计",
    "## 6. DDoS",
    "## 7. 发布前全量回归",
    "## 8. 发布门",
    "## 9. 未做 / 人工清单",
    "## 10. 下季度动作",
)


# ────────────────────────── 小工具 ──────────────────────────

def _rel(path: str) -> str:
    """仓库内路径转相对（便于报告里引用），空值给「—」，其余原样返回。"""
    if not path:
        return "—"
    try:
        p = os.path.abspath(path)
        if p.lower().startswith((REPO + os.sep).lower()):
            return os.path.relpath(p, REPO).replace("\\", "/")
    except Exception:  # noqa: BLE001
        pass
    return path.replace("\\", "/")


def _now() -> dt.datetime:
    return dt.datetime.now().astimezone()


def _iso(d: dt.datetime) -> str:
    return d.isoformat(timespec="seconds")


def parse_ts(value):
    """解析 ISO-8601 / 'YYYY-MM-DD HH:MM:SS' / 'YYYY-MM-DD'；失败返回 None。"""
    if value in (None, ""):
        return None
    s = str(value).strip().replace("Z", "+00:00")
    try:
        d = dt.datetime.fromisoformat(s)
    except ValueError:
        d = None
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d"):
            try:
                d = dt.datetime.strptime(str(value).strip(), fmt)
                break
            except ValueError:
                continue
        if d is None:
            return None
    if d.tzinfo is None:
        d = d.astimezone()          # 无时区按本机时区理解（报告里只用来算"多少天前"）
    return d


def age_days(when, now=None):
    if when is None:
        return None
    now = now or _now()
    return max(0, (now - when).days)


def read_json(path: str):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def latest_in(directory: str, pattern: str):
    """取目录里按文件名排序最新的一个（时间戳命名 ⇒ 等价时间序）。"""
    if not directory or not os.path.isdir(directory):
        return ""
    files = sorted(glob.glob(os.path.join(directory, pattern)))
    return files[-1] if files else ""


def first_json(text: str):
    """从可能夹日志的 stdout 里取第一个 JSON 对象。"""
    if not text:
        return None
    start = text.find("{")
    while start != -1:
        depth, in_str, esc = 0, False, False
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


def fmt_num(v, unit=""):
    if v is None:
        return "缺失"
    if isinstance(v, bool):
        return str(v)
    if isinstance(v, (int, float)):
        return ("%g" % v) + unit
    return str(v) + unit


# ────────────────────────── 期间 ──────────────────────────

def parse_period(text: str):
    m = re.fullmatch(r"(\d{4})-Q([1-4])", (text or "").strip())
    if not m:
        raise ValueError("--period 必须是 YYYY-QN（如 2026-Q4），收到 %r" % (text,))
    year, quarter = int(m.group(1)), int(m.group(2))
    if not (2000 <= year <= 2100):
        raise ValueError("--period 年份超出合理范围：%d" % year)
    return year, quarter


def default_period(today: dt.date = None) -> str:
    today = today or dt.date.today()
    return "%d-Q%d" % (today.year, (today.month - 1) // 3 + 1)


def period_range(year: int, quarter: int):
    start = dt.date(year, 3 * quarter - 2, 1)
    end = dt.date(year + (1 if quarter == 4 else 0), 1 if quarter == 4 else 3 * quarter + 1, 1)
    return start, end - dt.timedelta(days=1)


# ────────────────────────── 证据：凭据轮换 ──────────────────────────

def _load_rotation_module():
    """从同目录 import（quarterly 与 credential_rotation 是同级文件）。"""
    if HERE not in sys.path:
        sys.path.insert(0, HERE)
    import credential_rotation as rot  # noqa: PLC0415  （同级模块，延迟导入）
    return rot


def gather_rotation(path: str) -> dict:
    out = {"available": False, "path": path, "reason": "", "items": [],
           "counts": {}, "critical_bad": [], "never": [], "overdue": [],
           "ok": False, "today": ""}
    if not os.path.isfile(path):
        out["reason"] = "登记表不存在：%s" % _rel(path)
        return out
    try:
        rot = _load_rotation_module()
        st = rot.status(path=path)
    except Exception as e:  # noqa: BLE001
        out["reason"] = "登记表不可用：%s: %s" % (type(e).__name__, e)
        return out
    out.update({
        "available": True,
        "ok": st["ok"],
        "today": st["today"],
        "items": st["items"],
        "counts": st["counts"],
        "critical_bad": st["critical_bad"],
        "critical_overdue": st["critical_overdue"],
        "critical_never": st["critical_never"],
        "never": st["never"],
        "overdue": st["overdue"],
        "due_soon": st["due_soon"],
        "status_dict": st,
    })
    return out


# ────────────────────────── 证据：CVE ──────────────────────────

def gather_cve(reports_dir: str, explicit: str) -> dict:
    out = {"available": False, "path": "", "reason": "", "status": "",
           "age_days": None, "vulns": [], "counts": {}, "generated_at": "",
           "requirements_sha256": "", "tool": "", "clean": False, "fresh": False}
    path = explicit or latest_in(reports_dir, "cve-*.json")
    if not path:
        out["reason"] = "无 CVE 报告（%s 下没有 cve-*.json）" % _rel(reports_dir)
        return out
    out["path"] = path
    if not os.path.isfile(path):
        out["reason"] = "指定的 CVE 报告不存在：%s" % _rel(path)
        return out
    try:
        data = read_json(path)
    except (OSError, json.JSONDecodeError) as e:
        out["reason"] = "CVE 报告不可解析：%s" % e
        return out
    vulns = data.get("vulns") or []
    status = str(data.get("status") or "").upper()
    if not status:
        status = "CLEAN" if (data.get("ok") and not vulns) else ("VULNS" if vulns else "SKIP")
    when = parse_ts(data.get("generated_at")) or dt.datetime.fromtimestamp(
        os.path.getmtime(path)).astimezone()
    out.update({
        "available": True,
        "status": status,
        "vulns": vulns if isinstance(vulns, list) else [],
        "counts": data.get("summary") or {},
        "generated_at": data.get("generated_at") or "",
        "requirements_sha256": data.get("requirements_sha256") or "",
        "tool": (data.get("tool") or {}).get("version") or "",
        "when": when,
        "age_days": age_days(when),
        "clean": status == "CLEAN" and not vulns,
        "skip_reason": data.get("skip_reason") or data.get("error") or "",
    })
    out["fresh"] = bool(out["age_days"] is not None
                        and out["age_days"] <= CVE_MAX_AGE_DAYS)
    return out


# ────────────────────────── 证据：HA 演练 ──────────────────────────

def gather_ha(reports_dir: str, explicit: str) -> dict:
    out = {"available": False, "path": "", "reason": "", "ok": False,
           "age_days": None, "rto_min": None, "rpo_min": None, "targets": {},
           "rto_ok": None, "rpo_ok": None, "generated_at": "", "extra_keys": [],
           "fresh": False, "meets_target": False}
    path = explicit or latest_in(reports_dir, "ha_*.json")
    if not path:
        out["reason"] = "无 HA 演练报告（%s 下没有 ha_*.json）" % _rel(reports_dir)
        return out
    out["path"] = path
    if not os.path.isfile(path):
        out["reason"] = "指定的 HA 报告不存在：%s" % _rel(path)
        return out
    try:
        data = read_json(path)
    except (OSError, json.JSONDecodeError) as e:
        out["reason"] = "HA 报告不可解析：%s" % e
        return out
    if not isinstance(data, dict):
        out["reason"] = "HA 报告顶层不是对象"
        return out
    when = parse_ts(data.get("generated_at")) or dt.datetime.fromtimestamp(
        os.path.getmtime(path)).astimezone()
    targets = data.get("targets") if isinstance(data.get("targets"), dict) else {}
    rto = data.get("rto_min")
    rpo = data.get("rpo_min")
    t_rto = targets.get("rto_min")
    t_rpo = targets.get("rpo_min")
    rto_ok = None if (rto is None or t_rto is None) else bool(float(rto) <= float(t_rto))
    rpo_ok = None if (rpo is None or t_rpo is None) else bool(float(rpo) <= float(t_rpo))
    out.update({
        "available": True,
        "ok": bool(data.get("ok")),
        "generated_at": data.get("generated_at") or "",
        "when": when,
        "age_days": age_days(when),
        "rto_min": rto,
        "rpo_min": rpo,
        "targets": targets,
        "rto_ok": rto_ok,
        "rpo_ok": rpo_ok,
        "extra_keys": sorted(k for k in data.keys()
                             if k not in ("ok", "generated_at", "rto_min", "rpo_min",
                                          "targets")),
    })
    out["fresh"] = bool(out["age_days"] is not None and out["age_days"] <= HA_MAX_AGE_DAYS)
    out["meets_target"] = bool(out["ok"] and out["fresh"]
                               and rto_ok is not False and rpo_ok is not False)
    return out


# ────────────────────────── 证据：staging（DDoS 回归） ──────────────────────────

def gather_staging(path: str) -> dict:
    out = {"available": False, "path": path, "reason": "", "scripts": [],
           "failed": [], "pass": False, "tiers": {}, "tier_lines": [],
           "gevent": {}, "total_seconds": None}
    if not path or not os.path.isfile(path):
        out["reason"] = "staging 结果不存在：%s" % _rel(path or "")
        return out
    try:
        data = read_json(path)
    except (OSError, json.JSONDecodeError) as e:
        out["reason"] = "staging 结果不可解析：%s" % e
        return out
    scripts = data.get("scripts") or []
    if not isinstance(scripts, list):
        scripts = []
    failed = data.get("failed") or [s.get("script") for s in scripts
                                    if isinstance(s, dict) and s.get("rc") not in (0,)]
    failed = [str(x) for x in failed if x]
    bad_rc = [str(s.get("script")) for s in scripts
              if isinstance(s, dict) and s.get("rc") not in (0,)]
    tiers = next((s for s in scripts
                  if isinstance(s, dict) and str(s.get("script", "")).startswith("08")), {})
    gevent = next((s for s in scripts
                   if isinstance(s, dict) and str(s.get("script", "")).startswith("09")), {})
    tier_lines = [c for c in (tiers.get("conclusion") or []) if isinstance(c, str)]
    out.update({
        "available": True,
        "scripts": scripts,
        "failed": sorted(set(failed + bad_rc)),
        "tiers": tiers,
        "gevent": gevent,
        "tier_lines": tier_lines,
        "total_seconds": data.get("total_seconds"),
    })
    out["pass"] = bool(scripts) and not out["failed"]
    out["tiers_ok"] = bool(tiers) and tiers.get("rc") == 0 and bool(tier_lines)
    return out


# ────────────────────────── 证据：run_all 日志 ──────────────────────────

def gather_run_all(log_path: str, exit_code) -> dict:
    out = {"available": False, "path": log_path, "reason": "", "passed": None,
           "total": None, "ok": False, "exit": exit_code if exit_code is not None else None,
           "skipped": None, "known_red": None}
    if not log_path:
        out["reason"] = "未提供 --run-all-log（本期没有全量回归留档）"
        return out
    if not os.path.isfile(log_path):
        out["reason"] = "run_all 日志不存在：%s" % _rel(log_path)
        return out
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError as e:
        out["reason"] = "run_all 日志不可读：%s" % e
        return out
    m = re.search(r"结果：PASS\s+(\d+)\s*/\s*(\d+)", text) or \
        re.search(r"PASS\s+(\d+)\s*/\s*(\d+)", text)
    if not m:
        out["reason"] = "日志里找不到 `结果：PASS n/n` 汇总行（不是 tests/run_all.py 的输出？）"
        return out
    out["passed"] = int(m.group(1))
    out["total"] = int(m.group(2))
    m2 = re.search(r"SKIP\s+(\d+)", text)
    out["skipped"] = int(m2.group(1)) if m2 else None
    m3 = re.search(r"已知预期红\s+(\d+)", text)
    out["known_red"] = int(m3.group(1)) if m3 else None
    if out["exit"] is None:
        m4 = re.search(r"\[exit code:\s*(\d+)\]", text) or \
            re.search(r"^\s*EXIT=(\d+)\s*$", text, re.MULTILINE)
        if m4:
            out["exit"] = int(m4.group(1))
    has_fail_block = "未通过：" in text
    out["available"] = True
    out["ok"] = bool(
        out["total"] and out["total"] > 0
        and out["passed"] == out["total"]
        and not has_fail_block
        and (out["exit"] is None or out["exit"] == 0))
    if out["passed"] != (out["total"] or 0):
        out["reason"] = "PASS %s/%s —— 有脚本未通过" % (out["passed"], out["total"])
    elif has_fail_block:
        out["reason"] = "日志里出现「未通过：」小节"
    elif out["exit"] not in (None, 0):
        out["reason"] = "run_all 退出码 %s" % out["exit"]
    return out


# ────────────────────────── 证据：审计统计 ──────────────────────────

def gather_audit(query_path: str) -> dict:
    out = {"available": False, "path": query_path, "reason": "", "stats": None,
           "stdout_tail": ""}
    if not query_path or not os.path.isfile(query_path):
        out["reason"] = "审计模块未部署/未落地（%s 不存在）" % _rel(query_path or "")
        return out
    cmd = [sys.executable, os.path.abspath(query_path), "--stats", "--json"]
    env = dict(os.environ)
    env["PYTHONPATH"] = ""
    env["PYTHONUTF8"] = "1"
    try:
        p = subprocess.run(cmd, cwd=BACKEND, env=env, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=AUDIT_TIMEOUT_SEC)
    except (OSError, subprocess.TimeoutExpired) as e:
        out["reason"] = "审计统计调用失败：%s: %s" % (type(e).__name__, e)
        return out
    out["stdout_tail"] = (p.stdout or "")[-800:]
    stats = first_json(p.stdout or "")
    if not isinstance(stats, dict):
        out["reason"] = ("审计统计输出不可解析（rc=%s）：%s"
                         % (p.returncode, (p.stderr or p.stdout or "")[:400].strip()))
        return out
    out["available"] = True
    out["stats"] = stats
    if not stats.get("ok"):
        out["reason"] = "审计统计返回 ok=false"
    return out


# ────────────────────────── 证据：演练留档（§11.4「演练记录」） ──────────────────────────

def gather_drills(reports_dir: str) -> list:
    """`ops/reports/` 下的演练留档（HA 切换 / 数据层迁移等）—— 报告里只列事实，不评价。"""
    if not reports_dir or not os.path.isdir(reports_dir):
        return []
    out, seen = [], set()
    for pat in ("ha_*.json", "db_drill_*.json", "*_drill_*.json"):
        for p in sorted(glob.glob(os.path.join(reports_dir, pat))):
            if p in seen:
                continue
            seen.add(p)
            try:
                when = dt.datetime.fromtimestamp(os.path.getmtime(p)).astimezone()
            except OSError:
                when = None
            out.append({"path": p, "when": when, "age_days": age_days(when)})
    return out


# ────────────────────────── 发布门 ──────────────────────────

def evaluate_gate(ev: dict) -> list:
    """返回阻断项列表（空 = 放行）。判据严格按 §S3-04 / 任务书 D 节。"""
    blockers = []

    rot = ev["rotation"]
    if not rot.get("available"):
        blockers.append("凭据轮换：登记表缺失/不可读 —— %s" % (rot.get("reason") or "未知原因"))
    else:
        crit = rot.get("critical_bad") or []
        if crit:
            detail = []
            for iid in crit:
                row = next((r for r in rot["items"] if r["id"] == iid), {})
                state = row.get("state")
                if state == "never":
                    detail.append("%s（从未轮换）" % iid)
                else:
                    detail.append("%s（过期 %s 天，到期日 %s）"
                                  % (iid, row.get("days_overdue"), row.get("due_date")))
            blockers.append("凭据轮换：关键凭据未就绪（%d 项）—— %s"
                            % (len(crit), "；".join(detail)))

    cve = ev["cve"]
    if not cve.get("available"):
        blockers.append("CVE：没有扫描报告 —— %s（缺扫描 ≠ 无漏洞）"
                        % (cve.get("reason") or "未知原因"))
    elif cve.get("status") == "SKIP":
        blockers.append("CVE：最新报告是 SKIP（未完成扫描，不得当作通过）—— %s"
                        % (cve.get("skip_reason") or _rel(cve.get("path", ""))))
    elif not cve.get("clean"):
        blockers.append("CVE：最新报告有 %d 个漏洞（必须升级后重扫）—— %s"
                        % (len(cve.get("vulns") or []), _rel(cve.get("path", ""))))
    elif not cve.get("fresh"):
        blockers.append("CVE：最新干净报告已 %s 天（>%d 天）—— %s"
                        % (cve.get("age_days"), CVE_MAX_AGE_DAYS,
                           _rel(cve.get("path", ""))))

    ha = ev["ha"]
    if not ha.get("available"):
        blockers.append("HA 演练：没有报告 —— %s"
                        % (ha.get("reason") or "未知原因"))
    elif not ha.get("ok"):
        blockers.append("HA 演练：最新报告 ok=false —— %s" % _rel(ha.get("path", "")))
    elif not ha.get("fresh"):
        blockers.append("HA 演练：最新报告已 %s 天（>%d 天）—— %s"
                        % (ha.get("age_days"), HA_MAX_AGE_DAYS, _rel(ha.get("path", ""))))
    else:
        if ha.get("rto_ok") is False:
            blockers.append("HA 演练：RTO 实测 %s min > 目标 %s min"
                            % (fmt_num(ha.get("rto_min")),
                               fmt_num((ha.get("targets") or {}).get("rto_min"))))
        if ha.get("rpo_ok") is False:
            blockers.append("HA 演练：RPO 实测 %s min > 目标 %s min"
                            % (fmt_num(ha.get("rpo_min")),
                               fmt_num((ha.get("targets") or {}).get("rpo_min"))))

    st = ev["staging"]
    if not st.get("available"):
        blockers.append("DDoS 回归：staging 证据缺失 —— %s"
                        % (st.get("reason") or "未知原因"))
    elif st.get("failed"):
        blockers.append("DDoS 回归：有失败脚本 —— %s" % ", ".join(st["failed"]))
    elif not st.get("scripts"):
        blockers.append("DDoS 回归：结果里没有 scripts 记录")

    ra = ev["run_all"]
    if not ra.get("available"):
        blockers.append("全量回归：run_all 证据缺失 —— %s"
                        % (ra.get("reason") or "未知原因"))
    elif not ra.get("ok"):
        blockers.append("全量回归：run_all 未 PASS —— %s"
                        % (ra.get("reason") or "未知原因"))

    return blockers


# ────────────────────────── 报告 ──────────────────────────

def _sym_rotation(rot):
    if not rot.get("available"):
        return SYM_MISS, "缺失（%s）" % (rot.get("reason") or "")
    c = rot.get("counts") or {}
    if rot.get("ok"):
        return SYM_OK, "全部 %s 项在周期内" % c.get("total", "?")
    crit = rot.get("critical_bad") or []
    return SYM_PART, ("欠账 %d 项（已过期 %s / 从未轮换 %s，其中关键 %d 项）"
                      % (c.get("overdue", 0) + c.get("never", 0),
                         c.get("overdue", 0), c.get("never", 0), len(crit)))


def _sym_cve(cve):
    if not cve.get("available"):
        return SYM_MISS, "缺失（%s）" % (cve.get("reason") or "")
    if cve.get("status") == "SKIP":
        return SYM_MISS, "SKIP —— 未完成扫描，不得当作通过"
    if not cve.get("clean"):
        return SYM_PART, "发现 %d 个漏洞（%s）" % (len(cve.get("vulns") or []),
                                                cve.get("generated_at") or "日期缺失")
    if not cve.get("fresh"):
        return SYM_PART, "干净但报告已 %s 天（>%d 天）" % (cve.get("age_days"),
                                                          CVE_MAX_AGE_DAYS)
    return SYM_OK, "干净（%s，%s 天前）" % (cve.get("generated_at"),
                                           cve.get("age_days"))


def _sym_ha(ha):
    if not ha.get("available"):
        return SYM_MISS, "缺失（%s）" % (ha.get("reason") or "")
    if not ha.get("ok"):
        return SYM_PART, "最新报告 ok=false（%s）" % (ha.get("generated_at") or "日期缺失")
    if not ha.get("fresh"):
        return SYM_PART, "报告已 %s 天（>%d 天）" % (ha.get("age_days"), HA_MAX_AGE_DAYS)
    if ha.get("rto_ok") is False or ha.get("rpo_ok") is False:
        return SYM_PART, "未达标（RTO %s / RPO %s）" % (fmt_num(ha.get("rto_min")),
                                                      fmt_num(ha.get("rpo_min")))
    return SYM_OK, "RTO %s min / RPO %s min（%s 天前）" % (
        fmt_num(ha.get("rto_min")), fmt_num(ha.get("rpo_min")), ha.get("age_days"))


def _concl_text(script: dict) -> str:
    """staging 结论 → 表格里的一句话。

    优先级：`[CONCLUSION]` > 含「N 台：」的三档结论行 > `[SEED]` > desc。
    （08_staging_tiers.py 只发 [SEED] 与三档行，直接取第一条会把"建了 1 万单"当结论。）
    """
    lines = [str(x).strip() for x in (script.get("conclusion") or []) if str(x).strip()]
    pick = next((x for x in lines if x.startswith("[CONCLUSION]")), None)
    if pick is None:
        pick = next((x for x in lines if re.search(r"\d+\s*台：", x)), None)
    if pick is None:
        pick = next((x for x in lines if x.startswith("[SEED]")), None)
    if pick is None:
        pick = lines[0] if lines else (script.get("desc") or "")
    pick = pick.replace("[CONCLUSION]", "").replace("[SEED]", "").strip()
    pick = re.sub(r"\s+", " ", pick)
    return pick if len(pick) <= 200 else pick[:199] + "…"


def _sym_staging(st):
    if not st.get("available"):
        return SYM_MISS, "缺失（%s）" % (st.get("reason") or "")
    if st.get("failed"):
        return SYM_PART, "失败 %d 个：%s" % (len(st["failed"]), ", ".join(st["failed"]))
    if not st.get("scripts"):
        return SYM_PART, "无 scripts 记录"
    if not st.get("tiers_ok"):
        return SYM_PART, "%d 个脚本全绿，但缺三档（08）结论" % len(st["scripts"])
    return SYM_OK, "%d 个脚本全绿（含三档 + gevent 对照）" % len(st["scripts"])


def _sym_run_all(ra):
    if not ra.get("available"):
        return SYM_MISS, "缺失（%s）" % (ra.get("reason") or "")
    if ra.get("ok"):
        return SYM_OK, "PASS %s/%s" % (ra.get("passed"), ra.get("total"))
    return SYM_PART, "PASS %s/%s（%s）" % (ra.get("passed"), ra.get("total"),
                                          ra.get("reason") or "未通过")


def _sym_audit(au):
    if not au.get("available"):
        return SYM_MISS, "缺失（%s）" % (au.get("reason") or "")
    s = au.get("stats") or {}
    note = "文件 %s / 行 %s / 拒绝 %s" % (s.get("files"), s.get("lines"), s.get("denied"))
    # 模块可用但一条记录都没有 ⇒ 只能算「部分」：审计链路是否真的在写，本期没被证明
    if not s.get("lines"):
        return SYM_PART, note + "（本期无记录）"
    return SYM_OK, note


def render_report(period: str, ev: dict, blockers: list, args, now: dt.datetime) -> str:
    year, quarter = parse_period(period)
    start, end = period_range(year, quarter)
    rot, cve, ha, st, ra, au = (ev["rotation"], ev["cve"], ev["ha"],
                                ev["staging"], ev["run_all"], ev["audit"])
    L = []
    A = L.append

    A("# 安全运营季度报告 — %s" % period)
    A("")
    A("- 报告期间：%s ~ %s" % (start.isoformat(), end.isoformat()))
    A("- 生成时间：%s" % _iso(now))
    A("- 生成命令：`python ops/quarterly_ops_report.py --period %s%s`"
      % (period, " --release-gate" if args.release_gate else ""))
    A("- 发布门：**%s**" % ("未通过（%d 项阻断）" % len(blockers) if blockers else "通过 ✅"))
    A("- 证据目录：`%s`" % _rel(args.reports_dir))
    A("")
    A("> 口径：本报告只汇总**已存在的证据**。缺失一律写「缺失」并进人工清单；"
      "不做推测、不补数字。状态符号：%s 就绪 ｜ %s 部分/有欠账 ｜ %s 缺失/阻断。"
      % (SYM_OK, SYM_PART, SYM_MISS))
    A("")

    # ── 1. 摘要 ──
    A("## 1. 摘要")
    A("")
    A("| 项 | 状态 | 说明 | 证据 |")
    A("|---|---|---|---|")
    rows = [
        ("凭据轮换", _sym_rotation(rot),
         _rel(rot.get("path") or "") or args.rotation_registry),
        ("CVE 扫描", _sym_cve(cve), _rel(cve.get("path") or "") or "—"),
        ("HA 演练（RTO/RPO）", _sym_ha(ha), _rel(ha.get("path") or "") or "—"),
        ("DDoS 三档回归", _sym_staging(st), _rel(st.get("path") or "") or "—"),
        ("发布前全量回归（run_all）", _sym_run_all(ra),
         _rel(ra.get("path") or "") or "—"),
        ("审计日志统计", _sym_audit(au), _rel(au.get("path") or "") or "—"),
    ]
    for name, (sym, note), evidence in rows:
        A("| %s | %s | %s | `%s` |" % (name, sym, note, evidence))
    A("| 发布门 | %s | %s | 见 §8 |"
      % ((SYM_OK, "全部判据通过") if not blockers
         else (SYM_MISS, "%d 项阻断" % len(blockers))))
    A("")

    # ── 2. 凭据轮换 ──
    A("## 2. 凭据轮换状态（到期 / 过期清单）")
    A("")
    if rot.get("available"):
        try:
            rot_mod = _load_rotation_module()
            A(rot_mod.render_md(rot["status_dict"]))
        except Exception:  # noqa: BLE001
            A("登记表可读，但渲染模块不可用（见下表明细）。")
            A("")
            A("| ID | 名称 | 关键 | 上次轮换 | 到期日 | 状态 |")
            A("|---|---|---|---|---|---|")
            for r in rot["items"]:
                A("| `%s` | %s | %s | %s | %s | %s |"
                  % (r["id"], r["name"], "是" if r["critical"] else "否",
                     r["last_rotated"] or "—", r["due_date"] or "—", r["state"]))
        A("")
        A("关键凭据欠账（发布门判据）：%s"
          % (("、".join("`%s`" % x for x in rot["critical_bad"]))
             if rot["critical_bad"] else "无"))
    else:
        A("**缺失**：%s" % (rot.get("reason") or ""))
        A("")
        A("证据路径：`%s`（约定 `ops/credential_rotation.json`）"
          % _rel(args.rotation_registry))
    A("")

    # ── 3. CVE ──
    A("## 3. CVE 扫描结论")
    A("")
    if cve.get("available"):
        A("- 最新报告：`%s`" % _rel(cve["path"]))
        A("- 报告时间：%s（%s 天前，判据 ≤%d 天）"
          % (cve.get("generated_at") or "缺失", cve.get("age_days"), CVE_MAX_AGE_DAYS))
        A("- 扫描状态：**%s**" % (cve.get("status") or "未知"))
        A("- requirements SHA-256：`%s`"
          % (cve.get("requirements_sha256") or "缺失"))
        A("- 工具版本：%s" % (cve.get("tool") or "未知"))
        c = cve.get("counts") or {}
        A("- 依赖 %s 个 ｜ 有漏洞的包 %s 个 ｜ 漏洞 %s 个"
          % (c.get("dependencies", "缺失"), c.get("vulnerable_packages", "缺失"),
             c.get("vulns", len(cve.get("vulns") or []))))
        vulns = cve.get("vulns") or []
        if vulns:
            A("")
            A("| 包 | 版本 | 漏洞 ID | 修复版本 |")
            A("|---|---|---|---|")
            for v in vulns:
                A("| %s | %s | %s | %s |" % (
                    v.get("package"), v.get("version"), v.get("id"),
                    ", ".join(v.get("fix_versions") or []) or "无（需评估）"))
        elif cve.get("status") == "SKIP":
            A("")
            A("**本次没有完成扫描**：%s" % (cve.get("skip_reason") or ""))
            A("")
            A("> 缺扫描 ≠ 无漏洞。必须换到装了 `pip-audit` 且能联网的机器上重跑 "
              "`python ops/cve_scan.py`，30 天内的干净报告才能放行。")
        elif cve.get("clean"):
            A("")
            A("未发现已知漏洞。")
    else:
        A("**缺失**：%s" % (cve.get("reason") or ""))
        A("")
        A("应执行：`python ops/cve_scan.py`（需联网 + 已安装 pip-audit）。")
    A("")

    # ── 4. 高可用 ──
    A("## 4. 高可用演练（RTO / RPO 实测 vs 目标）")
    A("")
    if ha.get("available"):
        t = ha.get("targets") or {}
        A("| 指标 | 实测 | 目标 | 判定 |")
        A("|---|---|---|---|")
        A("| RTO（恢复时间） | %s | %s | %s |"
          % (fmt_num(ha.get("rto_min"), " min"),
             fmt_num(t.get("rto_min"), " min"),
             {True: "达标", False: "**未达标**", None: "缺失"}[ha.get("rto_ok")]))
        A("| RPO（数据丢失窗口） | %s | %s | %s |"
          % (fmt_num(ha.get("rpo_min"), " min"),
             fmt_num(t.get("rpo_min"), " min"),
             {True: "达标", False: "**未达标**", None: "缺失"}[ha.get("rpo_ok")]))
        A("")
        A("- 报告：`%s`" % _rel(ha["path"]))
        A("- 演练时间：%s（%s 天前，判据 ≤%d 天）"
          % (ha.get("generated_at") or "缺失", ha.get("age_days"), HA_MAX_AGE_DAYS))
        A("- 演练结论：%s" % ("ok" if ha.get("ok") else "**ok=false（未通过）**"))
        if ha.get("extra_keys"):
            A("- 报告内其余字段：%s"
              % "、".join("`%s`" % k for k in ha["extra_keys"][:20]))
    else:
        A("**缺失**：%s" % (ha.get("reason") or ""))
        A("")
        A("应执行：HA 演练（`ops/` 下的演练脚本）→ 产出 `ops/reports/ha_<ts>.json`，"
          "契约 `{ok, generated_at, rto_min, rpo_min, targets:{rto_min:30, rpo_min:5}}`。")
    A("")
    drills = ev.get("drills") or []
    A("### 演练留档（`%s`）" % _rel(args.reports_dir))
    A("")
    if drills:
        A("| 留档 | 时间 | 多少天前 |")
        A("|---|---|---|")
        for d in drills:
            A("| `%s` | %s | %s |" % (_rel(d["path"]),
                                     _iso(d["when"]) if d["when"] else "缺失",
                                     fmt_num(d["age_days"])))
    else:
        A("**缺失**：本期没有任何演练留档（`ha_*.json` / `db_drill_*.json`）。"
          "§11.4 的出口要求「季度报告 + 演练记录」，缺记录只能按未完成处理。")
    A("")

    # ── 5. 审计 ──
    A("## 5. 审计日志统计")
    A("")
    if au.get("available"):
        s = au.get("stats") or {}
        A("- 统计来源：`%s --stats --json`（subprocess 调用，非 import）"
          % _rel(au.get("path")))
        A("- 时间范围：%s ~ %s" % (s.get("since") or "（无记录）",
                                 s.get("until") or "（无记录）"))
        A("- 文件 %s 个 ｜ 行 %s ｜ 拒绝(denied) %s"
          % (s.get("files"), s.get("lines"), s.get("denied")))
        if not s.get("lines"):
            A("- ⚠ 本期**没有任何审计记录**：不能当作「没有未授权访问」，只能当作"
              "「审计链路未被证明在写」。先在真机上跑一次下载/删除/列表各一条，再回看这里。")
        by_action = s.get("by_action") if isinstance(s.get("by_action"), dict) else {}
        by_result = s.get("by_result") if isinstance(s.get("by_result"), dict) else {}
        if by_action:
            A("")
            A("| 动作 | 次数 |")
            A("|---|---|")
            for k in sorted(by_action, key=lambda x: (-int(by_action[x] or 0), str(x))):
                A("| %s | %s |" % (k, by_action[k]))
        if by_result:
            A("")
            A("| 结果 | 次数 |")
            A("|---|---|")
            for k in sorted(by_result):
                A("| %s | %s |" % (k, by_result[k]))
        if au.get("reason"):
            A("")
            A("⚠ %s" % au["reason"])
        A("")
        A("查询入口：`python ops/audit_query.py --stats --json`（谁读了哪个文件见 "
          "`docs/文件访问审计说明_2026-10-24.md`）。")
    else:
        A("**缺失**：%s" % (au.get("reason") or ""))
        A("")
        A("审计日志模块（`ops/audit_query.py` + 后端文件访问审计）尚未部署/落地 ⇒ "
          "本期无法回答「谁、何时、读了哪个顾客文件」。")
    A("")

    # ── 6. DDoS ──
    A("## 6. DDoS 三档回归（staging）")
    A("")
    if st.get("available"):
        A("- 结果文件：`%s`（总耗时 %s s）"
          % (_rel(st["path"]), fmt_num(st.get("total_seconds"))))
        A("- 失败脚本：%s" % (", ".join(st["failed"]) if st["failed"] else "无"))
        A("")
        A("| 脚本 | 结论 | rc | 秒 |")
        A("|---|---|---|---|")
        for s in st.get("scripts") or []:
            A("| %s | %s | %s | %s |" % (
                s.get("script"), _concl_text(s), s.get("rc"), s.get("seconds")))
        A("")
        A("### 三档（3 / 6 / 10 台，§11.2 设备增长模型）")
        A("")
        if st.get("tier_lines"):
            for line in st["tier_lines"]:
                A("- %s" % line)
        else:
            A("**缺失**：08_staging_tiers.py 没有产出三档结论。")
        A("")
        if st.get("gevent"):
            A("### 运行时对照（S3-01a）")
            A("")
            for line in (st["gevent"].get("conclusion") or []):
                A("- %s" % line)
            A("")
    else:
        A("**缺失**：%s" % (st.get("reason") or ""))
        A("")
        A("应执行：`python tests/staging/99_run_all.py`（隔离副本，约 7 分钟），"
          "结果落 `tests/staging/results/99_run_all.json`。")
    A("")

    # ── 7. run_all ──
    A("## 7. 发布前全量回归（run_all）")
    A("")
    if ra.get("available"):
        A("- 日志：`%s`" % _rel(ra["path"]))
        A("- 汇总：PASS **%s / %s**（SKIP %s，已知预期红 %s，退出码 %s）"
          % (ra.get("passed"), ra.get("total"), fmt_num(ra.get("skipped")),
             fmt_num(ra.get("known_red")), fmt_num(ra.get("exit"))))
        A("- 判定：%s" % ("通过 ✅" if ra.get("ok")
                        else "**未通过 ❌**（%s）" % (ra.get("reason") or "")))
    else:
        A("**缺失**：%s" % (ra.get("reason") or ""))
        A("")
        A("应执行（PYTHONPATH 必须置空）：")
        A("")
        A("```bash")
        A('PYTHONPATH= PYTHONUTF8=1 "<python>" tests/run_all.py | tee /tmp/run_all.log')
        A("```")
        A("")
        A("然后把该日志用 `--run-all-log` 传回本脚本。")
    A("")

    # ── 8. 发布门 ──
    A("## 8. 发布门判定（--release-gate）")
    A("")
    A("判据（全部满足才放行，任一不满足即阻断）：")
    A("")
    A("1. 关键（critical）凭据无过期、无「从未轮换」；")
    A("2. CVE 有 **%d 天内**的干净扫描报告（SKIP/缺失/有漏洞均阻断）；" % CVE_MAX_AGE_DAYS)
    A("3. HA 演练有 **%d 天内**的 ok 报告，且 RTO ≤ 目标、RPO ≤ 目标；" % HA_MAX_AGE_DAYS)
    A("4. staging DDoS 回归结果存在且无失败脚本；")
    A("5. `tests/run_all.py` 通过（PASS n/n，退出码 0）。")
    A("")
    if blockers:
        A("**结论：未通过（exit %d）** —— %d 项阻断：" % (1, len(blockers)))
        A("")
        for i, b in enumerate(blockers, 1):
            A("%d. %s" % (i, b))
    else:
        A("**结论：通过 ✅（exit 0）** —— 上述 5 类判据全部满足。")
    A("")

    # ── 9. 未做 / 人工清单 ──
    A("## 9. 未做 / 人工清单")
    A("")
    manual = build_manual_list(ev, args)
    if manual:
        for item in manual:
            A("- [ ] %s" % item)
    else:
        A("本期无未做事项。")
    A("")

    # ── 10. 下季度动作 ──
    A("## 10. 下季度动作")
    A("")
    for item in build_next_actions(ev, blockers):
        A("- [ ] %s" % item)
    A("")
    A("---")
    A("")
    A("附：证据路径一览")
    A("")
    A("| 证据 | 路径 | 状态 |")
    A("|---|---|---|")
    for name, p, ok in (
            ("凭据轮换登记表", args.rotation_registry, rot.get("available")),
            ("CVE 报告", cve.get("path") or "", cve.get("available")),
            ("HA 演练报告", ha.get("path") or "", ha.get("available")),
            ("staging 三档回归", args.staging_result, st.get("available")),
            ("run_all 日志", args.run_all_log or "", ra.get("available")),
            ("审计统计", args.audit_query, au.get("available"))):
        A("| %s | `%s` | %s |" % (name, _rel(p) if p else "—",
                                  SYM_OK if ok else SYM_MISS))
    A("")
    return "\n".join(L)


def build_manual_list(ev: dict, args) -> list:
    out = []
    rot, cve, ha, st, ra, au = (ev["rotation"], ev["cve"], ev["ha"],
                                ev["staging"], ev["run_all"], ev["audit"])
    if rot.get("available"):
        for r in rot["items"]:
            if r["state"] == "never" and r["critical"]:
                out.append("轮换关键凭据 `%s`（%s，负责人 %s）：%s —— 完成后 "
                           "`python ops/credential_rotation.py --mark %s --date <实际日期>`"
                           % (r["id"], r["name"], r["owner"],
                              r["method"][:90] + ("…" if len(r["method"]) > 90 else ""),
                              r["id"]))
            elif r["state"] == "overdue" and r["critical"]:
                out.append("处理已过期的关键凭据 `%s`（已过期 %d 天，到期日 %s）"
                           % (r["id"], r["days_overdue"], r["due_date"]))
        for r in rot["items"]:
            if r["state"] in ("never", "overdue") and not r["critical"]:
                out.append("轮换非关键凭据 `%s`（%s）—— 不阻断发布，但不能长期不换"
                           % (r["id"], r["name"]))
    else:
        out.append("补齐凭据轮换登记表：`ops/credential_rotation.json`")
    if not cve.get("available"):
        out.append("在有 pip-audit 且可联网的机器上跑 `python ops/cve_scan.py`，"
                   "把 `ops/reports/cve-*.json` 纳入仓库/留档")
    elif cve.get("status") == "SKIP":
        out.append("CVE 扫描未完成（SKIP）：换到装了 `pip-audit` 且能联网的机器重跑 "
                   "`python ops/cve_scan.py`，直到拿到 CLEAN")
    elif not cve.get("clean"):
        out.append("升级 CVE 报告里的漏洞包并重扫（明细见 §3）")
    elif not cve.get("fresh"):
        out.append("CVE 干净报告已超过 %d 天，重跑 `python ops/cve_scan.py`"
                   % CVE_MAX_AGE_DAYS)
    if not ha.get("available"):
        out.append("跑一次 HA 演练并产出 `ops/reports/ha_*.json`（RTO ≤30 min / RPO ≤5 min）")
    elif not ha.get("ok"):
        out.append("HA 演练报告 ok=false，需定位原因并重跑")
    elif not ha.get("fresh"):
        out.append("HA 演练报告已超过 %d 天，重跑演练" % HA_MAX_AGE_DAYS)
    if ha.get("available") and (ha.get("rto_ok") is False or ha.get("rpo_ok") is False):
        out.append("RTO/RPO 未达标：按 §11.4 复核 warm standby / 备份频率")
    if not st.get("available"):
        out.append("跑 `python tests/staging/99_run_all.py` 产出 DDoS 三档回归证据")
    elif st.get("failed"):
        out.append("staging 失败脚本需修复后复跑：%s" % ", ".join(st["failed"]))
    if not ra.get("available"):
        out.append("跑 `python tests/run_all.py` 并把输出留档（`--run-all-log` 引用）")
    elif not ra.get("ok"):
        out.append("run_all 未通过（%s）：修复后全量复跑" % (ra.get("reason") or ""))
    if not au.get("available"):
        out.append("审计日志模块未部署/未落地 ⇒ 「谁读过哪个文件」本期无证据")
    out.append("备份 GPG 密钥未启用（`backup_gpg_key`）：按 §S2-06 给备份加密")
    out.append("Tabletop 演练：按《安全运营手册》四个场景各记录一次判据-止血-复盘")
    out.append("证据保留：季度报告与演练 JSON 保留 1 年，日志/扫描报告保留 90 天")
    return out


def build_next_actions(ev: dict, blockers: list) -> list:
    out = []
    rot, cve, ha = ev["rotation"], ev["cve"], ev["ha"]
    if rot.get("critical_bad"):
        out.append("下季度首要：把 %d 项关键凭据欠账清零（`--check` 必须 exit 0），"
                   "并把轮换日期写进登记表" % len(rot["critical_bad"]))
    out.append("CVE 订阅例行化：每月 1 日跑 `python ops/cve_scan.py`，"
               "有漏洞当周升级；季度末核对订阅源是否有新公告")
    if not ha.get("available") or not ha.get("fresh"):
        out.append("HA：每季度至少一次真实切换演练，RTO/RPO 实测写进 `ops/reports/ha_*.json`")
    else:
        out.append("HA：保持季度演练节奏，并把本次 RTO/RPO 与上季度对比看趋势")
    out.append("DDoS：设备数接近扩容阈值（在线 >6 / 注册 >10）时提前跑 "
               "`tests/staging/99_run_all.py` 三档复核，不等季度")
    out.append("审计：季度末抽查 10 条文件访问记录，确认「谁/何时/哪个文件」可追溯")
    out.append("Tabletop：下季度换 2 个新场景（如备份被删、域名/证书失效）演练")
    if blockers:
        out.append("发布门阻断项必须在下次发版前全部关闭，否则按 §10 回滚/降级流程处理")
    return out


# ────────────────────────── 主流程 ──────────────────────────

def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001  pragma: no cover
        pass

    ap = argparse.ArgumentParser(
        description="S3-04 安全运营季度报告 + 发布门",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="退出码：0 报告已生成；1 发布门未通过；2 参数/IO 错误")
    ap.add_argument("--period", default="", help="报告季度 YYYY-QN（默认按今天算）")
    ap.add_argument("--rotation-registry", default=DEFAULT_ROTATION,
                    help="凭据轮换登记表（默认 ops/credential_rotation.json）")
    ap.add_argument("--reports-dir", default=DEFAULT_REPORTS,
                    help="证据目录（默认 ops/reports，读 cve-*.json / ha_*.json）")
    ap.add_argument("--ha-report", default="", help="指定 HA 报告（默认取最新 ha_*.json）")
    ap.add_argument("--cve-report", default="", help="指定 CVE 报告（默认取最新 cve-*.json）")
    ap.add_argument("--staging-result", default=DEFAULT_STAGING,
                    help="staging 结果（默认 tests/staging/results/99_run_all.json）")
    ap.add_argument("--run-all-log", default="",
                    help="tests/run_all.py 的输出日志（可选；解析 PASS n/n 与退出码）")
    ap.add_argument("--run-all-exit", type=int, default=None,
                    help="run_all 的退出码（可选；不传则从日志里找 [exit code: N]）")
    ap.add_argument("--audit-query", default=DEFAULT_AUDIT_QUERY,
                    help="审计统计 CLI（默认 ops/audit_query.py，存在才调）")
    ap.add_argument("--out", default="",
                    help="输出 md（默认 docs/安全运营季度报告_<period>.md）")
    ap.add_argument("--release-gate", action="store_true",
                    help="按发布门判据评审；不通过 exit 1 并逐条列阻断项")
    args = ap.parse_args(argv)

    now = _now()
    period = args.period or default_period()
    try:
        parse_period(period)
    except ValueError as e:
        print("[OPS] 参数错误：%s" % e, file=sys.stderr)
        return 2

    out_path = args.out or os.path.join(REPO, "docs",
                                        "安全运营季度报告_%s.md" % period)

    ev = {
        "rotation": gather_rotation(os.path.abspath(args.rotation_registry)),
        "cve": gather_cve(os.path.abspath(args.reports_dir),
                          os.path.abspath(args.cve_report) if args.cve_report else ""),
        "ha": gather_ha(os.path.abspath(args.reports_dir),
                        os.path.abspath(args.ha_report) if args.ha_report else ""),
        "staging": gather_staging(os.path.abspath(args.staging_result)),
        "run_all": gather_run_all(os.path.abspath(args.run_all_log) if args.run_all_log else "",
                                  args.run_all_exit),
        "audit": gather_audit(os.path.abspath(args.audit_query)),
        "drills": gather_drills(os.path.abspath(args.reports_dir)),
    }
    blockers = evaluate_gate(ev)
    md = render_report(period, ev, blockers, args, now)

    try:
        d = os.path.dirname(os.path.abspath(out_path))
        os.makedirs(d, exist_ok=True)
        with open(out_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(md)
    except OSError as e:
        print("[OPS] 写报告失败：%s" % e, file=sys.stderr)
        return 2

    print("[OPS] 季度报告已生成：%s" % out_path)
    for name, (sym, note) in (
            ("凭据轮换", _sym_rotation(ev["rotation"])),
            ("CVE", _sym_cve(ev["cve"])),
            ("HA", _sym_ha(ev["ha"])),
            ("DDoS", _sym_staging(ev["staging"])),
            ("run_all", _sym_run_all(ev["run_all"])),
            ("审计统计", _sym_audit(ev["audit"]))):
        print("  %s %-10s %s" % (sym, name, note))
    if blockers:
        print("[OPS] 发布门：未通过（%d 项阻断）" % len(blockers))
        for i, b in enumerate(blockers, 1):
            print("  %d. %s" % (i, b))
    else:
        print("[OPS] 发布门：通过 ✅")

    if args.release_gate and blockers:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
