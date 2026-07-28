"""TCP/UDP 原始抓包后端 - Unix 跨平台实现（Linux AF_PACKET / macOS BPF）。

在 macOS / Linux 上替代 Windows 的 WinDivert 抓包：
- Linux: 用 socket(AF_PACKET, SOCK_RAW, ETH_P_ALL) 嗅探所有以太网包
  - 优势：纯 stdlib，无外部依赖；内核态过滤可用 BPF filter 进一步加速
  - 限制：需要 root（CAP_NET_RAW），抓到的是二层帧（含 Ethernet 头）
- macOS: 用 BPF 设备 /dev/bpfN 嗅探
  - 优势：纯 stdlib，无外部依赖；macOS 原生抓包机制
  - 限制：需要 root；BPF 设备需要手动查找空闲设备

抓到的包以与 Windows 版本完全一致的数据结构入队 enrich worker，
保持 raw_capture.py 主流程不变。

性能说明：
- AF_PACKET 在 1000 pps 下吞吐充足（内核缓冲 + 用户态批量读取）
- BPF 在 macOS 上性能优秀（内核态过滤，仅复制匹配包到用户态）
- 与 WinDivert SNIFF 模式等价：只嗅探不拦截，包正常流转不断网
"""

from __future__ import annotations

import base64
import errno
import fcntl
import os
import queue
import socket
import struct
import sys
import threading
import time
from datetime import datetime

from .. import db, logger
from ..ip_region import lookup as _ip_region_lookup

IS_LINUX = sys.platform.startswith("linux")
IS_MACOS = sys.platform == "darwin"
IS_UNIX = IS_LINUX or IS_MACOS

# Linux AF_PACKET 常量
if IS_LINUX:
    # ETH_P_ALL = 0x0003，捕获所有协议的以太网帧
    ETH_P_ALL = 0x0003
    # socket.htons 转网络字节序（AF_PACKET 协议参数需要）
    _ETH_P_ALL_NET = socket.htons(ETH_P_ALL)
else:
    _ETH_P_ALL_NET = 0

# macOS BPF 常量
if IS_MACOS:
    # BIOCSETIF: 把 BPF 设备绑定到指定网络接口
    BIOCSETIF = 0x8010426D
    # BIOCIMMEDIATE: 启用立即返回模式（不做缓冲，包立即返回给用户态）
    BIOCIMMEDIATE = 0x80044270
    # BIOCSBLEN: 设置 BPF 缓冲区长度
    BIOCSBLEN = 0xC0044266
    # BIOCGBLEN: 查询当前 BPF 缓冲区长度
    BIOCGBLEN = 0x40044266
    # BIOCPROMISC: 启用混杂模式（接收所有包，不仅是发往本机的）
    BIOCPROMISC = 0x20004269
    # BPF 设备路径前缀
    _BPF_DEV_PREFIX = "/dev/bpf"


# macOS BPF 头核心字段格式与大小（前 16 字节）
# struct bpf_hdr 核心字段：tv_sec(u32) + tv_usec(u32) + caplen(u16) + datalen(u16) + hdrlen(u16) + pad(u16)
# macOS 10.6+ 的 bpf_timeval 使用 32 位字段，sizeof(struct bpf_hdr)=16；
# 用 hdrlen 字段（内核设置的含对齐长度）定位下一条记录。
# 注意：struct.unpack 要求缓冲区长度精确等于格式所需字节数（<IIHHHH = 16 字节）
_BPF_HDR_FMT_BASE = "<IIHHHH"
_BPF_HDR_BASE_SIZE = 16

# BPF 包对齐边界：BPF_ALIGNMENT = sizeof(long)
# 64 位系统 sizeof(long)=8，32 位系统 sizeof(long)=4
# 内核用 BPF_WORDALIGN(hdrlen + caplen) 定位下一条记录，
# 用 4 字节对齐在 64 位 macOS 上会导致偏移错误（差 4 字节），
# 进而 struct.unpack 读到垃圾值 break，丢失后续整批包。
_BPF_ALIGNMENT = 8 if sys.maxsize > 2**32 else 4


# IP 协议号
_IPPROTO_TCP = 6
_IPPROTO_UDP = 17


