# -*- coding: utf-8 -*-
"""修复被误改写成「本地」的云端订单来源（2026-10-18 现场）。

判据（本地直接打印的单**不建** order_files 行）：
    orders.source='local' AND orders.openid<>'local' AND EXISTS(order_files)

口径：原来源按 openid 判定 —— `o6...` 等真实微信 openid ⇒ 'wechat'；
      `dev_...` 设备号 ⇒ 'app'；两者都不是 ⇒ 不动（打印出来人工看）。

用法（服务器上）:
    /home/printer-backend/venv/bin/python fix_order_source.py            # 预演
    /home/printer-backend/venv/bin/python fix_order_source.py --apply    # 提交
"""
import sqlite3
import sys

DB = "/home/printer-backend/orders.db"
APPLY = "--apply" in sys.argv

WHERE = """source = 'local' AND openid <> 'local'
           AND openid <> '' AND EXISTS (SELECT 1 FROM order_files f WHERE f.order_id = orders.id)"""


def guess_source(openid: str) -> str:
    if openid.startswith("dev_"):
        return "app"
    if openid.startswith("o") and len(openid) >= 25:   # 微信 openid 形态
        return "wechat"
    return ""


def main():
    conn = sqlite3.connect(DB, timeout=30)
    try:
        rows = conn.execute(
            f"SELECT id, order_number, openid, owner_name, status, created_at "
            f"FROM orders WHERE {WHERE} ORDER BY id").fetchall()
        print(f"命中 {len(rows)} 行（source='local' 但 openid 是真实用户 / 有子任务行）\n")
        plans = []
        for oid, num, openid, owner, status, created in rows:
            src = guess_source(openid or "")
            flag = "→ " + (src or "【无法判定，跳过】")
            print(f"  #{oid:<5} {num:<18} openid={openid[:22]:<24} owner={owner or '-':<8} "
                  f"{status:<8} {created}  {flag}")
            if src:
                plans.append((src, oid))
        print()
        if not APPLY:
            print(f"预演模式：将改写 {len(plans)} 行；确认无误后加 --apply 提交")
            return 0
        if not plans:
            print("无可改写行")
            return 0
        conn.execute("BEGIN IMMEDIATE")
        for src, oid in plans:
            conn.execute("UPDATE orders SET source = ? WHERE id = ?", (src, oid))
        conn.commit()
        print(f"已提交：{len(plans)} 行")
        left = conn.execute(f"SELECT COUNT(*) FROM orders WHERE {WHERE}").fetchone()[0]
        print(f"复核：剩余未修复 {left} 行（应为 0）")
        for src, n in conn.execute("SELECT source, COUNT(*) FROM orders GROUP BY source ORDER BY 2 DESC"):
            print(f"  source={src}: {n}")
        return 0 if left == 0 else 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
