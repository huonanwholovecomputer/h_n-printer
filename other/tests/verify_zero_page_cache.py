"""verify_zero_page_cache.py — 缓存「0 页」两处收口（2026-11-24 P0-1 第 2 步，修正版）。

**修正记录（重要）**：第一版把「索引里 page_count<=0 就当未命中 / 不写索引」当成修法，
被两条既有验收打红（`verify_cloud_download_staging` §8、`verify_network_resilience` §5），
复查找到了真正的原因：

    `_save_pdf_to_cache(..., page_count=0)` 是**正常写法** ——
    `cloud_client._download_file_inner` 下载完成时注释就写着「页数在打印时确定」（2164 行）。
    所以索引里的 0 是「下载时还没数页」的**占位**，不是坏数据；把它当未命中会把云端的
    PDF 缓存整个废掉（每单重下）。

真正的缺陷在**读侧把它当真页数用**：云端命中分支原来 `if page_count > 0:` 才去数页，
否则跳过 `get_pdf_info` 直接记「→ 0 页」，那 0 进了报价
（11-23 22:20 记 0 页、22:32 同一单实际打了 20 页）。

现在的判据：**索引正数就用索引；<=0 就回 PDF 本身数**（`resolve_cached_page_count`，
用的还是同一把 `get_pdf_info`，不另立口径、不编数字）。

断言：
  A 页数解析：索引 0 ⇒ 回读 PDF；索引正数 ⇒ 直接用（不白读盘）；都拿不到 ⇒ 0（交给下游标"未知"）
  B 变异自证：把回读函数换成恒 0 ⇒ A 段判据变红
  C 死条目收敛：索引有、文件没的条目剔除，但给 10 分钟宽限避开落盘竞态
  D 变异自证：宽限改 0 ⇒ C 段"刚落盘的被保住"判据变红
"""
from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(ROOT / "local_print_tool"))

fails: list[str] = []
checks = 0


def check(name: str, cond: bool, detail: str = "") -> bool:
    global checks
    checks += 1
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}" + (f"  ← {detail}" if detail else ""))
        fails.append(name)
    return bool(cond)


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.messages: list[str] = []

    def emit(self, record):
        self.messages.append(record.getMessage())


def _write_pdf(path: str) -> None:
    with open(path, "wb") as f:
        f.write(b"%PDF-1.4\n")


def _read_index(cache_dir: str) -> dict:
    p = os.path.join(cache_dir, "index.json")
    if not os.path.isfile(p):
        return {}
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