# ---------- 性能优化：与 raw_capture.py 完全一致的 LRU 缓存 ----------
# Unix 抓包独立维护一套缓存，避免与 Windows 版本互相干扰

_PID_CACHE_UNIX: "OrderedDict" = None  # 延迟初始化
_PID_CACHE_TTL = 3.0
_PID_CACHE_MAX = 2000
_PID_CACHE_LOCK = threading.Lock()

_PROC_NAME_CACHE_UNIX: "OrderedDict" = None
_PROC_NAME_CACHE_TTL = 5.0
_PROC_NAME_CACHE_MAX = 500
_PROC_NAME_LOCK = threading.Lock()

_IP_REGION_CACHE_UNIX: "OrderedDict" = None
_IP_REGION_CACHE_TTL = 600.0  # 10 分钟
_IP_REGION_CACHE_MAX = 500
_IP_REGION_LOCK = threading.Lock()

_LOCAL_IP_CACHE_UNIX: frozenset[str] | None = None
_LOCAL_IP_CACHE_TS_UNIX: float = 0.0
_LOCAL_IP_CACHE_TTL_UNIX = 60.0
_LOCAL_IP_CACHE_LOCK_UNIX = threading.Lock()

# 缓存初始化锁：保护 _ensure_caches() 的懒初始化（4 个 enrich worker 并发调用时避免 OrderedDict 被覆盖）
_CACHE_INIT_LOCK = threading.Lock()


def _ensure_caches():
    """懒初始化 OrderedDict 缓存（避免 import 时报错）。

    线程安全：用 _CACHE_INIT_LOCK 保护，多个 enrich worker 并发调用时
    只会初始化一次，避免一个线程的 OrderedDict 被另一个线程覆盖（丢失已缓存数据）。
    """
    global _PID_CACHE_UNIX, _PROC_NAME_CACHE_UNIX, _IP_REGION_CACHE_UNIX
    # 双检锁：先无锁检查（快路径），命中则直接返回；未命中再加锁初始化
    if _PID_CACHE_UNIX is None:
        with _CACHE_INIT_LOCK:
            if _PID_CACHE_UNIX is None:
                from collections import OrderedDict
                _PID_CACHE_UNIX = OrderedDict()
                _PROC_NAME_CACHE_UNIX = OrderedDict()
                _IP_REGION_CACHE_UNIX = OrderedDict()


def _is_local_ip_cached(ip: str) -> bool:
    """判断 IP 是否本机 IP（带缓存，与 raw_capture.py 等价）。"""
    global _LOCAL_IP_CACHE_UNIX, _LOCAL_IP_CACHE_TS_UNIX
    if ip in ("127.0.0.1", "::1"):
        return True
    now = time.time()
    local_ips = _LOCAL_IP_CACHE_UNIX
    if local_ips is None or now - _LOCAL_IP_CACHE_TS_UNIX > _LOCAL_IP_CACHE_TTL_UNIX:
        with _LOCAL_IP_CACHE_LOCK_UNIX:
            if _LOCAL_IP_CACHE_UNIX is None or now - _LOCAL_IP_CACHE_TS_UNIX > _LOCAL_IP_CACHE_TTL_UNIX:
                ips: set[str] = set()
                try:
                    hostname = socket.gethostname()
                    for addr in socket.getaddrinfo(hostname, None):
                        ips.add(addr[4][0])
                except Exception:  # noqa: BLE001
                    pass
                _LOCAL_IP_CACHE_UNIX = frozenset(ips)
                _LOCAL_IP_CACHE_TS_UNIX = now
            local_ips = _LOCAL_IP_CACHE_UNIX
    return ip in local_ips


