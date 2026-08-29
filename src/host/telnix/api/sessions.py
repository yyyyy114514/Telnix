"""Session and flow query API."""

import asyncio
import json

from fastapi import APIRouter, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from .. import db
from ..logger import _capture_log
from . import err, ok

router = APIRouter()


@router.get("/sessions")
async def list_sessions():
    """Session list (including flow_count)."""
    sessions = db.get_sessions()
    # §4.5 批量统计每个会话的流量数，避免前端逐个 sessions show 查询
    counts = db.count_flows_batch([s["id"] for s in sessions if s.get("id")])
    for s in sessions:
        s["flow_count"] = counts.get(s.get("id"), 0)
    return ok(sessions)


@router.get("/sessions/{session_id}")
async def get_session_detail(session_id: int):
    """Session details (including flow count, duration)."""
    session = db.get_session(session_id)
    if not session:
        return err("Session not found")
    flow_count = db.count_flows(session_id)
    session["flow_count"] = flow_count
    return ok(session)


@router.delete("/sessions/{session_id}")
async def delete_session(session_id: int):
    """Delete session and all its flows."""
    if not db.get_session(session_id):
        return err("Session not found")
    deleted = db.delete_session(session_id)
    return ok({"deleted": True, "session_id": session_id, "flows_deleted": deleted})


@router.get("/sessions/{session_id}/flows")
async def list_flows(session_id: int, limit: int = 100, offset: int = 0,
                     host: str = "", process: str = "",
                     status_code: int | None = None, method: str = "",
                     since_id: int = 0, protocol: str = "",
                     tag: str = "", has_tags: bool = False,
                     lite: bool = Query(False)):
    """Get session flow list (supports filtering + incremental query + tag filtering).

    - tag: only return flows with specified tag (tags field LIKE match)
    - has_tags: when True, only return flows with non-empty tags
    - lite: when True, only return lightweight fields (excluding request_body/response_body/raw_data),
      for list acceleration (fetch full details separately via GET /flows/{id} when selected).
      Default False for frontend compatibility (returns full fields when frontend doesn't pass lite).
    """
    if not db.get_session(session_id):
        return err("Session not found")
    flows = db.get_flows(
        session_id, limit=limit, offset=offset,
        host=host or None, process=process or None,
        status_code=status_code, method=method or None,
        since_id=since_id, protocol=protocol or None,
        tag=tag or None,
        lite=lite,
        has_tags=has_tags,
    )
    # 性能优化：增量轮询(since_id>0)时跳过 COUNT 查询，前端已有 total
    total = db.count_flows(session_id) if since_id == 0 else None
    max_id = db.get_max_flow_id(session_id)
    return ok({"flows": flows, "total": total, "limit": limit, "offset": offset,
               "max_id": max_id})


@router.get("/flows/all")
async def list_all_flows(limit: int = 200, offset: int = 0,
                         host: str = "", process: str = "",
                         status_code: int | None = None, method: str = "",
                         protocol: str = "", since_id: int = 0,
                         path: str = "", url: str = "", tag: str = "",
                         has_tags: bool = False,
                         lite: bool = False):
    """Query all flows across sessions (for global analysis). Does not depend on active session.

    Note: This route must be defined before /flows/{flow_id}, otherwise "all" would be treated as flow_id.
    When lite=true, only returns lightweight fields (excluding request_body/response_body/request_headers/response_headers),
    for global analysis list acceleration (fetch full details separately via GET /flows/{id} when selected).
    """
    flows, total = db.get_all_flows(
        limit=limit, offset=offset,
        host=host or None, process=process or None,
        status_code=status_code, method=method or None,
        protocol=protocol or None, since_id=since_id or 0,
        path=path or None, url=url or None,
        tag=tag or None,
        lite=lite,
        skip_total=bool(since_id),
        has_tags=has_tags,
    )
    return ok({"flows": flows, "total": total, "limit": limit, "offset": offset})


