"""DNS 劫持后端（WinDivert）。

通过 WinDivert 在网络层截获 UDP 53 端口的 DNS 响应包，
按规则修改 Answer 段中 A 记录的 IP 地址，实现本地 DNS 劫持。

工作模式：
- filter: udp and (udp.SrcPort == 53 or udp.DstPort == 53)
- 默认 flags=0（拦截模式）：recv 取出包后必须 send 才会继续流转
- 只修改 src_port=53 的入站 DNS 响应包
- 修改 A 记录 IP 后重算 IP/UDP 校验和
- 不需要劫持的包原样 send

跨平台说明：WinDivert 仅 Windows 可用，非 Windows 平台调用 start 返回失败。
"""
from __future__ import annotations

import ipaddress
import os
import socket
import struct
import sys
import threading
import time
from collections import deque
from datetime import datetime

from .. import logger

IS_WINDOWS = sys.platform == "win32"


# 命中日志环形缓冲（前端可拉取最近 N 条）
_HIJACK_LOG: deque = deque(maxlen=200)
_HIJACK_LOG_LOCK = threading.Lock()

# 统计计数器
_STATS = {
    "total_packets": 0,        # 总共截获的 DNS 响应包
    "hijacked_packets": 0,     # 实际修改的包
    "skipped_no_match": 0,     # 未匹配规则的包
    "errors": 0,               # 解析/修改失败次数
}
_STATS_LOCK = threading.Lock()


def _log_hijack(domain: str, original_ips: list, new_ip: str, src: str):
    """记录一条劫持日志。"""
    entry = {
        "ts": datetime.now().isoformat(),
        "domain": domain,
        "original_ips": original_ips,
        "new_ip": new_ip,
        "dns_server": src,
    }
    with _HIJACK_LOG_LOCK:
        _HIJACK_LOG.append(entry)


def get_hijack_log() -> list:
    with _HIJACK_LOG_LOCK:
        return list(_HIJACK_LOG)


def get_stats() -> dict:
    with _STATS_LOCK:
        return dict(_STATS)


def _reset_stats():
    with _STATS_LOCK:
        for k in _STATS:
            _STATS[k] = 0


