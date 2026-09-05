; HN打印工具 一体化安装包（Inno Setup 7）
; 构建：python ..\build_installer.py（推荐，自动从 gui.py 读 APP_VERSION 传入）
;   或  ISCC.exe /DMyAppVersion=4.5.7 HN打印工具.iss
; 输出：..\dist\h_n-printer_setup_{版本}.exe
;
; ⚠ 必须排除所有用户数据文件（Excludes 列表）——否则会把开发机的
;   服务器地址 + 打印机 token（print_config.json）分发给所有安装者！
;   用户数据统一存 %APPDATA%\HN打印工具（paths.py），安装包不包含任何用户数据。
; 安装目录用英文 C:\Program Files\h_n printer（避免中文路径兼容问题）。

#ifndef MyAppVersion
  #define MyAppVersion "4.5.7"
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
; 安装整个程序目录（递归），排除一切用户数据文件（配置/收支/缓存/日志/客户端ID）
Source: "..\dist\HN打印工具\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs; \
  Excludes: "print_config.json,theme_settings.json,user_bindings.json,printer-local.db,logs,pdf_cache,finance\print_data.json,.client_id"

; 覆盖安装时先清掉旧 _internal 与旧 exe（新版本删除的文件不残留）
[InstallDelete]
Type: filesandordirs; Name: "{app}\_internal"
Type: files; Name: "{app}\HN打印工具.exe"

[Icons]
Name: "{group}\HN打印工具"; Filename: "{app}\HN打印工具.exe"
Name: "{autodesktop}\HN打印工具"; Filename: "{app}\HN打印工具.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\HN打印工具.exe"; Description: "立即运行 HN 打印工具"; Flags: nowait postinstall skipifsilent
