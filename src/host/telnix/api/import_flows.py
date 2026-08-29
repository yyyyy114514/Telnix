"""Import API: supports importing flow data in JSON and HAR formats.

JSON format (Telnix native export):
    { "session": {...}, "flows": [...] }  or  [...] (pure flows array)

HAR format (HTTP Archive 1.2 standard):
    { "log": { "entries": [...] } }
"""

import json
from datetime import datetime

from fastapi import APIRouter, Body
from pydantic import BaseModel

from .. import db
from ..logger import _capture_log
from . import err, ok

router = APIRouter()


class ImportRequest(BaseModel):
    format: str = "json"  # json | har
    content: str = ""     # File content (string)
    session_name: str = ""  # Optional: session name to import into, auto-generated if empty


def _har_entry_to_flow(entry: dict, session_id: int) -> dict:
    """Convert HAR entry to flow dict."""
    req = entry.get("request", {}) or {}
    resp = entry.get("response", {}) or {}
    method = req.get("method") or "GET"
    url = req.get("url") or ""
    # 解析 url 为 scheme/host/path
    scheme = host = path = ""
    if url:
        if "://" in url:
            scheme, rest = url.split("://", 1)
        else:
            scheme, rest = "http", url
        if "/" in rest:
            host, path_part = rest.split("/", 1)
            path = "/" + path_part
        else:
            host = rest
            path = ""
    # headers: HAR [{name, value}] → JSON 字符串
    req_headers = {}
    for h in req.get("headers", []) or []:
        n = h.get("name")
        if n:
            req_headers[n] = h.get("value", "")
    resp_headers = {}
    for h in resp.get("headers", []) or []:
        n = h.get("name")
        if n:
            resp_headers[n] = h.get("value", "")
    # body
    req_body = ""
    if req.get("postData"):
        req_body = req["postData"].get("text", "") or ""
    resp_body = ""
    resp_size = 0
    if resp.get("content"):
        resp_body = resp["content"].get("text", "") or ""
        resp_size = resp["content"].get("size", 0) or 0
        # HAR 中 size 可能缺失或为 -1，优先用实际 body 长度
        if resp_body and resp_size <= 0:
            resp_size = len(resp_body.encode("utf-8", errors="replace"))
    return {
        "session_id": session_id,
        "timestamp": entry.get("startedDateTime") or datetime.now().isoformat(),
        "pid": None,
        "process_name": "",
        "method": method,
        "url": url,
        "scheme": scheme,
        "host": host,
        "path": path,
        "request_headers": json.dumps(req_headers, ensure_ascii=False),
        "request_body": req_body,
        "status_code": resp.get("status") if resp.get("status") else None,
        "response_headers": json.dumps(resp_headers, ensure_ascii=False),
        "response_body": resp_body,
        "duration_ms": int(entry.get("time") or 0),
        "size": resp_size,
        "protocol": "http",
    }


def _telnix_flow_to_flow(flow: dict, session_id: int) -> dict:
    """Convert Telnix exported flow dict (may contain extra fields like id/session_id) to insertable flow."""
    # 允许缺失字段，用默认值补齐
    return {
        "session_id": session_id,
        "timestamp": flow.get("timestamp") or datetime.now().isoformat(),
        "pid": flow.get("pid"),
        "process_name": flow.get("process_name") or "",
        "method": flow.get("method") or "GET",
        "url": flow.get("url") or "",
        "scheme": flow.get("scheme") or "",
        "host": flow.get("host") or "",
        "path": flow.get("path") or "",
        "request_headers": flow.get("request_headers") or "{}",
        "request_body": flow.get("request_body") or "",
        "status_code": flow.get("status_code"),
        "response_headers": flow.get("response_headers") or "{}",
        "response_body": flow.get("response_body") or "",
        "duration_ms": flow.get("duration_ms") or 0,
        "size": flow.get("size") or 0,
        "protocol": flow.get("protocol") or "http",
    }


@router.post("/import")
async def import_flows(body: ImportRequest = Body(...)):
    """Import flow data (JSON or HAR format).

    Auto-creates new session, inserts all flows. Returns new session ID and imported count.
    """
    if not body.content:
        return err("Content is empty")
    fmt = (body.format or "json").lower()
    try:
        data = json.loads(body.content)
    except json.JSONDecodeError as e:
        return err(f"JSON parse failed: {e}")

    # 创建新会话
    sname = body.session_name or f"Import {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    session_id = db.create_session(sname)

    flows_to_insert: list[dict] = []
    if fmt == "har":
        entries = (data.get("log") or {}).get("entries") or []
        for entry in entries:
            flows_to_insert.append(_har_entry_to_flow(entry, session_id))
    else:
        # JSON 格式：可能是 {session, flows} 或纯 flows 数组
        if isinstance(data, dict):
            flows_list = data.get("flows") or []
        elif isinstance(data, list):
            flows_list = data
        else:
            return err("Unrecognized JSON format: expected {session, flows} or flows array")
        for f in flows_list:
            flows_to_insert.append(_telnix_flow_to_flow(f, session_id))

    if not flows_to_insert:
        # 没有流量，删掉刚建的空会话
        db.delete_session(session_id)
        return err("No flows found to import")

    # 批量插入（单事务 + SAVEPOINT 失败隔离，远快于逐条 insert + 逐条提交）
    inserted = db.insert_flows_batch(flows_to_insert)

    return ok({
        "session_id": session_id,
        "session_name": sname,
        "imported": inserted,
        "total": len(flows_to_insert),
    }, msg=f"Imported {inserted} flows to session #{session_id}")
