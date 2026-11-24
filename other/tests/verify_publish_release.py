# -*- coding: utf-8 -*-
"""验收 · GitHub Release 发布脚本（2026-11-24 事故两连）。

事故一：4.7.4 的 Release 说明是脚本默认那一行「Windows 安装包 h_n-printer_setup_v4.7.4.exe
（含 Android APK）」—— 公开用户只认 Release 这一页，等于什么都没说。
事故二：APK 资产名是 gradle 原始产物 `app-release.apk`，不再是约定名
`h_n-printer_android_v{versionName}.apk`（4.7.1 及以前一直是这样）。

本脚本直接 import 发布脚本本身（不复制一份逻辑来测），钉住：
  A. 没有 --notes / --notes-file ⇒ 退出码 2，且提示去扫上一个 Release 的改动；
  B. 旧的自动一句话说明 ⇒ 也拒发（只有文件名 = 没写）；
  C. APK 资产名一律按 build.gradle 的 versionName 起名；名字合不了约定且读不到
     versionName 时返回空 ⇒ 调用方拒发，绝不把 app-release.apk 原样传上去；
  D. stage_apk 真的复制出约定名副本（幂等：同 md5 不重复复制）。
"""
import contextlib
import importlib.util
import io
import os
import re
import shutil
import sys
import tempfile
import types

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TOOL = os.path.join(REPO, "local_print_tool", "tools", "publish_release.py")
GRADLE = os.path.join(REPO, "mobile_apps", "android_app", "android", "app", "build.gradle")

failures = []
total = 0


def chk(name, ok, detail=""):
    global total
    total += 1
    print(("[PASS] " if ok else "[FAIL] ") + name + (("  -- " + detail) if (detail and not ok) else ""))
    if not ok:
        failures.append(name)


def load_module():
    spec = importlib.util.spec_from_file_location("publish_release_under_test", TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def call_capture(fn, *a, **kw):
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            fn(*a, **kw)
    except SystemExit as e:
        return getattr(e, "code", None), buf.getvalue()
    return None, buf.getvalue()


src = open(TOOL, encoding="utf-8").read()
chk("发布脚本不再自带「只有文件名」的默认说明",
    "（含 Android APK）" not in src and "notes = args.notes.strip() or" not in src)
chk("说明入口有 --notes-file（长说明走文件，避免命令行转义）",
    "--notes-file" in src and "notes_file" in src)

mod = load_module()

# ── A/B：说明必填 ───────────────────────────────────────────────
rc, out = call_capture(mod._collect_notes, types.SimpleNamespace(notes="", notes_file=""))
chk("没有说明 ⇒ 退出码 2", rc == 2, "实际 %r" % (rc,))
chk("没有说明 ⇒ 提示去扫上一个 Release 的全部改动",
    ("上一个 tag" in out or "上一个 Release" in out) and "git log" in out, out[:300])

legacy = "Windows 安装包 h_n-printer_setup_v4.7.4.exe（含 Android APK）"
rc, out = call_capture(mod._collect_notes, types.SimpleNamespace(notes=legacy, notes_file=""))
chk("旧的自动一句话说明 ⇒ 拒发", rc == 2, "实际 %r" % (rc,))
chk("拒发时点名 4.7.4 这个事故形态", "4.7.4" in out, out[:300])

tmpdir = tempfile.mkdtemp(prefix="pubrel_")
try:
    notes_path = os.path.join(tmpdir, "notes.md")
    with open(notes_path, "w", encoding="utf-8") as f:
        f.write("v4.7.4：修三处会悄悄出错的地方。\n\n- 云端单按 0 页计费。\n")
    notes = mod._collect_notes(types.SimpleNamespace(notes="", notes_file=notes_path))
    chk("--notes-file 正常读入（去首尾空白）", notes.startswith("v4.7.4：修三处"), repr(notes[:40]))

    # ── C：APK 资产名 ────────────────────────────────────────────
    gradle_txt = open(GRADLE, encoding="utf-8").read()
    m = re.search(r'versionName\s+"([^"]+)"', gradle_txt)
    gradle_ver = m.group(1) if m else ""
    read_ver = mod.read_android_version_name()
    chk("从 build.gradle 读出 versionName", bool(read_ver) and read_ver == gradle_ver,
        "读出 %r / gradle %r" % (read_ver, gradle_ver))

    expect = "h_n-printer_android_v%s.apk" % read_ver
    chk("gradle 原始产物名 app-release.apk ⇒ 自动改成约定名",
        mod.apk_asset_name(os.path.join("x", "app-release.apk"), read_ver) == expect,
        mod.apk_asset_name(os.path.join("x", "app-release.apk"), read_ver))
    chk("已经是约定名 ⇒ 原样沿用",
        mod.apk_asset_name(os.path.join("dist", expect), "") == expect)
    chk("名字合不了约定且读不到 versionName ⇒ 返回空（调用方拒发）",
        mod.apk_asset_name(os.path.join("x", "app-release.apk"), "") == "")

    # ── D：stage_apk 真复制 ────────────────────────────────────
    fake_src = os.path.join(tmpdir, "app-release.apk")
    with open(fake_src, "wb") as f:
        f.write(b"PK\x03\x04fake-apk" + os.urandom(64))
    mod.DIST_DIR = os.path.join(tmpdir, "dist")
    staged, copied = mod.stage_apk(fake_src, expect)
    chk("stage_apk 产出约定名副本", copied and os.path.basename(staged) == expect, staged)
    chk("副本内容与源 APK 一致",
        open(staged, "rb").read() == open(fake_src, "rb").read())
    mtime = os.path.getmtime(staged)
    staged2, _ = mod.stage_apk(fake_src, expect)
    chk("stage_apk 幂等（同 md5 不重复复制）", staged2 == staged and os.path.getmtime(staged2) == mtime)

    # ── main()：漏说明时在找 exe 之前就拒发（不依赖本机构建产物） ──
    rc, out = call_capture(mod.main, ["--version", "4.7.4"])
    chk("publish_release.py --version 4.7.4（无说明）⇒ 退出码 2", rc == 2, "实际 %r" % (rc,))
    chk("拒绝理由指向发布流程.md", "发布流程.md" in out, out[:300])
finally:
    shutil.rmtree(tmpdir, ignore_errors=True)

if failures:
    print("\n[FAIL] %d 项未通过：" % len(failures))
    for name in failures:
        print("  - " + name)
    sys.exit(1)
print("\n[PASS] 全部 %d 项通过" % total)
sys.exit(0)
