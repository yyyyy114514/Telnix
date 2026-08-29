#!/usr/bin/env bash
# Telnix 启动脚本（macOS / Linux）
# Usage: ./start.sh [args...]
#
# 等价于 Windows 的 run.ps1，启动后端服务。
# 传递给 `python3 -m telnix` 的参数：
#   ./start.sh                  # 默认（自动开浏览器）
#   ./start.sh --no-browser     # 不开浏览器（agent 自动化场景）
#   ./start.sh --port 18902     # 自定义端口
#
# 启动后访问 http://127.0.0.1:18901 即可使用。
# 退出时按 Ctrl+C，后端会自动清理（mac/linux 无系统代理可清理，仅停止服务）。

set -e

# 脚本所在目录（用于定位 src/host）
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$SCRIPT_DIR"
HOST_DIR="$PROJECT_ROOT/src/host"

# ---------- 颜色输出 ----------
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

# ---------- 检查后端包是否就位 ----------
TELNIX_INIT="$HOST_DIR/telnix/__init__.py"
if [ ! -f "$TELNIX_INIT" ]; then
    echo "${COLOR_RED}ERROR: telnix 包未找到: $TELNIX_INIT${COLOR_RESET}"
    echo "${COLOR_YELLOW}请先安装依赖：${COLOR_RESET}"
    echo "  ./scripts/install-deps-linux.sh   # Linux"
    echo "  ./scripts/install-deps-mac.sh     # macOS"
    echo "  或手动：cd src/host && pip install -e ."
    exit 1
fi

# ---------- 检查前端是否已构建 ----------
WEB_DIR="$HOST_DIR/telnix/web"
UI_DIST="$PROJECT_ROOT/src/ui/dist"
if [ ! -d "$WEB_DIR" ] && [ ! -d "$UI_DIST" ]; then
    echo "${COLOR_YELLOW}WARNING: 前端未构建，启动后浏览器会显示空白${COLOR_RESET}"
    echo "${COLOR_YELLOW}请先运行: ./build.sh${COLOR_RESET}"
    echo ""
fi

# ---------- 关闭占用 18901 / 8888 端口的残留进程 ----------
for port in 18901 8888; do
    pids="$(lsof -ti tcp:"$port" -sTCP:LISTEN 2>/dev/null || true)"
    if [ -n "$pids" ]; then
        for p in $pids; do
            if [ "$p" = "$$" ]; then
                continue
            fi
            proc_name="$(ps -p "$p" -o comm= 2>/dev/null || echo 'unknown')"
            kill -9 "$p" 2>/dev/null || true
            echo "${COLOR_GRAY}[OK] 已关闭占用端口 $port 的残留进程: $proc_name (PID $p)${COLOR_RESET}"
        done
    fi
done

# ---------- 平台提示 ----------
OS_NAME="$(uname -s)"
if [ "$OS_NAME" = "Darwin" ]; then
    PLATFORM_LABEL="macOS"
elif [ "$OS_NAME" = "Linux" ]; then
    PLATFORM_LABEL="Linux"
else
    PLATFORM_LABEL="$OS_NAME（非 Windows / macOS / Linux，部分功能可能不可用）"
fi

echo ""
echo "${COLOR_CYAN}========================================${COLOR_RESET}"
echo "${COLOR_CYAN}  Starting Telnix... ($PLATFORM_LABEL)${COLOR_RESET}"
echo "${COLOR_CYAN}========================================${COLOR_RESET}"
echo "${COLOR_GRAY}  Directory: $HOST_DIR${COLOR_RESET}"
echo "${COLOR_GRAY}  URL:        http://127.0.0.1:18901${COLOR_RESET}"
echo "${COLOR_GRAY}  Args:       $*${COLOR_RESET}"
echo "${COLOR_GRAY}  注意：当前平台不会自动设置系统代理，请手动配置浏览器/系统代理为 127.0.0.1:8888${COLOR_RESET}"
echo ""

cd "$HOST_DIR"
exec python3 -m telnix "$@"
