"""OpenNet 入口：启动代理 + FastAPI + 自动开浏览器 + 设置系统代理。

启动流程：
1. 初始化 SQLite
2. 初始化 SSLBumpManager（生成根证书如果不存在）
3. 启动代理服务器（127.0.0.1:8888）
4. 启动 FastAPI（127.0.0.1:18901）
5. 设置系统代理为 127.0.0.1:8888
6. 注册 atexit + 控制台信号恢复代理
7. 自动打开浏览器（除非 --no-browser）

启动参数：
  python -m opennet                  正常启动（自动开浏览器）
  python -m opennet --no-browser     不开浏览器（agent 自动化场景用）

端口说明：
  - API 端口 18901（注意：18899/18900 在本机被 Windows 动态端口保留，
    bind 会报 WSAEACCES，所以用 18901）
  - 代理端口 8888（系统代理指向此端口）
  - CLI 默认连 18901（cli.py:92 与 config.DEFAULT_PORT 保持一致）
"""

import argparse
import atexit
import ctypes
import os
import sys
import threading
import time
import webbrowser

import uvicorn
import winreg

from .config import (
    get_cert_dir,
    get_host,
    get_masquerade_name,
    get_port,
    get_proxy_host,
    get_proxy_port,
)
from .db import init_db
from .proxy.server import ProxyServer
from .proxy.ssl_bump import SSLBumpManager
from .server import AppState, create_app
from .api import system as system_api

_INTERNET_SETTINGS = (
    r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
)
INTERNET_OPTION_SETTINGS_CHANGED = 39
INTERNET_OPTION_REFRESH = 37


# ---------- 系统代理 ----------

def set_system_proxy(host: str = "127.0.0.1", port: int = 8888):
    """设置 Windows 系统代理（写注册表 + 通知系统刷新）。"""
    proxy_str = f"{host}:{port}"
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _INTERNET_SETTINGS, 0,
                        winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, "ProxyServer", 0, winreg.REG_SZ, proxy_str)
        winreg.SetValueEx(key, "ProxyEnable", 0, winreg.REG_DWORD, 1)
    _notify_settings_changed()


def clear_system_proxy():
    """清除系统代理。"""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _INTERNET_SETTINGS, 0,
                            winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, "ProxyEnable", 0, winreg.REG_DWORD, 0)
        _notify_settings_changed()
    except OSError:
        pass


def _notify_settings_changed():
    """通知系统代理设置已改变，让应用立即生效。"""
    try:
        wininet = ctypes.windll.wininet
        wininet.InternetSetOptionW(0, INTERNET_OPTION_SETTINGS_CHANGED, 0, 0)
        wininet.InternetSetOptionW(0, INTERNET_OPTION_REFRESH, 0, 0)
    except Exception:  # noqa: BLE001
        pass


# ---------- 看门狗 ----------

def setup_watchdog():
    """注册看门狗：进程退出时自动恢复代理设置。

    atexit 覆盖正常退出；SetConsoleCtrlHandler 覆盖 Ctrl+C / 关闭控制台。
    """
    atexit.register(clear_system_proxy)

    @ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_uint)
    def _handler(ctrl_type):
        # CTRL_C_EVENT=0, CTRL_BREAK_EVENT=1, CTRL_CLOSE_EVENT=2,
        # CTRL_LOGOFF_EVENT=5, CTRL_SHUTDOWN_EVENT=6
        clear_system_proxy()
        return False  # 让默认处理继续

    try:
        ctypes.windll.kernel32.SetConsoleCtrlHandler(_handler, True)
    except Exception:  # noqa: BLE001
        pass


# ---------- 浏览器 ----------

def _open_browser_later(url: str, delay: float = 1.5):
    """延迟打开浏览器，等服务起来。"""

    def _open():
        time.sleep(delay)
        webbrowser.open(url)

    threading.Thread(target=_open, daemon=True).start()


# ---------- 主入口 ----------

def _do_restart():
    """重启服务：先清代理 + 启动新进程 + 退出当前进程。

    Windows 上 os.execv 会让新进程继承当前进程的 socket handle，
    导致新进程无法 bind 18901/8888 端口（端口被占用，ERR_CONNECTION_REFUSED）。
    改用 subprocess.Popen + close_fds=True 避免继承 socket。
    """
    import subprocess
    print("[OpenNet] 正在重启服务...")
    try:
        clear_system_proxy()
    except Exception:  # noqa: BLE001
        pass
    python = sys.executable
    argv = [python, "-m", "opennet"] + sys.argv[1:]
    # close_fds=True 确保新进程不继承当前 socket（关键！）
    # stdout/stderr 默认继承，新进程日志输出到当前终端
    try:
        subprocess.Popen(argv, close_fds=True)
    except Exception as e:  # noqa: BLE001
        print(f"[OpenNet] 启动新进程失败: {e}", file=sys.stderr)
        os._exit(1)
    # 当前进程立即退出，socket 被 OS 回收
    # 用 os._exit 跳过 atexit（已手动清代理，避免 atexit 卡住）
    os._exit(0)


