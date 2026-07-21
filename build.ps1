# OpenNet Build Script
# Usage: .\build.ps1

$projectPath = (Resolve-Path $PSScriptRoot).Path
$uiPath = Join-Path $projectPath 'src\ui'
$distPath = Join-Path $uiPath 'dist'

Write-Host "========================================" -ForegroundColor Cyan
Write-Host "OpenNet Build Script" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

# Key: single-thread esbuild (Go runtime) to avoid Windows deadlock
$env:UV_THREADPOOL_SIZE = '1'
$env:GOMAXPROCS = '1'
Write-Host "[0/4] Set UV_THREADPOOL_SIZE=1 + GOMAXPROCS=1" -ForegroundColor Green
Write-Host ""

# 1. Kill stale Node/Python processes
Write-Host "[1/4] Killing stale processes..." -ForegroundColor Yellow

$targets = Get-WmiObject Win32_Process | Where-Object {
    ($_.Name -match 'node\.exe|python\.exe') -and
    ($_.CommandLine -like "*$projectPath*" -or $_.CommandLine -like "*opennet*")
}

if ($targets) {
    foreach ($p in $targets) {
        Write-Host "  Kill: $($p.Name) (PID: $($p.ProcessId))" -ForegroundColor DarkYellow
        try {
            Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop
        } catch {
            Write-Host "  Failed PID $($p.ProcessId): $_" -ForegroundColor Red
        }
    }
    Start-Sleep -Milliseconds 500
    Write-Host "  Killed $($targets.Count) process(es)" -ForegroundColor Green
} else {
    Write-Host "  No stale processes found" -ForegroundColor Green
}

# 2. Clean dist
Write-Host ""
Write-Host "[2/4] Cleaning dist..." -ForegroundColor Yellow
if (Test-Path $distPath) {
    Remove-Item -Recurse -Force $distPath -ErrorAction SilentlyContinue
    Write-Host "  Deleted dist" -ForegroundColor Green
} else {
    Write-Host "  dist not found, skip" -ForegroundColor Green
}

# 3. Clean Vite cache
Write-Host ""
Write-Host "[3/4] Cleaning Vite cache..." -ForegroundColor Yellow
$viteCache = Join-Path $uiPath 'node_modules\.vite'
if (Test-Path $viteCache) {
    Remove-Item -Recurse -Force $viteCache -ErrorAction SilentlyContinue
    Write-Host "  Deleted .vite cache" -ForegroundColor Green
} else {
    Write-Host "  .vite cache not found, skip" -ForegroundColor Green
}

# 4. Build
Write-Host ""
Write-Host "[4/4] Building..." -ForegroundColor Yellow
Write-Host "  npm run build" -ForegroundColor Gray
Write-Host ""
Write-Host "----------------------------------------" -ForegroundColor Cyan

Set-Location $uiPath
npm run build

$exitCode = $LASTEXITCODE
Write-Host "----------------------------------------" -ForegroundColor Cyan
Write-Host ""

if ($exitCode -eq 0) {
    Write-Host "Build OK!" -ForegroundColor Green
    $distFiles = Get-ChildItem $distPath -Recurse -File | Measure-Object
    Write-Host "dist: $($distFiles.Count) file(s)" -ForegroundColor Gray
} else {
    Write-Host "Build FAILED (exit: $exitCode)" -ForegroundColor Red
}

Write-Host ""
Write-Host "Press Enter to exit..." -ForegroundColor Gray
Read-Host | Out-Null
