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
from typing import Any

import uvicorn

# 平台判断：Windows 特定模块（winreg/ctypes.windll）只在 Windows 上导入
IS_WINDOWS = sys.platform == "win32"
IS_LINUX = sys.platform.startswith("linux")
IS_MACOS = sys.platform == "darwin"
IS_UNIX = IS_LINUX or IS_MACOS
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
    get_data_dir,
    get_host,
    get_masquerade_name,
    get_port,
    get_proxy_host,
    get_proxy_port,
)
from .db import init_db
from .proxy.server import ProxyServer
from .proxy.ssl_bump import SSLBumpManager
# 性能优化：mitmproxy 引擎延迟导入（import mitmproxy 链路重 ~50MB，
# 启动时 import 会让 main() 慢 1-2s）。改为在 main() 内按需 import。
# 此处只导入轻量的引擎可用性标志函数。
# asyncio 代理引擎（G 方案）也延迟导入（依赖 h2/httpx 等）
from .server import AppState, create_app
from .api import system as system_api
# 跨平台系统代理配置（Windows registry / macOS networksetup / Linux gsettings）
from . import system_proxy


def _check_mitmproxy_available() -> bool:
    """延迟检测 mitmproxy 是否可用（避免启动时 eager import 拖慢启动）。

    在 main() 内首次调用时执行 import，结果缓存到模块级。
    """
    global _mitmproxy_available_cache
    if _mitmproxy_available_cache is None:
        try:
            import mitmproxy  # noqa: F401
            _mitmproxy_available_cache = True
        except Exception:  # noqa: BLE001
            _mitmproxy_available_cache = False
    return _mitmproxy_available_cache


_mitmproxy_available_cache: bool | None = None

# F5 修复：模块级 proxy 引用，供信号处理器 / _do_quit / _do_restart 在 os._exit(0) 前访问。
# proxy 是 main() 局部变量，模块级函数无法直接访问；提升为模块级以便显式调 stop() 释放资源。
# Any 类型：可能是 ProxyServer / MitmproxyEngine / AsyncProxyServer，三者都有 stop() 方法。
_proxy: Any = None

_INTERNET_SETTINGS = (
    r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
)
INTERNET_OPTION_SETTINGS_CHANGED = 39
INTERNET_OPTION_REFRESH = 37


# ---------- 系统代理 ----------
# 跨平台系统代理配置委托给 system_proxy 模块：
# - Windows: 写注册表（ProxyServer/ProxyEnable/ProxyOverride）+ 通知系统刷新
# - macOS: networksetup -setwebproxy/-setsecurewebproxy
# - Linux GNOME: gsettings set org.gnome.system.proxy
# - Linux KDE: kwriteconfig5
# 代理状态标记文件（崩溃恢复用）由 system_proxy 模块管理

# 兼容旧代码：保留 _PROXY_ACTIVE_FLAG / _get_proxy_flag_path / _notify_settings_changed
# 但实际逻辑委托给 system_proxy 模块
_PROXY_ACTIVE_FLAG = None  # 兼容字段，实际由 system_proxy 模块管理


def _get_proxy_flag_path() -> str:
    """代理状态标记文件路径（委托给 system_proxy 模块）。"""
    return system_proxy._get_proxy_flag_path()


def set_system_proxy(host: str = "127.0.0.1", port: int = 8888) -> bool:
    """设置系统代理（跨平台）。

    平台支持：
    - Windows: 写注册表 + 通知系统刷新
    - macOS: networksetup 设置 HTTP/HTTPS 代理
    - Linux GNOME: gsettings 设置 org.gnome.system.proxy
    - Linux KDE: kwriteconfig5 设置 kioslaverc

    性能优化：设置代理排除 localhost/127.0.0.1，让浏览器直连本地 API。
    否则 SSE（/api/flows/stream）走代理时会被存储-转发模式缓冲，无法实时推送。

    返回 True 表示成功，False 表示失败。
    """
    return system_proxy.set_system_proxy(host, port)


def clear_system_proxy() -> bool:
    """清除系统代理（跨平台）。

    返回 True 表示成功，False 表示失败。
    """
    return system_proxy.clear_system_proxy()


