"""Send packet API: construct HTTP request from scratch and send (Composer feature).

Supports method/url/headers/body/timeout, independent of capture flow, does not write to flows table.
"""

import asyncio
import ipaddress
import json
import os
import socket
import ssl
import time
from urllib.parse import urlsplit

from fastapi import APIRouter
from pydantic import BaseModel

from ..proxy.server import Headers, SocketReader, read_body
from .. import logger
from . import err, ok

router = APIRouter()


class SendRequest(BaseModel):
    """Send packet request parameters."""
    method: str = "GET"
    url: str
    headers: dict | None = None      # Custom request headers
    body: str | None = None          # Request body (string)
    timeout: float = 30.0            # Timeout seconds


@router.post("/send")
async def send_request(req: SendRequest):
    """Send custom HTTP request and return response.

    Does not go through proxy, direct socket connection to target server.
    Does not write to flows table (send packet is independent feature, decoupled from capture).
    """
    method = (req.method or "GET").upper().strip()
    if method not in {"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"}:
        return err(f"Unsupported HTTP method: {method}")
    url = (req.url or "").strip()
    if not url:
        return err("URL cannot be empty")
    if not url.startswith(("http://", "https://")):
        return err("URL must start with http:// or https://")

    try:
        result = await asyncio.to_thread(_do_send, req)
        return ok(result)
    except asyncio.TimeoutError:
        return err(f"Request timeout ({req.timeout}s)")
    except Exception as e:  # noqa: BLE001
        return err(f"Request failed: {e}")


def _resolve_safe_target(host: str, port: int) -> str | None:
    """SSRF protection: resolve host and verify each candidate IP is not internal/loopback/link-local/reserved address.

    Returns IP string for direct connection; returns None if forbidden or unresolvable.
    Key: only use resolved IP for direct connection, to avoid secondary resolution being bypassed by DNS rebinding.
    Set environment variable TELNIX_DISABLE_SSRF_GUARD=1 to disable (local debugging only, security risk).
    """
    # S3 修复：移除 TELNIX_DISABLE_SSRF_GUARD 的"跳过校验直接连接"分支。
    # 原实现一旦该全局 env 被设置，即对内网/元数据(169.254.169.254)发起 SSRF，
    # 风险过高。现该 env 仅记录告警、不再具有绕过效果；本地调试内网请通过
    # 受控转发方式，而非关闭全局守卫。
    if os.environ.get("TELNIX_DISABLE_SSRF_GUARD") == "1":
        logger.warning(
            "api", "TELNIX_DISABLE_SSRF_GUARD 已不再绕过 SSRF 校验",
            "访问内网/元数据请通过其他受控方式，而非关闭全局守卫"
        )
    if not host:
        return None
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except Exception:  # noqa: BLE001
        return None
    for info in infos:
        try:
            addr = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if (addr.is_private or addr.is_loopback or addr.is_link_local
                or addr.is_reserved or addr.is_multicast or addr.is_unspecified):
            return None
    # 全部通过校验，使用第一个解析结果直连（不再二次解析）
    return infos[0][4][0]


def _do_send(req: SendRequest) -> dict:
    """Synchronously execute HTTP request (called in thread pool)."""
    sp = urlsplit(req.url)
    scheme = sp.scheme or "http"
    host = sp.hostname
    if not host:
        raise ValueError("Invalid URL: missing host")
    port = sp.port or (443 if scheme == "https" else 80)
    path = (sp.path or "/") + (("?" + sp.query) if sp.query else "")
    method = (req.method or "GET").upper().strip()
    body_bytes = (req.body or "").encode("utf-8") if req.body else b""

    # 构造请求头
    headers = Headers()
    headers.set("Host", host + (f":{port}" if port not in (80, 443) else ""))
    headers.set("Connection", "close")
    headers.set("User-Agent", "Telnix-Composer/1.0")
    if body_bytes:
        headers.set("Content-Length", str(len(body_bytes)))
    # 用户自定义请求头覆盖默认
    if req.headers:
        for k, v in req.headers.items():
            if k.lower() in ("host", "connection", "content-length"):
                continue  # 不允许用户覆盖这些
            headers.set(k, str(v))
    # 如果用户没设 Content-Type 但有 body，默认 text/plain
    if body_bytes and not headers.get("Content-Type"):
        headers.set("Content-Type", "text/plain; charset=utf-8")

    timeout = max(1.0, min(300.0, float(req.timeout or 30.0)))
    resolved_ip = _resolve_safe_target(host, port)
    if resolved_ip is None:
        raise ValueError("Target address is forbidden or unresolvable (private/loopback/link-local/reserved address, SSRF risk)")
    t0 = time.time()
    target = socket.create_connection((resolved_ip, port), timeout=timeout)
    try:
        if scheme == "https":
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            target = ctx.wrap_socket(target, server_hostname=host)
        target.settimeout(timeout)
        req_line = f"{method} {path} HTTP/1.1\r\n".encode("latin-1")
        target.sendall(req_line + headers.to_bytes() + b"\r\n" + body_bytes)

        reader = SocketReader(target)
        status_line = reader.read_line()
        sp_parts = status_line.decode("latin-1").split(" ", 2)
        status_code = int(sp_parts[1]) if len(sp_parts) >= 2 and sp_parts[1].isdigit() else 0
        reason = sp_parts[2].strip() if len(sp_parts) >= 3 else ""
        resp_headers = Headers.from_lines(reader.read_headers())
        resp_body = read_body(reader, resp_headers, method=method, status_code=status_code)
        duration_ms = int((time.time() - t0) * 1000)
        return {
            "status_code": status_code,
            "reason": reason,
            "response_headers": resp_headers.to_dict(),
            "response_body": _to_text(resp_body),
            "size": len(resp_body),
            "duration_ms": duration_ms,
            "request": {
                "method": method,
                "url": req.url,
                "headers": headers.to_dict(),
                "body": req.body or "",
            },
        }
    finally:
        try:
            target.close()
        except OSError:
            pass


def _to_text(b: bytes) -> str:
    if not b:
        return ""
    try:
        return b.decode("utf-8")
    except UnicodeDecodeError:
        import base64
        return "base64:" + base64.b64encode(b).decode("ascii")
