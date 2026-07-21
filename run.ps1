# OpenNet 一键运行脚本
# Usage: .\run.ps1 [args...]
#
# 传递给 python -m opennet 的参数：
#   .\run.ps1                  # 默认（自动开浏览器）
#   .\run.ps1 --no-browser     # 不开浏览器
#   .\run.ps1 --port 18902     # 自定义端口

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path $PSScriptRoot).Path
$hostDir = Join-Path $projectRoot "src\host"

# 检查后端是否已安装
$opennetPkg = Join-Path $hostDir "opennet\__init__.py"
if (-not (Test-Path $opennetPkg)) {
    Write-Host "ERROR: opennet 包未找到: $opennetPkg" -ForegroundColor Red
    Write-Host "请先运行: .\install.ps1" -ForegroundColor Yellow
    exit 1
}

# 检查前端是否已构建（src\host\opennet\web 或 src\ui\dist）
$webDir = Join-Path $hostDir "opennet\web"
$uiDist = Join-Path $projectRoot "src\ui\dist"
if (-not (Test-Path $webDir) -and -not (Test-Path $uiDist)) {
    Write-Host "WARNING: 前端未构建，启动后浏览器会显示空白" -ForegroundColor Yellow
    Write-Host "请先运行: .\build.ps1" -ForegroundColor Yellow
    Write-Host ""
}

# 关闭系统代理（避免上次 OpenNet 异常退出后代理设置残留）
try {
    $proxyPath = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings"
    Set-ItemProperty -Path $proxyPath -Name ProxyEnable -Value 0 -Type DWord -ErrorAction Stop
    Write-Host "[OK] 系统代理已关闭（避免残留）" -ForegroundColor Gray
} catch {
    # 忽略权限错误
}

Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  Starting OpenNet..." -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  Directory: $hostDir" -ForegroundColor Gray
Write-Host "  URL:       http://127.0.0.1:18901" -ForegroundColor Gray
Write-Host "  Args:      $args" -ForegroundColor Gray
Write-Host ""

Set-Location $hostDir
python -m opennet @args
