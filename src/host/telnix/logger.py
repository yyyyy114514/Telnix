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
    """设置最低记录级别（低于此级别的日志不记录，用于性能优化）。"""
    global _min_level
    _min_level = _LEVEL_ORDER.get(level, 0)


def is_enabled(level: str) -> bool:
    """检查指定级别是否会被记录（用于守卫昂贵的 detail 计算）。"""
    return _LEVEL_ORDER.get(level, 0) >= _min_level


def log(level: str, category: str, message: str, detail: str = ""):
    """记录一条日志。"""
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


def get_logs(
    level: str | None = None,
    category: str | None = None,
    keyword: str | None = None,
    limit: int = 500,
    offset: int = 0,
) -> list[dict]:
    """查询日志，支持按级别/分类/关键词筛选。"""
    with _lock:
        entries = list(_buffer)
    # 筛选
    if level:
        entries = [e for e in entries if e["level"] == level]
    if category:
        entries = [e for e in entries if e["category"] == category]
    if keyword:
        kw = keyword.lower()
        entries = [
            e for e in entries
            if kw in e["message"].lower() or kw in e.get("detail", "").lower()
        ]
    # 倒序（最新在前）
    entries.reverse()
    # 分页
    total = len(entries)
    entries = entries[offset:offset + limit]
    return entries, total


def clear_logs():
    """清空日志。"""
    with _lock:
        _buffer.clear()
        _counter = 0


def export_logs(level: str | None = None, category: str | None = None,
                keyword: str | None = None) -> str:
    """导出日志为 JSONL 文本。"""
    entries, _ = get_logs(level=level, category=category, keyword=keyword, limit=10000)
    # 恢复正序（旧到新）方便阅读
    entries.reverse()
    lines = []
    for e in entries:
        lines.append(json.dumps(e, ensure_ascii=False))
    return "\n".join(lines)
