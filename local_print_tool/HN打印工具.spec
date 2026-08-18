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
    datas=[('styles_dark.qss', '.'), ('styles_light.qss', '.'), ('HN_printer.png', '.'), ('theme_settings.json', '.'), ('finance/settlement.html', 'finance'), ('finance/chart.umd.min.js', 'finance')],
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
    name='HN打印工具',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
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
    name='HN打印工具',
)
