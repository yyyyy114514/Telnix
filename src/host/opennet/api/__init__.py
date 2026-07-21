"""API 模块公共工具：统一响应格式。

所有 API 返回 {"code": 0, "data": ..., "msg": "..."} 格式。
"""

from typing import Any


def ok(data: Any = None, msg: str = "ok") -> dict:
    """成功响应。"""
    return {"code": 0, "data": data, "msg": msg}


def err(msg: str = "error", code: int = -1, data: Any = None) -> dict:
    """失败响应。"""
    return {"code": code, "data": data, "msg": msg}