def _lookup_pid_cached(src_ip, src_port, dst_ip, dst_port, list_rows_fn, is_udp: bool = False) -> int | None:
    """带 LRU 缓存的 PID 反查（与 raw_capture.py 等价）。"""
    _ensure_caches()
    if is_udp:
        src_is_local = _is_local_ip_cached(src_ip)
        local_ip = src_ip if src_is_local else dst_ip
        local_port = src_port if src_is_local else dst_port
        cache_key = ("u", local_ip, local_port)
    else:
        cache_key = ("t", src_ip, src_port, dst_ip, dst_port)

    now = time.time()
    with _PID_CACHE_LOCK:
        item = _PID_CACHE_UNIX.get(cache_key)
        if item is not None:
            ts, pid = item
            if now - ts < _PID_CACHE_TTL:
                _PID_CACHE_UNIX.move_to_end(cache_key)
                return pid
            _PID_CACHE_UNIX.pop(cache_key, None)

    pid = None
    try:
        if is_udp:
            for (lip, lport, p) in list_rows_fn():
                if lport == local_port and (lip == local_ip or lip == "0.0.0.0"):
                    pid = p
                    break
        else:
            for (lip, lport, rip, rport, p, _state) in list_rows_fn():
                if lip == src_ip and lport == src_port and rip == dst_ip and rport == dst_port:
                    pid = p
                    break
                if lip == dst_ip and lport == dst_port and rip == src_ip and rport == src_port:
                    pid = p
                    break
    except Exception:  # noqa: BLE001
        pid = None

    with _PID_CACHE_LOCK:
        _PID_CACHE_UNIX[cache_key] = (now, pid)
        _PID_CACHE_UNIX.move_to_end(cache_key)
        if len(_PID_CACHE_UNIX) > _PID_CACHE_MAX:
            _PID_CACHE_UNIX.popitem(last=False)
    return pid


def _proc_name_cached(pid: int) -> str:
    """带 LRU 缓存的进程名查询，5 秒 TTL。"""
    _ensure_caches()
    now = time.time()
    with _PROC_NAME_LOCK:
        item = _PROC_NAME_CACHE_UNIX.get(pid)
        if item is not None:
            ts, name = item
            if now - ts < _PROC_NAME_CACHE_TTL:
                _PROC_NAME_CACHE_UNIX.move_to_end(pid)
                return name
            _PROC_NAME_CACHE_UNIX.pop(pid, None)

    try:
        import psutil
        name = psutil.Process(pid).name()
    except Exception:  # noqa: BLE001
        name = ""

    with _PROC_NAME_LOCK:
        _PROC_NAME_CACHE_UNIX[pid] = (now, name)
        _PROC_NAME_CACHE_UNIX.move_to_end(pid)
        if len(_PROC_NAME_CACHE_UNIX) > _PROC_NAME_CACHE_MAX:
            _PROC_NAME_CACHE_UNIX.popitem(last=False)
    return name


def _ip_region_cached(ip: str) -> str:
    """带 LRU 缓存的 IP 属地查询，10 分钟 TTL。"""
    _ensure_caches()
    now = time.time()
    with _IP_REGION_LOCK:
        item = _IP_REGION_CACHE_UNIX.get(ip)
        if item is not None:
            ts, region = item
            if now - ts < _IP_REGION_CACHE_TTL:
                _IP_REGION_CACHE_UNIX.move_to_end(ip)
                return region
            _IP_REGION_CACHE_UNIX.pop(ip, None)

    region = _ip_region_lookup(ip) or ""

    with _IP_REGION_LOCK:
        _IP_REGION_CACHE_UNIX[ip] = (now, region)
        _IP_REGION_CACHE_UNIX.move_to_end(ip)
        if len(_IP_REGION_CACHE_UNIX) > _IP_REGION_CACHE_MAX:
            _IP_REGION_CACHE_UNIX.popitem(last=False)
    return region


