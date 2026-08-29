"""Cookie manager REST API.

Aggregates cookies from the flows table in real time (no separate storage):
- Request Cookie headers  -> cookies sent by the client
- Response Set-Cookie headers -> cookies set by the server (with attributes)

Grouped by host, supports host filtering, single-host clearing, and full clearing.
Clearing strips the Cookie/Set-Cookie headers from the stored flows.
"""
import json

from fastapi import APIRouter

from .. import db
from ..logger import _capture_log
from . import err, ok

router = APIRouter()

# Cookie / Set-Cookie 头名（小写匹配，HTTP 头名大小写不敏感）
_COOKIE_HEADER = "cookie"
_SET_COOKIE_HEADER = "set-cookie"


def _parse_headers(raw: str | None) -> dict[str, str]:
    """Parse a JSON header blob stored in flows table into a {name: value} dict.

    Storage format is primarily a JSON object {"Header-Name": "value"} (see
    proxy Headers.to_dict()). Also tolerates a list of {"name","value"} items
    (used by some import/export paths). Header name comparison is case-insensitive;
    on duplicate names the last value wins (mirrors to_dict() behavior).
    """
    if not raw:
        return {}
    try:
        obj = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    if isinstance(obj, dict):
        # 保持原大小写，查找时用小写匹配
        return {str(k): str(v) for k, v in obj.items()}
    if isinstance(obj, list):
        out: dict[str, str] = {}
        for item in obj:
            if not isinstance(item, dict):
                continue
            name = item.get("name") or item.get("key")
            value = item.get("value")
            if name is None:
                continue
            out[str(name)] = "" if value is None else str(value)
        return out
    return {}


def _get_header_ci(headers: dict[str, str], target_lower: str) -> str | None:
    """Case-insensitive header lookup. Returns the value or None."""
    for name, value in headers.items():
        if name.lower() == target_lower:
            return value
    return None


def _get_all_set_cookie(headers: dict[str, str]) -> list[str]:
    """Return all Set-Cookie header values (case-insensitive).

    A response may carry multiple Set-Cookie headers, but to_dict() collapses
    duplicate names into one entry (last wins). We also tolerate a JSON list
    representation where multiple Set-Cookie entries survive.
    """
    values: list[str] = []
    for name, value in headers.items():
        if name.lower() == _SET_COOKIE_HEADER:
            if isinstance(value, list):
                values.extend(str(v) for v in value)
            elif value:
                values.append(str(value))
    return values


def _parse_request_cookies(cookie_value: str) -> list[tuple[str, str]]:
    """Parse a request Cookie header value into [(name, value), ...].

    Format: "name1=value1; name2=value2" (RFC 6265). Empty/invalid pairs are
    skipped rather than raising, so one malformed entry does not drop the rest.
    """
    cookies: list[tuple[str, str]] = []
    if not cookie_value:
        return cookies
    for pair in cookie_value.split(";"):
        pair = pair.strip()
        if not pair or "=" not in pair:
            continue
        name, _, value = pair.partition("=")
        name = name.strip()
        if not name:
            continue
        cookies.append((name, value.strip()))
    return cookies


def _parse_set_cookie(value: str) -> dict | None:
    """Parse a single Set-Cookie header value into a cookie dict.

    Returns None if the value has no name=value pair. Attributes parsed:
    Domain, Path, Expires, Max-Age, Secure, HttpOnly, SameSite.
    Max-Age takes precedence over Expires when both present (RFC 6265 5.2.2).
    """
    if not value:
        return None
    parts = [p.strip() for p in value.split(";")]
    if not parts or "=" not in parts[0]:
        return None
    name, _, val = parts[0].partition("=")
    name = name.strip()
    if not name:
        return None
    cookie = {
        "name": name,
        "value": val.strip(),
        "domain": "",
        "path": "/",
        "expires": "",
        "secure": False,
        "httponly": False,
        "samesite": "",
    }
    for attr in parts[1:]:
        if not attr:
            continue
        key, _, av = attr.partition("=")
        key = key.strip().lower()
        av = av.strip()
        if key == "domain":
            cookie["domain"] = av
        elif key == "path":
            cookie["path"] = av or "/"
        elif key == "expires":
            cookie["expires"] = av
        elif key == "max-age":
            # Max-Age 覆盖 Expires（RFC 6265）；记录为可读的相对说明
            try:
                seconds = int(av)
                cookie["expires"] = f"max-age={seconds}"
            except ValueError:
                pass
        elif key == "secure":
            cookie["secure"] = True
        elif key == "httponly":
            cookie["httponly"] = True
        elif key == "samesite":
            cookie["samesite"] = av
    return cookie


