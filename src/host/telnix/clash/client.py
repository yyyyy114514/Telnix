"""Mihomo external-controller API client.

All methods return a (data, error) tuple; when error is not None, data is None.
Pure synchronous HTTP calls (using httpx), executed by the caller in a thread pool.
"""

from __future__ import annotations

import json
import socket
import threading
import time
import urllib.parse
from typing import Any

import httpx

# ---------- 上游代理地址缓存（异步探测架构） ----------
# 性能优化：后台线程定期探测 Mihomo 状态，get_upstream_proxy() 只读内存缓存，
# 永远不阻塞代理线程。原实现每 3 秒过期，N 个代理线程同时触发探测，
# 每个阻塞 1-4 秒（is_reachable + configs HTTP 调用），导致过包卡顿。
#
# 新架构：
# - _upstream_cache["value"] = (host, port) 或 None，后台线程定期更新
# - TTL 30 秒（Mihomo 状态不会频繁变化）
# - 不可达时退避 5 秒再探测（避免每秒探测）
# - 首次调用时同步探测一次（避免启动时缓存为空）
# - enable/disable/config 时主动 invalidate + 立即触发探测
_UPSTREAM_CACHE_LOCK = threading.Lock()
_upstream_cache: dict[str, Any] = {"value": None, "ts": 0.0, "probed": False}
_UPSTREAM_CACHE_TTL = 30.0  # 秒（从 3 秒延长到 30 秒）
_UPSTREAM_NO_PROXY_BACKOFF = 5.0  # 不可达时 5 秒内不重复探测
_upstream_probe_thread: threading.Thread | None = None
_upstream_probe_stop = threading.Event()
_upstream_probe_event = threading.Event()  # 触发立即探测


def _normalize_bool(v: Any) -> bool:
    """Normalize a boolean value: tolerates bool/int/str types.

    settings.json may store a bool (from clash/enable|disable) or a "1"/"0"
    string (from the settings PUT interface). The string "0" is truthy in
    Python, so it must be checked explicitly.
    """
    if isinstance(v, bool):
        return v
    if isinstance(v, int):
        return v != 0
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)


def invalidate_upstream_proxy_cache() -> None:
    """Invalidate the upstream proxy cache. Called after enable/disable/config.

    Immediately triggers the background probe thread to re-probe, avoiding the
    30-second delay.
    """
    with _UPSTREAM_CACHE_LOCK:
        _upstream_cache["value"] = None
        _upstream_cache["ts"] = 0.0
        _upstream_cache["probed"] = False
    # 触发立即探测
    _upstream_probe_event.set()


def _upstream_probe_once() -> tuple[tuple[str, int] | None, float]:
    """Probe Mihomo status once, returning (value, next_delay).

    - Enabled and reachable -> ((host, port), TTL)
    - Enabled but unreachable -> (None, backoff)
    - Not enabled -> (None, backoff)
    """
    from .. import settings_store
    integrated = _normalize_bool(settings_store.get_setting("clash_integrated", False))
    if not integrated:
        return None, _UPSTREAM_NO_PROXY_BACKOFF

    # 启用：探测 Mihomo 是否在线
    client = get_client_from_settings()
    if not client.is_reachable():
        return None, _UPSTREAM_NO_PROXY_BACKOFF

    # 优先用用户手动配置的端口，否则从 Mihomo API 动态获取
    port_raw = settings_store.get_setting("clash_mixed_port", 0)
    try:
        port = int(port_raw) if port_raw else 0
    except (TypeError, ValueError):
        port = 0
    if not port:
        port = client.mixed_port()
        if not port:
            return None, _UPSTREAM_NO_PROXY_BACKOFF
    host = settings_store.get_setting("clash_api_host", "127.0.0.1")
    return (host, int(port)), _UPSTREAM_CACHE_TTL