def _enrich_one(session_id: int, raw: dict, list_tcp_fn, list_udp_fn,
                pid_filter, port_filter, self_ports):
    """处理单个 raw dict：PID 反查 → 进程名 → PID 过滤 → DNS 解析 → IP 属地 → 写库。

    与 RawCapture._enrich_one 逻辑完全等价，独立实现避免循环依赖。
    """
    proto_name = raw["proto_name"]
    src_ip, dst_ip = raw["src_ip"], raw["dst_ip"]
    src_port, dst_port = raw["src_port"], raw["dst_port"]
    payload = raw["payload"]
    is_outbound = raw["is_outbound"]
    remote_ip = raw["remote_ip"]
    remote_port = raw["remote_port"]
    local_port = raw["local_port"]
    timestamp = raw["timestamp"]

    # PID 反查（带 LRU 缓存）
    if proto_name == "tcp":
        pid = _lookup_pid_cached(src_ip, src_port, dst_ip, dst_port, list_tcp_fn, is_udp=False)
    else:
        pid = _lookup_pid_cached(src_ip, src_port, dst_ip, dst_port, list_udp_fn, is_udp=True)

    # PID 过滤
    if pid_filter and pid not in pid_filter:
        return

    proc_name = ""
    if pid:
        proc_name = _proc_name_cached(pid)

    # DNS 解析：UDP 端口 53 的包尝试解析为 DNS
    dns_info = None
    if proto_name == "udp" and (src_port == 53 or dst_port == 53) and payload:
        try:
            from .dns_parser import parse_dns
            dns_info = parse_dns(payload)
        except Exception:  # noqa: BLE001
            dns_info = None

    if dns_info:
        q = dns_info.get("questions") or [{}]
        qname = q[0].get("qname", "") if q else ""
        qtype = q[0].get("qtype_name", "") if q else ""
        if dns_info.get("is_response"):
            answers = dns_info.get("answers") or []
            a_values = []
            for a in answers[:5]:
                if a.get("type") in (1, 28):
                    a_values.append(a.get("rdata", ""))
            ans_str = ", ".join(a_values) if a_values else dns_info.get("rcode_name", "")
            path = f"DNS {qname} {qtype} -> {ans_str}"
            method = "DNS-RESP"
        else:
            path = f"DNS {qname} {qtype}"
            method = "DNS-QUERY"
        host = qname or remote_ip
        dns_summary = _dns_summary(dns_info)
        flow = {
            "session_id": session_id,
            "timestamp": timestamp,
            "pid": pid,
            "process_name": proc_name,
            "method": method,
            "url": f"udp://{remote_ip}:{remote_port}",
            "scheme": "dns",
            "host": host,
            "path": path,
            "request_headers": "{}",
            "request_body": dns_summary if not dns_info.get("is_response") else "",
            "status_code": 0 if dns_info.get("rcode") == 0 else (dns_info.get("rcode") or None),
            "response_headers": "{}",
            "response_body": dns_summary if dns_info.get("is_response") else "",
            "duration_ms": 0,
            "size": len(payload),
            "protocol": "dns",
            "raw_data": "base64:" + base64.b64encode(payload).decode("ascii") if payload else None,
            "src_port": src_port,
            "dst_port": dst_port,
            "remote_ip": remote_ip,
            "ip_region": _ip_region_cached(remote_ip) if remote_ip else "",
        }
    else:
        flow = {
            "session_id": session_id,
            "timestamp": timestamp,
            "pid": pid,
            "process_name": proc_name,
            "method": "SEND" if is_outbound else "RECV",
            "url": f"{proto_name}://{remote_ip}:{remote_port}",
            "scheme": proto_name,
            "host": remote_ip,
            "path": f"/{proto_name}/{local_port}->{remote_port}",
            "request_headers": "{}",
            "request_body": "base64:" + base64.b64encode(payload).decode("ascii") if payload and is_outbound else "",
            "status_code": None,
            "response_headers": "{}",
            "response_body": "base64:" + base64.b64encode(payload).decode("ascii") if payload and not is_outbound else "",
            "duration_ms": 0,
            "size": len(payload),
            "protocol": proto_name,
            "raw_data": "base64:" + base64.b64encode(payload).decode("ascii") if payload else None,
            "src_port": src_port,
            "dst_port": dst_port,
            "remote_ip": remote_ip,
            "ip_region": _ip_region_cached(remote_ip) if remote_ip else "",
        }
    db.insert_flow_async(flow)


def _dns_summary(dns_info: dict) -> str:
    """把 DNS 解析结果格式化为可读文本。"""
    import json as _json
    return _json.dumps(dns_info, ensure_ascii=False, indent=2)


