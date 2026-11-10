#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""验收 · 更新失败要有回滚说明书（健壮性体检 🔴17）

背景（2026-11-10）：
  `installer/HN打印工具.iss` 的 `[InstallDelete]` 会在装新版本之前删掉 `_internal` 与
  `HN打印工具.exe`；而 `update.cmd` 的 `:install_failed` 分支以前只 `start` 那个已被删掉的
  exe，注释里承诺的 `ROLLBACK_README.txt` 在全仓**没有任何写入点**。
  结果：一旦安装失败（UAC 被拒 / 杀软拦截 / 磁盘满），用户看到的是「程序没了，也没有任何说明」。

现在：
  ① `updater.write_update_note()` 在**启动安装器之前**写下这次更新的回滚说明
     （旧版本号、新版本号、安装包位置、previous\\ 目录、手动重装命令）
  ② `update.cmd` 的 `:install_failed` 分支自己再写一份 `INSTALL_FAILED.txt`
     并 `notepad` 打开那份回滚说明

反向验证（必红）：
  · 删掉 cmd 里 `start "" notepad "<note>"` 那行 → ② 红
  · 把 `write_update_note` 里的目标版本号去掉 → ① 红
"""
import os
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LOCAL_TOOL = os.path.join(REPO, "local_print_tool")
sys.path.insert(0, LOCAL_TOOL)

import updater  # noqa: E402

_fails = []


def check(name, cond, detail=""):
    if cond:
        print(f"  [PASS] {name}")
    else:
        _fails.append(name)
        print(f"  [FAIL] {name}  ← {detail}")


_SANDBOX = tempfile.mkdtemp(prefix="hn_upd_rb_")
_ready = os.path.join(_SANDBOX, "updates", "ready")
os.makedirs(_ready, exist_ok=True)
_previous = os.path.join(_SANDBOX, "updates", "previous")
os.makedirs(_previous, exist_ok=True)
_setup = os.path.join(_ready, "h_n-printer_setup_4.5.40.exe")
open(_setup, "wb").write(b"MZ fake installer")
_exe = os.path.join(_SANDBOX, "app", "HN打印工具.exe")
os.makedirs(os.path.dirname(_exe), exist_ok=True)
open(_exe, "wb").write(b"MZ fake exe")

print("── ① 回滚说明：装之前就写好，且内容是这一版的具体信息 ──")
note = updater.write_update_note(_ready, "4.5.39", "4.5.40", _setup,
                                 os.path.dirname(_exe))
check("①-a 返回了路径且文件真的在", bool(note) and os.path.isfile(note), note)
_text = open(note, encoding="utf-8").read() if os.path.isfile(note) else ""
check("①-b 标题说明了是「本次更新」的回滚说明",
      "本次更新" in _text and "回滚" in _text, _text[:80])
check("①-c 从哪个版本 → 到哪个版本", "4.5.39" in _text and "4.5.40" in _text)
check("①-d 安装包位置写在里面", _setup in _text)
check("①-e 上一版目录写在里面（回滚的真正材料）", _previous in _text)
check("①-f 安装目录写在里面（重装要装回原处）", os.path.dirname(_exe) in _text)
check("①-g 手动重装命令行写着 /DIR= 与 /VERYSILENT",
      "/DIR=" in _text and "/VERYSILENT" in _text)
check("①-h 文件在 updates 根目录（不在 ready\\，安装器不会动它）",
      os.path.dirname(note) == os.path.join(_SANDBOX, "updates"),
      os.path.dirname(note))

print("── ② update.cmd 的安装失败分支：自己留说明 + 打开回滚说明 ──")
_cmd = os.path.join(_ready, "update.cmd")
ok = updater.write_update_cmd(_setup, _exe, _cmd,
                              start_cmd=f'start "" "{_exe}"',
                              install_dir=os.path.dirname(_exe),
                              allow_silent_install=True)
check("②-a 生成成功", ok and os.path.isfile(_cmd))
_cmd_txt = open(_cmd, encoding="gbk", errors="replace").read() if os.path.isfile(_cmd) else ""
check("②-b 有 :install_failed 分支", ":install_failed" in _cmd_txt)
_fail_block = _cmd_txt.split(":install_failed", 1)[-1] if ":install_failed" in _cmd_txt else ""
check("②-c 失败分支里写了 INSTALL_FAILED.txt", "INSTALL_FAILED.txt" in _fail_block)
check("②-d 失败分支里让人看到回滚说明（notepad 打开它）",
      "notepad" in _fail_block and updater.ROLLBACK_NOTE_NAME in _fail_block,
      _fail_block[:200])
check("②-e 注释解释了为什么以前是空承诺（.iss 先删程序）",
      "[InstallDelete]" in _cmd_txt or "InstallDelete" in _cmd_txt, _fail_block[:120])
check("②-f 仍然会尝试重新启动程序（装了但没起来的情况）",
      "start" in _fail_block.split("notepad", 1)[-1], _fail_block[-200:])

print("── ③ 主程序侧：启动安装器之前就调用（源码层）──")
_src = open(os.path.join(LOCAL_TOOL, "gui.py"), encoding="utf-8").read()
_w = _src.split("def _update_download_worker", 1)[1].split("\n    def ", 1)[0]
check("③-a 下载完成后调用了 write_update_note", "write_update_note(" in _w)
check("③-b 调用点在 `_run_cmd_minimized`（真正启动安装器）之前",
      _w.index("write_update_note(") < _w.index("_run_cmd_minimized("), "顺序反了")
check("③-c 传了当前版本与目标版本",
      "APP_VERSION" in _w.split("write_update_note(", 1)[-1][:300])

print()
if _fails:
    print(f"存在失败 ❌ {len(_fails)} 项：" + "、".join(_fails))
    sys.exit(1)
print("全部通过 ✅ 更新失败的回滚说明（🔴17）")
