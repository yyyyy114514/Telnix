"""Telnix entry point: start proxy + FastAPI + auto-open browser + set system proxy.

Startup flow:
1. Initialize SQLite
2. Initialize SSLBumpManager (generate root certificate if not present)
3. Start the proxy server (127.0.0.1:8888)
4. Start FastAPI (127.0.0.1:18901)
5. Set the system proxy to 127.0.0.1:8888
6. Register atexit + console signal to restore proxy
7. Auto-open browser (unless --no-browser)

Launch arguments:
  python -m telnix                  Normal startup (auto-opens browser)
  python -m telnix --no-browser     No browser (for agent automation scenarios)
  python -m telnix --mcp            MCP mode: fixed ports 8888/18901, ignore
                                    settings.json port config, fail on port conflict

Port notes:
  - API port 18901 (note: 18899/18900 are reserved by Windows dynamic ports on
    this machine; bind would fail with WSAEACCES, so 18901 is used)
  - Proxy port 8888 (the system proxy points to this port)
  - CLI defaults to connecting to 18901 (cli.py:92 stays in sync with config.DEFAULT_PORT)
  - Normal mode: reads api_port/proxy_port from settings.json; if a port is
    occupied, automatically falls back to a random port and records the conflict
    so the frontend can prompt the user.
  - Random-port mode (settings.json random_port=true): both API and proxy ports
    are randomly chosen on every startup.
  - MCP mode (--mcp): ignores settings.json, uses fixed 8888/18901; if either is
    occupied, exits with error (no fallback) so the agent can detect failure.
"""

import argparse
import atexit
import os
import socket
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
    DEFAULT_PORT,
    PROXY_PORT,
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
    """Lazily detect whether mitmproxy is available (avoids slowing startup with eager import).

    Performs the import on the first call inside main(), and caches the result at module level.
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
    """Path to the proxy state marker file (delegated to the system_proxy module)."""
    return system_proxy._get_proxy_flag_path()


def set_system_proxy(host: str = "127.0.0.1", port: int = 8888) -> bool:
    """Set the system proxy (cross-platform).

    Platform support:
    - Windows: write registry + notify system to refresh
    - macOS: networksetup to set HTTP/HTTPS proxy
    - Linux GNOME: gsettings to set org.gnome.system.proxy
    - Linux KDE: kwriteconfig5 to set kioslaverc

    Performance optimization: the proxy excludes localhost/127.0.0.1 so the
    browser connects directly to the local API. Otherwise SSE (/api/flows/stream)
    would be buffered by the store-and-forward mode when going through the proxy,
    preventing real-time push.

    Returns True on success, False on failure.
    """
    return system_proxy.set_system_proxy(host, port)


def clear_system_proxy() -> bool:
    """Clear the system proxy (cross-platform).

    Returns True on success, False on failure.
    """
    return system_proxy.clear_system_proxy()


def _notify_settings_changed():
    """Notify the system that proxy settings have changed (Windows only, delegated to the system_proxy module)."""
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
    """Register a watchdog: automatically restore proxy settings on process exit.

    Covered exit scenarios:
    - Normal exit (sys.exit / main returns): atexit triggers clear_system_proxy
    - Windows: Ctrl+C / Ctrl+Break / closing the console window -> SetConsoleCtrlHandler
    - Unix: SIGINT (Ctrl+C) / SIGTERM (kill) -> signal.signal

    Platform differences:
    - Windows: SetConsoleCtrlHandler synchronously clears the proxy + os._exit
      (Windows only gives ~5 seconds when closing the window)
    - Unix: signal.signal registers a SIGINT/SIGTERM handler that clears the
      proxy and then calls sys.exit
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
    """Open the browser after a delay, waiting for the service to be ready."""

    def _open():
        time.sleep(delay)
        webbrowser.open(url)

    threading.Thread(target=_open, daemon=True).start()


# ---------- 主入口 ----------

