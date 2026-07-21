"""日志 API：查询、清空、导出日志。"""

from fastapi import APIRouter, Query
from fastapi.responses import PlainTextResponse

from .. import logger
from . import ok

router = APIRouter()


@router.get("/logs")
async def get_logs(
    level: str | None = Query(None),
    category: str | None = Query(None),
    keyword: str | None = Query(None),
    limit: int = Query(500),
    offset: int = Query(0),
):
    """查询日志。"""
    entries, total = logger.get_logs(
        level=level, category=category, keyword=keyword,
        limit=limit, offset=offset,
    )
    return ok({"entries": entries, "total": total})


@router.delete("/logs")
async def clear_logs():
    """清空日志。"""
    logger.clear_logs()
    return ok({"cleared": True})


@router.get("/logs/export", response_class=PlainTextResponse)
async def export_logs(
    level: str | None = Query(None),
    category: str | None = Query(None),
    keyword: str | None = Query(None),
):
    """导出日志为 JSONL 格式。"""
    content = logger.export_logs(level=level, category=category, keyword=keyword)
    return PlainTextResponse(content, media_type="application/x-jsonlines")
