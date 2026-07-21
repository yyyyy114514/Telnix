"""系统控制 API：重启服务 / 退出 / 关闭系统代理。

设计要点：
- 重启：通过 os.execv 在同进程内重启，继承同一控制台窗口。重启前清系统代理。
- 退出：调用 _quit_hook（由 __main__.py 注入），清代理 + sys.exit。
- 关闭系统代理：调用注入的 _clear_proxy_hook。
- 管理员重启（restart-as-admin）：先经 GUI 用户确认（置顶弹窗），同意后 ShellExecuteW runas。
  CLI 发起 → 后端创建 pending 请求并阻塞等待 → 前端轮询弹窗 → 用户响应 → 解除阻塞。
"""

import os
import sys
import threading
import time
import uuid

from fastapi import APIRouter, Request

from . import ok, err

router = APIRouter()

# 由 __main__.py 注入的钩子（可选）
_restart_hook = None
_quit_hook = None
_clear_proxy_hook = None
_enable_proxy_hook = None
# 系统代理当前是否开启（由 __main__ 更新）
system_proxy_on = False

# ------ 代理丢失检测 ------
# _proxy_lost：期望开启（system_proxy_on=True）但实际注册表已关闭/被抢占时置 True
# 前端轮询 /status 时检测该字段，弹窗询问是否重新开启
# 用户主动关闭代理（/system/clear-proxy）时同步重置为 False，避免误报
_proxy_lost = False
_proxy_monitor_thread: threading.Thread | None = None
_proxy_monitor_started = False
_proxy_monitor_lock = threading.Lock()


def set_hooks(restart=None, quit=None, clear_proxy=None, enable_proxy=None):
    """注入重启/退出/清代理/开代理的回调。"""
    global _restart_hook, _quit_hook, _clear_proxy_hook, _enable_proxy_hook
    if restart is not None:
        _restart_hook = restart
    if quit is not None:
        _quit_hook = quit
    if clear_proxy is not None:
        _clear_proxy_hook = clear_proxy
    if enable_proxy is not None:
        _enable_proxy_hook = enable_proxy


def mark_proxy_on(v: bool):
    """更新系统代理开关状态。

    用户主动开启/关闭代理时调用此函数。
    主动关闭时同时重置 _proxy_lost=False，避免前端继续弹窗。
    """
    global system_proxy_on, _proxy_lost
    system_proxy_on = v
    if not v:
        _proxy_lost = False


def is_proxy_lost() -> bool:
    """查询代理是否丢失（前端轮询 /status 时使用）。"""
    return _proxy_lost


def start_proxy_monitor():
    """启动代理状态监控线程（仅启动一次，由 __main__.py 调用）。

    每 5 秒读取注册表实际代理状态，与 system_proxy_on 对比：
    - 期望开启但实际关闭 → _proxy_lost=True
    - 期望关闭时不监控（避免误报）
    """
    global _proxy_monitor_thread, _proxy_monitor_started
    with _proxy_monitor_lock:
        if _proxy_monitor_started:
            return
        _proxy_monitor_started = True
    _proxy_monitor_thread = threading.Thread(
        target=_proxy_monitor_loop,
        daemon=True,
        name="proxy-monitor",
    )
    _proxy_monitor_thread.start()


def _read_actual_proxy_enabled() -> bool:
    """读取注册表实际系统代理开关（ProxyEnable）。"""
    try:
        import winreg
        reg_path = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, reg_path) as key:
            enable, _ = winreg.QueryValueEx(key, "ProxyEnable")
            return bool(enable)
    except Exception:  # noqa: BLE001
        return False