def _aggregate_cookies() -> dict[str, list[dict]]:
    """Scan all flows and aggregate cookies grouped by host.

    Returns {host: [cookie, ...]}. Cookies are deduplicated by (name, domain,
    path); Set-Cookie (server-set, with attributes) takes precedence over the
    request Cookie header (which only carries name=value).
    """
    # 只取必要字段，避免拉取大体积的 request_body/response_body
    with db.get_connection() as conn:
        rows = conn.execute(
            "SELECT id, host, request_headers, response_headers, timestamp "
            "FROM flows WHERE host IS NOT NULL AND host != '' "
            "ORDER BY id ASC"
        ).fetchall()

    # key: (host, name_lower, domain_lower, path) -> cookie dict
    by_key: dict[tuple, dict] = {}
    # host -> [(name, cookie)] 保留插入顺序便于前端稳定展示
    host_order: dict[str, list[tuple]] = {}

    for row in rows:
        host = row["host"]
        if not host:
            continue
        flow_id = row["id"]
        ts = row["timestamp"]

        # 响应 Set-Cookie：带属性，优先级高
        resp_headers = _parse_headers(row["response_headers"])
        for sc in _get_all_set_cookie(resp_headers):
            parsed = _parse_set_cookie(sc)
            if not parsed:
                continue
            name = parsed["name"]
            domain = parsed["domain"] or host
            path = parsed["path"] or "/"
            key = (host, name.lower(), domain.lower(), path)
            parsed["host"] = host
            parsed["source"] = "set-cookie"
            parsed["flow_id"] = flow_id
            parsed["last_seen"] = ts
            by_key[key] = parsed
            host_order.setdefault(host, []).append((name, parsed))

        # 请求 Cookie：仅 name=value，作为补充
        req_headers = _parse_headers(row["request_headers"])
        cookie_hdr = _get_header_ci(req_headers, _COOKIE_HEADER)
        for name, value in _parse_request_cookies(cookie_hdr or ""):
            key = (host, name.lower(), host.lower(), "/")
            if key in by_key:
                # 已有 Set-Cookie 版本，仅刷新 last_seen
                existing = by_key[key]
                existing["last_seen"] = ts
                existing["flow_id"] = flow_id
                continue
            parsed = {
                "name": name,
                "value": value,
                "domain": host,
                "path": "/",
                "expires": "",
                "secure": False,
                "httponly": False,
                "samesite": "",
                "host": host,
                "source": "request-cookie",
                "flow_id": flow_id,
                "last_seen": ts,
            }
            by_key[key] = parsed
            host_order.setdefault(host, []).append((name, parsed))

    # 组装结果：每个 host 去重后保持出现顺序
    result: dict[str, list[dict]] = {}
    for host, items in host_order.items():
        deduped: list[dict] = []
        seen_keys: set[tuple] = set()
        for _name, cookie in items:
            k = (cookie["name"].lower(),
                 (cookie["domain"] or "").lower(),
                 cookie["path"] or "/")
            if k in seen_keys:
                continue
            seen_keys.add(k)
            deduped.append(cookie)
        result[host] = deduped
    return result


