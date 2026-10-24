#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""audit_query.py — S3-04 文件访问审计查询 CLI（读 `<backend>/logs/audit/*.jsonl`）

依据：docs/安全加固修复计划_2026-10-23.md §S3-04（DS-24）「安全运营：文件访问审计日志」。
消费方：季度安全报告（`--stats --json` 的字段是**严格契约**，改字段等于改外部交付件）。

用法：
  # 谁读了哪个文件（按文件 / 按人 / 按动作过滤，时间倒序）
  python3 ops/audit_query.py --file-id 3f2a... --limit 50
  python3 ops/audit_query.py --openid oxxx --action download --since 2026-10-01
  python3 ops/audit_query.py --result denied --since 2026-10-01 --until 2026-10-31

  # 季度报告（JSON 契约，供脚本消费；缺目录时 ok=true 且全 0，不是崩溃）
  python3 ops/audit_query.py --stats --json --since 2026-07-01 --until 2026-09-30

  # 换一个目录（离线拷回来的审计分片 / 演练）
  python3 ops/audit_query.py --dir /tmp/audit_copy --stats --json

说明：
  · `--since/--until` 支持 ISO8601（可带时区）、`%Y-%m-%d %H:%M:%S`、纯日期。
    纯日期作为 `--until` 时补到当天 23:59:59（"查到这一天为止"）。
  · `--stats` 只认 `--since/--until`（按动作/结果过滤是人类查询用的；统计口径固定为
    "这段时间内的全部审计"），传了其它过滤参数会在 stderr 明确提示后被忽略。
  · 审计日志只有 file_id + 扩展名，**没有**原文件名/绝对路径（S2-11）。

退出码：0 = 成功（含"目录不存在 ⇒ 全 0"）；2 = 参数/时间格式错误。
"""
import argparse
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import file_audit  # noqa: E402  （同目录上一级的独立模块，不 import Flask）


def _print_entries(entries):
    if not entries:
        print("（没有匹配的审计记录）")
        return
    print("%-25s %-9s %-10s %-34s %-22s %-10s %-6s %s"
          % ("时间(ts)", "动作", "结果", "file_id", "openid", "actor", "ext", "ip"))
    for e in entries:
        print("%-25s %-9s %-10s %-34s %-22s %-10s %-6s %s"
              % (str(e.get("ts") or "")[:25],
                 str(e.get("action") or "")[:9],
                 str(e.get("result") or "")[:10],
                 str(e.get("file_id") or "")[:34],
                 str(e.get("openid") or "")[:22],
                 str(e.get("actor") or "")[:10],
                 str(e.get("ext") or "")[:6],
                 str(e.get("ip") or "")))


def _print_stats(payload):
    print("审计分片: %d  记录: %d" % (payload["files"], payload["lines"]))
    print("拒绝(denied): %d" % payload["denied"])
    print("按动作: %s" % (json.dumps(payload["by_action"], ensure_ascii=False, sort_keys=True) or "{}"))
    print("按结果: %s" % (json.dumps(payload["by_result"], ensure_ascii=False, sort_keys=True) or "{}"))
    if payload.get("since") or payload.get("until"):
        print("区间: %s ~ %s" % (payload.get("since") or "(不限)", payload.get("until") or "(不限)"))


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="S3-04 文件访问审计查询（谁、读了哪个文件、何时）",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", default="",
                    help="审计目录（默认 <backend>/logs/audit；也可用 HN_FILE_AUDIT_DIR）")
    ap.add_argument("--file-id", dest="file_id", default="", help="按 file_id 精确过滤")
    ap.add_argument("--openid", default="", help="按 openid 精确过滤")
    ap.add_argument("--action", default="", help="按动作过滤：download / delete / list")
    ap.add_argument("--result", default="", help="按结果过滤：ok / denied / not_found / conflict")
    ap.add_argument("--since", default="", help="起始时间（含）")
    ap.add_argument("--until", default="", help="截止时间（含；纯日期=当天末尾）")
    ap.add_argument("--limit", type=int, default=200, help="最多返回条数（1..500，默认 200）")
    ap.add_argument("--stats", action="store_true", help="输出聚合统计（供季度报告）")
    ap.add_argument("--json", dest="as_json", action="store_true", help="机器可读 JSON 输出")
    args = ap.parse_args(argv)

    if args.dir:
        os.environ[file_audit.AUDIT_DIR_ENV] = args.dir

    if args.stats:
        ignored = [name for name, val in (("--file-id", args.file_id), ("--openid", args.openid),
                                          ("--action", args.action), ("--result", args.result))
                   if val]
        if ignored:
            print("[提示] --stats 只按 --since/--until 统计，已忽略 %s"
                  % " ".join(ignored), file=sys.stderr)
        try:
            s = file_audit.stats(since=args.since or None, until=args.until or None)
        except ValueError as e:
            print("[错误] %s" % e, file=sys.stderr)
            return 2
        # 严格契约（季度报告消费）：字段名/类型不可变，只能加字段不能改名。
        payload = {
            "ok": True,
            "files": s["files"],
            "lines": s["lines"],
            "by_action": s["by_action"],
            "by_result": s["by_result"],
            "denied": s["denied"],
            "since": args.since or "",
            "until": args.until or "",
        }
        if args.as_json:
            print(json.dumps(payload, ensure_ascii=False))
        else:
            _print_stats(payload)
        return 0

    try:
        entries = file_audit.query(
            file_id=args.file_id or None,
            openid=args.openid or None,
            action=args.action or None,
            result=args.result or None,
            since=args.since or None,
            until=args.until or None,
            limit=args.limit)
    except ValueError as e:
        print("[错误] %s" % e, file=sys.stderr)
        return 2

    if args.as_json:
        print(json.dumps({"ok": True, "count": len(entries), "entries": entries},
                         ensure_ascii=False))
    else:
        _print_entries(entries)
    return 0


if __name__ == "__main__":
    sys.exit(main())
