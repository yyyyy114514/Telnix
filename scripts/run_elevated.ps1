$ErrorActionPreference = "Continue"
$projectRoot = (Resolve-Path "$PSScriptRoot\..").Path
$logFile = Join-Path $projectRoot "telnix_elevated.log"
$errFile = Join-Path $projectRoot "telnix_elevated.err.log"
# 关闭系统代理（避免残留）
try {
    $proxyPath = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings"
    Set-ItemProperty -Path $proxyPath -Name ProxyEnable -Value 0 -Type DWord -ErrorAction Stop
} catch {}
Set-Location (Join-Path $projectRoot "src\host")
# 用 tee 同时输出到日志和原 stdout
& python -m telnix --no-browser *>&1 | Tee-Object -FilePath $logFile
