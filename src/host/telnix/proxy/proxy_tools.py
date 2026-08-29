"""代理工具：No Caching / Force CORS / Block List / Allow List / Map Local / Map Remote / Mirror。

设计：
- 全局开关 + 配置存储在 settings.json（通过 settings_store）
- 进程级配置缓存（同 throttle.py 模式），settings_store mtime 变化时自动失效
- 在代理转发前注入请求头/检查黑白名单，在响应返回后注入 CORS 头

配置项：
- no_caching (0/1): 给所有请求注入 Cache-Control: no-cache, no-store + Pragma: no-cache
- force_cors (0/1): 给所有响应注入 Access-Control-Allow-Origin: * 等 CORS 头
- block_list_enabled (0/1): 是否启用黑名单
- block_list (JSON array): 黑名单规则列表 [{pattern, mode}]
- allow_list_enabled (0/1): 是否启用白名单
- allow_list (JSON array): 白名单规则列表 [{pattern, mode}]

匹配模式：
- wildcard: 通配符匹配（* 匹配任意，? 匹配单字符），匹配 host 或 host+path
- exact: 精确匹配 host
- regex: 正则匹配完整 URL

变量替换（Map Local）：
- {path}: 完整路径，如 /api/users/123/profile
- {path.*}: 路径通配，如 {path.*} 匹配 /api/users/123/profile 的任意部分
- {id} / {name}: URL 路径段作为变量（自动从路径段解析）
- {query.param}: 查询参数，如 {query.page}
- $1 / $2: 正则捕获组（如 /api/users/(\d+)/orders → mock/users_$1_orders.json）
"""
import json
import os
import re
import threading
import time
from urllib.parse import urlsplit, unquote, parse_qs

from .. import settings_store


# ---------- 配置缓存 ----------
_cfg_cache: dict = {
    "no_caching": False,
    "force_cors": False,
    "block_list_enabled": False,
    "block_list": [],
    "allow_list_enabled": False,
    "allow_list": [],
    "map_local_enabled": False,
    "map_local_rules": [],
    "map_remote_enabled": False,
    "map_remote_rules": [],
    "mirror_enabled": False,
    "mirror_rules": [],
}
_cfg_loaded: bool = False
_cfg_lock = threading.Lock()

# 通配符编译缓存
_pattern_cache: dict = {}
_pattern_cache_lock = threading.Lock()

# 规则命中统计（进程内共享）
# 结构: {
#   "map_local_rules": {
#     0: {"hit_count": 10, "last_hits": [{"ts": 1234567890.123, "url": "...", "matched_path": "..."}]},
#     1: {"hit_count": 5, "last_hits": [...]},
#   },
#   "map_remote_rules": {...}
# }
_hit_stats: dict = {
    "map_local_rules": {},
    "map_remote_rules": {},
}
_hit_stats_lock = threading.Lock()
_MAX_LAST_HITS = 10  # 最多保留最近 10 次命中

# 流式媒体 URL 扩展名：命中时 inject_no_caching 跳过注入，避免破坏媒体 CDN 缓存
_MEDIA_URL_EXTENSIONS = (
    ".mp4", ".m4v", ".mkv", ".webm", ".flv", ".avi", ".mov", ".wmv",
    ".mp3", ".m4a", ".aac", ".ogg", ".oga", ".wav", ".flac", ".opus",
    ".m3u8", ".ts", ".mpd", ".m4s", ".cmfv", ".cmfa",
)


def _get_cfg() -> dict:
    if not _cfg_loaded:
        _refresh_config()
    return _cfg_cache


def invalidate_cache() -> None:
    """配置变更后调用，使缓存失效。"""
    global _cfg_loaded
    with _cfg_lock:
        _cfg_loaded = False


def get_config() -> dict:
    _refresh_config()
    return dict(_cfg_cache)


# ---------- 模式匹配 ----------

# 正则缓存（用于 wildcard 转换后的正则和用户输入的 regex 模式）
_pattern_cache_lock = threading.Lock()
_pattern_cache: dict[str, re.Pattern] = {}
_MAX_PATTERN_CACHE = 500


