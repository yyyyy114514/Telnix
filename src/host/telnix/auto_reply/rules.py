"""Auto-reply rule management + wildcard matching.

Rules are stored in SQLite (auto_reply_rules table). Match modes:
- wildcard: * -> .*, ? -> ., escape other regex metacharacters
- exact: exact equality
- regex: used directly as regex

The match target is the full URL. Provides an in-memory cache (with TTL) that is
refreshed when rules change.

Performance optimization (v10):
- _cache changed to a triplet (rules, compiled_regexes, filters), pre-compiled regex + pre-split filters
- Fixed empty list falsy bug (querying DB every time when no rules)
- TTL raised from 2s to 10s, relies on invalidate_cache for active invalidation
- Sorting done once at load time, no longer sorted() per request

Performance optimization (v11, no-capture scenario):
- Module-level _has_rules flag: lock-free read, avoids entering _cache_lock per request under high concurrency
- host_matches_any_rule result LRU cache: queried on every CONNECT when not capturing, cache avoids repeated traversal
- has_active_rules_fast(): uses _has_rules for quick check, skips find_matching_rule when no rules
"""

import re
import threading
import time

from .. import db


def _get_redos_threshold(key: str, default: int) -> int:
    """Read ReDoS protection threshold from settings_store (allows runtime configuration).

    Design fix: moved from hardcoded constants to settings_store.
    """
    try:
        from .. import settings_store
        v = settings_store.get_setting(key, default)
        if isinstance(v, (int, float)) and v > 0:
            return int(v)
    except Exception:  # noqa: BLE001
        pass
    return default


# 防止 ReDoS（灾难性回溯）：对规则 pattern 施加预算上限。
# - 通配符/正则模式长度上限（过长的 pattern 本身即异常）
# - wildcard 模式通配符数量上限（过多 .* 在长输入上会指数级回溯）
# - regex 模式量化符/分支数量上限（嵌套量词如 (a+)+ 易触发回溯爆炸）
# 超出预算的模式直接视为"编译失败"（返回 None），匹配时安全跳过该规则。
# 设计修复：从 settings_store 读取，支持用户调整阈值
_MAX_PATTERN_LEN = _get_redos_threshold("max_pattern_len", 256)
_MAX_WILDCARDS = _get_redos_threshold("max_wildcards", 16)
_MAX_REGEX_QUANTIFIERS = _get_redos_threshold("max_regex_quantifiers", 12)

_cache_lock = threading.Lock()
# 性能优化：_cache 改为存 (rules, compiled_regexes, filters) 三元组
# None 表示未初始化（区分"无规则"和"未加载"，修复空 list falsy bug）
_cache: tuple[list[dict], list[re.Pattern | None], list[dict]] | None = None
_cache_ts: float = 0.0
_CACHE_TTL = 10.0  # 提到 10 秒，靠 invalidate_cache 主动失效

# 性能优化（v11）：模块级标志位，无锁读
# _load_rules 时更新；invalidate_cache 后置 False（保守）
# 在 invalidate_cache 到下次 _load_rules 之间可能短暂不准，但最坏只是多调一次 find_matching_rule
_has_rules: bool = False

# 性能优化（v11）：host_matches_any_rule 结果缓存
# 未抓包但有规则时，每次 HTTPS CONNECT 都会查 host_matches_any_rule
# 同一 host 反复连接（keep-alive 断开后重连）时，缓存避免重复遍历所有规则
_host_match_cache: dict[str, bool] = {}
_host_match_lock = threading.Lock()
_HOST_CACHE_MAX = 1024  # 上限，避免无界增长


def invalidate_cache():
    """Call after rules change to clear the cache."""
    global _cache_ts, _cache, _has_rules
    with _cache_lock:
        _cache_ts = 0.0
        _cache = None  # 彻底清空，下次 _load_rules 重新加载
        _has_rules = False  # 保守置 False，下次 _load_rules 会重新计算
    # 清空 host 匹配缓存（规则变了，旧结果失效）
    with _host_match_lock:
        _host_match_cache.clear()


def _load_rules() -> tuple[list[dict], list[re.Pattern | None], list[dict]]:
    """Load enabled rules, pre-compile regexes + pre-split filters + pre-sort.

    Returns (rules, compiled_regexes, filters):
    - rules: list of enabled rules (sorted by pattern length descending, once)
    - compiled_regexes: pre-compiled regex for each rule (one-to-one with rules, None=compile failed)
    - filters: pre-split filter dict for each rule {method_parts, status_parts, ...}
    """
    global _cache, _cache_ts, _has_rules
    now = time.time()
    with _cache_lock:
        if _cache is not None and now - _cache_ts < _CACHE_TTL:
            return _cache
        raw_rules = [r for r in db.get_rules() if r.get("enabled")]
        # 按 pattern 长度降序，一次排序（不在每请求 sorted）
        raw_rules.sort(key=lambda r: len(r.get("pattern", "")), reverse=True)
        # 预编译正则 + 预拆分 filter
        compiled: list[re.Pattern | None] = []
        filters: list[dict] = []
        for r in raw_rules:
            compiled.append(_compile(r.get("pattern", ""), r.get("match_mode", "wildcard")))
            filters.append(_precompile_filters(r))
        _cache = (raw_rules, compiled, filters)
        _cache_ts = now
        # 更新模块级标志位（无锁读用）
        _has_rules = bool(raw_rules)
        return _cache


