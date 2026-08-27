"""
updater.py — 本地打印工具自更新模块（v4.5）

流程：检查服务器更新清单 → 有新版本 → 下载安装包 → MD5 校验 → 生成 update.cmd
（轮询等待主程序退出 → 静默运行 Inno Setup 安装包 → 启动新版本）→ 主程序退出。

约定（与部署侧保持一致）：
- 更新清单：https://hn-space.cn/updates/update.json
  {"version": "4.4.2", "url": "https://hn-space.cn/updates/h_n-printer_setup_4.4.2.exe", "md5": "...", "notes": "..."}
- 安装包：Inno Setup 产物（英文文件名，避免 cmd/路径编码问题），静默参数
  /VERYSILENT /SUPPRESSMSGBOXES /NORESTART /SP-
- 安装目录：C:\\Program Files\\h_n printer（英文名，无中文路径）
- 用户数据在 %APPDATA%\\HN打印工具（paths.py），安装覆盖程序目录不影响配置
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import logging
import os
import urllib.request

logger = logging.getLogger(__name__)

UPDATE_MANIFEST_URL = "https://hn-space.cn/updates/update.json"
FETCH_TIMEOUT = 10
DOWNLOAD_CHUNK = 65536
_WAIT_MAX_LOOPS = 60  # update.cmd 轮询等待主程序退出上限（约 2 分钟）


class UpdateCancelled(Exception):
    """用户取消下载。"""


def fetch_update_info(timeout: int = FETCH_TIMEOUT) -> dict | None:
    """拉取更新清单。网络失败/格式错误返回 None（静默，不打扰用户）。"""
    try:
        req = urllib.request.Request(
            UPDATE_MANIFEST_URL, headers={"User-Agent": "HN-Print-Updater/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if not isinstance(data, dict) or not data.get("version"):
            logger.warning(f"更新清单格式异常: {data!r}")
            return None
        return data
    except Exception as e:
        logger.info(f"检查更新失败（忽略）: {e}")
        return None


def compare_versions(current: str, latest: str) -> bool:
    """latest > current 返回 True。按 . 分段数字比较（4.4.10 > 4.4.9）。"""
    def _parts(v: str):
        out = []
        for seg in str(v).strip().split("."):
            try:
                out.append(int(seg))
            except ValueError:
                out.append(0)
        return out
    return _parts(latest) > _parts(current)


def download_setup(url: str, expected_md5: str, dest_dir: str,
                   progress_cb=None) -> str | None:
    """下载安装包到 dest_dir 并校验 MD5。返回文件路径，失败/取消返回 None。
    progress_cb(downloaded, total) 在下载循环中回调（跨线程安全：由调用方负责信号转发）；
    回调返回 False 表示用户取消（抛 UpdateCancelled 中止并清理半截文件）。"""
    os.makedirs(dest_dir, exist_ok=True)
    fname = os.path.basename(url.split("?")[0]) or "setup.exe"
    dest = os.path.join(dest_dir, fname)
    tmp = dest + ".part"
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "HN-Print-Updater/1.0"})
        with urllib.request.urlopen(req, timeout=120) as resp, open(tmp, "wb") as f:
            hasher = hashlib.md5()
            total = int(resp.headers.get("Content-Length", 0) or 0)
            downloaded = 0
            chunk_idx = 0
            while True:
                chunk = resp.read(DOWNLOAD_CHUNK)
                if not chunk:
                    break
                f.write(chunk)
                hasher.update(chunk)
                downloaded += len(chunk)
                # 进度回调（约每 1MB 回调一次，避免高频信号刷 UI）
                if progress_cb:
                    chunk_idx += 1
                    if chunk_idx % 16 == 0 or downloaded >= total:
                        if progress_cb(downloaded, total) is False:
                            raise UpdateCancelled("用户取消下载")
        os.replace(tmp, dest)
        actual = hasher.hexdigest()
        if expected_md5 and actual.lower() != expected_md5.lower():
            logger.error(f"安装包 MD5 校验失败: 期望 {expected_md5}, 实际 {actual}")
            try:
                os.remove(dest)
            except OSError:
                pass
            return None
        return dest
    except UpdateCancelled:
        logger.info("下载被用户取消")
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    except Exception as e:
        logger.error(f"下载安装包失败: {e}")
        try:
            os.remove(tmp)
        except OSError:
            pass
        return None


def write_update_cmd(setup_path: str, exe_path: str, cmd_path: str,
                     start_cmd: str = "") -> bool:
    """生成 update.cmd：轮询等待主程序退出 → 静默安装 → 启动新版本 → 清理临时文件。
    cmd 按本地代码页（GBK）解析，文件用 gbk 编码写，避免中文路径乱码。
    start_cmd：启动新程序的完整命令（可含 cd /d + 参数）；为空时默认
    `start "" "{exe_path}"`（打包版直接启动 exe）。"""
    exe_name = os.path.basename(exe_path)
    if not start_cmd:
        start_cmd = f'start "" "{exe_path}"'
    lines = [
        "@echo off",
        "rem HN print tool self-update",
        "set n=0",
        ":wait",
        f'tasklist | findstr /i "{exe_name}" >nul',
        "if errorlevel 1 goto install",
        "set /a n+=1",
        f"if %n% gtr {_WAIT_MAX_LOOPS} goto install",
        "ping -n 2 127.0.0.1 >nul",
        "goto wait",
        ":install",
        f'"{setup_path}" /VERYSILENT /SUPPRESSMSGBOXES /NORESTART /SP-',
        start_cmd,
        "ping -n 2 127.0.0.1 >nul",
        f'del /q "{setup_path}"',
        "del \"%~f0\"",
    ]
    try:
        with open(cmd_path, "w", encoding="gbk", errors="replace") as f:
            f.write("\r\n".join(lines) + "\r\n")
        return True
    except OSError as e:
        logger.error(f"写入 update.cmd 失败: {e}")
        return False


def _run_cmd_minimized(cmd_path: str) -> bool:
    """以最小化窗口运行 update.cmd（SW_SHOWMINNOACTIVE=7）。"""
    try:
        ctypes.windll.shell32.ShellExecuteW(None, "open", cmd_path, None, None, 7)
        return True
    except Exception as e:
        logger.error(f"启动更新脚本失败: {e}")
        return False


def prepare_update(manifest: dict, current_version: str, progress_cb=None) -> tuple[str, str] | None:
    """下载并准备更新。成功返回 (setup_path, exe_path)；任何环节失败返回 None。
    exe_path 取当前运行的主程序路径（安装目录不变，更新后同一位置）。
    progress_cb 透传给 download_setup（进度回调；返回 False 取消 → 抛 UpdateCancelled）。"""
    url = manifest.get("url", "")
    md5 = manifest.get("md5", "")
    if not url:
        logger.error("更新清单缺少 url")
        return None
    dest_dir = os.path.join(os.environ.get("TEMP", "."), "hn_update")
    setup_path = download_setup(url, md5, dest_dir, progress_cb=progress_cb)
    if not setup_path:
        return None
    exe_path = os.path.abspath(__import__("sys").executable)
    return setup_path, exe_path
