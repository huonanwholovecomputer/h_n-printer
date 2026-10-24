#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""S3-02/S3-03 共用的 SQLite 外键违例分类（唯一真相，别在多个脚本里各写一份）。

背景（2026-10-24 生产演练实测）
--------------------------------
线上 `orders.db` 的 `PRAGMA foreign_key_check` 有 **228 条**违例，全部是同一类：

    orders.file_id → files.id

`files` 行到期后由保留期清理（S1-03 / S2-05 引用计数）删掉，但 `orders` 行**刻意保留**
`file_id` 作历史凭据（结算、追溯、重印都要用）；SQLite 默认 `PRAGMA foreign_keys=OFF`，
所以这类"指向已过期文件"的历史引用一直存在。它**不是损坏**，也不能靠"删订单"来消掉。

判据（两个演练工具必须同口径）
------------------------------
· 良性类 = `BENIGN_FK_RULES` 里的 (table, parent) 组合 ⇒ 计入 benign、写进报告 warnings；
· 其它组合（例如某次迁移写坏的外键、别的表指向缺失父行）⇒ unexpected，**判失败**；
· 用户库（users.db）没有"保留期历史引用"的豁免 ⇒ 调用方传 `benign_rules=set()`；
· 需要"任何违例即失败"的严格口径时同样传 `benign_rules=set()`（db_migration_drill 的
  `--strict-fk` 就是这么实现的）。

改这个文件前先看 `tests/verify_db_migration.py` 与 `tests/verify_ha_drill.py`：两边都有
"良性不判失败 / 非良性必须失败 / 变异删掉判据必须红"的用例。
"""

#: 已知良性外键违例：(子表, 父表)。新增条目必须能说清"为什么不是损坏"。
BENIGN_FK_RULES = frozenset({("orders", "files")})

#: 生产上该良性类的成因说明（写进报告 warnings，运维一眼能懂）。
BENIGN_FK_RULE_NOTES = {
    ("orders", "files"): "文件到期已清理（S1-03/S2-05），订单保留历史 file_id 作凭据",
}


def classify_fk_violations(conn, benign_rules=BENIGN_FK_RULES) -> dict:
    """把 `PRAGMA foreign_key_check` 的结果按 (table -> parent) 分类。

    返回：
      {"total": int, "benign": int, "unexpected": int,
       "detail": {"table->parent": n}, "samples": [{table,rowid,parent}], "error": str}
    `total == -1` 表示 PRAGMA 本身执行失败（坏库/锁），调用方按"无法体检"处理。
    """
    try:
        rows = conn.execute("PRAGMA foreign_key_check").fetchall()
    except Exception as e:  # noqa: BLE001 —— 坏库/锁都可能在这里抛
        return {"total": -1, "benign": 0, "unexpected": 0, "detail": {},
                "samples": [], "error": "%s: %s" % (type(e).__name__, e)}
    detail: dict = {}
    samples: list = []
    benign = unexpected = 0
    for table, rowid, parent, _fkid in rows:
        key = "%s->%s" % (table, parent)
        detail[key] = detail.get(key, 0) + 1
        if (str(table), str(parent)) in benign_rules:
            benign += 1
        else:
            unexpected += 1
            if len(samples) < 5:
                samples.append({"table": table, "rowid": rowid, "parent": parent})
    return {"total": len(rows), "benign": benign, "unexpected": unexpected,
            "detail": detail, "samples": samples, "error": ""}


def warning_text(fk: dict) -> str:
    """把良性违例渲染成一条报告用 warning（没有良性违例时返回空串）。"""
    if not fk or not fk.get("benign"):
        return ""
    notes = []
    for key in fk.get("detail") or {}:
        child, _, parent = key.partition("->")
        note = BENIGN_FK_RULE_NOTES.get((child, parent))
        if note:
            notes.append("%s（%s）" % (key, note))
    return ("外键违例 %d 条属于已知良性类（%s），不计失败；明细 %s"
            % (fk.get("benign"), "；".join(notes) or "见 BENIGN_FK_RULES", fk.get("detail")))
