"""用户设置存储：JSON 文件实现。

所有用户设置（含 GUI 偏好：列排序、导航顺序、主题等）统一存在
`<data_dir>/settings.json`，便于用户直接查看和编辑。

迁移：首次启动若 JSON 不存在但 SQLite settings 表有数据，自动迁移过来。

性能优化：模块级内存缓存（_cache_data + _cache_mtime），避免每次 get_setting
都读文件 + json.loads。代理热路径单次请求会调 4-6 次 get_setting（throttle、
clash、规则查询等），高并发下文件 IO 是主要瓶颈。
缓存通过 mtime 失效：set_setting 写入后会更新 mtime，下次 get 自动重读；
外部直接编辑文件也会被 mtime 检测到。
"""

import json
import os
import threading
from typing import Any

from .config import get_data_dir

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
    """直接读磁盘，不查缓存。"""
    path = get_settings_path()
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _load() -> dict:
    """读取 JSON 文件（带内存缓存，按 mtime 失效）。

    性能优化：
    - 首次加载后缓存在内存，后续 get_setting 直接返回缓存
    - 通过 os.stat().st_mtime 检测文件变更，写入或外部修改都能感知
    - stat() 比 open+json.loads 快 100 倍（μs vs ms 级）
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
    """原子写入 JSON（先写临时文件再 rename，避免半截写入）。

    注意：调用方必须已持有 _LOCK（避免与 _load 慢路径竞争）。
    """
    global _cache_data, _cache_mtime, _cache_loaded
    path = get_settings_path()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=False)
    os.replace(tmp, path)
    # 写后立即更新缓存，避免下次 get 又读磁盘
    try:
        mtime = os.stat(path).st_mtime
    except OSError:
        mtime = 0.0
    _cache_data = data
    _cache_mtime = mtime
    _cache_loaded = True


def get_setting(key: str, default: Any = "") -> Any:
    """读取单个设置项。"""
    data = _load()
    v = data.get(key)
    return v if v is not None and v != "" else default


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
    """读取全部设置。"""
    return _load()


def set_all_settings(items: dict) -> None:
    """批量写入（合并到现有设置，不删除其他键）。"""
    data = _load()  # 自管锁
    data.update(items)
    with _LOCK:
        _save(data)


def migrate_from_sqlite_if_needed(sqlite_get_all_settings) -> None:
    """首次启动迁移：JSON 不存在但 SQLite 有数据时，把 SQLite 的 settings 表迁过来。

    参数 sqlite_get_all_settings 是一个返回 dict 的可调用对象，
    通常传 db._sqlite_get_all_settings。
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
