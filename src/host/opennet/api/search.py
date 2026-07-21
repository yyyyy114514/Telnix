"""搜索与统计 API：跨 body 正则搜索 + 流量分组统计。"""

import base64

from fastapi import APIRouter
from pydantic import BaseModel

from .. import db
from . import err, ok

router = APIRouter()


class SearchRequest(BaseModel):
    session_id: int = 0  # 0 表示跨所有会话搜索
    body_regex: str | None = None
    binary_hex: str | None = None
    limit: int = 200
    # §3.3 多条件组合搜索（与 body_regex/binary_hex 是 AND 关系）
    header_regex: str | None = None  # 对 request_headers/response_headers 做正则匹配
    method: str | None = None  # 精确匹配（不区分大小写）
    status_code: int | None = None  # 精确匹配
    pid: int | None = None  # 精确匹配
    process_name: str | None = None  # 精确匹配
    # §3.14 hex 偏移范围搜索（仅对 binary_hex 生效）
    offset_start: int | None = None  # 起始字节偏移（含）
    offset_end: int | None = None  # 结束字节偏移（不含）


def _hex_dump(data: bytes, offset: int = 0, length: int = 0) -> str:
    """生成 hex dump 格式字符串。"""
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
    """跨 body 正则/二进制搜索流量。支持多条件组合搜索（§3.3）和 hex 偏移范围（§3.14）。"""
    # 允许只用精确字段过滤（不传 body_regex/binary_hex/header_regex）
    # 原校验保持兼容：若全部条件为空，返回错误
    has_any = (body.body_regex or body.binary_hex or body.header_regex
               or body.method or body.status_code is not None
               or body.pid is not None or body.process_name)
    if not has_any:
        return err("需要至少一个搜索条件（body_regex/binary_hex/header_regex/method/status_code/pid/process_name）")
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
        offset_start=body.offset_start,
        offset_end=body.offset_end,
    )
    return ok({"matches": results, "count": len(results)})


@router.get("/sessions/{session_id}/stats")
async def session_stats(session_id: int):
    """会话流量分组统计。"""
    if not db.get_session(session_id):
        return err("会话不存在")
    return ok(db.stats_flows(session_id))


@router.get("/flows/{flow_id}/hex")
async def flow_hex(flow_id: int, offset: int = 0, length: int = 0,
                   field: str = "response_body"):
    """获取流量的 hex dump（field=request_body|response_body|raw_data）。"""
    flow = db.get_flow(flow_id)
    if not flow:
        return err("流量不存在")
    val = flow.get(field) or ""
    if val.startswith("base64:"):
        try:
            raw = base64.b64decode(val[7:])
        except Exception:  # noqa: BLE001
            return err("base64 解码失败")
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
