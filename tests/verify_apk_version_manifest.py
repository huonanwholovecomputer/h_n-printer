# -*- coding: utf-8 -*-
"""验收 · 🟡-D5 第 13 条：APK 版本号必须三处一致（gradle / 清单 / 产物名）

缺陷（审查报告 D5）：`android/app/build.gradle` 的 `versionCode`/`versionName` 与
`app_update.json` 的 `version`/`versionCode` 是**两处手填**、没有任何脚本比对 —— "同错即通过"。
真实后果：只改 `versionName` 忘了改 `versionCode` 时，APP 端 `remote <= current` 判定为"已是最新"，
老用户**永远收不到更新**（而且线上清单看着一切正常，没有任何报错）。

本脚本把四处串起来：
  ① `build.gradle` 的 versionCode / versionName；
  ② 仓库里两份 `app_update.json`（`android_app/app_update.json` 与 `android_app/updates/app_update.json`）
     必须**逐字段一致**（历史上它们被分别更新过）；
  ③ 清单的 `version` == gradle 的 `versionName`；清单的 `versionCode` == gradle 的 `versionCode`
     （且是**整数**，不是字符串 —— 否则 APP 端 parseInt 依赖宽松解析）；
  ④ 清单 `url` 的文件名必须是命名约定 `h_n-printer_android_v{version}.apk`；
     `dist/` 下若已有该版本的产物，其文件名也必须对得上。

外加**判据自证**（变异对拍）：把 versionCode / versionName / url 分别改坏，判定函数必须报错 ——
否则这类"值对值"的脚本很容易写成永远为绿。

用法: python tests/verify_apk_version_manifest.py     （exit 0 = 全绿）
"""
import io
import json
import os
import re
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_DIR = os.path.join(REPO, "mobile_apps", "android_app")
GRADLE = os.path.join(APP_DIR, "android", "app", "build.gradle")
MANIFESTS = [
    os.path.join(APP_DIR, "app_update.json"),
    os.path.join(APP_DIR, "updates", "app_update.json"),
]

ok_all = True


def check(name, cond, detail=""):
    global ok_all
    if not cond:
        ok_all = False
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


def read_gradle_version(path):
    """从 build.gradle 取 (versionCode:int, versionName:str)；取不到返回 (None, None)。"""
    src = open(path, encoding="utf-8").read()
    mc = re.search(r"versionCode\s+(\d+)", src)
    mn = re.search(r'versionName\s+"([^"]+)"', src)
    return (int(mc.group(1)) if mc else None), (mn.group(1) if mn else None)


def judge(gradle_code, gradle_name, manifests, dist_names=()):
    """返回问题清单（空列表 = 一致）。抽成函数是为了做变异自证。"""
    problems = []
    if gradle_code is None or not gradle_name:
        problems.append("build.gradle 里读不到 versionCode / versionName")
        return problems
    for label, m in manifests:
        if m is None:
            problems.append(f"{label} 读取失败")
            continue
        if m.get("version") != gradle_name:
            problems.append(f"{label}.version={m.get('version')!r} != versionName={gradle_name!r}")
        if not isinstance(m.get("versionCode"), int):
            problems.append(f"{label}.versionCode 不是整数：{m.get('versionCode')!r}")
        elif m["versionCode"] != gradle_code:
            problems.append(f"{label}.versionCode={m['versionCode']} != versionCode={gradle_code}")
        url = m.get("url") or ""
        want = f"h_n-printer_android_v{gradle_name}.apk"
        if not url.endswith("/" + want):
            problems.append(f"{label}.url 文件名不符约定（应指向 {want}）：{url}")
        md5 = (m.get("md5") or "").lower()
        if not re.fullmatch(r"[0-9a-f]{32}", md5):
            problems.append(f"{label}.md5 不是 32 位十六进制：{m.get('md5')!r}")
    for name in dist_names:
        if not name.endswith(f"_v{gradle_name}.apk"):
            problems.append(f"dist 产物名与 versionName 不符：{name}")
    return problems


print("=== A. 现行值是否三处一致 ===")
_gcode, _gname = read_gradle_version(GRADLE)
print(f"build.gradle: versionCode={_gcode} versionName={_gname}")
check("build.gradle 能读到 versionCode / versionName", _gcode is not None and bool(_gname))

