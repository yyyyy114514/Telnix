"""Mihomo external-controller API 客户端。

所有方法返回 (data, error) 元组，error 非 None 时 data 为 None。
纯同步 HTTP 调用（用 urllib，不引入额外依赖），由调用方在线程池中执行。
"""

from __future__ import annotations

import json
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

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
    """规范化布尔值：兼容 bool/int/str 类型。

    settings.json 可能存 bool（来自 clash/enable|disable）或 "1"/"0" 字符串
    （来自 settings PUT 接口）。字符串 "0" 在 Python 中是 truthy，必须显式判断。
    """
    if isinstance(v, bool):
        return v
    if isinstance(v, int):
        return v != 0
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)


def invalidate_upstream_proxy_cache() -> None:
    """失效上游代理缓存。在 enable/disable/config 后调用。

    立即触发后台探测线程重新探测，避免 30 秒延迟。
    """
    with _UPSTREAM_CACHE_LOCK:
        _upstream_cache["value"] = None
        _upstream_cache["ts"] = 0.0
        _upstream_cache["probed"] = False
    # 触发立即探测
    _upstream_probe_event.set()


def _upstream_probe_once() -> tuple[tuple[str, int] | None, float]:
    """探测一次 Mihomo 状态，返回 (value, next_delay)。

    - 启用且可达 → ((host, port), TTL)
    - 启用但不可达 → (None, backoff)
    - 未启用 → (None, backoff)
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
    """懒启动后台探测线程（首次调用 get_upstream_proxy 时创建）。"""
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
    """Mihomo RESTful API 客户端。

    文档：https://wiki.metacubex.one/api/
    """

    def __init__(self, api_url: str = "http://127.0.0.1:9090", secret: str = ""):
        # 容错：用户在设置页可能填 "127.0.0.1:9090" 忘加协议，自动补 http://
        # 否则 urlparse 解析出 hostname=None，_request 拼接的 URL urllib 不认
        if api_url and "://" not in api_url:
            api_url = "http://" + api_url
        self.api_url = api_url.rstrip("/")
        self.secret = secret
        # 显式禁用代理：避免 urllib 读取 http_proxy 环境变量，
        # 导致 API 调用走 Telnix 自身代理形成循环
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    # ---------- 底层 ----------

    def _headers(self) -> dict:
        h = {"Accept": "application/json"}
        if self.secret:
            h["Authorization"] = f"Bearer {self.secret}"
        return h

    def _request(self, method: str, path: str, body: Any = None,
                 timeout: float = 3.0, params: dict | None = None) -> tuple[Any, str | None]:
        """发 HTTP 请求。返回 (data, error)。超时默认 3 秒（避免状态接口卡 20 秒）。"""
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
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with self._opener.open(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                if not raw:
                    return None, None  # 204 No Content
                try:
                    return json.loads(raw), None
                except json.JSONDecodeError:
                    return raw, None
        except urllib.error.HTTPError as e:
            try:
                err_body = json.loads(e.read().decode("utf-8", errors="replace"))
                msg = err_body.get("message") or str(err_body)
            except Exception:  # noqa: BLE001
                msg = f"HTTP {e.code}: {e.reason}"
            return None, msg
        except urllib.error.URLError as e:
            return None, f"无法连接 Mihomo ({self.api_url}): {e.reason}"
        except Exception as e:  # noqa: BLE001
            return None, f"请求异常: {e}"

    # ---------- 状态 ----------

    def version(self) -> tuple[dict, str | None]:
        return self._request("GET", "/version")

    def configs(self) -> tuple[dict, str | None]:
        """获取当前运行配置。"""
        return self._request("GET", "/configs")

    def patch_configs(self, patch: dict) -> tuple[None, str | None]:
        """更新基本配置（PATCH）。"""
        return self._request("PATCH", "/configs", patch)

    def reload_config(self, force: bool = False) -> tuple[None, str | None]:
        """重新加载配置文件。"""
        return self._request("PUT", "/configs", {"path": "", "payload": ""},
                             params={"force": "true" if force else "false"})

    # ---------- 代理 / 节点 ----------

    def proxies(self) -> tuple[dict, str | None]:
        """获取所有代理和策略组。"""
        return self._request("GET", "/proxies")

    def proxy(self, name: str) -> tuple[dict, str | None]:
        """获取单个代理/策略组详情。"""
        return self._request("GET", f"/proxies/{urllib.parse.quote(name)}")

    def select_proxy(self, group: str, name: str) -> tuple[None, str | None]:
        """切换策略组选中的节点。"""
        return self._request("PUT", f"/proxies/{urllib.parse.quote(group)}", {"name": name})

    def proxy_delay(self, name: str, url: str = "https://www.gstatic.com/generate_204",
                    timeout: int = 5000) -> tuple[dict, str | None]:
        """测试节点延迟。timeout 为 Mihomo 侧测试超时（ms），socket 超时设为 timeout/1000 + 2 秒。"""
        sock_to = timeout / 1000.0 + 2.0
        return self._request("GET", f"/proxies/{urllib.parse.quote(name)}/delay",
                             params={"url": url, "timeout": str(timeout)}, timeout=sock_to)

    def group_delay(self, group: str, url: str = "https://www.gstatic.com/generate_204",
                    timeout: int = 5000) -> tuple[dict, str | None]:
        """测试策略组内所有节点延迟。socket 超时放宽到 15 秒（多节点测试耗时较长）。"""
        return self._request("GET", f"/group/{urllib.parse.quote(group)}/delay",
                             params={"url": url, "timeout": str(timeout)}, timeout=15.0)

    def clear_fixed(self, name: str) -> tuple[None, str | None]:
        """清除 URLTest/Fallback 的 fixed 选择。"""
        return self._request("DELETE", f"/proxies/{urllib.parse.quote(name)}")

    # ---------- 代理集合（订阅） ----------

    def providers(self) -> tuple[dict, str | None]:
        """获取所有代理集合（订阅源）。"""
        return self._request("GET", "/providers/proxies")

    def provider(self, name: str) -> tuple[dict, str | None]:
        """获取单个订阅源详情。"""
        return self._request("GET", f"/providers/proxies/{urllib.parse.quote(name)}")

    def update_provider(self, name: str) -> tuple[None, str | None]:
        """更新（拉取）订阅。"""
        return self._request("PUT", f"/providers/proxies/{urllib.parse.quote(name)}")

    def provider_healthcheck(self, name: str) -> tuple[None, str | None]:
        """触发订阅源健康检查。"""
        return self._request("GET", f"/providers/proxies/{urllib.parse.quote(name)}/healthcheck")

    # ---------- 规则 ----------

    def rules(self) -> tuple[dict, str | None]:
        """获取规则列表。"""
        return self._request("GET", "/rules")

    def rule_providers(self) -> tuple[dict, str | None]:
        """获取规则集合。"""
        return self._request("GET", "/providers/rules")

    def update_rule_provider(self, name: str) -> tuple[None, str | None]:
        """更新规则集合。"""
        return self._request("PUT", f"/providers/rules/{urllib.parse.quote(name)}")

    # ---------- 连接 ----------

    def connections(self) -> tuple[dict, str | None]:
        """获取当前活跃连接。"""
        return self._request("GET", "/connections")

    def close_all_connections(self) -> tuple[None, str | None]:
        """关闭所有连接。"""
        return self._request("DELETE", "/connections")

    def close_connection(self, conn_id: str) -> tuple[None, str | None]:
        """关闭指定连接。"""
        return self._request("DELETE", f"/connections/{urllib.parse.quote(conn_id)}")

    # ---------- DNS ----------

    def dns_query(self, name: str, qtype: str = "A") -> tuple[dict, str | None]:
        """DNS 查询。"""
        return self._request("GET", "/dns/query", params={"name": name, "type": qtype})

    def flush_dns_cache(self) -> tuple[None, str | None]:
        """清除 DNS 缓存。"""
        return self._request("POST", "/cache/dns/flush")

    def flush_fakeip(self) -> tuple[None, str | None]:
        """清除 FakeIP 缓存。"""
        return self._request("POST", "/cache/fakeip/flush")

    # ---------- 工具 ----------

    def is_reachable(self) -> bool:
        """快速检测 Mihomo 是否在线。1 秒超时（本地端口足够，避免防火墙拦截等满 2 秒）。"""
        try:
            parsed = urllib.parse.urlparse(self.api_url)
            host = parsed.hostname or "127.0.0.1"
            port = parsed.port or 9090
            with socket.create_connection((host, port), timeout=1.0):
                return True
        except (OSError, ValueError):
            return False

    def mixed_port(self) -> int | None:
        """获取 Mihomo 的 mixed-port（用于上游代理转发）。

        优先从 /configs 读 mixed-port，回退到 socks-port/http-port，都无则 None。
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
    """从 settings_store 读取配置，构造 ClashClient。"""
    from .. import settings_store
    api_url = settings_store.get_setting("clash_api_url", "http://127.0.0.1:9090")
    secret = settings_store.get_setting("clash_secret", "")
    return ClashClient(api_url, secret)


