"""通过 GetExtendedTcpTable 反查客户端连接的 PID + 进程名。

客户端连接代理时，客户端的源端口对应一个 PID。在 TCP 表中查找
local_addr=client_ip, local_port=client_port 的行，取其 OwningPid。

跨平台说明：GetExtendedTcpTable 是 Windows 专属 API，非 Windows 平台用 psutil
的 net_connections() 替代（已有 psutil 依赖，功能等价但精度略低）。
"""

import concurrent.futures
import socket
import sys
import threading
import time

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
    """返回 (local_ip, local_port, remote_ip, remote_port, pid, state) 列表。

    Windows：用 GetExtendedTcpTable（性能高，原生 API）。
    非 Windows：用 psutil.net_connections(kind="inet")（跨平台，state 字段无 Windows 状态码）。
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
    """返回 (local_ip, local_port, pid) 列表。

    UDP 是无连接的，UDP 表只有 local 端信息（无 remote_ip/remote_port）。
    匹配时按本机 IP+端口查找：出站包用 src，入站包用 dst。

    用 psutil 获取 UDP 连接表（比 ctypes GetExtendedUdpTable 更可靠，
    后者在 64 位系统上结构体偏移容易出错）。psutil 本身跨平台。
    带 1 秒 TTL 模块级缓存：同一秒内多个 worker 共享一次 psutil 扫描结果。
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
        self._addr_cache: dict[tuple[str, int], tuple[float, int | None, str | None]] = {}
        self._addr_cache_lock = threading.Lock()
        self._addr_cache_ttl = 3.0  # 3 秒 TTL（端口复用窗口短）
        # 性能优化：客户端 (ip,) → (pid, name) 的 fallback 缓存
        # 高并发短连接场景（每请求新源端口），(ip,port) 缓存永远 miss，
        # 但同一客户端进程的 PID 稳定，(ip,) 缓存可大幅降命中率
        self._ip_cache: dict[str, tuple[float, int | None, str | None]] = {}
        self._ip_cache_lock = threading.Lock()
        self._ip_cache_ttl = 30.0  # 30 秒 TTL（进程 PID 比端口稳定得多）
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
        """client_addr = (ip, port)，返回 (pid, process_name)。失败返回 (None, None)。

        single-flight 模式（兼顾性能 + 进程名显示）：
        1. (ip, port) → (pid, name) LRU 缓存（3 秒 TTL）
        2. (ip,) → pid 缓存（30 秒 TTL）：同一客户端进程的 PID 稳定
           注意：localhost（127.0.0.1/::1）跳过此缓存——本机多进程共享同 IP，
           用 ip_cache 会导致进程识别错误（如 chrome 的 PID 给了 curl）
        3. 缓存 miss 时触发后台扫描（只扫一次），并发连接等待 Event 完成
           - 扫描完成（~5ms）后 Event.set，所有等待连接立即查缓存
           - 超时（150ms）则返回 (None, None)，不阻塞代理主线程
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
        """触发后台 TCP 表扫描。已有扫描在跑则等待，避免重复扫描。"""
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
        """后台执行 TCP 表全量扫描 + 更新缓存。完成后 set Event。"""
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
                if len(self._addr_cache) > 1000:
                    items = sorted(self._addr_cache.items(), key=lambda x: x[1][0])
                    self._addr_cache = dict(items[-500:])
            # 写 (ip,) fallback 缓存
            target_pid = port_pid_map.get((client_ip, client_port))
            if target_pid:
                name = self._process_name(target_pid)
                with self._ip_cache_lock:
                    self._ip_cache[client_ip] = (now, target_pid, name)
                    if len(self._ip_cache) > 200:
                        items = sorted(self._ip_cache.items(), key=lambda x: x[1][0])
                        self._ip_cache = dict(items[-100:])
        finally:
            event.set()
            with self._refresh_lock:
                if self._refresh_event is event:
                    self._refresh_event = None

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
