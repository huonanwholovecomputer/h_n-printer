# -*- coding: utf-8 -*-
"""APK 发布后自检（发版最后一步必跑）

出现的真实事故：清单 `app_update.json` 先上传、APK 因 scp 超时没传上去 →
线上清单指向一个 404，所有还在旧版本的用户点「检查更新」都会下载失败。
本脚本把「本地产物 ↔ 本地清单 ↔ 线上清单 ↔ 线上 APK」四者串起来比对，
任何一环不一致 / 不可下载都直接失败。

用法：python tests/verify_apk_published.py
"""
import hashlib
import io
import json
import os
import sys
import urllib.error
import urllib.request

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_DIR = os.path.join(REPO, "mobile_apps", "android_app")
DIST = os.path.join(APP_DIR, "dist")
LOCAL_MANIFEST = os.path.join(APP_DIR, "app_update.json")
MANIFEST_URL = "https://hn-space.cn/updates/app_update.json"
# app/www/updater.js 里写死的清单地址，必须与上面一致
UPDATER_JS = os.path.join(APP_DIR, "www", "updater.js")

ok = True


def check(name, cond, detail=""):
    global ok
    if not cond:
        ok = False
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


def md5_file(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch(url, timeout=60):
    req = urllib.request.Request(url, headers={"User-Agent": "HN-Publish-Check/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read()


# 1) 本地清单
local = json.load(open(LOCAL_MANIFEST, encoding="utf-8"))
print(f"本地清单: version={local['version']} versionCode={local['versionCode']}")
print(f"          url={local['url']}")

# 2) 本地产物（按新命名约定）
apk_name = f"h_n-printer_android_v{local['version']}.apk"
apk_path = os.path.join(DIST, apk_name)
check(f"本地存在产物 dist/{apk_name}", os.path.isfile(apk_path), apk_path)

local_md5 = md5_file(apk_path) if os.path.isfile(apk_path) else ""
check("本地清单 md5 == 本地 APK md5", local_md5 == local["md5"],
      f"清单 {local['md5']} vs 文件 {local_md5}")
check("清单 url 的文件名与命名约定一致（h_n-printer_android_v{版本}.apk）",
      local["url"].endswith("/" + apk_name), local["url"])

# 3) updater.js 指向的清单地址必须与脚本假设一致
if os.path.isfile(UPDATER_JS):
    js = open(UPDATER_JS, encoding="utf-8").read()
    check("www/updater.js 仍指向 BASE_URL + '/updates/app_update.json'",
          "'/updates/app_update.json'" in js)

# 4) 线上清单
try:
    st, body = fetch(MANIFEST_URL + "?t=" + str(os.getpid()))
    remote = json.loads(body.decode("utf-8"))
except Exception as e:
    check("可获取线上清单", False, repr(e))
    sys.exit(1)
check("线上清单可获取", True, f"HTTP {st}")
print(f"线上清单: version={remote.get('version')} versionCode={remote.get('versionCode')}")

for k in ("version", "versionCode", "md5", "url"):
    check(f"线上清单 {k} 与本地一致", remote.get(k) == local.get(k),
          f"线上 {remote.get(k)!r} vs 本地 {local.get(k)!r}")

# 5) 线上 APK 可下载且内容一致（这才是「清单先上线」事故的直接体检项）
try:
    st, blob = fetch(remote["url"], timeout=120)
    check("线上 APK 可下载（HTTP 200）", st == 200, f"HTTP {st}")
    check("线上 APK 大小与本地一致", len(blob) == os.path.getsize(apk_path),
          f"线上 {len(blob)} vs 本地 {os.path.getsize(apk_path)}")
    check("线上 APK md5 与清单一致", hashlib.md5(blob).hexdigest() == remote["md5"],
          hashlib.md5(blob).hexdigest())
except urllib.error.HTTPError as e:
    check("线上 APK 可下载（HTTP 200）", False,
          f"HTTP {e.code} —— 清单指向的文件不存在！旧版本用户会更新失败，"
          f"请立刻上传 {os.path.basename(remote['url'])} 或把清单 url 改回可用文件")
except Exception as e:
    check("线上 APK 可下载", False, repr(e))

print("\n" + "=" * 52)
print("发布自检:", "全部通过 ✅" if ok else "存在失败 ❌")
sys.exit(0 if ok else 1)
