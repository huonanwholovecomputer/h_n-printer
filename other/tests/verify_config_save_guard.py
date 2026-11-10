#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""验收 · 配置写盘失败必须让人看见（健壮性体检 🔴18）

背景（2026-11-10）：
  `MainWindow._save_config()` 失败时只 `logger.warning` + `return False`，而全仓 48 处
  调用点**没有一处**检查返回值（`grep -c "_save_config()"` = 48，`if not self._save_config()` = 0）。
  于是改价格 / 改打印机档案 / 改标签页时，界面显示已改、磁盘还是旧值，重启回旧值且全无提示。

现在：`_save_config` 失败时经 `_configSaveFailed` 信号回主线程，写一条日志 + 状态栏红字提示
（60 秒限频，避免后台同步每 10 秒刷一次把状态栏占死）。后台线程里 emit 也安全。

反向验证（必红）：
  · 把 `self._configSaveFailed.emit(str(e))` 删掉 → ① ② 红
  · 去掉 60 秒限频判断 → ③ 红
"""
import os
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LOCAL_TOOL = os.path.join(REPO, "local_print_tool")
_SANDBOX = tempfile.mkdtemp(prefix="hn_cfg_guard_")
os.environ["APPDATA"] = _SANDBOX
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, LOCAL_TOOL)
os.chdir(LOCAL_TOOL)

from PySide6.QtWidgets import QApplication  # noqa: E402

_app = QApplication.instance() or QApplication([])

import gui    # noqa: E402
import paths  # noqa: E402

_fails = []


def check(name, cond, detail=""):
    if cond:
        print(f"  [PASS] {name}")
    else:
        _fails.append(name)
        print(f"  [FAIL] {name}  ← {detail}")


class Sink:
    def __init__(self):
        self.lines = []

    def log(self, msg):
        self.lines.append(str(msg))

    def text(self):
        return "\n".join(self.lines)


sink = Sink()
win = gui.MainWindow(config_path=paths.config_path())
win._cloud_client = None
win._log = sink.log          # 收集日志，别再往界面/文件里写

print("── ① 写盘失败：返回 False + 日志 + 状态栏 ──")


def _boom(_path):
    raise OSError("磁盘已满（模拟）")


win._config.save = _boom
_ok = win._save_config(sync_ui=False)
check("①-a 返回值是 False（调用方能判断）", _ok is False, _ok)
check("①-b 日志里明确说了「配置没能写进磁盘」",
      "配置没能写进磁盘" in sink.text(), sink.text()[-300:])
check("①-c 日志里带上了失败原因", "磁盘已满" in sink.text(), sink.text()[-300:])
check("①-d 日志里给了配置文件路径（方便排查）",
      str(win._config_path) in sink.text(), win._config_path)
check("①-e 状态栏也提示了（不看日志的人也能发现）",
      "配置未能保存" in win.statusBar().currentMessage(),
      win.statusBar().currentMessage())

print("── ② 60 秒限频：一次故障不刷屏 ──")
_before = len(sink.lines)
win._save_config(sync_ui=False)
win._save_config(sync_ui=False)
check("②-a 紧接着再失败两次，不再重复记日志",
      len(sink.lines) == _before, (len(sink.lines), _before))
win._config_save_fail_ts -= 61.0          # 模拟过了限频窗口
win._save_config(sync_ui=False)
check("②-b 过了 60 秒会再提醒一次（持续故障不该只提一次）",
      len(sink.lines) > _before, len(sink.lines))

print("── ③ 恢复正常：不提示、返回 True ──")
sink.lines.clear()
win._config.save = lambda p: open(p, "w", encoding="utf-8").write("{}")
_ok = win._save_config(sync_ui=False)
check("③-a 恢复了就返回 True", _ok is True, _ok)
check("③-b 成功时不写告警日志", "配置没能写进磁盘" not in sink.text(), sink.text()[-200:])

print("── ④ 后台线程 emit 也安全（不能直接碰 QWidget）──")
_src = open(os.path.join(LOCAL_TOOL, "gui.py"), encoding="utf-8").read()
check("④-a 定义了 _configSaveFailed 信号",
      "_configSaveFailed = Signal(str)" in _src.split("class MainWindow", 1)[1][:1200])
check("④-b 在 __init__ 里连到主线程槽",
      "self._configSaveFailed.connect(self._on_config_save_failed)" in _src)
_save = _src.split("def _save_config(", 1)[1].split("\n    def ", 1)[0]
check("④-c _save_config 失败分支里 emit 了这个信号",
      "self._configSaveFailed.emit(str(e))" in _save)
check("④-d 有 60 秒限频的判据",
      "_config_save_fail_ts" in _src and "60.0" in _src.split("def _on_config_save_failed", 1)[-1][:600])
check("④-e 提示走日志 + 状态栏两条路",
      "配置没能写进磁盘" in _src and "showMessage" in _src.split("def _on_config_save_failed", 1)[-1][:900])

print()
if _fails:
    print(f"存在失败 ❌ {len(_fails)} 项：" + "、".join(_fails))
    sys.exit(1)
print("全部通过 ✅ 配置写盘失败可见（🔴18）")
