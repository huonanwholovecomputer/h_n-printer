# 一键构建 debug APK（JDK 17 + Android SDK 装在 C:\Users\Administrator\android-tools）
$ErrorActionPreference = 'Stop'
$tools = 'C:\Users\Administrator\android-tools'
$env:JAVA_HOME = "$tools\jdk-17.0.20+8"
$env:ANDROID_HOME = "$tools\sdk"
$env:ANDROID_SDK_ROOT = "$tools\sdk"

Set-Location (Join-Path $PSScriptRoot 'android')
& .\gradlew.bat assembleDebug
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

$apk = Join-Path $PSScriptRoot 'android\app\build\outputs\apk\debug\app-debug.apk'
if (Test-Path $apk) {
  Write-Host "构建成功: $apk"
} else {
  Write-Host "构建结束，但未找到 APK 产物"
}
