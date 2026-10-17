; HN打印工具 一体化安装包（Inno Setup 7）
; 构建：python ..\build_installer.py（推荐，自动从 gui.py 读 APP_VERSION 传入）
;   或  ISCC.exe /DMyAppVersion=4.5.7 HN打印工具.iss
; 输出：..\dist\h_n-printer_setup_{版本}.exe
;
; ⚠ 用户数据（云端配置/主题/收支/离线库/暂存副本/攒批账本/缓存/日志）统一存
;   %APPDATA%\HN打印工具（见 paths.py），安装包里**不应**有任何一项。
;
;   两道防线，不要把它们搞混：
;     · **主防线 = .spec 的 datas 白名单** —— PyInstaller 只打包 datas 里显式列出的资源，
;       用户数据压根不进 dist/，所以当前 [Files] 的 Excludes 其实"什么都拦不到"；
;     · **第二道防线 = 下面 Source 行的 Excludes** —— 拦的是"dist/ 里一旦出现用户数据"
;       的情形（有人放宽 datas、或构建前误把开发机数据拷进 dist）。它当前是无效的，
;       **正因为无效才更容易被误当成主防线**。
;   缺任何一道都危险：只有第二道 → 有人以为它在拦，其实数据已经在 dist 里了；
;   只有第一道 → 一次 `datas=[('*','.')]` 的放宽就会把 print_config.json
;   （本机服务器地址 + 打印机 token）分发给所有拿到安装包的人。
;   本 Excludes 清单由 tests/verify_installer_excludes.py 与 paths.py 的
;   get_app_data_dir() 清单做**集合比对** —— 将来新增一个用户数据目录会被那条守卫抓住。
; 安装目录用英文 C:\Program Files\h_n printer（避免中文路径兼容问题）。

#ifndef MyAppVersion
  #define MyAppVersion "4.5.21"
#endif

[Setup]
AppId={{6F2C8A91-4D7E-4B2A-9E3C-1F0D5B8A7C66}
AppName=HN打印工具
AppVersion={#MyAppVersion}
AppPublisher=HN-Print
; 版本元数据：AppVersion 只影响"产品版本"；"文件版本"须用 VersionInfoVersion 显式设置，
; 否则安装包属性里"文件版本"会显示 0.0.0.0（此前版本一直如此，2026-09 修正）。
VersionInfoVersion={#MyAppVersion}.0
VersionInfoCopyright=Copyright (C) HN-Print
VersionInfoCompany=HN-Print
VersionInfoProductName=HN打印工具
VersionInfoProductVersion={#MyAppVersion}
DefaultDirName={autopf}\h_n printer
DefaultGroupName=HN打印工具
UninstallDisplayIcon={app}\HN打印工具.exe
OutputDir=..\dist
OutputBaseFilename=h_n-printer_setup_{#MyAppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=admin
ArchitecturesInstallIn64BitMode=x64compatible
DisableProgramGroupPage=yes
; 自更新静默安装时用户看不到向导
CloseApplications=yes

[Languages]
Name: "chinesesimp"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加图标:"; Flags: unchecked

[Files]
; 安装整个程序目录（递归），排除一切用户数据文件（配置/收支/缓存/暂存副本/攒批账本/打印机档案/日志/客户端ID）
; ⚠ 这是**第二道防线**（主防线是 .spec 的 datas 白名单，见文件头说明）。
;   清单必须与 paths.py 的 get_app_data_dir() 下的全部条目一致 ——
;   tests/verify_installer_excludes.py 会做集合比对，漏一项就红。
Source: "..\dist\HN打印工具\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs; \
  Excludes: "print_config.json,theme_settings.json,user_bindings.json,printer-local.db,.processed_tasks.json,print_data.json,printer_profiles.json,logs,pdf_cache,file_cache,duplex_stack,.client_id,finance\print_data.json"

; 覆盖安装时先清掉旧 _internal 与旧 exe（新版本删除的文件不残留）
[InstallDelete]
Type: filesandordirs; Name: "{app}\_internal"
Type: files; Name: "{app}\HN打印工具.exe"

[Icons]
Name: "{group}\HN打印工具"; Filename: "{app}\HN打印工具.exe"
Name: "{autodesktop}\HN打印工具"; Filename: "{app}\HN打印工具.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\HN打印工具.exe"; Description: "立即运行 HN 打印工具"; Flags: nowait postinstall skipifsilent