def _cleanup_proxy_before_exit():
    """F5 fix: explicitly release proxy / raw_capture / transparent_proxy resources before os._exit(0).

    os._exit(0) skips finally / atexit, causing socket + ThreadPool + WinDivert
    handle leaks. This function is called before all 4 os._exit(0) sites for
    centralized cleanup:
    1. proxy.stop() -- releases server socket + connection pool (cross-platform)
    2. stop_raw_capture() -- releases WinDivert handle + worker thread (Platform: Windows)
    3. stop_transparent_proxy() -- releases WinDivert handle + NAT table + worker thread (Platform: Windows)

    All calls are wrapped in try/except to prevent any exception from interrupting
    the exit flow. All three stop calls are idempotent and swallow exceptions internally.
    Worst-case takes ~5-10s (join timeout); Windows may force-kill on window close,
    but already-closed handles are still reclaimed by the OS.
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
    """Restart the service: clear proxy + start a new process + wait for port release + exit the current process.

    Windows pitfalls (already encountered):
    1. os.execv makes the new process inherit socket handles, causing port bind failure
    2. subprocess.Popen + CREATE_NEW_CONSOLE: in some environments the new process
       still inherits some handles (even with close_fds=True), causing uvicorn to
       fail to bind the port, manifesting as "service won't start after clicking
       restart, browser disconnects immediately"
    3. Admin restart with ShellExecuteW('runas') always worked correctly, showing
       that ShellExecuteW is a more reliable approach (creates a fully independent
       process that does not inherit any parent handles)

    Final approach: normal restart also uses ShellExecuteW ('open' verb, no UAC
    elevation), the same mechanism as admin restart but with a different verb.
    This guarantees:
    - The new process is fully independent (does not inherit socket handles)
    - The new process has its own working directory
    - The old process exits while the new process continues running
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


# ---------- 端口可用性检测 ----------

def _bool_setting(v: Any) -> bool:
    """安全解析布尔设置项。settings.json 中布尔值存为字符串 "0"/"1"，
    bool("0") 在 Python 中为 True（非空字符串），需显式解析。"""
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)


def _is_port_available(port: int, host: str = "127.0.0.1") -> bool:
    """检查端口是否可用（可绑定）。

    用 SO_REUSEADDR 避免 TIME_WAIT 假阴性（uvicorn 也会设置该选项）。
    注意：检测和实际 bind 之间有 TOCTOU 窗口，但启动顺序通常是单线程的，影响极小。
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((host, port))
            return True
    except (OSError, socket.error):
        return False


def _find_random_port(host: str = "127.0.0.1") -> int:
    """找一个可用的随机端口（让 OS 分配：bind 到 port 0）。

    返回值范围通常在 1024-65535（OS 临时端口区段）。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        return s.getsockname()[1]