class UnixRawCapture:
    """Unix 平台 TCP/UDP 抓包后端。

    对外接口与 Windows 的 RawCapture 类保持一致：
    - start() / stop() / status()
    - _enrich_queue 暴露给 enrich worker

    与 Windows 版本的差异：
    - 没有 pydivert，直接用 socket(AF_PACKET) 或 BPF 设备
    - 抓到的是二层帧，需要手动解析 Ethernet + IP + TCP/UDP 头
    - SNIFF 模式（只读不写），不影响网络流量
    """

    def __init__(self, session_id: int):
        self.session_id = session_id
        self._running = False
        self._thread: threading.Thread | None = None
        self._sock: socket.socket | None = None
        self._bpf_fd: int | None = None
        self._last_error: str = ""
        # Telnix 自身端口，代码层排除避免回环噪音
        self._self_ports = {8888, 18901}
        self._pid_filter: set[int] | None = None
        self._port_filter: set[int] | None = None
        # 与 Windows 版本一致的 enrich 队列
        self._enrich_queue: queue.Queue = queue.Queue(maxsize=20000)
        self._enrich_threads: list[threading.Thread] = []
        self._enrich_worker_count = 4
        # macOS BPF 设备路径（启动时选定）
        self._bpf_dev_path: str | None = None
        # 网络接口名（启动时选定）
        self._ifname: str | None = None
        # 丢包计数
        self._dropped = 0
        self._dropped_lock = threading.Lock()
        # BPF 缓冲区长度（macOS）
        self._bpf_buf_len = 1 * 1024 * 1024

    def set_pid_filter(self, pids: set[int] | None):
        self._pid_filter = pids

    def set_port_filter(self, ports: set[int] | None):
        self._port_filter = ports

    def set_filter(self, filter_str: str):
        """Unix 上不支持 WinDivert filter 字符串，忽略。"""
        # 端口过滤通过代码层实现
        pass

    @property
    def running(self) -> bool:
        return self._running

    @property
    def last_error(self) -> str:
        return self._last_error

    def _is_admin(self) -> bool:
        """检查 root 权限（AF_PACKET/BPF 都需要）。"""
        try:
            return os.geteuid() == 0
        except AttributeError:
            return False

    def start(self) -> bool:
        """启动抓包。"""
        if not IS_UNIX:
            self._last_error = "UnixRawCapture 仅支持 Linux/macOS"
            return False
        if self._running:
            return True
        try:
            if not self._is_admin():
                self._last_error = "需要 root 权限（AF_PACKET/BPF 需要 CAP_NET_RAW）"
                logger.error("raw", "Unix 抓包需要 root", "请用 sudo 启动 Telnix")
                return False

            if IS_LINUX:
                ok = self._start_linux()
            else:
                ok = self._start_macos()
            if not ok:
                return False

            self._running = True
            self._thread = threading.Thread(target=self._capture_loop, daemon=True, name="raw-unix-capture")
            self._thread.start()
            for i in range(self._enrich_worker_count):
                t = threading.Thread(target=self._enrich_loop, daemon=True, name=f"raw-unix-enrich-{i}")
                t.start()
                self._enrich_threads.append(t)
            logger.info("raw", "TCP/UDP 抓包已启动 (Unix)",
                        f"platform={sys.platform}, ifname={self._ifname}")
            return True
        except Exception as e:  # noqa: BLE001
            self._last_error = str(e)
            logger.error("raw", "Unix 抓包启动失败", str(e))
            self._cleanup_socket()
            return False

    def _start_linux(self) -> bool:
        """Linux: 用 AF_PACKET raw socket 抓包。"""
        try:
            self._sock = socket.socket(
                socket.AF_PACKET, socket.SOCK_RAW, _ETH_P_ALL_NET
            )
            # 加大接收缓冲区到 4MB，避免高 pps 丢包
            try:
                self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
            except OSError:
                pass  # 内核限制可能不允许这么大，忽略
            # AF_PACKET with ETH_P_ALL 自动嗅探所有接口
            self._ifname = "any"
            return True
        except PermissionError as e:
            self._last_error = f"权限不足：{e}（需要 CAP_NET_RAW / root）"
            return False
        except OSError as e:
            self._last_error = f"AF_PACKET 创建失败: {e}"
            return False

    def _start_macos(self) -> bool:
        """macOS: 用 BPF 设备抓包。"""
        # 查找空闲的 BPF 设备 /dev/bpf0 ~ /dev/bpf255
        bpf_path = None
        fd = None
        for i in range(256):
            path = f"{_BPF_DEV_PREFIX}{i}"
            try:
                fd = os.open(path, os.O_RDWR)
                bpf_path = path
                break
            except OSError as e:
                if e.errno == errno.EBUSY:
                    continue  # 设备被占用，尝试下一个
                if e.errno == errno.EACCES:
                    self._last_error = f"权限不足：无法打开 {path}（需要 root）"
                    return False
                # ENOENT 等其他错误：跳出循环
                break
        if bpf_path is None or fd is None:
            self._last_error = "找不到空闲的 BPF 设备（/dev/bpfN 全部占用）"
            return False
        self._bpf_fd = fd
        self._bpf_dev_path = bpf_path

        try:
            # 设置缓冲区长度（BPF 一次 read 返回多个包，缓冲区越大吞吐越高）
            buf_len = 1 * 1024 * 1024  # 1MB
            fcntl.ioctl(fd, BIOCSBLEN, struct.pack("I", buf_len))
            # 查询实际缓冲区长度
            actual_len = struct.unpack("I", fcntl.ioctl(fd, BIOCGBLEN, b"\x00" * 4))[0]

            # 选定接口：优先选 en0（默认网卡），失败时尝试 en1
            ifname = self._find_active_interface()
            if not ifname:
                self._last_error = "找不到活动的网络接口"
                self._cleanup_socket()
                return False
            self._ifname = ifname

            # 把 BPF 设备绑定到接口（BIOCSETIF 接受 ifreq 结构）
            # ifreq 结构：char[16] ifname + 16 字节 padding
            ifreq = ifname.encode("utf-8").ljust(32, b"\x00")
            fcntl.ioctl(fd, BIOCSETIF, ifreq)

            # 启用立即返回模式（默认 BPF 会缓冲到 buf 满，立即模式让包立即返回）
            fcntl.ioctl(fd, BIOCIMMEDIATE, struct.pack("I", 1))

            # 启用混杂模式（接收所有包，不仅发往本机的）
            try:
                fcntl.ioctl(fd, BIOCPROMISC, 0)
            except OSError:
                pass  # 部分接口不支持混杂模式，忽略

            # 缓冲区长度用作 read 的 buffer 大小
            self._bpf_buf_len = max(actual_len, buf_len)
            return True
        except OSError as e:
            self._last_error = f"BPF 配置失败: {e}"
            self._cleanup_socket()
            return False

    def _find_active_interface(self) -> str | None:
        """查找活动网络接口（macOS）。"""
        # 优先用 en0（默认有线/无线），失败时尝试 en1
        candidates = ["en0", "en1", "en2"]
        for name in candidates:
            try:
                # 用 socket.ioctl SIOCGIFFLAGS 检查接口是否 up
                SIOCGIFFLAGS = 0xc0206911
                ifreq = name.encode("utf-8").ljust(32, b"\x00")
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, 0)
                try:
                    result = fcntl.ioctl(sock.fileno(), SIOCGIFFLAGS, ifreq)
                finally:
                    sock.close()
                flags = struct.unpack("16sH14x", result)[1]
                IFF_UP = 0x1
                IFF_RUNNING = 0x40
                if (flags & IFF_UP) and (flags & IFF_RUNNING):
                    return name
            except OSError:
                continue
        # 兜底：直接返回 en0，让 BPF 自己判断
        return "en0"

    def _cleanup_socket(self):
        """清理 socket / BPF 设备。"""
        if self._sock is not None:
            try:
                self._sock.close()
            except Exception:  # noqa: BLE001
                pass
            self._sock = None
        if self._bpf_fd is not None:
            try:
                os.close(self._bpf_fd)
            except Exception:  # noqa: BLE001
                pass
            self._bpf_fd = None
        self._bpf_dev_path = None

    def stop(self):
        """停止抓包。"""
        self._running = False
        # 关闭 socket / BPF 设备，解除 recv 阻塞
        self._cleanup_socket()
        if self._thread:
            self._thread.join(timeout=2)
            self._thread = None
        # 唤醒 enrich worker
        for _ in range(len(self._enrich_threads)):
            try:
                self._enrich_queue.put_nowait(None)
            except queue.Full:
                pass
        for t in self._enrich_threads:
            t.join(timeout=2)
        self._enrich_threads.clear()
        with self._dropped_lock:
            total_dropped = self._dropped
            self._dropped = 0
        if total_dropped > 0:
            logger.warning("raw", "Unix 抓包停止，丢包统计", f"累计丢包: {total_dropped}")
        logger.info("raw", "TCP/UDP 抓包已停止 (Unix)")

    def _capture_loop(self):
        """抓包主循环（与 Windows 版本相同的轻量入队模式）。"""
        while self._running:
            try:
                if IS_LINUX:
                    self._capture_one_linux()
                else:
                    self._capture_one_macos()
            except Exception as e:  # noqa: BLE001
                if self._running:
                    logger.error("raw", "Unix 抓包循环异常", str(e))
                    time.sleep(0.1)

    def _capture_one_linux(self):
        """Linux: 从 AF_PACKET socket 读一个包并解析。"""
        try:
            # recvfrom 返回 (data, (ifname, proto, pkttype, hatype, halen))
            data, _ = self._sock.recvfrom(65536)
        except BlockingIOError:
            return
        except OSError as e:
            if self._running:
                logger.error("raw", "AF_PACKET recv 失败", str(e))
            return
        # Linux AF_PACKET 数据含 Ethernet 头（14 字节）
        # Ethernet: dst_mac(6) + src_mac(6) + ethertype(2)
        if len(data) < 14:
            return
        ethertype = struct.unpack("!H", data[12:14])[0]
        # 0x0800=IPv4, 0x86DD=IPv6, 0x0806=ARP
        if ethertype != 0x0800:
            # 仅处理 IPv4（IPv6 抓包暂不支持）
            return
        # 跳过 Ethernet 头，解析 IP 包
        self._parse_and_enqueue_ip_packet(data[14:])

    def _capture_one_macos(self):
        """macOS: 从 BPF 设备读一批包并解析。

        BPF 一次 read 返回多个包，每个包前有 bpf_hdr 结构：
        struct bpf_hdr {
            bpf_u_int32 bh_tstamp;     // 时间戳（秒）
            u_short bh_caplen;         // 捕获长度
            u_short bh_datalen;        // 原始包长度
            u_short bh_hdrlen;         // bpf_hdr 长度（含 padding）
        };
        """
        try:
            data = os.read(self._bpf_fd, self._bpf_buf_len)
        except OSError as e:
            if self._running:
                logger.error("raw", "BPF read 失败", str(e))
            return
        if not data:
            return
        # 遍历 BPF 包
        offset = 0
        while offset + _BPF_HDR_BASE_SIZE <= len(data):
            # bpf_hdr 核心字段：tv_sec(4) + tv_usec(4) + caplen(2) + datalen(2) + hdrlen(2) + pad(2) = 16 字节
            # 注意：struct.unpack 要求缓冲区长度精确等于格式所需字节数（<IIHHHH 需 16 字节），
            # 之前传 18 字节会抛 struct.error 导致首包即 break，整批包丢失。
            # 64 位 macOS 上 bpf_hdr 实际为 24 字节（含对齐填充），但前 16 字节核心字段布局一致，
            # 用 hdrlen 字段（含对齐）定位下一条记录。
            try:
                tv_sec, tv_usec, caplen, datalen, hdrlen, _pad = struct.unpack(
                    _BPF_HDR_FMT_BASE, data[offset:offset + _BPF_HDR_BASE_SIZE]
                )
            except struct.error:
                break
            if hdrlen < _BPF_HDR_BASE_SIZE or caplen == 0:
                break
            pkt_start = offset + hdrlen
            pkt_end = pkt_start + caplen
            if pkt_end > len(data):
                break
            packet = data[pkt_start:pkt_end]
            # BPF 包含 Ethernet 头
            if len(packet) >= 14:
                ethertype = struct.unpack("!H", packet[12:14])[0]
                if ethertype == 0x0800:
                    self._parse_and_enqueue_ip_packet(packet[14:])
            # 对齐到下一个包：内核用 BPF_WORDALIGN(hdrlen + caplen) 定位下一条记录，
            # BPF_ALIGNMENT = sizeof(long)（64 位系统=8，32 位=4）。
            # 之前用 4 字节对齐在 64 位 macOS 上会导致偏移错误，丢失后续包。
            offset = pkt_end
            if offset % _BPF_ALIGNMENT != 0:
                offset += _BPF_ALIGNMENT - (offset % _BPF_ALIGNMENT)

    def _parse_and_enqueue_ip_packet(self, ip_data: bytes):
        """解析 IPv4 包并构造 raw dict 入队。"""
        if len(ip_data) < 20:
            return
        # IP 头前 4 位 version + IHL
        ver_ihl = ip_data[0]
        version = (ver_ihl >> 4) & 0xF
        if version != 4:
            return
        ihl = ver_ihl & 0xF
        ip_hdr_len = ihl * 4
        if ip_hdr_len < 20 or len(ip_data) < ip_hdr_len:
            return
        protocol = ip_data[9]
        # 仅处理 TCP / UDP
        if protocol != _IPPROTO_TCP and protocol != _IPPROTO_UDP:
            return
        # 源/目的 IP（网络字节序转字符串）
        src_ip = socket.inet_ntoa(ip_data[12:16])
        dst_ip = socket.inet_ntoa(ip_data[16:20])

        # 代码层排除 loopback（与 Windows 版本一致）
        if src_ip == "127.0.0.1" or dst_ip == "127.0.0.1":
            return

        # 解析 TCP / UDP 头
        transport = ip_data[ip_hdr_len:]
        if protocol == _IPPROTO_TCP:
            if len(transport) < 20:
                return
            src_port, dst_port = struct.unpack("!HH", transport[:4])
            proto_name = "tcp"
            # TCP 头长度（data offset 字段）
            data_offset = ((transport[12] >> 4) & 0xF) * 4
            if len(transport) < data_offset:
                return
            payload = transport[data_offset:]
        else:  # UDP
            if len(transport) < 8:
                return
            src_port, dst_port = struct.unpack("!HH", transport[:4])
            proto_name = "udp"
            udp_len = struct.unpack("!H", transport[4:6])[0]
            if udp_len >= 8 and len(transport) >= udp_len:
                payload = transport[8:udp_len]
            else:
                payload = transport[8:]

        # 排除 Telnix 自身端口
        if src_port in self._self_ports or dst_port in self._self_ports:
            return

        # 端口过滤
        if self._port_filter:
            if src_port not in self._port_filter and dst_port not in self._port_filter:
                return

        # 判断方向：本机发出的包算请求
        is_outbound = _is_local_ip_cached(src_ip)
        remote_ip = dst_ip if is_outbound else src_ip
        remote_port = dst_port if is_outbound else src_port
        local_port = src_port if is_outbound else dst_port

        raw = {
            "proto_name": proto_name,
            "src_ip": src_ip, "dst_ip": dst_ip,
            "src_port": src_port, "dst_port": dst_port,
            "payload": payload,
            "is_outbound": is_outbound,
            "remote_ip": remote_ip, "remote_port": remote_port,
            "local_port": local_port,
            "timestamp": datetime.now().isoformat(),
        }
        try:
            self._enrich_queue.put_nowait(raw)
        except queue.Full:
            with self._dropped_lock:
                self._dropped += 1
                if self._dropped % 100 == 0:
                    logger.warning("raw", "Unix 抓包队列满丢包", f"累计丢包: {self._dropped}")

    def _enrich_loop(self):
        """enrich worker：从队列取 raw dict，补充 PID/进程名/IP属地/DNS，写库。"""
        from .process_lookup import _list_tcp_owner_rows, _list_udp_owner_rows
        while self._running:
            try:
                raw = self._enrich_queue.get(timeout=1)
            except queue.Empty:
                continue
            if raw is None:  # 停止信号
                break
            try:
                _enrich_one(
                    self.session_id, raw,
                    _list_tcp_owner_rows, _list_udp_owner_rows,
                    self._pid_filter, self._port_filter, self._self_ports,
                )
            except Exception as e:  # noqa: BLE001
                if self._running:
                    logger.error("raw", "Unix enrich 异常", str(e))


def is_unix_raw_capture_available() -> bool:
    """检查 Unix 抓包后端是否可用（仅判断平台，不检查 root）。"""
    return IS_UNIX


def get_unix_raw_capture_status() -> dict:
    """返回 Unix 抓包后端状态（用于前端显示）。"""
    try:
        is_admin = os.geteuid() == 0
    except AttributeError:
        is_admin = False
    return {
        "running": False,  # 实际状态由 RawCapture facade 维护
        "supported": True,
        "backend": "af_packet" if IS_LINUX else ("bpf" if IS_MACOS else "none"),
        "is_admin": is_admin,
        "hint": ("就绪" if is_admin else "需要 root 权限") if IS_UNIX else "不支持的平台",
    }
