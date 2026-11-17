# -*- coding: utf-8 -*-
"""验收 · APK 版本清单自动回写（2026-11-17 审计 Y6）。

缺陷：`app_update.json` 的 version / versionCode / url / md5 是**手填**。漏改 versionCode 时
APP 端 `remote <= current` 判定为已最新 ⇒ 老用户永远收不到更新，而线上清单看着一切正常。

本脚本直接**从 build-release-apk.ps1 里抽出那段回写代码**（避免复制一份来测、测完就漂），
喂给它一个假 APK + 一份带 notes/force 的旧清单，真跑 PowerShell，然后逐字段核对三份清单：
  version / versionCode / url 由版本号推出，md5 = APK 的 MD5，notes / force 保留人工值。

本机没有 powershell ⇒ [HN-SKIP]（静态断言仍会跑）。
"""
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace',
                              line_buffering=True)

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPT = os.path.join(REPO, 'mobile_apps', 'android_app', 'build-release-apk.ps1')

failures = []


def chk(name, ok, detail=''):
    print(('[PASS] ' if ok else '[FAIL] ') + name + (('  -- ' + detail) if (detail and not ok) else ''))
    if not ok:
        failures.append(name)


src = open(SCRIPT, encoding='utf-8-sig').read()
chk('build-release-apk.ps1 会回写 app_update.json', 'app_update.json' in src)
chk('回写用 APK 实际 MD5（不是手填）', 'Get-FileHash -Algorithm MD5' in src)
chk('保留 notes / force（只回写版本三要素 + md5）',
    '[string]$seed.notes' in src and '[bool]$seed.force' in src)

pwsh = shutil.which('powershell') or shutil.which('pwsh')
if not pwsh:
    print('[HN-SKIP] 本机没有 powershell：回写代码的行为段没跑，静态断言通过不等于清单会更新')
else:
    try:
        start = src.index('# 自动回写三份 app_update.json')
        warn = src.index('Write-Warning "跳过 app_update.json 回写', start)
        end = src.index('\n  }\n', warn) + len('  }\n')
        block = src[start:end]
    except ValueError:
        chk('能从脚本里抽出回写代码块', False, '标记行找不到（脚本被改写？）')
        block = None
    if block:
        tmpdir = tempfile.mkdtemp(prefix='mf_')
        try:
            app = os.path.join(tmpdir, 'android_app')
            os.makedirs(os.path.join(app, 'updates'))
            os.makedirs(os.path.join(app, 'dist'))
            seed = {'version': '0.0.1', 'versionCode': 1, 'url': 'https://x/h_n-printer_android_v0.0.1.apk',
                    'md5': '0' * 32, 'notes': '保留我', 'force': False}
            open(os.path.join(app, 'app_update.json'), 'w', encoding='utf-8').write(
                json.dumps(seed, ensure_ascii=False))
            harness = os.path.join(tmpdir, 'h.ps1')
            head = ('$PSScriptRoot = \'%s\'\n'
                    "$distDir = Join-Path $PSScriptRoot 'dist'\n"
                    "$version = '1.1.70'; $vc = 70\n"
                    '$friendly = "h_n-printer_android_v${version}.apk"\n'
                    '$apk = Join-Path $distDir $friendly\n'
                    'New-Item -ItemType Directory -Force -Path $distDir | Out-Null\n'
                    "[IO.File]::WriteAllText($apk, 'fake apk')\n") % app.replace('\\', '\\\\')
            open(harness, 'w', encoding='utf-8-sig').write(head + block)
            r = subprocess.run([pwsh, '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                                '-File', harness], capture_output=True, text=True,
                               encoding='utf-8', errors='replace', timeout=120)
            chk('回写代码块能跑通（rc=0）', r.returncode == 0, (r.stdout + r.stderr)[-400:])
            for rel in ('app_update.json', 'updates/app_update.json', 'dist/app_update.json'):
                p = os.path.join(app, rel)
                if not os.path.exists(p):
                    chk('%s 被回写' % rel, False, '文件不存在')
                    continue
                m = json.load(open(p, encoding='utf-8'))
                good = (m.get('version') == '1.1.70' and m.get('versionCode') == 70
                        and (m.get('url') or '').endswith('h_n-printer_android_v1.1.70.apk')
                        and re.fullmatch(r'[0-9a-f]{32}', m.get('md5') or '')
                        and m.get('notes') == '保留我' and m.get('force') is False)
                chk('%s 版本/url/md5 由产物推出且保留 notes/force' % rel, good,
                    json.dumps(m, ensure_ascii=False))
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

print()
if failures:
    print('%d 条失败' % len(failures))
    sys.exit(1)
print('APK 清单自动回写：全部通过')