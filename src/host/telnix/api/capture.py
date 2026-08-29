"""Capture control API: start/stop/status/clear."""

import asyncio
import threading
from datetime import datetime

from fastapi import APIRouter, Request
from pydantic import BaseModel

from .. import db
from ..logger import _capture_log
from . import err, ok

router = APIRouter()


class CaptureStartBody(BaseModel):
    """capture start request body. When auto_stop_seconds > 0, backend starts timer to auto-stop.

    Design fix: auto_stop_seconds has a maximum limit of 86400 seconds (24 hours).
    """
    auto_stop_seconds: float | None = None

    @property
    def validated_auto_stop(self) -> float:
        """Get validated auto_stop_seconds with upper limit."""
        if self.auto_stop_seconds is None or self.auto_stop_seconds <= 0:
            return 0.0
        # Upper limit: 24 hours (86400 seconds)
        AUTO_STOP_MAX = 86400
        return min(self.auto_stop_seconds, AUTO_STOP_MAX)


@router.get("/status")
async def status(request: Request):
    """Capture status + proxy status + certificate status + pinning suspected processes."""
    state = request.app.state.telnix
    proxy = state.proxy
    cert_installed = False
    if proxy and proxy.ssl_bump:
        cert_installed = proxy.cert_installed
    # 疑似证书 pinning 的进程列表（TLS 握手失败 + 证书相关错误）
    pinning_suspected = []
    if proxy:
        with proxy._pinning_lock:  # noqa: SLF001
            pinning_suspected = list(proxy._pinning_suspected)  # noqa: SLF001
    # SSL bump 失败已降级的 host 列表
    ssl_bump_failed_hosts = []
    if proxy:
        with proxy._ssl_bump_failed_lock:  # noqa: SLF001
            ssl_bump_failed_hosts = sorted(proxy._ssl_bump_failed_hosts)  # noqa: SLF001
    # HTTP/2 连接池统计
    h2_stats = None
    if proxy and hasattr(proxy, "_h2_pool"):
        h2_stats = proxy._h2_pool.stats()  # noqa: SLF001
    return ok({
        "capturing": proxy.capturing if proxy else False,
        "session_id": state.current_session_id,
        "proxy_running": proxy is not None and proxy._running,  # noqa: SLF001
        "proxy_host": proxy.host if proxy else None,
        "proxy_port": proxy.port if proxy else None,
        "cert_installed": cert_installed,
        "system_proxy_on": _system_proxy_state(),
        "proxy_lost": _proxy_lost_state(),
        "breakpoint": proxy.breakpoint.status() if proxy else None,
        "pinning_suspected": pinning_suspected,
        "ssl_bump_failed_hosts": ssl_bump_failed_hosts,
        "started_at": state.started_at,
        "h2_stats": h2_stats,
        # 队列满时丢弃的流量计数（监控写入背压）
        "flow_dropped_count": db._flow_dropped_count,  # noqa: SLF001
        # update 队列满时丢弃的更新计数（监控写入背压）
        "update_dropped_count": db._update_dropped_count,  # noqa: SLF001
        # 端口信息：实际使用的端口 + 端口冲突提示（手动设置端口被占用时前端弹窗）
        "api_port": state.api_port,
        # port_conflict: None 或 {"api": {"old": x, "new": y}, "proxy": {"old": x, "new": y}}
        "port_conflict": state.port_conflict,
    })


def _system_proxy_state() -> bool:
    """Read system proxy switch state (from system_api module)."""
    try:
        from . import system
        return system.system_proxy_on
    except Exception as e:  # noqa: BLE001
        _capture_log("error", "API exception in capture.py", extra={"exc": repr(e)})
        _capture_log("error", "system proxy state error", extra={"exc": repr(e)})
        return False