def _proxy_monitor_loop():
    """代理状态监控循环（后台线程）。"""
    global _proxy_lost
    # 启动时先等 10 秒，让前端初始化完成，避免初次轮询误触发
    time.sleep(10)
    last_seen_actual = None  # 上一次实际状态（None 表示未初始化）
    while True:
        time.sleep(5)
        try:
            # 只在用户期望开启时监控
            if not system_proxy_on:
                last_seen_actual = None
                if _proxy_lost:
                    global_set_proxy_lost(False)
                continue
            actual = _read_actual_proxy_enabled()
            if not actual and (last_seen_actual is None or last_seen_actual):
                # 期望开但实际关：代理丢失
                if not _proxy_lost:
                    global_set_proxy_lost(True)
                    print("[OpenNet] 检测到系统代理已丢失", file=sys.stderr)
            elif actual and not (last_seen_actual is None or last_seen_actual):
                # 代理恢复了（前端弹窗后用户同意重新开启）
                if _proxy_lost:
                    global_set_proxy_lost(False)
                    print("[OpenNet] 系统代理已恢复", file=sys.stderr)
            last_seen_actual = actual
        except Exception as e:  # noqa: BLE001
            print(f"[OpenNet] 代理监控异常: {e}", file=sys.stderr)


def global_set_proxy_lost(v: bool):
    """线程安全地更新 _proxy_lost。"""
    global _proxy_lost
    _proxy_lost = v


# ---------- 管理员重启：用户确认流程 ----------
# 待确认请求池：request_id → {event, response, created_at, source}
# - event: threading.Event，用户响应时 set
# - response: 'accept' | 'reject' | None（未响应）
# - source: 'cli' | 'api'
_pending_admin_requests: dict[str, dict] = {}
_pending_lock = threading.Lock()


def _create_pending_request(source: str = "cli") -> str:
    """创建一个待确认的管理员重启请求，返回 request_id。

    创建后立即在新线程中弹原生 Windows MessageBox（置顶），用户响应后回传结果。
    """
    rid = uuid.uuid4().hex[:12]
    with _pending_lock:
        _pending_admin_requests[rid] = {
            "event": threading.Event(),
            "response": None,
            "created_at": time.time(),
            "source": source,
        }
    # 启动线程弹原生 Windows 置顶 Yes/No 弹窗（不阻塞 API 线程）
    threading.Thread(
        target=_native_message_box_thread,
        args=(rid, source),
        daemon=True,
    ).start()
    return rid


def _native_message_box_thread(rid: str, source: str):
    """在新线程中弹原生 Windows Yes/No 弹窗，把结果回传到 pending 请求。"""
    title = "OpenNet 管理员重启请求"
    message = (
        f"来源: {source}\n\n"
        "CLI / Agent 请求以管理员身份重启 OpenNet，\n"
        "以辅助 agent 抓包（TCP/UDP 抓包需要管理员权限）。\n\n"
        "同意后将弹出 UAC 提权窗口，OpenNet 会以管理员身份重启。\n\n"
        "是否同意？"
    )
    result = _show_native_message_box(title, message)
    response = 'accept' if result == 'yes' else 'reject'
    _respond_pending_request(rid, response)


def _show_native_message_box(title: str, message: str) -> str:
    """显示原生 Windows 置顶 Yes/No 弹窗，返回 'yes' / 'no'。

    使用 MB_TOPMOST | MB_SYSTEMMODAL | MB_SETFOREGROUND 确保窗口置顶显示，
    引起用户注意（任务栏图标闪烁 + 强制前置）。
    """
    if sys.platform != 'win32':
        # 非 Windows 平台退化：直接同意（无原生弹窗）
        return 'yes'
    MB_YESNO = 0x00000004
    MB_ICONQUESTION = 0x00000020
    MB_TOPMOST = 0x00040000
    MB_SETFOREGROUND = 0x00010000
    MB_SYSTEMMODAL = 0x00001000
    MB_DEFBUTTON2 = 0x00000100  # 默认聚焦「否」按钮，避免误按回车同意
    IDYES = 6
    IDNO = 7
    flags = (
        MB_YESNO
        | MB_ICONQUESTION
        | MB_TOPMOST
        | MB_SETFOREGROUND
        | MB_SYSTEMMODAL
        | MB_DEFBUTTON2
    )
    try:
        import ctypes
        # MessageBoxW 是 Unicode 版本，支持中文
        result = ctypes.windll.user32.MessageBoxW(
            None,                  # hWnd: 无父窗口（桌面）
            message,
            title,
            flags,
        )
        return 'yes' if result == IDYES else 'no'
    except Exception:  # noqa: BLE001
        # 任何异常都视为拒绝（保守安全策略）
        return 'no'


