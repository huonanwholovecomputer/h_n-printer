#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
agent_cli.py — 命令行调用本地打印工具的「智能助手接口」（agent_api.py）

给 DSH（或任何本机脚本）用的一条命令入口：不用自己拼 HTTP、不用记端口和令牌
（都从 %APPDATA%\\HN打印工具\\agent_api.json 读）。

典型用法（Windows bash / PowerShell 都行）：

    python tools/agent_cli.py ping
    python tools/agent_cli.py status
    python tools/agent_cli.py tabs
    python tools/agent_cli.py new
    python tools/agent_cli.py add --tab 2 --file "D:/微信/作业.pdf" --duplex on --copies 2
    python tools/agent_cli.py add --new-tab --file a.pdf --file b.docx --remark "3 号楼 201"
    python tools/agent_cli.py set --tab 2 --index 0 --page-range "1-3,5"
    python tools/agent_cli.py get --tab 2
    python tools/agent_cli.py rm --tab 2 --index 1
    python tools/agent_cli.py print --tab 2 --yes
    python tools/agent_cli.py call files.add --json "{\\"paths\\":[\\"a.pdf\\"]}"

输出一律是 JSON（UTF-8 原文，不转义中文），成功 exit 0、失败 exit 1 ——
失败时 stdout 里也有 {"ok": false, "error": {...}}，调用方按需解析即可。
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent_api  # noqa: E402


def _emit(payload: dict, pretty: bool) -> int:
    text = json.dumps(payload, ensure_ascii=False, indent=2 if pretty else None)
    try:
        sys.stdout.write(text + "\n")
    except UnicodeEncodeError:                        # 老控制台：退回转义输出
        sys.stdout.write(json.dumps(payload, ensure_ascii=True) + "\n")
    return 0 if payload.get("ok") else 1


def _call(op: str, params: dict, args) -> dict:
    return agent_api.request(op, params, timeout=args.timeout)


def _job_options(args) -> dict:
    """命令行里的单文件参数 → options（只放用户真的写了的项）。"""
    opts = {}
    if args.copies is not None:
        opts["copies"] = args.copies
    if args.duplex is not None:
        opts["duplex"] = args.duplex
    if args.duplex_mode is not None:
        opts["duplex_mode"] = args.duplex_mode
    if args.page_range is not None:
        opts["page_range"] = args.page_range
    if args.simplex_pages is not None:
        opts["simplex_pages"] = args.simplex_pages
        opts["simplex_enabled"] = True
    if args.simplex_off:
        opts["simplex_enabled"] = False
    if args.engine is not None:
        opts["engine"] = args.engine
    if args.dpi is not None:
        opts["dpi"] = args.dpi
    if args.remark is not None:
        opts["remark"] = args.remark
    if args.name is not None:
        opts["display_name"] = args.name
    return opts


