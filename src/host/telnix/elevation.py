"""跨平台提权模块。

平台支持：
- Windows: ShellExecuteW('runas') 触发 UAC 提权
- macOS: osascript 弹出系统授权对话框（需要用户输入密码）
- Linux: pkexec（PolicyKit GUI 弹窗）或 sudo（CLI）

设计要点：
- 提权后重新启动 Telnix 进程（保留启动参数）
- 旧进程退出，新进程以高权限运行
- 优雅降级：工具不可用时返回错误信息
"""
from __future__ import annotations

import os
import subprocess
import sys
from typing import Optional

IS_WINDOWS = sys.platform == "win32"
IS_LINUX = sys.platform.startswith("linux")
IS_MACOS = sys.platform == "darwin"
IS_UNIX = IS_LINUX or IS_MACOS


def is_admin() -> bool:
    """检查当前进程是否具有管理员/root 权限。

    Windows: ctypes.windll.shell32.IsUserAnAdmin()
    Unix: os.geteuid() == 0 (root)
    """
    if IS_WINDOWS:
        try:
            import ctypes
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:  # noqa: BLE001
            return False
    try:
        return os.geteuid() == 0
    except AttributeError:
        return False


def _build_restart_argv() -> list[str]:
    """构建重启命令的参数列表（保留启动参数）。"""
    if getattr(sys, 'frozen', False):
        # PyInstaller 打包：直接运行 exe
        return [sys.executable]
    # Python 脚本：python -m telnix [flags...]
    argv = [sys.executable, "-m", "telnix"]
    # 透传启动参数（保留 --no-browser 等），过滤非 flag 参数
    argv.extend(a for a in sys.argv[1:] if a.startswith('-'))
    return argv


def _get_restart_cwd() -> str:
    """获取重启进程的工作目录。"""
    # telnix 包的父目录（即 src/host/），让 `python -m telnix` 能找到包
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def elevate_windows() -> tuple[bool, str]:
    """Windows: 用 ShellExecuteW('runas') 触发 UAC 提权。

    返回 (success, message)。
    """
    try:
        import ctypes
        exe = sys.executable
        if getattr(sys, 'frozen', False):
            # PyInstaller 打包：直接运行 exe
            params = ''
        else:
            # python -m telnix [flags...]
            argv_extra = [a for a in sys.argv[1:] if a.startswith('-')]
            params = '-m telnix'
            if argv_extra:
                params += ' ' + ' '.join(argv_extra)
        cwd = _get_restart_cwd()
        ret = ctypes.windll.shell32.ShellExecuteW(
            None, 'runas', exe, params, cwd, 1  # SW_SHOWNORMAL
        )
        if ret <= 32:
            return False, f'UAC 提权失败，返回码 {ret}（用户可能取消了 UAC）'
        return True, '已批准 UAC 提权'
    except Exception as e:  # noqa: BLE001
        return False, f'UAC 提权失败: {e}'


