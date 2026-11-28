"""startup.py — 开机自启（HKCU 的 Run 项）+ 静默启动（最小化到托盘）。

为什么写 `HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run` 而不是「启动」文件夹快捷方式：
  · 不用 COM（IShellLink），也不用管理员权限，不会触发 UAC；
  · 路径与参数一目了然，排查「为什么没自启」时直接看注册表即可；
  · 卸载/关掉时一条 DeleteValue 就干净，不留 .lnk 垃圾。

「静默启动」不是另存一个开关，而是**同一条命令里的 `--start-minimized` 参数** —— 这样
「开没开自启」永远只有一个真相（有没有这个值），不会出现「配置说静默、注册表没写参数」
这种自相矛盾的状态。main.py 收到这个参数就不弹主窗口，直接进托盘。

本模块只碰注册表、不弹窗、不依赖 PySide6，方便验收脚本在沙盒子里直接跑
（`KEY_PATH` / `VALUE_NAME` / `KEY_ROOT` 都是模块级变量，测试可临时改到测试子键）。
"""

from __future__ import annotations

import os
import subprocess
import sys

# 用户级 Run 键：登录时由 Explorer 以当前用户身份执行，不需要管理员权限。
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
# 值名就是任务管理器「启动」页里显示的名字，也是关闭时 DeleteValue 的键。
VALUE_NAME = "HN打印工具"
# 静默启动开关（只认这一个参数，别再加别名，否则注册表里会百花齐放）
SILENT_FLAG = "--start-minimized"

KEY_ROOT = None          # None ⇒ 运行时取 winreg.HKEY_CURRENT_USER（便于测试替换）
KEY_PATH = RUN_KEY


def _winreg():
    """延迟导入 winreg：非 Windows 上导入即 ImportError，这里给一句人话。"""
    try:
        import winreg  # noqa: PLC0415  （Windows 专属模块，必须在函数里导）
    except ImportError as e:  # pragma: no cover - 本工具只在 Windows 跑
        raise RuntimeError("开机自启只支持 Windows（winreg 不可用）") from e
    return winreg


def _root():
    if KEY_ROOT is not None:
        return KEY_ROOT
    return _winreg().HKEY_CURRENT_USER


def is_frozen() -> bool:
    """是不是 PyInstaller 打出来的 exe（打包版没有 .py 可指，直接指自己）。"""
    return bool(getattr(sys, "frozen", False))


def app_command(silent: bool = False) -> str:
    """要写进 Run 项的命令行。

    · 打包版：`"<安装目录>\\HN打印工具.exe"`
    · 源码版：`"<python.exe>" "<local_print_tool>\\main.py"`（开发/验收直接用）
    · silent=True 时追加 `--start-minimized`（main.py 认它，不弹主窗口）。
    统一走 subprocess.list2cmdline：路径含空格、中文、括号都能正确加引号。
    """
    if is_frozen():
        argv = [sys.executable]
    else:
        here = os.path.dirname(os.path.abspath(__file__))
        argv = [sys.executable, os.path.join(here, "main.py")]
    if silent:
        argv.append(SILENT_FLAG)
    return subprocess.list2cmdline(argv)


def read_command() -> str:
    """读回当前登记的命令行；没登记 / 读不了都返回空串（不抛异常，供 GUI 显示）。"""
    try:
        wr = _winreg()
        with wr.OpenKey(_root(), KEY_PATH) as key:
            value, _typ = wr.QueryValueEx(key, VALUE_NAME)
            return str(value or "").strip()
    except (FileNotFoundError, OSError, RuntimeError):
        return ""


def is_enabled() -> bool:
    """Run 项里有没有这个名字（有就等于开了，路径对不对另说：见 command_is_current）。"""
    return bool(read_command())


def is_silent() -> bool:
    """当前登记的命令带不带 --start-minimized。"""
    return SILENT_FLAG in read_command().lower()


def command_is_current() -> bool:
    """登记的路径是否还指着现在这个程序。

    换安装目录 / 免安装版挪了位置 / 从源码版换成打包版之后，注册表里还留着旧路径：
    开机时执行旧命令什么也不会发生（explorer 静默失败），而 GUI 若只看「有没有值」
    就会一直显示「已开启」—— 所以设置页要能把这种情况说出来。
    """
    cmd = read_command()
    if not cmd:
        return False
    return cmd.lower().startswith(app_command(silent=is_silent()).lower())


def set_enabled(enabled: bool, silent: bool = False) -> tuple[bool, str]:
    """开 / 关开机自启。返回 (是否成功, 给人看的一句话)，绝不抛异常给 Qt 槽。"""
    try:
        wr = _winreg()
        root = _root()
    except RuntimeError as e:
        return False, str(e)
    try:
        if enabled:
            cmd = app_command(silent=silent)
            # 先 CreateKeyEx（键不存在也能建），写值后立刻读回比对 —— SetValueEx 成功
            # 但写入被组策略/安全软件静默重定向的情况只靠读回才能发现。
            with wr.CreateKeyEx(root, KEY_PATH, 0, wr.KEY_SET_VALUE) as key:
                wr.SetValueEx(key, VALUE_NAME, 0, wr.REG_SZ, cmd)
            got = read_command()
            if got.lower() != cmd.lower():
                return False, "写入注册表后读回不一致（可能被安全软件拦截）"
            return True, "已登记开机自启" + ("（静默启动）" if silent else "")
        try:
            with wr.OpenKey(root, KEY_PATH, 0, wr.KEY_SET_VALUE) as key:
                wr.DeleteValue(key, VALUE_NAME)
        except FileNotFoundError:
            pass
        if read_command():
            return False, "删除注册表项失败（值仍然存在）"
        return True, "已关闭开机自启"
    except OSError as e:
        return False, f"注册表操作失败：{e}"


def status() -> dict:
    """给设置页用的一份快照（全部是纯数据，不抛异常）。"""
    cmd = read_command()
    expected = app_command(silent=is_silent() if cmd else True)
    return {
        "enabled": bool(cmd),
        "silent": SILENT_FLAG in cmd.lower(),
        "command": cmd,
        "expected": expected,
        "current": bool(cmd) and command_is_current(),
        "value_name": VALUE_NAME,
        "key": KEY_PATH,
        "exe": sys.executable,
        "frozen": is_frozen(),
    }
