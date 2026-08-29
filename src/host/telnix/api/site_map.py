"""Site Map API: tree structure of visited URLs (Burp Suite-style)."""

from fastapi import APIRouter

from .. import db
from ..logger import _capture_log
from . import ok

router = APIRouter()


@router.get("/site-map")
async def get_site_map():
    """Return the site map tree structure.

    Aggregates all captured flows by host and URL path segments, returning a
    nested tree so the frontend can render the site hierarchy with el-tree.
    """
    return ok(db.get_site_map())
