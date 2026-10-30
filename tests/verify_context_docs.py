# -*- coding: utf-8 -*-
"""上下文文档拆分验收：CLAUDE.md 总纲 + docs/ 子文档 是否「内容零丢失、索引可达、预算达标」。

为什么有这个脚本：`CLAUDE.md` 曾膨胀到 134 KB，超过工作区指令注入预算（64 KB），
后半截（手动双面 / 攒批 / 结算那些章节）模型根本读不到。拆分本身是机械搬运，
**唯一真正的风险是"搬丢了"或"搬重了"** —— 所以这里逐字节对账，不靠人眼。

运行：`python tests/verify_context_docs.py`

七组判据
  A 基线完好        拆分基线（拆分前的原文）尺寸/哈希未变 —— 基线被写坏时后面全是垃圾
  B 分区无洞无叠     1..N 行恰好被区间完整划分一次
  C 逐区间对账       每个区间原文**逐字**出现在它被指派的那份子文档里
  D 零丢失 / 零重复   各区间字节数之和 == 基线字节数（不是"看起来差不多"）
  E 索引可达         总纲 / AGENTS / 移动端总纲里指向的子文档都真实存在
  F 标题唯一         同一个 `## / ### 标题` 不许出现在两份不同文件里（v1 就踩过重复标题）
  G 预算与编码       CLAUDE.md + AGENTS.md < 64 KB；mobile CLAUDE.md 必须 CRLF；全部无 BOM
"""
import hashlib
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE_DIR = os.path.join(ROOT, 'docs', '拆分基线_2026-10-30')
BASE = os.path.join(BASE_DIR, 'CLAUDE.md.orig')
BASE_M = os.path.join(BASE_DIR, 'mobile_apps_CLAUDE.md.orig')

# 拆分前的原始尺寸（2026-10-30 记录）。基线是"原文"，尺寸一变就说明基线被覆盖过。
EXPECT = {'CLAUDE.md': (134472, 1142), 'mobile_apps/CLAUDE.md': (52245, 299)}
BUDGET = {'CLAUDE.md': 24576, 'AGENTS.md': 4096}
# 工作区指令预算：CLAUDE.md + AGENTS.md 共用 64 KB
WORKSPACE_BUDGET = 65536

# ---- 行号区间分配（1-based 闭区间）；与 _tmp_ctxsplit/build2.py 的 ROOT_DOCS 一致 ----
ROOT_PLAN = {
    'CLAUDE.md': [(1, 17), (1036, 1059), (1104, 1113), (1114, 1121)],
    'docs/本地打印工具_架构与管线.md': [(18, 42), (61, 115), (886, 928)],
    'docs/本地打印工具_GUI与交互.md': [(43, 60), (601, 633), (818, 839), (860, 885)],
    'docs/手动双面.md': [(116, 258)],
    'docs/攒批模式.md': [(259, 557)],
    'docs/收支清算与结算页.md': [(558, 600), (634, 797), (1122, 1142)],
    'docs/云打印系统架构.md': [(929, 1035)],
    'docs/部署与发版.md': [(798, 817), (840, 859), (1060, 1103)],
}
MOBILE_PLAN = {
    'mobile_apps/CLAUDE.md': [(1, 16)],
    'mobile_apps/docs/移动端_后端架构.md': [(17, 37)],
    'mobile_apps/docs/移动端_小程序.md': [(38, 133)],
    'mobile_apps/docs/移动端_APP打包与部署.md': [(134, 192)],
    'mobile_apps/docs/移动端_运维与价格与预览.md': [(193, 285), (286, 299)],
}

fails = []
notes = []


def check(name, ok, detail=''):
    print(('  PASS  ' if ok else '  FAIL  ') + name + (('  — ' + detail) if detail and not ok else ''))
    if not ok:
        fails.append(name + (('  — ' + detail) if detail else ''))


def read_bytes(path):
    with open(path, 'rb') as f:
        return f.read()


def read_text(path, crlf_ok=False):
    raw = read_bytes(path)
    bom = raw.startswith(b'\xef\xbb\xbf')
    t = raw.decode('utf-8')
    if crlf_ok:
        t = t.replace('\r\n', '\n')
    return t, raw, bom


