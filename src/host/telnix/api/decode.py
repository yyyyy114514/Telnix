"""协议深度解析 API：从 TCP/UDP 二进制数据解析出协议字段。

支持协议（按端口和首字节自动探测）：
- DNS（53/5353）
- TLS（443/8443 等，识别 ClientHello/ServerHello）
- HTTP（80/8080 等，请求/响应）
- NTP（123）
- SMTP/POP3/IMAP/FTP 命令-响应（25/110/143/21 等）
- SSH 版本字符串（22）

不引入新依赖，纯标准库 struct/bytes 操作。
"""
import base64
import struct
import socket
import time

from fastapi import APIRouter

from .. import db
from . import err, ok

router = APIRouter()


# ---------- 通用解析字段结构 ----------
# fields: [{label, value, color?}]，color 可选：'red'|'amber'|'green'|'blue'|'dim'
# color 用于前端高亮关键字段

# ---------- DNS 解析（RFC 1035） ----------
def _decode_dns(data: bytes) -> dict:
    """DNS 协议解析。"""
    if len(data) < 12:
        return {"protocol": "DNS", "error": "数据过短"}
    tx_id, flags, qdcount, ancount, nscount, arcount = struct.unpack(">HHHHHH", data[:12])
    is_response = bool(flags & 0x8000)
    opcode = (flags >> 11) & 0x0F
    rcode = flags & 0x000F
    qr_label = "响应" if is_response else "查询"
    opcode_label = {
        0: "标准查询", 1: "反向查询", 2: "服务器状态",
        4: "通知", 5: "更新",
    }.get(opcode, f"未知({opcode})")
    rcode_label = {
        0: "无错误", 1: "格式错误", 2: "服务器失败",
        3: "域名不存在", 4: "未实现", 5: "拒绝查询",
    }.get(rcode, f"未知({rcode})")
    fields = [
        {"label": "方向", "value": qr_label, "color": "blue"},
        {"label": "Transaction ID", "value": f"0x{tx_id:04x}"},
        {"label": "Opcode", "value": opcode_label},
        {"label": "QR", "value": "Response" if is_response else "Query"},
        {"label": "RCODE", "value": rcode_label, "color": "amber" if rcode else None},
        {"label": "问题数", "value": qdcount},
        {"label": "回答数", "value": ancount, "color": "green" if ancount else None},
        {"label": "授权数", "value": nscount},
        {"label": "附加数", "value": arcount},
    ]
    offset = 12
    # 解析 Question 段
    questions = []
    for _ in range(qdcount):
        if offset >= len(data):
            break
        name, offset = _parse_dns_name(data, offset)
        if offset + 4 > len(data):
            break
        qtype, qclass = struct.unpack(">HH", data[offset:offset + 4])
        offset += 4
        type_label = _dns_type_label(qtype)
        class_label = _dns_class_label(qclass)
        questions.append(f"{name} ({type_label}/{class_label})")
    if questions:
        fields.append({"label": "查询问题", "value": "\n".join(questions), "color": "blue"})
    # 解析 Answer 段（简略）
    answers = []
    for _ in range(ancount):
        if offset >= len(data):
            break
        name, offset = _parse_dns_name(data, offset)
        if offset + 10 > len(data):
            break
        rtype, rclass, ttl, rdlen = struct.unpack(">HHIH", data[offset:offset + 10])
        offset += 10
        if offset + rdlen > len(data):
            break
        rdata = data[offset:offset + rdlen]
        offset += rdlen
        rdata_label = _format_dns_rdata(rtype, rdata)
        answers.append(f"{name} → {rdata_label} (TTL={ttl}s)")
    if answers:
        fields.append({"label": "回答", "value": "\n".join(answers), "color": "green"})
    return {"protocol": "DNS", "summary": f"{qr_label} {qdcount}问/{ancount}答", "fields": fields}


