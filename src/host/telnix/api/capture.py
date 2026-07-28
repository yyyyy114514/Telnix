"""抓包控制 API：开始/停止/状态/清空。"""

import asyncio
import threading
from datetime import datetime

from fastapi import APIRouter, Request
from pydantic import BaseModel

from .. import db
from . import err, ok

router = APIRouter()


class CaptureStartBody(BaseModel):
    """capture start 请求体。auto_stop_seconds > 0 时后端启动定时器自动停止。"""
    auto_stop_seconds: float | None = None


@router.get("/status")
async def status(request: Request):
    """抓包状态 + 代理状态 + 证书状态 + pinning 疑似进程。"""
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
    })


def _system_proxy_state() -> bool:
    """读取系统代理开关状态（从 system_api 模块）。"""
    try:
        from . import system
        return system.system_proxy_on
    except Exception:  # noqa: BLE001
        return False


def _proxy_lost_state() -> bool:
    """读取代理丢失状态（从 system_api 模块）。"""
    try:
        from . import system
        return system.is_proxy_lost()
    except Exception:  # noqa: BLE001
        return False


@router.post("/capture/start")
async def capture_start(request: Request, body: CaptureStartBody | None = None):
    """开始抓包：创建新会话，开启记录。

    可选 auto_stop_seconds（>0）：后端启动 daemon 线程，到时自动停止。
    agent 退出不影响定时器（由后端常驻进程持有）。
    开始抓包时自动开启系统代理（若未开启）。
    """
    state = request.app.state.telnix
    proxy = state.proxy
    if proxy is None:
        return err("代理未启动")
    # 自动开启系统代理（若未开启）
    proxy_enabled = False
    try:
        from . import system
        if not system.system_proxy_on and system._enable_proxy_hook:
            system._enable_proxy_hook()
            system.mark_proxy_on(True)
            proxy_enabled = True
    except Exception:  # noqa: BLE001
        pass
    # 结束旧会话
    if state.current_session_id:
        db.update_session_ended(state.current_session_id)
    session_id = db.create_session(
        name=f"会话 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    state.current_session_id = session_id
    proxy.session_id = session_id
    await asyncio.to_thread(proxy.refresh_cert_status)
    proxy.refresh_ignored()
    proxy.capturing = True
    # 新抓包会话开始时清空 SSL bump 失败列表（给用户重新安装证书后重试的机会）
    with proxy._ssl_bump_failed_lock:  # noqa: SLF001
        proxy._ssl_bump_failed_hosts.clear()  # noqa: SLF001
    out = {"session_id": session_id, "capturing": True, "system_proxy_on": _system_proxy_state(), "proxy_auto_enabled": proxy_enabled}
    # 后端常驻进程负责定时停止，agent CLI 退出也不会影响
    auto_stop = float(body.auto_stop_seconds) if body and body.auto_stop_seconds else 0.0
    if auto_stop > 0:
        out["auto_stop_seconds"] = auto_stop

        def _auto_stop_timer(seconds: float, sid: int):
            import time
            time.sleep(seconds)
            # 仅当仍是同一个会话且仍在抓包时才停止（避免停掉后续新会话）
            if state.current_session_id == sid and proxy.capturing:
                proxy.capturing = False
                db.update_session_ended(sid)
                from .. import logger
                logger.info("capture", f"自动停止抓包：session_id={sid}（auto_stop {seconds}s 到期）")

        t = threading.Thread(target=_auto_stop_timer,
                             args=(auto_stop, session_id), daemon=True)
        t.start()
    return ok(out)


@router.post("/capture/stop")
async def capture_stop(request: Request):
    """停止抓包：结束会话，但**不关闭系统代理**。

    设计：stop 只是把 capturing 置 false + 结束会话，代理服务器仍在 8888 端口运行，
    系统代理保持开启。这样：
    - 流量正常转发（代理透明转发，不会断网）
    - 规则（自动修改）仍生效（SSL bump + 规则匹配独立于 capturing 状态）
    - 只是不再记录新流量到 flows 表

    若需关闭系统代理，显式调 `POST /system/clear-proxy`（CLI `proxy off`）。
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
    """暂停抓包记录（会话保留，代理仍跑，区别于 stop 结束会话）。"""
    state = request.app.state.telnix
    proxy = state.proxy
    if proxy:
        proxy.capturing = False
    # 不结束会话，便于 resume 继续
    return ok({"capturing": False, "paused": True, "session_id": state.current_session_id})


@router.post("/capture/resume")
async def capture_resume(request: Request):
    """恢复抓包记录（需要已有活动会话）。"""
    state = request.app.state.telnix
    proxy = state.proxy
    if proxy is None:
        return err("代理未启动")
    if not state.current_session_id:
        return err("无活动会话，请先 capture start")
    proxy.capturing = True
    return ok({"capturing": True, "session_id": state.current_session_id})


@router.post("/capture/clear")
async def capture_clear(request: Request):
    """清空流量。有活动会话只清该会话，无活动会话清空所有 flows。

    先临时置 capturing=False 切断新 flow 入队源头，再 flush 异步队列 + DELETE，
    避免 flush/delete 期间新入队 flow 在 DELETE 后被写入（清空后残留）。
    清空完成后恢复原 capturing 状态（保留"清空后继续抓"语义）。
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