def main() -> int:
    print("=" * 78)
    print("verify_zero_page_cache.py — 缓存 0 页收口（P0-1 第 2 步 · 修正版）")
    print("=" * 78)

    import xlsx_printer as xp
    import retention
    import cloud_client as cc

    cap = _Capture()
    for lg in (xp.logger, retention.logger, cc.logger):
        lg.addHandler(cap)
        lg.setLevel(logging.DEBUG)

    try:
        # ── A. 页数解析：0 是占位，回 PDF 本身数 ──────────────────
        print("\n=== A. resolve_cached_page_count：索引 0 必须回读 PDF ===")
        calls = {"n": 0}

        def fake_info(pdf_path):
            calls["n"] += 1
            return {"page_count": 20, "orientation": "portrait"}

        n = cc.resolve_cached_page_count({"page_count": 0}, "x.pdf", info_fn=fake_info)
        check("A1 索引 0 页 ⇒ 回读 PDF 得到 20", n == 20, f"实得 {n}")
        check("A2 确实去读了 PDF（不是凭空编的）", calls["n"] == 1, f"实得调用 {calls['n']} 次")

        calls["n"] = 0
        n2 = cc.resolve_cached_page_count({"page_count": 7}, "x.pdf", info_fn=fake_info)
        check("A3 索引正数 ⇒ 直接用索引 7", n2 == 7, f"实得 {n2}")
        check("A4 索引正数时不白读盘", calls["n"] == 0, f"实得调用 {calls['n']} 次")

        n3 = cc.resolve_cached_page_count({}, "x.pdf", info_fn=fake_info)
        check("A5 索引没这个键 ⇒ 也回读 PDF", n3 == 20, f"实得 {n3}")

        n4 = cc.resolve_cached_page_count({"page_count": 0}, "x.pdf",
                                          info_fn=lambda p: {"page_count": 0})
        check("A6 两边都是 0 ⇒ 返回 0（交给下游标『未知』，不编数字）", n4 == 0, f"实得 {n4}")

        n5 = cc.resolve_cached_page_count({"page_count": 0}, "x.pdf",
                                          info_fn=lambda p: (_ for _ in ()).throw(OSError("bad")))
        check("A7 回读抛异常 ⇒ 退回 0 且不炸", n5 == 0, f"实得 {n5}")

        # ── B. 变异自证 ───────────────────────────────────────────
        print("\n=== B. 变异自证：回读恒 0 ⇒ A1/A5 判据必须变红 ===")
        zero_info = lambda p: {"page_count": 0}   # noqa: E731
        check("B1 回读恒 0 时 A1 的判据（=20）不再成立",
              cc.resolve_cached_page_count({"page_count": 0}, "x.pdf", info_fn=zero_info) != 20)

        # ── C. 死条目收敛 + 宽限（保留：这条有独立证据）────────────
        print("\n=== C. sweep_pdf_cache 剔除死条目，但给刚落盘的宽限 ===")
        d3 = tempfile.mkdtemp(prefix="hn_idx_sweep_")
        now = time.time()
        old = (datetime.now() - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
        fresh = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        _write_pdf(os.path.join(d3, "live0001_ewps.pdf"))
        index = {
            "live0001_ewps": {"page_count": 4, "created_at": fresh},
            "dead0001_ewps": {"page_count": 3, "created_at": old},   # 文件没了、创建很久了
            "fresh001_ewps": {"page_count": 2, "created_at": fresh},  # 文件也没了，但刚创建
        }
        removed, new_index, _freed = retention.sweep_pdf_cache(d3, dict(index), hours=24, now=now)
        check("C1 文件在的条目保住", "live0001_ewps" in new_index, f"实得 keys={list(new_index)}")
        check("C2 文件没了且已过宽限的条目被剔除", "dead0001_ewps" not in new_index,
              f"实得 keys={list(new_index)}")
        check("C3 文件没了但刚落盘的条目**暂时**保住（避开落盘竞态）",
              "fresh001_ewps" in new_index, f"实得 keys={list(new_index)}")
        check("C4 剔除计入 removed（保证索引会被落盘）", removed >= 1, f"removed={removed}")
        check("C5 没动任何 PDF 文件（只清索引）",
              os.path.isfile(os.path.join(d3, "live0001_ewps.pdf")))

        # ── D. 变异自证 ───────────────────────────────────────────
        print("\n=== D. 变异自证：宽限改 0 ⇒ C3 判据必须变红 ===")
        saved = retention._INDEX_MISSING_GRACE_S
        try:
            retention._INDEX_MISSING_GRACE_S = 0.0
            _r2, idx2, _f2 = retention.sweep_pdf_cache(d3, dict(index), hours=24, now=now)
            check("D1 宽限为 0 时刚落盘的死条目也被剔除（C3 的判据因此有牙）",
                  "fresh001_ewps" not in idx2, f"实得 keys={list(idx2)}")
            check("D2 文件在的那条仍然保住", "live0001_ewps" in idx2)
        finally:
            retention._INDEX_MISSING_GRACE_S = saved

        # ── E. 写侧不该挡正常占位（回归守卫）──────────────────────
        print("\n=== E. 回归守卫：下载时的 page_count=0 占位必须照常写进索引 ===")
        d4 = tempfile.mkdtemp(prefix="hn_idx_placeholder_")
        xp.upsert_cache_index(d4, "aaaa1111_ewps", {"page_count": 0, "created_at": fresh})
        check("E1 page_count=0 的占位条目照常写入（不能被当坏数据挡掉）",
              "aaaa1111_ewps" in _read_index(d4), f"实得 keys={list(_read_index(d4))}")
    finally:
        for lg in (xp.logger, retention.logger, cc.logger):
            lg.removeHandler(cap)

    print("\n" + "=" * 78)
    if fails:
        print(f"FAILED  {len(fails)}/{checks}: {fails}")
        return 1
    print(f"ALL PASS  {checks}/{checks}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