def get_upstream_proxy() -> tuple[str, int] | None:
    """获取上游代理地址（Mihomo mixed-port）。永远不阻塞，只读内存缓存。

    返回 (host, port) 让流量走代理；返回 None 则流量直连。
    - clash_integrated=False → 永远 None（流量直连，即使 Clash 页可见）
    - clash_integrated=True 且 Mihomo 可达 → 返回 (host, port)
    - clash_integrated=True 但 Mihomo 不可达 → 返回 None（流量自动直连，不修改 clash_integrated）

    注意：clash_integrated 与 clash_enabled 分离：
    - clash_enabled 只控制侧边栏 Clash 入口可见性
    - clash_integrated 控制流量是否走 Mihomo 代理

    性能优化：后台线程定期探测，此函数只读缓存（O(1)），不阻塞代理线程。
    首次调用时同步探测一次（避免启动时 30 秒直连），后续由后台线程更新。
    """
    # 首次调用同步探测一次
    with _UPSTREAM_CACHE_LOCK:
        probed = _upstream_cache["probed"]
    if not probed:
        _sync_probe_first_call()
    # 启动后台探测线程
    _ensure_probe_thread()
    # 只读缓存
    with _UPSTREAM_CACHE_LOCK:
        return _upstream_cache["value"]


def shutdown_probe_thread():
    """关闭后台探测线程（程序退出时调用）。"""
    _upstream_probe_stop.set()
    _upstream_probe_event.set()
