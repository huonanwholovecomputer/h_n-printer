# One-click debug APK build (JDK 17 + Android SDK in C:\Users\Administrator\android-tools)
$ErrorActionPreference = 'Stop'
$tools = 'C:\Users\Administrator\android-tools'
$env:JAVA_HOME = "$tools\jdk-17.0.20+8"
$env:ANDROID_HOME = "$tools\sdk"
$env:ANDROID_SDK_ROOT = "$tools\sdk"

# IMPORTANT: sync latest www into android/app/src/main/assets/public BEFORE building,
# otherwise the APK packages the STALE web assets from the last sync.
# (Browser serves index.html from www/ directly, so it shows fixes that are missing in the APK.)
$capCli = Join-Path $PSScriptRoot 'node_modules\@capacitor\cli\bin\capacitor'
if (Test-Path $capCli) {
  Write-Host '==> cap sync android (copy latest www into Android assets)'
  Set-Location $PSScriptRoot
  # Disable Capacitor telemetry once (persisted to sysconfig), so the first-run
  # interactive "Share anonymous usage data?" prompt cannot stall the build.
  & node $capCli telemetry off 2>&1 | Out-Null
  & node $capCli sync android
  if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
} else {
  Write-Warning "cap CLI not found at $capCli - SKIPPING sync! APK will use stale web assets. Run npm install first."
}

Set-Location (Join-Path $PSScriptRoot 'android')
# 2>&1: 合并 stderr（javac 警告等）到 stdout，避免 PowerShell 将外部程序 stderr 视为终止错误
& .\gradlew.bat assembleDebug 2>&1
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

$apk = Join-Path $PSScriptRoot 'android\app\build\outputs\apk\debug\app-debug.apk'
if (Test-Path $apk) {
  Write-Host "BUILD OK: $apk"
  # 复制为友好文件名：HN云打印_v{版本号}_debug.apk（版本号自动从 build.gradle 读取）
  $gradleFile = Join-Path $PSScriptRoot 'android\app\build.gradle'
  $version = ''
  if (Test-Path $gradleFile) {
    $m = [regex]::Match((Get-Content $gradleFile -Raw), 'versionName\s+"([^"]+)"')
    if ($m.Success) { $version = $m.Groups[1].Value }
  }
  $distDir = Join-Path $PSScriptRoot 'dist'
  New-Item -ItemType Directory -Force -Path $distDir | Out-Null
  $friendly = "HN云打印_v${version}_debug.apk"
  Copy-Item $apk (Join-Path $distDir $friendly) -Force
  Write-Host "COPIED : $(Join-Path $distDir $friendly)"
} else {
  Write-Host 'Build finished but APK not found'
}