def _tab_options(args) -> dict:
    opts = {}
    for attr, key in (("delivery", "delivery_enabled"), ("location", "delivery_location"),
                      ("urgency", "urgency"), ("cover_page", "cover_page"),
                      ("cover_price", "cover_page_price"), ("owner", "owner_name"),
                      ("admin_print", "is_admin_print"), ("tab_remark", "remark")):
        value = getattr(args, attr, None)
        if value is not None:
            opts[key] = value
    return opts


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="本地打印工具 · 智能助手接口命令行",
                                formatter_class=argparse.RawDescriptionHelpFormatter,
                                epilog=__doc__)
    p.add_argument("--pretty", action="store_true", help="缩进输出（默认单行 JSON）")
    p.add_argument("--timeout", type=float, default=60.0, help="单次调用超时秒数（默认 60）")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("ping", help="探活（不需要令牌，也不需要程序开着接口）")

    sub.add_parser("status", help="程序/打印机/忙碌状态")
    sub.add_parser("tabs", help="全部标签页摘要")

    g = sub.add_parser("get", help="看一个标签页的明细（含每个文件）")
    g.add_argument("--tab", required=True)

    n = sub.add_parser("new", help="新建标签页")
    n.add_argument("--no-switch", action="store_true", help="建好后切回原标签页")

    a = sub.add_parser("add", help="往标签页加文件")
    a.add_argument("--file", action="append", required=True, dest="files", help="可重复")
    a.add_argument("--tab", help="目标标签页（默认当前页）")
    a.add_argument("--new-tab", action="store_true", help="新建一个标签页再放")
    a.add_argument("--no-switch", action="store_true", help="加完切回原标签页")
    a.add_argument("--copies", type=int)
    a.add_argument("--duplex", choices=["on", "off"])
    a.add_argument("--duplex-mode", choices=["long-edge", "short-edge"])
    a.add_argument("--page-range", help='如 "1-3,5"；空串=全部页')
    a.add_argument("--simplex-pages", help='封面单独打印的页码，如 "1"')
    a.add_argument("--simplex-off", action="store_true", help="取消封面单独打印")
    a.add_argument("--engine", choices=["word", "wps", "libreoffice"])
    a.add_argument("--dpi", type=int, choices=[0, 200, 300, 400, 600])
    a.add_argument("--name", help="显示名（默认文件名）")
    a.add_argument("--remark", help="这个文件的备注")

    s = sub.add_parser("set", help="改某个文件的参数")
    s.add_argument("--tab", help="默认当前页")
    s.add_argument("--index", type=int, required=True, help="文件序号，从 0 开始")
    s.add_argument("--copies", type=int)
    s.add_argument("--duplex", choices=["on", "off"])
    s.add_argument("--duplex-mode", choices=["long-edge", "short-edge"])
    s.add_argument("--page-range")
    s.add_argument("--simplex-pages")
    s.add_argument("--simplex-off", action="store_true")
    s.add_argument("--engine", choices=["word", "wps", "libreoffice"])
    s.add_argument("--dpi", type=int, choices=[0, 200, 300, 400, 600])
    s.add_argument("--name")
    s.add_argument("--remark")

    r = sub.add_parser("rm", help="移除某个文件")
    r.add_argument("--tab")
    r.add_argument("--index", type=int, required=True)

    c = sub.add_parser("clear", help="清空标签页里的文件（保留标签页）")
    c.add_argument("--tab")

    d = sub.add_parser("del", help="删除整个标签页")
    d.add_argument("--tab", required=True)
    d.add_argument("--force", action="store_true", help="连已冻结（出过订单）的也删")

    t = sub.add_parser("tabset", help="改标签页的附加服务/归属/备注")
    t.add_argument("--tab")
    t.add_argument("--delivery", type=lambda v: v.lower() in ("1", "true", "yes", "on"))
    t.add_argument("--location", help="派送地点，如 图书馆")
    t.add_argument("--urgency", choices=["低", "中", "高"])
    t.add_argument("--cover-page", type=lambda v: v.lower() in ("1", "true", "yes", "on"))
    t.add_argument("--cover-price", type=float)
    t.add_argument("--owner", help="订单归属成员姓名")
    t.add_argument("--admin-print", type=lambda v: v.lower() in ("1", "true", "yes", "on"))
    t.add_argument("--tab-remark")

    pr = sub.add_parser("print", help="开始打印（真的出纸，必须加 --yes）")
    pr.add_argument("--tab")
    pr.add_argument("--yes", action="store_true", help="确认开始打印")

    call = sub.add_parser("call", help="直接调一个 op（原始 JSON 参数）")
    call.add_argument("op")
    call.add_argument("--json", default="{}", help="params 的 JSON 字符串")

    cl = sub.add_parser("cloud", help="看云端订单数据（只读、不限时间）")
    cl.add_argument("--order", help="订单号（HN20261105-0015）或 order_id")
    cl.add_argument("--status", help="逗号分隔：sent,failed,timeout,abandoned,canceled…")
    cl.add_argument("--source", choices=["wechat", "app", "local", "cloud"])
    cl.add_argument("--owner", help="归属名（模糊匹配）")
    cl.add_argument("--file", help="文件名（模糊匹配）")
    cl.add_argument("--since", help='起始时间，如 "2026-11-01" 或 "2026-11-01 08:00:00"')
    cl.add_argument("--until", help='截止时间（含），格式同上')
    cl.add_argument("--limit", type=int, default=200, help="1..1000，默认 200")
    cl.add_argument("--offset", type=int, default=0)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    cmd = args.cmd

    if cmd == "ping":
        return _emit(agent_api.ping(), args.pretty)

    if cmd == "call":
        try:
            params = json.loads(args.json or "{}")
        except ValueError as e:
            return _emit({"ok": False, "error": {"code": "bad_params",
                                                "message": f"--json 不是合法 JSON: {e}"}}, args.pretty)
        return _emit(_call(args.op, params, args), args.pretty)

    if cmd == "status":
        return _emit(_call("status", {}, args), args.pretty)
    if cmd == "tabs":
        return _emit(_call("tabs.list", {}, args), args.pretty)
    if cmd == "get":
        return _emit(_call("tabs.get", {"tab": args.tab}, args), args.pretty)
    if cmd == "new":
        return _emit(_call("tabs.create", {"switch": not args.no_switch}, args), args.pretty)
    if cmd == "add":
        params = {"paths": list(args.files), "switch": not args.no_switch}
        if args.new_tab:
            params["new_tab"] = True
        elif args.tab:
            params["tab"] = args.tab
        opts = _job_options(args)
        if opts:
            params["options"] = opts
        return _emit(_call("files.add", params, args), args.pretty)
    if cmd == "set":
        params = {"index": args.index}
        if args.tab:
            params["tab"] = args.tab
        params.update(_job_options(args))
        return _emit(_call("files.update", params, args), args.pretty)
    if cmd == "rm":
        params = {"index": args.index}
        if args.tab:
            params["tab"] = args.tab
        return _emit(_call("files.remove", params, args), args.pretty)
    if cmd == "clear":
        params = {"tab": args.tab} if args.tab else {}
        return _emit(_call("tabs.clear", params, args), args.pretty)
    if cmd == "del":
        return _emit(_call("tabs.delete", {"tab": args.tab, "force": args.force}, args), args.pretty)
    if cmd == "tabset":
        params = _tab_options(args)
        if args.tab:
            params["tab"] = args.tab
        return _emit(_call("tabs.update", params, args), args.pretty)
    if cmd == "print":
        params = {"confirm": bool(args.yes)}
        if args.tab:
            params["tab"] = args.tab
        return _emit(_call("tabs.print", params, args), args.pretty)
    if cmd == "cloud":
        params = {}
        for key in ("order", "status", "source", "owner", "file", "since", "until"):
            val = getattr(args, key, None)
            if val:
                params[key] = val
        if args.limit is not None:
            params["limit"] = args.limit
        if args.offset:
            params["offset"] = args.offset
        return _emit(_call("cloud.orders", params, args), args.pretty)

    return _emit({"ok": False, "error": {"code": "bad_command", "message": cmd}}, args.pretty)


if __name__ == "__main__":
    sys.exit(main())