def _notify_settings_changed():
    """通知系统代理设置已改变（仅 Windows 有效，委托给 system_proxy 模块）。"""
    # system_proxy.set_system_proxy 内部已调用 _notify_windows_settings_changed
    # 此函数保留用于兼容旧代码调用
    if IS_WINDOWS:
        system_proxy._notify_windows_settings_changed()


# ---------- 看门狗 ----------
# 关键：_console_ctrl_handler 必须放在模块级，避免被 Python 垃圾回收。
# ctypes 的 WINFUNCTYPE 返回的 callback 对象只被 SetConsoleCtrlHandler
# 以原始函数指针形式持有，Python 侧若无引用就会被 GC，导致 Ctrl+C / 关窗口
# 时 handler 已被释放，Windows 调用已释放的函数指针会静默失败（代理不清理）。
_console_ctrl_handler = None
_unix_signal_handler_installed = False


def setup_watchdog():
    """注册看门狗：进程退出时自动恢复代理设置。

    覆盖的退出场景：
    - 正常退出（sys.exit / main 返回）：atexit 触发 clear_system_proxy
    - Windows: Ctrl+C / Ctrl+Break / 关闭控制台窗口 → SetConsoleCtrlHandler
    - Unix: SIGINT (Ctrl+C) / SIGTERM (kill) → signal.signal

    平台差异：
    - Windows: SetConsoleCtrlHandler 同步清代理 + os._exit（Windows 关窗口只给 ~5 秒）
    - Unix: signal.signal 注册 SIGINT/SIGTERM 处理器，清代理后 sys.exit
    """
    global _console_ctrl_handler, _unix_signal_handler_installed
    atexit.register(clear_system_proxy)

    if IS_WINDOWS:
        # Windows: 注册 SetConsoleCtrlHandler
        @ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_uint)
        def _handler(ctrl_type):
            # CTRL_C_EVENT=0, CTRL_BREAK_EVENT=1, CTRL_CLOSE_EVENT=2,
            # CTRL_LOGOFF_EVENT=5, CTRL_SHUTDOWN_EVENT=6
            # 关机/注销时写关机日志，便于事后诊断（标记文件会被 clear_system_proxy 删除，
            # 所以此处额外写一个 .proxy_shutdown.log 记录是否触发了 handler）
            if ctrl_type in (2, 5, 6):
                try:
                    log_path = os.path.join(os.path.dirname(_get_proxy_flag_path()),
                                            ".proxy_shutdown.log")
                    with open(log_path, "w", encoding="utf-8") as f:
                        f.write(f"{time.time()}\nctrl_type={ctrl_type}\n")
                except OSError:
                    pass
            try:
                clear_system_proxy()
            except Exception:  # noqa: BLE001
                pass
            # 同步清完代理后立即退出，不依赖 atexit / finally（避免被 Windows 强杀）
            # F5 修复：os._exit 前显式释放 proxy/WinDivert 资源，避免句柄泄露
            _cleanup_proxy_before_exit()
            os._exit(0)

        # 关键：保存到模块级变量，防止局部变量被 GC 后 ctypes 调用已释放的函数指针
        _console_ctrl_handler = _handler

        try:
            ctypes.windll.kernel32.SetConsoleCtrlHandler(_handler, True)
        except Exception:  # noqa: BLE001
            pass
    else:
        # Unix: 注册 signal 处理器（SIGINT=Ctrl+C, SIGTERM=kill）
        if _unix_signal_handler_installed:
            return
        _unix_signal_handler_installed = True
        import signal

        def _unix_signal_handler(signum, frame):
            try:
                clear_system_proxy()
            except Exception:  # noqa: BLE001
                pass
            # Unix 上 signal handler 中可以直接 sys.exit，会触发 atexit
            # 但为保险起见用 os._exit 避免卡住
            # F5 修复：os._exit 前显式释放 proxy/WinDivert 资源，避免句柄泄露
            _cleanup_proxy_before_exit()
            os._exit(0)

        try:
            signal.signal(signal.SIGINT, _unix_signal_handler)
            signal.signal(signal.SIGTERM, _unix_signal_handler)
        except (ValueError, OSError):
            # 非主线程无法注册 signal handler，忽略
            pass


