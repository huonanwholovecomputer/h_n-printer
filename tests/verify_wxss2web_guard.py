# -*- coding: utf-8 -*-
"""wxss2web 生成器防误跑闸门验收（2026-11-02 事故后加）

背景：`mobile_apps/android_app/tools/wxss2web.mjs` 把小程序 WXSS 转成 App 的
`www/styles.css`，但它 **忽略输出路径参数、永远写回 www/styles.css**（旧 README 写的
`[输出路径]` 是错的）。而 `www/styles.css` 早已被手工大改约 2000 行（几何口径、
玻璃拟态、APP 专有卡片「近 N 条订单」…）—— 2026-11-02 有人想做"只读比对"跑了一次，
当场把 188 行手工样式抹掉，靠 `git checkout` 才救回来。

本脚本钉三件事：
  ① 生成器**没有 --force 就必须拒绝执行**（非零退出 + 不写文件）；
  ② README 不再教人"直接跑一下就行"，而是写明它是破坏性脚本；
  ③ 生成器**模板**里已经包含那些手工样式（overscroll 接力、滚轮去 snap、近 N 条卡片），
     否则哪天真要重新生成，照样会把这些改动一起抹掉。

判据要能真红：把生成器里的 --force 闸门删掉、或把 README 的警告改回旧文案，
对应的 check 立刻转 FAIL。
"""

import hashlib
import os
import re
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 中文 Windows 的控制台默认是 GBK，直接 print 打勾/叉会抛 UnicodeEncodeError
# （等于脚本在"全部通过"那一刻自己崩掉）。这里统一按 UTF-8 + replace 输出。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

GEN = os.path.join(REPO, 'mobile_apps', 'android_app', 'tools', 'wxss2web.mjs')
OUT_CSS = os.path.join(REPO, 'mobile_apps', 'android_app', 'www', 'styles.css')
README = os.path.join(REPO, 'mobile_apps', 'android_app', 'README.md')

_fails = []


def check(name, cond, detail=''):
    if cond:
        print(f'[PASS] {name}')
    else:
        print(f'[FAIL] {name}' + (f'  -- {detail}' if detail else ''))
        _fails.append(name)


def read(path):
    with open(path, encoding='utf-8') as f:
        return f.read()


def sha(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(65536), b''):
            h.update(chunk)
    return h.hexdigest()


def strip_comments(src):
    """剥掉 /* */ 与 // 注释，避免"注释里写了关键字"骗过断言。"""
    src = re.sub(r'/\*.*?\*/', '', src, flags=re.S)
    src = re.sub(r'(?m)^\s*//.*$', '', src)
    return src


gen_src = read(GEN)
gen_code = strip_comments(gen_src)
readme = read(README)

print('=== ① 生成器默认拒绝执行（--force 闸门）===')
check('生成器识别 --force 开关', '--force' in gen_code, '没有 --force 判定')
check('没有 --force 时以非零码退出',
      re.search(r"if\s*\(\s*!\s*process\.argv\.includes\(\s*['\"]--force['\"]\s*\)\s*\)", gen_code) is not None
      and 'process.exit(1)' in gen_code,
      '找不到 "if (!process.argv.includes(--force)) { ... process.exit(1) }"')
check('闸门在写文件之前（exit 调用早于 writeFileSync）',
      gen_code.find('process.exit(1)') != -1
      and gen_code.find('process.exit(1)') < gen_code.find('writeFileSync('),
      'process.exit(1) 出现在 writeFileSync 之后 —— 闸门拦不住覆盖')
check('文件头写明它永远写回 www/styles.css、不接受输出路径',
      '永远写回' in gen_src and '不接受输出路径' in gen_src,
      '文件头缺少破坏性说明')
check('用法行已改成 node tools/wxss2web.mjs --force',
      'node tools/wxss2web.mjs --force' in gen_src and '[输出路径]' not in gen_src,
      '用法行仍写 [输出路径]（会让人以为可以只读试跑）')

print('\n=== ② README 不再教人直接跑 ===')
check('README 不再出现「重新运行 node tools/wxss2web.mjs 即可再生成」这种误导',
      '重新运行 `node tools/wxss2web.mjs` 即可再生成' not in readme)
check('README 明确写了会覆盖 / 必须 --force',
      '会直接覆盖' in readme and '--force' in readme,
      'README 没写清破坏性与 --force')
check('README 说明 www/styles.css 不是纯生成物（手工改过）',
      '不是纯生成物' in readme or '手工大改' in readme)

print('\n=== ③ 模板已包含手工样式（重跑生成器不会丢）===')
tpl_code = strip_comments(gen_src)
check('模板 html/body 仍保留 overscroll-behavior: none',
      re.search(r'html,\s*body\s*\{[^}]*overscroll-behavior:\s*none', tpl_code) is not None)