class DnsHijacker:
    """DNS 劫持后端。"""

    def __init__(self):
        self._running = False
        self._thread: threading.Thread | None = None
        self._divert = None
        self._last_error: str = ""
        # 规则：domain → fake_ip（精确匹配或 *.suffix 通配）
        self._rules: dict[str, str] = {}
        # 默认劫持 IP（所有未在 _rules 中但匹配的域名都返回此 IP）
        # 空字符串表示只劫持 _rules 中明确列出的域名
        self._default_ip: str = ""
        self._enabled: bool = True  # 总开关（启动时为 True，stop 时为 False）
        self._lock = threading.RLock()
        # 本机 IP 集合缓存（避免每次包都查询）
        self._local_ips: set[str] = set()
        self._local_ips_ts: float = 0.0

    @property
    def running(self) -> bool:
        return self._running

    @property
    def last_error(self) -> str:
        return self._last_error

    def set_rules(self, rules: dict[str, str], default_ip: str = ""):
        """更新劫持规则。

        rules: {domain: fake_ip}，domain 支持通配符前缀 *.example.com
        default_ip: 默认劫持 IP（所有未匹配规则的 A 记录查询都返回此 IP）
        """
        with self._lock:
            # 规范化规则：domain 转小写，去前后空白
            self._rules = {
                k.strip().lower(): v.strip()
                for k, v in rules.items()
                if k.strip() and v.strip()
            }
            self._default_ip = default_ip.strip()

    def get_rules(self) -> tuple[dict[str, str], str]:
        with self._lock:
            return dict(self._rules), self._default_ip

    def _match_rule(self, domain: str) -> str | None:
        """匹配域名，返回劫持 IP 或 None。"""
        d = domain.strip().lower()
        if not d:
            return None
        with self._lock:
            # 1. 精确匹配
            if d in self._rules:
                return self._rules[d]
            # 2. 通配符匹配（*.example.com 匹配 foo.example.com）
            for pattern, ip in self._rules.items():
                if pattern.startswith("*.") and d.endswith(pattern[1:]):
                    return ip
            # 3. 默认 IP（如果设置了）
            if self._default_ip:
                return self._default_ip
        return None

    def _refresh_local_ips(self):
        """刷新本机 IP 集合（用于判断包方向）。"""
        now = time.time()
        if now - self._local_ips_ts < 30:
            return
        ips: set[str] = set()
        try:
            hostname = socket.gethostname()
            for info in socket.getaddrinfo(hostname, None):
                ip = info[4][0]
                if ":" not in ip:  # 仅 IPv4
                    ips.add(ip)
        except Exception:  # noqa: BLE001
            pass
        ips.add("127.0.0.1")
        self._local_ips = ips
        self._local_ips_ts = now

    def _is_local_ip(self, ip: str) -> bool:
        self._refresh_local_ips()
        return ip in self._local_ips

    def _is_admin(self) -> bool:
        if not IS_WINDOWS:
            try:
                return os.geteuid() == 0  # type: ignore[attr-defined]
            except AttributeError:
                return False
        try:
            import ctypes
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            return False

    def start(self) -> tuple[bool, str]:
        """启动 DNS 劫持。返回 (success, msg)。"""
        if not IS_WINDOWS:
            msg = "当前平台不支持 DNS 劫持（WinDivert 仅 Windows 可用）"
            logger.error("dns_hijack", "DNS 劫持不支持当前平台", msg)
            return False, msg
        try:
            import pydivert  # type: ignore  # noqa: F401
        except ImportError:
            msg = "pydivert 未安装，请运行: pip install pydivert"
            logger.error("dns_hijack", "pydivert 未安装", msg)
            return False, msg
        if self._running:
            return True, "已在运行"
        try:
            import pydivert  # type: ignore
            if not self._is_admin():
                msg = "DNS 劫持需要管理员权限"
                logger.error("dns_hijack", "权限不足", msg)
                return False, msg
            # WinDivert filter：只拦截 UDP 53 端口的包
            # flags=0：拦截模式，recv 取出的包不再流转，必须 send 才会继续传递
            self._divert = pydivert.WinDivert(
                "udp and (udp.SrcPort == 53 or udp.DstPort == 53)",
                flags=0,
            )
            self._divert.open()
            self._running = True
            self._enabled = True
            _reset_stats()
            self._thread = threading.Thread(target=self._capture_loop, daemon=True, name="dns-hijack")
            self._thread.start()
            rules, default_ip = self.get_rules()
            logger.info("dns_hijack", "DNS 劫持已启动",
                        f"rules={len(rules)} 条, default_ip={default_ip or '(无)'}")
            return True, "DNS 劫持已启动"
        except Exception as e:  # noqa: BLE001
            err_msg = str(e)
            self._last_error = err_msg
            logger.error("dns_hijack", "WinDivert 启动失败", err_msg)
            self._divert = None
            self._running = False
            low = err_msg.lower()
            if "找不到" in err_msg or "not found" in low:
                return False, "WinDivert 驱动文件缺失，请确保已安装 pydivert"
            if "access is denied" in low or "拒绝访问" in low:
                return False, "权限不足，请用管理员身份运行 Telnix"
            if "签名" in err_msg or "sign" in low:
                return False, "WinDivert 驱动加载被拦截，可能是杀软拦截，请加入白名单"
            return False, f"启动失败: {err_msg}"

    def stop(self) -> tuple[bool, str]:
        """停止 DNS 劫持。"""
        if not self._running:
            return True, "未在运行"
        self._running = False
        self._enabled = False
        if self._divert:
            try:
                self._divert.close()
            except Exception:  # noqa: BLE001
                pass
            self._divert = None
        if self._thread:
            self._thread.join(timeout=2)
            self._thread = None
        logger.info("dns_hijack", "DNS 劫持已停止",
                    f"stats={get_stats()}")
        return True, "DNS 劫持已停止"

    def _capture_loop(self):
        """抓包主循环：recv → 判断是否需要劫持 → 修改 → send。"""
        import pydivert  # type: ignore
        global _STATS
        while self._running:
            try:
                packet = self._divert.recv()
                if packet is None:
                    continue
                ip_hdr = packet.ipv4
                if ip_hdr is None:
                    # IPv6 暂不支持（DNS over IPv6 较少）
                    self._divert.send(packet)
                    continue
                udp_hdr = packet.udp
                if udp_hdr is None:
                    self._divert.send(packet)
                    continue
                src_port = udp_hdr.src_port
                dst_port = udp_hdr.dst_port
                # 只处理 DNS 响应包（src_port=53，从远端 DNS 服务器到本机）
                if src_port != 53:
                    # DNS 查询包（dst_port=53）原样放行
                    self._divert.send(packet)
                    continue
                # 判断方向：响应包的目标是本机
                dst_ip = ip_hdr.dst_addr
                if not self._is_local_ip(dst_ip):
                    # 不是发往本机的包，原样放行
                    self._divert.send(packet)
                    continue
                with _STATS_LOCK:
                    _STATS["total_packets"] += 1

                payload = packet.payload or b""
                if len(payload) < 12:
                    # 不完整的 DNS 包
                    self._divert.send(packet)
                    continue

                try:
                    new_payload, hijacked, domain, original_ips, new_ip = self._maybe_hijack(payload)
                    if hijacked and new_payload:
                        # 修改包 payload 并重算校验和
                        packet.payload = new_payload
                        # pydivert 2.x 提供 recalc_checksums
                        try:
                            packet.recalc_checksums()
                        except Exception:  # noqa: BLE001
                            # 旧版可能没有此方法，手动算
                            self._manual_recalc(packet)
                        with _STATS_LOCK:
                            _STATS["hijacked_packets"] += 1
                        _log_hijack(domain or "?", original_ips, new_ip, ip_hdr.src_addr)
                        logger.info("dns_hijack", "劫持 DNS 响应",
                                    f"domain={domain} {original_ips} -> {new_ip}")
                    else:
                        with _STATS_LOCK:
                            _STATS["skipped_no_match"] += 1
                except Exception as e:  # noqa: BLE001
                    with _STATS_LOCK:
                        _STATS["errors"] += 1
                    logger.error("dns_hijack", "劫持处理失败", str(e))
                # 不论是否修改，都要 send（否则会断网）
                self._divert.send(packet)
            except Exception as e:  # noqa: BLE001
                if self._running:
                    logger.error("dns_hijack", "劫持循环异常", str(e))
                    time.sleep(0.05)

    def _manual_recalc(self, packet):
        """旧版 pydivert 手动重算校验和的 fallback。"""
        try:
            import pydivert  # type: ignore
            pydivert.Helper.calc_checksums(packet)
        except Exception:  # noqa: BLE001
            pass

    def _maybe_hijack(self, data: bytes) -> tuple:
        """解析 DNS 响应，按规则修改 A 记录。

        返回 (new_payload, hijacked, domain, original_ips, new_ip)
        """
        if len(data) < 12:
            return data, False, "", [], ""
        # DNS header：12 字节
        # ID(2) FLAGS(2) QDCOUNT(2) ANCOUNT(2) NSCOUNT(2) ARCOUNT(2)
        flags = struct.unpack(">H", data[2:4])[0]
        qdcount = struct.unpack(">H", data[4:6])[0]
        ancount = struct.unpack(">H", data[6:8])[0]
        # QR=1 表示响应
        is_response = (flags & 0x8000) != 0
        if not is_response or qdcount == 0:
            return data, False, "", [], ""

        offset = 12
        # 解析 Question 段（取第一个查询的域名）
        domain, offset = self._parse_name(data, offset)
        if not domain:
            return data, False, "", [], ""
        # 读取 QTYPE(2) + QCLASS(2)
        if offset + 4 > len(data):
            return data, False, domain, [], ""
        qtype = struct.unpack(">H", data[offset:offset+2])[0]
        offset += 4
        # 跳过剩余的 Question（如果有）
        for _ in range(qdcount - 1):
            _, offset = self._parse_name(data, offset)
            offset += 4

        # 匹配规则
        new_ip = self._match_rule(domain)
        if not new_ip:
            return data, False, domain, [], ""

        # 校验 IP 合法性
        try:
            ip_bytes = socket.inet_aton(new_ip)
        except OSError:
            logger.warning("dns_hijack", "规则 IP 无效", f"domain={domain} ip={new_ip}")
            return data, False, domain, [], ""

        # AAAA 查询（IPv6）：清空 Answer 段，强制浏览器回退到 IPv4 A 记录查询
        # 浏览器优先用 IPv6，若不处理 AAAA 则劫持无效（会直接用真实 IPv6 地址访问）
        if qtype == 28:
            new_data = bytearray(data[:offset])
            struct.pack_into(">H", new_data, 6, 0)   # ancount = 0
            struct.pack_into(">H", new_data, 8, 0)   # nscount = 0
            struct.pack_into(">H", new_data, 10, 0) # arcount = 0
            return bytes(new_data), True, domain, ["AAAA"], new_ip

        # 遍历 Answer 段，找到所有 A 记录（TYPE=1）并修改 RDATA
        new_data = bytearray(data)
        original_ips: list[str] = []
        modified = False
        for i in range(ancount):
            if offset >= len(new_data):
                break
            # Answer 中的 NAME 可能是指针（压缩），先尝试解析
            name_start = offset
            ans_name, offset = self._parse_name(bytes(new_data), offset)
            if offset + 10 > len(new_data):
                break
            rtype = struct.unpack(">H", new_data[offset:offset+2])[0]
            rclass = struct.unpack(">H", new_data[offset+2:offset+4])[0]
            ttl = struct.unpack(">I", new_data[offset+4:offset+8])[0]
            rdlength = struct.unpack(">H", new_data[offset+8:offset+10])[0]
            rdata_offset = offset + 10
            if rdata_offset + rdlength > len(new_data):
                break
            # TYPE=1 是 A 记录，CLASS=1 是 IN
            if rtype == 1 and rclass == 1 and rdlength == 4:
                # 修改 A 记录的 IP
                old_ip_bytes = bytes(new_data[rdata_offset:rdata_offset+4])
                old_ip = socket.inet_ntoa(old_ip_bytes)
                original_ips.append(old_ip)
                # 覆盖 IP 地址
                new_data[rdata_offset:rdata_offset+4] = ip_bytes
                modified = True
            offset = rdata_offset + rdlength

        if not modified:
            # Answer 段中没有 A 记录（可能是 CNAME 等）
            # 此时构造一个新的 A 记录应答太复杂，简单跳过
            return data, False, domain, [], ""

        return bytes(new_data), True, domain, original_ips, new_ip

    def _parse_name(self, data: bytes, offset: int) -> tuple[str, int]:
        """解析 DNS 域名（支持压缩指针）。

        返回 (domain, new_offset)：new_offset 指向 NAME 后的位置
        """
        labels: list[str] = []
        pos = offset
        jumped = False
        jump_pos = 0
        iterations = 0  # 防止死循环
        while pos < len(data) and iterations < 100:
            iterations += 1
            length = data[pos]
            if length == 0:
                pos += 1
                if not jumped:
                    jump_pos = pos
                break
            if (length & 0xC0) == 0xC0:
                # 指针
                if pos + 1 >= len(data):
                    break
                if not jumped:
                    jump_pos = pos + 2
                ptr = ((length & 0x3F) << 8) | data[pos + 1]
                pos = ptr
                jumped = True
                continue
            else:
                # 标签
                pos += 1
                if pos + length > len(data):
                    break
                labels.append(data[pos:pos+length].decode("ascii", errors="replace"))
                pos += length
        domain = ".".join(labels)
        final_offset = jump_pos if jumped and jump_pos > 0 else pos
        return domain.lower(), final_offset


