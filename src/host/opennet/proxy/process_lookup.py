"""通过 GetExtendedTcpTable 反查客户端连接的 PID + 进程名。

客户端连接代理时，客户端的源端口对应一个 PID。在 TCP 表中查找
local_addr=client_ip, local_port=client_port 的行，取其 OwningPid。
"""

import ctypes
import socket
import struct
import threading
import time

import psutil

# GetExtendedTcpTable 参数
TCP_TABLE_OWNER_PID_ALL = 5
AF_INET = 2


class _MIB_TCPROW_OWNER_PID(ctypes.Structure):
    _fields_ = [
        ("dwState", ctypes.c_ulong),
        ("dwLocalAddr", ctypes.c_ulong),
        ("dwLocalPort", ctypes.c_ulong),
        ("dwRemoteAddr", ctypes.c_ulong),
        ("dwRemotePort", ctypes.c_ulong),
        ("dwOwningPid", ctypes.c_ulong),
    ]


class _MIB_TCPTABLE_OWNER_PID(ctypes.Structure):
    _fields_ = [
        ("dwNumEntries", ctypes.c_ulong),
        ("table", _MIB_TCPROW_OWNER_PID * 1),
    ]


_phlpapi = ctypes.windll.iphlpapi


def _list_tcp_owner_rows() -> list[tuple[str, int, str, int, int, int]]:
    """返回 (local_ip, local_port, remote_ip, remote_port, pid, state) 列表。"""
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
    """返回 (local_ip, local_port, pid) 列表。

    UDP 是无连接的，UDP 表只有 local 端信息（无 remote_ip/remote_port）。
    匹配时按本机 IP+端口查找：出站包用 src，入站包用 dst。

    用 psutil 获取 UDP 连接表（比 ctypes GetExtendedUdpTable 更可靠，
    后者在 64 位系统上结构体偏移容易出错）。
    """
    rows: list[tuple[str, int, int]] = []
    try:
        for c in psutil.net_connections(kind="udp"):
            if c.laddr:
                rows.append((c.laddr.ip, c.laddr.port, c.pid or 0))
    except Exception:  # noqa: BLE001
        pass
    return rows