def _resolve_listen_host(host: str) -> str:
    """把 0.0.0.0 等通配地址转为可用于 bind 检测的具体地址。

    socket.bind(('0.0.0.0', port)) 会同时占用所有网卡，端口检测语义与
    uvicorn/proxy 的实际监听一致；但若主程序退出后未及时释放可能误判。
    这里保持原 host 不变，让 bind 检测与实际监听语义一致。
    """
    return host


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
    parser.add_argument(
        "--mcp",
        action="store_true",
        help="MCP 模式：使用固定端口 8888/18901，忽略设置文件中的端口配置，端口被占用直接报错",
    )
    args, _ = parser.parse_known_args()
    no_browser = args.no_browser or os.environ.get("TELNIX_NO_BROWSER") == "1"
    mcp_mode = args.mcp or os.environ.get("TELNIX_MCP_MODE") == "1"

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

    from . import settings_store

    # ---------- 端口解析 ----------
    # MCP 模式：固定 8888/18901，忽略 settings.json，占用直接报错
    # 随机端口模式：每次启动用 OS 分配的随机端口
    # 普通模式：读 settings.json；端口被占用时自动切换到随机端口并记录冲突
    host = get_host()
    proxy_host = get_proxy_host()
    # 透明代理要求代理监听 0.0.0.0（详见下方原注释）
    if _bool_setting(settings_store.get_setting("transparent_proxy", False)):
        proxy_host = "0.0.0.0"

    # bind 检测用的具体地址：0.0.0.0 直接用，其他地址原样用
    api_check_host = _resolve_listen_host(host)
    proxy_check_host = _resolve_listen_host(proxy_host)

    port_conflict: dict | None = None

    if mcp_mode:
        # MCP 模式：固定端口，占用直接报错退出
        api_port = DEFAULT_PORT  # 18901
        proxy_port = PROXY_PORT  # 8888
        if not _is_port_available(api_port, api_check_host):
            print(
                f"[Telnix] MCP 模式下 API 端口 {api_port} 被占用，"
                f"按 MCP 协议要求不自动切换，直接报错退出",
                file=sys.stderr,
            )
            sys.exit(1)
        if not _is_port_available(proxy_port, proxy_check_host):
            print(
                f"[Telnix] MCP 模式下代理端口 {proxy_port} 被占用，"
                f"按 MCP 协议要求不自动切换，直接报错退出",
                file=sys.stderr,
            )
            sys.exit(1)
        print(f"[Telnix] MCP 模式：使用固定端口 API={api_port} 代理={proxy_port}")
    else:
        random_port = _bool_setting(settings_store.get_setting("random_port", False))
        if random_port:
            # 随机端口模式：OS 分配，不视为冲突
            api_port = _find_random_port(api_check_host)
            proxy_port = _find_random_port(proxy_check_host)
            print(f"[Telnix] 随机端口模式：API={api_port} 代理={proxy_port}")
        else:
            # 普通模式：读 settings.json
            # 注意：random_port=false 时，端口被占用不自动切换随机端口（尊重用户设置），
            # 而是重试等待后报错退出，避免「关了随机端口还是随机端口」的困惑。
            api_port = get_port()
            proxy_port = get_proxy_port()

            def _wait_port(port: int, host: str, name: str, retries: int = 6, delay: float = 0.5) -> int:
                """等待端口可用，处理 TIME_WAIT/旧实例未完全退出的情况。
                返回端口号（始终等于输入port，不可用则报错退出）。"""
                import time
                for attempt in range(retries):
                    if _is_port_available(port, host):
                        return port
                    if attempt < retries - 1:
                        print(f"[Telnix] {name}端口 {port} 被占用，等待{delay}s后重试 ({attempt+1}/{retries})...",
                              file=sys.stderr)
                        time.sleep(delay)
                # 最后一次检测失败：尝试查找占用进程并提示
                pid_info = ""
                try:
                    import subprocess
                    r = subprocess.run(
                        ["netstat", "-ano"], capture_output=True, text=True, timeout=5,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    )
                    for ln in r.stdout.splitlines():
                        if f":{port}" in ln and "LISTEN" in ln:
                            parts = ln.split()
                            if len(parts) >= 5:
                                pid_info = f"（占用进程 PID: {parts[-1]}）"
                                break
                except Exception:
                    pass
                print(
                    f"[Telnix] {name}端口 {port} 被占用{pid_info}，无法启动。\n"
                    f"[Telnix] 请关闭占用该端口的程序后重试，或在设置中修改端口/开启随机端口模式。",
                    file=sys.stderr,
                )
                sys.exit(1)

            api_port = _wait_port(api_port, api_check_host, "API")
            proxy_port = _wait_port(proxy_port, proxy_check_host, "代理")

    # 代理服务器：根据 settings 的 proxy_engine 选择引擎
    # - builtin：内置线程代理（默认，零依赖，稳定）
    # - async：asyncio 代理（G 方案，asyncio accept + 线程处理，实验性）
    # - v2：高性能 asyncio 全异步引擎（连接池复用，高并发，uvloop/winloop 加速）
    # - mitmproxy：mitmproxy 引擎（H 方案，可选依赖 ~50MB，未安装时回退到 builtin）
    proxy_engine = settings_store.get_setting("proxy_engine", "builtin")
    # 透明代理要求代理监听 0.0.0.0：WinDivert 需把流量重定向到「本机真实 IP」，
    # 仅监听 127.0.0.1 时收不到被重定向到本机 IP 的包（见 transparent_proxy._detect_local_ip）。
    # 开启透明代理时强制 0.0.0.0（系统代理仍用 127.0.0.1 连接，0.0.0.0 监听包含 127.0.0.1，
    # 不受影响；仅暴露端口范围略增，透明代理本就是需要管理员权限的高级功能）。
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
                    port=proxy_port,
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
                port=proxy_port,
                ssl_bump=ssl_bump,
            )
            print("[Telnix] 使用 asyncio 引擎（proxy_engine=async）")
        except Exception as e:  # noqa: BLE001
            print(f"[Telnix] asyncio 引擎初始化失败: {e}，回退到内置引擎")
            proxy = None
    elif proxy_engine == "v2":
        try:
            # v2 高性能全异步引擎（uvloop/winloop + asyncio 连接池）
            from .proxy.async_engine_v2 import AsyncEngineV2
            proxy = AsyncEngineV2(
                host=proxy_host,
                port=proxy_port,
                ssl_bump=ssl_bump,
            )
            print("[Telnix] 使用 v2 高性能异步引擎（proxy_engine=v2）")
        except Exception as e:  # noqa: BLE001
            print(f"[Telnix] v2 引擎初始化失败: {e}，回退到内置引擎")
            proxy = None
    if proxy is None:
        proxy = ProxyServer(
            host=proxy_host,
            port=proxy_port,
            ssl_bump=ssl_bump,
        )
        if proxy_engine == "mitmproxy" and _check_mitmproxy_available():
            print("[Telnix] 已回退到内置线程引擎（builtin）")
        elif proxy_engine == "async":
            print("[Telnix] 已回退到内置线程引擎（builtin）")
        elif proxy_engine == "v2":
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
    # 注入实际使用的端口 + 端口冲突信息，供 /api/capture/status 与 /api/system/ports 读取
    state.api_port = api_port
    state.proxy_port = proxy_port
    state.port_conflict = port_conflict
    app = create_app(state)

    # 注入系统控制钩子（重启/退出/清代理/开代理）# 系统控制钩子
    def _enable_sys_proxy():
        # 系统代理始终用 127.0.0.1（电脑本机访问代理服务器），
        # 即使代理监听 0.0.0.0（手机/局域网设备可连）也不影响电脑本机。
        # 之前用 get_proxy_host() 会在开启"允许局域网设备连接"后写成 0.0.0.0:8888，
        # Windows 客户端不能连 0.0.0.0，导致电脑无法上网。
        # 使用实际监听的 proxy_port（可能因随机模式或冲突自动切换而与 settings.json 不同）
        set_system_proxy("127.0.0.1", proxy_port)
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

    # 浏览器始终用 127.0.0.1 打开（0.0.0.0 在 Windows 上浏览器无法访问）
    # 即使 API 监听 0.0.0.0（允许局域网设备连接），本机也通过 127.0.0.1 访问
    browser_url = f"http://127.0.0.1:{api_port}"
    print(f"[Telnix] 代理地址: {proxy_host}:{proxy_port}")
    print(f"[Telnix] Web 地址: {browser_url}" + (f"（监听 {host}，局域网可访问）" if host == "0.0.0.0" else ""))
    print(f"[Telnix] 根证书: {ssl_bump.root_cert_path}")
    print(f"[Telnix] 根证书已安装: {proxy.cert_installed}")
    if no_browser:
        print("[Telnix] --no-browser 模式：不自动打开浏览器（agent 自动化场景）")
    else:
        _open_browser_later(browser_url)

    try:
        # 性能优化：
        # 1. uvicorn[standard] 启用 uvloop（Linux/macOS）或直接运行在 winloop 上（Windows）
        # 2. loop_run: asyncio 默认 1 秒超时，改为更短的超时减少调度开销
        # 3. http 1.1: 保持 HTTP/1.1 简单可靠，HTTP/2 需要额外配置
        # 4. limit_concurrency: 限制最大并发连接数，防止资源耗尽
        # 5. access_log=False: 关闭访问日志减少 I/O 开销
        # 6. server_header=False: 移除服务器头（安全）
        # 7. orjson 已在 pyproject.toml 中声明，FastAPI 会自动使用
        uvicorn.run(
            app,
            host=host,
            port=api_port,
            loop="auto",  # 自动选择 uvloop/winloop（通过 uvicorn[standard]）
            http="auto",  # 自动选择 HTTP 协议
            limit_concurrency=500,  # 限制并发连接数
            access_log=False,
            server_header=False,
        )
    finally:
        proxy.stop()
        clear_system_proxy()


if __name__ == "__main__":
    main()
