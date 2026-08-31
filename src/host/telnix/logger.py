"""内存日志系统：记录操作、警告、错误，支持导出。

日志存在内存环形缓冲区中，最多保留 10000 条。
支持按级别和关键词筛选，可导出为文件。
"""

import json
import threading
from collections import deque
from datetime import datetime

# 日志级别
DEBUG = "DEBUG"
INFO = "INFO"
WARNING = "WARNING"
ERROR = "ERROR"

# 级别排序（用于 is_enabled 过滤）
_LEVEL_ORDER = {DEBUG: 0, INFO: 1, WARNING: 2, ERROR: 3}
_min_level = 0  # 默认 DEBUG，记录所有级别

# 环形缓冲区
_MAX_ENTRIES = 10000
_buffer: deque = deque(maxlen=_MAX_ENTRIES)
_lock = threading.Lock()
_counter = 0


def set_min_level(level: str) -> None:
    """Set the minimum recording level (logs below this level are not recorded, for performance)."""
    global _min_level
    _min_level = _LEVEL_ORDER.get(level, 0)


def is_enabled(level: str) -> bool:
    """Check whether the given level will be recorded (used to guard expensive detail computation)."""
    return _LEVEL_ORDER.get(level, 0) >= _min_level


def log(level: str, category: str, message: str, detail: str = ""):
    """Record a single log entry."""
    if not is_enabled(level):
        return
    global _counter
    with _lock:
        _counter += 1
        entry = {
            "id": _counter,
            "timestamp": datetime.now().isoformat(),
            "level": level,
            "category": category,
            "message": message,
            "detail": detail,
        }
        _buffer.append(entry)


def debug(category: str, message: str, detail: str = ""):
    log(DEBUG, category, message, detail)


def info(category: str, message: str, detail: str = ""):
    log(INFO, category, message, detail)


def warning(category: str, message: str, detail: str = ""):
    log(WARNING, category, message, detail)


def error(category: str, message: str, detail: str = ""):
    log(ERROR, category, message, detail)


def _capture_log(level: str, message: str, **kwargs):
    """内部日志函数，供 API 层捕获异常使用（兼容旧接口）。"""
    detail = kwargs.get("extra", {}).get("exc", "") if kwargs.get("extra") else ""
    log(level, "api", message, detail)


def get_logs(
    level: str | None = None,
    category: str | None = None,
    keyword: str | None = None,
    limit: int = 500,
    offset: int = 0,
) -> list[dict]:
    """查询日志，支持按级别/分类/关键词筛选。

    性能优化：反向迭代 deque（最新在前），按需收集，避免全量复制 + 反转。
    - 无筛选：O(offset+limit) 而非 O(n)
    - 有筛选：O(n) 但无中间列表分配，无 reverse
    """
    from itertools import islice

    has_filter = bool(level or category or keyword)
    kw = keyword.lower() if keyword else None

    with _lock:
        if not has_filter:
            # 无筛选：直接反向取分页切片，避免全量复制
            total = len(_buffer)
            entries = list(islice(reversed(_buffer), offset, offset + limit))
            return entries, total
        # 有筛选：反向遍历，筛选 + 计数 + 按需收集
        entries = []
        total = 0
        for e in reversed(_buffer):
            if level and e["level"] != level:
                continue
            if category and e["category"] != category:
                continue
            if kw:
                msg_lower = e["message"].lower()
                detail_lower = e.get("detail", "").lower()
                if kw not in msg_lower and kw not in detail_lower:
                    continue
            total += 1
            if total > offset and len(entries) < limit:
                entries.append(e)
    return entries, total


def clear_logs():
    """Clear all logs."""
    global _counter
    with _lock:
        _buffer.clear()
        _counter = 0


def get_counter() -> int:
    """Get current log entry counter (for SSE streaming to detect new entries)."""
    return _counter


def get_entries_since(counter: int) -> tuple[list[dict], int]:
    """Get entries logged after the given counter, in ascending (oldest-first) order.

    Returns (entries, current_counter). If the counter was reset (logs cleared),
    returns the whole buffer so the stream resynchronizes instead of going silent.
    """
    from itertools import islice

    with _lock:
        current = _counter
        if current < counter:
            # Counter reset (clear_logs): send everything we still have
            return list(_buffer), current
        count = current - counter
        if count <= 0:
            return [], current
        if count >= len(_buffer):
            return list(_buffer), current
        return list(islice(_buffer, len(_buffer) - count, len(_buffer))), current


def get_stats() -> dict:
    """Get log statistics for dashboard display."""
    with _lock:
        total = len(_buffer)
        by_level = {"DEBUG": 0, "INFO": 0, "WARNING": 0, "ERROR": 0}
        by_category: dict[str, int] = {}
        recent_errors: list[dict] = []

        for e in _buffer:
            level = e.get("level", "INFO")
            if level in by_level:
                by_level[level] += 1

            cat = e.get("category", "unknown")
            by_category[cat] = by_category.get(cat, 0) + 1

            if level == "ERROR" and len(recent_errors) < 10:
                recent_errors.append({
                    "id": e.get("id"),
                    "timestamp": e.get("timestamp"),
                    "category": cat,
                    "message": e.get("message", "")[:200],
                    "detail": (e.get("detail") or "")[:500],
                })

        return {
            "total": total,
            "by_level": by_level,
            "by_category": by_category,
            "recent_errors": recent_errors,
        }


def export_logs(level: str | None = None, category: str | None = None,
                keyword: str | None = None) -> str:
    """Export logs as JSONL text."""
    entries, _ = get_logs(level=level, category=category, keyword=keyword, limit=10000)
    # 恢复正序（旧到新）方便阅读
    entries.reverse()
    lines = []
    for e in entries:
        lines.append(json.dumps(e, ensure_ascii=False))
    return "\n".join(lines)
