# OpenNet Release 构建脚本
# Usage: .\build-release.ps1
#
# 产出：dist\opennet_host\opennet_host.exe（onedir 模式，含 _internal/）
# 性能优先：onedir 启动快（无需解压临时目录），适合日常使用
#
# 前置条件：
#   1. Python 3.10+ 已安装 opennet 包（pip install -e src\host）
#   2. Node.js 18+ 已安装前端依赖（cd src\ui && npm install）
#   3. PyInstaller 已安装（pip install pyinstaller）

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path $PSScriptRoot).Path
$hostDir = Join-Path $projectRoot "src\host"
$uiDir = Join-Path $projectRoot "src\ui"

Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  OpenNet Release Builder" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

# ---------- 1. 构建前端 ----------
Write-Host "[1/3] Building frontend..." -ForegroundColor Yellow
# 直接用 npx vite build（绕过 npm.ps1 包装脚本的 stderr 误报问题）
$env:UV_THREADPOOL_SIZE = '1'
$env:GOMAXPROCS = '1'
Set-Location $uiDir
# 先清理 dist
if (Test-Path "dist") { Remove-Item -Recurse -Force dist }
# vue-tsc 类型检查
npx vue-tsc --noEmit 2>&1 | Out-Host
if ($LASTEXITCODE -ne 0) {
    Write-Host "  WARNING: vue-tsc 类型检查失败，继续构建" -ForegroundColor Yellow
}
# vite build
npx vite build 2>&1 | Out-Host
if ($LASTEXITCODE -ne 0) {
    Write-Host "  ERROR: vite build 失败" -ForegroundColor Red
    exit 1
}
$uiDist = Join-Path $uiDir "dist"
if (-not (Test-Path (Join-Path $uiDist "index.html"))) {
    Write-Host "  ERROR: 前端 dist/index.html 不存在" -ForegroundColor Red
    exit 1
}
Write-Host "  Frontend built: $uiDist" -ForegroundColor Green

# ---------- 2. 清理旧产物 ----------
Write-Host ""
Write-Host "[2/3] Cleaning old build artifacts..." -ForegroundColor Yellow
$buildDir = Join-Path $hostDir "build"
$distDir = Join-Path $hostDir "dist"
if (Test-Path $buildDir) { Remove-Item -Recurse -Force $buildDir }
if (Test-Path $distDir) { Remove-Item -Recurse -Force $distDir }
Write-Host "  Cleaned" -ForegroundColor Green

# ---------- 3. PyInstaller 打包 ----------
Write-Host ""
Write-Host "[3/3] Running PyInstaller..." -ForegroundColor Yellow
Set-Location $hostDir
pyinstaller opennet_host.spec --noconfirm 2>&1 | Out-Host
if ($LASTEXITCODE -ne 0) {
    Write-Host "  ERROR: PyInstaller 打包失败" -ForegroundColor Red
    exit 1
}

$exePath = Join-Path $distDir "opennet_host\opennet_host.exe"
if (-not (Test-Path $exePath)) {
    Write-Host "  ERROR: exe 未生成: $exePath" -ForegroundColor Red
    exit 1
}

# ---------- 统计 ----------
$distSize = (Get-ChildItem (Join-Path $distDir "opennet_host") -Recurse | Measure-Object -Property Length -Sum).Sum
$distSizeMB = [math]::Round($distSize / 1MB, 2)
$fileCount = (Get-ChildItem (Join-Path $distDir "opennet_host") -Recurse -File | Measure-Object).Count

Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  Build complete!" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ('  Output:  ' + $exePath) -ForegroundColor White
Write-Host ('  Size:    ' + $distSizeMB + ' MB / ' + $fileCount + ' files') -ForegroundColor White
Write-Host ""
Write-Host "Usage:" -ForegroundColor Gray
Write-Host "  cd dist\opennet_host" -ForegroundColor Gray
Write-Host "  .\opennet_host.exe                # start (auto open browser)" -ForegroundColor Gray
Write-Host "  .\opennet_host.exe --no-browser   # no browser" -ForegroundColor Gray
Write-Host ""
Write-Host "Note: TCP/UDP capture requires admin, right-click exe -> Run as admin" -ForegroundColor Yellow
Write-Host ""

# ---------- zip ----------
$zipPath = Join-Path $distDir "opennet_host.zip"
if (Test-Path $zipPath) { Remove-Item -Force $zipPath }
Write-Host "Creating zip archive..." -ForegroundColor Yellow
Compress-Archive -Path (Join-Path $distDir "opennet_host") -DestinationPath $zipPath -CompressionLevel Optimal
$zipSizeMB = [math]::Round((Get-Item $zipPath).Length / 1MB, 2)
Write-Host ('  Zip: ' + $zipPath + ' / ' + $zipSizeMB + ' MB') -ForegroundColor Green
Write-Host ""
