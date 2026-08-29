"""Log API: query, clear, export logs, and SSE streaming."""

import json
import asyncio
from fastapi import APIRouter, Query
from fastapi.responses import PlainTextResponse, StreamingResponse

from .. import logger
from ..logger import _capture_log
from . import ok

router = APIRouter()

# detail 字段上限：避免单条日志附带的超长堆栈/包体在序列化与前端渲染时造成卡顿
_LOG_DETAIL_MAX = 4096

# SSE 流式推送间隔（秒）
_SSE_INTERVAL = 0.5


def _truncate_detail(entry: dict) -> dict:
    detail = entry.get("detail") or ""
    if len(detail) > _LOG_DETAIL_MAX:
        entry = dict(entry)
        entry["detail"] = detail[:_LOG_DETAIL_MAX] + "...(truncated)"
    return entry


@router.get("/logs")
async def get_logs(
    level: str | None = Query(None),
    category: str | None = Query(None),
    keyword: str | None = Query(None),
    limit: int = Query(500),
    offset: int = Query(0),
):
    """Query logs."""
    entries, total = logger.get_logs(
        level=level, category=category, keyword=keyword,
        limit=limit, offset=offset,
    )
    entries = [_truncate_detail(e) for e in entries]
    return ok({"entries": entries, "total": total})

@router.delete("/logs")
async def clear_logs():
    """Clear logs."""
    logger.clear_logs()
    return ok({"cleared": True})


@router.get("/logs/export", response_class=PlainTextResponse)
async def export_logs(
    level: str | None = Query(None),
    category: str | None = Query(None),
    keyword: str | None = Query(None),
):
    """Export logs as JSONL format."""
    content = logger.export_logs(level=level, category=category, keyword=keyword)
    return PlainTextResponse(content, media_type="application/x-jsonlines")


@router.get("/logs/stats")
async def get_log_stats():
    """获取日志统计数据（用于仪表盘）。"""
    return ok(logger.get_stats())


# ---------- SSE 流式日志推送 ----------

@router.get("/logs/stream")
async def stream_logs():
    """SSE 流式推送新日志条目（实时监控）。

    客户端可通过 EventSource API 订阅：
    ```js
    const es = new EventSource('/api/logs/stream')
    es.addEventListener('log', (e) => {
      const entry = JSON.parse(e.data)
      // 处理新日志条目
    })
    es.addEventListener('stats', (e) => {
      const stats = JSON.parse(e.data)
      // 处理统计数据
    })
    ```
    """
    async def event_generator():
        last_counter = logger.get_counter()

        while True:
            await asyncio.sleep(_SSE_INTERVAL)

            current_counter = logger.get_counter()
            if current_counter > last_counter:
                # 获取新增的日志条目
                count = current_counter - last_counter
                entries, _ = logger.get_logs(limit=10000)
                new_entries = entries[-count:] if len(entries) >= count else entries

                for entry in reversed(new_entries):
                    truncated = _truncate_detail(entry)
                    yield f"event: log\ndata: {json.dumps(truncated, ensure_ascii=False)}\n\n".encode("utf-8")

                last_counter = current_counter

            # 定期推送统计数据（用于前端统计面板）
            stats = logger.get_stats()
            yield f"event: stats\ndata: {json.dumps(stats, ensure_ascii=False)}\n\n".encode("utf-8")

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