def _parse_dns_name(data: bytes, offset: int) -> tuple[str, int]:
    """解析 DNS 名称（支持压缩指针）。"""
    labels = []
    jumped = False
    original_offset = offset
    jumps = 0
    while offset < len(data) and jumps < 10:
        length = data[offset]
        if length == 0:
            offset += 1
            break
        if (length & 0xC0) == 0xC0:
            # 压缩指针
            if offset + 1 >= len(data):
                break
            pointer = ((length & 0x3F) << 8) | data[offset + 1]
            if not jumped:
                original_offset = offset + 2
                jumped = True
            offset = pointer
            jumps += 1
            continue
        offset += 1
        if offset + length > len(data):
            break
        labels.append(data[offset:offset + length].decode("ascii", errors="replace"))
        offset += length
    name = ".".join(labels) if labels else "(root)"
    final_offset = original_offset if jumped else offset
    return name, final_offset


def _dns_type_label(t: int) -> str:
    return {
        1: "A", 2: "NS", 5: "CNAME", 6: "SOA", 12: "PTR",
        15: "MX", 16: "TXT", 28: "AAAA", 33: "SRV", 35: "NAPTR",
        43: "DS", 46: "RRSIG", 47: "NSEC", 48: "DNSKEY", 50: "NSEC3",
        99: "SPF", 255: "ANY", 257: "CAA",
    }.get(t, f"TYPE{t}")


def _dns_class_label(c: int) -> str:
    return {1: "IN", 3: "CH", 4: "HS", 255: "ANY"}.get(c, f"CLASS{c}")


def _format_dns_rdata(rtype: int, rdata: bytes) -> str:
    if rtype == 1 and len(rdata) == 4:  # A
        return socket.inet_ntoa(rdata)
    if rtype == 28 and len(rdata) == 16:  # AAAA
        return socket.inet_ntop(socket.AF_INET6, rdata)
    if rtype in (2, 5, 12):  # NS/CNAME/PTR
        # 用偏移 0 解析
        name, _ = _parse_dns_name(rdata, 0)
        return name
    if rtype == 15 and len(rdata) >= 3:  # MX
        pref = struct.unpack(">H", rdata[:2])[0]
        name, _ = _parse_dns_name(rdata, 2)
        return f"pref={pref} {name}"
    if rtype == 16:  # TXT
        if rdata:
            txt_len = rdata[0]
            txt = rdata[1:1 + txt_len].decode("utf-8", errors="replace")
            return f'"{txt}"'
    return f"<{len(rdata)}字节>"


# ---------- TLS 解析（RFC 5246 / RFC 8446） ----------
def _decode_tls(data: bytes) -> dict:
    """TLS 协议解析（识别 ClientHello/ServerHello 等）。"""
    if len(data) < 5:
        return {"protocol": "TLS", "error": "数据过短"}
    content_type, version, length = struct.unpack(">BHH", data[:5])
    version_major = (version >> 8) & 0xFF
    version_minor = version & 0xFF
    content_label = {
        20: "ChangeCipherSpec", 21: "Alert", 22: "Handshake", 23: "ApplicationData",
    }.get(content_type, f"Unknown({content_type})")
    version_label = {0x0301: "TLS 1.0", 0x0302: "TLS 1.1", 0x0303: "TLS 1.2", 0x0304: "TLS 1.3"}.get(
        version, f"0x{version_major:02x}{version_minor:02x}")
    fields = [
        {"label": "ContentType", "value": content_label, "color": "blue"},
        {"label": "Version", "value": version_label, "color": "green"},
        {"label": "Length", "value": length},
    ]
    if content_type == 22 and len(data) >= 9:
        # Handshake
        hs_type = data[5]
        hs_len = (data[6] << 16) | (data[7] << 8) | data[8]
        hs_label = {
            1: "ClientHello", 2: "ServerHello", 4: "NewSessionTicket",
            11: "Certificate", 12: "ServerKeyExchange", 13: "CertificateRequest",
            14: "ServerHelloDone", 15: "CertificateVerify", 16: "ClientKeyExchange",
            20: "Finished",
        }.get(hs_type, f"Unknown({hs_type})")
        fields.append({"label": "HandshakeType", "value": hs_label, "color": "amber"})
        fields.append({"label": "HandshakeLength", "value": hs_len})
        # 解析 ClientHello
        if hs_type == 1 and len(data) >= 43:
            try:
                # legacy_version + random + session_id_len
                legacy_v = struct.unpack(">H", data[9:11])[0]
                v_label = {0x0303: "TLS 1.2"}.get(legacy_v, f"0x{legacy_v:04x}")
                fields.append({"label": "ClientVersion", "value": v_label})
                fields.append({"label": "Random", "value": data[11:43].hex()})
                off = 43
                if off < len(data):
                    sid_len = data[off]
                    off += 1 + sid_len
                    if off + 2 <= len(data):
                        cipher_len = struct.unpack(">H", data[off:off + 2])[0]
                        off += 2
                        ciphers = []
                        for i in range(0, cipher_len, 2):
                            if off + 2 > len(data):
                                break
                            cipher = struct.unpack(">H", data[off:off + 2])[0]
                            off += 2
                            ciphers.append(f"0x{cipher:04x}")
                        if ciphers:
                            fields.append({"label": "CipherSuites", "value": ", ".join(ciphers[:10]) + ("..." if len(ciphers) > 10 else "")})
            except Exception:
                pass
    return {"protocol": "TLS", "summary": f"{content_label} ({version_label})", "fields": fields}


