#!/usr/bin/env python3
r"""ops/stuck_subtasks.py — 把「排队中但目标设备不可用」的子任务**改派**到另一台设备。

为什么需要
----------
`process_pending_orders` 每轮只捞 `order_files WHERE status='queued'`。子任务的
`orders.target_client` 指向一台不在线/未接单的设备时，它会一直排队 —— 这是**故意的**：
设备可能一会儿就回来，自动判失败会让顾客重下单。但设备一旦换了 client_id
（现场：`R7000-AHP10-168dbdc3d1` → `RN-AHPN-NdbdcNdN`），那批子任务就永远绑在旧身份上。
2026-11-24 日志体检实测：25 条这样的子任务被轮询 10,234 次。

本工具只做一件事
---------------
    UPDATE orders SET target_client = ? WHERE id = ?

**只改派，不作废**：不碰 `order_files.status`、不碰 `orders.status`、不碰任何金额列。
作废属于订单状态订正，要动 `MANUAL_FIX_STATUSES` / 结算口径（硬规矩 6、7），口径未定之前不开这扇门。

用法
----
    python3 ops/stuck_subtasks.py                          # 列出所有排队子任务（只读）
    python3 ops/stuck_subtasks.py --reassign-of 123 --to CLIENT --dry-run
    python3 ops/stuck_subtasks.py --reassign-of 123 --to CLIENT --yes
    python3 ops/stuck_subtasks.py --reassign-from OLD_CLIENT --to NEW_CLIENT --yes

没带 `--yes` 一律**只打印将执行的 SQL**，不写库。

已知限制（写在这里，别指望它）
------------------------------
「目标设备在不在线」是服务进程内存里的状态（`get_active_clients()`），库里没有设备表，
所以本工具**判断不出在线与否**，只能列出"谁是目标、等了多久"。在线与否请对照
`journalctl -u printer-backend | grep '\[WAIT\]'` —— 那条日志现在会带订单号和目标设备。
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(os.path.dirname(HERE), "orders.db")

# 终态集合（与 app.py MANUAL_FIX_STATUSES 同源口径）。本工具只用它**提示**"这单已经动过了"，
# 不用它做任何写入。
TERMINAL = ("sent", "failed", "abandoned", "canceled", "rejected", "timeout")


def _age(created_at: str) -> str:
    try:
        t = datetime.strptime(str(created_at)[:19], "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return "?"
    d = datetime.now() - t
    if d.total_seconds() < 0:
        return "0m"
    h, m = divmod(int(d.total_seconds() // 60), 60)
    return f"{h}h{m:02d}m" if h else f"{m}m"


def _connect(path: str) -> sqlite3.Connection:
    if not os.path.isfile(path):
        raise SystemExit(f"数据库不存在: {path}")
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _queued(conn: sqlite3.Connection, where: str = "", params: tuple = ()):
    sql = """
        SELECT of.id AS of_id, of.order_id, of.status AS of_status,
               o.order_number, o.target_client, of.created_at
        FROM order_files of
        JOIN orders o ON of.order_id = o.id
        WHERE of.status = 'queued'
    """
    if where:
        sql += " AND " + where
    sql += " ORDER BY of.created_at"
    return list(conn.execute(sql, params))


def _order_file_mix(conn: sqlite3.Connection, order_id: int) -> dict:
    """这个订单下各状态的文件数（用来提示"已经不是纯排队单了"）。"""
    out: dict[str, int] = {}
    for r in conn.execute(
            "SELECT status, COUNT(*) n FROM order_files WHERE order_id = ? GROUP BY status",
            (order_id,)):
        out[str(r["status"])] = int(r["n"])
    return out


def cmd_list(conn: sqlite3.Connection) -> int:
    rows = _queued(conn)
    if not rows:
        print("没有排队中的子任务。")
        return 0
    print(f"排队中的子任务 {len(rows)} 条：")
    print(f"  {'of_id':>7}  {'订单号':<20} {'目标设备':<34} {'已等':>7}  订单内状态分布")
    for r in rows:
        mix = _order_file_mix(conn, int(r["order_id"]))
        mix_s = " ".join(f"{k}={v}" for k, v in sorted(mix.items()))
        # 混单标记刻意用 ASCII 的 [MIXED]：验收脚本要 grep 它，而拿中文文案当判据是
        # verify_guard_quality 棘轮一直在压的反模式。
        mixed = "  [MIXED] 与终态混单" if any(k in TERMINAL for k in mix) else ""
        print(f"  {r['of_id']:>7}  {str(r['order_number'] or '-'):<20} "
              f"{str(r['target_client'] or '(未指定)'):<34} {_age(r['created_at']):>7}  {mix_s}{mixed}")
    print()
    print("（目标设备在不在线只在服务内存里，库里没有设备表 —— 请对照 "
          "`journalctl -u printer-backend | grep '\\[WAIT\\]'`）")
    return 0


def _do_reassign(conn: sqlite3.Connection, order_ids: set, to_client: str,
                 apply: bool) -> int:
    if not order_ids:
        print("没有需要改派的订单。")
        return 0
    print(f"将把 {len(order_ids)} 个订单的 target_client 改为 {to_client!r}：")
    for oid in sorted(order_ids):
        cur = conn.execute("SELECT target_client FROM orders WHERE id = ?", (oid,)).fetchone()
        mix = _order_file_mix(conn, oid)
        queued_n = mix.get("queued", 0)
        mixed = any(k in TERMINAL for k in mix)
        print(f"  order#{oid}: {str(cur['target_client'] or '-')!s} → {to_client}   "
              f"排队 {queued_n} 个" + ("   ⚠ 同单还有终态文件（改派只影响后续派单，已终态的不动）"
                                       if mixed else ""))
    print("  SQL: UPDATE orders SET target_client = ? WHERE id IN (...)")
    if not apply:
        print("\n[dry-run] 未写库。加 --yes 才执行。")
        return 0
    with conn:
        for oid in sorted(order_ids):
            conn.execute("UPDATE orders SET target_client = ? WHERE id = ?", (to_client, oid))
    print(f"\n已改派 {len(order_ids)} 个订单（order_files.status 一个都没动）。")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="把排队中的子任务改派到另一台设备（只改 target_client，不作废）")
    ap.add_argument("--db", default=DEFAULT_DB, help=f"数据库路径（默认 {DEFAULT_DB}）")
    ap.add_argument("--list", action="store_true", help="列出排队中的子任务（默认行为，只读）")
    ap.add_argument("--reassign-of", type=int, metavar="OF_ID",
                    help="按子任务 id 改派它所属的订单")
    ap.add_argument("--reassign-from", metavar="CLIENT",
                    help="把所有目标设备为 CLIENT 的排队子任务改派走")
    ap.add_argument("--to", metavar="CLIENT", default="", help="新目标设备 client_id")
    ap.add_argument("--dry-run", action="store_true", help="只打印，不写库（默认行为）")
    ap.add_argument("--yes", action="store_true", help="真的写库")
    args = ap.parse_args(argv)

    conn = _connect(args.db)
    try:
        if not args.reassign_of and not args.reassign_from:
            return cmd_list(conn)

        to_client = (args.to or "").strip()
        if not to_client:
            print("--to 不能为空：改派必须给出新目标设备 client_id。", file=sys.stderr)
            return 2
        apply = bool(args.yes) and not args.dry_run

        order_ids: set[int] = set()
        if args.reassign_of is not None:
            row = conn.execute(
                "SELECT of.id, of.order_id, of.status FROM order_files of WHERE of.id = ?",
                (args.reassign_of,)).fetchone()
            if row is None:
                print(f"子任务 #{args.reassign_of} 不存在。", file=sys.stderr)
                return 2
            if str(row["status"]) != "queued":
                print(f"子任务 #{args.reassign_of} 状态是 {row['status']!r}，不是排队中 —— "
                      f"改派只对 queued 有意义。", file=sys.stderr)
                return 2
            order_ids.add(int(row["order_id"]))
        if args.reassign_from:
            rows = _queued(conn, "o.target_client = ?", (args.reassign_from,))
            if not rows:
                print(f"没有目标设备为 {args.reassign_from!r} 的排队子任务。")
                return 0
            order_ids |= {int(r["order_id"]) for r in rows}

        # 只为"确实还有排队文件"的订单改派
        keep = set()
        for oid in order_ids:
            if _order_file_mix(conn, oid).get("queued", 0) > 0:
                keep.add(oid)
            else:
                print(f"  order#{oid} 没有排队中的文件，跳过。")
        return _do_reassign(conn, keep, to_client, apply)
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
