#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把一次发版的产物发布到 GitHub Release（推荐渠道，免费不限量）。

为什么要有它：COS 桶改成私有 + 下载码之后，**公开用户唯一的正规渠道就是 GitHub
Release**。所以每发一版必须在这里也放一份安装包，否则新用户 / 拿不到下载码的人
就只能干等。手工在网页上拖 70 MB 的 exe 容易漏、也容易把 APK 忘掉，故脚本化。

它**只做 GitHub**这三件事（其余仍是手工步骤，见 docs/部署与发版.md 与
local_print_tool/发布流程.md）：
  1. 算出 exe 的 md5 / sha256（填 update.json 要这两个值）；
  2. `gh release create <版本>`（已存在就只同步说明 + 补资产）；
  3. 上传 exe（和 APK）为 Release 资产，`--clobber` 允许覆盖同名资产。

说明必须自己写（2026-11-24 起强制，`--notes` / `--notes-file` 二选一）：
  默认那行「Windows 安装包 h_n-printer_setup_vX.exe」不算说明 —— 4.7.4 就是这么发
  出去的，用户看到 Release 页一片空白，第 4.7.5 版到底修了什么无从考。写之前先扫
  `git log --oneline <上一个 tag>..HEAD` 与 `git diff --stat`，规则见发布流程.md。

APK 资产名统一 `h_n-printer_android_v{versionName}.apk`（与 4.7.1 及以前一致）：
  传进来的若还是 gradle 原始产物 `app-release.apk`，脚本会从 build.gradle 读
  versionName、复制成约定名再上传（4.7.4 曾把 `app-release.apk` 原样传上去）。

用法：
  python tools/publish_release.py --version 4.7.4 --notes-file ..\\..\\docs\\release_4.7.4.md
  python tools/publish_release.py --version 4.7.4 --notes "v4.7.4：..." --apk none
  python tools/publish_release.py --version 4.7.4 --notes-file notes.md --dry-run

