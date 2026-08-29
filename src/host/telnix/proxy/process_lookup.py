"""Reverse lookup of client connection PID + process name via GetExtendedTcpTable.

When a client connects to the proxy, the client's source port corresponds to a PID.
Look up the row in the TCP table where local_addr=client_ip, local_port=client_port,
and take its OwningPid.

Cross-platform notes: GetExtendedTcpTable is a Windows-only API; on non-Windows
platforms, psutil's net_connections() is used instead (psutil is already a
dependency; functionally equivalent but slightly lower precision).
"""

import concurrent.futures
import socket
import sys
import threading
import time
from collections import OrderedDict

import psutil

# 后台 PID 查找专用线程池（模块级单例）
# single-flight 模式：所有并发连接共享一次 TCP 表扫描结果
_PID_REFRESH_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=4, thread_name_prefix="pid-refresh"
)

# UDP 表快照缓存（1 秒 TTL，避免高频调用 psutil.net_connections 全表扫描）
_UDP_ROWS_CACHE: list[tuple[str, int, int]] | None = None
_UDP_ROWS_CACHE_TS: float = 0.0
_UDP_ROWS_CACHE_TTL = 1.0
_UDP_ROWS_CACHE_LOCK = threading.Lock()

# 平台判断：ctypes.windll 仅 Windows 可用
IS_WINDOWS = sys.platform == "win32"
if IS_WINDOWS:
    import ctypes
    import struct
else:
    ctypes = None  # type: ignore[assignment]
    struct = None  # type: ignore[assignment]

# GetExtendedTcpTable 参数（仅 Windows 用）
TCP_TABLE_OWNER_PID_ALL = 5
AF_INET = 2


if IS_WINDOWS:
    class _MIB_TCPROW_OWNER_PID(ctypes.Structure):  # type: ignore[name-defined]
        _fields_ = [
            ("dwState", ctypes.c_ulong),
            ("dwLocalAddr", ctypes.c_ulong),
            ("dwLocalPort", ctypes.c_ulong),
            ("dwRemoteAddr", ctypes.c_ulong),
            ("dwRemotePort", ctypes.c_ulong),
            ("dwOwningPid", ctypes.c_ulong),
        ]


    class _MIB_TCPTABLE_OWNER_PID(ctypes.Structure):  # type: ignore[name-defined]
        _fields_ = [
            ("dwNumEntries", ctypes.c_ulong),
            ("table", _MIB_TCPROW_OWNER_PID * 1),
        ]


    _phlpapi = ctypes.windll.iphlpapi


def _list_tcp_owner_rows() -> list[tuple[str, int, str, int, int, int]]:
    """Return list of (local_ip, local_port, remote_ip, remote_port, pid, state).

    Windows: uses GetExtendedTcpTable (high performance, native API).
    Non-Windows: uses psutil.net_connections(kind="inet") (cross-platform; state
    field lacks Windows status codes).
    """
    if not IS_WINDOWS:
        # 非 Windows 平台：用 psutil 替代 GetExtendedTcpTable
        rows: list[tuple[str, int, str, int, int, int]] = []
        try:
            for c in psutil.net_connections(kind="inet"):
                if not c.laddr or not c.raddr:
                    continue
                # state 映射：psutil 用字符串，这里转为 0（非 Windows 无需精确状态码）
                rows.append((c.laddr.ip, c.laddr.port, c.raddr.ip, c.raddr.port,
                             c.pid or 0, 0))
        except Exception:  # noqa: BLE001
            pass
        return rows
    # Windows 原生路径
    size = ctypes.c_ulong(0)
    _phlpapi.GetExtendedTcpTable(None, ctypes.byref(size), False, AF_INET,
                                 TCP_TABLE_OWNER_PID_ALL, 0)
    buf = (ctypes.c_byte * size.value)()
    ret = _phlpapi.GetExtendedTcpTable(buf, ctypes.byref(size), False, AF_INET,
                                       TCP_TABLE_OWNER_PID_ALL, 0)
    if ret != 0:
        return []
    table = ctypes.cast(buf, ctypes.POINTER(_MIB_TCPTABLE_OWNER_PID)).contents
    n = table.dwNumEntries
    rows = []
    # 第一项在 table.table[0]，后续项紧随其后
    base = ctypes.addressof(table) + ctypes.sizeof(ctypes.c_ulong)
    row_size = ctypes.sizeof(_MIB_TCPROW_OWNER_PID)
    for i in range(n):
        row = ctypes.cast(base + i * row_size,
                          ctypes.POINTER(_MIB_TCPROW_OWNER_PID)).contents
        local_ip = socket.inet_ntoa(struct.pack("<I", row.dwLocalAddr))
        remote_ip = socket.inet_ntoa(struct.pack("<I", row.dwRemoteAddr))
        # 端口以网络字节序存储在低 16 位
        local_port = socket.ntohs(row.dwLocalPort & 0xFFFF)
        remote_port = socket.ntohs(row.dwRemotePort & 0xFFFF)
        rows.append((local_ip, local_port, remote_ip, remote_port,
                     int(row.dwOwningPid), int(row.dwState)))
    return rows