def _upstream_probe_loop():
    """后台探测线程：定期探测 Mihomo 状态，更新缓存。

    首次立即探测，之后按 next_delay 休眠；invalidate 触发立即探测。
    """
    while not _upstream_probe_stop.is_set():
        try:
            value, next_delay = _upstream_probe_once()
            with _UPSTREAM_CACHE_LOCK:
                _upstream_cache["value"] = value
                _upstream_cache["ts"] = time.time()
                _upstream_cache["probed"] = True
        except Exception:  # noqa: BLE001
            with _UPSTREAM_CACHE_LOCK:
                _upstream_cache["value"] = None
                _upstream_cache["ts"] = time.time()
                _upstream_cache["probed"] = True
            next_delay = _UPSTREAM_NO_PROXY_BACKOFF
        # 等待 next_delay 或被 invalidate 触发立即探测
        _upstream_probe_event.wait(next_delay)
        _upstream_probe_event.clear()


def _ensure_probe_thread():
    """Lazily start the background probe thread (created on first get_upstream_proxy call)."""
    global _upstream_probe_thread
    if _upstream_probe_thread is not None and _upstream_probe_thread.is_alive():
        return
    with _UPSTREAM_CACHE_LOCK:
        if _upstream_probe_thread is not None and _upstream_probe_thread.is_alive():
            return
        _upstream_probe_thread = threading.Thread(
            target=_upstream_probe_loop, daemon=True, name="clash-probe"
        )
        _upstream_probe_thread.start()


def _sync_probe_first_call() -> tuple[tuple[str, int] | None, float]:
    """首次调用时同步探测一次，避免启动时缓存为空导致 30 秒直连。

    后续调用由后台线程定期更新。此函数仅在 _upstream_cache["probed"]=False 时调用。
    """
    value, next_delay = _upstream_probe_once()
    with _UPSTREAM_CACHE_LOCK:
        _upstream_cache["value"] = value
        _upstream_cache["ts"] = time.time()
        _upstream_cache["probed"] = True
    return value, next_delay


