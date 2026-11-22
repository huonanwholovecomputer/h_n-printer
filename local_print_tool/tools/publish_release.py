#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把一次发版的产物发布到 GitHub Release（推荐渠道，免费不限量）。

为什么要有它：COS 桶改成私有 + 下载码之后，**公开用户唯一的正规渠道就是 GitHub
Release**。所以每发一版必须在这里也放一份安装包，否则新用户 / 拿不到下载码的人
就只能干等。手工在网页上拖 70 MB 的 exe 容易漏、也容易把 APK 忘掉，故脚本化。

它**只做 GitHub**这三件事（其余仍是手工步骤，见 docs/部署与发版.md）：
  1. 算出 exe 的 md5 / sha256（填 update.json 要这两个值）；
  2. `gh release create v<版本>`（已存在就跳过创建，只补资产）；
  3. 上传 exe（和 APK）为 Release 资产，`--clobber` 允许覆盖同名资产。

COS 上传（写权限子账号）与 update.json 的签名不在这里做 —— 私钥不该进脚本。

用法：
  python tools/publish_release.py --version 4.7.0
  python tools/publish_release.py --version 4.7.0 --apk path/to/app-release.apk --dry-run
  python tools/publish_release.py --version 4.7.0 --notes 本次修复…   # 覆盖自动生成的说明

退出码：0 成功 / 2 参数或环境不对 / 3 gh 命令失败。
"""

import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys

REPO_DEFAULT = "huonanwholovecomputer/h_n-printer"
HERE = os.path.dirname(os.path.abspath(__file__))
TOOL_DIR = os.path.dirname(HERE)                     # local_print_tool/
DIST_DIR = os.path.join(TOOL_DIR, "dist")
APK_DEFAULT = os.path.join(
    TOOL_DIR, "..", "mobile_apps", "android_app", "android", "app",
    "build", "outputs", "apk", "release", "app-release.apk")
VERSION_RE = re.compile(r"^\d+(?:\.\d+){1,3}$")


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


def main(argv=None):
    ap = argparse.ArgumentParser(description="发布安装包到 GitHub Release")
    ap.add_argument("--version", required=True, help="版本号，如 4.7.0")
    ap.add_argument("--exe", default="", help="安装包路径（默认 dist/h_n-printer_setup_v<版本>.exe）")
    ap.add_argument("--apk", default="", help="APK 路径（默认 android release 产物；传 none 跳过）")
    ap.add_argument("--notes", default="", help="Release 说明（不传则自动生成一句话）")
    ap.add_argument("--repo", default=REPO_DEFAULT)
    ap.add_argument("--dry-run", action="store_true", help="只打印将执行的命令")
    args = ap.parse_args(argv)

    ver = args.version.strip().lstrip("v")
    if not VERSION_RE.match(ver):
        _die(2, "版本号格式不对：%r（形如 4.7.0）" % args.version)
    tag = "v" + ver

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
    if apk.lower() == "none":
        apk = ""
    elif not apk:
        apk = os.path.abspath(APK_DEFAULT) if os.path.isfile(APK_DEFAULT) else ""
    else:
        apk = os.path.abspath(apk)
        if not os.path.isfile(apk):
            _die(2, "找不到 APK：%s" % apk)

    if not shutil.which("gh"):
        _die(2, "找不到 gh 命令 —— 请先安装 GitHub CLI 并 `gh auth login`")

    _say("═" * 62)
    _say("发布到 GitHub Release：%s  (%s)" % (tag, args.repo))
    _say("═" * 62)
    _say("  安装包：%s  (%s)" % (exe, _human(os.path.getsize(exe))))
    if apk:
        _say("  APK   ：%s  (%s)" % (apk, _human(os.path.getsize(apk))))
    else:
        _say("  APK   ：跳过")

    _say("")
    _say("校验值（填 installer/update.json 用）：")
    md5 = _hash_file(exe, "md5")
    sha256 = _hash_file(exe, "sha256")
    _say("  md5    = %s" % md5)
    _say("  sha256 = %s" % sha256)

    notes = args.notes.strip() or ("Windows 安装包 h_n-printer_setup_v%s.exe%s"
                                   % (ver, "（含 Android APK）" if apk else ""))
    notes = notes.replace("\\", "\\\\")

    _say("")
    _say("[1/2] 确认 Release %s 存在（不存在就建）" % tag)
    rc = _run(["gh", "release", "view", tag, "--repo", args.repo], dry_run=args.dry_run)
    if rc != 0:
        rc = _run(["gh", "release", "create", tag,
                   "--repo", args.repo,
                   "--title", "v" + ver,
                   "--notes", notes], dry_run=args.dry_run)
        if rc != 0:
            _die(3, "创建 Release 失败（检查 gh auth status / 仓库权限）")
    else:
        _say("  已存在，直接补资产（--clobber 覆盖同名文件）")

    _say("")
    _say("[2/2] 上传资产")
    files = [exe] + ([apk] if apk else [])
    rc = _run(["gh", "release", "upload", tag] + files + ["--repo", args.repo, "--clobber"],
              dry_run=args.dry_run)
    if rc != 0:
        _die(3, "上传失败")

    _say("")
    _say("✅ GitHub Release 完成：https://github.com/%s/releases/tag/%s" % (args.repo, tag))
    _say("")
    _say("别忘了另外两步（脚本故意不代做）：")
    _say("  1) 上传 COS（写权限子账号）：coscmd upload \"%s\" /" % os.path.basename(exe))
    _say("  2) 改 installer/update.json（version/md5/sha256/notes）→ 签名 → scp 到服务器 updates/")
    _say("     python tools/sign_update_manifest.py ..\\installer\\update.json --key <私钥> --write")
    return 0


if __name__ == "__main__":
    sys.exit(main())