def _list_udp_owner_rows() -> list[tuple[str, int, int]]:
    """Return list of (local_ip, local_port, pid).

    UDP is connectionless; the UDP table only has local-end info (no
    remote_ip/remote_port). Matching is done by local IP+port: outbound packets
    use src, inbound packets use dst.

    Uses psutil to get the UDP connection table (more reliable than ctypes
    GetExtendedUdpTable, whose struct offsets are error-prone on 64-bit systems).
    psutil itself is cross-platform. Includes a 1-second TTL module-level cache:
    multiple workers within the same second share a single psutil scan result.
    """
    global _UDP_ROWS_CACHE, _UDP_ROWS_CACHE_TS
    now = time.time()
    cached = _UDP_ROWS_CACHE
    if cached is not None and now - _UDP_ROWS_CACHE_TS < _UDP_ROWS_CACHE_TTL:
        return cached
    with _UDP_ROWS_CACHE_LOCK:
        if _UDP_ROWS_CACHE is not None and now - _UDP_ROWS_CACHE_TS < _UDP_ROWS_CACHE_TTL:
            return _UDP_ROWS_CACHE
        rows: list[tuple[str, int, int]] = []
        try:
            for c in psutil.net_connections(kind="udp"):
                if c.laddr:
                    rows.append((c.laddr.ip, c.laddr.port, c.pid or 0))
        except Exception:  # noqa: BLE001
            pass
        _UDP_ROWS_CACHE = rows
        _UDP_ROWS_CACHE_TS = now
        return rows