def _precompile_filters(rule: dict) -> dict:
    """Pre-split filter fields to avoid split + strip per request.

    Returns {method_set, status_set, pid_set, process_set}, each a set or None (None=no filter).
    """
    def _to_set(s: str, lower: bool = False) -> set[str] | None:
        if not s:
            return None
        parts = [p.strip() for p in s.split(",") if p.strip()]
        if not parts:
            return None
        return {p.lower() for p in parts} if lower else set(parts)

    return {
        "method_set": _to_set(rule.get("method_filter", ""), lower=True),
        "status_set": _to_set(rule.get("status_filter", "")),
        "pid_set": _to_set(rule.get("pid_filter", "")),
        "process_set": _to_set(rule.get("process_filter", ""), lower=True),
    }


def _wildcard_to_regex(pattern: str) -> re.Pattern:
    """Wildcard to regex: * -> .*, ? -> ., others escaped."""
    out = []
    for ch in pattern:
        if ch == "*":
            out.append(".*")
        elif ch == "?":
            out.append(".")
        else:
            out.append(re.escape(ch))
    return re.compile("^" + "".join(out) + "$", re.IGNORECASE)


def _has_nested_quantifier(pattern: str) -> bool:
    """Detect nested quantifier structures (e.g. ``(a+)+``, ``(a*)*``, ``(a{2,}){3,}``).

    Approach: use a stack to track bracket groups, recording whether a quantifier
    appears inside each group; when a closing bracket is encountered and followed
    by a quantifier (* + ? {), if the group contains a quantifier internally it is
    considered a nested quantifier with catastrophic backtracking risk. Escaped
    characters and brackets inside character classes are skipped.
    """
    stack: list[bool] = []  # 每层分组"内部是否出现量词"
    in_class = False  # 是否在 [...] 字符类内
    i = 0
    n = len(pattern)
    while i < n:
        ch = pattern[i]
        if ch == "\\":
            i += 2
            continue
        if in_class:
            if ch == "]":
                in_class = False
            i += 1
            continue
        if ch == "[":
            in_class = True
            i += 1
            continue
        if ch == "(":
            stack.append(False)
            i += 1
            continue
        if ch == ")":
            had_quant = stack.pop() if stack else False
            # 闭括号后是否紧跟量词
            nxt = pattern[i + 1] if i + 1 < n else ""
            if nxt in "*+?{" and had_quant:
                return True
            # 分组被量化后，其自身对外层也算"含量词"
            if stack and (nxt in "*+?{" or had_quant):
                stack[-1] = True
            i += 1
            continue
        if ch in "*+?{":
            if stack:
                stack[-1] = True
            i += 1
            continue
        i += 1
    return False


def _compile(pattern: str, mode: str) -> re.Pattern | None:
    # 长度预算：任何模式过长都直接判为不安全，拒绝编译
    if not pattern or len(pattern) > _MAX_PATTERN_LEN:
        return None
    try:
        if mode == "regex":
            # 量化符 / 分支预算：抑制嵌套量词导致的灾难性回溯。
            # 仅统计扁平量化符数量不足以防止 ReDoS，例如 (a+)+ 仅有 2 个
            # 量化符但仍可触发指数级回溯，因此额外检测"分组后紧跟量词"的嵌套结构。
            if _has_nested_quantifier(pattern):
                return None
            quantifiers = sum(pattern.count(c) for c in ("*", "+", "?", "{", "|"))
            if quantifiers > _MAX_REGEX_QUANTIFIERS:
                return None
            return re.compile(pattern, re.IGNORECASE)
        if mode == "exact":
            # 完全相等：大小写敏感，不使用 IGNORECASE（与文档"完全相等"语义一致）
            return re.compile("^" + re.escape(pattern) + "$")
        # wildcard 模式：通配符数量预算
        if pattern.count("*") + pattern.count("?") > _MAX_WILDCARDS:
            return None
        return _wildcard_to_regex(pattern)
    except re.error:
        return None


def match_url(pattern: str, url: str, mode: str = "wildcard") -> bool:
    rx = _compile(pattern, mode)
    if rx is None:
        return False
    return rx.search(url) is not None


