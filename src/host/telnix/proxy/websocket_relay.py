"""WebSocket 帧解析与双向转发。

在 HTTP/1.1 Upgrade: websocket 握手成功（101 响应）后，连接转为 WebSocket 帧协议。
本模块负责：
- 解析 WebSocket 帧（RFC 6455）
- 双向转发 client<->server 的帧（分片透传，不等 FIN 才转发）
- 按 message（同 opcode 的连续帧聚合）记录到 flows 表（后台线程异步记录）

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

# 单帧大小限制（1MB，与浏览器默认一致，防止恶意大帧 OOM）
_MAX_FRAME_SIZE = 1 * 1024 * 1024
# 单 message 聚合大小上限（防止分片放大攻击）
_MAX_MESSAGE_SIZE = 16 * 1024 * 1024

# 后台记录线程池（避免阻塞 WS 转发主路径）
_WS_RECORD_EXECUTOR = None
_WS_RECORD_LOCK = threading.Lock()


def _get_ws_record_executor():
    """懒启动后台线程池用于 WS message 记录。

    性能优化：workers 从 2 增到 4，避免高并发 WS 场景下 recorder 队列堆积
    导致 message 记录延迟（透传不受影响，仅影响 DB 记录时机）。
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
    """WebSocket 协议错误。"""


def read_frame(sock: socket.socket) -> Optional[tuple[int, bytes, bool, bool]]:
    """从 socket 读取一个 WebSocket 帧。

    返回 (opcode, payload, fin, rsv1) 或 None（连接关闭）。
    客户端→服务端的帧带 mask，本函数自动解 mask。
    rsv1=True 表示 permessage-deflate 压缩（本实现不支持，调用方应处理）。
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
        raise WSError(f"非法 RSV 位: rsv2={rsv2} rsv3={rsv3}")

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
        raise WSError(f"帧过大: {payload_len} bytes (max {_MAX_FRAME_SIZE})")

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
    """发送一个 WebSocket 帧。

    服务端→客户端的帧不 mask，客户端→服务端的帧必须 mask。
    rsv1 用于透传 permessage-deflate 标记。
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

    sock.sendall(bytes(out))


def _recv_exact(sock: socket.socket, n: int) -> Optional[bytes]:
    """精确读取 n 字节，连接关闭返回 None。"""
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
    """XOR 解 mask（向量化：int.from_bytes 批量 XOR，替代逐字节循环）。

    性能：64KB 消息从 ~65536 次 Python 循环降为 1 次大整数 XOR，提升 ~50x。
    优化：mask_key 固定 4 字节，特化为 4 字节步长循环，避免 mask_repeated 临时分配。
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
    """双向转发 WebSocket，按 message 聚合记录到 flows 表。

    - client_sock: 客户端侧 socket（已完成 TLS/HTTP 握手）
    - server_sock: 目标服务器侧 socket（已收到 101 响应后的连接）
    - max_messages: 单连接最多记录多少条 message（防止恶意长连接刷屏）
    - idle_timeout: 空闲超时秒数（无任何帧则断开）

    转发策略：分片透传（每个帧原样转发，保留 FIN/opcode/RSV1），
    不再等 FIN 才转发，避免分片 message 的首字节延迟累积。
    仅在 FIN=1 时聚合完整 message 记录到 DB（后台线程异步）。
    """
    msg_count = 0
    stop_flag = threading.Event()

    def _direction(src: socket.socket, dst: socket.socket, direction: str):
        """转发一个方向的帧，分片透传，FIN 时聚合记录。"""
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
                    logger.warning("ws", f"WS message 过大 ({len(agg_payload)} bytes), 丢弃聚合")
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
                            logger.warning("ws", "WS record submit 失败", str(e))

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
    """记录一条 WebSocket message 到 flows 表（在后台线程执行）。

    - direction='c2s': 客户端→服务端（请求），method='WS-SEND'
    - direction='s2c': 服务端→客户端（响应），method='WS-RECV'
    payload 存到 request_body（c2s）或 response_body（s2c），raw_data 存原始字节
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
