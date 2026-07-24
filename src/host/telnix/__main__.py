"""Telnix 入口：启动代理 + FastAPI + 自动开浏览器 + 设置系统代理。

启动流程：
1. 初始化 SQLite
2. 初始化 SSLBumpManager（生成根证书如果不存在）
3. 启动代理服务器（127.0.0.1:8888）
4. 启动 FastAPI（127.0.0.1:18901）
5. 设置系统代理为 127.0.0.1:8888
6. 注册 atexit + 控制台信号恢复代理
7. 自动打开浏览器（除非 --no-browser）

启动参数：
  python -m telnix                  正常启动（自动开浏览器）
  python -m telnix --no-browser     不开浏览器（agent 自动化场景用）

端口说明：
  - API 端口 18901（注意：18899/18900 在本机被 Windows 动态端口保留，
    bind 会报 WSAEACCES，所以用 18901）
  - 代理端口 8888（系统代理指向此端口）
  - CLI 默认连 18901（cli.py:92 与 config.DEFAULT_PORT 保持一致）
"""

import argparse
import atexit
import os
import sys
import threading
import time
import webbrowser

import uvicorn

# 平台判断：Windows 特定模块（winreg/ctypes.windll）只在 Windows 上导入
IS_WINDOWS = sys.platform == "win32"
if IS_WINDOWS:
    import ctypes
    import winreg
else:
    # 非 Windows 平台：winreg/ctypes.windll 不存在，置为 None 占位
    # 调用方在调用前需先判断 IS_WINDOWS
    ctypes = None  # type: ignore[assignment]
    winreg = None  # type: ignore[assignment]

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
# mitmproxy 引擎可选导入：未安装时 MITMPROXY_AVAILABLE=False，导入本身不报错
from .proxy.mitmproxy_engine import MitmproxyEngine, MITMPROXY_AVAILABLE
# asyncio 代理引擎（G 方案）
from .proxy.async_proxy import AsyncProxyServer
from .server import AppState, create_app
from .api import system as system_api

_INTERNET_SETTINGS = (
    r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
)
INTERNET_OPTION_SETTINGS_CHANGED = 39
INTERNET_OPTION_REFRESH = 37


# ---------- 系统代理 ----------

def set_system_proxy(host: str = "127.0.0.1", port: int = 8888):
    """设置 Windows 系统代理（写注册表 + 通知系统刷新）。

    性能优化：设置 ProxyOverride 排除 localhost/127.0.0.1，让浏览器直连本地 API。
    否则 SSE（/api/flows/stream）走代理时会被存储-转发模式缓冲，无法实时推送。

    非 Windows 平台：打印"不支持"日志并跳过（macOS/Linux 系统代理需走 networksetup/gsettings，
    不在此次跨平台支持范围内）。
    """
    if not IS_WINDOWS:
        # 非 Windows 平台不支持自动设置系统代理，跳过不报错
        print("[Telnix] 当前平台不支持自动设置系统代理，请手动配置浏览器/系统代理"
              f"为 {host}:{port}", file=sys.stderr)
        return
    proxy_str = f"{host}:{port}"
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _INTERNET_SETTINGS, 0,
                        winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, "ProxyServer", 0, winreg.REG_SZ, proxy_str)
        winreg.SetValueEx(key, "ProxyEnable", 0, winreg.REG_DWORD, 1)
        # 排除本地地址：浏览器直连 127.0.0.1:18901（API/SSE），不走代理
        # 这样 SSE 流式响应不被代理缓冲，可实时推送新流量到前端
        winreg.SetValueEx(key, "ProxyOverride", 0, winreg.REG_SZ,
                          "localhost;127.0.0.1;<local>")
    _notify_settings_changed()


def clear_system_proxy():
    """清除系统代理。

    非 Windows 平台：无操作（set_system_proxy 在非 Windows 上也不写注册表）。
    """
    if not IS_WINDOWS:
        return
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _INTERNET_SETTINGS, 0,
                            winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, "ProxyEnable", 0, winreg.REG_DWORD, 0)
        _notify_settings_changed()
    except OSError:
        pass


def _notify_settings_changed():
    """通知系统代理设置已改变，让应用立即生效。仅 Windows 有效。"""
    if not IS_WINDOWS:
        return
    try:
        wininet = ctypes.windll.wininet
        wininet.InternetSetOptionW(0, INTERNET_OPTION_SETTINGS_CHANGED, 0, 0)
        wininet.InternetSetOptionW(0, INTERNET_OPTION_REFRESH, 0, 0)
    except Exception:  # noqa: BLE001
        pass


