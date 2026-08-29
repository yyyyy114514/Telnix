"""Search and statistics API: cross-body regex search + flow group statistics."""

import base64

from fastapi import APIRouter
from pydantic import BaseModel

from .. import db
from ..logger import _capture_log
from . import err, ok

router = APIRouter()


class SearchRequest(BaseModel):
    session_id: int = 0  # 0 means search across all sessions
    body_regex: str | None = None
    binary_hex: str | None = None
    limit: int = 200
    # §3.3 Multi-condition combined search (AND relationship with body_regex/binary_hex)
    header_regex: str | None = None  # Regex match on request_headers/response_headers
    method: str | None = None  # Exact match (case-insensitive)
    status_code: int | None = None  # Exact match
    pid: int | None = None  # Exact match
    process_name: str | None = None  # Exact match
    # Host filter (substring match, case-insensitive)
    host: str | None = None
    # Status code range filter (inclusive)
    status_min: int | None = None
    status_max: int | None = None
    # §3.14 hex offset range search (only effective for binary_hex)
    offset_start: int | None = None  # Starting byte offset (inclusive)
    offset_end: int | None = None  # Ending byte offset (exclusive)


def _hex_dump(data: bytes, offset: int = 0, length: int = 0) -> str:
    """Generate hex dump format string."""
    if length:
        data = data[offset:offset + length]
    else:
        data = data[offset:]
    lines = []
    for i in range(0, len(data), 16):
        chunk = data[i:i + 16]
        hex_part = " ".join(f"{b:02x}" for b in chunk)
        hex_part = hex_part.ljust(48)
        ascii_part = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        lines.append(f"{offset + i:08x}  {hex_part}  |{ascii_part}|")
    return "\n".join(lines)


@router.post("/flows/search")
async def search_flows(body: SearchRequest):
    """Cross-body regex/binary search flows. Supports multi-condition combined search (§3.3) and hex offset range (§3.14)."""
    # 允许只用精确字段过滤（不传 body_regex/binary_hex/header_regex）
    # 原校验保持兼容：若全部条件为空，返回错误
    has_any = (body.body_regex or body.binary_hex or body.header_regex
               or body.method or body.status_code is not None
               or body.pid is not None or body.process_name
               or body.host or body.status_min is not None or body.status_max is not None)
    if not has_any:
        return err("At least one search condition is required (body_regex/binary_hex/header_regex/method/status_code/pid/process_name/host/status_min/status_max)")
    results = db.search_flows(
        body.session_id,
        body_regex=body.body_regex,
        binary_hex=body.binary_hex,
        limit=body.limit,
        header_regex=body.header_regex,
        method=body.method,
        status_code=body.status_code,
        pid=body.pid,
        process_name=body.process_name,
        host=body.host,
        status_min=body.status_min,
        status_max=body.status_max,
        offset_start=body.offset_start,
        offset_end=body.offset_end,
    )
    return ok({"matches": results, "count": len(results)})


@router.get("/sessions/{session_id}/stats")
async def session_stats(session_id: int):
    """Session flow group statistics."""
    if not db.get_session(session_id):
        return err("Session not found")
    return ok(db.stats_flows(session_id))


@router.get("/flows/{flow_id}/hex")
async def flow_hex(flow_id: int, offset: int = 0, length: int = 0,
                   field: str = "response_body"):
    """Get flow's hex dump (field=request_body|response_body|raw_data)."""
    flow = db.get_flow(flow_id)
    if not flow:
        return err("Flow not found")
    val = flow.get(field) or ""
    if val.startswith("base64:"):
        try:
            raw = base64.b64decode(val[7:])
        except Exception as e:  # noqa: BLE001
            _capture_log("error", "API exception in search.py", extra={"exc": repr(e)})
            return err("base64 decode failed")
    else:
        raw = val.encode("utf-8", errors="replace")
    if not raw:
        return ok({"hex": "", "size": 0, "field": field})
    dump = _hex_dump(raw, offset, length)
    return ok({
        "hex": dump,
        "size": len(raw),
        "field": field,
        "offset": offset,
        "length": length,
    })