def _proxy_lost_state() -> bool:
    """Read proxy lost state (from system_api module)."""
    try:
        from . import system
        return system.is_proxy_lost()
    except Exception as e:  # noqa: BLE001
        _capture_log("error", "API exception in capture.py", extra={"exc": repr(e)})
        _capture_log("error", "proxy lost state error", extra={"exc": repr(e)})
        return False


@router.post("/capture/start")
async def capture_start(request: Request, body: CaptureStartBody | None = None):
    """Start capture: create new session, enable recording.

    Optional auto_stop_seconds (>0): backend starts daemon thread, auto-stop when time arrives.
    Agent exit does not affect timer (held by backend persistent process).
    When capture starts, system proxy is auto-enabled (if not enabled).
    """
    state = request.app.state.telnix
    proxy = state.proxy
    if proxy is None:
        return err("Proxy not started")
    # 自动开启系统代理（若未开启）
    proxy_enabled = False
    try:
        from . import system
        if not system.system_proxy_on and system._enable_proxy_hook:
            system._enable_proxy_hook()
            system.mark_proxy_on(True)
            proxy_enabled = True
    except Exception as e:  # noqa: BLE001
        _capture_log("error", "API exception in capture.py", extra={"exc": repr(e)})
        _capture_log("error", "enable system proxy error", extra={"exc": repr(e)})
        pass
    # 始终复用单一 session：已有会话则重置 ended_at，否则创建新会话
    if state.current_session_id:
        db.reset_session_ended(state.current_session_id)
        session_id = state.current_session_id
    else:
        session_id = db.create_session(
            name=f"Session {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    state.current_session_id = session_id
    proxy.session_id = session_id
    await asyncio.to_thread(proxy.refresh_cert_status)
    proxy.refresh_ignored()
    proxy.capturing = True
    # 抓包代际计数器：每次 capture_start 递增，用于 auto_stop_timer 区分
    # "旧抓包仍在进行"和"新抓包复用了同一 session"（session 复用时 session_id 守卫失效）
    state.capture_generation = getattr(state, "capture_generation", 0) + 1
    # 新抓包会话开始时清空 SSL bump 失败列表（给用户重新安装证书后重试的机会）
    with proxy._ssl_bump_failed_lock:  # noqa: SLF001
        proxy._ssl_bump_failed_hosts.clear()  # noqa: SLF001
    out = {"session_id": session_id, "capturing": True, "system_proxy_on": _system_proxy_state(), "proxy_auto_enabled": proxy_enabled}
    # 后端常驻进程负责定时停止，agent CLI 退出也不会影响
    # 设计修复：auto_stop_seconds 有上限（24小时），防止恶意或误操作设置过大值
    auto_stop = 0.0
    if body and body.auto_stop_seconds:
        auto_stop = body.validated_auto_stop
    if auto_stop > 0:
        out["auto_stop_seconds"] = auto_stop
        gen = state.capture_generation

        def _auto_stop_timer(seconds: float, sid: int, gen: int):
            import time
            time.sleep(seconds)
            # 校验代际未变 + 仍是同一会话 + 仍在抓包，避免停掉复用 session 的新抓包
            if (getattr(state, "capture_generation", 0) == gen
                    and state.current_session_id == sid
                    and proxy.capturing):
                proxy.capturing = False
                db.update_session_ended(sid)
                from .. import logger
                logger.info("capture", f"Auto-stop capture: session_id={sid} (auto_stop {seconds}s expired)")

        t = threading.Thread(target=_auto_stop_timer,
                             args=(auto_stop, session_id, gen), daemon=True)
        t.start()
    return ok(out)


@router.post("/capture/stop")
async def capture_stop(request: Request):
    """Stop capture: end session, but **do not close system proxy**.

    Design: stop just sets capturing to false + ends session, proxy server still runs on port 8888,
    system proxy stays enabled. So:
    - Traffic forwards normally (proxy transparently forwards, no network disconnection)
    - Rules (auto-modify) still take effect (SSL bump + rule matching independent of capturing state)
    - Just no longer records new flows to flows table

    If you need to close system proxy, explicitly call `POST /system/clear-proxy` (CLI `proxy off`).
    """
    state = request.app.state.telnix
    proxy = state.proxy
    if proxy:
        proxy.capturing = False
    if state.current_session_id:
        db.update_session_ended(state.current_session_id)
    return ok({"capturing": False})


@router.post("/capture/pause")
async def capture_pause(request: Request):
    """Pause capture recording (session retained, proxy still runs, unlike stop which ends session)."""
    state = request.app.state.telnix
    proxy = state.proxy
    if proxy:
        proxy.capturing = False
    # 不结束会话，便于 resume 继续
    return ok({"capturing": False, "paused": True, "session_id": state.current_session_id})


@router.post("/capture/resume")
async def capture_resume(request: Request):
    """Resume capture recording (requires existing active session)."""
    state = request.app.state.telnix
    proxy = state.proxy
    if proxy is None:
        return err("Proxy not started")
    if not state.current_session_id:
        return err("No active session, please run capture start first")
    proxy.capturing = True
    return ok({"capturing": True, "session_id": state.current_session_id})


@router.post("/capture/clear")
async def capture_clear(request: Request):
    """Clear flows. If active session exists, only clear that session; otherwise clear all flows.

    First temporarily set capturing=False to cut off new flow enqueue source, then flush async queue + DELETE,
    to avoid new enqueued flows during flush/delete being written after DELETE (residual after clear).
    After clear completes, restore original capturing state (retain "continue capture after clear" semantics).
    """
    state = request.app.state.telnix
    proxy = state.proxy
    # F9 修复：临时停止抓包，切断新 flow 入队（capturing 无锁，但此处写 False 足以让
    # proxy 线程的入队门控 server.py:1766 立即生效，不再产生新 flow）
    was_capturing = bool(proxy.capturing) if proxy else False
    if proxy:
        proxy.capturing = False
    # 排空异步写入队列（此刻起不再有新 flow 入队，flush 可真正排空）
    db.flush_pending_flows(timeout=2.0)
    try:
        if state.current_session_id:
            db.delete_flows(state.current_session_id)
            db.reset_max_flow_id()
            return ok({"cleared": True, "scope": "session", "capturing": was_capturing})
        # 无活动会话：清空所有流量（用户明确点了清空）
        n = db.delete_all_flows()
        db.reset_max_flow_id()
        return ok({"cleared": True, "scope": "all", "deleted": n, "capturing": was_capturing})
    finally:
        # 恢复原抓包状态：若原先在抓包，清空后继续抓新流量（非残留）
        if proxy and was_capturing:
            proxy.capturing = True


# ---------- 触发式捕获（Trigger Capture）----------

from ..trigger import get_trigger_manager, parse_trigger_dsl  # noqa: E402


class TriggerBody(BaseModel):
    """触发条件配置请求体。conditions 为条件列表，dsl 为 DSL 字符串（二选一）。"""
    dsl: str = ""
    conditions: list[dict] | None = None


@router.get("/capture/trigger")
async def get_trigger():
    """获取当前触发式捕获配置。"""
    return ok(get_trigger_manager().get_state())


@router.put("/capture/trigger")
async def set_trigger(body: TriggerBody):
    """配置触发式捕获条件。

    conditions 优先；若未提供则解析 dsl 字符串。
    空条件则禁用触发式捕获（恢复正常记录）。
    """
    tm = get_trigger_manager()
    if body.conditions is not None:
        conds = [{"field": str(c.get("field", "")).lower(), "value": str(c.get("value", ""))}
                 for c in body.conditions if c.get("field") and c.get("value")]
    else:
        conds = parse_trigger_dsl(body.dsl)
    tm.configure(conds)
    return ok(tm.get_state())


@router.post("/capture/trigger/reset")
async def reset_trigger():
    """重置触发状态（已触发标记归零，但保留条件）。"""
    tm = get_trigger_manager()
    tm.reset()
    return ok(tm.get_state())
