"""WebSocket 帧解析与双向转发。

在 HTTP/1.1 Upgrade: websocket 握手成功（101 响应）后，连接转为 WebSocket 帧协议。
本模块负责：
- 解析 WebSocket 帧（RFC 6455）
- 双向转发 client<->server 的帧
- 按 message（同 opcode 的连续帧聚合）记录到 flows 表

帧格式（RFC 6455）：
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
OP_TEXT = 0x1   # 文本帧
OP_BIN = 0x2    # 二进制帧
OP_CLOSE = 0x8  # 关闭
OP_PING = 0x9   # ping
OP_PONG = 0xA   # pong

_OPCODE_NAMES = {
    OP_CONT: "continuation", OP_TEXT: "text", OP_BIN: "binary",
    OP_CLOSE: "close", OP_PING: "ping", OP_PONG: "pong",
}


class WSError(Exception):
    """WebSocket 协议错误。"""


def read_frame(sock: socket.socket) -> Optional[tuple[int, bytes, bool]]:
    """从 socket 读取一个 WebSocket 帧。

    返回 (opcode, payload, fin) 或 None（连接关闭）。
    客户端→服务端的帧带 mask，本函数自动解 mask。
    """
    # 读 2 字节头
    hdr = _recv_exact(sock, 2)
    if hdr is None:
        return None
    b0, b1 = hdr[0], hdr[1]
    fin = (b0 & 0x80) != 0
    opcode = b0 & 0x0F
    masked = (b1 & 0x80) != 0
    payload_len = b1 & 0x7F

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

    # 限制单帧大小（防止恶意大帧 OOM，64MB 足够大多数场景）
    if payload_len > 64 * 1024 * 1024:
        raise WSError(f"帧过大: {payload_len} bytes")

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

    return opcode, payload, fin


def write_frame(sock: socket.socket, opcode: int, payload: bytes,
                fin: bool = True, mask: bool = False):
    """发送一个 WebSocket 帧。

    服务端→客户端的帧不 mask，客户端→服务端的帧必须 mask。
    """
    b0 = (0x80 if fin else 0) | (opcode & 0x0F)
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

    sock.sendall(bytes(out))


def _recv_exact(sock: socket.socket, n: int) -> Optional[bytes]:
    """精确读取 n 字节，连接关闭返回 None。"""
    buf = bytearray()
    while len(buf) < n:
        try:
            chunk = sock.recv(n - len(buf))
        except (OSError, socket.timeout):
            return None
        if not chunk:
            return None
        buf += chunk
    return bytes(buf)


def _apply_mask(payload: bytes, mask_key: bytes) -> bytes:
    """XOR 解 mask（向量化：int.from_bytes 批量 XOR，替代逐字节循环）。

    性能：64KB 消息从 ~65536 次 Python 循环降为 1 次大整数 XOR，提升 ~50x。
    """
    if not mask_key or not payload:
        return payload
    mk_len = len(mask_key)
    plen = len(payload)
    # 对齐到 mask_key 长度的整数倍
    aligned_len = (plen // mk_len) * mk_len
    if aligned_len == 0:
        # payload 比 mask_key 还短，逐字节处理尾部
        return bytes(payload[i] ^ mask_key[i % mk_len] for i in range(plen))
    # 构造与 payload 对齐部分等长的 mask 重复字节
    mask_repeated = (mask_key * (aligned_len // mk_len + 1))[:aligned_len]
    # int XOR 批量处理对齐部分
    p_int = int.from_bytes(payload[:aligned_len], 'big')
    m_int = int.from_bytes(mask_repeated, 'big')
    out = (p_int ^ m_int).to_bytes(aligned_len, 'big')
    # 处理尾部剩余字节（最多 mk_len-1 字节）
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
    """双向转发 WebSocket，按 message 聚合记录到 flows 表。

    - client_sock: 客户端侧 socket（已完成 TLS/HTTP 握手）
    - server_sock: 目标服务器侧 socket（已收到 101 响应后的连接）
    - max_messages: 单连接最多记录多少条 message（防止恶意长连接刷屏）
    - idle_timeout: 空闲超时秒数（无任何帧则断开）
    """
    # 每个方向独立的帧聚合缓冲
    # 当 FIN=1 时聚合完成，记录一条 flow
    msg_count = 0
    stop_flag = threading.Event()

    def _direction(src: socket.socket, dst: socket.socket, direction: str):
        """转发一个方向的帧，聚合 message 后记录。"""
        nonlocal msg_count
        agg_opcode = None
        agg_payload = bytearray()
        try:
            src.settimeout(idle_timeout)
            while not stop_flag.is_set():
                try:
                    frame = read_frame(src)
                except (OSError, socket.timeout, WSError):
                    break
                if frame is None:
                    break
                opcode, payload, fin = frame

                # 控制帧（close/ping/pong）：立即转发，不参与聚合
                if opcode >= 0x8:
                    try:
                        # close 帧原样转发到对端
                        write_frame(dst, opcode, payload, fin=True,
                                    mask=(direction == "c2s"))
                    except OSError:
                        pass
                    if opcode == OP_CLOSE:
                        break
                    continue

                # ping 请求对方回 pong，pong 直接转给对端
                if opcode == OP_PING:
                    try:
                        write_frame(dst, OP_PONG, payload, fin=True,
                                    mask=(direction == "c2s"))
                    except OSError:
                        pass
                    continue

                # 数据帧聚合
                if opcode in (OP_TEXT, OP_BIN):
                    agg_opcode = opcode
                    agg_payload = bytearray(payload)
                elif opcode == OP_CONT:
                    agg_payload += payload
                else:
                    continue

                if fin:
                    # message 聚合完成
                    msg_payload = bytes(agg_payload)
                    agg_opcode = None
                    agg_payload = bytearray()

                    # 转发整个 message 到对端（单帧，FIN=1）
                    # 注意：原帧可能分片，这里重新组装为单帧发送
                    try:
                        write_frame(dst, agg_opcode or OP_TEXT, msg_payload,
                                    fin=True, mask=(direction == "c2s"))
                    except OSError:
                        pass

                    # 记录到数据库
                    if capturing and session_id and msg_count < max_messages:
                        msg_count += 1
                        _record_ws_message(
                            session_id, pid, proc_name, method, url,
                            scheme, host, path, request_headers,
                            direction, agg_opcode or OP_TEXT, msg_payload,
                            remote_ip, ip_region)

        except Exception as e:  # noqa: BLE001
            logger.info("ws", f"WebSocket {direction} 转发异常", str(e))
        finally:
            stop_flag.set()
            try:
                dst.shutdown(socket.SHUT_WR)
            except OSError:
                pass

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
    """记录一条 WebSocket message 到 flows 表。

    - direction='c2s': 客户端→服务端（请求），method='WS-SEND'
    - direction='s2c': 服务端→客户端（响应），method='WS-RECV'
    payload 存到 request_body（c2s）或 response_body（s2c），raw_data 存原始字节
    """
    op_name = _OPCODE_NAMES.get(opcode, f"op{opcode}")
    is_outbound = direction == "c2s"

    # payload 编码：text 直接 utf-8，binary 用 base64
    try:
        payload_text = payload.decode("utf-8")
        body_field = payload_text
    except UnicodeDecodeError:
        body_field = "base64:" + base64.b64encode(payload).decode("ascii")

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
        "raw_data": "base64:" + base64.b64encode(payload).decode("ascii") if payload else None,
        "remote_ip": remote_ip or None,
        "ip_region": ip_region or None,
    }
    try:
        # 性能优化：异步入队，不阻塞 WS 转发线程
        db.insert_flow_async(flow)
    except Exception as e:  # noqa: BLE001
        logger.warning("ws", "WebSocket message 记录失败", str(e))


def is_websocket_upgrade(headers) -> bool:
    """判断 HTTP 请求是否为 WebSocket Upgrade 请求。

    检查 Upgrade: websocket 和 Connection: Upgrade 头。
    """
    upgrade = (headers.get("Upgrade") or "").lower()
    connection = (headers.get("Connection") or "").lower()
    return "websocket" in upgrade and "upgrade" in connection


def compute_accept_key(sec_websocket_key: str) -> str:
    """计算 Sec-WebSocket-Accept 值（RFC 6455 §4.2.2）。

    SHA1(key + magic GUID) 再 base64 编码。
    """
    import hashlib
    magic = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
    digest = hashlib.sha1((sec_websocket_key + magic).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")