def _do_quit():
    """退出 OpenNet：清代理 + 停服务 + sys.exit。"""
    print("[OpenNet] 正在退出...")
    try:
        clear_system_proxy()
    except Exception:  # noqa: BLE001
        pass
    # 立即退出（atexit 会触发 clear_system_proxy，但保险起见先调一次）
    os._exit(0)


def main():
    # 解析启动参数：--no-browser 用于 agent 自动化场景，不自动开浏览器
    parser = argparse.ArgumentParser(
        prog="python -m opennet",
        description="OpenNet 抓包工具",
        add_help=True,
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="不自动打开浏览器（agent / 自动化场景使用，避免影响用户）",
    )
    args, _ = parser.parse_known_args()
    no_browser = args.no_browser or os.environ.get("OPENNET_NO_BROWSER") == "1"

    init_db()

    # 启动时无条件清理残留系统代理（防止上次崩溃/强杀后代理残留导致全网瘫痪）
    try:
        clear_system_proxy()
    except Exception:  # noqa: BLE001
        pass

    # SSL bump 管理（生成根证书）
    ssl_bump = SSLBumpManager(get_cert_dir())

    # 代理服务器
    proxy = ProxyServer(
        host=get_proxy_host(),
        port=get_proxy_port(),
        ssl_bump=ssl_bump,
    )
    proxy.refresh_cert_status()
    proxy.start()

    # 全局状态
    state = AppState()
    state.proxy = proxy
    app = create_app(state)

    # 注入系统控制钩子（重启/退出/清代理/开代理）# 系统控制钩子
    def _enable_sys_proxy():
        # 系统代理始终用 127.0.0.1（电脑本机访问代理服务器），
        # 即使代理监听 0.0.0.0（手机/局域网设备可连）也不影响电脑本机。
        # 之前用 get_proxy_host() 会在开启"允许局域网设备连接"后写成 0.0.0.0:8888，
        # Windows 客户端不能连 0.0.0.0，导致电脑无法上网。
        set_system_proxy("127.0.0.1", get_proxy_port())
        system_api.mark_proxy_on(True)

    def _clear_sys_proxy():
        clear_system_proxy()
        system_api.mark_proxy_on(False)

    system_api.set_hooks(
        restart=_do_restart,
        quit=_do_quit,
        clear_proxy=_clear_sys_proxy,
        enable_proxy=_enable_sys_proxy,
    )

    # 系统代理：启动时不自动开启（避免网络中断），由 capture start/stop 控制
    # 仅注册看门狗（退出时确保清代理，防止残留）
    system_api.mark_proxy_on(False)
    setup_watchdog()
    # 启动代理状态监控线程：检测非主动失去代理并通知前端弹窗
    system_api.start_proxy_monitor()

    host = get_host()
    port = get_port()
    # 浏览器始终用 127.0.0.1 打开（0.0.0.0 在 Windows 上浏览器无法访问）
    # 即使 API 监听 0.0.0.0（允许局域网设备连接），本机也通过 127.0.0.1 访问
    browser_url = f"http://127.0.0.1:{port}"
    print(f"[OpenNet] 进程伪装名: {get_masquerade_name()}")
    print(f"[OpenNet] 代理地址: {get_proxy_host()}:{get_proxy_port()}")
    print(f"[OpenNet] Web 地址: {browser_url}" + (f"（监听 {host}，局域网可访问）" if host == "0.0.0.0" else ""))
    print(f"[OpenNet] 根证书: {ssl_bump.root_cert_path}")
    print(f"[OpenNet] 根证书已安装: {proxy.cert_installed}")
    if no_browser:
        print("[OpenNet] --no-browser 模式：不自动打开浏览器（agent 自动化场景）")
    else:
        _open_browser_later(browser_url)

    try:
        uvicorn.run(app, host=host, port=port, log_level="info")
    finally:
        proxy.stop()
        clear_system_proxy()


if __name__ == "__main__":
    main()
