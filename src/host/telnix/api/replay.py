"""Request replay API (supports replay with modified parameters)."""

import asyncio
import ipaddress
import json
import os
import socket
import ssl
from urllib.parse import urlsplit

from fastapi import APIRouter, Request
from pydantic import BaseModel

from .. import db, logger
from ..logger import _capture_log
from ..proxy.server import Headers, SocketReader, read_body, _reason  # noqa: F401
from . import err, ok

router = APIRouter()


class ReplayOverride(BaseModel):
    """Override parameters for replay."""
    method: str | None = None
    url: str | None = None
    host: str | None = None       # Redirect to other host:port
    port: int | None = None
    scheme: str | None = None     # http | https
    headers: dict | None = None   # Override/add request headers
    body: str | None = None       # Override request body
    fuzz: str | None = None       # fuzz expression: key=start..end (currently supports JSON body numeric fields)


class RepeatOptions(BaseModel):
    """Repeat Advanced: concurrent batch replay with stats."""
    count: int = 1                # Total replay count (1-1000)
    concurrency: int = 1          # Concurrent workers (1-50)
    interval_ms: int = 0          # Delay between batches in milliseconds (0=no delay)
    override: ReplayOverride | None = None  # Optional override applied to all replays


@router.post("/flows/{flow_id}/replay")
async def replay_flow(flow_id: int, body: ReplayOverride | None = None):
    """Replay specified flow's request (supports parameter modification).

    No body = replay as-is;
    With body = replay with override parameters.
    """
    flow = db.get_flow(flow_id)
    if not flow:
        return err("Flow not found")
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
        return err(f"Replay failed: {e}")


@router.post("/flows/{flow_id}/repeat")
async def repeat_flow(flow_id: int, opts: RepeatOptions):
    """Repeat Advanced: replay a flow N times with concurrency and interval control.

    Returns aggregated results plus statistics (success/fail counts, duration min/max/avg,
    status code distribution). Each replay reuses the existing _replay logic.
    """
    # 参数范围校验，避免恶意大并发拖垮后端
    if opts.count < 1 or opts.count > 1000:
        return err("count must be between 1 and 1000")
    if opts.concurrency < 1 or opts.concurrency > 50:
        return err("concurrency must be between 1 and 50")
    if opts.interval_ms < 0 or opts.interval_ms > 60000:
        return err("interval_ms must be between 0 and 60000")
    flow = db.get_flow(flow_id)
    if not flow:
        return err("Flow not found")
    try:
        results, stats = await asyncio.to_thread(
            _repeat_replay, flow, opts.count, opts.concurrency, opts.interval_ms, opts.override
        )
        return ok({
            "count": opts.count,
            "concurrency": opts.concurrency,
            "interval_ms": opts.interval_ms,
            "results": results,
            "stats": stats,
        })
    except Exception as e:  # noqa: BLE001
        return err(f"Repeat replay failed: {e}")


def _repeat_replay(
    flow: dict,
    count: int,
    concurrency: int,
    interval_ms: int,
    override: ReplayOverride | None,
) -> tuple[list, dict]:
    """Server-side batch replay worker.

    Uses ThreadPoolExecutor with bounded workers; optional sleep between submissions.
    Returns (results, stats) where stats aggregates success/fail/duration/status distribution.
    """
    import time
    from concurrent.futures import ThreadPoolExecutor, as_completed

    interval_s = interval_ms / 1000.0
    results: list = [None] * count  # 预分配按 index 排序，避免后续 sort
    success = 0
    fail = 0
    durations: list = []
    status_dist: dict = {}

    def _one(idx: int) -> tuple[int, dict]:
        t0 = time.time()
        try:
            r = _replay(flow, override)
            dt = (time.time() - t0) * 1000.0
            r["index"] = idx
            r["duration_ms"] = round(dt, 2)
            r["ok"] = True
            return idx, r
        except Exception as e:  # noqa: BLE001
            dt = (time.time() - t0) * 1000.0
            return idx, {
                "index": idx, "ok": False, "error": str(e),
                "duration_ms": round(dt, 2),
            }

    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        futures = []
        for i in range(count):
            futures.append(ex.submit(_one, i))
            if interval_s > 0:
                time.sleep(interval_s)
        for fut in as_completed(futures):
            idx, r = fut.result()
            results[idx] = r
            if r.get("ok"):
                success += 1
                durations.append(r.get("duration_ms", 0))
                sc = r.get("status_code")
                if sc is not None:
                    status_dist[str(sc)] = status_dist.get(str(sc), 0) + 1
            else:
                fail += 1

    stats = {
        "success": success,
        "fail": fail,
        "duration_min_ms": round(min(durations), 2) if durations else 0,
        "duration_max_ms": round(max(durations), 2) if durations else 0,
        "duration_avg_ms": round(sum(durations) / len(durations), 2) if durations else 0,
        "status_distribution": status_dist,
    }
    return results, stats


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
    except Exception as e:  # noqa: BLE001
        _capture_log("error", "API exception in replay.py", extra={"exc": repr(e)})
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
        raise ValueError("Invalid URL")

    resolved_ip = _resolve_safe_target(host, port)
    if resolved_ip is None:
        raise ValueError("Target address is forbidden or unresolvable (private/loopback/link-local/reserved address, SSRF risk)")

    headers = Headers.from_dict(_safe_json(flow.get("request_headers")))
    headers.set("Host", host + (f":{port}" if port not in (80, 443) else ""))
    headers.set("Connection", "close")
    headers.remove("Transfer-Encoding")
    if override and override.headers:
        for k, v in override.headers.items():
            headers.set(k, str(v))

    target = socket.create_connection((resolved_ip, port), timeout=30)
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
    """Batch fuzz replay: override.fuzz = 'user_id=1..100'."""
    import re
    m = re.match(r"^(\w+)=(\d+)\.\.(\d+)$", override.fuzz.strip())
    if not m:
        raise ValueError("Invalid fuzz expression format, expected key=start..end")
    key, start, end = m.group(1), int(m.group(2)), int(m.group(3))
    if end - start > 500:
        raise ValueError("Fuzz range too large (>500), refused to execute")
    results = []
    for val in range(start, end + 1):
        # 修改 JSON body 里的 key 字段
        body_str = flow.get("request_body") or ""
        try:
            data = json.loads(body_str)
            data[key] = val
            new_body = json.dumps(data, ensure_ascii=False)
        except Exception as e:  # noqa: BLE001
            _capture_log("error", "API exception in replay.py", extra={"exc": repr(e)})
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
    except Exception as e:  # noqa: BLE001
        _capture_log("error", "API exception in replay.py", extra={"exc": repr(e)})
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
        except Exception as e:  # noqa: BLE001
            _capture_log("error", "API exception in replay.py", extra={"exc": repr(e)})
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