class ClashClient:
    """Mihomo RESTful API client.

    Docs: https://wiki.metacubex.one/api/
    """

    def __init__(self, api_url: str = "http://127.0.0.1:9090", secret: str = ""):
        # 容错：用户在设置页可能填 "127.0.0.1:9090" 忘加协议，自动补 http://
        # 否则 urlparse 解析出 hostname=None，_request 拼接的 URL urllib 不认
        if api_url and "://" not in api_url:
            api_url = "http://" + api_url
        self.api_url = api_url.rstrip("/")
        self.secret = secret
        # 显式禁用代理：避免 httpx 读取 http_proxy 环境变量，
        # 导致 API 调用走 Telnix 自身代理形成循环
        self._http_client = httpx.Client(
            timeout=httpx.Timeout(10.0),
            proxy=None,  # 显式禁用代理
        )

    # ---------- 底层 ----------

    def _headers(self) -> dict:
        h = {"Accept": "application/json"}
        if self.secret:
            h["Authorization"] = f"Bearer {self.secret}"
        return h

    def _request(self, method: str, path: str, body: Any = None,
                 timeout: float = 3.0, params: dict | None = None) -> tuple[Any, str | None]:
        """Send an HTTP request. Returns (data, error). Default timeout 3 seconds (avoids status interface hanging for 20s).

        Note: the timeout parameter is passed to httpx per-request, overriding the
        client-level 10s default. This ensures /version and /configs fail fast (3s)
        instead of hanging for 10 seconds when Mihomo is slow or unresponsive.
        """
        url = f"{self.api_url}{path}"
        if params:
            qs = "&".join(f"{k}={urllib.parse.quote(str(v))}" for k, v in params.items() if v is not None)
            if qs:
                url += f"?{qs}"
        data = None
        headers = self._headers()
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        try:
            # 关键：把 timeout 传给 httpx，否则用 client 级 10s 默认值
            resp = self._http_client.request(
                method, url, content=data, headers=headers, timeout=timeout,
            )
            # 非 2xx 视为错误（如 401 secret 错误、404 路径不存在、500 服务器错误）
            # httpx 默认不抛 HTTPStatusError，需手动检查状态码
            if resp.status_code >= 400:
                try:
                    err_body = resp.json()
                    msg = err_body.get("message") or str(err_body)
                except Exception:  # noqa: BLE001
                    msg = resp.text or f"HTTP {resp.status_code}"
                return None, f"HTTP {resp.status_code}: {msg}"
            if resp.status_code == 204 or not resp.text:
                return None, None  # 204 No Content
            try:
                return resp.json(), None
            except Exception:
                return resp.text, None
        except httpx.ConnectError as e:
            return None, f"Cannot connect to Mihomo ({self.api_url}): {e}"
        except httpx.ReadTimeout:
            return None, f"Request timeout ({timeout}s): {self.api_url}{path}"
        except Exception as e:  # noqa: BLE001
            return None, f"Request error: {e}"

    # ---------- 状态 ----------

    def version(self) -> tuple[dict, str | None]:
        return self._request("GET", "/version")

    def configs(self) -> tuple[dict, str | None]:
        """Get current running configuration."""
        return self._request("GET", "/configs")

    def patch_configs(self, patch: dict) -> tuple[None, str | None]:
        """Update basic configuration (PATCH)."""
        return self._request("PATCH", "/configs", patch)

    def reload_config(self, force: bool = False) -> tuple[None, str | None]:
        """Reload the configuration file."""
        return self._request("PUT", "/configs", {"path": "", "payload": ""},
                             params={"force": "true" if force else "false"})

    # ---------- 代理 / 节点 ----------

    def proxies(self) -> tuple[dict, str | None]:
        """Get all proxies and policy groups."""
        return self._request("GET", "/proxies")

    def proxy(self, name: str) -> tuple[dict, str | None]:
        """Get details of a single proxy/policy group."""
        return self._request("GET", f"/proxies/{urllib.parse.quote(name)}")

    def select_proxy(self, group: str, name: str) -> tuple[None, str | None]:
        """Switch the selected node of a policy group."""
        return self._request("PUT", f"/proxies/{urllib.parse.quote(group)}", {"name": name})

    def proxy_delay(self, name: str, url: str = "https://www.gstatic.com/generate_204",
                    timeout: int = 5000) -> tuple[dict, str | None]:
        """Test node latency. timeout is the Mihomo-side test timeout (ms); socket timeout is set to timeout/1000 + 2 seconds."""
        sock_to = timeout / 1000.0 + 2.0
        return self._request("GET", f"/proxies/{urllib.parse.quote(name)}/delay",
                             params={"url": url, "timeout": str(timeout)}, timeout=sock_to)

    def group_delay(self, group: str, url: str = "https://www.gstatic.com/generate_204",
                    timeout: int = 5000) -> tuple[dict, str | None]:
        """Test latency of all nodes in a policy group. Socket timeout is relaxed to 15 seconds (multi-node testing takes longer)."""
        return self._request("GET", f"/group/{urllib.parse.quote(group)}/delay",
                             params={"url": url, "timeout": str(timeout)}, timeout=15.0)

    def clear_fixed(self, name: str) -> tuple[None, str | None]:
        """Clear the fixed selection of a URLTest/Fallback group."""
        return self._request("DELETE", f"/proxies/{urllib.parse.quote(name)}")

    # ---------- 代理集合（订阅） ----------

    def providers(self) -> tuple[dict, str | None]:
        """Get all proxy providers (subscription sources)."""
        return self._request("GET", "/providers/proxies")

    def provider(self, name: str) -> tuple[dict, str | None]:
        """Get details of a single subscription source."""
        return self._request("GET", f"/providers/proxies/{urllib.parse.quote(name)}")

    def update_provider(self, name: str) -> tuple[None, str | None]:
        """Update (fetch) the subscription."""
        return self._request("PUT", f"/providers/proxies/{urllib.parse.quote(name)}")

    def provider_healthcheck(self, name: str) -> tuple[None, str | None]:
        """Trigger a health check for the subscription source."""
        return self._request("GET", f"/providers/proxies/{urllib.parse.quote(name)}/healthcheck")

    # ---------- 规则 ----------

    def rules(self) -> tuple[dict, str | None]:
        """Get the rule list."""
        return self._request("GET", "/rules")

    def rule_providers(self) -> tuple[dict, str | None]:
        """Get rule providers."""
        return self._request("GET", "/providers/rules")

    def update_rule_provider(self, name: str) -> tuple[None, str | None]:
        """Update a rule provider."""
        return self._request("PUT", f"/providers/rules/{urllib.parse.quote(name)}")

    # ---------- 连接 ----------

    def connections(self) -> tuple[dict, str | None]:
        """Get active connections."""
        return self._request("GET", "/connections")

    def close_all_connections(self) -> tuple[None, str | None]:
        """Close all connections."""
        return self._request("DELETE", "/connections")

    def close_connection(self, conn_id: str) -> tuple[None, str | None]:
        """Close a specific connection."""
        return self._request("DELETE", f"/connections/{urllib.parse.quote(conn_id)}")

    # ---------- DNS ----------

    def dns_query(self, name: str, qtype: str = "A") -> tuple[dict, str | None]:
        """DNS query."""
        return self._request("GET", "/dns/query", params={"name": name, "type": qtype})

    def flush_dns_cache(self) -> tuple[None, str | None]:
        """Flush the DNS cache."""
        return self._request("POST", "/cache/dns/flush")

    def flush_fakeip(self) -> tuple[None, str | None]:
        """Flush the FakeIP cache."""
        return self._request("POST", "/cache/fakeip/flush")

    # ---------- 工具 ----------

    def is_reachable(self) -> bool:
        """Check whether the Clash/Mihomo RESTful API is actually responding (not just a port open).

        This avoids false positives when some other service is listening on the API port.
        Uses the lightweight /version endpoint (1.5s timeout sufficient for local API).
        """
        data, err = self._request("GET", "/version", timeout=1.5)
        return err is None and data is not None

    def mixed_port(self) -> int | None:
        """Get Mihomo's mixed-port (used for upstream proxy forwarding).

        Prefers mixed-port from /configs, falls back to socks-port/http-port,
        returns None if none are present.
        """
        cfg, err = self.configs()
        if err or not isinstance(cfg, dict):
            return None
        for k in ("mixed-port", "socks-port", "port"):
            v = cfg.get(k)
            if v and isinstance(v, int) and v > 0:
                return v
        return None


