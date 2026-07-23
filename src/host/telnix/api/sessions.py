"""会话与流量查询 API。"""

import asyncio
import json

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from .. import db
from . import err, ok

router = APIRouter()


@router.get("/sessions")
async def list_sessions():
    """会话列表（含 flow_count）。"""
    sessions = db.get_sessions()
    # §4.5 批量统计每个会话的流量数，避免前端逐个 sessions show 查询
    counts = db.count_flows_batch([s["id"] for s in sessions if s.get("id")])
    for s in sessions:
        s["flow_count"] = counts.get(s.get("id"), 0)
    return ok(sessions)


@router.get("/sessions/{session_id}")
async def get_session_detail(session_id: int):
    """会话详情（含流量数、时长）。"""
    session = db.get_session(session_id)
    if not session:
        return err("会话不存在")
    flow_count = db.count_flows(session_id)
    session["flow_count"] = flow_count
    return ok(session)


@router.delete("/sessions/{session_id}")
async def delete_session(session_id: int):
    """删除会话及其所有流量。"""
    if not db.get_session(session_id):
        return err("会话不存在")
    deleted = db.delete_session(session_id)
    return ok({"deleted": True, "session_id": session_id, "flows_deleted": deleted})


@router.get("/sessions/{session_id}/flows")
async def list_flows(session_id: int, limit: int = 100, offset: int = 0,
                     host: str = "", process: str = "",
                     status_code: int | None = None, method: str = "",
                     since_id: int = 0, protocol: str = "",
                     tag: str = "", has_tags: bool = False):
    """获取会话流量列表（支持过滤 + 增量查询 + 标签过滤）。

    - tag: 只返回带指定标签的流量（tags 字段 LIKE 匹配）
    - has_tags: True 时只返回 tags 非空的流量
    """
    if not db.get_session(session_id):
        return err("会话不存在")
    flows = db.get_flows(
        session_id, limit=limit, offset=offset,
        host=host or None, process=process or None,
        status_code=status_code, method=method or None,
        since_id=since_id, protocol=protocol or None,
        tag=tag or None,
    )
    if has_tags:
        flows = [f for f in flows if f.get("tags")]
    total = db.count_flows(session_id)
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
    """跨会话查询所有流量（用于全局分析）。不依赖活动会话。

    注意：此路由必须定义在 /flows/{flow_id} 之前，否则 "all" 会被当作 flow_id。
    lite=true 时只返回轻量字段（不含 request_body/response_body/request_headers/response_headers），
    用于全局分析列表加速（选中详情时再单独 GET /flows/{id} 补齐）。
    """
    flows, total = db.get_all_flows(
        limit=limit, offset=offset,
        host=host or None, process=process or None,
        status_code=status_code, method=method or None,
        protocol=protocol or None, since_id=since_id or 0,
        path=path or None, url=url or None,
        tag=tag or None,
        lite=lite,
    )
    if has_tags:
        flows = [f for f in flows if f.get("tags")]
    return ok({"flows": flows, "total": total, "limit": limit, "offset": offset})