# ---------- 看门狗 ----------

def setup_watchdog():
    """注册看门狗：进程退出时自动恢复代理设置。

    覆盖三种退出场景：
    - 正常退出（sys.exit / main 返回）：atexit 触发 clear_system_proxy
    - Ctrl+C / Ctrl+Break：SetConsoleCtrlHandler 同步清代理 + os._exit
    - 关闭控制台窗口（CTRL_CLOSE_EVENT）：同上，Windows 只给 ~5 秒，
      必须在 handler 里同步清完再退出，否则 winreg 调用会被强杀导致代理残留

    非 Windows 平台：仅注册 atexit（clear_system_proxy 内部会判断平台跳过），
    不注册 SetConsoleCtrlHandler（Windows 专属 API）。
    """
    atexit.register(clear_system_proxy)

    if not IS_WINDOWS:
        # 非 Windows 平台：无需注册 Windows 控制台信号处理器
        return

    @ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_uint)
    def _handler(ctrl_type):
        # CTRL_C_EVENT=0, CTRL_BREAK_EVENT=1, CTRL_CLOSE_EVENT=2,
        # CTRL_LOGOFF_EVENT=5, CTRL_SHUTDOWN_EVENT=6
        try:
            clear_system_proxy()
        except Exception:  # noqa: BLE001
            pass
        # 同步清完代理后立即退出，不依赖 atexit / finally（避免被 Windows 强杀）
        os._exit(0)

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
    """重启服务：先清代理 + 启动新进程 + 等待端口释放 + 退出当前进程。

    Windows 上的坑（已踩过的）：
    1. os.execv 会让新进程继承 socket handle，导致端口 bind 失败
    2. subprocess.Popen + CREATE_NEW_CONSOLE：实测在某些环境下新进程仍会继承
       部分 handle（即使 close_fds=True），导致 uvicorn bind 端口失败，
       表现为「点击重启后服务起不来、浏览器直接断开」
    3. admin 重启用 ShellExecuteW('runas') 一直工作正常，说明 ShellExecuteW
       是更可靠的方式（创建完全独立的进程，不继承父进程任何 handle）

    最终方案：普通重启也用 ShellExecuteW（'open' verb，无 UAC 提权），
    与 admin 重启同机制，仅 verb 不同。这样保证：
    - 新进程是完全独立的（不继承 socket handle）
    - 新进程有自己的工作目录
    - 旧进程退出后新进程继续运行
    """
    import time
    print("[Telnix] 正在重启服务...")
    try:
        clear_system_proxy()
    except Exception:  # noqa: BLE001
        pass
    # 计算新进程启动参数（与 _do_shell_elevate 一致，仅 verb 不同）
    if getattr(sys, 'frozen', False):
        # PyInstaller 打包：直接运行 exe
        exe = sys.executable
        params = ''
    else:
        exe = sys.executable  # python.exe
        # 透传启动参数（保留 --no-browser 等），过滤非 flag 参数
        argv_extra = [a for a in sys.argv[1:] if a.startswith('-')]
        params = '-m telnix'
        if argv_extra:
            params += ' ' + ' '.join(argv_extra)
    # cwd：telnix 包的父目录（即 src/host/），让 `python -m telnix` 能找到包
    cwd = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if IS_WINDOWS:
        try:
            import ctypes
            # 'open' verb：普通启动（无 UAC）；与 'runas' 相比仅权限不同
            # 返回值 > 32 表示成功；<= 32 表示错误码
            ret = ctypes.windll.shell32.ShellExecuteW(
                None, 'open', exe, params, cwd, 1  # SW_SHOWNORMAL
            )
            if ret <= 32:
                print(f"[Telnix] ShellExecuteW 启动失败，返回码 {ret}",
                      file=sys.stderr)
                os._exit(1)
        except Exception as e:  # noqa: BLE001
            print(f"[Telnix] 启动新进程失败: {e}", file=sys.stderr)
            os._exit(1)
    else:
        # 非 Windows：用 subprocess.Popen 启动（无 ShellExecuteW）
        import subprocess
        try:
            argv = [exe, "-m", "telnix"] + [a for a in sys.argv[1:] if a.startswith('-')]
            subprocess.Popen(
                argv,
                close_fds=True,
                cwd=cwd,
                start_new_session=True,  # 创建新会话，脱离父进程
            )
        except Exception as e:  # noqa: BLE001
            print(f"[Telnix] 启动新进程失败: {e}", file=sys.stderr)
            os._exit(1)
    # 等待新进程启动并 bind 端口（避免端口抢占导致浏览器断连）
    time.sleep(1.5)
    # 当前进程退出，socket 被 OS 回收
    # 用 os._exit 跳过 atexit（已手动清代理，避免 atexit 卡住）
    os._exit(0)


