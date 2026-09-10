# -*- coding: utf-8 -*-
"""APK 上传前校验（发版必跑）

解决两类经典翻车：
  1. 只跑 `gradlew assembleRelease` 而跳过 `cap sync` → APK 里打的是上次同步的**旧 web 资源**；
  2. 上传时忘了改 `app_update.json`（或改的是本地 dist/ 那份，而 APP 实际读服务器那份）。

做法：把 APK 内 `assets/public/` 的每个文件与 `www/` 下的源文件**逐字节比对**，
不一致就直接失败；同时打印 MD5（要填进 app_update.json）与版本号。

用法：
    python tests/verify_apk_release.py                      # 自动找 dist/ 下最新 APK
    python tests/verify_apk_release.py path/to/xxx.apk
"""
import hashlib
import io
import os
import re
import sys
import zipfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_DIR = os.path.join(REPO, "mobile_apps", "android_app")
WWW = os.path.join(APP_DIR, "www")
DIST = os.path.join(APP_DIR, "dist")
GRADLE = os.path.join(APP_DIR, "android", "app", "build.gradle")

ok_all = True


def check(name, cond, detail=""):
    global ok_all
    if not cond:
        ok_all = False
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


def find_latest_apk() -> str | None:
    if not os.path.isdir(DIST):
        return None
    apks = [os.path.join(DIST, f) for f in os.listdir(DIST) if f.lower().endswith(".apk")]
    return max(apks, key=os.path.getmtime) if apks else None


apk = sys.argv[1] if len(sys.argv) > 1 else find_latest_apk()
if not apk or not os.path.isfile(apk):
    sys.exit("找不到 APK（可显式传入路径）")

print(f"APK: {apk}")
print(f"大小: {os.path.getsize(apk):,} bytes")

md5 = hashlib.md5(open(apk, "rb").read()).hexdigest()
print(f"MD5 : {md5}")

# 版本号（以 build.gradle 为准，并校验文件名与之一致）
gradle_src = open(GRADLE, encoding="utf-8").read()
vcode = re.search(r"versionCode\s+(\d+)", gradle_src)
vname = re.search(r'versionName\s+"([^"]+)"', gradle_src)
vcode = vcode.group(1) if vcode else "?"
vname = vname.group(1) if vname else "?"
print(f"版本: versionName={vname}  versionCode={vcode}")
check("APK 文件名含当前 versionName", vname in os.path.basename(apk),
      f"{os.path.basename(apk)} vs {vname}")

# 逐字节比对 APK 内 assets/public/ 与 www/
z = zipfile.ZipFile(apk)
prefix = "assets/public/"
packed = {n[len(prefix):]: n for n in z.namelist() if n.startswith(prefix)}

www_files = []
for root, _dirs, files in os.walk(WWW):
    for f in files:
        full = os.path.join(root, f)
        www_files.append(os.path.relpath(full, WWW).replace("\\", "/"))

check("APK 内含 assets/public/ 资源", bool(packed), f"{len(packed)} 个文件")
check("www/ 下文件都已进包", set(www_files) <= set(packed),
      "缺: " + ", ".join(sorted(set(www_files) - set(packed))[:5]))

# 关键的 JS 资源逐字节比对（HTML/CSS 同样处理）
mismatch, missing = [], []
for rel in sorted(www_files):
    if rel not in packed:
        missing.append(rel)
        continue
    src = open(os.path.join(WWW, rel), "rb").read()
    if z.read(packed[rel]) != src:
        mismatch.append(rel)

check("APK 内 web 资源与 www/ 逐字节一致（cap sync 已生效）",
      not mismatch and not missing,
      f"不一致: {mismatch[:5]} 缺失: {missing[:5]}")

# 针对已知修复的显式标记（防止"资源一致但源文件本身是旧版"的误判）
if "print.js" in packed:
    js = z.read(packed["print.js"]).decode("utf-8", "replace")
    for marker, label in (
        ("function fileAlive(f)", "文件身份判定 fileAlive（审计 🔴6）"),
        ("f._uploadTimer = entry", "计时器绑定文件对象（审计 🔴6）"),
        ("function fileIndexOf(f)", "异步回调现算下标（审计 🔴6）"),
        ("function pyRound2(x)", "Python 同口径取整 pyRound2（派送费 1 分差）"),
        ("replace(/ /g, '').replace(/[、，；]/g, ',')", "页码范围与后端同口径（空格删除+严格整数）"),
    ):
        check(f"APK 内 print.js 含 {label}", marker in js)
    check("APK 内 print.js 已移除旧的索引计时器表", "_uploadTimers" not in js)
    check("APK 内 print.js 已移除旧的派送费写法",
          "baseTotal * (p.deliveryPercent / 100)" not in js)
    check("APK 内 print.js 已移除旧的空白→逗号替换",
          "replace(/[、，；\\s]/g, ',')" not in js)

print("\n" + "=" * 52)
print("APK 校验:", "全部通过 ✅" if ok_all else "存在失败 ❌")
print(f"\n要填进 app_update.json 的值：")
print(f'  "versionCode": {vcode},')
print(f'  "version": "{vname}",')
print(f'  "md5": "{md5}",')
print(f'  "url": "https://hn-space.cn/updates/h_n-printer_android_v{vname}.apk"')
sys.exit(0 if ok_all else 1)