退出码：0 成功 / 2 参数或环境不对 / 3 gh 命令失败。
"""

import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys

# Windows 控制台常是 GBK：打印 ✅/❌ 或中文路径会 UnicodeEncodeError 打断发布流程
# （2026-11-23 实测：资产已传完，最后一行 "✅ GitHub Release 完成" 把脚本炸了）。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

REPO_DEFAULT = "huonanwholovecomputer/h_n-printer"
HERE = os.path.dirname(os.path.abspath(__file__))
TOOL_DIR = os.path.dirname(HERE)                     # local_print_tool/
DIST_DIR = os.path.join(TOOL_DIR, "dist")
ANDROID_DIR = os.path.join(TOOL_DIR, "..", "mobile_apps", "android_app")
GRADLE_DEFAULT = os.path.join(ANDROID_DIR, "android", "app", "build.gradle")
APK_DEFAULT = os.path.join(ANDROID_DIR, "android", "app", "build", "outputs",
                           "apk", "release", "app-release.apk")
VERSION_RE = re.compile(r"^\d+(?:\.\d+){1,3}$")
# 约定名（2026-09 起）：h_n-printer_android_v{versionName}.apk，与安装包同一风格。
APK_ASSET_RE = re.compile(r"^h_n-printer_android_v[\w.\-]+\.apk$")
APK_NAME_FMT = "h_n-printer_android_v%s.apk"


def _say(msg=""):
    print(msg, flush=True)


def _die(code, msg):
    print("❌ " + msg, flush=True)
    sys.exit(code)


def _hash_file(path, algo):
    h = hashlib.new(algo)
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return ("%.1f %s" % (n, unit)) if unit != "B" else ("%d B" % n)
        n /= 1024.0


def _run(args, dry_run=False):
    _say("  $ " + " ".join(args))
    if dry_run:
        return 0
    try:
        proc = subprocess.run(args, capture_output=True, text=True,
                              encoding="utf-8", errors="replace")
    except FileNotFoundError:
        _die(2, "找不到 gh 命令 —— 请先安装 GitHub CLI 并 `gh auth login`")
    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    if out:
        for line in out.splitlines():
            _say("    " + line)
    if proc.returncode != 0:
        if err:
            for line in err.splitlines():
                _say("    ! " + line)
        return proc.returncode
    return 0


def read_android_version_name(gradle=GRADLE_DEFAULT):
    """从 build.gradle 读 versionName（APK 资产名要用它）；读不到返回空串。"""
    try:
        with open(gradle, encoding="utf-8") as f:
            txt = f.read()
    except OSError:
        return ""
    m = re.search(r'versionName\s+"([^"]+)"', txt)
    return m.group(1).strip() if m else ""


def apk_asset_name(apk_path, version_name):
    """返回该上传的资产名；已经合约定就原样用，否则按 versionName 起名。

    返回空串表示「名字合不了约定」——调用方应当拒绝发布而不是把 app-release.apk
    直接传上去（4.7.4 的教训）。
    """
    base = os.path.basename(apk_path)
    if APK_ASSET_RE.match(base):
        return base
    if version_name:
        return APK_NAME_FMT % version_name
    return ""


def stage_apk(apk_path, asset_name):
    """把 APK 摆到资产名下（同名就原地用），返回 (真正要上传的路径, 是否为副本)。"""
    if os.path.basename(apk_path) == asset_name:
        return apk_path, False
    target = os.path.join(DIST_DIR, asset_name)
    src_md5 = _hash_file(apk_path, "md5")
    if os.path.isfile(target) and _hash_file(target, "md5") == src_md5:
        return target, True
    os.makedirs(DIST_DIR, exist_ok=True)
    shutil.copy2(apk_path, target)
    _say("  已按约定复制：%s → %s" % (os.path.basename(apk_path), asset_name))
    return target, True


def _collect_notes(args):
    """说明必须自己写；旧的自动一句话直接拒绝（4.7.4 的事故形态）。"""
    notes = ""
    if args.notes_file:
        path = os.path.abspath(args.notes_file)
        if not os.path.isfile(path):
            _die(2, "找不到说明文件：%s" % path)
        with open(path, encoding="utf-8-sig") as f:
            notes = f.read()
    elif args.notes:
        notes = args.notes
    notes = notes.strip()
    if not notes:
        _die(2, "拒绝发布：没有 Release 说明。\n"
                "   公开用户只认 GitHub Release 这一页，先扫一遍上一个 Release 至今的全部改动，\n"
                "   写好「改了什么 / 用户会看到什么」再发：\n"
                "     git log --oneline <上一个 tag>..HEAD\n"
                "     git diff --stat <上一个 tag>..HEAD\n"
                "   然后传 --notes-file <文件>（或 --notes \"...\"）。规则见 local_print_tool/发布流程.md。")
    # 旧默认文案：只有文件名，没有任何改动内容 —— 等于没写。
    if notes.startswith("Windows 安装包") and len(notes) < 60:
        _die(2, "拒绝发布：这段说明只有文件名，没有写改了什么（4.7.4 就是这么发的）。\n"
                "   请按发布流程.md 的「Release 说明写法」重写：现象 → 现在的行为，不抄根因与内部文件名。")
    return notes


def main(argv=None):
    ap = argparse.ArgumentParser(description="发布安装包到 GitHub Release")
    ap.add_argument("--version", required=True, help="版本号，如 4.7.4")
    ap.add_argument("--exe", default="", help="安装包路径（默认 dist/h_n-printer_setup_v<版本>.exe）")
    ap.add_argument("--apk", default="", help="APK 路径（默认 android release 产物；传 none 跳过）")
    ap.add_argument("--apk-name", default="",
                    help="APK 资产名（默认按 versionName 起 h_n-printer_android_v<版本>.apk）")
    ap.add_argument("--notes", default="", help="Release 说明（与 --notes-file 二选一，必填）")
    ap.add_argument("--notes-file", default="", help="Release 说明文件（UTF-8，推荐）")
    ap.add_argument("--repo", default=REPO_DEFAULT)
    ap.add_argument("--dry-run", action="store_true", help="只打印将执行的命令")
    args = ap.parse_args(argv)

    ver = args.version.strip().lstrip("v")
    if not VERSION_RE.match(ver):
        _die(2, "版本号格式不对：%r（形如 4.7.4）" % args.version)
    # Release 标签一律裸版本号（4.7.1 / 4.7.0 / 4.6.13 …，与历史一致）：
    # 曾经这里加过 v 前缀，结果 gh release view v4.7.1 找不到 4.7.1，只能手工补。
    tag = ver
    notes = _collect_notes(args)

    exe = args.exe.strip() or os.path.join(DIST_DIR, "h_n-printer_setup_v%s.exe" % ver)
    exe = os.path.abspath(exe)
    if not os.path.isfile(exe):
        # 兼容不带 v 的老命名（安装包历史上两种都出现过）
        alt = os.path.join(DIST_DIR, "h_n-printer_setup_%s.exe" % ver)
        if os.path.isfile(alt):
            exe = alt
        else:
            _die(2, "找不到安装包：%s\n   先跑 `python build_installer.py`（产物在 dist/）" % exe)

    apk = args.apk.strip()
    apk_name = args.apk_name.strip()
    apk_copied = False
    if apk.lower() == "none":
        apk = ""
    elif not apk:
        # 约定名的产物优先（build-release-apk.ps1 会落在 android_app/dist/）
        apk_ver = read_android_version_name()
        preferred = os.path.join(ANDROID_DIR, "dist", APK_NAME_FMT % apk_ver) if apk_ver else ""
        if preferred and os.path.isfile(preferred):
            apk = os.path.abspath(preferred)
        elif os.path.isfile(APK_DEFAULT):
            apk = os.path.abspath(APK_DEFAULT)
        else:
            apk = ""
    else:
        apk = os.path.abspath(apk)
        if not os.path.isfile(apk):
            _die(2, "找不到 APK：%s" % apk)
    if apk:
        asset = apk_name or apk_asset_name(apk, read_android_version_name())
        if not asset:
            _die(2, "APK 文件名不合约定，且读不到 build.gradle 的 versionName：%s\n"
                    "   约定名是 h_n-printer_android_v<versionName>.apk；可用 --apk-name 显式指定。"
                    % os.path.basename(apk))
        apk, apk_copied = stage_apk(apk, asset)

    if not shutil.which("gh"):
        _die(2, "找不到 gh 命令 —— 请先安装 GitHub CLI 并 `gh auth login`")

    _say("═" * 62)
    _say("发布到 GitHub Release：%s  (%s)" % (tag, args.repo))
    _say("═" * 62)
    _say("  安装包：%s  (%s)" % (exe, _human(os.path.getsize(exe))))
    if apk:
        _say("  APK   ：%s  (%s)%s" % (apk, _human(os.path.getsize(apk)),
                                      "  [按约定复制]" if apk_copied else ""))
    else:
        _say("  APK   ：跳过")

    _say("")
    _say("校验值（填 installer/update.json 用）：")
    md5 = _hash_file(exe, "md5")
    sha256 = _hash_file(exe, "sha256")
    _say("  md5    = %s" % md5)
    _say("  sha256 = %s" % sha256)

    _say("")
    _say("Release 说明（%d 字）：" % len(notes))
    for line in notes.splitlines():
        _say("  │ " + line)

    _say("")
    _say("[1/2] 确认 Release %s 存在（不存在就建，已存在就同步说明）" % tag)
    rc = _run(["gh", "release", "view", tag, "--repo", args.repo], dry_run=args.dry_run)
    if rc != 0:
        rc = _run(["gh", "release", "create", tag,
                   "--repo", args.repo,
                   "--title", "h_n-printer v" + ver,
                   "--notes", notes], dry_run=args.dry_run)
        if rc != 0:
            _die(3, "创建 Release 失败（检查 gh auth status / 仓库权限）")
    else:
        _say("  已存在：同步说明 + 补资产（--clobber 覆盖同名文件）")
        if _run(["gh", "release", "edit", tag, "--repo", args.repo,
                 "--notes", notes], dry_run=args.dry_run) != 0:
            _die(3, "更新 Release 说明失败")

    _say("")
    _say("[2/2] 上传资产")
    files = [exe] + ([apk] if apk else [])
    rc = _run(["gh", "release", "upload", tag] + files + ["--repo", args.repo, "--clobber"],
              dry_run=args.dry_run)
    if rc != 0:
        _die(3, "上传失败")

    _say("")
    _say("✅ GitHub Release 完成：https://github.com/%s/releases/tag/%s" % (args.repo, tag))
    _say("   核对资产名（APK 必须是 h_n-printer_android_v<版本>.apk，不是 app-release.apk）：")
    _say("   gh release view %s --repo %s --json assets --jq '.assets[].name'"
         % (tag, args.repo))
    _say("")
    _say("别忘了另外两步（脚本故意不代做）：")
    _say("  1) 上传 COS（写权限子账号）：coscmd upload \"%s\" /" % os.path.basename(exe))
    _say("  2) 改 installer/update.json（version/md5/sha256/notes）→ 签名 → scp 到服务器 updates/")
    _say("     python tools/sign_update_manifest.py ..\\installer\\update.json --key <私钥> --write")
    return 0


if __name__ == "__main__":
    sys.exit(main())
