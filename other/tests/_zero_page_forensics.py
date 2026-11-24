"""_zero_page_forensics.py — P0-1 取证：『缓存命中 → 0 页』到底是索引写坏还是真 0 页文件。

只读，不联网，不动订单、不动缓存。两个数据源：
  1. 7 天日志里 17 条「📦 缓存命中: <名> → 0 页 (MD5=xxxxxxxx...)」的 MD5 前 8 位；
  2. 本机 pdf_cache 目录里现存 PDF 的真实页数（fitz 读 / pdfinfo 兜底）。

判定：
  · MD5 仍命中某个现存 PDF 且真实页数 > 0  ⇒ **索引写坏了**（缓存 meta.page_count 被写成 0），
    修法 = 把 page_count<=0 判为未命中重转。
  · MD5 找不到对应文件                     ⇒ 缓存已清、索引残留（meta 索引表没跟着清），
    修法 = 同上（未命中重走转换）。
  · 命中且真实页数确实 = 0                ⇒ 真 0 页文件，修法改成只加强日志 + 上报标记。

脱敏：文件名只打**脱敏指纹**（扩展名 + 长度 + 首字符 + 末 2 字符），不打原名 ——
pdf_cache 里的名字是 MD5 命名的，但日志里的是顾客原始文件名。
"""
from __future__ import annotations

import hashlib
import os
import re
import sys
from collections import Counter
from pathlib import Path

LOG_DIR = Path.home() / "AppData" / "Roaming" / "HN打印工具" / "logs"
PDF_CACHE = Path.home() / "AppData" / "Roaming" / "HN打印工具" / "pdf_cache"

ZERO = re.compile(r"缓存命中:\s*(?P<name>.+?)\s*→\s*0\s*页\s*\(MD5=(?P<md5>[0-9a-fA-F]+)")
DAYS = [f"2026-11-{i:02d}.log" for i in range(17, 24)]


def mask(name: str) -> str:
    """顾客文件名脱敏：只留扩展名、长度、首字符、末两字符。"""
    stem, ext = os.path.splitext(name)
    if len(stem) <= 4:
        core = stem[:1] + "*" * max(0, len(stem) - 1)
    else:
        core = f"{stem[0]}{'*' * (len(stem) - 3)}{stem[-2:]}"
    return f"{core}{ext}(len={len(name)})"


def pdf_pages(path: Path) -> int:
    try:
        import fitz
        with fitz.open(str(path)) as doc:
            return int(doc.page_count)
    except Exception:
        pass
    # 兜底：数 /Type /Page（不含 /Type /Pages）
    try:
        raw = path.read_bytes()
        return len(re.findall(rb"/Type\s*/Page(?![s])", raw))
    except Exception:
        return -1


def main() -> int:
    print("=" * 78)
    print("P0-1 取证：『缓存命中 → 0 页』归因（只读 / 脱敏）")
    print("=" * 78)

    hits: list[tuple[str, str, str]] = []      # (day, masked_name, md5prefix)
    for day in DAYS:
        f = LOG_DIR / day
        if not f.is_file():
            continue
        for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
            m = ZERO.search(line)
            if m:
                hits.append((day, m.group("name"), m.group("md5")[:8]))

    print(f"\n日志侧：{len(hits)} 条「→ 0 页」记录")
    for day, name, pre in hits:
        print(f"  {day}  {mask(name):32s}  MD5={pre}...")

    if not PDF_CACHE.is_dir():
        print(f"\npdf_cache 不存在：{PDF_CACHE}")
        return 2
    files = list(PDF_CACHE.rglob("*.pdf"))
    print(f"\n缓存侧：{PDF_CACHE} 下 {len(files)} 个 PDF")

    # 建 MD5 前缀索引（文件名通常是 <md5>_<引擎>.pdf，也可能在子目录里）
    by_prefix: dict[str, list[Path]] = {}
    by_hash_file: dict[str, Path] = {}
    for p in files:
        by_prefix.setdefault(p.name[:8].lower(), []).append(p)
        try:
            h = hashlib.md5(p.read_bytes()).hexdigest()
            by_hash_file.setdefault(h, p)
        except OSError:
            pass

    print("\n" + "-" * 78)
    print("逐条归因")
    print("-" * 78)
    verdict: Counter = Counter()
    for day, name, pre in hits:
        cands = by_prefix.get(pre, [])
        if not cands:
            cands = [by_hash_file[h] for h in by_hash_file if h.startswith(pre)]
        if not cands:
            verdict["索引残留（缓存已清）"] += 1
            print(f"  {pre}  → 找不到对应缓存文件 ⇒ 索引残留")
            continue
        p = cands[0]
        pages = pdf_pages(p)
        if pages == 0:
            verdict["真 0 页文件"] += 1
            print(f"  {pre}  → {p.name}  真实页数=0 ⇒ 真 0 页文件")
        elif pages > 0:
            verdict["索引写坏（文件有页却记 0）"] += 1
            print(f"  {pre}  → {p.name}  真实页数={pages} ⇒ **索引写坏**")
        else:
            verdict["读不出来"] += 1
            print(f"  {pre}  → {p.name}  页数读不出来（文件损坏？）")

    print("\n" + "=" * 78)
    print("归因汇总")
    for k, v in verdict.most_common():
        print(f"  {v:3d}  {k}")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())