_loaded = []
for p in MANIFESTS:
    rel = os.path.relpath(p, APP_DIR)
    try:
        with open(p, encoding="utf-8") as f:
            _loaded.append((rel, json.load(f)))
        print(f"{rel}: version={_loaded[-1][1].get('version')} "
              f"versionCode={_loaded[-1][1].get('versionCode')}")
    except Exception as e:  # noqa: BLE001
        _loaded.append((rel, None))
        print(f"{rel}: 读取失败 {e!r}")

_dist = os.path.join(APP_DIR, "dist")
_dist_names = sorted(n for n in os.listdir(_dist)) if os.path.isdir(_dist) else []
_dist_apks = [n for n in _dist_names if n.endswith(".apk") and n.startswith("h_n-printer_android_v")]
_target_apks = [n for n in _dist_apks if n.endswith(f"_v{_gname}.apk")]

_problems = judge(_gcode, _gname, _loaded, dist_names=_target_apks)
check("★ 四处（gradle 版本 / 两份清单 / 产物名）一致", not _problems, "；".join(_problems))

check("两份 app_update.json 逐字段一致（历史上曾被分别更新）",
      len(_loaded) == 2 and all(m for _l, m in _loaded)
      and _loaded[0][1] == _loaded[1][1],
      "字段差异：" + str({k: (_loaded[0][1].get(k), _loaded[1][1].get(k))
                          for k in set(_loaded[0][1]) | set(_loaded[1][1])
                          if _loaded[0][1].get(k) != _loaded[1][1].get(k)}) if all(m for _l, m in _loaded) else "有清单读取失败")
check(f"dist/ 里存在该版本的产物（{_gname}）", bool(_target_apks),
      f"现有：{[n for n in _dist_apks][-3:]}")
check("清单 versionCode 是整数类型（APP 端 parseInt 不依赖宽松解析）",
      all(isinstance(m.get("versionCode"), int) for _l, m in _loaded if m))

print("\n=== B. 判据自证（变异必须报错，否则是永远为绿的假守卫）===")
_base = [{k: (dict(v) if isinstance(v, dict) else v) for k, v in m.items()} for _l, m in _loaded]
_good = judge(_gcode, _gname, [(MANIFESTS[0], _base[0])], dist_names=[])
check("B0 未变异时应无问题", not _good, "；".join(_good))

_mut_code = [{k: v for k, v in _base[0].items()}, {**(_base[0]), "versionCode": _gcode + 1}]
check("B1 清单 versionCode 加 1（忘了同步 versionCode 的典型错法）→ 必报错",
      bool(judge(_gcode, _gname, [("mut", _mut_code[1])], dist_names=[])))

_mut_name = {**(_base[0]), "version": "9.9.9"}
check("B2 清单 version 与 versionName 不一致 → 必报错",
      bool(judge(_gcode, _gname, [("mut", _mut_name)], dist_names=[])))

_mut_url = {**(_base[0]), "url": (_base[0].get("url") or "").replace(f"_v{_gname}.apk", "_v0.0.0.apk")}
check("B3 url 指向别的版本文件名 → 必报错",
      bool(judge(_gcode, _gname, [("mut", _mut_url)], dist_names=[])))

_mut_type = {**(_base[0]), "versionCode": str(_gcode)}
check("B4 versionCode 被写成字符串 → 必报错",
      bool(judge(_gcode, _gname, [("mut", _mut_type)], dist_names=[])))

_mut_md5 = {**(_base[0]), "md5": "not-a-md5"}
check("B5 md5 不是 32 位十六进制 → 必报错",
      bool(judge(_gcode, _gname, [("mut", _mut_md5)], dist_names=[])))

check("B6 dist 产物名与 versionName 不符 → 必报错",
      bool(judge(_gcode, _gname, [(MANIFESTS[0], _base[0])], dist_names=["h_n-printer_android_v0.0.0.apk"])))

print("\n" + "=" * 60)
print("APK 版本清单一致性:", "全部通过 ✅" if ok_all else "存在失败 ❌")
sys.exit(0 if ok_all else 1)