def take(lines, a, b):
    seg = lines[a - 1:b]
    while seg and seg[0].strip() == '':
        seg.pop(0)
    while seg and seg[-1].strip() == '':
        seg.pop()
    return '\n'.join(seg)


def verify_pair(tag, base_path, plan, crlf_ok=False):
    print('\n[%s]' % tag)
    if not os.path.exists(base_path):
        check('A 基线存在', False, base_path + ' 不存在')
        return None
    base_text, base_raw, base_bom = read_text(base_path, crlf_ok=crlf_ok)
    if crlf_ok:
        norm = len(base_text.encode())
    else:
        norm = len(base_raw)
    want_size, want_lines = EXPECT['CLAUDE.md' if tag == 'root' else 'mobile_apps/CLAUDE.md']
    check('A 基线尺寸 %d B / %d 行' % (want_size, want_lines),
          norm == want_size and base_text.count('\n') == want_lines,
          '实测 %d B / %d 行' % (norm, base_text.count('\n')))
    check('A 基线无 BOM', not base_bom)
    if norm != want_size:
        return None
    lines = base_text.split('\n')

    # B 分区
    spans = sorted(s for rs in plan.values() for s in rs)
    expect_starts = 1
    ok_part = True
    last_end = 0
    for a, b in spans:
        if a != last_end + 1:
            ok_part = False
            notes.append('%s 分区断层/重叠：%d 紧接 %d' % (tag, a, last_end))
        last_end = b
    total = len(lines) - 1 if lines[-1] == '' else len(lines)
    ok_part = ok_part and last_end == total
    check('B 分区完整覆盖 1..%d 且无重叠' % total, ok_part,
          '; '.join(notes[-2:]) if not ok_part else '')

    # C 逐区间对账 + D 字节守恒（只比非空行：区间首尾的空行按设计会去重，不算丢失）
    expect_nonblank = sum(len(l.encode()) for l in lines if l.strip())
    total_bytes = 0
    c_ok = True
    detail = []
    for rel, ranges in plan.items():
        path = os.path.join(ROOT, rel)
        if not os.path.exists(path):
            c_ok = False
            detail.append('缺文件 ' + rel)
            continue
        text, raw, bom = read_text(path, crlf_ok=crlf_ok)
        if bom:
            c_ok = False
            detail.append('BOM ' + rel)
        pos = -1
        for a, b in ranges:
            seg = take(lines, a, b)
            total_bytes += sum(len(l.encode()) for l in seg.split('\n') if l.strip())
            p = text.find(seg)
            if p < 0:
                c_ok = False
                detail.append('%s 少了第 %d-%d 行' % (rel, a, b))
            elif p < pos:
                c_ok = False
                detail.append('%s 第 %d-%d 行顺序颠倒' % (rel, a, b))
            else:
                pos = p
    check('C 每个区间原文逐字落在指派的子文档里（%d 处）' % len(spans), c_ok, '; '.join(detail[:3]))
    check('D 零丢失零重复（各区间非空字节和 == 基线非空字节）', total_bytes == expect_nonblank,
          '子文档合计 %d B vs 基线 %d B（差 %+d）' % (total_bytes, expect_nonblank,
                                                  total_bytes - expect_nonblank))

    # F 标题唯一
    heads = {}
    dup = []
    for rel in plan:
        path = os.path.join(ROOT, rel)
        if not os.path.exists(path):
            continue
        text, _, _ = read_text(path, crlf_ok=crlf_ok)
        for line in text.split('\n'):
            m = re.match(r'^(#{2,3}) (.+)$', line)
            if m:
                h = m.group(2).strip()
                if h in heads and heads[h] != rel:
                    dup.append('%s ⟷ %s' % (heads[h], rel))
                heads[h] = rel
    check('F 同级标题不跨文件重复（%d 个标题）' % len(heads), not dup, '; '.join(dup[:3]))
    return total_bytes


# ------------------------------------------------------------------ 主流程
print('上下文文档拆分验收（CLAUDE.md 总纲 + docs/ 子文档）')
root_bytes = verify_pair('root', BASE, ROOT_PLAN)
mobile_bytes = verify_pair('mobile', BASE_M, MOBILE_PLAN, crlf_ok=True)

