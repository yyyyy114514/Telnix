"""API 访问控制：本地回环免鉴权 + 非回环需 Token。

威胁模型：
- Telnix 是本地抓包代理工具，默认仅本机（127.0.0.1 / ::1）访问 Web UI 与 API。
- 当用户开启"允许局域网设备连接"时，Web/API 会监听 0.0.0.0，
  局域网内其他设备（甚至同网段的陌生设备）也能访问完整控制面
  （可触发抓包、改系统代理、装/卸根证书、执行自定义发包等高危操作）。

缓解策略：
- 回环地址（本机浏览器）直接信任，无需 Token，桌面体验不变。
- 非回环地址（手机、局域网其他设备）必须携带有效 Token
  （X-API-Token 头或 ?token= 查询参数），否则返回 401。
- Token 首次启动时自动生成并持久化到设置库，常驻内存缓存，读取开销极低。
- 比较使用 hmac.compare_digest 做常量时间比较，避免计时侧信道。
"""

import hashlib
import hmac
import secrets

from fastapi import Request

from .. import settings_store

_API_TOKEN_KEY = "api_token"
_TOKEN_CACHE: dict = {"value": None}


def _ensure_token() -> str:
    """读取或生成 API Token（进程内缓存，避免每次请求读库）。"""
    cached = _TOKEN_CACHE["value"]
    if cached:
        return cached
    try:
        v = settings_store.get_setting(_API_TOKEN_KEY)
    except Exception:  # noqa: BLE001
        v = None
    if not v or not isinstance(v, str) or len(v) < 16:
        v = secrets.token_hex(32)
        try:
            settings_store.set_setting(_API_TOKEN_KEY, v)
        except Exception:  # noqa: BLE001
            pass
    _TOKEN_CACHE["value"] = v
    return v


def get_api_token() -> str:
    """返回当前 API Token（供移动端证书下载链接等场景使用）。"""
    return _ensure_token()


def is_loopback(host: str) -> bool:
    """判断请求来源是否为本地回环地址。"""
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


def authorize(request: Request) -> bool:
    """校验请求是否允许访问 API。

    - 回环来源：免鉴权。
    - 非回环来源：必须携带与本地 Token 一致的 X-API-Token 头或 token 查询参数。
    """
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
    except Exception:  # noqa: BLE001
        return False