def _wait_pending_request(rid: str, timeout: float = 60.0) -> str | None:
    """阻塞等待用户响应，返回 'accept' / 'reject' / None（超时）。"""
    with _pending_lock:
        item = _pending_admin_requests.get(rid)
    if not item:
        return None
    # 阻塞等待，最多 timeout 秒
    triggered = item["event"].wait(timeout=timeout)
    if not triggered:
        return None
    return item["response"]


def _respond_pending_request(rid: str, response: str) -> bool:
    """用户响应 pending 请求，返回是否成功。"""
    with _pending_lock:
        item = _pending_admin_requests.get(rid)
        if not item or item["response"] is not None:
            return False
        item["response"] = response
        item["event"].set()
    return True


def _cleanup_pending_request(rid: str):
    """清理已处理的 pending 请求。"""
    with _pending_lock:
        _pending_admin_requests.pop(rid, None)


def _list_pending_requests() -> list[dict]:
    """列出所有待确认请求（用于前端轮询）。"""
    now = time.time()
    out = []
    with _pending_lock:
        items = list(_pending_admin_requests.items())
    for rid, item in items:
        # 超过 120s 未响应的视为过期，跳过（CLI 端已超时退出）
        if now - item["created_at"] > 120:
            continue
        if item["response"] is not None:
            continue
        out.append({
            "request_id": rid,
            "source": item["source"],
            "created_at": item["created_at"],
            "action": "restart-as-admin",
            "message": "CLI 请求以管理员身份重启 OpenNet 以辅助 agent 抓包（TCP/UDP 抓包需要管理员权限）",
        })
    return out


def _do_shell_elevate() -> tuple[bool, str]:
    """执行 UAC 提权，启动新的管理员进程。返回 (success, message)。"""
    if _clear_proxy_hook is not None:
        try:
            _clear_proxy_hook()
        except Exception:  # noqa: BLE001
            pass
    if getattr(sys, 'frozen', False):
        exe = sys.executable
        params = ''
    else:
        exe = sys.executable  # python.exe
        # 透传启动参数（保留 --no-browser 等），确保管理员进程行为一致
        argv_extra = [a for a in sys.argv[1:] if a.startswith('-')]
        params = '-m opennet'
        if argv_extra:
            params += ' ' + ' '.join(argv_extra)
    try:
        import ctypes
        ret = ctypes.windll.shell32.ShellExecuteW(
            None, 'runas', exe, params, None, 1  # SW_SHOWNORMAL
        )
        if ret <= 32:
            return False, f'提权失败，返回码 {ret}（用户可能取消了 UAC）'
    except Exception as e:  # noqa: BLE001
        return False, f'提权失败: {e}'
    # 退出当前非管理员进程
    threading.Thread(target=_delayed_quit, daemon=True).start()
    return True, '已批准'


@router.post("/system/restart")
async def restart_service(request: Request):
    """重启前后端服务（同进程内 os.execv 重启）。"""
    if _restart_hook is None:
        return err("重启钩子未注入")
    # 异步执行，避免请求未返回就退出
    threading.Thread(target=_delayed_restart, daemon=True).start()
    return ok({"restarting": True}, "正在重启服务，请稍候...")


@router.post("/system/quit")
async def quit_service(request: Request):
    """退出 OpenNet（关闭前后端 + 清系统代理）。"""
    threading.Thread(target=_delayed_quit, daemon=True).start()
    return ok({"quitting": True}, "正在退出 OpenNet...")