def elevate_macos() -> tuple[bool, str]:
    """macOS: 用 osascript 弹出系统授权对话框。

    通过 AppleScript 执行 `do shell script ... with administrator privileges`，
    系统会弹出密码输入框，用户输入密码后命令以 root 身份执行。

    返回 (success, message)。

    安全：用 shlex.quote() 对每个参数做 shell 转义，避免命令注入。
    cwd 中的反斜杠和双引号也做 AppleScript 字符串转义。
    """
    import shlex
    argv = _build_restart_argv()
    cwd = _get_restart_cwd()
    # 用 osascript 执行 sudo 命令（会弹出系统密码框）
    # 注意：osascript 的 administrator privileges 只能执行单条 shell 命令
    # 安全转义：每个参数用 shlex.quote() 包裹，防止 argv 中混入 shell 元字符
    cmd_str = ' '.join(shlex.quote(a) for a in argv)
    # AppleScript 字符串中反斜杠和双引号需要转义
    cwd_escaped = cwd.replace('\\', '\\\\').replace('"', '\\"')
    applescript = (
        f'do shell script "cd \\"{cwd_escaped}\\" && {cmd_str}" '
        f'with administrator privileges'
    )
    try:
        result = subprocess.run(
            ['osascript', '-e', applescript],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode != 0:
            err_msg = result.stderr.strip()
            if 'User canceled' in err_msg or 'user canceled' in err_msg.lower():
                return False, '用户取消了授权'
            return False, f'osascript 授权失败: {err_msg}'
        return True, '已批准 macOS 授权'
    except subprocess.TimeoutExpired:
        return False, 'osascript 授权超时（60 秒未响应）'
    except FileNotFoundError:
        return False, 'osascript 不可用（仅 macOS 自带）'
    except Exception as e:  # noqa: BLE001
        return False, f'macOS 授权失败: {e}'


def elevate_linux_pkexec() -> tuple[bool, str]:
    """Linux: 用 pkexec（PolicyKit）弹出 GUI 授权对话框。

    pkexec 是 PolicyKit 的命令行工具，桌面环境通常会弹出密码框。
    返回 (success, message)。

    安全：用 shlex.quote() 对每个参数做 shell 转义，避免命令注入。
    """
    import shlex
    argv = _build_restart_argv()
    cwd = _get_restart_cwd()
    # pkexec 直接执行命令（会弹出 GUI 授权框）
    try:
        # pkexec 不支持设置 cwd，用 sh -c 包装
        # 安全转义：每个参数用 shlex.quote() 包裹
        cmd_str = ' '.join(shlex.quote(a) for a in argv)
        result = subprocess.run(
            ['pkexec', 'sh', '-c', f'cd {shlex.quote(cwd)} && {cmd_str}'],
            capture_output=True, timeout=60,
        )
        if result.returncode != 0:
            return False, f'pkexec 授权失败（返回码 {result.returncode}）'
        return True, '已批准 pkexec 授权'
    except subprocess.TimeoutExpired:
        return False, 'pkexec 授权超时（60 秒未响应）'
    except FileNotFoundError:
        return False, 'pkexec 不可用（请安装 policykit-1）'
    except Exception as e:  # noqa: BLE001
        return False, f'pkexec 授权失败: {e}'


def elevate_linux_sudo() -> tuple[bool, str]:
    """Linux: 用 sudo 命令提权（CLI 模式，需要用户在终端输入密码）。

    返回 (success, message)。

    安全：用 shlex.quote() 对每个参数做 shell 转义，避免命令注入。
    """
    import shlex
    argv = _build_restart_argv()
    cwd = _get_restart_cwd()
    # sudo 会继承当前终端的 stdin，用户可以输入密码
    try:
        # 用 sudo -k 强制要求密码（避免缓存），-- 表示后续是非选项参数
        # 安全转义：每个参数用 shlex.quote() 包裹
        cmd_str = ' '.join(shlex.quote(a) for a in argv)
        result = subprocess.run(
            ['sudo', '-k', 'sh', '-c', f'cd {shlex.quote(cwd)} && {cmd_str}'],
            stdin=sys.stdin,  # 继承终端 stdin 让用户输入密码
        )
        if result.returncode != 0:
            return False, f'sudo 提权失败（返回码 {result.returncode}）'
        return True, '已通过 sudo 提权'
    except FileNotFoundError:
        return False, 'sudo 不可用'
    except Exception as e:  # noqa: BLE001
        return False, f'sudo 提权失败: {e}'


def elevate() -> tuple[bool, str]:
    """跨平台提权：重启 Telnix 进程为管理员/root 权限。

    平台支持：
    - Windows: ShellExecuteW('runas') 触发 UAC
    - macOS: osascript with administrator privileges
    - Linux: pkexec（GUI）或 sudo（CLI）

    返回 (success, message)。
    成功后调用方应退出当前进程（新进程已独立运行）。
    """
    if IS_WINDOWS:
        return elevate_windows()
    elif IS_MACOS:
        return elevate_macos()
    elif IS_LINUX:
        # 优先用 pkexec（GUI 弹窗更友好），失败时回退到 sudo
        ok, msg = elevate_linux_pkexec()
        if not ok and 'pkexec 不可用' in msg:
            # pkexec 不存在，回退到 sudo
            return elevate_linux_sudo()
        return ok, msg
    else:
        return False, f'当前平台 {sys.platform} 不支持提权'


def get_elevation_info() -> dict:
    """返回提权后端信息（用于前端显示）。"""
    admin = is_admin()
    if IS_WINDOWS:
        return {
            "platform": "windows",
            "backend": "uac",
            "is_admin": admin,
            "supported": True,
            "hint": "就绪" if not admin else "已是管理员",
        }
    elif IS_MACOS:
        return {
            "platform": "macos",
            "backend": "osascript",
            "is_admin": admin,
            "supported": True,
            "hint": "就绪" if not admin else "已是 root",
        }
    elif IS_LINUX:
        # 检测 pkexec 是否可用
        pkexec_available = os.path.exists('/usr/bin/pkexec') or os.path.exists('/bin/pkexec')
        sudo_available = os.path.exists('/usr/bin/sudo') or os.path.exists('/bin/sudo')
        backend = "pkexec" if pkexec_available else ("sudo" if sudo_available else "none")
        return {
            "platform": "linux",
            "backend": backend,
            "is_admin": admin,
            "supported": pkexec_available or sudo_available,
            "hint": (
                "就绪" if not admin and (pkexec_available or sudo_available)
                else "已是 root" if admin
                else "未找到 pkexec/sudo，请手动用 sudo 启动"
            ),
        }
    return {
        "platform": sys.platform,
        "backend": "none",
        "is_admin": admin,
        "supported": False,
        "hint": "不支持的平台",
    }