check('模板 .file-list-scroll 有 overscroll-behavior-y: contain（嵌套滚动接力）',
      'overscroll-behavior-y: contain' in tpl_code,
      '重跑生成器会把文件列表的接力修复抹掉')
wheel_block = re.search(r'\.wheel-viewport\s*\{[^}]*\}', tpl_code)
check('模板 .wheel-viewport 存在且不含 scroll-snap-type（滚轮阻力修复）',
      wheel_block is not None and 'scroll-snap-type' not in wheel_block.group(0),
      '模板滚轮段还有 scroll-snap-type，重跑会把阻力问题带回来')
check('模板 .wheel-item 不含 scroll-snap-align',
      '.wheel-item {' in tpl_code
      and 'scroll-snap-align' not in tpl_code.split('.wheel-item {', 1)[1].split('}', 1)[0])
check('模板含「近 N 条订单」卡片样式（.recent-orders-card / .recent-order-limit-entry）',
      '.recent-orders-card' in tpl_code and '.recent-order-limit-entry' in tpl_code,
      '重跑生成器会把管理员卡片样式整段抹掉')
check('模板含 .device-state-stack-pending（离线攒批「待应用」角标）',
      '.device-state-stack-pending' in tpl_code)

print('\n=== ④ 动态验证：不加 --force 跑一次，必须拒绝且不动 styles.css ===')
if not os.path.exists(OUT_CSS):
    check('www/styles.css 存在（动态验证前提）', False)
else:
    # ⚠ 这条动态判据本身就会执行那个脚本。若闸门被人删掉，脚本就会真的覆盖
    # www/styles.css —— 所以这里先把整份文件读进内存，跑完无论对错都按字节还原，
    # 绝不让"验收脚本自己造成事故"（2026-11-02 就发生过一次：变异自证跑完没还原）。
    before_bytes = open(OUT_CSS, 'rb').read()
    before = sha(OUT_CSS)
    before_size = len(before_bytes)
    try:
        try:
            proc = subprocess.run(
                ['node', 'tools/wxss2web.mjs'],
                cwd=os.path.join(REPO, 'mobile_apps', 'android_app'),
                capture_output=True, text=True, timeout=120,
                # node 往 stderr 写的是 UTF-8 中文；中文 Windows 上 subprocess 默认按
                # cp936/GBK 解码会抛 UnicodeDecodeError（reader 线程死掉 → out 变空串
                # → 下面两条判据假红）。显式钉 UTF-8 + replace 兜底。
                encoding='utf-8', errors='replace',
            )
            out = (proc.stdout or '') + (proc.stderr or '')
            check('无参数运行返回非零退出码', proc.returncode != 0, f'returncode={proc.returncode}')
            check('拒绝时提示里带 --force', '--force' in out, out.strip()[:200])
            check('拒绝时明确说没写文件', '未写入' in out or '没有写入' in out, out.strip()[:200])
        except FileNotFoundError:
            check('本机有 node 可执行（动态验证前提）', False, 'node 不在 PATH')
        except subprocess.TimeoutExpired:
            check('生成器没有卡住', False, 'timeout 120s')
        changed = sha(OUT_CSS) != before
        check('www/styles.css 内容未被改动（字节级）', not changed,
              f'{before_size} → {os.path.getsize(OUT_CSS)}')
    finally:
        # 无条件还原：闸门失效时这里把文件写回去，保证验收脚本永远不留副作用
        if open(OUT_CSS, 'rb').read() != before_bytes:
            with open(OUT_CSS, 'wb') as f:
                f.write(before_bytes)
            print('  [还原] www/styles.css 被脚本改动过，已按运行前字节还原')
    # 用「只有手工维护版才有」的取值做标记：HEAD 的 www/styles.css 把滚轮选中项字号
    # 手工调成 4.32cqw，而生成器模板写的是 4.8cqw（两边各自保留自己的口径）。一旦有人
    # 真跑了生成器，www/styles.css 会变成 4.8cqw，这条立刻红 —— 比"文件行数"稳。
    # 注意：若将来两边口径被统一，请换一个新的手工标记（别直接删掉这条判据）。
    css_now = read(OUT_CSS)
    check('www/styles.css 仍是手工维护版（滚轮选中项 4.32cqw 标记还在）',
          '4.32cqw' in css_now,
          '文件像是被生成器覆盖过（模板口径是 4.8cqw）')

print()
if _fails:
    print(f'wxss2web 防误跑闸门验收: 存在失败 ❌  ({len(_fails)} 项)')
    for n in _fails:
        print('  - ' + n)
    sys.exit(1)
print('wxss2web 防误跑闸门验收: 全部通过 ✅')