# 单例
_dns_hijacker: DnsHijacker | None = None
_singleton_lock = threading.Lock()


def get_hijacker() -> DnsHijacker:
    """获取 DnsHijacker 单例。"""
    global _dns_hijacker
    with _singleton_lock:
        if _dns_hijacker is None:
            _dns_hijacker = DnsHijacker()
        return _dns_hijacker


def start_hijack(rules: dict[str, str], default_ip: str = "") -> tuple[bool, str]:
    """启动 DNS 劫持。

    rules: {domain: fake_ip} 字典
    default_ip: 默认劫持 IP（所有未匹配规则的 A 记录查询都返回此 IP）
    """
    h = get_hijacker()
    h.set_rules(rules, default_ip)
    return h.start()


def stop_hijack() -> tuple[bool, str]:
    """停止 DNS 劫持。"""
    h = get_hijacker()
    return h.stop()


def update_rules(rules: dict[str, str], default_ip: str = "") -> tuple[bool, str]:
    """更新规则（运行时热更新）。"""
    h = get_hijacker()
    h.set_rules(rules, default_ip)
    return True, f"已更新 {len(rules)} 条规则"


def hijack_status() -> dict:
    """获取当前状态。"""
    h = get_hijacker()
    rules, default_ip = h.get_rules()
    return {
        "running": h.running,
        "last_error": h.last_error,
        "rules": rules,
        "default_ip": default_ip,
        "stats": get_stats(),
        "log": get_hijack_log(),
        "is_admin": h._is_admin() if IS_WINDOWS else False,
        "is_windows": IS_WINDOWS,
    }


def clear_log():
    """清空劫持日志。"""
    with _HIJACK_LOG_LOCK:
        _HIJACK_LOG.clear()
    with _STATS_LOCK:
        for k in _STATS:
            _STATS[k] = 0
    return True
