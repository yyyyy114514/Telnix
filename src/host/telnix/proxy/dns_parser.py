"""DNS protocol parsing (no external dependencies, parses RFC 1035 itself).

Supports parsing DNS query/response packets, extracting:
- Domain name, query type (A/AAAA/CNAME/MX/TXT/NS etc.)
- Response code (NOERROR/NXDOMAIN etc.)
- Answer records (A=IP, CNAME=domain, AAAA=IPv6 etc.)

Parse only, no sending. Called by raw_capture: when capturing UDP port 53 packets, call parse_dns() to parse.
"""
import struct
from typing import Tuple

# DNS 记录类型常量
TYPE_A = 1
TYPE_NS = 2
TYPE_CNAME = 5
TYPE_SOA = 6
TYPE_PTR = 12
TYPE_MX = 15
TYPE_TXT = 16
TYPE_AAAA = 28
TYPE_SRV = 33

TYPE_NAMES = {
    1: "A", 2: "NS", 5: "CNAME", 6: "SOA", 12: "PTR",
    15: "MX", 16: "TXT", 28: "AAAA", 33: "SRV",
    255: "ANY", 43: "DS", 46: "RRSIG", 47: "NSEC", 48: "DNSKEY",
    50: "NSEC3", 51: "NSEC3PARAM",
}

RCODE_NAMES = {
    0: "NOERROR", 1: "FORMERR", 2: "SERVFAIL", 3: "NXDOMAIN",
    4: "NOTIMP", 5: "REFUSED",
}


def parse_dns(data: bytes) -> dict | None:
    """Parse DNS packet.

    Returns dict:
    - is_response: bool (True=response, False=query)
    - id: int (transaction ID)
    - rcode: int (response code, only meaningful for responses)
    - rcode_name: str
    - questions: list[dict] (question section, each contains qname, qtype, qtype_name)
    - answers: list[dict] (answer section, each contains name, type, type_name, ttl, parsed rdata value)
    Returns None if not a valid DNS packet (insufficient length/format error).
    """
    if not data or len(data) < 12:
        return None
    try:
        # Header（12 字节）
        tx_id, flags, qdcount, ancount, nscount, arcount = struct.unpack(
            ">HHHHHH", data[:12])
        # QR 位（最高位）：0=查询，1=响应
        is_response = bool(flags & 0x8000)
        opcode = (flags >> 11) & 0xF
        rcode = flags & 0xF
        offset = 12
        # 解析 Question 段
        questions = []
        for _ in range(qdcount):
            qname, offset = _read_name(data, offset)
            if offset + 4 > len(data):
                return None
            qtype, qclass = struct.unpack(">HH", data[offset:offset + 4])
            offset += 4
            questions.append({
                "qname": qname,
                "qtype": qtype,
                "qtype_name": TYPE_NAMES.get(qtype, str(qtype)),
                "qclass": qclass,
            })
        # 解析 Answer/Authority/Additional 段
        answers = []
        for _ in range(ancount + nscount + arcount):
            rr_name, offset = _read_name(data, offset)
            if offset + 10 > len(data):
                break
            rr_type, rr_class, rr_ttl, rdlength = struct.unpack(
                ">HHIH", data[offset:offset + 10])
            offset += 10
            if offset + rdlength > len(data):
                break
            rdata = data[offset:offset + rdlength]
            offset += rdlength
            # 解析 rdata（A/AAAA/CNAME/PTR/MX/TXT 等常见类型）
            rdata_value = _parse_rdata(rr_type, rdata, data, offset - rdlength)
            answers.append({
                "name": rr_name,
                "type": rr_type,
                "type_name": TYPE_NAMES.get(rr_type, str(rr_type)),
                "ttl": rr_ttl,
                "rdata": rdata_value,
            })
        return {
            "is_response": is_response,
            "id": tx_id,
            "opcode": opcode,
            "rcode": rcode,
            "rcode_name": RCODE_NAMES.get(rcode, str(rcode)),
            "questions": questions,
            "answers": answers,
        }
    except (struct.error, IndexError):
        return None


def _read_name(data: bytes, offset: int) -> Tuple[str, int]:
    """Read DNS name (supports compression pointers).

    Returns (name, next offset). Name format example.com (dot-separated).

    Security: limit total iteration count (including compression pointer jumps), to prevent malicious compression pointer loops causing infinite loops.
    RFC 1035 §4.1.4 specifies compression pointers can only point backward, but malicious packets may not comply.
    """
    labels = []
    jumped = False
    original_offset = offset
    # 总迭代上限（含指针跳转）：正常名字最多 128 个 label + 若干次指针跳转，
    # 用 256 足够覆盖合法包，同时防止恶意指针循环死循环。
    # 之前 safety 只在读取 label 时递增，指针跳转不计数，
    # 恶意包构造指针环（A→B→A）可导致无限循环，hang 住 enrich worker。
    iterations = 0
    _MAX_ITERATIONS = 256
    while iterations < _MAX_ITERATIONS:
        iterations += 1
        if offset >= len(data):
            break
        length = data[offset]
        # 压缩指针（高 2 位 = 11）
        if (length & 0xC0) == 0xC0:
            if offset + 1 >= len(data):
                break
            pointer = ((length & 0x3F) << 8) | data[offset + 1]
            if not jumped:
                original_offset = offset + 2  # 指针占 2 字节
                jumped = True
            offset = pointer
            continue
        offset += 1
        if length == 0:
            break
        if offset + length > len(data):
            break
        labels.append(data[offset:offset + length].decode("ascii", errors="replace"))
        offset += length
    name = ".".join(labels)
    # 返回的 offset 是名字结束后的位置（首次没跳的情况）
    return name, original_offset if jumped else offset


def _parse_rdata(rr_type: int, rdata: bytes, full_data: bytes,
                 rdata_offset: int) -> str:
    """Parse rdata into human-readable string."""
    try:
        if rr_type == TYPE_A and len(rdata) == 4:
            # A 记录：4 字节 IPv4
            return ".".join(str(b) for b in rdata)
        if rr_type == TYPE_AAAA and len(rdata) == 16:
            # AAAA 记录：16 字节 IPv6
            parts = []
            for i in range(0, 16, 2):
                parts.append(f"{rdata[i]:02x}{rdata[i+1]:02x}")
            return ":".join(parts)
        if rr_type in (TYPE_CNAME, TYPE_PTR, TYPE_NS):
            # CNAME/PTR/NS：域名（可能用压缩指针）
            name, _ = _read_name(full_data, rdata_offset)
            return name
        if rr_type == TYPE_MX and len(rdata) >= 3:
            # MX：2 字节 preference + 域名
            preference = struct.unpack(">H", rdata[:2])[0]
            name, _ = _read_name(full_data, rdata_offset + 2)
            return f"{preference} {name}"
        if rr_type == TYPE_TXT:
            # TXT：1 字节长度 + 字符串（可能多条）
            txts = []
            i = 0
            while i < len(rdata):
                tlen = rdata[i]
                i += 1
                if i + tlen > len(rdata):
                    break
                txts.append(rdata[i:i + tlen].decode("utf-8", errors="replace"))
                i += tlen
            return " | ".join(txts)
        # 其他类型：返回 hex
        return rdata.hex()
    except Exception:  # noqa: BLE001
        return rdata.hex()