def _compile_wildcard(pattern: str) -> re.Pattern:
    """将通配符模式编译为正则（带缓存）。"""
    with _pattern_cache_lock:
        compiled = _pattern_cache.get(pattern)
        if compiled is not None:
            return compiled
        # 转义所有正则特殊字符，然后恢复 * 和 ?
        regex = re.escape(pattern).replace(r"\*", ".*").replace(r"\?", ".")
        compiled = re.compile(f"^{regex}$", re.IGNORECASE)
        if len(_pattern_cache) > _MAX_PATTERN_CACHE:
            _pattern_cache.clear()
        _pattern_cache[pattern] = compiled
        return compiled


def _compile_regex(pattern: str) -> re.Pattern | None:
    """编译正则模式（带缓存）。返回 None 如果 pattern 无效。"""
    with _pattern_cache_lock:
        compiled = _pattern_cache.get(pattern)
        if compiled is not None:
            return compiled
        try:
            compiled = re.compile(pattern, re.IGNORECASE)
        except re.error:
            return None
        if len(_pattern_cache) > _MAX_PATTERN_CACHE:
            _pattern_cache.clear()
        _pattern_cache[pattern] = compiled
        return compiled


def _match_pattern(pattern: str, mode: str, host: str, url: str) -> tuple[bool, dict | None]:
    """检查单个模式是否匹配。

    - wildcard: 通配符匹配 host 或 host+path（自动尝试两者）
    - exact: 精确匹配 host
    - regex: 正则匹配完整 URL

    返回 (matched: bool, groups: dict | None)
    - 对于 wildcard/exact，返回 ({}, {})
    - 对于 regex，返回 (matched, regex_groups_dict)
    """
    if not pattern:
        return False, None
    if mode == "regex":
        compiled = _compile_regex(pattern)
        if compiled is None:
            return False, None
        try:
            match = compiled.search(url)
            if match:
                groups = match.groupdict() or {}
                return True, groups
            return False, None
        except Exception:  # noqa: BLE001
            return False, None
    if mode == "exact":
        return host.lower() == pattern.lower(), {}
    # wildcard: 尝试匹配 host 和 url（去掉 scheme）
    target = host
    # 也尝试匹配 host+path
    url_no_scheme = url
    for prefix in ("https://", "http://"):
        if url_no_scheme.lower().startswith(prefix):
            url_no_scheme = url_no_scheme[len(prefix):]
            break
    compiled = _compile_wildcard(pattern)
    matched_host = compiled.match(target)
    matched_url = compiled.match(url_no_scheme)
    if matched_host or matched_url:
        # wildcard 模式也提取捕获组（如果规则中有 (?P<name>...)）
        try:
            # 提取正则模式到变量（避免 f-string 中使用反斜杠）
            regex_str = "^" + re.escape(pattern).replace(r'\*', '.*').replace(r'\?', '.') + "$"
            m = re.match(regex_str, url_no_scheme, re.IGNORECASE)
            if m:
                return True, m.groupdict()
        except re.error:
            pass
        return True, {}
    return False, None


def _record_hit(rule_type: str, rule_index: int, url: str, resolved_path: str = "") -> None:
    """记录规则命中。"""
    with _hit_stats_lock:
        if rule_type not in _hit_stats:
            _hit_stats[rule_type] = {}
        if rule_index not in _hit_stats[rule_type]:
            _hit_stats[rule_type][rule_index] = {"hit_count": 0, "last_hits": []}
        stats = _hit_stats[rule_type][rule_index]
        stats["hit_count"] += 1
        stats["last_hits"].insert(0, {"ts": time.time(), "url": url, "resolved_path": resolved_path})
        # 只保留最近 10 条
        if len(stats["last_hits"]) > _MAX_LAST_HITS:
            stats["last_hits"] = stats["last_hits"][:_MAX_LAST_HITS]