# ---------- HTTP 解析 ----------
def _decode_http(data: bytes) -> dict:
    """HTTP 请求/响应解析。"""
    try:
        text = data.decode("utf-8", errors="replace")
        # 找到 headers 结束（CRLF CRLF 或 LF LF）
        end_idx = text.find("\r\n\r\n")
        if end_idx == -1:
            end_idx = text.find("\n\n")
            sep_len = 2
        else:
            sep_len = 4
        head = text[:end_idx] if end_idx != -1 else text
        body = text[end_idx + sep_len:] if end_idx != -1 else ""
        lines = head.split("\r\n") if "\r\n" in head else head.split("\n")
        if not lines:
            return {"protocol": "HTTP", "error": "空请求"}
        first = lines[0]
        # 请求行
        if any(first.startswith(m + " ") for m in ("GET", "POST", "PUT", "DELETE", "HEAD", "OPTIONS", "PATCH", "CONNECT")):
            parts = first.split(" ", 2)
            method = parts[0] if parts else ""
            path = parts[1] if len(parts) > 1 else ""
            version = parts[2] if len(parts) > 2 else ""
            headers = {}
            for line in lines[1:]:
                if ":" in line:
                    k, _, v = line.partition(":")
                    headers[k.strip()] = v.strip()
            return {
                "protocol": "HTTP",
                "summary": f"{method} {path}",
                "fields": [
                    {"label": "方向", "value": "请求", "color": "blue"},
                    {"label": "方法", "value": method, "color": "green"},
                    {"label": "路径", "value": path},
                    {"label": "版本", "value": version, "color": "dim"},
                    {"label": "Headers", "value": "\n".join(f"{k}: {v}" for k, v in headers.items())},
                    {"label": "Body", "value": body[:2000], "color": "dim"} if body else None,
                ],
            }
        # 响应状态行
        elif first.startswith("HTTP/"):
            parts = first.split(" ", 2)
            version = parts[0] if parts else ""
            code = parts[1] if len(parts) > 1 else ""
            reason = parts[2] if len(parts) > 2 else ""
            headers = {}
            for line in lines[1:]:
                if ":" in line:
                    k, _, v = line.partition(":")
                    headers[k.strip()] = v.strip()
            try:
                code_int = int(code)
                code_color = "green" if code_int < 300 else "amber" if code_int < 500 else "red"
            except Exception:
                code_color = "dim"
            return {
                "protocol": "HTTP",
                "summary": f"{code} {reason}",
                "fields": [
                    {"label": "方向", "value": "响应", "color": "blue"},
                    {"label": "版本", "value": version, "color": "dim"},
                    {"label": "状态码", "value": code, "color": code_color},
                    {"label": "原因", "value": reason},
                    {"label": "Headers", "value": "\n".join(f"{k}: {v}" for k, v in headers.items())},
                    {"label": "Body", "value": body[:2000], "color": "dim"} if body else None,
                ],
            }
        return {"protocol": "HTTP", "error": "无法识别请求行/状态行", "fields": []}
    except Exception as e:
        return {"protocol": "HTTP", "error": str(e)}