def get_client_from_settings() -> ClashClient:
    """Read configuration from settings_store and construct a ClashClient."""
    from .. import settings_store
    api_url = settings_store.get_setting("clash_api_url", "http://127.0.0.1:9090")
    # 安全修复：clash_secret 加密存储，读取时解密
    from .. import secure_storage
    raw_secret = settings_store.get_setting("clash_secret", "")
    secret = secure_storage.decrypt(raw_secret) if secure_storage.is_encrypted(raw_secret) else raw_secret
    return ClashClient(api_url, secret)


def get_upstream_proxy() -> tuple[str, int] | None:
    """Get the upstream proxy address (Mihomo mixed-port). Never blocks; only reads the in-memory cache.

    Returns (host, port) to route traffic through the proxy; returns None for direct connection.
    - clash_integrated=False -> always None (direct traffic, even if the Clash page is visible)
    - clash_integrated=True and Mihomo reachable -> returns (host, port)
    - clash_integrated=True but Mihomo unreachable -> returns None (traffic falls back to direct,
      clash_integrated is not modified)

    Note: clash_integrated is separate from clash_enabled:
    - clash_enabled only controls the visibility of the Clash entry in the sidebar
    - clash_integrated controls whether traffic goes through the Mihomo proxy

    Performance optimization: fully non-blocking. The first call returns None directly (direct
    connection) and triggers a background probe; once the background thread probes successfully
    it fills the cache, and subsequent requests can go through the proxy.
    This avoids the original implementation's first synchronous probe that could block the first
    packet for up to 4 seconds.
    """
    # 启动后台探测线程（懒启动，首次调用时创建）
    _ensure_probe_thread()
    # 只读缓存，首次调用时缓存为空返回 None（直连），后台探测完成后自动填充
    with _UPSTREAM_CACHE_LOCK:
        return _upstream_cache["value"]


def shutdown_probe_thread():
    """Shut down the background probe thread (called on program exit)."""
    _upstream_probe_stop.set()
    _upstream_probe_event.set()
