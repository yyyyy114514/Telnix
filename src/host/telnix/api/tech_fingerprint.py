"""Tech stack fingerprinting API: identify tech stack used by target site based on traffic response content."""

from fastapi import APIRouter

from .. import db
from ..logger import _capture_log
from ..tech_fingerprint import fingerprint_flow
from . import err, ok

router = APIRouter()


@router.get("/flows/{flow_id}/tech-fingerprint")
async def get_tech_fingerprint(flow_id: int):
    """Identify tech stack of specified flow.

    Returns [{name, category, confidence, version?}] sorted by category, deduplicated.
    category: server / language / framework / frontend / cms / cdn_waf / analytics / build_tool
    confidence: high / medium / low
    """
    flow = db.get_flow(flow_id)
    if not flow:
        return err("Flow not found")
    try:
        result = fingerprint_flow(flow)
        return ok({"items": result, "count": len(result)})
    except Exception as e:  # noqa: BLE001
        return err(f"Fingerprint failed: {e}")
