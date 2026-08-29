"""API access control: loopback exempt from auth + non-loopback requires Token.

Threat model:
- Telnix is a local capture proxy tool, by default only accessible from localhost (127.0.0.1 / ::1) for Web UI and API.
- When user enables "allow LAN devices to connect", Web/API listens on 0.0.0.0,
  other devices on the LAN (even unfamiliar devices on the same subnet) can access the full control plane
  (can trigger capture, change system proxy, install/uninstall root certificate, execute custom send packets and other high-risk operations).

Mitigation strategy:
- Loopback address (local browser) is directly trusted, no Token required, desktop experience unchanged.
- Non-loopback address (mobile, other LAN devices) must carry a valid Token
  (X-API-Token header or ?token= query parameter), otherwise return 401.
- Token is automatically generated on first launch and persisted to settings store, cached in memory, very low read overhead.
- Comparison uses hmac.compare_digest for constant-time comparison, to avoid timing side channels.
"""

import hashlib
import hmac
import secrets

from fastapi import Request

from .. import settings_store
from ..logger import _capture_log

_API_TOKEN_KEY = "api_token"
_TOKEN_CACHE: dict = {"value": None}


def _ensure_token() -> str:
    """Read or generate API Token (cached in-process, avoid reading store on every request)."""
    cached = _TOKEN_CACHE["value"]
    if cached:
        return cached
    try:
        v = settings_store.get_setting(_API_TOKEN_KEY)
    except Exception as e:  # noqa: BLE001
        _capture_log("error", "API exception in auth.py", extra={"exc": repr(e)})
        v = None
    if not v or not isinstance(v, str) or len(v) < 16:
        v = secrets.token_hex(32)
        try:
            settings_store.set_setting(_API_TOKEN_KEY, v)
        except Exception as e:

            _capture_log("error", "API exception", extra={"exc": repr(e)})

            pass
    _TOKEN_CACHE["value"] = v
    return v


def get_api_token() -> str:
    """Return current API Token (for scenarios like mobile certificate download links)."""
    return _ensure_token()


def is_loopback(host: str) -> bool:
    """Determine whether request source is a local loopback address."""
    if not host:
        return False
    h = host.strip().lower()
    if h in ("127.0.0.1", "::1", "localhost", "::ffff:127.0.0.1"):
        return True
    if h.startswith("127."):
        return True
    if h.startswith("::ffff:127."):
        return True
    return False


def _host_header_is_safe(request: Request) -> bool:
    """校验 Host 头，防止 DNS rebinding 攻击。

    DNS rebinding: 恶意网站把自己的域名解析到 127.0.0.1，浏览器据此向本地
    API 发请求（源 IP 是回环，会被 is_loopback 放行）。通过校验 Host 头只允许
    localhost / IP 字面量（而非任意外部域名）可有效阻断此类攻击。
    """
    host_header = request.headers.get("host", "")
    if not host_header:
        # 无 Host 头（HTTP/1.0 或非浏览器客户端）：放行，交由 Token 逻辑判断
        return True
    # 去掉端口部分（IPv6 形如 [::1]:port）
    h = host_header.strip().lower()
    if h.startswith("["):
        # IPv6: [::1]:8080 -> ::1
        h = h[1:].split("]", 1)[0]
    else:
        h = h.rsplit(":", 1)[0] if h.count(":") == 1 else h
    if h in ("localhost", "127.0.0.1", "::1", "0.0.0.0"):
        return True
    if h.startswith("127.") or h.startswith("::ffff:127."):
        return True
    # 纯 IP 字面量（LAN 模式下的局域网 IP）放行；外部域名一律拒绝
    import ipaddress
    try:
        ipaddress.ip_address(h)
        return True
    except ValueError:
        return False


def _origin_is_safe(request: Request) -> bool:
    """CSRF 防护：校验 Origin 头。

    恶意网站可诱导浏览器向本地 API 发起跨站请求（CSRF）触发高危副作用。
    浏览器发起的跨站请求会带 Origin 头指向恶意站点。此处只对存在 Origin 头
    且为写操作(POST/PUT/DELETE/PATCH)的请求校验：Origin 的主机名必须是
    localhost / IP 字面量，外部域名一律拒绝。非浏览器客户端(CLI/移动端)通常
    不带 Origin 头，配合 Token 鉴权不受影响。
    """
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return True
    origin = request.headers.get("origin", "")
    if not origin:
        return True  # 无 Origin：非浏览器请求，交由 Token 逻辑
    try:
        from urllib.parse import urlparse
        host = (urlparse(origin).hostname or "").lower()
    except Exception as e:  # noqa: BLE001
        _capture_log("error", "API exception in auth.py", extra={"exc": repr(e)})
        return False
    if host in ("localhost", "127.0.0.1", "::1") or host.startswith("127."):
        return True
    import ipaddress
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def authorize(request: Request) -> bool:
    """Verify whether request is allowed to access API.

    - Loopback source: no auth required (but Host header must not be an external domain).
    - Non-loopback source: must carry X-API-Token header or token query parameter matching local Token.
    """
    # DNS rebinding 防护：任何请求的 Host 头都不能是外部域名
    if not _host_header_is_safe(request):
        return False
    # CSRF 防护：写操作的 Origin 头不能是外部域名
    if not _origin_is_safe(request):
        return False
    client = request.client
    host = client.host if client is not None else ""
    if is_loopback(host):
        return True
    token = request.headers.get("X-API-Token") or request.query_params.get("token")
    if not token:
        return False
    expected = _ensure_token()
    try:
        return hmac.compare_digest(token, expected)
    except Exception as e:  # noqa: BLE001
        _capture_log("error", "API exception in auth.py", extra={"exc": repr(e)})
        return False