def get_hit_stats() -> dict:
    """获取所有规则的命中统计。"""
    with _hit_stats_lock:
        # 深拷贝避免外部修改
        return json.loads(json.dumps(_hit_stats))


def clear_hit_stats(rule_type: str | None = None, rule_index: int | None = None) -> None:
    """清除命中统计。

    - rule_type + rule_index: 清除指定规则
    - 只有 rule_type: 清除该类型所有规则
    - 全为 None: 清除全部
    """
    with _hit_stats_lock:
        if rule_type is None:
            _hit_stats["map_local_rules"].clear()
            _hit_stats["map_remote_rules"].clear()
        elif rule_index is None:
            if rule_type in _hit_stats:
                _hit_stats[rule_type].clear()
        else:
            if rule_type in _hit_stats and rule_index in _hit_stats[rule_type]:
                del _hit_stats[rule_type][rule_index]


# ---------- 变量替换引擎 ----------

def _parse_url_vars(url: str) -> dict:
    """解析 URL 各部分为字典。

    返回: {"path": "...", "query": {"page": "1", "size": "20"}, "segments": ["api", "users", "123", "profile"]}
    """
    sp = urlsplit(url)
    # 去掉 scheme
    path = sp.path
    segments = [s for s in path.split("/") if s]
    query = {}
    if sp.query:
        try:
            query = {k: v[0] if len(v) == 1 else v for k, v in parse_qs(sp.query).items()}
        except Exception:
            # 解析失败时用简单方式
            for pair in sp.query.split("&"):
                if "=" in pair:
                    k, v = pair.split("=", 1)
                    query[unquote(k)] = unquote(v)
                elif pair:
                    query[unquote(pair)] = ""
    return {"path": path, "query": query, "segments": segments}


def _resolve_path_vars(path_template: str, url: str, regex_groups: dict | None = None) -> str:
    """变量替换：将路径模板中的变量替换为实际值。

    支持的变量：
    - {path}: 完整路径
    - {segments[0]}, {segments[1]}: 路径段（0-based）
    - {id}, {name} 等: 尝试从路径段中查找（按名称或位置）
    - $1, $2: 正则捕获组
    - {query.param}: 查询参数
    - {wildcard[0]}: 通配符匹配的路径段（用于 /* → mock/{wildcard[0]}）
    """
    if not path_template:
        return path_template

    vars_info = _parse_url_vars(url)
    result = path_template

    # 替换 $1, $2 等正则捕获组
    if regex_groups:
        for k, v in sorted(regex_groups.items(), key=lambda x: (x[0].isdigit(), int(x[0]) if x[0].isdigit() else 0)):
            # 支持 $1 或 $groupname
            if k.isdigit():
                result = result.replace(f"${k}", v)
            else:
                result = result.replace(f"${k}", v)

    # 替换 {path}
    result = result.replace("{path}", vars_info["path"])

    # 替换 {segments[N]}
    segments = vars_info["segments"]
    for i, seg in enumerate(segments):
        result = result.replace(f"{{segments[{i}]}}", seg)

    # 替换 {wildcard[N]}（用于 /* 匹配的场景）
    # 通配符规则 /api/* → mock/{wildcard[0]}
    wildcard_segments = []
    if "/*" in path_template or "{wildcard[" in path_template:
        # 提取 path 中通配符匹配的部分
        pass  # 已在上面 {path} 中处理

    # 替换 {query.param}
    for param, value in vars_info["query"].items():
        result = result.replace(f"{{query.{param}}}", str(value))

    # 尝试替换命名的路径变量（如 {id}, {userId}）
    # 优先从 regex_groups 中找，其次从路径段中按名称匹配
    for seg in segments:
        if seg and not seg.isdigit():
            # 检查是否有 {seg} 占位
            if "{" + seg + "}" in result:
                result = result.replace("{" + seg + "}", seg)

    return result


