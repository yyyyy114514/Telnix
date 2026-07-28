"""弱网模拟（Throttle）API：模拟高延迟、低带宽、丢包等网络环境。

设置项（存 settings.json）：
- throttle_enabled (0/1)
- throttle_latency_ms：每连接延迟毫秒数（模拟 RTT）
- throttle_bps_kbps：限速 KB/s（0=不限速）
- throttle_drop_pct：丢包率 0-100（仅隧道流量生效）
"""
from fastapi import APIRouter
from pydantic import BaseModel

from .. import settings_store
from ..proxy import throttle as throttle_mod
from . import ok

router = APIRouter()


class ThrottleConfig(BaseModel):
    """弱网配置。所有字段可选，未传不动。"""
    enabled: bool | None = None
    latency_ms: int | None = None
    bps_kbps: int | None = None
    drop_pct: int | None = None


@router.get("/throttle")
async def get_throttle():
    """读取当前弱网配置。"""
    return ok(throttle_mod.get_config())


@router.put("/throttle")
async def set_throttle(body: ThrottleConfig):
    """更新弱网配置（热生效，无需重启）。"""
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