class ProcessLookup:
    """通过 GetExtendedTcpTable 反查 PID + 进程名。带缓存。"""

    def __init__(self):
        self._pid_cache: dict[int, str] = {}
        self._lock = threading.Lock()
        # 进程列表缓存（避免每次都遍历 psutil.process_iter，耗时 3-4 秒）
        self._list_cache: list[dict] = []
        self._list_cache_time: float = 0.0
        self._list_cache_lock = threading.Lock()
        self._list_cache_ttl = 5.0  # 缓存 5 秒
        # 性能优化：客户端 (ip, port) → (pid, name) 的 LRU 缓存
        # 短时间同端口的多次连接复用结果，避免每次都全表扫描 + psutil 调用
        self._addr_cache: dict[tuple[str, int], tuple[int | None, str | None]] = {}
        self._addr_cache_lock = threading.Lock()
        self._addr_cache_ttl = 3.0  # 3 秒 TTL（端口复用窗口短）
        # 性能优化：客户端 (ip,) → (pid, name) 的 fallback 缓存
        # 高并发短连接场景（每请求新源端口），(ip,port) 缓存永远 miss，
        # 但同一客户端进程的 PID 稳定，(ip,) 缓存可大幅降命中率
        self._ip_cache: dict[str, tuple[float, int | None, str | None]] = {}
        self._ip_cache_lock = threading.Lock()
        self._ip_cache_ttl = 30.0  # 30 秒 TTL（进程 PID 比端口稳定得多）
        # 性能优化：lookup 锁，防止高并发下多线程同时全表扫描 TCP 表
        # 同一时间只允许一个线程做 _list_tcp_owner_rows()，其他线程等结果走缓存
        self._lookup_lock = threading.Lock()

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

    def lookup(self, client_addr) -> tuple[int | None, str | None]:
        """client_addr = (ip, port)，返回 (pid, process_name)。失败返回 (None, None)。

        性能优化：
        1. (ip, port) → (pid, name) LRU 缓存（3 秒 TTL），同端口短时间复用
        2. (ip,) → pid 短缓存（30 秒 TTL）：高并发短连接场景（每请求新源端口），
           (ip,port) 缓存永远 miss，但同一客户端进程的 PID 稳定，(ip,) 缓存可大幅降命中率
        3. lookup 锁 + 双检锁：高并发下只允许一个线程全表扫描 TCP 表，
           其他线程等锁释放后走缓存，避免 50 线程同时调用 _list_tcp_owner_rows()
        4. 一次性构建 {(ip, port): pid} 字典（O(n) 一次构建，O(1) 查询）
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
        # 1b. 查 (ip,) fallback 缓存（高并发短连接场景的救命缓存）
        with self._ip_cache_lock:
            ip_cached = self._ip_cache.get(client_ip)
            if ip_cached is not None:
                ip_ts, ip_pid, ip_name = ip_cached
                if now - ip_ts < self._ip_cache_ttl:
                    # (ip,) 命中：写回 (ip, port) 缓存，后续同端口命中走精确缓存
                    with self._addr_cache_lock:
                        self._addr_cache[addr_key] = (now, ip_pid, ip_name)
                    return ip_pid, ip_name
        # 2. lookup 锁 + 双检锁：高并发下只允许一个线程全表扫描
        # 第一个线程拿锁扫描 + 写缓存；其他线程等锁释放后查缓存命中返回
        with self._lookup_lock:
            # 双检锁：拿锁后再查一次缓存（可能已被前一个线程写入）
            now = time.time()
            with self._addr_cache_lock:
                cached = self._addr_cache.get(addr_key)
                if cached is not None:
                    ts, pid, name = cached
                    if now - ts < self._addr_cache_ttl:
                        return pid, name
            with self._ip_cache_lock:
                ip_cached = self._ip_cache.get(client_ip)
                if ip_cached is not None:
                    ip_ts, ip_pid, ip_name = ip_cached
                    if now - ip_ts < self._ip_cache_ttl:
                        with self._addr_cache_lock:
                            self._addr_cache[addr_key] = (now, ip_pid, ip_name)
                        return ip_pid, ip_name
            # 3. 真正全表扫描（只一个线程执行）
            try:
                rows = _list_tcp_owner_rows()
            except Exception:  # noqa: BLE001
                return None, None
            port_pid_map: dict[tuple[str, int], int] = {}
            same_ip_pids: list[int] = []
            for (lip, lport, _rip, _rport, pid, _state) in rows:
                port_pid_map[(lip, lport)] = pid
                if lip == client_ip:
                    same_ip_pids.append(pid)
            pid = port_pid_map.get(addr_key)
            if pid is None:
                # 端口可能还在 TIME_WAIT 或刚建立，给一次重试机会但不 sleep
                try:
                    rows = _list_tcp_owner_rows()
                except Exception:  # noqa: BLE001
                    return None, None
                for (lip, lport, _rip, _rport, pid2, _state) in rows:
                    if lip == client_ip and lport == client_port:
                        pid = pid2
                        break
            if pid is None:
                with self._addr_cache_lock:
                    self._addr_cache[addr_key] = (now, None, None)
                return None, None
            name = self._process_name(pid)
            with self._addr_cache_lock:
                self._addr_cache[addr_key] = (now, pid, name)
                if len(self._addr_cache) > 1000:
                    items = sorted(self._addr_cache.items(), key=lambda x: x[1][0])
                    self._addr_cache = dict(items[-500:])
            # 写 (ip,) 缓存：仅当同 ip 的所有连接都属于同一个 pid 时才写
            unique_pids = set(same_ip_pids) if same_ip_pids else {pid}
            if len(unique_pids) == 1:
                with self._ip_cache_lock:
                    self._ip_cache[client_ip] = (now, pid, name)
                    if len(self._ip_cache) > 200:
                        items = sorted(self._ip_cache.items(), key=lambda x: x[1][0])
                        self._ip_cache = dict(items[-100:])
            return pid, name

    def list_processes(self) -> list[dict]:
        """列出所有进程（去重）。

        psutil.process_iter 遍历 400+ 进程耗时 3-4 秒，加 5 秒缓存避免
        每次请求都全量遍历。首次调用或缓存过期时才真正遍历。
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
