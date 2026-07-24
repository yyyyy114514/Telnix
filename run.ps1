# Telnix 一键运行脚本
# Usage: .\run.ps1 [args...]
#
# 传递给 python -m telnix 的参数：
#   .\run.ps1                  # 默认（自动开浏览器）
#   .\run.ps1 --no-browser     # 不开浏览器
#   .\run.ps1 --port 18902     # 自定义端口

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path $PSScriptRoot).Path
$hostDir = Join-Path $projectRoot "src\host"

# ---------- 查找系统 Python（避免用到 TRAE 内置 Python）----------
function Find-SystemPython {
    # 1. py launcher（官方 Windows Python 启动器，指向系统 Python）
    $pyLauncher = Get-Command py -ErrorAction SilentlyContinue
    if ($pyLauncher) {
        try {
            $exe = & py -3 -c "import sys; print(sys.executable)" 2>$null
            if ($exe -and $exe -notmatch 'TRAE') {
                return $exe.Trim()
            }
        } catch { }
    }
    # 2. 搜索常见系统安装路径
    $candidates = @()
    $localApp = [Environment]::GetEnvironmentVariable('LOCALAPPDATA')
    if ($localApp) {
        $candidates += Get-ChildItem "$localApp\Programs\Python\Python3*\python.exe" -ErrorAction SilentlyContinue |
            Where-Object { $_.FullName -notmatch 'TRAE' } |
            Sort-Object Name -Descending |
            Select-Object -ExpandProperty FullName
    }
    $candidates += Get-ChildItem "C:\Python3*\python.exe" -ErrorAction SilentlyContinue |
        Where-Object { $_.FullName -notmatch 'TRAE' } |
        Select-Object -ExpandProperty FullName
    $candidates += Get-ChildItem "C:\Program Files\Python3*\python.exe" -ErrorAction SilentlyContinue |
        Where-Object { $_.FullName -notmatch 'TRAE' } |
        Select-Object -ExpandProperty FullName
    if ($candidates.Count -gt 0) {
        return $candidates[0]
    }
    # 3. 兜底：PATH 中的 python（可能不是系统 Python）
    return 'python'
}

$pythonExe = Find-SystemPython
Write-Host "[Python] $pythonExe" -ForegroundColor Gray

# 检查后端是否已安装
$telnixPkg = Join-Path $hostDir "telnix\__init__.py"
if (-not (Test-Path $telnixPkg)) {
    Write-Host "ERROR: telnix 包未找到: $telnixPkg" -ForegroundColor Red
    Write-Host "请先运行: .\install.ps1" -ForegroundColor Yellow
    exit 1
}

# 检查前端是否已构建（src\host\telnix\web 或 src\ui\dist）
$webDir = Join-Path $hostDir "telnix\web"
$uiDist = Join-Path $projectRoot "src\ui\dist"
if (-not (Test-Path $webDir) -and -not (Test-Path $uiDist)) {
    Write-Host "WARNING: 前端未构建，启动后浏览器会显示空白" -ForegroundColor Yellow
    Write-Host "请先运行: .\build.ps1" -ForegroundColor Yellow
    Write-Host ""
}

# ---------- 关闭残留进程（占用 18901 / 8888 端口）----------
foreach ($port in @(18901, 8888)) {
    try {
        $conns = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
        foreach ($conn in $conns) {
            $pid = $conn.OwningProcess
            if ($pid -and $pid -ne 0) {
                try {
                    $proc = Get-Process -Id $pid -ErrorAction SilentlyContinue
                    $procName = if ($proc) { $proc.ProcessName } else { "unknown" }
                    Stop-Process -Id $pid -Force -ErrorAction SilentlyContinue
                    Write-Host "[OK] 已关闭占用端口 $port 的残留进程: $procName (PID $pid)" -ForegroundColor Gray
                } catch { }
            }
        }
    } catch { }
}

# 关闭系统代理（避免上次 Telnix 异常退出后代理设置残留）
try {
    $proxyPath = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings"
    Set-ItemProperty -Path $proxyPath -Name ProxyEnable -Value 0 -Type DWord -ErrorAction Stop
    Write-Host "[OK] 系统代理已关闭（避免残留）" -ForegroundColor Gray
} catch {
    # 忽略权限错误
}

Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  Starting Telnix..." -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  Directory: $hostDir" -ForegroundColor Gray
Write-Host "  URL:       http://127.0.0.1:18901" -ForegroundColor Gray
Write-Host "  Args:      $args" -ForegroundColor Gray
Write-Host ""

Set-Location $hostDir
& $pythonExe -m telnix @args