def _resolve_file_path(rule_file_path: str, url: str, mode: str, pattern: str, regex_groups: dict | None = None) -> str:
    """解析 Map Local 文件路径，支持变量替换。

    返回解析后的文件路径。
    """
    if not rule_file_path:
        return rule_file_path

    # 检查是否启用了变量替换（检查模板中是否有变量语法）
    has_vars = (
        "{path}" in rule_file_path or
        "{query." in rule_file_path or
        "$1" in rule_file_path or
        "$2" in rule_file_path or
        "{segments[" in rule_file_path or
        re.search(r"\{\w+\}", rule_file_path) is not None  # 通用命名变量 {id} 等
    )

    if not has_vars:
        return rule_file_path

    return _resolve_path_vars(rule_file_path, url, regex_groups)


def _match_headers(rule_headers: dict | None, request_headers: dict | None) -> bool:
    """检查请求头是否匹配规则中的 header 条件。

    rule_headers 格式: {"X-Token": "*abc*", "Content-Type": "application/json"}
    - *value*: 包含匹配（不区分大小写）
    - value: 精确匹配（不区分大小写）
    - /^regex$/: 正则匹配
    返回 True 表示匹配（或无 header 条件）。
    """
    if not rule_headers:
        return True
    if not request_headers:
        # 有 header 条件但无请求头 → 不匹配
        return False
    req_lower = {k.lower(): v for k, v in request_headers.items()}
    for key, pattern in rule_headers.items():
        val = req_lower.get(key.lower())
        if val is None:
            return False
        if pattern.startswith("^") and pattern.endswith("$") and pattern.count("$") == 1:
            # 正则
            try:
                if not re.search(pattern, val, re.IGNORECASE):
                    return False
            except re.error:
                return False
        elif pattern.startswith("*") and pattern.endswith("*"):
            if pattern[1:-1].lower() not in val.lower():
                return False
        elif pattern.startswith("*"):
            if not val.lower().endswith(pattern[1:].lower()):
                return False
        elif pattern.endswith("*"):
            if not val.lower().startswith(pattern[:-1].lower()):
                return False
        else:
            if val.lower() != pattern.lower():
                return False
    return True


def _match_list(rules: list, host: str, url: str) -> bool:
    """检查规则列表中是否有匹配项。"""
    for rule in rules:
        if isinstance(rule, str):
            # 兼容纯字符串模式（默认 wildcard）
            matched, _ = _match_pattern(rule, "wildcard", host, url)
            if matched:
                return True
        elif isinstance(rule, dict):
            pattern = rule.get("pattern", "")
            mode = rule.get("mode", "wildcard")
            matched, _ = _match_pattern(pattern, mode, host, url)
            if matched:
                return True
    return False


# ---------- 代理注入接口 ----------

def should_block(host: str, url: str) -> bool:
    """检查请求是否应被黑名单阻断。

    优先级：白名单 > 黑名单（白名单命中则放行，黑名单命中则阻断）
    """
    cfg = _get_cfg()
    # 白名单优先：命中则放行
    if cfg["allow_list_enabled"] and cfg["allow_list"]:
        if _match_list(cfg["allow_list"], host, url):
            return False
    # 黑名单：命中则阻断
    if cfg["block_list_enabled"] and cfg["block_list"]:
        if _match_list(cfg["block_list"], host, url):
            return True
    return False


def inject_no_caching(headers, url: str = "") -> None:
    """给请求头注入 no-cache 指令（原地修改 Headers 对象）。

    对视频/音频等流式媒体请求跳过注入：no-cache 会强制 CDN 对每个分片回源，
    破坏媒体 CDN 缓存并加剧首字节延迟。检测依据：
    - 请求带 Range 头（播放器分片请求的强信号）
    - URL 路径扩展名匹配常见媒体格式（.mp4/.m3u8/.ts/.mpd/.m4s/.flv/.webm/.mp3 等）
    """
    cfg = _get_cfg()
    if not cfg["no_caching"]:
        return
    # 流式媒体请求：带 Range 头或 URL 扩展名匹配媒体格式，跳过 no-cache 注入
    if headers.has("Range"):
        return
    if url:
        # 取路径部分（去掉 query），小写后比较扩展名
        _path = url.split("?", 1)[0].split("#", 1)[0].lower()
        for _ext in _MEDIA_URL_EXTENSIONS:
            if _path.endswith(_ext):
                return
    headers.set("Cache-Control", "no-cache, no-store, must-revalidate")
    headers.set("Pragma", "no-cache")
    headers.set("Expires", "0")