def _do_quit():
    """退出 Telnix：清代理 + 停服务 + sys.exit。"""
    print("[Telnix] 正在退出...")
    try:
        clear_system_proxy()
    except Exception:  # noqa: BLE001
        pass
    # 立即退出（atexit 会触发 clear_system_proxy，但保险起见先调一次）
    os._exit(0)


def main():
    # 解析启动参数：--no-browser 用于 agent 自动化场景，不自动开浏览器
    parser = argparse.ArgumentParser(
        prog="python -m telnix",
        description="Telnix 抓包工具",
        add_help=True,
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="不自动打开浏览器（agent / 自动化场景使用，避免影响用户）",
    )
    args, _ = parser.parse_known_args()
    no_browser = args.no_browser or os.environ.get("TELNIX_NO_BROWSER") == "1"

    init_db()

    # 启动时无条件清理残留系统代理（防止上次崩溃/强杀后代理残留导致全网瘫痪）
    try:
        clear_system_proxy()
    except Exception:  # noqa: BLE001
        pass

    # SSL bump 管理（生成根证书）
    ssl_bump = SSLBumpManager(get_cert_dir())

    # 代理服务器：根据 settings 的 proxy_engine 选择引擎
    # - builtin：内置线程代理（默认，零依赖，稳定）
    # - async：asyncio 代理（G 方案，高并发无 GIL 瓶颈，实验性）
    # - mitmproxy：mitmproxy 引擎（H 方案，可选依赖 ~50MB，未安装时回退到 builtin）
    from . import settings_store
    proxy_engine = settings_store.get_setting("proxy_engine", "builtin")
    proxy = None
    if proxy_engine == "mitmproxy":
        if not MITMPROXY_AVAILABLE:
            print("[Telnix] proxy_engine=mitmproxy 但 mitmproxy 未安装，"
                  "回退到内置引擎。可执行 pip install mitmproxy 启用。")
        else:
            try:
                proxy = MitmproxyEngine(
                    host=get_proxy_host(),
                    port=get_proxy_port(),
                    ssl_bump=ssl_bump,
                )
                print("[Telnix] 使用 mitmproxy 引擎（proxy_engine=mitmproxy）")
            except Exception as e:  # noqa: BLE001
                print(f"[Telnix] mitmproxy 引擎初始化失败: {e}，回退到内置引擎")
                proxy = None
    elif proxy_engine == "async":
        try:
            proxy = AsyncProxyServer(
                host=get_proxy_host(),
                port=get_proxy_port(),
                ssl_bump=ssl_bump,
            )
            print("[Telnix] 使用 asyncio 引擎（proxy_engine=async）")
        except Exception as e:  # noqa: BLE001
            print(f"[Telnix] asyncio 引擎初始化失败: {e}，回退到内置引擎")
            proxy = None
    if proxy is None:
        proxy = ProxyServer(
            host=get_proxy_host(),
            port=get_proxy_port(),
            ssl_bump=ssl_bump,
        )
        if proxy_engine == "mitmproxy" and MITMPROXY_AVAILABLE:
            print("[Telnix] 已回退到内置线程引擎（builtin）")
        elif proxy_engine == "async":
            print("[Telnix] 已回退到内置线程引擎（builtin）")
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
    print(f"[Telnix] 进程伪装名: {get_masquerade_name()}")
    print(f"[Telnix] 代理地址: {get_proxy_host()}:{get_proxy_port()}")
    print(f"[Telnix] Web 地址: {browser_url}" + (f"（监听 {host}，局域网可访问）" if host == "0.0.0.0" else ""))
    print(f"[Telnix] 根证书: {ssl_bump.root_cert_path}")
    print(f"[Telnix] 根证书已安装: {proxy.cert_installed}")
    if no_browser:
        print("[Telnix] --no-browser 模式：不自动打开浏览器（agent 自动化场景）")
    else:
        _open_browser_later(browser_url)

    try:
        uvicorn.run(app, host=host, port=port, log_level="info",
                    access_log=False)
    finally:
        proxy.stop()
        clear_system_proxy()


if __name__ == "__main__":
    main()
