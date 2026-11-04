import sys
sys.stdout.reconfigure(encoding="utf-8")
# -*- coding: utf-8 -*-
"""自证真红：逐个需求故意改坏一处 → 跑验收脚本 → 必须变红 → 改回 → 必须回绿。
只做一次性验证，跑完文件字节级还原。用法：python tests/_selfcheck_app_redgreen.py"""
import os, subprocess, sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WWW = os.path.join(ROOT, 'mobile_apps', 'android_app', 'www')

APP = 'mobile_apps/android_app/www/'
GEN = 'mobile_apps/android_app/tools/wxss2web.mjs'

CASES = [
    # (标题, 文件（仓库相对路径）, 原文, 改成, 验收脚本)
    ('①-非管理员守卫被去掉', APP + 'me.js',
     "  if (state.role !== 'admin') return false;   // 非管理员：不发请求\n".encode(),
     b"  if (false) return false;\n",
     'other/tests/verify_app_recent_orders.js'),
    ('②-离线开关又灰死', APP + 'app.js',
     b"const disabled = (d.online !== false && d.stack_can_toggle === false) || stackTogglingId === d.client_id;",
     b"const disabled = (d.online === false || d.stack_can_toggle === false) || stackTogglingId === d.client_id;",
     'other/tests/verify_app_stack_pending.js'),
    ('③-强制吸附加回来', APP + 'styles.css',
     b"  height: 100%;\n  overflow-y: auto;\n",
     b"  height: 100%;\n  overflow-y: auto;\n  scroll-snap-type: y mandatory;\n",
     'other/tests/verify_app_wheel.js'),
    ('③-拖动中也吸附', APP + 'print.js',
     b"    if (!touching && !snapping) scheduleSnap();",
     b"    if (!snapping) scheduleSnap();",
     'other/tests/verify_app_wheel.js'),
    ('④-内层 overscroll 收口被放开', APP + 'styles.css',
     b"  overscroll-behavior-y: contain;\n  overscroll-behavior-x: contain;",
     b"  overscroll-behavior-y: auto;\n  overscroll-behavior-x: auto;",
     'other/tests/verify_app_nested_scroll.js'),
    ('④-余量帧整帧丢位移', APP + 'app.js',
     b"    const room = ddy < 0 ? maxTop - el.scrollTop : el.scrollTop;",
     b"    const room = Math.abs(ddy);",
     'other/tests/verify_app_nested_scroll.js'),
    ('④-越界负余量被当余量加回去', APP + 'app.js',
     b"    ddy = ddy < 0 ? ddy + Math.max(0, room) : ddy - Math.max(0, room);",
     b"    ddy = ddy < 0 ? ddy + room : ddy - room;",
     'other/tests/verify_app_nested_scroll.js'),
    # 用户追补需求：N 的超管设置入口
    ('①-N入口非超管也渲染', APP + 'me.js',
     b"  const isSuper = !!state.isSuperAdmin;",
     b"  const isSuper = true;   // \xe6\x95\x85\xe6\x84\x8f\xef\xbc\x9a\xe9\x9d\x9e\xe8\xb6\x85\xe7\xae\xa1\xe4\xb9\x9f\xe6\xb8\xb2\xe6\x9f\x93",
     'other/tests/verify_app_recent_orders.js'),
    ('①-N的POST字段名写错', APP + 'me.js',
     b"      body: JSON.stringify({ limit: limit }),",
     b"      body: JSON.stringify({ count: limit }),",
     'other/tests/verify_app_recent_orders.js'),
    # 生成器模板同步：漏一处，任何人重跑生成器就静默回退修复
    ('③-生成器模板把强制吸附加回来', GEN,
     b"  overscroll-behavior: contain;\n  -webkit-overflow-scrolling: touch;\n",
     b"  scroll-snap-type: y mandatory;\n  overscroll-behavior: contain;\n  -webkit-overflow-scrolling: touch;\n",
     'other/tests/verify_app_wheel.js'),
    ('④-生成器模板丢掉 overscroll 收口', GEN,
     b"  overscroll-behavior-y: contain;\n  overscroll-behavior-x: contain;",
     b"  overscroll-behavior-y: auto;\n  overscroll-behavior-x: auto;",
     'other/tests/verify_app_nested_scroll.js'),
    ('①-生成器模板丢掉卡片样式', GEN,
     b".recent-order-meta {",
     b".recent-order-meta-GONE {",
     'other/tests/verify_app_recent_orders.js'),
]


def run(test):
    p = subprocess.run(['node', test], cwd=ROOT, capture_output=True)
    out = (p.stdout + p.stderr).decode('utf-8', 'replace')
    fails = [l for l in out.splitlines() if l.startswith('[FAIL]')]
    return p.returncode, fails, out


ok = True
for title, fname, old, new, test in CASES:
    path = os.path.join(ROOT, fname)
    with open(path, 'rb') as f:
        orig = f.read()
    # 各文件换行不一（styles.css / print.js 是 CRLF，其余 LF）——按文件实际换行匹配，
    # 免得判据因为 \r 命中 0 次而"静默跳过"（跳过也是 ok=False，但会掩盖真正的红）
    variant = (old, new)
    if orig.count(old) != 1 and b'\r\n' in orig:
        crlf_old = old.replace(b'\n', b'\r\n')
        if orig.count(crlf_old) == 1:
            variant = (crlf_old, new.replace(b'\n', b'\r\n'))
    old, new = variant
    hit = orig.count(old)
    if hit != 1:
        print(f'!! [{title}] 目标片段命中 {hit} 次，跳过')
        ok = False
        continue
    try:
        with open(path, 'wb') as f:
            f.write(orig.replace(old, new))
        code, fails, out = run(test)
        red_ok = code != 0 and len(fails) > 0
        print(f'[{title}] 改坏后 exit={code}，FAIL={len(fails)} 项 -> ' + ('真红 ✅' if red_ok else '没变红 ❌'))
        for l in fails[:4]:
            print('      ' + l[:120])
        if not red_ok:
            ok = False
    finally:
        with open(path, 'wb') as f:
            f.write(orig)
    code, fails, out = run(test)
    green_ok = code == 0
    print(f'[{title}] 还原后 exit={code} -> ' + ('回绿 ✅' if green_ok else '没回绿 ❌'))
    if not green_ok:
        ok = False
        print(out[-600:])

print('\n' + ('✅ 全部自证通过' if ok else '❌ 有自证失败'))
sys.exit(0 if ok else 1)
