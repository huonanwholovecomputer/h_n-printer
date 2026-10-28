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
    # 本 datas **就是主防线**（白名单）：用户数据（print_config.json / theme_settings.json /
    # file_cache / duplex_stack / logs / pdf_cache 等，见 paths.py）一律不得出现在这里 ——
    # 安装包的 .iss Excludes 只是第二道防线。两份清单都由
    # tests/verify_installer_excludes.py 与 paths.py 做集合比对。
    # （theme_settings.json 曾同时出现在这里与 Excludes 里，自相矛盾：它按 paths.py 属用户数据，
    #   已从 datas 移除 —— 主题设置丢失时程序按默认主题启动。）
    datas=[('styles_dark.qss', '.'), ('styles_light.qss', '.'), ('HN_printer.png', '.'), ('finance/settlement.html', 'finance'), ('finance/chart.umd.min.js', 'finance')],
    # S1-02 更新链加固（2026-10-23）：updater.py 用 cryptography 校验 update.json 的
    # ed25519 签名，是**延迟 import**（点「检查更新」时才进）—— PyInstaller 的静态分析
    # 看不到它，必须显式列；漏收 = 打包后门店机一检查更新就 ImportError（新版收不到更新，
    # 且只有现场才会发现）。cryptography 的 Rust 扩展由 hooks-contrib 的
    # hook-cryptography.py + pyi_rth_cryptography_openssl 负责收集（datas/binaries），
    # 这里补 hiddenimports 覆盖我们直接 import 的那几个子模块。
    # S1-05（2026-10-23）：财务 HMAC 的密钥用 DPAPI（win32crypt）保护；取不到会降级，
    # 但显式收进来才能走 DPAPI 主路径（否则打包版永远走降级分支）。
    # 按天日志（daily_log）在 MainWindow.__init__ 里才 import —— 静态分析本应收得到，
    # 但打包漏收的后果是「门店机一启动就 ImportError」，故与 cryptography 一样显式列一遍。
    hiddenimports=['PySide6.QtCore', 'PySide6.QtGui', 'PySide6.QtWidgets', 'pythoncom', 'win32com', 'win32com.client',
                   'win32crypt', 'daily_log',
                   'cryptography', 'cryptography.exceptions',
                   'cryptography.hazmat.primitives.asymmetric.ed25519',
                   'cryptography.hazmat.primitives.serialization',
                   'cryptography.hazmat.bindings._rust'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # 排除与打印无关的大型数据科学/ML 依赖（2026-09 体积排查）：
    # 根因：pymupdf.table → pandas → networkx/scipy → torch 级联，加上 numpy/llvmlite 等，把安装包从 110MB 撑到 231MB。
    # 已核实安全：应用自身与保留依赖(PySide6/PyMuPDF/PyPDF2/reportlab/python-docx/requests)均不 import numpy；
    #   pymupdf 仅在 to_pandas() 内惰性用 pandas（本工具不做表格提取）；engineio/socketio 对 aiohttp 为 try 保护导入（同步客户端走 requests）。
    excludes=['torch', 'tensorflow', 'jax', 'onnx', 'onnxruntime',
              'scipy', 'pandas', 'numpy',
              'networkx', 'numba', 'llvmlite',
              'matplotlib', 'sympy', 'mpmath', 'ml_dtypes', 'contourpy',
              'aiohttp', 'yarl', 'multidict', 'frozenlist', 'aiosignal', 'propcache',
              'pydantic', 'pydantic_core', 'openpyxl', 'fsspec', 'tqdm'],
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
