"""Breakpoint control API: on/off/status. Supports timeout (to avoid agent forgetting to release, causing permanent connection blocking)."""

from fastapi import APIRouter, Request
from pydantic import BaseModel

from .. import db
from ..logger import _capture_log
from . import err, ok

router = APIRouter()


class BreakpointToggle(BaseModel):
    enabled: bool
    timeout: float | None = None  # 断点超时秒数（0 或 null = 永不超时，保持旧行为）


@router.post("/breakpoint/request")
async def toggle_request_break(body: BreakpointToggle, request: Request):
    """Enable/disable request breakpoint. Optional timeout (seconds), auto-release on timeout."""
    proxy = request.app.state.telnix.proxy
    if proxy is None:
        return err("Proxy not started")
    timeout = body.timeout if body.timeout is not None else 0.0
    proxy.breakpoint.set_request(body.enabled, timeout=timeout)
    db.set_setting("break_on_request", "1" if body.enabled else "0")
    if body.timeout is not None:
        db.set_setting("breakpoint_timeout", str(body.timeout))
    return ok(proxy.breakpoint.status())


@router.post("/breakpoint/response")
async def toggle_response_break(body: BreakpointToggle, request: Request):
    """Enable/disable response breakpoint. Optional timeout (seconds), auto-release on timeout."""
    proxy = request.app.state.telnix.proxy
    if proxy is None:
        return err("Proxy not started")
    timeout = body.timeout if body.timeout is not None else 0.0
    proxy.breakpoint.set_response(body.enabled, timeout=timeout)
    db.set_setting("break_on_response", "1" if body.enabled else "0")
    if body.timeout is not None:
        db.set_setting("breakpoint_timeout", str(body.timeout))
    return ok(proxy.breakpoint.status())


@router.post("/breakpoint/timeout")
async def set_breakpoint_timeout(body: dict, request: Request):
    """Set breakpoint timeout alone (does not affect switch state). body: {timeout: float}."""
    proxy = request.app.state.telnix.proxy
    if proxy is None:
        return err("Proxy not started")
    seconds = float(body.get("timeout", 0))
    proxy.breakpoint.set_timeout(seconds)
    db.set_setting("breakpoint_timeout", str(seconds))
    return ok(proxy.breakpoint.status())


@router.get("/breakpoint/status")
async def breakpoint_status(request: Request):
    """Breakpoint status + list of paused flows (including wait duration for each pending)."""
    proxy = request.app.state.telnix.proxy
    if proxy is None:
        return err("Proxy not started")
    pending = db.get_pending_breakpoint_flows()
    return ok({**proxy.breakpoint.status(), "pending_flows": pending})