# ---------- 浏览器 ----------

def _open_browser_later(url: str, delay: float = 1.5):
    """延迟打开浏览器，等服务起来。"""

    def _open():
        time.sleep(delay)
        webbrowser.open(url)

    threading.Thread(target=_open, daemon=True).start()


# ---------- 主入口 ----------

def _cleanup_proxy_before_exit():
    """F5 修复：在 os._exit(0) 前显式释放 proxy / raw_capture / transparent_proxy 资源。

    os._exit(0) 跳过 finally / atexit，导致 socket + ThreadPool + WinDivert 句柄泄露。
    此函数在 4 处 os._exit(0) 前调用，集中清理：
    1. proxy.stop() — 释放 server socket + 连接池（跨平台）
    2. stop_raw_capture() — 释放 WinDivert 句柄 + 工作线程 (Platform: Windows)
    3. stop_transparent_proxy() — 释放 WinDivert 句柄 + NAT 表 + 工作线程 (Platform: Windows)

    所有调用用 try/except 包裹，避免任何异常打断退出流程。三个 stop 都幂等且内部吞异常。
    最坏耗时 ~5-10s（join timeout），Windows 关窗口可能被强杀，但已 close 的句柄仍会被 OS 回收。
    """
    # 1. ProxyServer（可能是 ProxyServer / MitmproxyEngine / AsyncProxyServer，都有 stop()）
    if _proxy is not None:
        stop_fn = getattr(_proxy, 'stop', None)
        if stop_fn is not None:
            try:
                stop_fn()
            except Exception:  # noqa: BLE001
                pass
    # 2. RawCapture（WinDivert 句柄，Platform: Windows）
    try:
        from .proxy.raw_capture import stop_raw_capture
        stop_raw_capture()
    except Exception:  # noqa: BLE001
        pass
    # 3. TransparentProxy（WinDivert 句柄 + NAT 表，Platform: Windows）
    try:
        from .proxy.transparent_proxy import stop_transparent_proxy
        stop_transparent_proxy()
    except Exception:  # noqa: BLE001
        pass


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
    # 先标记代理状态为关闭，避免监控线程在 clear_system_proxy() 与 os._exit()
    # 之间的时间窗内误判"代理丢失"导致前端弹窗
    try:
        from .api import system as system_api
        system_api.mark_proxy_on(False)
    except Exception:  # noqa: BLE001
        pass
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
    # F5 修复：os._exit 前显式释放 proxy/WinDivert 资源，避免句柄泄露
    _cleanup_proxy_before_exit()
    os._exit(0)