def inject_cors(headers) -> None:
    """给响应头注入 CORS 头（原地修改 Headers 对象）。"""
    cfg = _get_cfg()
    if not cfg["force_cors"]:
        return
    headers.set("Access-Control-Allow-Origin", "*")
    headers.set("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, PATCH, HEAD, OPTIONS")
    headers.set("Access-Control-Allow-Headers", "*")
    headers.set("Access-Control-Expose-Headers", "*")
    headers.set("Access-Control-Max-Age", "86400")


# ---------- Map Local / Map Remote ----------


def _refresh_config() -> None:
    global _cfg_cache, _cfg_loaded
    no_caching = settings_store.get_setting("no_caching", "0") == "1"
    force_cors = settings_store.get_setting("force_cors", "0") == "1"
    block_enabled = settings_store.get_setting("block_list_enabled", "0") == "1"
    allow_enabled = settings_store.get_setting("allow_list_enabled", "0") == "1"
    block_raw = settings_store.get_setting("block_list", "[]")
    allow_raw = settings_store.get_setting("allow_list", "[]")
    try:
        block_list = json.loads(block_raw) if isinstance(block_raw, str) else (block_raw or [])
    except (json.JSONDecodeError, TypeError):
        block_list = []
    try:
        allow_list = json.loads(allow_raw) if isinstance(allow_raw, str) else (allow_raw or [])
    except (json.JSONDecodeError, TypeError):
        allow_list = []
    map_local_enabled = settings_store.get_setting("map_local_enabled", "0") == "1"
    map_remote_enabled = settings_store.get_setting("map_remote_enabled", "0") == "1"
    map_local_raw = settings_store.get_setting("map_local_rules", "[]")
    map_remote_raw = settings_store.get_setting("map_remote_rules", "[]")
    mirror_enabled = settings_store.get_setting("mirror_enabled", "0") == "1"
    mirror_raw = settings_store.get_setting("mirror_rules", "[]")
    try:
        map_local_rules = json.loads(map_local_raw) if isinstance(map_local_raw, str) else (map_local_raw or [])
    except (json.JSONDecodeError, TypeError):
        map_local_rules = []
    try:
        map_remote_rules = json.loads(map_remote_raw) if isinstance(map_remote_raw, str) else (map_remote_raw or [])
    except (json.JSONDecodeError, TypeError):
        map_remote_rules = []
    try:
        mirror_rules = json.loads(mirror_raw) if isinstance(mirror_raw, str) else (mirror_raw or [])
    except (json.JSONDecodeError, TypeError):
        mirror_rules = []
    _cfg_cache = {
        "no_caching": no_caching,
        "force_cors": force_cors,
        "block_list_enabled": block_enabled,
        "block_list": block_list,
        "allow_list_enabled": allow_enabled,
        "allow_list": allow_list,
        "map_local_enabled": map_local_enabled,
        "map_local_rules": map_local_rules,
        "map_remote_enabled": map_remote_enabled,
        "map_remote_rules": map_remote_rules,
        "mirror_enabled": mirror_enabled,
        "mirror_rules": mirror_rules,
    }
    _cfg_loaded = True


