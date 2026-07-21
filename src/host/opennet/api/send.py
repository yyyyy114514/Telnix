"""发包 API：从零构造 HTTP 请求发送（Composer 功能）。

支持 method/url/headers/body/timeout，独立于抓包流程，不写入 flows 表。
"""

import asyncio
import json
import socket
import ssl
import time
from urllib.parse import urlsplit

from fastapi import APIRouter
from pydantic import BaseModel

from ..proxy.server import Headers, SocketReader, read_body
from . import err, ok

router = APIRouter()


class SendRequest(BaseModel):
    """发包请求参数。"""
    method: str = "GET"
    url: str
    headers: dict | None = None      # 自定义请求头
    body: str | None = None          # 请求体（字符串）
    timeout: float = 30.0            # 超时秒数


@router.post("/send")
async def send_request(req: SendRequest):
    """发送自定义 HTTP 请求并返回响应。

    不走代理，直接 socket 连接目标服务器。
    不写入 flows 表（发包是独立功能，与抓包解耦）。
    """
    method = (req.method or "GET").upper().strip()
    if method not in {"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"}:
        return err(f"不支持的 HTTP 方法: {method}")
    url = (req.url or "").strip()
    if not url:
        return err("URL 不能为空")
    if not url.startswith(("http://", "https://")):
        return err("URL 必须以 http:// 或 https:// 开头")

    try:
        result = await asyncio.to_thread(_do_send, req)
        return ok(result)
    except asyncio.TimeoutError:
        return err(f"请求超时（{req.timeout}s）")
    except Exception as e:  # noqa: BLE001
        return err(f"请求失败: {e}")


def _do_send(req: SendRequest) -> dict:
    """同步执行 HTTP 请求（在线程池中调用）。"""
    sp = urlsplit(req.url)
    scheme = sp.scheme or "http"
    host = sp.hostname
    if not host:
        raise ValueError("无效的 URL：缺少 host")
    port = sp.port or (443 if scheme == "https" else 80)
    path = (sp.path or "/") + (("?" + sp.query) if sp.query else "")
    method = (req.method or "GET").upper().strip()
    body_bytes = (req.body or "").encode("utf-8") if req.body else b""

    # 构造请求头
    headers = Headers()
    headers.set("Host", host + (f":{port}" if port not in (80, 443) else ""))
    headers.set("Connection", "close")
    headers.set("User-Agent", "OpenNet-Composer/1.0")
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
    t0 = time.time()
    target = socket.create_connection((host, port), timeout=timeout)
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
