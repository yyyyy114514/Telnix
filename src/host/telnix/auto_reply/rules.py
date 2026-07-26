"""自动回复规则管理 + 通配符匹配。

规则存于 SQLite（auto_reply_rules 表）。匹配模式：
- wildcard：* -> .*，? -> .，转义其它正则元字符
- exact：完全相等
- regex：直接当正则

匹配目标为完整 URL。提供内存缓存（带 TTL），规则变更时刷新。

性能优化（v10）：
- _cache 改为三元组 (rules, compiled_regexes, filters)，预编译正则 + 预拆分 filter
- 修复空 list falsy bug（无规则时每次查 DB）
- TTL 从 2 秒提到 10 秒，靠 invalidate_cache 主动失效
- 排序在加载时一次完成，不再每请求 sorted()

性能优化（v11，未抓包场景）：
- 模块级 _has_rules 标志位：无锁读，避免高并发下每请求都进 _cache_lock
- host_matches_any_rule 结果 LRU 缓存：未抓包时每次 CONNECT 都查，缓存避免重复遍历
- has_active_rules_fast()：用 _has_rules 快速判断，无规则时跳过 find_matching_rule
"""

import re
import threading
import time

from .. import db

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
    """规则变更后调用，清空缓存。"""
    global _cache_ts, _cache, _has_rules
    with _cache_lock:
        _cache_ts = 0.0
        _cache = None  # 彻底清空，下次 _load_rules 重新加载
        _has_rules = False  # 保守置 False，下次 _load_rules 会重新计算
    # 清空 host 匹配缓存（规则变了，旧结果失效）
    with _host_match_lock:
        _host_match_cache.clear()


def _load_rules() -> tuple[list[dict], list[re.Pattern | None], list[dict]]:
    """加载启用规则，预编译正则 + 预拆分 filter + 预排序。

    返回 (rules, compiled_regexes, filters)：
    - rules: 启用的规则列表（按 pattern 长度降序，一次排序）
    - compiled_regexes: 每条规则预编译的正则（与 rules 一一对应，None=编译失败）
    - filters: 每条规则预拆分的 filter dict {method_parts, status_parts, ...}
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
    """预拆分 filter 字段，避免每请求都 split + strip。

    返回 {method_set, status_set, pid_set, process_set}，每个是 set 或 None（None=不过滤）。
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
    """通配符转正则：* -> .*，? -> .，其它转义。"""
    out = []
    for ch in pattern:
        if ch == "*":
            out.append(".*")
        elif ch == "?":
            out.append(".")
        else:
            out.append(re.escape(ch))
    return re.compile("^" + "".join(out) + "$", re.IGNORECASE)


def _compile(pattern: str, mode: str) -> re.Pattern | None:
    try:
        if mode == "regex":
            return re.compile(pattern, re.IGNORECASE)
        if mode == "exact":
            return re.compile("^" + re.escape(pattern) + "$", re.IGNORECASE)
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
    """返回匹配 url 的启用规则，无则 None。

    优先级：更具体的 pattern 优先（pattern 字符串越长越具体）。
    这样 *logii.steamstart.top/api/usage/verify* 会优先于 *steamstart.top* 匹配。

    额外过滤字段（§4.1）：
    - method_filter: 逗号分隔的 HTTP 方法，空=不过滤，非空=method 必须在其中
    - status_filter: 逗号分隔的状态码，空=不过滤，非空=status_code 必须在其中
    - pid_filter: 逗号分隔的 PID，空=不过滤，非空=pid 必须在其中
    - process_filter: 逗号分隔的进程名，空=不过滤，非空=process_name 必须在其中

    性能优化：正则已预编译，filter 已预拆分为 set，匹配只需 O(1) 查找。
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
    """检查 value 是否匹配预拆分的 filter set。

    - filter_set 为 None：不过滤，返回 True
    - value 为 None：但 filter_set 非空，返回 False（无法匹配）
    - 否则：value（转 str，按需 lower）是否在 filter_set 中
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
    """检查 value 是否匹配 filter_str（逗号分隔）。

    - filter_str 为空：不过滤，返回 True
    - value 为 None：但 filter_str 非空，返回 False（无法匹配）
    - 否则：value（经 transform 后）是否在 filter_str 拆分后的集合中
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
    """是否有启用的自动回复规则（用于决定是否做 SSL bump）。"""
    rules, _, _ = _load_rules()
    return bool(rules)


def has_active_rules_fast() -> bool:
    """快速检查是否有启用的规则（无锁读模块级标志位）。

    性能优化（v11）：避免高并发下每请求都进 _cache_lock。
    - 首次调用（_has_rules=False）会触发 _load_rules 加载并更新标志位
    - 后续调用直接读 _has_rules，无锁
    - 规则变更时 invalidate_cache 会置 False，下次调用重新加载

    用于未抓包时的早期短路：无规则时跳过 _match_auto_reply 等开销。
    """
    if _has_rules:
        return True
    # _has_rules 为 False 可能是"未加载"或"确实无规则"
    # 调用 _load_rules 触发加载（如果未加载），更新标志位
    # 下次调用就能直接读 _has_rules
    return has_active_rules()


def host_matches_any_rule(host: str) -> bool:
    """检查 host 是否匹配某条启用规则的 pattern（用于精细化 SSL bump 决策）。

    bump 决策优化：只有 host 匹配某条规则 pattern 时才做 SSL bump，
    避免对所有 HTTPS 流量都 bump 导致钉扎站点（edge/bing/bilibili 等）断连。

    匹配方式：把 host 当成 URL 的一部分（`https://{host}/`）跑 find_matching_rule。
    pattern 通配符如 `*httpbin.org*` 会匹配 `https://httpbin.org/`。

    性能优化（v11）：结果缓存。未抓包时每次 HTTPS CONNECT 都会查此函数，
    同一 host 反复连接（keep-alive 断开后重连）时缓存避免重复遍历。
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