# ---------- NTP 解析 ----------
def _decode_ntp(data: bytes) -> dict:
    """NTP v3/v4 协议解析。"""
    if len(data) < 48:
        return {"protocol": "NTP", "error": "数据过短"}
    leap = (data[0] >> 6) & 0x03
    ver = (data[0] >> 3) & 0x07
    mode = data[0] & 0x07
    stratum = data[1]
    poll = data[2]
    precision = data[3]
    leap_label = {0: "无警告", 1: "+1s", 2: "-1s", 3: "告警"}.get(leap)
    mode_label = {
        1: "对称主动", 2: "对称被动", 3: "客户端", 4: "服务器",
        5: "广播", 6: "组播控制", 7: "私有",
    }.get(mode, f"未知({mode})")
    stratum_label = {
        0: "无效/ Kiss-of-Death", 1: "主参考", 16: "不可达",
    }.get(stratum, f"层级 {stratum}")
    fields = [
        {"label": "Leap", "value": leap_label, "color": "amber" if leap else None},
        {"label": "Version", "value": f"v{ver}", "color": "dim"},
        {"label": "Mode", "value": mode_label, "color": "blue"},
        {"label": "Stratum", "value": stratum_label, "color": "red" if stratum == 16 else None},
        {"label": "Poll", "value": f"2^{poll} s = {2 ** poll}s" if poll < 32 else "N/A"},
        {"label": "Precision", "value": f"2^{precision - 256 if precision > 128 else precision} s"},
    ]
    # Root Delay / Root Dispersion
    if len(data) >= 16:
        root_delay = struct.unpack(">I", data[4:8])[0] / 65536.0
        root_disp = struct.unpack(">I", data[8:12])[0] / 65536.0
        fields.append({"label": "Root Delay", "value": f"{root_delay:.3f}s"})
        fields.append({"label": "Root Dispersion", "value": f"{root_disp:.3f}s"})
    # 时间戳
    if len(data) >= 48:
        ts = struct.unpack(">II", data[40:48])
        # NTP epoch: 1900-01-01, 转 unix epoch (2208988800 秒差)
        ntp_epoch = 2208988800
        unix_ts = ts[0] - ntp_epoch + ts[1] / 2 ** 32
        try:
            dt = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(unix_ts))
            fields.append({"label": "Transmit Timestamp", "value": f"{dt} UTC ({ts[1]})", "color": "green"})
        except Exception:
            fields.append({"label": "Transmit Timestamp", "value": f"{ts[0]}.{ts[1]}"})
    return {"protocol": "NTP", "summary": f"{mode_label} v{ver}", "fields": fields}


# ---------- 文本协议（SMTP/FTP/POP3/IMAP/SSH 等） ----------
def _decode_text(data: bytes, proto: str) -> dict:
    """通用文本协议解析：显示前几行命令/响应。"""
    try:
        text = data.decode("utf-8", errors="replace")
        lines = text.split("\r\n") if "\r\n" in text else text.split("\n")
        preview = "\n".join(lines[:20])
        return {
            "protocol": proto,
            "summary": lines[0][:80] if lines else "",
            "fields": [
                {"label": "首行", "value": lines[0] if lines else "", "color": "blue"},
                {"label": "完整内容", "value": preview + ("..." if len(lines) > 20 else ""), "color": "dim"},
            ],
        }
    except Exception as e:
        return {"protocol": proto, "error": str(e)}


def _decode_unknown(data: bytes) -> dict:
    """未知协议：显示前 N 字节预览。"""
    preview = data[:64].hex(" ")
    printable = "".join(chr(b) if 32 <= b < 127 else "·" for b in data[:64])
    return {
        "protocol": "Unknown",
        "summary": f"{len(data)} 字节",
        "fields": [
            {"label": "长度", "value": f"{len(data)} 字节", "color": "dim"},
            {"label": "前 64 字节 Hex", "value": preview, "color": "dim"},
            {"label": "前 64 字节 ASCII", "value": printable, "color": "dim"},
        ],
    }


