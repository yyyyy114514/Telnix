"""WebSocket frame parsing and bidirectional relay.

After the HTTP/1.1 Upgrade: websocket handshake succeeds (101 response), the
connection switches to the WebSocket frame protocol. This module is responsible for:
- Parsing WebSocket frames (RFC 6455)
- Bidirectionally relaying client<->server frames (transparent fragment forwarding,
  not waiting for FIN before forwarding)
- Recording per message (aggregating consecutive frames with the same opcode) to the
  flows table (asynchronously recorded by a background thread)

Frame format (RFC 6455):
  0                   1                   2                   3
  0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1
 +-+-+-+-+-------+-+-------------+-------------------------------+
 |F|R|R|R| opcode|M| Payload len |    Extended payload length    |
 |I|S|S|S|  (4)  |A|     (7)     |             (16/64)           |
 |N|V|V|V|       |S|             |   (if payload len==126/127)   |
 | |1|2|3|       |K|             |                               |
 +-+-+-+-+-------+-+-------------+ - - - - - - - - - - - - - - - +
 |     Extended payload length continued, if payload len == 127  |
 + - - - - - - - - - - - - - - - +-------------------------------+
 |                               |Masking-key, if MASK set to 1  |
 +-------------------------------+-------------------------------+
 | Masking-key (continued)       |          Payload Data         |
 +-------------------------------- - - - - - - - - - - - - - - - +
 :                     Payload Data continued ...                :
 + - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - +
 |                     Payload Data continued ...                |
 +---------------------------------------------------------------+
"""

from __future__ import annotations

import base64
import os
import socket
import struct
import threading
import time
from datetime import datetime
from typing import Callable, Optional

from .. import db, logger


# WebSocket opcode
OP_CONT = 0x0   # continuation
OP_TEXT = 0x1   # text frame
OP_BIN = 0x2    # binary frame
OP_CLOSE = 0x8  # close
OP_PING = 0x9   # ping
OP_PONG = 0xA   # pong

_OPCODE_NAMES = {
    OP_CONT: "continuation", OP_TEXT: "text", OP_BIN: "binary",
    OP_CLOSE: "close", OP_PING: "ping", OP_PONG: "pong",
}

# 单帧大小限制（1MB，与浏览器默认一致，防止恶意大帧 OOM）
_MAX_FRAME_SIZE = 1 * 1024 * 1024
# 单 message 聚合大小上限（防止分片放大攻击）
_MAX_MESSAGE_SIZE = 16 * 1024 * 1024

# 后台记录线程池（避免阻塞 WS 转发主路径）
_WS_RECORD_EXECUTOR = None
_WS_RECORD_LOCK = threading.Lock()


def _get_ws_record_executor():
    """Lazily start a background thread pool for WS message recording.

    Performance: workers increased from 2 to 4, avoiding recorder queue buildup in
    high-concurrency WS scenarios that causes message recording delay (passthrough
    is unaffected; only DB recording timing is affected).
    """
    global _WS_RECORD_EXECUTOR
    import concurrent.futures
    if _WS_RECORD_EXECUTOR is None:
        with _WS_RECORD_LOCK:
            if _WS_RECORD_EXECUTOR is None:
                _WS_RECORD_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
                    max_workers=4, thread_name_prefix="ws-recorder"
                )
    return _WS_RECORD_EXECUTOR


class WSError(Exception):
    """WebSocket protocol error."""