@router.get("/flows/stream")
async def stream_flows(request: Request):
    """SSE 推送新流量（lite 字段）。替代前端 500ms 轮询。

    事件格式：data: {"id":..., "method":..., "host":...}\n\n
    客户端用 EventSource 监听，收到后更新 maxFlowId 并插入列表顶部。
    初始连接时立即推送一次 total/max_id，让前端同步基线。
    """
    loop = asyncio.get_event_loop()
    q: asyncio.Queue = asyncio.Queue(maxsize=1000)

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
                    flow = await asyncio.wait_for(q.get(), timeout=15.0)
                    yield f"data: {json.dumps({'type':'flow','flow':flow})}\n\n"
                except asyncio.TimeoutError:
                    # 心跳：保持连接，防止代理/浏览器超时断开
                    yield ": ping\n\n"
        finally:
            db.unregister_flow_subscriber(q)

    return StreamingResponse(event_gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@router.get("/flows/stats")
async def flows_stats(group_by: str = "host",
                      host: str = "", process: str = ""):
    """全量流量分组统计（不分页，用于统计图显示所有数据比例）。

    group_by: host / process / content_type / status_code / method
    返回 {groups: [{key, label, count}], total}
    """
    groups = db.get_flows_stats(
        group_by=group_by,
        host=host or None,
        process=process or None,
    )
    total = sum(g["count"] for g in groups)
    return ok({"groups": groups, "total": total, "group_by": group_by})


@router.get("/flows/overview")
async def flows_overview():
    """跨会话多维聚合统计（一次返回所有维度），供 CoolUI 仪表盘使用。

    性能优化：2 秒 TTL 内存缓存，避免 CoolUI 高频轮询时每秒执行 8 条聚合 SQL。
    返回 {total, total_bytes, incoming_bytes, outgoing_bytes,
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
    _overview_cache["data"] = data
    _overview_cache["ts"] = now
    return ok(data)


# overview 缓存：{data: ..., ts: ...}，2 秒 TTL
_overview_cache: dict = {}


@router.get("/flows/tags")
async def flows_tags_summary():
    """全局标签统计：返回所有流量用过的标签及每标签的 flow 数（§4.2）。

    返回 {tags: [{tag, count}], total_flows_with_tags}
    """
    tags = db.get_all_tags_summary()
    return ok({"tags": tags, "count": len(tags)})


class SetTagsBody(BaseModel):
    """§3.1 流量标签：设置标签（逗号分隔字符串）和可选备注。"""
    tags: str
    tag_note: str | None = None


@router.post("/flows/{flow_id}/tags")
async def set_flow_tags(flow_id: int, body: SetTagsBody):
    """§3.1 设置流量标签（POST 兼容旧客户端）。body: {tags: "analyzed,suspicious", tag_note?: "备注"}"""
    if not db.get_flow(flow_id):
        return err("流量不存在")
    db.update_flow_tags(flow_id, body.tags or "", body.tag_note)
    return ok({"flow_id": flow_id, "tags": body.tags or "",
               "tag_note": body.tag_note or ""})


@router.patch("/flows/{flow_id}/tags")
async def patch_flow_tags(flow_id: int, body: SetTagsBody):
    """§3.1 设置流量标签（PATCH）。body: {tags: "analyzed,suspicious", tag_note?: "备注"}"""
    if not db.get_flow(flow_id):
        return err("流量不存在")
    db.update_flow_tags(flow_id, body.tags or "", body.tag_note)
    return ok({"flow_id": flow_id, "tags": body.tags or "",
               "tag_note": body.tag_note or ""})


class ClearAllFlowsBody(BaseModel):
    """清理流量数据。mode: all=全部, before_id=删除 id<before_id 的旧数据。"""
    mode: str = "all"
    before_id: int | None = None


@router.post("/flows/clear")
async def clear_all_flows(body: ClearAllFlowsBody):
    """清理流量数据（跨会话）。"""
    # 性能优化：异步写入时，清空前先 flush 确保所有 pending 数据已写入
    db.flush_pending_flows(timeout=1.0)
    if body.mode == "before_id" and body.before_id:
        n = db.delete_flows_before(body.before_id)
    else:
        n = db.delete_all_flows()
    return ok({"deleted": n})


@router.get("/flows/{flow_id}")
async def get_flow(flow_id: int):
    """单条流量详情。"""
    flow = db.get_flow(flow_id)
    if not flow:
        return err("流量不存在")
    return ok(flow)


class FlowPatch(BaseModel):
    """断点放行时修改流量字段。"""
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
    """修改流量（断点放行时修改请求/响应字段）。"""
    flow = db.get_flow(flow_id)
    if not flow:
        return err("流量不存在")
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
    """放行断点（action: release 放行 / drop 丢弃）。"""
    state = request.app.state.telnix
    proxy = state.proxy
    if proxy is None:
        return err("代理未启动")
    flow = db.get_flow(flow_id)
    if not flow:
        return err("流量不存在")
    if flow.get("breakpoint_status") is None:
        return err("该流量未处于断点暂停状态")
    ok_flag = proxy.breakpoint.release(flow_id, body.action)
    if not ok_flag:
        return err("未找到该断点（可能已放行）")
    return ok({"flow_id": flow_id, "action": body.action})


class BatchReleaseBody(BaseModel):
    ids: list[int]
    action: str = "release"


@router.post("/flows/batch-release")
async def batch_release_flows(body: BatchReleaseBody, request: Request):
    """批量放行断点。单次 DB 查询获取待放行 id，避免 N+1。"""
    state = request.app.state.telnix
    proxy = state.proxy
    if proxy is None:
        return err("代理未启动")
    # 单次 SQL 查询获取有断点状态的 id 列表（避免循环 get_flow 的 N+1）
    pending_ids = db.get_pending_flow_ids(body.ids)
    released = 0
    for fid in pending_ids:
        if proxy.breakpoint.release(fid, body.action):
            released += 1
    return ok({"released": released, "total": len(body.ids)})


@router.delete("/flows/{flow_id}")
async def delete_flow(flow_id: int):
    """删除单条流量。"""
    db.delete_flow(flow_id)
    return ok({"deleted": True})


class BatchDeleteFlowsBody(BaseModel):
    ids: list[int]


@router.post("/flows/batch-delete")
async def batch_delete_flows(body: BatchDeleteFlowsBody):
    """批量删除流量。"""
    db.delete_flow_batch(body.ids)
    return ok({"deleted": len(body.ids)})

