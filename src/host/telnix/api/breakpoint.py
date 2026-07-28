"""断点控制 API：开/关/状态。支持超时（避免 agent 忘了放行导致连接永久阻塞）。"""

from fastapi import APIRouter, Request
from pydantic import BaseModel

from .. import db
from . import err, ok

router = APIRouter()


class BreakpointToggle(BaseModel):
    enabled: bool
    timeout: float | None = None  # 断点超时秒数（0 或 null = 永不超时，保持旧行为）


@router.post("/breakpoint/request")
async def toggle_request_break(body: BreakpointToggle, request: Request):
    """开启/关闭请求断点。可选 timeout（秒），超时自动放行。"""
    proxy = request.app.state.telnix.proxy
    if proxy is None:
        return err("代理未启动")
    timeout = body.timeout if body.timeout is not None else 0.0
    proxy.breakpoint.set_request(body.enabled, timeout=timeout)
    db.set_setting("break_on_request", "1" if body.enabled else "0")
    if body.timeout is not None:
        db.set_setting("breakpoint_timeout", str(body.timeout))
    return ok(proxy.breakpoint.status())


@router.post("/breakpoint/response")
async def toggle_response_break(body: BreakpointToggle, request: Request):
    """开启/关闭响应断点。可选 timeout（秒），超时自动放行。"""
    proxy = request.app.state.telnix.proxy
    if proxy is None:
        return err("代理未启动")
    timeout = body.timeout if body.timeout is not None else 0.0
    proxy.breakpoint.set_response(body.enabled, timeout=timeout)
    db.set_setting("break_on_response", "1" if body.enabled else "0")
    if body.timeout is not None:
        db.set_setting("breakpoint_timeout", str(body.timeout))
    return ok(proxy.breakpoint.status())


@router.post("/breakpoint/timeout")
async def set_breakpoint_timeout(body: dict, request: Request):
    """单独设置断点超时（不影响开关状态）。body: {timeout: float}。"""
    proxy = request.app.state.telnix.proxy
    if proxy is None:
        return err("代理未启动")
    seconds = float(body.get("timeout", 0))
    proxy.breakpoint.set_timeout(seconds)
    db.set_setting("breakpoint_timeout", str(seconds))
    return ok(proxy.breakpoint.status())


@router.get("/breakpoint/status")
async def breakpoint_status(request: Request):
    """断点状态 + 暂停中的流量列表（含每个 pending 的等待时长）。"""
    proxy = request.app.state.telnix.proxy
    if proxy is None:
        return err("代理未启动")
    pending = db.get_pending_breakpoint_flows()
    return ok({**proxy.breakpoint.status(), "pending_flows": pending})