def read_frame(sock: socket.socket) -> Optional[tuple[int, bytes, bool, bool]]:
    """Read a WebSocket frame from the socket.

    Returns (opcode, payload, fin, rsv1) or None (connection closed).
    Client→server frames are masked; this function automatically unmasks.
    rsv1=True indicates permessage-deflate compression (not supported by this
    implementation; the caller should handle it).
    """
    # 读 2 字节头
    hdr = _recv_exact(sock, 2)
    if hdr is None:
        return None
    b0, b1 = hdr[0], hdr[1]
    fin = (b0 & 0x80) != 0
    rsv1 = (b0 & 0x40) != 0
    rsv2 = (b0 & 0x20) != 0
    rsv3 = (b0 & 0x10) != 0
    opcode = b0 & 0x0F
    masked = (b1 & 0x80) != 0
    payload_len = b1 & 0x7F

    # RSV2/RSV3 必须为 0（RFC 6455），RSV1 仅在 permessage-deflate 协商时允许
    if rsv2 or rsv3:
        raise WSError(f"Invalid RSV bits: rsv2={rsv2} rsv3={rsv3}")

    if payload_len == 126:
        ext = _recv_exact(sock, 2)
        if ext is None:
            return None
        payload_len = struct.unpack("!H", ext)[0]
    elif payload_len == 127:
        ext = _recv_exact(sock, 8)
        if ext is None:
            return None
        payload_len = struct.unpack("!Q", ext)[0]

    # 限制单帧大小（防止恶意大帧 OOM）
    if payload_len > _MAX_FRAME_SIZE:
        raise WSError(f"Frame too large: {payload_len} bytes (max {_MAX_FRAME_SIZE})")

    mask_key = b""
    if masked:
        mask_key = _recv_exact(sock, 4)
        if mask_key is None:
            return None

    payload = b""
    if payload_len > 0:
        payload = _recv_exact(sock, payload_len)
        if payload is None:
            return None
        if masked:
            payload = _apply_mask(payload, mask_key)

    return opcode, payload, fin, rsv1


def write_frame(sock: socket.socket, opcode: int, payload: bytes,
                fin: bool = True, mask: bool = False, rsv1: bool = False):
    """Send a WebSocket frame.

    Server→client frames are not masked; client→server frames must be masked.
    rsv1 is used to passthrough the permessage-deflate flag.
    """
    b0 = (0x80 if fin else 0) | (0x40 if rsv1 else 0) | (opcode & 0x0F)
    out = bytearray([b0])

    plen = len(payload)
    mask_bit = 0x80 if mask else 0
    if plen < 126:
        out.append(mask_bit | plen)
    elif plen < 65536:
        out.append(mask_bit | 126)
        out += struct.pack("!H", plen)
    else:
        out.append(mask_bit | 127)
        out += struct.pack("!Q", plen)

    if mask:
        mask_key = os.urandom(4)
        out += mask_key
        out += _apply_mask(payload, mask_key)
    else:
        out += payload

    # 性能优化：sock.sendall 接受 bytearray，避免转 bytes 的内存复制
    sock.sendall(out)


def _recv_exact(sock: socket.socket, n: int) -> Optional[bytes]:
    """Read exactly n bytes; returns None if the connection is closed."""
    # 预分配避免多次 realloc（大帧场景）
    buf = bytearray(n)
    view = memoryview(buf)
    got = 0
    while got < n:
        try:
            chunk = sock.recv_into(view[got:], n - got)
        except (OSError, socket.timeout):
            return None
        if not chunk:
            return None
        got += chunk
    return bytes(buf)