def update_config(items: dict) -> dict:
    """批量更新配置，返回更新后的完整配置。"""
    if "no_caching" in items:
        settings_store.set_setting("no_caching", "1" if items["no_caching"] else "0")
    if "force_cors" in items:
        settings_store.set_setting("force_cors", "1" if items["force_cors"] else "0")
    if "block_list_enabled" in items:
        settings_store.set_setting("block_list_enabled", "1" if items["block_list_enabled"] else "0")
    if "allow_list_enabled" in items:
        settings_store.set_setting("allow_list_enabled", "1" if items["allow_list_enabled"] else "0")
    if "block_list" in items:
        settings_store.set_setting("block_list", json.dumps(items["block_list"], ensure_ascii=False))
    if "allow_list" in items:
        settings_store.set_setting("allow_list", json.dumps(items["allow_list"], ensure_ascii=False))
    if "map_local_enabled" in items:
        settings_store.set_setting("map_local_enabled", "1" if items["map_local_enabled"] else "0")
    if "map_remote_enabled" in items:
        settings_store.set_setting("map_remote_enabled", "1" if items["map_remote_enabled"] else "0")
    if "map_local_rules" in items:
        settings_store.set_setting("map_local_rules", json.dumps(items["map_local_rules"], ensure_ascii=False))
    if "map_remote_rules" in items:
        settings_store.set_setting("map_remote_rules", json.dumps(items["map_remote_rules"], ensure_ascii=False))
    if "mirror_enabled" in items:
        settings_store.set_setting("mirror_enabled", "1" if items["mirror_enabled"] else "0")
    if "mirror_rules" in items:
        settings_store.set_setting("mirror_rules", json.dumps(items["mirror_rules"], ensure_ascii=False))
    invalidate_cache()
    return get_config()


def check_map_local(host: str, url: str, request_headers: dict | None = None) -> dict | None:
    """检查请求是否匹配 Map Local 规则。

    返回 {"status": int, "headers": dict, "body": bytes} 或 None。
    支持 header 条件匹配：rule["headers"] = {"X-Token": "*abc*"}
    支持变量替换：{path}、$1、正则捕获组、{query.page}、{segments[0]}、{id} 等
    """
    cfg = _get_cfg()
    if not cfg["map_local_enabled"] or not cfg["map_local_rules"]:
        return None
    for idx, rule in enumerate(cfg["map_local_rules"]):
        if not isinstance(rule, dict):
            continue
        pattern = rule.get("pattern", "")
        mode = rule.get("mode", "wildcard")
        matched, regex_groups = _match_pattern(pattern, mode, host, url)
        if not matched:
            continue
        # Header 条件匹配
        rule_headers = rule.get("headers")
        if not _match_headers(rule_headers, request_headers):
            continue
        rule_file_path = rule.get("file_path", "")
        if not rule_file_path:
            continue

        # 变量替换解析文件路径
        resolved_path = _resolve_file_path(rule_file_path, url, mode, pattern, regex_groups)

        # 通配符目录映射支持：如果规则使用通配符模式且路径模板包含 {path}，先替换再匹配
        if not os.path.isfile(resolved_path):
            continue
        try:
            with open(resolved_path, "rb") as f:
                body = f.read()
        except OSError:
            continue
        status = int(rule.get("status", 200))
        content_type = rule.get("content_type", "application/octet-stream")
        # 从文件扩展名推断 Content-Type
        if not rule.get("content_type"):
            ext = os.path.splitext(resolved_path)[1].lower()
            _ext_map = {
                ".html": "text/html; charset=utf-8",
                ".htm": "text/html; charset=utf-8",
                ".css": "text/css; charset=utf-8",
                ".js": "application/javascript; charset=utf-8",
                ".json": "application/json; charset=utf-8",
                ".xml": "application/xml; charset=utf-8",
                ".txt": "text/plain; charset=utf-8",
                ".png": "image/png",
                ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg",
                ".gif": "image/gif",
                ".svg": "image/svg+xml",
                ".ico": "image/x-icon",
                ".woff": "font/woff",
                ".woff2": "font/woff2",
                ".pdf": "application/pdf",
            }
            content_type = _ext_map.get(ext, "application/octet-stream")
        # 记录命中
        _record_hit("map_local_rules", idx, url, resolved_path)
        return {
            "status": status,
            "headers": {"Content-Type": content_type},
            "body": body,
        }
    return None