def find_matching_rule(url: str, method: str | None = None,
                       status_code: int | None = None, pid: int | None = None,
                       process_name: str | None = None) -> dict | None:
    """Return the enabled rule matching the url, or None if no match.

    Priority: more specific patterns first (longer pattern string = more specific).
    So *logii.steamstart.top/api/usage/verify* takes priority over *steamstart.top*.

    Additional filter fields (§4.1):
    - method_filter: comma-separated HTTP methods, empty=no filter, non-empty=method must be in it
    - status_filter: comma-separated status codes, empty=no filter, non-empty=status_code must be in it
    - pid_filter: comma-separated PIDs, empty=no filter, non-empty=pid must be in it
    - process_filter: comma-separated process names, empty=no filter, non-empty=process_name must be in it

    Performance optimization: regexes are pre-compiled, filters pre-split into sets, matching is O(1) lookup.
    """
    if not url:
        return None
    rules, compiled_regexes, filters = _load_rules()
    for i, rule in enumerate(rules):
        rx = compiled_regexes[i]
        if rx is None or not rx.search(url):
            continue
        # URL 匹配后，检查过滤字段（用预拆分的 set，O(1) 查找）
        flt = filters[i]
        if not _match_filter_set(flt["method_set"], method, lower=True):
            continue
        if not _match_filter_set(flt["status_set"], status_code):
            continue
        if not _match_filter_set(flt["pid_set"], pid):
            continue
        if not _match_filter_set(flt["process_set"], process_name, lower=True):
            continue
        return rule
    return None


def _match_filter_set(filter_set: set[str] | None, value, *,
                      lower: bool = False) -> bool:
    """Check whether value matches the pre-split filter set.

    - filter_set is None: no filter, returns True
    - value is None: but filter_set is non-empty, returns False (cannot match)
    - Otherwise: whether value (converted to str, lowercased if needed) is in filter_set
    """
    if filter_set is None:
        return True
    if value is None:
        return False
    val_str = str(value)
    if lower:
        val_str = val_str.lower()
    return val_str in filter_set


# 保留旧接口兼容（万一有外部调用）
def _match_filter(filter_str: str, value, *,
                  case_sensitive: bool = True,
                  value_transform=None) -> bool:
    """Check whether value matches filter_str (comma-separated).

    - filter_str is empty: no filter, returns True
    - value is None: but filter_str is non-empty, returns False (cannot match)
    - Otherwise: whether value (after transform) is in the set split from filter_str
    """
    if not filter_str:
        return True
    if value is None:
        return False
    # 拆分逗号分隔的值，去除空白
    parts = [p.strip() for p in filter_str.split(",") if p.strip()]
    if not parts:
        return True
    # value 转换
    val = value
    if value_transform:
        val = value_transform(value)
    val_str = str(val)
    if case_sensitive:
        return val_str in parts
    val_lower = val_str.lower()
    return val_lower in [p.lower() for p in parts]


def has_active_rules() -> bool:
    """Whether there are enabled auto-reply rules (used to decide whether to do SSL bump)."""
    rules, _, _ = _load_rules()
    return bool(rules)


def has_active_rules_fast() -> bool:
    """Quickly check whether there are enabled rules (lock-free read of module-level flag).

    Performance optimization (v11): avoids entering _cache_lock per request under high concurrency.
    - First call (_has_rules=False) triggers _load_rules to load and update the flag
    - Subsequent calls directly read _has_rules, lock-free
    - On rule change, invalidate_cache sets it to False; next call reloads

    Used for early short-circuit when not capturing: skips _match_auto_reply overhead when no rules.
    """
    if _has_rules:
        return True
    # _has_rules 为 False 可能是"未加载"或"确实无规则"
    # 调用 _load_rules 触发加载（如果未加载），更新标志位
    # 下次调用就能直接读 _has_rules
    return has_active_rules()


def host_matches_any_rule(host: str) -> bool:
    """Check whether host matches any enabled rule's pattern (for fine-grained SSL bump decisions).

    Bump decision optimization: only do SSL bump when host matches a rule pattern,
    avoiding bumping all HTTPS traffic which breaks pinned sites (edge/bing/bilibili etc.).

    Matching: treats host as part of a URL (`https://{host}/`) and runs find_matching_rule.
    Pattern wildcards like `*httpbin.org*` will match `https://httpbin.org/`.

    Performance optimization (v11): result caching. When not capturing, this function is
    queried on every HTTPS CONNECT; caching avoids repeated traversal for the same host
    reconnecting after keep-alive breaks.
    """
    if not host:
        return False
    # 快速路径：确认无规则（_cache 已加载且为空）时直接返回 False
    # 注意：_has_rules=False 可能是"未加载"或"确实无规则"
    # _load_rules() 会区分这两种情况（加载后 _has_rules 会被正确设置）
    if not _has_rules and _cache is not None:
        # _cache 已加载但 _has_rules=False → 确实无规则
        return False
    # 查缓存（仅在有可能有规则时才查，避免无规则时填充缓存）
    with _host_match_lock:
        cached = _host_match_cache.get(host)
    if cached is not None:
        return cached
    # 缓存未命中：遍历规则
    pseudo_url = f"https://{host}/"
    rules, compiled_regexes, _ = _load_rules()
    matched = False
    for i, rx in enumerate(compiled_regexes):
        if rx is None:
            continue
        if rx.search(pseudo_url) or rx.search(host):
            matched = True
            break
    # 写缓存（带上限保护）
    with _host_match_lock:
        if len(_host_match_cache) >= _HOST_CACHE_MAX:
            # 简单 FIFO 淘汰：弹出一个最旧的 key
            _host_match_cache.pop(next(iter(_host_match_cache)), None)
        _host_match_cache[host] = matched
    return matched
