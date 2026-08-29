"""用户设置存储：JSON 文件实现。

所有用户设置（含 GUI 偏好：列排序、导航顺序、主题等）统一存在
`<data_dir>/settings.json`，便于用户直接查看和编辑。

迁移：首次启动若 JSON 不存在但 SQLite settings 表有数据，自动迁移过来。

性能优化：
- orjson（若可用）：比标准 json 快 5-10 倍
- 模块级内存缓存（_cache_data + _cache_mtime），避免每次 get_setting 都读文件
- 代理热路径单次请求会调 4-6 次 get_setting（throttle、clash、规则查询等）
缓存通过 mtime 失效：set_setting 写入后会更新 mtime，下次 get 自动重读；
外部直接编辑文件也会被 mtime 检测到。
"""

import os
import threading
from typing import Any

from .config import get_data_dir

try:
    import orjson
    _HAS_ORJSON = True
except ImportError:
    import json as _json
    _HAS_ORJSON = False

_LOCK = threading.Lock()
# 内存缓存：避免每次 get_setting 都 open+json.loads
# 高并发下 50 线程同时读 settings.json，文件 IO + JSON 解析是主要瓶颈
# 通过 mtime 失效：写入后 mtime 变化，下次 get 自动重读
_cache_data: dict = {}
_cache_mtime: float = 0.0
_cache_loaded: bool = False


def get_settings_path() -> str:
    """settings.json 完整路径。"""
    return os.path.join(get_data_dir(), "settings.json")


def _load_uncached() -> dict:
    """Read directly from disk, bypassing the cache."""
    path = get_settings_path()
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "rb") as f:
            data = orjson.loads(f) if _HAS_ORJSON else _json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _load() -> dict:
    """Read the JSON file (with in-memory cache, invalidated by mtime).

    Performance optimizations:
    - After the first load, data is cached in memory; subsequent get_setting
      calls return the cache directly.
    - File changes are detected via os.stat().st_mtime, picking up both writes
      and external modifications.
    - stat() is ~100x faster than open+json.loads (microseconds vs milliseconds).
    - orjson 比标准 json 快 5-10 倍。
    """
    global _cache_data, _cache_mtime, _cache_loaded
    path = get_settings_path()
    try:
        mtime = os.stat(path).st_mtime
    except OSError:
        # 文件不存在
        with _LOCK:
            _cache_data = {}
            _cache_mtime = 0.0
            _cache_loaded = True
        return {}
    # 快路径：mtime 未变，直接返回缓存（无锁读，mtime 是原子 float）
    if _cache_loaded and mtime == _cache_mtime:
        return _cache_data
    # 慢路径：mtime 变了，重新读文件
    with _LOCK:
        # 双检锁：拿锁后再检查一次（可能已被其他线程更新）
        if _cache_loaded and mtime == _cache_mtime:
            return _cache_data
        data = _load_uncached()
        _cache_data = data
        _cache_mtime = mtime
        _cache_loaded = True
        return data


def _save(data: dict) -> None:
    """Atomically write JSON (write a temp file then rename, to avoid partial writes).

    Note: the caller must already hold _LOCK (to avoid racing with the _load slow path).
    Uses orjson if available (5-10x faster than standard json).
    """
    global _cache_data, _cache_mtime, _cache_loaded
    path = get_settings_path()
    tmp = path + ".tmp"
    if _HAS_ORJSON:
        with open(tmp, "wb") as f:
            f.write(orjson.dumps(data, option=orjson.OPT_INDENT_2))
    else:
        with open(tmp, "w", encoding="utf-8") as f:
            _json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=False)
    os.replace(tmp, path)
    # 写后立即更新缓存，避免下次 get 又读磁盘
    try:
        mtime = os.stat(path).st_mtime
    except OSError:
        mtime = 0.0
    _cache_data = data
    _cache_mtime = mtime
    _cache_loaded = True


# 默认性能配置值
DEFAULT_PERFORMANCE_CONFIG = {
    "max_body_size": 10 * 1024 * 1024,  # 10MB
    "decompress_threshold": 1024,  # 1KB
    "ssl_context_cache_size": 256,
    "max_connections": 200,
}

# 性能预设方案
PERFORMANCE_PRESETS = {
    "light": {
        "max_body_size": 5 * 1024 * 1024,
        "decompress_threshold": 512,
        "ssl_context_cache_size": 64,
        "max_connections": 50,
    },
    "standard": {
        "max_body_size": 10 * 1024 * 1024,
        "decompress_threshold": 1024,
        "ssl_context_cache_size": 256,
        "max_connections": 200,
    },
    "high_performance": {
        "max_body_size": 50 * 1024 * 1024,
        "decompress_threshold": 4096,
        "ssl_context_cache_size": 512,
        "max_connections": 500,
    },
}

# 默认 SSL/TLS 配置
DEFAULT_SSL_CONFIG = {
    "min_tls_version": "TLS 1.2",
    "cipher_suites": "ECDHE-RSA-AES128-GCM-SHA256:ECDHE-RSA-AES256-GCM-SHA384:ECDHE-RSA-CHACHA20-POLY1305",
    "sni_spoofing": False,
    "cert_expiry_alert": True,
}


def get_setting(key: str, default: Any = "") -> Any:
    """Read a single setting."""
    data = _load()
    v = data.get(key)
    return v if v is not None and v != "" else default


def get_setting_int(key: str, default: int = 0) -> int:
    """Read a setting as integer."""
    v = get_setting(key, default)
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def has_setting(key: str) -> bool:
    """Check if a key exists in settings (distinguishes null/missing from explicit empty).

    Returns True if the key exists in the JSON (even if value is None or ""),
    False if the key is completely absent.
    """
    data = _load()
    return key in data


def set_setting(key: str, value: Any) -> None:
    """写入单个设置项。

    注意：不能在持有 _LOCK 时调 _load()，否则与 _load 慢路径死锁。
    改为先读（不持锁），再持锁写。
    """
    data = _load()  # 自管锁，不持外层锁
    data[key] = value
    with _LOCK:
        _save(data)


def get_all_settings() -> dict:
    """Read all settings."""
    return _load()


def set_all_settings(items: dict) -> None:
    """Batch write (merge into existing settings, does not remove other keys)."""
    data = _load()  # 自管锁
    data.update(items)
    with _LOCK:
        _save(data)


def migrate_from_sqlite_if_needed(sqlite_get_all_settings) -> None:
    """First-launch migration: when the JSON does not exist but SQLite has data,
    migrate the SQLite settings table over.

    The sqlite_get_all_settings parameter is a callable returning a dict,
    typically db._sqlite_get_all_settings.
    """
    path = get_settings_path()
    if os.path.exists(path):
        return  # JSON 已存在，不迁移
    try:
        legacy = sqlite_get_all_settings() or {}
    except Exception:  # noqa: BLE001
        legacy = {}
    if not legacy:
        return
    with _LOCK:
        _save(legacy)