def _do_quit():
    """退出 Telnix：清代理 + 停服务 + sys.exit。"""
    print("[Telnix] 正在退出...")
    # 先标记代理状态为关闭，避免监控线程在 clear_system_proxy() 与 os._exit()
    # 之间的时间窗内误判"代理丢失"导致前端弹窗
    try:
        from .api import system as system_api
        system_api.mark_proxy_on(False)
    except Exception:  # noqa: BLE001
        pass
    try:
        clear_system_proxy()
    except Exception:  # noqa: BLE001
        pass
    # 立即退出（atexit 会触发 clear_system_proxy，但保险起见先调一次）
    # F5 修复：os._exit 前显式释放 proxy/WinDivert 资源，避免句柄泄露
    _cleanup_proxy_before_exit()
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

    # 启动时无条件清理残留系统代理（防止上次崩溃/强杀/关机时代理残留导致全网瘫痪）
    # 检查标记文件：若存在说明上次没干净退出（可能被关机强杀），记录诊断信息
    try:
        flag_path = _get_proxy_flag_path()
        if os.path.exists(flag_path):
            # 读取标记内容（设置时间戳）用于诊断
            try:
                with open(flag_path, "r", encoding="utf-8") as f:
                    content = f.read().strip()
                print(f"[Telnix] 检测到上次未清理的系统代理标记：{content}", file=sys.stderr)
                print("[Telnix] 可能上次未正常退出（被关机强杀/崩溃），正在清理...",
                      file=sys.stderr)
            except OSError:
                pass
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
    # 透明代理要求代理监听 0.0.0.0：WinDivert 需把流量重定向到「本机真实 IP」，
    # 仅监听 127.0.0.1 时收不到被重定向到本机 IP 的包（见 transparent_proxy._detect_local_ip）。
    # 开启透明代理时强制 0.0.0.0（系统代理仍用 127.0.0.1 连接，0.0.0.0 监听包含 127.0.0.1，
    # 不受影响；仅暴露端口范围略增，透明代理本就是需要管理员权限的高级功能）。
    proxy_host = get_proxy_host()
    if settings_store.get_setting("transparent_proxy", False):
        proxy_host = "0.0.0.0"
    proxy = None
    if proxy_engine == "mitmproxy":
        if not _check_mitmproxy_available():
            print("[Telnix] proxy_engine=mitmproxy 但 mitmproxy 未安装，"
                  "回退到内置引擎。可执行 pip install mitmproxy 启用。")
        else:
            try:
                # 延迟导入：仅在选择 mitmproxy 引擎时执行（避免启动时 eager import）
                from .proxy.mitmproxy_engine import MitmproxyEngine
                proxy = MitmproxyEngine(
                    host=proxy_host,
                    port=get_proxy_port(),
                    ssl_bump=ssl_bump,
                )
                print("[Telnix] 使用 mitmproxy 引擎（proxy_engine=mitmproxy）")
            except Exception as e:  # noqa: BLE001
                print(f"[Telnix] mitmproxy 引擎初始化失败: {e}，回退到内置引擎")
                proxy = None
    elif proxy_engine == "async":
        try:
            # 延迟导入：仅在选择 async 引擎时执行（依赖 h2/httpx）
            from .proxy.async_proxy import AsyncProxyServer
            proxy = AsyncProxyServer(
                host=proxy_host,
                port=get_proxy_port(),
                ssl_bump=ssl_bump,
            )
            print("[Telnix] 使用 asyncio 引擎（proxy_engine=async）")
        except Exception as e:  # noqa: BLE001
            print(f"[Telnix] asyncio 引擎初始化失败: {e}，回退到内置引擎")
            proxy = None
    if proxy is None:
        proxy = ProxyServer(
            host=proxy_host,
            port=get_proxy_port(),
            ssl_bump=ssl_bump,
        )
        if proxy_engine == "mitmproxy" and _check_mitmproxy_available():
            print("[Telnix] 已回退到内置线程引擎（builtin）")
        elif proxy_engine == "async":
            print("[Telnix] 已回退到内置线程引擎（builtin）")
    # F5 修复：把 proxy 提升到模块级，供信号处理器/_do_quit/_do_restart 在 os._exit(0) 前调 stop()
    global _proxy
    _proxy = proxy
    # 性能优化：certutil 检测耗时 ~1-2s（subprocess.run），改为后台异步执行，
    # 不阻塞 main() 启动。前端通过 /cert/status 轮询时拿到最新结果。
    def _refresh_cert_in_bg():
        try:
            proxy.refresh_cert_status()
        except Exception:  # noqa: BLE001
            pass
    threading.Thread(target=_refresh_cert_in_bg, daemon=True,
                     name="cert-refresh").start()
    # 记录代理实际监听地址，供透明代理启动校验（需 0.0.0.0 才能接收重定向到本机 IP 的流量）
    try:
        from .proxy import transparent_proxy as _tp_mod
        _tp_mod.proxy_listen_host = proxy_host
    except Exception:  # noqa: BLE001
        pass
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
        # 隐蔽性：禁用 uvicorn 默认 Server 头（默认会暴露 "uvicorn"）
        # server=False 关闭 Server 头；access_log=False 不打访问日志
        uvicorn.run(app, host=host, port=port, log_level="info",
                    access_log=False, server_header=False)
    finally:
        proxy.stop()
        clear_system_proxy()


if __name__ == "__main__":
    main()
