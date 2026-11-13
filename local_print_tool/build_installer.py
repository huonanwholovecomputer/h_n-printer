"""build_installer.py — 构建本地打印工具一体化安装包

用法: python build_installer.py [--skip-pyinstaller]
产物: dist/h_n-printer_setup_v{版本}.exe（2026-11-13 起版本号前加 v，如 h_n-printer_setup_v4.6.13.exe）

流程: PyInstaller(API, 规避中文 argv 传参问题) → Inno Setup(ISCC, subprocess UTF-16 传参)
依赖: Inno Setup 7 (ISCC.exe)；--skip-pyinstaller 跳过重新打包（dist 已是最新时）
"""

from __future__ import annotations

import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))

_ISCC_CANDIDATES = (
    r"C:\Program Files (x86)\Inno Setup 7\ISCC.exe",
    r"C:\Program Files\Inno Setup 7\ISCC.exe",
    r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
)


def _setup_fname(version: str) -> str:
    """安装包文件名（与 .iss 的 OutputBaseFilename 同口径，用于复核产物确实生成）。"""
    return f"h_n-printer_setup_v{version}.exe"


def _read_app_version() -> str:
    with open(os.path.join(ROOT, "gui.py"), encoding="utf-8") as f:
        gui = f.read()
    m = re.search(r'APP_VERSION\s*=\s*"([^"]+)"', gui)
    return m.group(1) if m else "0.0.0"


def _pyinstaller_build() -> None:
    spec = os.path.join(ROOT, "HN打印工具.spec")
    print("==> 1/2 PyInstaller Release 打包")
    try:
        import PyInstaller.__main__  # 延迟导入，避免无 PyInstaller 时 import 失败
        PyInstaller.__main__.run([
            spec, "--noconfirm",
            "--distpath", os.path.join(ROOT, "dist"),
            "--workpath", os.path.join(ROOT, "build"),
        ])
    except SystemExit as e:
        # PyInstaller 失败时以 SystemExit 抛错；成功时正常返回
        if e.code:
            raise


def _inno_build(version: str) -> None:
    iscc = next((p for p in _ISCC_CANDIDATES if os.path.isfile(p)), None)
    if not iscc:
        print("ISCC.exe 未找到，请先安装 Inno Setup 7")
        sys.exit(1)
    iss = os.path.join(ROOT, "installer", "HN打印工具.iss")
    subprocess.run([iscc, f"/DMyAppVersion={version}", iss], check=True)


def main() -> None:
    skip_py = "--skip-pyinstaller" in sys.argv
    if not skip_py:
        _pyinstaller_build()
    version = _read_app_version()
    print(f"==> 2/2 Inno Setup 打包安装包 v{version}")
    _inno_build(version)
    setup = os.path.join(ROOT, "dist", _setup_fname(version))
    if not os.path.isfile(setup):
        # ISCC 覆盖「删除悬挂态」的旧同名产物时会静默不输出，必须自己复核产物
        print(f"BUILD FAILED: 未生成 {setup}（同名旧产物可能处于删除悬挂态，换 /F<新名> 重试）")
        sys.exit(1)
    print(f"BUILD OK: {setup} ({os.path.getsize(setup):,} B)")


if __name__ == "__main__":
    main()