class ProcessLookup:
    """Reverse lookup of PID + process name via GetExtendedTcpTable. With caching."""

    def __init__(self):
        self._pid_cache: dict[int, str] = {}
        self._lock = threading.Lock()
        # 进程列表缓存（避免每次都遍历 psutil.process_iter，耗时 3-4 秒）
        self._list_cache: list[dict] = []
        self._list_cache_time: float = 0.0
        self._list_cache_lock = threading.Lock()
        self._list_cache_ttl = 5.0  # 缓存 5 秒
        # 性能优化：客户端 (ip, port) → (pid, name) 的 LRU 缓存（OrderedDict，O(1) 淘汰）
        self._addr_cache: OrderedDict[tuple[str, int], tuple[float, int | None, str | None]] = OrderedDict()
        self._addr_cache_lock = threading.Lock()
        self._addr_cache_ttl = 3.0  # 3 秒 TTL（端口复用窗口短）
        self._addr_cache_max_size = 500
        # 性能优化：客户端 (ip,) → (pid, name) 的 fallback 缓存（OrderedDict，O(1) 淘汰）
        self._ip_cache: OrderedDict[str, tuple[float, int | None, str | None]] = OrderedDict()
        self._ip_cache_lock = threading.Lock()
        self._ip_cache_ttl = 30.0  # 30 秒 TTL（进程 PID 比端口稳定得多）
        self._ip_cache_max_size = 100
        # single-flight 模式：所有并发连接共享一次 TCP 表扫描
        # 首次 miss 时启动后台扫描，并发连接等待 Event 完成后查缓存
        # 避免同步阻塞每个连接（200ms × 50 连接 = 10s 延迟）
        self._refresh_event: threading.Event | None = None
        self._refresh_lock = threading.Lock()

    def _process_name(self, pid: int) -> str | None:
        if pid is None or pid <= 0:
            return None
        with self._lock:
            cached = self._pid_cache.get(pid)
            if cached is not None:
                return cached
        # 查询失败的 None 不缓存：进程退出后 PID 可能被复用，下次应重新查询
        try:
            name = psutil.Process(pid).name()
        except Exception:  # noqa: BLE001
            return None
        with self._lock:
            self._pid_cache[pid] = name
        return name

    def lookup(self, client_addr, timeout: float = 0.05) -> tuple[int | None, str | None]:
        """client_addr = (ip, port); returns (pid, process_name). On failure returns (None, None).

        single-flight mode (balancing performance + process name display):
        1. (ip, port) -> (pid, name) LRU cache (3-second TTL)
        2. (ip,) -> pid cache (30-second TTL): the same client process's PID is stable
           Note: localhost (127.0.0.1/::1) skips this cache — multiple local processes
           share the same IP, so ip_cache would cause process misidentification
           (e.g. chrome's PID assigned to curl)
        3. On cache miss, trigger a background scan (only once); concurrent connections
           wait for the Event to complete
           - After scan completes (~5ms), Event.set; all waiting connections query the cache immediately
           - On timeout (150ms), return (None, None) without blocking the proxy main thread
        """
        if not client_addr:
            return None, None
        client_ip, client_port = client_addr[0], client_addr[1]
        addr_key = (client_ip, client_port)
        now = time.time()
        # 1. 查 (ip, port) 精确缓存
        with self._addr_cache_lock:
            cached = self._addr_cache.get(addr_key)
            if cached is not None:
                ts, pid, name = cached
                if now - ts < self._addr_cache_ttl:
                    return pid, name
        # 1b. 查 (ip,) fallback 缓存（仅非 localhost：本机多进程共享 127.0.0.1 会误判）
        # F13 修复：消除嵌套锁，统一锁顺序为 _ip_cache_lock → 释放 → _addr_cache_lock（非嵌套）。
        # 原代码先持 _ip_cache_lock 再嵌套 _addr_cache_lock，与 _do_refresh 的锁顺序不一致（死锁隐患）。
        # ip_cached 是 tuple（不可变），读出后释放锁不影响本次值（无 TOCTOU）。
        if client_ip not in ("127.0.0.1", "::1", "0.0.0.0"):
            with self._ip_cache_lock:
                ip_cached = self._ip_cache.get(client_ip)
            if ip_cached is not None:
                ip_ts, ip_pid, ip_name = ip_cached
                if now - ip_ts < self._ip_cache_ttl:
                    with self._addr_cache_lock:
                        self._addr_cache[addr_key] = (now, ip_pid, ip_name)
                    return ip_pid, ip_name
        # 2. single-flight：触发或等待后台扫描
        self._trigger_refresh(client_ip, client_port)
        # 等待扫描完成（带超时），不阻塞代理主线程太久
        if self._refresh_event is not None:
            self._refresh_event.wait(timeout=timeout)
        # 3. 再次查缓存（扫描完成后结果已写入）
        with self._addr_cache_lock:
            cached = self._addr_cache.get(addr_key)
            if cached is not None:
                ts, pid, name = cached
                if time.time() - ts < self._addr_cache_ttl:
                    return pid, name
        if client_ip not in ("127.0.0.1", "::1", "0.0.0.0"):
            with self._ip_cache_lock:
                ip_cached = self._ip_cache.get(client_ip)
                if ip_cached is not None:
                    ip_ts, ip_pid, ip_name = ip_cached
                    if time.time() - ip_ts < self._ip_cache_ttl:
                        return ip_pid, ip_name
        return None, None

    def _trigger_refresh(self, client_ip: str, client_port: int):
        """Trigger a background TCP table scan. If a scan is already running, wait to avoid duplicate scans."""
        with self._refresh_lock:
            if self._refresh_event is not None and not self._refresh_event.is_set():
                return  # 已有扫描在跑，等待即可
            self._refresh_event = threading.Event()
            event = self._refresh_event
        try:
            _PID_REFRESH_EXECUTOR.submit(self._do_refresh, client_ip, client_port, event)
        except Exception:  # noqa: BLE001
            # 线程池满，立即 set Event 避免等待方永久阻塞
            event.set()
            with self._refresh_lock:
                if self._refresh_event is event:
                    self._refresh_event = None

    def _do_refresh(self, client_ip: str, client_port: int, event: threading.Event):
        """Run a full TCP table scan in the background + update cache. Sets the Event when done."""
        try:
            now = time.time()
            try:
                rows = _list_tcp_owner_rows()
            except Exception:  # noqa: BLE001
                return
            port_pid_map: dict[tuple[str, int], int] = {}
            for (lip, lport, _rip, _rport, pid, _state) in rows:
                port_pid_map[(lip, lport)] = pid
            # 批量更新 addr_cache（所有同 IP 的端口一次性写入）
            # F8 修复：锁外预计算进程名，锁内仅做字典写入。
            # 原代码持 _addr_cache_lock 期间在循环内调 _process_name → psutil.Process(pid).name()
            # （Windows 1-10ms/次），50 端口 × 5ms = 250ms 全程持锁，阻塞所有 lookup 调用。
            # _process_name 自身有 _pid_cache + _lock 保护，可安全在锁外调用。
            name_map: dict[int, str | None] = {}
            for (lip, lport), pid in port_pid_map.items():
                if lip == client_ip and pid not in name_map:
                    name_map[pid] = self._process_name(pid)
            with self._addr_cache_lock:
                for (lip, lport), pid in port_pid_map.items():
                    if lip == client_ip:
                        self._addr_cache[(lip, lport)] = (now, pid, name_map[pid])
                # O(1) LRU 淘汰：OrderedDict.popitem(last=False) 移除最旧条目
                while len(self._addr_cache) > self._addr_cache_max_size:
                    self._addr_cache.popitem(last=False)
            # 写 (ip,) fallback 缓存
            target_pid = port_pid_map.get((client_ip, client_port))
            if target_pid:
                name = self._process_name(target_pid)
                with self._ip_cache_lock:
                    self._ip_cache[client_ip] = (now, target_pid, name)
                    # O(1) LRU 淘汰：OrderedDict.popitem(last=False) 移除最旧条目
                    while len(self._ip_cache) > self._ip_cache_max_size:
                        self._ip_cache.popitem(last=False)
        finally:
            event.set()
            with self._refresh_lock:
                if self._refresh_event is event:
                    self._refresh_event = None

    def list_processes(self) -> list[dict]:
        """List all processes (deduplicated).

        psutil.process_iter iterates 400+ processes taking 3-4 seconds; a 5-second
        cache avoids full iteration on every request. Actual iteration only happens
        on first call or when the cache expires.
        """
        import time as _time
        now = _time.time()
        with self._list_cache_lock:
            if self._list_cache and (now - self._list_cache_time) < self._list_cache_ttl:
                return self._list_cache
        # 缓存未命中或已过期，重新遍历
        result: dict[int, str] = {}
        try:
            for p in psutil.process_iter(['pid', 'name']):
                try:
                    info = p.info  # type: ignore[attr-defined]
                    pid = info.get('pid')
                    name = info.get('name')
                    if pid and name:
                        result[pid] = name
                except Exception:  # noqa: BLE001
                    continue
        except Exception:  # noqa: BLE001
            pass
        lst = [{"pid": pid, "name": name} for pid, name in sorted(result.items())]
        with self._list_cache_lock:
            self._list_cache = lst
            self._list_cache_time = now
        return lst