def _apply_mask(payload: bytes, mask_key: bytes) -> bytes:
    """XOR unmask (vectorized: int.from_bytes batch XOR, replacing byte-by-byte loop).

    Performance: 64KB message goes from ~65536 Python loops to 1 big-integer XOR, ~50x faster.
    Optimization: mask_key is fixed 4 bytes, specialized to 4-byte stride loop to avoid
    temporary mask_repeated allocation.
    """
    if not mask_key or not payload:
        return payload
    mk_len = len(mask_key)
    plen = len(payload)
    # mask_key 固定 4 字节（RFC 6455），特化为 4 字节步长
    if mk_len == 4:
        # 4 字节步长向量化
        aligned_len = (plen // 4) * 4
        if aligned_len >= 4:
            p_int = int.from_bytes(payload[:aligned_len], 'big')
            # 构造 4 字节重复的 mask 大整数
            m_int = int.from_bytes(mask_key * (aligned_len // 4), 'big')
            out = (p_int ^ m_int).to_bytes(aligned_len, 'big')
        else:
            out = b""
        # 处理尾部剩余字节（最多 3 字节）
        tail_len = plen - aligned_len
        if tail_len:
            tail = payload[aligned_len:]
            out += bytes(tail[i] ^ mask_key[(aligned_len + i) % 4] for i in range(tail_len))
        return out
    # 通用路径（非 4 字节 mask，理论上不会触发）
    aligned_len = (plen // mk_len) * mk_len
    if aligned_len == 0:
        return bytes(payload[i] ^ mask_key[i % mk_len] for i in range(plen))
    mask_repeated = (mask_key * (aligned_len // mk_len + 1))[:aligned_len]
    p_int = int.from_bytes(payload[:aligned_len], 'big')
    m_int = int.from_bytes(mask_repeated, 'big')
    out = (p_int ^ m_int).to_bytes(aligned_len, 'big')
    tail_len = plen - aligned_len
    if tail_len:
        tail = payload[aligned_len:]
        out += bytes(tail[i] ^ mask_key[(aligned_len + i) % mk_len] for i in range(tail_len))
    return out


def relay_websocket(
    client_sock: socket.socket,
    server_sock: socket.socket,
    *,
    session_id: Optional[int],
    pid: Optional[int],
    proc_name: str,
    method: str,
    url: str,
    scheme: str,
    host: str,
    path: str,
    request_headers: dict,
    remote_ip: str = "",
    ip_region: str = "",
    capturing: bool = False,
    max_messages: int = 500,
    idle_timeout: int = 300,
):
    """Bidirectionally relay WebSocket, aggregating per message to record to the flows table.

    - client_sock: client-side socket (TLS/HTTP handshake already completed)
    - server_sock: target server-side socket (connection after receiving the 101 response)
    - max_messages: maximum number of messages recorded per connection (prevents malicious
      long-connection flooding)
    - idle_timeout: idle timeout in seconds (disconnect when no frames at all)

    Relay strategy: transparent fragment forwarding (each frame is forwarded as-is,
    preserving FIN/opcode/RSV1), no longer waiting for FIN before forwarding, avoiding
    first-byte latency accumulation for fragmented messages.
    Only when FIN=1 is the complete message aggregated and recorded to the DB
    (asynchronously by a background thread).
    """
    msg_count = 0
    stop_flag = threading.Event()

    def _direction(src: socket.socket, dst: socket.socket, direction: str):
        """Relay frames in one direction with transparent fragment forwarding; aggregate on FIN."""
        nonlocal msg_count
        agg_opcode = None
        agg_payload = bytearray()
        agg_rsv1 = False
        try:
            # 同时设置 src 和 dst 的 timeout：
            # src 用于 recv_into（读帧），dst 用于 sendall（写帧）
            # 若只设 src，dst 可能继承上游的短 timeout（如 30s），
            # 导致 sendall 在长连接空闲后超时，引发 code=1006 异常断开
            src.settimeout(idle_timeout)
            dst.settimeout(idle_timeout)
            while not stop_flag.is_set():
                try:
                    frame = read_frame(src)
                except (OSError, socket.timeout, WSError):
                    break
                if frame is None:
                    break
                opcode, payload, fin, rsv1 = frame

                # 控制帧（close/ping/pong）：立即转发，不参与聚合
                if opcode >= 0x8:
                    try:
                        # 控制帧不带 RSV1，且必须 FIN=1
                        write_frame(dst, opcode, payload, fin=True,
                                    mask=(direction == "c2s"), rsv1=False)
                    except OSError:
                        pass
                    if opcode == OP_CLOSE:
                        break
                    continue

                # 数据帧：立即透传（保留原 opcode/fin/rsv1）
                # 分片透传：每个帧原样转发，不等 FIN
                # CONT 帧透传时保持 CONT opcode；首帧保持 TEXT/BIN opcode
                try:
                    write_frame(dst, opcode, payload, fin=fin,
                                mask=(direction == "c2s"), rsv1=rsv1)
                except OSError:
                    pass

                # 聚合用于 DB 记录（不影响转发）
                if opcode in (OP_TEXT, OP_BIN):
                    agg_opcode = opcode
                    agg_payload = bytearray(payload)
                    agg_rsv1 = rsv1
                elif opcode == OP_CONT:
                    agg_payload += payload
                else:
                    continue

                # 检查 message 总大小（防止分片放大攻击）
                if len(agg_payload) > _MAX_MESSAGE_SIZE:
                    logger.warning("ws", f"WS message too large ({len(agg_payload)} bytes), discarding aggregation")
                    agg_payload = bytearray()
                    agg_opcode = None
                    continue

                if fin:
                    # message 聚合完成，后台记录到数据库
                    msg_payload = bytes(agg_payload)
                    record_opcode = agg_opcode or OP_TEXT
                    agg_opcode = None
                    agg_payload = bytearray()

                    if capturing and session_id and msg_count < max_messages:
                        msg_count += 1
                        # 提交到后台线程池，不阻塞转发主路径
                        try:
                            _get_ws_record_executor().submit(
                                _record_ws_message,
                                session_id, pid, proc_name, method, url,
                                scheme, host, path, request_headers,
                                direction, record_opcode, msg_payload,
                                remote_ip, ip_region,
                            )
                        except Exception as e:  # noqa: BLE001
                            logger.warning("ws", "WS record submit failed", str(e))

        except Exception as e:  # noqa: BLE001
            logger.info("ws", f"WebSocket {direction} forwarding exception", str(e))
        finally:
            # B2 修复：仅置 stop_flag 通知对端方向线程结束读循环，
            # 不再在此处对 dst 调用 shutdown(SHUT_WR)。原实现在一端（如 c2s）
            # 读到 close 帧后立即 shutdown 其对端(server_sock) 写端，会导致 server
            # 收到 FIN 提前断连，server→client 尚未转发的帧被丢弃（连接假死/丢数据）。
            # 两端 socket 的统一关闭由主函数 join 后 close() 完成（close 隐含 shutdown）。
            stop_flag.set()

    t_c2s = threading.Thread(target=_direction,
                             args=(client_sock, server_sock, "c2s"),
                             daemon=True)
    t_s2c = threading.Thread(target=_direction,
                             args=(server_sock, client_sock, "s2c"),
                             daemon=True)
    t_c2s.start()
    t_s2c.start()
    t_c2s.join()
    t_s2c.join()
    try:
        client_sock.close()
    except OSError:
        pass
    try:
        server_sock.close()
    except OSError:
        pass


def _record_ws_message(
    session_id: int, pid: Optional[int], proc_name: str,
    method: str, url: str, scheme: str, host: str, path: str,
    request_headers: dict, direction: str, opcode: int,
    payload: bytes, remote_ip: str, ip_region: str,
):
    """Record a WebSocket message to the flows table (executed in a background thread).

    - direction='c2s': client→server (request), method='WS-SEND'
    - direction='s2c': server→client (response), method='WS-RECV'
    payload is stored in request_body (c2s) or response_body (s2c); raw_data stores the raw bytes
    """
    op_name = _OPCODE_NAMES.get(opcode, f"op{opcode}")
    is_outbound = direction == "c2s"

    # payload 编码：text 直接 utf-8，binary 用 base64
    # 一次性计算，避免重复扫描
    try:
        payload_text = payload.decode("utf-8")
        body_field = payload_text
        raw_data = "base64:" + base64.b64encode(payload).decode("ascii") if payload else None
    except UnicodeDecodeError:
        b64 = base64.b64encode(payload).decode("ascii")
        body_field = "base64:" + b64
        raw_data = "base64:" + b64 if payload else None

    flow = {
        "session_id": session_id,
        "timestamp": datetime.now().isoformat(),
        "pid": pid,
        "process_name": proc_name,
        "method": "WS-SEND" if is_outbound else "WS-RECV",
        "url": url,
        "scheme": scheme,
        "host": host,
        "path": path,
        "request_headers": "{}",
        "request_body": body_field if is_outbound else "",
        "status_code": None,
        "response_headers": "{}",
        "response_body": body_field if not is_outbound else "",
        "duration_ms": 0,
        "size": len(payload),
        "protocol": "ws",
        "raw_data": raw_data,
        "remote_ip": remote_ip or None,
        "ip_region": ip_region or None,
    }
    try:
        db.insert_flow_async(flow)
    except Exception as e:  # noqa: BLE001
        logger.warning("ws", "WebSocket message record failed", str(e))


def is_websocket_upgrade(headers) -> bool:
    """Determine whether the HTTP request is a WebSocket Upgrade request.

    Checks the Upgrade: websocket and Connection: Upgrade headers.
    """
    upgrade = (headers.get("Upgrade") or "").lower()
    connection = (headers.get("Connection") or "").lower()
    return "websocket" in upgrade and "upgrade" in connection


def compute_accept_key(sec_websocket_key: str) -> str:
    """Compute the Sec-WebSocket-Accept value (RFC 6455 §4.2.2).

    SHA1(key + magic GUID) then base64 encode.
    """
    import hashlib
    magic = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
    digest = hashlib.sha1((sec_websocket_key + magic).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")
