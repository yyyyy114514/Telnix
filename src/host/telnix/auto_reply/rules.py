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


def invalidate_cache():
    """规则变更后调用，清空缓存。"""
    global _cache_ts, _cache
    with _cache_lock:
        _cache_ts = 0.0
        _cache = None  # 彻底清空，下次 _load_rules 重新加载


def _load_rules() -> tuple[list[dict], list[re.Pattern | None], list[dict]]:
    """加载启用规则，预编译正则 + 预拆分 filter + 预排序。

    返回 (rules, compiled_regexes, filters)：
    - rules: 启用的规则列表（按 pattern 长度降序，一次排序）
    - compiled_regexes: 每条规则预编译的正则（与 rules 一一对应，None=编译失败）
    - filters: 每条规则预拆分的 filter dict {method_parts, status_parts, ...}
    """
    global _cache, _cache_ts
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


def host_matches_any_rule(host: str) -> bool:
    """检查 host 是否匹配某条启用规则的 pattern（用于精细化 SSL bump 决策）。

    bump 决策优化：只有 host 匹配某条规则 pattern 时才做 SSL bump，
    避免对所有 HTTPS 流量都 bump 导致钉扎站点（edge/bing/bilibili 等）断连。

    匹配方式：把 host 当成 URL 的一部分（`https://{host}/`）跑 find_matching_rule。
    pattern 通配符如 `*httpbin.org*` 会匹配 `https://httpbin.org/`。
    """
    if not host:
        return False
    # 构造伪 URL 跑规则匹配（pattern 通常针对完整 URL 或 host）
    pseudo_url = f"https://{host}/"
    rules, compiled_regexes, _ = _load_rules()
    for i, rx in enumerate(compiled_regexes):
        if rx is None:
            continue
        if rx.search(pseudo_url) or rx.search(host):
            return True
    return False
