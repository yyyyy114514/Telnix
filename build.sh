#!/usr/bin/env bash
# Telnix Build Script (macOS / Linux)
# Usage: ./build.sh
#
# 等价于 Windows 的 build.ps1，构建前端到 src/ui/dist。
# 构建完成后即可用 ./start.sh 启动后端（后端会自动加载 dist 目录）。

set -e

# 脚本所在目录（用于定位项目根 / src/ui）
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$SCRIPT_DIR"
UI_DIR="$PROJECT_ROOT/src/ui"
DIST_DIR="$UI_DIR/dist"

# ---------- 颜色输出（可选，非 tty 自动禁用）----------
if [ -t 1 ]; then
    COLOR_CYAN=$'\033[36m'
    COLOR_GREEN=$'\033[32m'
    COLOR_YELLOW=$'\033[33m'
    COLOR_RED=$'\033[31m'
    COLOR_GRAY=$'\033[90m'
    COLOR_RESET=$'\033[0m'
else
    COLOR_CYAN=""; COLOR_GREEN=""; COLOR_YELLOW=""; COLOR_RED=""; COLOR_GRAY=""; COLOR_RESET=""
fi

echo "${COLOR_CYAN}========================================${COLOR_RESET}"
echo "${COLOR_CYAN}Telnix Build Script (macOS / Linux)${COLOR_RESET}"
echo "${COLOR_CYAN}========================================${COLOR_RESET}"
echo ""

# ---------- [0/4] 检测 python3 / npm ----------
echo "${COLOR_GREEN}[0/4] Checking toolchain...${COLOR_RESET}"

if ! command -v python3 >/dev/null 2>&1; then
    echo "${COLOR_RED}  ERROR: 未找到 python3${COLOR_RESET}"
    echo "  macOS:   brew install python"
    echo "  Debian:  sudo apt install python3"
    echo "  RHEL:    sudo dnf install python3"
    exit 1
fi
PY_VER="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
echo "  python3: $PY_VER ($(command -v python3))"

if ! command -v npm >/dev/null 2>&1; then
    echo "${COLOR_RED}  ERROR: 未找到 npm${COLOR_RESET}"
    echo "  请先安装 Node.js 18+:"
    echo "    macOS:  brew install node"
    echo "    Debian: curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash - && sudo apt install -y nodejs"
    echo "    RHEL:   curl -fsSL https://rpm.nodesource.com/setup_20.x | sudo -E bash - && sudo dnf install -y nodejs"
    echo "    或访问: https://nodejs.org/"
    exit 1
fi
NPM_VER="$(npm --version)"
NODE_VER="$(node --version)"
echo "  node:    $NODE_VER"
echo "  npm:     $NPM_VER"

# Node 18+ 检查
NODE_MAJOR="$(echo "$NODE_VER" | sed 's/^v//' | cut -d. -f1)"
if [ "$NODE_MAJOR" -lt 18 ] 2>/dev/null; then
    echo "${COLOR_YELLOW}  WARNING: Node $NODE_VER 版本过低，建议 18+${COLOR_RESET}"
fi
echo ""

# ---------- [1/4] 关闭占用 18901 / 8888 端口的残留进程 ----------
# mac/linux 用 lsof + kill（等价于 build.ps1 的 Get-WmiObject + Stop-Process）
echo "${COLOR_YELLOW}[1/4] Killing stale processes on port 18901/8888...${COLOR_RESET}"
killed=0
for port in 18901 8888; do
    # -t 仅输出 PID，-sTCP:LISTEN 只看监听状态的连接
    pids="$(lsof -ti tcp:"$port" -sTCP:LISTEN 2>/dev/null || true)"
    if [ -n "$pids" ]; then
        for p in $pids; do
            # 不强杀自己（脚本进程通常不在监听这些端口，保险起见过滤）
            if [ "$p" = "$$" ]; then
                continue
            fi
            echo "  Kill: PID $p (port $port)"
            kill -9 "$p" 2>/dev/null || true
            killed=$((killed + 1))
        done
    fi
done
if [ "$killed" -gt 0 ]; then
    sleep 0.5
    echo "${COLOR_GREEN}  Killed $killed process(es)${COLOR_RESET}"
else
    echo "${COLOR_GREEN}  No stale processes found${COLOR_RESET}"
fi
echo ""

# ---------- [1.5/4] 检查 node_modules ----------
if [ ! -d "$UI_DIR/node_modules" ]; then
    echo "${COLOR_RED}  ERROR: node_modules not found. Run npm install first:${COLOR_RESET}"
    echo "    cd src/ui && npm install"
    exit 1
fi

# ---------- [2/4] 清理 dist ----------
echo "${COLOR_YELLOW}[2/4] Cleaning dist...${COLOR_RESET}"
if [ -d "$DIST_DIR" ]; then
    rm -rf "$DIST_DIR"
    echo "${COLOR_GREEN}  Deleted dist${COLOR_RESET}"
else
    echo "${COLOR_GREEN}  dist not found, skip${COLOR_RESET}"
fi
echo ""

# ---------- [3/4] 清理 Vite 缓存 ----------
echo "${COLOR_YELLOW}[3/4] Cleaning Vite cache...${COLOR_RESET}"
VITE_CACHE="$UI_DIR/node_modules/.vite"
if [ -d "$VITE_CACHE" ]; then
    rm -rf "$VITE_CACHE"
    echo "${COLOR_GREEN}  Deleted .vite cache${COLOR_RESET}"
else
    echo "${COLOR_GREEN}  .vite cache not found, skip${COLOR_RESET}"
fi
echo ""

# ---------- [4/4] 构建 ----------
echo "${COLOR_YELLOW}[4/4] Building...${COLOR_RESET}"
echo "${COLOR_GRAY}  npm run build${COLOR_RESET}"
echo ""
echo "${COLOR_CYAN}----------------------------------------${COLOR_RESET}"

cd "$UI_DIR"

# Optional type check (non-blocking — type errors won't prevent build)
echo "${COLOR_GRAY}[typecheck] Running vue-tsc (non-blocking)...${COLOR_RESET}"
npm run typecheck >/dev/null 2>&1
if [ $? -eq 0 ]; then
    echo "${COLOR_GREEN}  Type check passed${COLOR_RESET}"
else
    echo "${COLOR_YELLOW}  Type check skipped/failed (build continues)${COLOR_RESET}"
fi
echo ""

# 注意：UV_THREADPOOL_SIZE=1 / GOMAXPROCS=1 是 Windows 专属 workaround
# （esbuild Go runtime 在 Windows 上有死锁 bug），macOS/Linux 无需设置
npm run build
EXIT_CODE=$?

echo "${COLOR_CYAN}----------------------------------------${COLOR_RESET}"
echo ""

if [ "$EXIT_CODE" -eq 0 ]; then
    echo "${COLOR_GREEN}Build OK!${COLOR_RESET}"
    if [ -d "$DIST_DIR" ]; then
        DIST_FILES=$(find "$DIST_DIR" -type f | wc -l | tr -d ' ')
        echo "${COLOR_GRAY}dist: $DIST_FILES file(s)${COLOR_RESET}"
    fi
else
    echo "${COLOR_RED}Build FAILED (exit: $EXIT_CODE)${COLOR_RESET}"
    exit "$EXIT_CODE"
fi
echo ""
echo "${COLOR_GRAY}启动 Telnix：./start.sh${COLOR_RESET}"
