"""API module common utilities: unified response format.

All APIs return {"code": 0, "data": ..., "msg": "..."} format.
"""

from typing import Any


def ok(data: Any = None, msg: str = "ok") -> dict:
    """Success response."""
    return {"code": 0, "data": data, "msg": msg}


def err(msg: str = "error", code: int = -1, data: Any = None) -> dict:
    """Failed response."""
    return {"code": code, "data": data, "msg": msg}