def check_map_remote(host: str, url: str, request_headers: dict | None = None) -> tuple | None:
    """检查请求是否匹配 Map Remote 规则。

    返回 (new_scheme, new_host, new_port, new_path, new_url) 或 None。
    支持 header 条件匹配：rule["headers"] = {"X-Token": "*abc*"}
    支持变量替换：{path}、$1、正则捕获组、{query.page} 等
    """
    cfg = _get_cfg()
    if not cfg["map_remote_enabled"] or not cfg["map_remote_rules"]:
        return None
    for idx, rule in enumerate(cfg["map_remote_rules"]):
        if not isinstance(rule, dict):
            continue
        pattern = rule.get("pattern", "")
        mode = rule.get("mode", "wildcard")
        matched, regex_groups = _match_pattern(pattern, mode, host, url)
        if not matched:
            continue
        # Header 条件匹配
        rule_headers = rule.get("headers")
        if not _match_headers(rule_headers, request_headers):
            continue
        target_url = rule.get("target_url", "")
        if not target_url:
            continue
        # 变量替换：替换 {path}、$1、{query.xxx}
        if regex_groups or "{path}" in target_url or "{query." in target_url or any(f"${i}" in target_url for i in range(1, 10)):
            target_url = _resolve_path_vars(target_url, url, regex_groups)
        try:
            sp = urlsplit(target_url)
        except ValueError:
            continue
        new_scheme = sp.scheme or "https"
        new_host = sp.hostname or host
        new_port = sp.port or (443 if new_scheme == "https" else 80)
        new_path = sp.path or "/"
        if sp.query:
            new_path += "?" + sp.query
        new_url = target_url
        # 记录命中
        _record_hit("map_remote_rules", idx, url, new_url)
        return (new_scheme, new_host, new_port, new_path, new_url)
    return None


# ---------- Mirror ----------

def check_mirror(host: str, url: str) -> str | None:
    """检查请求是否匹配 Mirror 规则。

    返回 save_dir（保存目录路径）或 None。
    """
    cfg = _get_cfg()
    if not cfg["mirror_enabled"] or not cfg["mirror_rules"]:
        return None
    for rule in cfg["mirror_rules"]:
        if not isinstance(rule, dict):
            continue
        pattern = rule.get("pattern", "")
        mode = rule.get("mode", "wildcard")
        if not _match_pattern(pattern, mode, host, url):
            continue
        save_dir = rule.get("save_dir", "")
        if save_dir:
            return save_dir
    return None


def save_mirror_response(save_dir: str, url: str, content_type: str, body: bytes) -> str | None:
    """将响应体保存到 mirror 目录，返回保存的文件路径或 None。"""
    if not save_dir or not body:
        return None
    try:
        os.makedirs(save_dir, exist_ok=True)
    except OSError:
        return None
    # 从 URL path 推断文件名
    sp = urlsplit(url)
    name = unquote(sp.path.rstrip("/").rsplit("/", 1)[-1]) if sp.path.rstrip("/") else "index"
    if not name:
        name = "index"
    # 无扩展名时从 Content-Type 推断
    if "." not in name and content_type:
        ct = content_type.split(";")[0].strip().lower()
        ext_map = {
            "text/html": ".html", "text/css": ".css", "text/plain": ".txt",
            "application/json": ".json", "application/xml": ".xml",
            "application/javascript": ".js", "image/png": ".png",
            "image/jpeg": ".jpg", "image/gif": ".gif", "image/svg+xml": ".svg",
            "application/pdf": ".pdf", "video/mp4": ".mp4",
        }
        name += ext_map.get(ct, ".bin")
    elif "." not in name:
        name += ".bin"
    # 安全化文件名
    name = re.sub(r'[<>:"/\\|?*]', "_", name)
    # 文件已存在则追加序号
    base, ext = os.path.splitext(name)
    candidate = name
    i = 1
    while os.path.exists(os.path.join(save_dir, candidate)):
        candidate = f"{base}_{i}{ext}"
        i += 1
    file_path = os.path.join(save_dir, candidate)
    try:
        with open(file_path, "wb") as f:
            f.write(body)
    except OSError:
        return None
    return file_path