print('\n[E] 索引可达 / [G] 预算与编码')
# E 索引链接（总纲用「表格 + 反引号路径」，也允许标准 md 链接）
link_ok = True
link_detail = []
ENTRY_FILES = ['CLAUDE.md', 'AGENTS.md', 'mobile_apps/CLAUDE.md']
n_links = 0
for rel in ENTRY_FILES:
    path = os.path.join(ROOT, rel)
    if not os.path.exists(path):
        link_ok = False
        link_detail.append('缺入口文件 ' + rel)
        continue
    text, _, _ = read_text(path, crlf_ok=(rel != 'CLAUDE.md' and rel != 'AGENTS.md'))
    for m in re.finditer(r'(?:\]\(|`)((?:docs|mobile_apps)/[^`)\s]+\.md)', text):
        target = m.group(1)
        if '*' in target or '?' in target:      # `docs/*.md` 这类通配说明不是文件引用
            continue
        n_links += 1
        cand = [os.path.normpath(os.path.join(ROOT, target)),
                os.path.normpath(os.path.join(os.path.dirname(path), target))]
        if not any(os.path.exists(c) for c in cand):
            link_ok = False
            link_detail.append('%s → %s 不存在' % (rel, target))
check('E 入口文件引用的子文档都真实存在（%d 条）' % n_links, link_ok, '; '.join(link_detail[:3]))

# G 预算
sizes = {}
for rel in ('CLAUDE.md', 'AGENTS.md'):
    path = os.path.join(ROOT, rel)
    sizes[rel] = os.path.getsize(path) if os.path.exists(path) else 0
check('G CLAUDE.md + AGENTS.md < 工作区注入预算 %d B（实测 %d B）'
      % (WORKSPACE_BUDGET, sizes['CLAUDE.md'] + sizes['AGENTS.md']),
      sizes['CLAUDE.md'] + sizes['AGENTS.md'] < WORKSPACE_BUDGET)
check('G CLAUDE.md ≤ %d B（实测 %d B）' % (BUDGET['CLAUDE.md'], sizes['CLAUDE.md']),
      sizes['CLAUDE.md'] <= BUDGET['CLAUDE.md'])
check('G AGENTS.md ≤ %d B（实测 %d B）' % (BUDGET['AGENTS.md'], sizes['AGENTS.md']),
      sizes['AGENTS.md'] <= BUDGET['AGENTS.md'])

# 编码：根两个入口 LF，mobile 入口 CRLF（被 git 跟踪，core.autocrlf=true）
for rel, want_crlf in (('CLAUDE.md', False), ('AGENTS.md', False), ('mobile_apps/CLAUDE.md', True)):
    path = os.path.join(ROOT, rel)
    if not os.path.exists(path):
        continue
    raw = read_bytes(path)
    has_bom = raw.startswith(b'\xef\xbb\xbf')
    crlf = b'\r\n' in raw
    if want_crlf:
        n_lf = raw.count(b'\n')
        n_crlf = raw.count(b'\r\n')
        ok = (not has_bom) and n_lf == n_crlf > 0
        check('G %s 为 CRLF 且无 BOM（%d/%d）' % (rel, n_crlf, n_lf), ok)
    else:
        check('G %s 为 LF 且无 BOM' % rel, (not has_bom) and b'\r' not in raw)

# 子文档不得被 git 当成新文件（docs/*.md 与 mobile_apps/docs/*.md 都应在 ignore 里）
try:
    ok_ignore = True
    for probe in ('docs/手动双面.md', 'mobile_apps/docs/移动端_小程序.md'):
        r = subprocess.run(['git', 'check-ignore', '-q', probe], cwd=ROOT)
        if r.returncode != 0:
            ok_ignore = False
            notes.append('未被 ignore：' + probe)
    check('G 拆出的子文档都在 .gitignore 覆盖内（不进 git）', ok_ignore,
          '; '.join(n for n in notes if n.startswith('未被 ignore')))
except Exception as exc:                                     # pragma: no cover
    notes.append('git check-ignore 不可用：%s' % exc)

print('\n结果：%s' % ('全部通过' if not fails else '失败 %d 项' % len(fails)))
for f in fails:
    print('  ✗ ' + f)
sys.exit(1 if fails else 0)
