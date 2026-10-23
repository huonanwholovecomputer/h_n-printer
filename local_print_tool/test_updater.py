# -*- coding: utf-8 -*-
"""test_updater.py — updater 模块端到端测试（真实服务器清单）

⚠ 手动冒烟脚本（会真的联网 + 下载约 100MB 安装包），不在 tests/ 自动验收里。
S1-02（2026-10-23）起：静默安装必须显式声明"用户已确认"；哈希走 sha256 优先 / md5 兼容。
"""
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from updater import (fetch_update_info, compare_versions, write_update_cmd, download_setup,
                     expected_hash)

info = fetch_update_info()
print("1) fetch_update_info ->", "OK" if info else "FAIL",
      ("version=" + str(info.get("version"))) if info else "")

print("2) compare_versions:")
print("   4.4.1 vs 4.4.1 ->", compare_versions("4.4.1", "4.4.1"), "(expect False)")
print("   4.4.0 vs 4.4.1 ->", compare_versions("4.4.0", "4.4.1"), "(expect True)")
print("   4.4.9 vs 4.4.10 ->", compare_versions("4.4.9", "4.4.10"), "(expect True)")

d = tempfile.mkdtemp()
setup = os.path.join(d, "h_n-printer_setup_4.4.1.exe")
exe = r"C:\Program Files\h_n printer\HN打印工具.exe"
cmd = os.path.join(d, "update.cmd")
ok = write_update_cmd(setup, exe, cmd, allow_silent_install=True)   # 本脚本=已获用户确认
print("3) write_update_cmd ->", "OK" if ok else "FAIL")
print("   --- update.cmd ---")
print(open(cmd, encoding="gbk").read())
print("   ------------------")

# 真实下载 + 哈希校验（约 100MB；sha256 优先，md5 兼容）
if info and info.get("url"):
    algo, digest = expected_hash(info)
    dest = download_setup(info["url"], digest, d, algo=algo)
    print("4) download_setup ->", "OK" if dest else "FAIL", os.path.basename(dest) if dest else "")
    if dest:
        os.remove(dest)

os.remove(cmd)
shutil.rmtree(d, ignore_errors=True)     # ready\previous\ROLLBACK_README.txt 都在 d 下
print("DONE")
