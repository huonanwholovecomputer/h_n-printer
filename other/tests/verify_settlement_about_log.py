# -*- coding: utf-8 -*-
"""验收 · 收支清算「关于」页的版本说明是否跟得上（2026-11-16 现场）

被验对象：`local_print_tool/finance/settlement.html` 的 `renderAbout()`。

现场问题（m00341）：v7.11–v7.17、v7.19、v7.20、v7.23、v7.24 这些改动**根本没写进关于页的版本说明**
—— 日志停在 v7.10 就直接跳到 v7.18，用户翻关于页看不到中间那些改动。

判据：
  A. 结构 —— 条目从 renderAbout 里取（`>vX.Y：`），版本号严格递增、无重复，
     每条都有 <b> 标题与「——」分隔，最后一条必须等于 `var APP_VER`。
  B. 覆盖 —— `docs/收支清算与结算页.md` 里每个 `### v7.N` 小节（= 真的做过并写了文档的改动）
     在关于页都必须有同号条目。这是本次漏条的判据：文档有、关于页没有 ⇒ 红。
  C. 反向自证 —— 内存里删掉 v7.15 条目、或把它的版本号改坏，B 段判据必须当场转红
     （证明断言不是空转的常量）。

用法：python other/tests/verify_settlement_about_log.py
exit 0 = 全部通过。
"""
import io
import os
import re
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
HTML = os.path.join(REPO, "local_print_tool", "finance", "settlement.html")
DOC = os.path.join(REPO, "docs", "收支清算与结算页.md")
ok_all = True


def check(name, cond, detail=""):
    global ok_all
    if cond:
        print(f"  [PASS] {name}")
    else:
        ok_all = False
        print(f"  [FAIL] {name}" + (f"  -- {detail}" if detail else ""))


def _entry_versions(html: str):
    """renderAbout 里所有条目的 (版本号字符串, 该条目原文) —— 保持出现顺序。"""
    m = re.search(r"function renderAbout\(\) \{([\s\S]*?)\n\}", html)
    if not m:
        return None
    body = m.group(1)
    out = []
    # 2026-11-16：从 v7.26.0 起版本号是三段制（X.Y.Z），历史两段制仍要认 —— 统一用 (?:.d+)+
    for em in re.finditer(r">(v\d+(?:\.\d+)+)：", body):
        start = em.start()
        end = body.find("</p>", start)
        # 一个条目在源码里是若干段 '...' + '...' 拼起来的：取到它的 </p> 为止当整条原文
        head = body[start:(end + 4) if end >= 0 else start + 400]
        out.append((em.group(1), head))
    return out


def _entry_numbers(entries):
    return [tuple(int(x) for x in v.lstrip("v").split(".")) for v, _ in entries]


def _doc_versions(doc_text: str):
    """文档里 `### v7.N …` 小节标题 → 版本号集合（只取 v7 系，v5/v6 是重构前的历史）。"""
    return sorted({f"v{x}" for x in re.findall(r"^### v(7(?:\.\d+)+)", doc_text, re.M)},
                  key=lambda s: tuple(int(n) for n in s.lstrip("v").split(".")))


def audit(html: str, doc_text: str):
    """跑 A/B 两段判据，返回失败项列表（空 = 通过）。供反向自证复用。"""
    fails = []
    entries = _entry_versions(html)
    if not entries:
        return ["在 settlement.html 里找不到 renderAbout() 的版本条目"]
    nums = _entry_numbers(entries)
    if any(b <= a for a, b in zip(nums, nums[1:])):
        fails.append(f"版本号未严格递增：{[v for v, _ in entries]}")
    if len(set(nums)) != len(nums):
        fails.append(f"版本号有重复：{[v for v, _ in entries]}")
    for ver, raw in entries:
        # v5 / v6 是一行式老条目（没有 <b> 标题），v7 起统一「vX.Y：<b>标题</b> —— 正文」
        if not ver.startswith("v7."):
            continue
        if "<b>" not in raw:
            fails.append(f"{ver} 条目缺少 <b> 标题")
        if "——" not in raw:
            fails.append(f"{ver} 条目缺少「——」分隔")
    m_ver = re.search(r"var APP_VER = '(v\d+(?:\.\d+)*)';", html)
    if not m_ver:
        fails.append("找不到 var APP_VER")
    elif entries[-1][0] != m_ver.group(1):
        fails.append(f"最后一条是 {entries[-1][0]}，但 APP_VER 是 {m_ver.group(1)}")
    have = {v for v, _ in entries}
    missing = [v for v in _doc_versions(doc_text) if v not in have]
    if missing:
        fails.append(f"文档有、关于页没有：{missing}")
    return fails


def main():
    with open(HTML, encoding="utf-8") as f:
        html = f.read()
    with open(DOC, encoding="utf-8") as f:
        doc = f.read()

    print("A/B 结构与覆盖")
    fails = audit(html, doc)
    for msg in fails:
        check(msg, False)
    if not fails:
        entries = _entry_versions(html)
        check(f"关于页版本条目 {len(entries)} 条，版本号严格递增、末条等于 APP_VER",
              True)
        check(f"文档里的 v7 小节（{len(_doc_versions(doc))} 个）在关于页全部有同号条目", True)
        check("每条都有 <b> 标题与「——」分隔（文案口径统一）", True)

    print("C 反向自证（判据必须能红）")
    doc_vs = _doc_versions(doc)
    # 变异 1：删掉 v7.15 那条
    mut1 = re.sub(r"\s*'<p style=\"margin-top:10px\">v7\.15：[^']*' \+", "", html, count=1)
    check("变异 1：删掉 v7.15 条目后，覆盖判据当场转红（不是空转）",
          mut1 != html and any("v7.15" in x for x in audit(mut1, doc)), "变异未生效")
    # 变异 2：把最后一条的版本号改成一个更小的号 → 递增/末条断言必须红
    last_ver = _entry_versions(html)[-1][0]
    mut2 = html.replace(f">{last_ver}：", ">v7.0：", 1)
    check("变异 2：把末条版本号改成 v7.0 后，序/末条判据当场转红",
          mut2 != html and bool(audit(mut2, doc)), "变异未生效")
    # 变异 3：文档里新增一个关于页没有的小节 → 覆盖判据必须红
    mut3_doc = doc + "\n### v7.99 只有文档没有关于页\n"
    check("变异 3：文档单方面新增 v7.99 小节后，覆盖判据当场转红",
          any("v7.99" in x for x in audit(html, mut3_doc)), "变异未生效")

    print()
    if ok_all:
        print("收支清算关于页版本说明验收：全部通过 ✅")
        print(f"  关于页条目 {len(_entry_versions(html))} 条；文档 v7 小节 {len(doc_vs)} 个全覆盖")
        return 0
    print("收支清算关于页版本说明验收：有失败项 ❌")
    return 1


if __name__ == "__main__":
    sys.exit(main())
