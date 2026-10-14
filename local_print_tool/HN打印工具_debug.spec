# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    # 注意：刻意不打包 print_config.json —— 它含本机真实服务器地址 + 打印机 token，
    # 打包进去会把打印机认证密钥分发给所有拿到安装包的人。
    # 程序首启动时 PrinterConfig.load() 因文件不存在会自动生成一份干净的默认配置（token 空、不连云端），
    # 接收方在设置里自行填写服务器地址与 token。
    # 同样不打包 finance/print_data.json —— 它含真实收支流水与成员姓名，属隐私数据。
    # 收支清算页在数据文件缺失时正常显示"空/未创建"（stats_server 返回 exists=false），由用户自行新建。
    # 本 datas 是**主防线**（白名单）：用户数据（print_config.json / theme_settings.json /
    # file_cache / duplex_stack / logs / pdf_cache 等，见 paths.py）一律不得出现在这里 ——
    # 两份 .spec 与 .iss 的 Excludes 都由 tests/verify_installer_excludes.py 与 paths.py 做集合比对。
    # （theme_settings.json 已移除：它按 paths.py 属用户数据，主题缺失时程序按默认主题启动。）
    datas=[('styles_dark.qss', '.'), ('styles_light.qss', '.'), ('HN_printer.png', '.'), ('finance/settlement.html', 'finance'), ('finance/chart.umd.min.js', 'finance')],
    hiddenimports=['PySide6.QtCore', 'PySide6.QtGui', 'PySide6.QtWidgets', 'pythoncom', 'win32com', 'win32com.client'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='HN打印工具_debug',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='HN_printer.ico',
    version='version_info.txt',
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='HN打印工具_debug',
)
