# -*- coding: utf-8 -*-
"""验收 · S1-10 部署链（tar 排除项）与主机硬化

依据：docs/安全加固修复计划_2026-10-23.md §S1-10（DS-10 / DS-18 / R9）。

现场形状：部署靠 `更新后端服务.txt` 里一条手写 tar 管道，排除项靠人记。漏一条
`--exclude=config.py` 就会把开发机的微信 AppSecret / SECRET_KEY / printer token 覆盖到生产
（DS-10）；`printer-backend.service` 原本还是 `Restart=no`（进程一崩就永久停服）。

本脚本：
  A. 静态接线：`deploy_backend.sh` 存在且排除项齐全（config/users.db/uploads/备份/运行时状态）、
     systemd 单元含硬化项、`single_instance.py` 用 Global\\+SID 且保留 Local\\ 回退。
  B. 行为：真跑 `bash deploy_backend.sh --dry-run`，断言清单里**不含**敏感文件、
     且含 app.py（证明排除项没把要部署的东西也排掉）。
  C. 变异自证：从脚本里删掉 `--exclude=./config.py` → 清单里出现 config.py（判据转红）。

用法：python tests/verify_deploy_excludes.py     （exit 0 = 全部通过）
"""
import io
import os
import re
import subprocess
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _sec_harness as H  # noqa: E402

H.install_cleanup_hook()
REPO = H.REPO
BACKEND = os.path.join(REPO, "mobile_apps", "printer-backend")
SCRIPT = os.path.join(BACKEND, "deploy_backend.sh")
UNIT = os.path.join(BACKEND, "printer-backend.service")
SI = os.path.join(REPO, "local_print_tool", "single_instance.py")

script = open(SCRIPT, encoding="utf-8").read() if os.path.exists(SCRIPT) else ""
unit = open(UNIT, encoding="utf-8").read() if os.path.exists(UNIT) else ""
si = open(SI, encoding="utf-8").read() if os.path.exists(SI) else ""

# ============================================================
# A. 静态接线
# ============================================================
print("=== A. 静态接线 ===")
H.chk("部署脚本存在（部署链唯一入口）", bool(script), SCRIPT)
for pat in ("config.py", "orders.db", "users.db", "uploads", "*.bak", "devices.json",
            "device_secrets.json", "printer_claim.json", "logs"):
    H.chk("部署脚本排除 %s" % pat, ("--exclude='./%s" % pat) in script or
          ("--exclude=./%s" % pat) in script, pat)
H.chk("部署脚本带 --dry-run（可离线核对清单）", "--dry-run" in script)

H.chk("systemd 单元存在", bool(unit), UNIT)
for key in ("Restart=always", "RestartSec=3", "ProtectSystem=strict",
            "ReadWritePaths=", "PrivateTmp=yes", "NoNewPrivileges=yes",
            "MemoryMax=", "TasksMax=", "LimitNOFILE=", "StartLimitIntervalSec=0"):
    H.chk("systemd 单元含 %s" % key, key in unit, key)
H.chk("配置从 /etc/hn-printer/config.py 读（服务器侧维护）",
      "HN_CONFIG_PATH=/etc/hn-printer/config.py" in unit
      and "HN_CONFIG_PATH" in open(os.path.join(BACKEND, "app.py"), encoding="utf-8").read())

H.chk("单实例锁用 Global\\\\ + 用户 SID",
      'for _ns in ("Global", "Local")' in si and "_user_sid_suffix()" in si)
H.chk("单实例锁按命名空间列表生成 Global / Local 两个名字（Local 是回退）",
      'for _ns in ("Global", "Local"):' in si
      and "SingleInstance_Mutex_%s" in si)
H.chk("单实例锁兼容旧版无后缀名（升级瞬间不双开）", "_LEGACY_MUTEX_NAME" in si)

# ============================================================
# B. 行为：真跑 dry-run 看清单
# ============================================================
print("\n=== B. 行为（bash deploy_backend.sh --dry-run）===")
proc = subprocess.run(["bash", SCRIPT, "--dry-run"], cwd=BACKEND, capture_output=True,
                      text=True, encoding="utf-8", errors="replace", timeout=120)
manifest = proc.stdout or ""
H.chk("dry-run 退出码 0", proc.returncode == 0, proc.stderr[-300:])
H.chk("清单含 app.py（要部署的东西没被排除掉）", "./app.py" in manifest)
H.chk("清单含 gunicorn_config.py", "./gunicorn_config.py" in manifest)

for bad in ("./config.py", "./orders.db", "./users.db", "./uploads/", "./devices.json",
            "./printer_claim.json", "./logs/", "./device_secrets.json"):
    hit = [ln for ln in manifest.splitlines()
           if ln == bad or (bad.endswith("/") and ln.startswith(bad))]
    H.chk("清单**不含** %s" % bad, not hit, hit[:3])

bak_hits = [ln for ln in manifest.splitlines() if re.search(r"\.bak", ln)]
H.chk("清单**不含**任何 *.bak 备份（含 token 的历史配置）", not bak_hits, bak_hits[:3])

# ============================================================
# C. 变异自证：删掉 config.py 的排除项
# ============================================================
print("\n=== C. 变异自证（删掉 config.py 排除项）===")
tmp_root, tmp_dst = H.make_copy("hn_deploy_mut_")
mut_script = os.path.join(tmp_dst, "deploy_backend.sh")
src_script = open(mut_script, encoding="utf-8").read()
if "  --exclude='./config.py'\n" in src_script:
    mutated = src_script.replace("  --exclude='./config.py'\n", "")
else:
    mutated = src_script.replace("--exclude='./config.py'", "")
open(mut_script, "w", encoding="utf-8").write(mutated)
# 复制一份假 config.py 进去（真实仓库里 config.py 被 gitignore，副本里也没有）
open(os.path.join(tmp_dst, "config.py"), "w", encoding="utf-8").write("SECRET_KEY='x'\n")
proc2 = subprocess.run(["bash", mut_script, "--dry-run"], cwd=tmp_dst, capture_output=True,
                       text=True, encoding="utf-8", errors="replace", timeout=120)
H.chk("去掉排除项后清单里出现 config.py（判据能红）",
      "./config.py" in (proc2.stdout or ""),
      (proc2.stdout or "")[:200] + (proc2.stderr or "")[-200:])

H.finish()