@router.post("/system/clear-proxy")
async def clear_proxy(request: Request):
    """仅关闭系统代理（保留服务运行）。"""
    if _clear_proxy_hook is not None:
        try:
            _clear_proxy_hook()
        except Exception as e:  # noqa: BLE001
            return err(f"清代理失败: {e}")
    mark_proxy_on(False)
    return ok({"cleared": True, "system_proxy_on": False}, "系统代理已关闭")


@router.post("/system/enable-proxy")
async def enable_proxy(request: Request):
    """重新开启系统代理（指向 OpenNet 代理端口）。"""
    if _enable_proxy_hook is None:
        return err("开代理钩子未注入")
    try:
        _enable_proxy_hook()
    except Exception as e:  # noqa: BLE001
        return err(f"开启代理失败: {e}")
    mark_proxy_on(True)
    return ok({"enabled": True, "system_proxy_on": True}, "系统代理已开启")


@router.post("/system/restart-as-admin")
async def restart_as_admin(request: Request):
    """以管理员身份重启 OpenNet（UAC 提权）。

    兼容旧接口：直接弹 UAC，不经 GUI 确认。
    新的 CLI 流程请使用 /system/request-admin-restart + /system/admin-request/{id}/wait。
    """
    success, msg = _do_shell_elevate()
    if not success:
        return err(msg)
    return ok({'restarting': True}, '正在以管理员身份重启 OpenNet...')


@router.post("/system/request-admin-restart")
async def request_admin_restart(request: Request):
    """创建一个待 GUI 确认的管理员重启请求。

    返回 request_id，CLI 应通过 /system/admin-request/{id}/wait 长轮询等待响应。
    """
    rid = _create_pending_request(source="cli")
    return ok({
        "request_id": rid,
        "message": "已创建待确认请求，等待 GUI 用户响应",
    })


@router.get("/system/admin-request/{rid}/wait")
async def admin_request_wait(rid: str, request: Request):
    """长轮询等待用户响应，超时 60s 返回 pending。"""
    import asyncio
    # 检查 rid 是否存在
    with _pending_lock:
        item = _pending_admin_requests.get(rid)
    if not item:
        return err("请求不存在或已处理")
    # 阻塞等待放到线程池，避免阻塞事件循环
    response = await asyncio.to_thread(_wait_pending_request, rid, 60.0)
    if response is None:
        return ok({"status": "pending", "request_id": rid}, "仍在等待用户响应")
    # 已响应
    if response == "accept":
        # 执行 UAC 提权
        success, msg = await asyncio.to_thread(_do_shell_elevate)
        _cleanup_pending_request(rid)
        if not success:
            return err(msg)
        return ok({"status": "accepted", "request_id": rid}, "用户已批准，正在以管理员身份重启")
    else:
        _cleanup_pending_request(rid)
        return ok({"status": "rejected", "request_id": rid}, "用户拒绝了管理员重启请求")


@router.get("/system/pending-admin-actions")
async def pending_admin_actions(request: Request):
    """前端轮询：获取待确认的管理员动作列表。"""
    return ok({"items": _list_pending_requests()})


@router.post("/system/admin-request/{rid}/respond")
async def admin_request_respond(rid: str, request: Request):
    """前端用户响应 pending 请求（accept/reject）。"""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    response = (body.get("response") or "").lower()
    if response not in ("accept", "reject"):
        return err("response 必须是 accept 或 reject")
    success = _respond_pending_request(rid, response)
    if not success:
        return err("请求不存在或已响应")
    return ok({"request_id": rid, "response": response}, "已提交响应")


def _delayed_restart():
    """延迟 500ms 执行重启，让 HTTP 响应先返回。"""
    import time
    time.sleep(0.5)
    try:
        _restart_hook()
    except Exception as e:  # noqa: BLE001
        print(f"[OpenNet] 重启失败: {e}", file=sys.stderr)


def _delayed_quit():
    """延迟 500ms 执行退出。"""
    import time
    time.sleep(0.5)
    try:
        _quit_hook()
    except Exception as e:  # noqa: BLE001
        print(f"[OpenNet] 退出失败: {e}", file=sys.stderr)
