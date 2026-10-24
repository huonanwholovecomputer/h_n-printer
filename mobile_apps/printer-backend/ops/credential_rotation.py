#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""credential_rotation.py — S3-04 凭据轮换日历（**只存元数据，绝不存秘密值**）

依据：docs/安全加固修复计划_2026-10-23.md §S3-04（DS-24「无凭据轮换流程」）、
§11.4 出口（季度报告 + 演练记录），以及决策表 D-1 / D-3 / D-8。

为什么是「日历」而不是「保险箱」
--------------------------------
凭据原文放在任何登记表里都会变成新的泄漏面（DS-02 的教训：一枚 token 以明文躺在
`print_config.json` / `.bak` / 日志 / git 历史里）。所以本工具**只记元数据**：
id、名称、负责人、轮换周期、上次轮换日期、轮换方式、证据路径、是否关键、备注。
读到本文件的人不会因此获得任何凭据。写入路径上还有第二道闸：`--mark --note` 的文本
若疑似含秘密（`token=` / 长 hex / PRIVATE KEY 头 / sha256 长度串…）直接拒收。

登记表 schema（`credential_rotation.json`）
------------------------------------------
    {"version": 1,
     "items": [{"id": str, "name": str, "owner": str, "interval_days": int,
                "last_rotated": "YYYY-MM-DD" | null, "method": str,
                "critical": bool, "evidence": str, "note": str}]}

`last_rotated: null` = **从未轮换**（不是"不知道"，是"没有记录到轮换"）。这类条目
一律进欠账清单 —— 诚实呈现人工欠账，而不是给个假日期让检查变绿。

用法
----
    python ops/credential_rotation.py --check            # 表格 + 到期日 + days_overdue
    python ops/credential_rotation.py --check --warn-only # 只看不拦（永远 exit 0）
    python ops/credential_rotation.py --list
    python ops/credential_rotation.py --mark tls_cert --date 2026-10-24 --note "certbot 续期核对"
    python ops/credential_rotation.py --report md
    python ops/credential_rotation.py --check --registry /tmp/test_registry.json  # 测试/演练

推出码：0 = 全部在周期内；1 = 有 overdue / never；2 = 参数或登记表 schema 错误。

`--mark` 的写入语义（**原子写**）
--------------------------------
1. 跨平台文件锁：`os.open(lock, O_CREAT|O_EXCL)` 抢锁，失败按 50ms 重试；
   锁文件超过 `LOCK_STALE_SEC` 视为残留（进程被强杀）自动回收。
2. 写前备份：`<registry>.bak-<YYYYmmdd-HHMMSS>`（保留上一次的完整内容）。
3. 临时文件 + `os.replace()` 原子替换 —— 中途断电不会留下半个 JSON。

模块接口（供 ops/quarterly_ops_report.py 导入）
-----------------------------------------------
`load_registry(path)` / `status(path, registry, today)` / `render_md(status_result)`。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import sys
import tempfile
import time
import unicodedata

VERSION = 1
DEFAULT_REGISTRY = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "credential_rotation.json")
WARN_DAYS = 14          # 到期前多少天进入「即将到期」提醒（不判失败）
LOCK_TIMEOUT_SEC = 10.0
LOCK_STALE_SEC = 120.0
LOCK_RETRY_SEC = 0.05
DATE_FMT = "%Y-%m-%d"

FIELDS = ("id", "name", "owner", "interval_days", "last_rotated", "method",
          "critical", "evidence", "note")

# 轮换策略（可选字段 `policy`，缺省 = scheduled）：
#   scheduled       —— 按 interval_days 周期轮换；过期/从未轮换算欠账，关键项拦发布门。
#   on_compromise   —— **只在确认泄漏时**才换（换了会破坏兼容性）：如 APK 签名 keystore，
#                      换掉后所有已装用户无法覆盖升级。不参与"到期"判定，也不拦发布门；
#                      正确动作是离线备份 + 泄漏时按预案换。
#   managed_external—— 由外部机制自动轮换/续期（如 certbot 续 TLS），登记表只做核对记录。
# 判据见 `_item_status()`：policy != scheduled 的条目不进 never/overdue 欠账，
# 发布门（quarterly_ops_report 的 critical_bad）因此也不会被"按需轮换"项永久卡住。
POLICIES = ("scheduled", "on_compromise", "managed_external")
POLICY_CN = {"scheduled": "周期", "on_compromise": "按需", "managed_external": "自动/外部"}

