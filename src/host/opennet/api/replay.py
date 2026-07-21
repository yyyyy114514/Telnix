"""请求重放 API（支持改参数重放）。"""

import asyncio
import json
import socket
import ssl
from urllib.parse import urlsplit

from fastapi import APIRouter, Request
from pydantic import BaseModel

from .. import db
from ..proxy.server import Headers, SocketReader, read_body, _reason  # noqa: F401
from . import err, ok

router = APIRouter()


class ReplayOverride(BaseModel):
    """重放时的覆盖参数。"""
    method: str | None = None
    url: str | None = None
    host: str | None = None       # 重定向到其他 host:port
    port: int | None = None
    scheme: str | None = None     # http | https
    headers: dict | None = None   # 覆盖/新增请求头
    body: str | None = None       # 覆盖请求体
    fuzz: str | None = None       # fuzz 表达式: key=start..end（暂支持 JSON body 数值字段）


@router.post("/flows/{flow_id}/replay")
async def replay_flow(flow_id: int, body: ReplayOverride | None = None):
    """重放指定流量的请求（支持改参数）。

    不传 body = 原样重放；
    传 body = 按覆盖参数重放。
    """
    flow = db.get_flow(flow_id)
    if not flow:
        return err("流量不存在")
    try:
        # fuzz 模式：批量重放
        if body and body.fuzz:
            # _replay_fuzz 内部多次同步阻塞，放线程池避免卡事件循环
            results = await asyncio.to_thread(_replay_fuzz, flow, body)
            return ok({"results": results, "count": len(results)})
        # _replay 是同步阻塞调用（socket.settimeout 30s），必须放线程池，
        # 否则会阻塞整个事件循环导致后端无法 accept 新连接
        result = await asyncio.to_thread(_replay, flow, body)
        return ok(result)
    except Exception as e:  # noqa: BLE001
        return err(f"重放失败: {e}")


def _replay(flow: dict, override: ReplayOverride | None = None) -> dict:
    url = flow.get("url") or ""
    sp = urlsplit(url)
    scheme = sp.scheme or "http"
    host = sp.hostname
    port = sp.port or (443 if scheme == "https" else 80)
    path = (sp.path or "/") + (("?" + sp.query) if sp.query else "")
    method = flow.get("method") or "GET"
    body_bytes = _to_bytes(flow.get("request_body") or "")

    # 应用 override
    if override:
        if override.url:
            sp2 = urlsplit(override.url)
            if sp2.scheme:
                scheme = override.scheme or sp2.scheme
            if sp2.hostname:
                host = sp2.hostname
            if sp2.port:
                port = sp2.port
            path = (sp2.path or "/") + (("?" + sp2.query) if sp2.query else "")
        if override.scheme:
            scheme = override.scheme
        if override.host:
            host = override.host
        if override.port:
            port = override.port
        if override.method:
            method = override.method
        if override.body is not None:
            body_bytes = _to_bytes(override.body)

    if not host:
        raise ValueError("无效的 URL")

    headers = Headers.from_dict(_safe_json(flow.get("request_headers")))
    headers.set("Host", host + (f":{port}" if port not in (80, 443) else ""))
    headers.set("Connection", "close")
    headers.remove("Transfer-Encoding")
    if override and override.headers:
        for k, v in override.headers.items():
            headers.set(k, str(v))

    target = socket.create_connection((host, port), timeout=30)
    try:
        if scheme == "https":
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            target = ctx.wrap_socket(target, server_hostname=host)
        target.settimeout(30)
        req_line = f"{method} {path} HTTP/1.1\r\n".encode("latin-1")
        target.sendall(req_line + headers.to_bytes() + b"\r\n" + body_bytes)

        reader = SocketReader(target)
        status_line = reader.read_line()
        sp_parts = status_line.decode("latin-1").split(" ", 2)
        status_code = int(sp_parts[1]) if len(sp_parts) >= 2 and sp_parts[1].isdigit() else 0
        resp_headers = Headers.from_lines(reader.read_headers())
        resp_body = read_body(reader, resp_headers, method=method,
                              status_code=status_code)
        return {
            "status_code": status_code,
            "response_headers": resp_headers.to_dict(),
            "response_body": _to_text(resp_body),
            "size": len(resp_body),
        }
    finally:
        try:
            target.close()
        except OSError:
            pass


def _replay_fuzz(flow: dict, override: ReplayOverride) -> list:
    """批量 fuzz 重放：override.fuzz = 'user_id=1..100'。"""
    import re
    m = re.match(r"^(\w+)=(\d+)\.\.(\d+)$", override.fuzz.strip())
    if not m:
        raise ValueError("fuzz 表达式格式错误，应为 key=start..end")
    key, start, end = m.group(1), int(m.group(2)), int(m.group(3))
    if end - start > 500:
        raise ValueError("fuzz 范围过大（>500），拒绝执行")
    results = []
    for val in range(start, end + 1):
        # 修改 JSON body 里的 key 字段
        body_str = flow.get("request_body") or ""
        try:
            data = json.loads(body_str)
            data[key] = val
            new_body = json.dumps(data, ensure_ascii=False)
        except Exception:  # noqa: BLE001
            new_body = body_str
        # 复制 override，去掉 fuzz，设置 body
        ov = override.model_copy(deep=True)
        ov.fuzz = None
        ov.body = new_body
        try:
            r = _replay(flow, override=ov)
            r["fuzz_value"] = val
            results.append(r)
        except Exception as e:  # noqa: BLE001
            results.append({"fuzz_value": val, "error": str(e)})
    return results


def _safe_json(s):
    if not s:
        return {}
    try:
        return json.loads(s)
    except Exception:  # noqa: BLE001
        return {}


def _to_bytes(s) -> bytes:
    if not s:
        return b""
    if isinstance(s, bytes):
        return s
    if s.startswith("base64:"):
        import base64
        try:
            return base64.b64decode(s[7:])
        except Exception:  # noqa: BLE001
            return b""
    return s.encode("utf-8")


def _to_text(b: bytes) -> str:
    if not b:
        return ""
    try:
        return b.decode("utf-8")
    except UnicodeDecodeError:
        import base64
        return "base64:" + base64.b64encode(b).decode("ascii")
