# -*- coding: utf-8 -*-
"""APK 打包脚本的静态 + 行为守卫（2026-11-17 审计 Y5）。

原来 cap CLI 缺失时只 Write-Warning 然后继续 gradlew ⇒ 打出的是上一次 sync 的旧 web 资源，
而且脚本仍以成功退出（fail-open）。这里同时钉住：① 两个脚本都保留 cap sync ② 缺失时
必须 fail-closed（Write-Error + exit 1 + 显式 -AllowStaleAssets 豁免）③ 真跑一份拷贝验证
缺 cap 时确实非 0 退出且报「cap CLI 缺失」。
"""
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
APP = os.path.join(ROOT, 'mobile_apps', 'android_app')
SCRIPTS = ['build-release-apk.ps1', 'build-apk.ps1']

failures = []


def chk(name, ok, detail=''):
    print(('[PASS] ' if ok else '[FAIL] ') + name + (('  ' + detail) if (detail and not ok) else ''))
    if not ok:
        failures.append(name)


for script in SCRIPTS:
    path = os.path.join(APP, script)
    src = open(path, encoding='utf-8-sig').read()
    chk('%s 仍包含 cap sync android' % script, 'sync android' in src)
    chk('%s 缺 cap CLI 时 fail-closed（Write-Error + exit 1）' % script,
        'Write-Error "cap CLI 缺失' in src and '  exit 1' in src)
    chk('%s 提供显式豁免 -AllowStaleAssets（而不是静默跳过）' % script,
        'param([switch]$AllowStaleAssets)' in src and 'elseif ($AllowStaleAssets)' in src)
    chk('%s 不再有 SKIPPING sync 的 fail-open 分支' % script, 'SKIPPING sync!' not in src)
    # PS 5.1 读无 BOM 的 UTF-8 会按 ANSI 解析：中文注释会变成乱码并让解析器报
    # "Missing type name after '['"（2026-11-17 实测：编辑工具改完这两个脚本后 BOM 被丢掉，直接跑不动）
    chk('%s 保留 UTF-8 BOM（被编辑工具剥掉就会解析失败）' % script,
        open(path, 'rb').read(3) == b'\xef\xbb\xbf')

# 行为：把 release 脚本拷到没有 node_modules 的临时目录真跑一次，应当在 gradle 之前就非 0 退出
pwsh = shutil.which('powershell') or shutil.which('pwsh')
if not pwsh:
    print('[HN-SKIP] 本机没有 powershell：打包脚本的行为段没跑，静态段通过不等于 fail-closed 生效')
else:
    tmp = tempfile.mkdtemp(prefix='apk_guard_')
    try:
        copy = os.path.join(tmp, 'build-apk.ps1')
        shutil.copyfile(os.path.join(APP, 'build-apk.ps1'), copy)
        r = subprocess.run([pwsh, '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                            '-File', copy], capture_output=True, text=True, encoding='utf-8',
                           errors='replace', timeout=120, cwd=tmp)
        out = (r.stdout or '') + (r.stderr or '')
        chk('行为：缺 cap CLI 时脚本非 0 退出（rc=%s）' % r.returncode, r.returncode != 0)
        chk('行为：报出「cap CLI 缺失」并指向 npm install',
            'cap CLI 缺失' in out and 'npm install' in out)
        chk('行为：没有走到 gradlew（fail-closed 在 sync 之前）', 'gradlew' not in out.lower())
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

if failures:
    print('\n%d 条失败' % len(failures))
    sys.exit(1)
print('\nAPK 打包脚本守卫：全部通过')