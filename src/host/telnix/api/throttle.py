"""Weak network simulation (Throttle) API: simulate high latency, low bandwidth, packet loss and other network environments.

Settings (stored in settings.json):
- throttle_enabled (0/1)
- throttle_latency_ms: per-connection delay in milliseconds (simulates RTT)
- throttle_bps_kbps: speed limit KB/s (0=unlimited)
- throttle_drop_pct: packet loss rate 0-100 (only effective for tunneled traffic)
"""
from fastapi import APIRouter
from pydantic import BaseModel

from .. import settings_store
from ..logger import _capture_log
from ..proxy import throttle as throttle_mod
from . import ok

router = APIRouter()


class ThrottleConfig(BaseModel):
    """Weak network configuration. All fields optional, unchanged if not provided."""
    enabled: bool | None = None
    latency_ms: int | None = None
    bps_kbps: int | None = None
    drop_pct: int | None = None


@router.get("/throttle")
async def get_throttle():
    """Read current weak network configuration."""
    return ok(throttle_mod.get_config())


@router.put("/throttle")
async def set_throttle(body: ThrottleConfig):
    """Update weak network configuration (takes effect immediately, no restart needed)."""
    if body.enabled is not None:
        settings_store.set_setting("throttle_enabled", "1" if body.enabled else "0")
    if body.latency_ms is not None:
        settings_store.set_setting("throttle_latency_ms", str(max(0, body.latency_ms)))
    if body.bps_kbps is not None:
        settings_store.set_setting("throttle_bps_kbps", str(max(0, body.bps_kbps)))
    if body.drop_pct is not None:
        # 丢包率限制 0-100
        settings_store.set_setting("throttle_drop_pct", str(max(0, min(100, body.drop_pct))))
    # 失效本模块缓存（settings_store 内部已通过 mtime 失效，这里只清本模块的二级缓存）
    throttle_mod.invalidate_cache()
    return ok(throttle_mod.get_config())