@router.get("/flows/stream")
async def stream_flows(request: Request):
    """SSE push new flows (lite fields). Replaces frontend 500ms polling.

    Event format: data: {"id":..., "method":..., "host":...}\n\n
    Client listens with EventSource, updates maxFlowId and inserts at list top upon receipt.
    On initial connection, immediately pushes total/max_id once for frontend to sync baseline.

    Design fix: heartbeat interval is configurable via settings (default 10s, max 15s).
    """
    loop = asyncio.get_running_loop()
    q: asyncio.Queue = asyncio.Queue(maxsize=2000)

    # 从 settings 读取心跳间隔，默认 10 秒（不超过 15 秒以防被代理关闭）
    try:
        from .. import settings_store
        heartbeat_interval = settings_store.get_setting("sse_heartbeat_interval", 10.0)
        heartbeat_interval = min(max(float(heartbeat_interval), 1.0), 15.0)  # 限制范围 1-15s
    except Exception:  # noqa: BLE001
        heartbeat_interval = 10.0

    async def event_gen():
        db.register_flow_subscriber(q, loop)
        try:
            # 初始事件：推送当前 total 和 max_id（前端用于同步基线）
            max_id = db.get_max_flow_id_all()
            yield f"data: {json.dumps({'type':'init','max_id':max_id})}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    flow = await asyncio.wait_for(q.get(), timeout=heartbeat_interval)
                    yield f"data: {json.dumps({'type':'flow','flow':flow})}\n\n"
                except asyncio.TimeoutError:
                    # 心跳：保持连接，防止代理/浏览器超时断开。
                    # 用 data: 形式（而非注释 : ping）以便前端 onmessage 收到后能刷新
                    # SSE 活跃时间戳，避免「健康但空闲」时被误判为假死而频繁兜底轮询。
                    yield f"data: {json.dumps({'type':'ping'})}\n\n"
        finally:
            db.unregister_flow_subscriber(q)

    return StreamingResponse(event_gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@router.get("/flows/stats")
async def flows_stats(group_by: str = "host",
                      host: str = "", process: str = ""):
    """Full flow group statistics (no pagination, for chart display of all data proportions).

    group_by: host / process / content_type / status_code / method
    Returns {groups: [{key, label, count}], total}

    Performance optimization: 2-second TTL memory cache, to avoid repeated aggregate SQL execution when charts poll frequently.
    Cache key includes all query parameters, to avoid wrong results for different parameters (fixes audit 7.1).
    """
    import time
    now = time.time()
    # 缓存 key 包含 group_by/host/process，避免不同参数命中同一缓存
    cache_key = (group_by, host or "", process or "")
    cached = _stats_cache.get(cache_key)
    if cached is not None and (now - cached["ts"]) < 2.0:
        return cached["resp"]
    groups = db.get_flows_stats(
        group_by=group_by,
        host=host or None,
        process=process or None,
    )
    total = sum(g["count"] for g in groups)
    resp = ok({"groups": groups, "total": total, "group_by": group_by})
    # 直接整体替换引用，避免 clear+update 之间的 read-then-write 竞态
    _stats_cache[cache_key] = {"resp": resp, "ts": now}
    # LRU 清理：缓存条目超过 32 个时删除最早的
    if len(_stats_cache) > 32:
        oldest_key = min(_stats_cache.keys(), key=lambda k: _stats_cache[k]["ts"])
        _stats_cache.pop(oldest_key, None)
    return resp


@router.get("/flows/endpoint-stats")
async def flows_endpoint_stats(session_id: int | None = None, limit: int = 2000):
    """Endpoint statistics with path template normalization (server-side aggregation).

    Design fix: moved from cli.py client-side processing to backend API.
    Aggregates flows by normalized path templates to avoid transferring all flows to client.

    Returns {endpoints: [{method, host, path_template, count, status_codes, sample_ids, query_keys}]}
    """
    endpoints = db.get_flows_endpoint_stats(session_id=session_id, limit=limit)
    return ok({"endpoints": endpoints, "count": len(endpoints)})


# stats 缓存：{(group_by, host, process): {"resp": ..., "ts": ...}}，2 秒 TTL
# 修复审计 7.1：原缓存 key 仅时间戳，不同参数会返回错误结果
_stats_cache: dict = {}


@router.get("/flows/topology")
async def flows_topology(host: str = "", process: str = "", max_nodes: int = 100):
    """网络拓扑数据（process → IP → host 连接关系图）。"""
    data = db.get_network_topology(
        host=host or None,
        process=process or None,
        max_nodes=max_nodes,
    )
    return ok(data)


@router.get("/flows/heatmap")
async def flows_heatmap(group_by: str = "host",
                        bucket_seconds: int = 60,
                        max_buckets: int = 120,
                        top_n: int = 20,
                        host: str = "",
                        process: str = ""):
    """2D 热力图聚合数据（时间分桶 × 维度分组）。

    group_by: host / process / method / status_range / ip_region
    bucket_seconds: 时间桶大小（秒）
    max_buckets: 最多返回多少个时间桶（从最新往前）
    top_n: 维度方向最多返回 top N
    Returns {buckets, dimensions, matrix, ...}
    """
    import time as _t
    now = _t.time()
    cache_key = ("heatmap", group_by, bucket_seconds, max_buckets, top_n, host or "", process or "")
    cached = _stats_cache.get(cache_key)
    if cached is not None and (now - cached["ts"]) < 2.0:
        return cached["resp"]
    data = db.get_flows_heatmap(
        group_by=group_by,
        bucket_seconds=bucket_seconds,
        max_buckets=max_buckets,
        top_n=top_n,
        host=host or None,
        process=process or None,
    )
    resp = ok(data)
    _stats_cache[cache_key] = {"resp": resp, "ts": now}
    if len(_stats_cache) > 32:
        oldest_key = min(_stats_cache.keys(), key=lambda k: _stats_cache[k]["ts"])
        _stats_cache.pop(oldest_key, None)
    return resp


@router.get("/flows/overview")
async def flows_overview():
    """Cross-session multi-dimensional aggregate statistics (returns all dimensions at once), for CoolUI dashboard.

    Performance optimization: 2-second TTL memory cache, to avoid executing 8 aggregate SQL queries per second when CoolUI polls frequently.
    Returns {total, total_bytes, incoming_bytes, outgoing_bytes,
          success_count, error_count, avg_duration_ms,
          by_protocol, by_method, by_status_range,
          by_host, by_process, by_ip_region}
    """
    import time
    now = time.time()
    cached = _overview_cache.get("data")
    cached_ts = _overview_cache.get("ts", 0)
    if cached is not None and (now - cached_ts) < 2.0:
        return ok(cached)
    data = db.get_flows_overview()
    # 整体替换 dict 引用（非 clear+update），避免并发 read-then-write 竞态
    _overview_cache.clear()
    _overview_cache["data"] = data
    _overview_cache["ts"] = now
    return ok(data)


# overview 缓存：{data: ..., ts: ...}，2 秒 TTL
_overview_cache: dict = {}


@router.get("/flows/tags")
async def flows_tags_summary():
    """Global tag statistics: returns all tags used by flows and flow count per tag (§4.2).

    Returns {tags: [{tag, count}], total_flows_with_tags}
    """
    tags = db.get_all_tags_summary()
    return ok({"tags": tags, "count": len(tags)})


class CreateSessionBody(BaseModel):
    """Create session body."""
    name: str | None = None
    color: str | None = None


class UpdateSessionBody(BaseModel):
    """Update session body."""
    name: str | None = None
    color: str | None = None


@router.post("/sessions")
async def create_session_api(body: CreateSessionBody, request: Request):
    """Create a new session and switch to it as the active session."""
    session_id = db.create_session(body.name, body.color)
    state = request.app.state.telnix
    state.current_session_id = session_id
    if state.proxy:
        state.proxy.session_id = session_id
    session = db.get_session(session_id)
    return ok(session)


@router.patch("/sessions/{session_id}")
async def update_session_api(session_id: int, body: UpdateSessionBody):
    """Update session name and/or color."""
    if not db.get_session(session_id):
        return err("Session not found")
    db.update_session(session_id, body.name, body.color)
    return ok(db.get_session(session_id))


@router.post("/sessions/{session_id}/switch")
async def switch_session_api(session_id: int, request: Request):
    """Switch to the specified session."""
    if not db.get_session(session_id):
        return err("Session not found")
    state = request.app.state.telnix
    state.current_session_id = session_id
    if state.proxy:
        state.proxy.session_id = session_id
    return ok({"session_id": session_id})


class SetTagsBody(BaseModel):
    """§3.1 Flow tags: set tags (comma-separated string) and optional note."""
    tags: str
    tag_note: str | None = None


@router.post("/flows/{flow_id}/tags")
async def set_flow_tags(flow_id: int, body: SetTagsBody):
    """§3.1 Set flow tags (POST for old client compatibility). body: {tags: "analyzed,suspicious", tag_note?: "note"}"""
    if not db.get_flow(flow_id):
        return err("Flow not found")
    db.update_flow_tags(flow_id, body.tags or "", body.tag_note)
    return ok({"flow_id": flow_id, "tags": body.tags or "",
               "tag_note": body.tag_note or ""})


@router.patch("/flows/{flow_id}/tags")
async def patch_flow_tags(flow_id: int, body: SetTagsBody):
    """§3.1 Set flow tags (PATCH). body: {tags: "analyzed,suspicious", tag_note?: "note"}"""
    if not db.get_flow(flow_id):
        return err("Flow not found")
    db.update_flow_tags(flow_id, body.tags or "", body.tag_note)
    return ok({"flow_id": flow_id, "tags": body.tags or "",
               "tag_note": body.tag_note or ""})


class ClearAllFlowsBody(BaseModel):
    """Clear flow data. mode: all=all, current=current session, before_id=delete old data with id<before_id."""
    mode: str = "all"
    before_id: int | None = None


@router.post("/flows/clear")
async def clear_all_flows(body: ClearAllFlowsBody):
    """Clear flow data (cross-session)."""
    # 性能优化：异步写入时，清空前先 flush 确保所有 pending 数据已写入
    db.flush_pending_flows(timeout=1.0)
    if body.mode == "before_id" and body.before_id:
        n = db.delete_flows_before(body.before_id)
    elif body.mode == "current":
        session_id = db.get_current_session_id()
        n = db.delete_flows_in_session(session_id)
    else:
        n = db.delete_all_flows()
    return ok({"deleted": n})


@router.get("/flows/{flow_id}")
async def get_flow(flow_id: int):
    """Single flow details."""
    flow = db.get_flow(flow_id)
    if not flow:
        return err("Flow not found")
    return ok(flow)


class FlowPatch(BaseModel):
    """Modify flow fields when releasing breakpoint."""
    method: str | None = None
    url: str | None = None
    host: str | None = None
    path: str | None = None
    request_headers: dict | None = None
    request_body: str | None = None
    status_code: int | None = None
    response_headers: dict | None = None
    response_body: str | None = None


@router.patch("/flows/{flow_id}")
async def patch_flow(flow_id: int, body: FlowPatch):
    """Modify flow (modify request/response fields when releasing breakpoint)."""
    flow = db.get_flow(flow_id)
    if not flow:
        return err("Flow not found")
    if body.method is not None or body.url is not None or body.host is not None \
            or body.path is not None or body.request_headers is not None \
            or body.request_body is not None:
        db.update_flow_request(
            flow_id,
            body.method or flow["method"],
            body.url or flow["url"],
            body.host or flow["host"],
            body.path or flow["path"],
            json.dumps(body.request_headers) if body.request_headers is not None
            else flow["request_headers"],
            body.request_body if body.request_body is not None
            else flow["request_body"],
        )
    if body.status_code is not None or body.response_headers is not None \
            or body.response_body is not None:
        db.update_flow_response_fields(
            flow_id,
            body.status_code if body.status_code is not None else flow["status_code"],
            json.dumps(body.response_headers) if body.response_headers is not None
            else flow["response_headers"],
            body.response_body if body.response_body is not None
            else flow["response_body"],
        )
    return ok(db.get_flow(flow_id))


class ReleaseBody(BaseModel):
    action: str = "release"  # release | drop


@router.post("/flows/{flow_id}/release")
async def release_flow(flow_id: int, body: ReleaseBody, request: Request):
    """Release breakpoint (action: release / drop)."""
    state = request.app.state.telnix
    proxy = state.proxy
    if proxy is None:
        return err("Proxy not started")
    flow = db.get_flow(flow_id)
    if not flow:
        return err("Flow not found")
    if flow.get("breakpoint_status") is None:
        return err("This flow is not in breakpoint pause state")
    ok_flag = proxy.breakpoint.release(flow_id, body.action)
    if not ok_flag:
        return err("Breakpoint not found (may have been released)")
    return ok({"flow_id": flow_id, "action": body.action})


class BatchReleaseBody(BaseModel):
    ids: list[int]
    action: str = "release"


@router.post("/flows/batch-release")
async def batch_release_flows(body: BatchReleaseBody, request: Request):
    """Batch release breakpoints. Single DB query to get pending ids, avoids N+1."""
    state = request.app.state.telnix
    proxy = state.proxy
    if proxy is None:
        return err("Proxy not started")
    # 单次 SQL 查询获取有断点状态的 id 列表（避免循环 get_flow 的 N+1）
    pending_ids = db.get_pending_flow_ids(body.ids)
    released = 0
    for fid in pending_ids:
        if proxy.breakpoint.release(fid, body.action):
            released += 1
    return ok({"released": released, "total": len(body.ids)})


@router.delete("/flows/{flow_id}")
async def delete_flow(flow_id: int):
    """Delete single flow."""
    db.delete_flow(flow_id)
    return ok({"deleted": True})


class BatchDeleteFlowsBody(BaseModel):
    ids: list[int]


@router.post("/flows/batch-delete")
async def batch_delete_flows(body: BatchDeleteFlowsBody):
    """Batch delete flows."""
    db.delete_flow_batch(body.ids)
    return ok({"deleted": len(body.ids)})