# 必须覆盖的凭据（S3-04 交付要求 + 现场已确认存在的凭据面）。
# 允许登记表多出别的条目；少了这些就说明登记表不完整 ⇒ schema 错误（exit 2）。
REQUIRED_IDS = (
    "printer_global_token",
    "device_tokens",
    "login_salt",
    "download_salt",
    "finance_module_passwords",
    "admin_passwords",
    "ssh_deploy_key",
    "cos_subaccount_key",
    "apk_keystore",
    "backup_gpg_key",
    "tls_cert",
)

# 疑似秘密的形态。命中即拒（写入时）或判 schema 错误（读取时）。
# 只匹配"像凭据"的形态，不匹配路径/日期/普通中文备注。
_SECRET_PATTERNS = (
    # 注意：这里刻意**不加** \b 前缀 —— `cloud_token=...` / `db_password=...` 这类
    # 带前缀的形态同样是秘密，\b 会漏掉它们（`_` 是词字符，没有边界）。
    re.compile(r"(?i)(token|password|passwd|secret|api[_-]?key|private[_-]?key"
               r"|authorization|access[_-]?key)\s*[:=]\s*\S{4,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)\bssh-(rsa|ed25519|ecdsa)\s+AAAA"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{12,}"),
    re.compile(r"\b[0-9a-fA-F]{32,}\b"),          # md5/sha/hex token（≥32 位）
    re.compile(r"\bAKID[0-9A-Za-z]{10,}\b"),      # 云厂商 access key id 形态
)


class RegistryError(Exception):
    """登记表/参数层面的错误 ⇒ CLI exit 2。"""


# ────────────────────────── 小工具 ──────────────────────────

def today_str() -> str:
    return dt.date.today().strftime(DATE_FMT)


def _dw(text) -> int:
    """显示宽度（中日韩全角算 2 列），只用于终端表格对齐。"""
    w = 0
    for ch in str(text):
        w += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return w


def _pad(text, width: int) -> str:
    s = str(text)
    if _dw(s) > width:                     # 按显示宽度截断，末尾补省略号
        out = ""
        for ch in s:
            if _dw(out + ch) > max(1, width - 1):
                break
            out += ch
        s = out + "…"
    return s + " " * max(0, width - _dw(s))


def looks_like_secret(text: str) -> bool:
    """文本里是否疑似含凭据原文（登记表与 `--note` 都过这一道）。"""
    if not text:
        return False
    return any(p.search(text) for p in _SECRET_PATTERNS)


def parse_date(value: str) -> dt.date:
    """严格 `YYYY-MM-DD`。'2026-2-3' / '2026-02-30' / 'tomorrow' 全部拒收。"""
    if not isinstance(value, str):
        raise ValueError("日期必须是字符串")
    try:
        d = dt.datetime.strptime(value, DATE_FMT).date()
    except ValueError:
        raise ValueError("不是合法日期（要求 YYYY-MM-DD）：%r" % (value,))
    if d.strftime(DATE_FMT) != value:
        raise ValueError("日期格式不严格（要求补零的 YYYY-MM-DD）：%r" % (value,))
    return d


def _check_item(idx: int, item) -> list:
    errs = []
    where = "items[%d]" % idx
    if not isinstance(item, dict):
        return ["%s 不是对象" % where]
    for f in FIELDS:
        if f not in item:
            errs.append("%s 缺字段 %r" % (where, f))
    if errs:
        return errs
    iid = item.get("id")
    if not isinstance(iid, str) or not iid.strip():
        errs.append("%s.id 必须是非空字符串" % where)
    else:
        where = "items[%d](%s)" % (idx, iid)
        if not re.fullmatch(r"[a-z][a-z0-9_]*", iid):
            errs.append("%s.id 只允许小写字母/数字/下划线，且以字母开头" % where)
    for f in ("name", "owner", "method", "evidence", "note"):
        v = item.get(f)
        if not isinstance(v, str):
            errs.append("%s.%s 必须是字符串" % (where, f))
        elif looks_like_secret(v):
            errs.append("%s.%s 疑似含秘密值（登记表禁止存凭据原文）" % (where, f))
    iv = item.get("interval_days")
    if isinstance(iv, bool) or not isinstance(iv, int):
        errs.append("%s.interval_days 必须是整数" % where)
    elif iv <= 0:
        errs.append("%s.interval_days 必须为正数（收到 %r）" % (where, iv))
    elif iv > 3650:
        errs.append("%s.interval_days 过大（>3650 天）" % where)
    crit = item.get("critical")
    if not isinstance(crit, bool):
        errs.append("%s.critical 必须是布尔值" % where)
    pol = item.get("policy", "scheduled")
    if pol not in POLICIES:
        errs.append("%s.policy 只能是 %s（收到 %r）"
                    % (where, "/".join(POLICIES), pol))
    last = item.get("last_rotated")
    if last is not None:
        try:
            d = parse_date(last)
            if d > dt.date.today() + dt.timedelta(days=1):
                errs.append("%s.last_rotated 是未来日期（%s）" % (where, last))
        except ValueError as e:
            errs.append("%s.last_rotated 非法：%s" % (where, e))
    return errs


def validate_registry(reg) -> list:
    """返回错误列表（空列表 = 通过）。"""
    errs = []
    if not isinstance(reg, dict):
        return ["顶层必须是对象"]
    if reg.get("version") != VERSION:
        errs.append("version 必须是 %d（收到 %r）" % (VERSION, reg.get("version")))
    items = reg.get("items")
    if not isinstance(items, list) or not items:
        errs.append("items 必须是非空数组")
        return errs
    seen = set()
    for i, item in enumerate(items):
        errs.extend(_check_item(i, item))
        if isinstance(item, dict) and isinstance(item.get("id"), str):
            iid = item["id"]
            if iid in seen:
                errs.append("id 重复：%s" % iid)
            seen.add(iid)
    missing = [x for x in REQUIRED_IDS if x not in seen]
    if missing:
        errs.append("登记表缺少必需条目：%s" % ", ".join(missing))
    return errs


def load_registry(path: str = "") -> dict:
    """读取并校验登记表。任何问题都抛 RegistryError（CLI 转成 exit 2）。"""
    path = path or DEFAULT_REGISTRY
    if not os.path.isfile(path):
        raise RegistryError("登记表不存在：%s" % path)
    try:
        with open(path, "r", encoding="utf-8") as f:
            reg = json.load(f)
    except json.JSONDecodeError as e:
        raise RegistryError("登记表不是合法 JSON（%s 行%s）：%s"
                            % (path, e.lineno, e.msg))
    errs = validate_registry(reg)
    if errs:
        raise RegistryError("登记表校验失败（%s）：\n  - %s" % (path, "\n  - ".join(errs)))
    return reg


# ────────────────────────── 状态计算 ──────────────────────────

def _item_status(item: dict, today: dt.date) -> dict:
    last = item["last_rotated"]
    interval = int(item["interval_days"])
    policy = item.get("policy", "scheduled")
    row = dict(item)
    row["policy"] = policy
    row["due_date"] = None
    row["days_overdue"] = None
    row["days_left"] = None
    # 按需/外部管理的凭据不参与"到期"判定：它们的正确动作不是按期轮换。
    if policy != "scheduled":
        row["state"] = "not_applicable"
        if last is not None:
            row["due_date"] = (parse_date(last) + dt.timedelta(days=interval)).strftime(DATE_FMT)
        return row
    if last is None:
        row["state"] = "never"
        return row
    last_d = parse_date(last)
    due = last_d + dt.timedelta(days=interval)
    row["due_date"] = due.strftime(DATE_FMT)
    if today > due:
        row["state"] = "overdue"
        row["days_overdue"] = (today - due).days
        row["days_left"] = 0
    else:
        left = (due - today).days
        row["days_left"] = left
        row["days_overdue"] = 0
        row["state"] = "due_soon" if left <= WARN_DAYS else "ok"
    return row


def status(path: str = "", registry: dict | None = None,
           today: dt.date | None = None) -> dict:
    """计算整体轮换状态（quarterly_ops_report 直接 import 这个）。

    never / overdue 都算欠账（`ok=False`）；due_soon 只提醒、不判失败。
    """
    today = today or dt.date.today()
    reg = registry if registry is not None else load_registry(path)
    rows = [_item_status(it, today) for it in reg["items"]]
    never = [r["id"] for r in rows if r["state"] == "never"]
    overdue = [r["id"] for r in rows if r["state"] == "overdue"]
    due_soon = [r["id"] for r in rows if r["state"] == "due_soon"]
    ok_rows = [r["id"] for r in rows if r["state"] == "ok"]
    not_applicable = [r["id"] for r in rows if r["state"] == "not_applicable"]
    critical_bad = [r["id"] for r in rows
                    if r["critical"] and r["state"] in ("never", "overdue")]
    critical_overdue = [r["id"] for r in rows
                        if r["critical"] and r["state"] == "overdue"]
    critical_never = [r["id"] for r in rows
                      if r["critical"] and r["state"] == "never"]
    return {
        "ok": not never and not overdue,
        "today": today.strftime(DATE_FMT),
        "warn_days": WARN_DAYS,
        "path": os.path.abspath(path or DEFAULT_REGISTRY),
        "items": rows,
        "counts": {
            "total": len(rows),
            "ok": len(ok_rows),
            "due_soon": len(due_soon),
            "overdue": len(overdue),
            "never": len(never),
            "not_applicable": len(not_applicable),
        },
        "never": never,
        "overdue": overdue,
        "due_soon": due_soon,
        "not_applicable": not_applicable,
        "critical_bad": critical_bad,
        "critical_overdue": critical_overdue,
        "critical_never": critical_never,
    }


STATE_CN = {"ok": "正常", "due_soon": "即将到期", "overdue": "已过期",
            "never": "从未轮换", "not_applicable": "按需/自动（不计欠账）"}


def render_md(st: dict) -> str:
    """把 status() 的结果渲染成 markdown（季度报告直接引用这一块）。"""
    c = st["counts"]
    lines = [
        "**凭据轮换状态**（检查日 %s，到期前 %d 天提醒）" % (st["today"], st["warn_days"]),
        "",
        "| ID | 名称 | 关键 | 策略 | 上次轮换 | 周期(天) | 到期日 | 状态 | 负责人 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in st["items"]:
        lines.append("| `%s` | %s | %s | %s | %s | %d | %s | %s | %s |" % (
            r["id"], r["name"], "⚠️ 是" if r["critical"] else "否",
            POLICY_CN.get(r["policy"], r["policy"]),
            r["last_rotated"] or "—", r["interval_days"],
            r["due_date"] or "—",
            STATE_CN.get(r["state"], r["state"]) +
            (("（+%d 天）" % r["days_overdue"]) if r["state"] == "overdue" else ""),
            r["owner"]))
    lines += [
        "",
        "合计 %d 项：正常 %d ｜ 即将到期 %d ｜ 已过期 %d ｜ 从未轮换 %d ｜ 按需/自动 %d"
        % (c["total"], c["ok"], c["due_soon"], c["overdue"], c["never"],
           c.get("not_applicable", 0)),
    ]
    bad = [r for r in st["items"] if r["state"] in ("never", "overdue")]
    if bad:
        lines += ["", "**欠账清单（必须人工处理）**："]
        for r in bad:
            if r["state"] == "never":
                lines.append("- `%s` %s —— **从未轮换**（负责人：%s；方式：%s）"
                             % (r["id"], r["name"], r["owner"], r["method"]))
            else:
                lines.append("- `%s` %s —— 已过期 %d 天（到期日 %s，负责人：%s）"
                             % (r["id"], r["name"], r["days_overdue"],
                                r["due_date"], r["owner"]))
    else:
        lines += ["", "本期末无欠账。"]
    return "\n".join(lines)


# ────────────────────────── 原子写 + 文件锁 ──────────────────────────

class FileLock:
    """跨平台独占锁：`O_CREAT | O_EXCL` 抢锁 + 超时重试 + 残留锁回收。

    Windows 上 `os.open(..., O_CREAT|O_EXCL)` 与 POSIX 语义一致（原子创建），
    不需要额外的 fcntl/msvcrt 分支。
    """

    def __init__(self, path: str, timeout: float = LOCK_TIMEOUT_SEC):
        self.path = path
        self.timeout = timeout
        self.acquired = False

    def __enter__(self):
        deadline = time.time() + self.timeout
        while True:
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                try:
                    os.write(fd, ("pid=%d ts=%s\n" % (
                        os.getpid(), dt.datetime.now().isoformat(timespec="seconds"))
                    ).encode("utf-8"))
                finally:
                    os.close(fd)
                self.acquired = True
                return self
            except FileExistsError:
                # 残留锁回收（进程被强杀时锁文件会留下）
                try:
                    age = time.time() - os.path.getmtime(self.path)
                    if age > LOCK_STALE_SEC:
                        os.unlink(self.path)
                        continue
                except OSError:
                    pass
                if time.time() >= deadline:
                    raise RegistryError(
                        "登记表被另一个进程锁定（%s），等待 %.0fs 超时；"
                        "确认没有并发 --mark 后可手动删除该锁文件" % (self.path, self.timeout))
                time.sleep(LOCK_RETRY_SEC)
            except OSError as e:
                raise RegistryError("无法创建锁文件 %s：%s" % (self.path, e))

    def __exit__(self, *exc):
        if self.acquired:
            try:
                os.unlink(self.path)
            except OSError:
                pass
            self.acquired = False
        return False


def atomic_save(reg: dict, path: str) -> str | None:
    """写前备份 + 临时文件 + os.replace 原子替换。返回备份文件路径（无旧文件则 None）。"""
    path = os.path.abspath(path)
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    backup = None
    if os.path.isfile(path):
        backup = "%s.bak-%s" % (path, time.strftime("%Y%m%d-%H%M%S"))
        shutil.copy2(path, backup)
    fd, tmp = tempfile.mkstemp(prefix=os.path.basename(path) + ".tmp-", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            json.dump(reg, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        tmp = ""
    finally:
        if tmp and os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass
    return backup


# ────────────────────────── 子命令 ──────────────────────────

def _cmd_check(st: dict) -> int:
    c = st["counts"]
    widths = (26, 34, 6, 10, 12, 12, 10, 26)
    print("[ROT] 凭据轮换检查（%s，到期前 %d 天提醒）"
          % (st["today"], st["warn_days"]))
    print("".join(_pad(h, w) for h, w in zip(
        ("ID", "名称", "关键", "策略", "上次轮换", "到期日", "过期天数", "状态"), widths)))
    print("-" * (sum(widths) + 12))
    for r in st["items"]:
        overdue_cell = "—" if r["days_overdue"] is None else str(r["days_overdue"])
        cells = (r["id"], r["name"], "是" if r["critical"] else "否",
                 POLICY_CN.get(r["policy"], r["policy"]),
                 r["last_rotated"] or "从未轮换", r["due_date"] or "—",
                 overdue_cell, STATE_CN.get(r["state"], r["state"]))
        print("".join(_pad(v, w) for v, w in zip(cells, widths)))
    print("-" * (sum(widths) + 12))
    print("合计 %d 项：正常 %d ｜ 即将到期 %d ｜ 已过期 %d ｜ 从未轮换 %d ｜ 按需/自动 %d"
          % (c["total"], c["ok"], c["due_soon"], c["overdue"], c["never"],
             c.get("not_applicable", 0)))
    if st["critical_bad"]:
        print("关键凭据欠账（%d）：%s" % (len(st["critical_bad"]),
                                      ", ".join(st["critical_bad"])))
    if st["never"]:
        print("从未轮换（%d）：%s" % (len(st["never"]), ", ".join(st["never"])))
    if st["overdue"]:
        print("已过期（%d）：%s" % (len(st["overdue"]), ", ".join(st["overdue"])))
    if st["ok"]:
        print("[ROT] 判定：PASS —— 全部凭据在轮换周期内")
        return 0
    print("[ROT] 判定：FAIL —— 存在欠账（未轮换/过期），必须人工处理后再放行")
    return 1


def _cmd_list(reg: dict, st: dict) -> int:
    by_id = {r["id"]: r for r in st["items"]}
    for item in reg["items"]:
        r = by_id[item["id"]]
        print("%s %s 负责人=%s 周期=%d天 上次=%s 关键=%s 策略=%s 状态=%s"
              % (_pad(item["id"], 26), _pad(item["name"], 34), item["owner"],
                 item["interval_days"], item["last_rotated"] or "从未轮换",
                 "是" if item["critical"] else "否",
                 POLICY_CN.get(r["policy"], r["policy"]),
                 STATE_CN.get(r["state"], r["state"])))
        print("    方式：%s" % item["method"])
        print("    证据：%s" % item["evidence"])
        if item["note"]:
            print("    备注：%s" % item["note"])
    return 0


def _cmd_mark(args) -> int:
    date_str = args.date or today_str()
    try:
        parse_date(date_str)
    except ValueError as e:
        raise RegistryError("--date 非法：%s" % e)
    if args.note and looks_like_secret(args.note):
        raise RegistryError("--note 疑似含秘密值 —— 登记表**绝对**不存 token/密码/私钥原文，"
                            "只写「谁在何时用什么方式换过」")
    path = os.path.abspath(args.registry)
    lock_path = path + ".lock"
    with FileLock(lock_path):
        reg = load_registry(path)
        target = next((it for it in reg["items"] if it["id"] == args.mark), None)
        if target is None:
            raise RegistryError(
                "未知 id：%r（可用 --list 查看全部 id；新增条目请直接编辑登记表）"
                % (args.mark,))
        old = target["last_rotated"]
        target["last_rotated"] = date_str
        if args.note:
            target["note"] = args.note.strip()
        # 写入前再校验一次：坏 schema 不许落盘（防止把坏数据写进真相文件）
        errs = validate_registry(reg)
        if errs:
            raise RegistryError("待写入的登记表校验失败：\n  - %s" % "\n  - ".join(errs))
        backup = atomic_save(reg, path)
    print("[ROT] 已登记轮换：%s → %s（原值：%s）"
          % (args.mark, date_str, old if old else "从未轮换"))
    if backup:
        print("[ROT] 写前备份：%s" % backup)
    print("[ROT] 原子写入完成：%s" % path)
    return 0


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001  pragma: no cover
        pass

    ap = argparse.ArgumentParser(
        description="S3-04 凭据轮换日历（只存元数据，绝不含秘密值）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="退出码：0 全部在周期内；1 有 overdue/never；2 参数或登记表 schema 错误")
    ap.add_argument("--registry", default=DEFAULT_REGISTRY,
                    help="登记表路径（默认脚本同目录 credential_rotation.json）")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--check", action="store_true", help="检查到期/欠账（有欠账 exit 1）")
    g.add_argument("--list", action="store_true", help="逐条列出（含方式/证据/备注）")
    g.add_argument("--mark", metavar="ID", help="登记一次轮换（原子写 + 写前备份 + 文件锁）")
    g.add_argument("--report", choices=["md"], help="输出 markdown 状态表")
    ap.add_argument("--warn-only", action="store_true",
                    help="只提醒不拦截：即使有欠账也 exit 0")
    ap.add_argument("--date", default="",
                    help="--mark 的轮换日期（YYYY-MM-DD，默认今天）")
    ap.add_argument("--note", default="", help="--mark 的备注（禁止写秘密值）")
    args = ap.parse_args(argv)

    try:
        if args.mark:
            return _cmd_mark(args)
        reg = load_registry(args.registry)
        st = status(registry=reg, today=dt.date.today(), path=args.registry)
        if args.list:
            return _cmd_list(reg, st)
        if args.report:
            print(render_md(st))
            return 0
        rc = _cmd_check(st)
        if args.warn_only and rc == 1:
            print("[ROT] --warn-only：有欠账但按提醒处理（exit 0）")
            return 0
        return rc
    except RegistryError as e:
        print("[ROT] 错误：%s" % e, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
