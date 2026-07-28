"""技术栈识别 API：根据流量响应内容识别目标站点使用的技术栈。"""

from fastapi import APIRouter

from .. import db
from ..tech_fingerprint import fingerprint_flow
from . import err, ok

router = APIRouter()


@router.get("/flows/{flow_id}/tech-fingerprint")
async def get_tech_fingerprint(flow_id: int):
    """识别指定流量的技术栈。

    返回 [{name, category, confidence, version?}] 按类别排序，去重。
    category: server / language / framework / frontend / cms / cdn_waf / analytics / build_tool
    confidence: high / medium / low
    """
    flow = db.get_flow(flow_id)
    if not flow:
        return err("流量不存在")
    try:
        result = fingerprint_flow(flow)
        return ok({"items": result, "count": len(result)})
    except Exception as e:  # noqa: BLE001
        return err(f"识别失败: {e}")