@router.get("/cookies")
async def list_cookies(host: str = ""):
    """Return all cookies grouped by host.

    Query param `host` (optional): fuzzy host filter (case-insensitive substring).
    Response shape:
    {
      "hosts": [
        {"host": "example.com", "count": 3, "cookies": [cookie, ...]},
        ...
      ],
      "total_hosts": 2,
      "total_cookies": 5
    }
    """
    grouped = _aggregate_cookies()
    hosts_out: list[dict] = []
    host_filter = (host or "").strip().lower()
    total_cookies = 0
    for h, cookies in grouped.items():
        if host_filter and host_filter not in h.lower():
            continue
        hosts_out.append({"host": h, "count": len(cookies), "cookies": cookies})
        total_cookies += len(cookies)
    # cookie 数多的 host 排前面
    hosts_out.sort(key=lambda x: (-x["count"], x["host"]))
    return ok({
        "hosts": hosts_out,
        "total_hosts": len(hosts_out),
        "total_cookies": total_cookies,
    })


def _strip_cookie_headers(headers_raw: str | None) -> tuple[str | None, bool]:
    """Remove Cookie/Set-Cookie headers from a JSON header blob.

    Returns (new_raw, changed). new_raw is None when input was empty/invalid.
    Preserves the original storage format (dict stays dict, list stays list).
    """
    if not headers_raw:
        return headers_raw, False
    try:
        obj = json.loads(headers_raw)
    except (ValueError, TypeError):
        return headers_raw, False
    changed = False
    if isinstance(obj, dict):
        drop = [k for k in obj if k.lower() in (_COOKIE_HEADER, _SET_COOKIE_HEADER)]
        for k in drop:
            del obj[k]
            changed = True
        new_raw = json.dumps(obj) if changed else headers_raw
    elif isinstance(obj, list):
        new_list = [item for item in obj
                    if not (isinstance(item, dict)
                            and str(item.get("name") or item.get("key") or "").lower()
                            in (_COOKIE_HEADER, _SET_COOKIE_HEADER))]
        changed = len(new_list) != len(obj)
        new_raw = json.dumps(new_list) if changed else headers_raw
    else:
        return headers_raw, False
    return new_raw, changed


@router.delete("/cookies/{host}")
async def delete_host_cookies(host: str):
    """Strip Cookie/Set-Cookie headers from all flows of the given host.

    `host` is matched by exact equality (case-insensitive) since hosts in the
    flows table are concrete values, not patterns.
    """
    host = (host or "").strip()
    if not host:
        return err("host is required")
    with db.get_connection() as conn:
        rows = conn.execute(
            "SELECT id, request_headers, response_headers FROM flows WHERE host = ? COLLATE NOCASE",
            (host,),
        ).fetchall()
        updated = 0
        for row in rows:
            req_raw, req_changed = _strip_cookie_headers(row["request_headers"])
            resp_raw, resp_changed = _strip_cookie_headers(row["response_headers"])
            if req_changed or resp_changed:
                conn.execute(
                    "UPDATE flows SET request_headers=?, response_headers=? WHERE id=?",
                    (req_raw, resp_raw, row["id"]),
                )
                updated += 1
        conn.commit()
    return ok({"host": host, "flows_updated": updated, "cleared": True})


@router.delete("/cookies")
async def delete_all_cookies():
    """Strip Cookie/Set-Cookie headers from all flows (every host)."""
    with db.get_connection() as conn:
        rows = conn.execute(
            "SELECT id, request_headers, response_headers FROM flows "
            "WHERE request_headers IS NOT NULL OR response_headers IS NOT NULL"
        ).fetchall()
        updated = 0
        for row in rows:
            req_raw, req_changed = _strip_cookie_headers(row["request_headers"])
            resp_raw, resp_changed = _strip_cookie_headers(row["response_headers"])
            if req_changed or resp_changed:
                conn.execute(
                    "UPDATE flows SET request_headers=?, response_headers=? WHERE id=?",
                    (req_raw, resp_raw, row["id"]),
                )
                updated += 1
        conn.commit()
    return ok({"flows_updated": updated, "cleared": True})
