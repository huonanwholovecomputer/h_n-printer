"""
paths.py — 用户数据目录统一管理（自更新配套）

所有用户数据（云端配置/主题/收支/离线订单库/日志）统一存 %APPDATA%\\HN打印工具\\，
与程序安装目录（C:\\Program Files\\h_n printer）完全解耦：
安装包覆盖/卸载程序目录不影响任何用户数据，自更新无需迁移配置。

首次从旧版（绿色版，数据散在程序目录）升级时，migrate_legacy_data() 自动复制到新目录
（幂等：目标已存在则跳过，源文件保留兜底不删除）。

pdf_cache 属于可重建缓存（丢了只是首次转换变慢），不做全量迁移，避免启动卡顿。
"""

from __future__ import annotations

import logging
import os
import shutil

logger = logging.getLogger(__name__)

# 安装目录（Inno Setup 安装到 %ProgramFiles%\\h_n printer，英文名避免中文路径问题；
# 更新器/update.cmd 用 sys.executable 定位新 exe，此常量仅作文档/校验参考）
INSTALL_DIR = r"C:\Program Files\h_n printer"


def get_app_data_dir() -> str:
    """用户数据根目录：%APPDATA%\\HN打印工具（与既有 .client_id 同处）"""
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    d = os.path.join(base, "HN打印工具")
    os.makedirs(d, exist_ok=True)
    return d


def config_path() -> str:
    """云端配置（服务器地址/token/打印机/任务列表）"""
    return os.path.join(get_app_data_dir(), "print_config.json")


def theme_settings_path() -> str:
    return os.path.join(get_app_data_dir(), "theme_settings.json")


def bindings_path() -> str:
    """收支清算「授权」绑定文件（openid → 成员）"""
    return os.path.join(get_app_data_dir(), "user_bindings.json")


def local_db_path() -> str:
    """离线订单 SQLite（OfflineSync 写入，收支清算「本地订单统计」读取）"""
    return os.path.join(get_app_data_dir(), "printer-local.db")


def finance_data_path() -> str:
    """收支清算本地数据 print_data.json（静态页面仍在程序目录/MEIPASS，数据与程序分离）"""
    return os.path.join(get_app_data_dir(), "print_data.json")


def logs_dir() -> str:
    d = os.path.join(get_app_data_dir(), "logs")
    os.makedirs(d, exist_ok=True)
    return d


def pdf_cache_dir() -> str:
    """PDF 转换缓存（可重建；旧版程序目录的缓存不迁移，按需重建）"""
    d = os.path.join(get_app_data_dir(), "pdf_cache")
    os.makedirs(d, exist_ok=True)
    return d


# ── 旧版数据迁移（绿色版/程序目录 → %APPDATA%）──


def migrate_legacy_data(script_dir: str) -> None:
    """把旧版程序目录中的用户数据复制到 %APPDATA%\\HN打印工具。
    幂等：目标已存在则跳过（新数据优先）；源文件保留兜底（不删除）。"""
    app_data = get_app_data_dir()
    for rel in ("print_config.json", "theme_settings.json", "user_bindings.json",
                "printer-local.db"):
        _migrate_file(os.path.join(script_dir, rel), os.path.join(app_data, rel))
    # finance/print_data.json（旧版放程序目录/finance 下，新位置在数据根目录）
    _migrate_file(os.path.join(script_dir, "finance", "print_data.json"),
                  os.path.join(app_data, "print_data.json"))
    _merge_dir(os.path.join(script_dir, "logs"), os.path.join(app_data, "logs"))


def _migrate_file(src: str, dst: str) -> None:
    if os.path.isfile(src) and not os.path.exists(dst):
        try:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
            logger.info(f"[MIGRATE] {os.path.basename(src)} → {dst}")
        except OSError as e:
            logger.warning(f"[MIGRATE] 复制 {src} 失败: {e}")


def _merge_dir(src_dir: str, dst_dir: str) -> None:
    if not os.path.isdir(src_dir):
        return
    os.makedirs(dst_dir, exist_ok=True)
    for name in os.listdir(src_dir):
        s = os.path.join(src_dir, name)
        d = os.path.join(dst_dir, name)
        if os.path.isfile(s) and not os.path.exists(d):
            try:
                shutil.copy2(s, d)
            except OSError as e:
                logger.warning(f"[MIGRATE] 复制 {s} 失败: {e}")
        elif os.path.isdir(s):
            _merge_dir(s, d)