# ---------- 端口/内容自动探测 ----------
def _pick_decoder(port: int, raw: bytes) -> callable:
    """按端口和首字节自动选择解析器。"""
    # 优先按端口
    if port in (53, 5353):
        return _decode_dns
    if port == 123:
        return _decode_ntp
    if port in (443, 8443, 4433, 5443, 2096):
        # TLS 还是 HTTP？
        if raw and raw[0] == 0x16:
            return _decode_tls
        return _decode_http
    if port in (80, 8080, 8000, 8008, 8081, 8888, 3000, 5000, 9000):
        return _decode_http
    if port in (25, 465, 587):
        return lambda d: _decode_text(d, "SMTP")
    if port in (110, 995):
        return lambda d: _decode_text(d, "POP3")
    if port in (143, 993):
        return lambda d: _decode_text(d, "IMAP")
    if port == 21:
        return lambda d: _decode_text(d, "FTP")
    if port == 22:
        return lambda d: _decode_text(d, "SSH")
    if port == 23:
        return lambda d: _decode_text(d, "Telnet")
    if port == 3306:
        return lambda d: _decode_text(d, "MySQL")
    if port == 5432:
        return lambda d: _decode_text(d, "PostgreSQL")
    if port == 6379:
        return lambda d: _decode_text(d, "Redis")
    if port == 27017:
        return lambda d: _decode_text(d, "MongoDB")
    # 内容探测
    if raw:
        if raw[0] == 0x16 and len(raw) >= 5:
            return _decode_tls
        if raw.startswith((b"GET ", b"POST ", b"PUT ", b"DELETE ", b"HEAD ", b"OPTIONS ", b"PATCH ", b"HTTP/")):
            return _decode_http
        # DNS 响应至少 12 字节且 flags 合理
        if len(raw) >= 12:
            flags = (raw[2] << 8) | raw[3]
            # QR 位为 0 或 1，opcode 0-2，rcode 0-6 才算合法
            if (flags & 0xC000) in (0x0000, 0x8000) and ((flags >> 11) & 0x0F) <= 2 and (flags & 0x000F) <= 6:
                return _decode_dns
        # 文本内容探测（HTML/XML/JSON/纯文本）：高比例可打印 ASCII
        printable = sum(1 for b in raw[:256] if 9 <= b < 127 or b in (0x0A, 0x0D))
        if printable >= min(len(raw), 256) * 0.7:
            # 区分 HTML / XML / JSON / 纯文本
            head = raw[:128].lstrip()
            if head.startswith(b"<"):
                # 判定 HTML / XML
                if head.startswith(b"<?xml"):
                    return lambda d: _decode_text(d, "XML")
                return lambda d: _decode_text(d, "HTML")
            if head.startswith(b"{") or head.startswith(b"["):
                return lambda d: _decode_text(d, "JSON")
            return lambda d: _decode_text(d, "Text")
    return _decode_unknown


@router.get("/flows/{flow_id}/decode")
async def decode_flow(flow_id: int, field: str = "raw_data"):
    """解析 TCP/UDP 流量的协议字段。

    field: raw_data | request_body | response_body
    按端口和首字节自动探测协议，返回结构化字段。
    """
    flow = db.get_flow(flow_id)
    if not flow:
        return err("流量不存在")
    val = flow.get(field) or ""
    if val.startswith("base64:"):
        try:
            raw = base64.b64decode(val[7:])
        except Exception:
            return err("base64 解码失败")
    else:
        raw = val.encode("utf-8", errors="replace")
    if not raw:
        return ok({"protocol": "empty", "summary": "无数据", "fields": []})
    port = flow.get("dst_port") or flow.get("src_port") or 0
    decoder = _pick_decoder(port, raw)
    result = decoder(raw)
    # 补充端口信息
    if "fields" in result and result["fields"] is None:
        result["fields"] = []
    if isinstance(result.get("fields"), list):
        result["fields"] = [
            {"label": "端口", "value": str(port), "color": "dim"},
            {"label": "数据长度", "value": f"{len(raw)} 字节", "color": "dim"},
        ] + result["fields"]
    return ok(result)
