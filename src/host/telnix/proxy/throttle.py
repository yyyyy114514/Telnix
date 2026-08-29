"""Weak network simulation (Throttle): simulates high latency, low bandwidth, packet loss, etc.

Design:
- Global switch + config stored in settings.json (throttle_enabled / throttle_latency_ms / throttle_bps_kbps)
- Call `delay()` at proxy forwarding to add RTT; call `send_throttled()` for rate-limited sending
- Only affects proxied traffic (HTTP/HTTPS); does not affect other local network traffic

Configuration items:
- throttle_enabled (0/1): whether enabled
- throttle_latency_ms (int): per-connection delay in milliseconds (simulates RTT)
- throttle_bps_kbps (int): rate limit KB/s (0 = unlimited)
- throttle_drop_pct (int): packet loss rate 0-100 (simulates weak network loss, only effective for tunneled traffic)

Performance optimizations:
1. Process-level config cache (_cfg_cache) + lazy loading; when disabled, a single bool check returns with zero overhead
2. On Windows, enable timeBeginPeriod(1) to raise clock precision to 1ms (default ~15ms),
   avoiding time.sleep(0.5ms) actually sleeping 15ms and causing the rate limit to be far below the configured value
"""
import random
import socket
import sys
import time

from .. import settings_store


# ---------- Windows 时钟精度优化 ----------
# Windows 默认时钟粒度 ~15ms，time.sleep 精度差。
# 启用 throttle 时调 timeBeginPeriod(1) 提高到 1ms，避免限速偏差 30 倍。
_WIN_TIMER_SET = False

def _ensure_win_timer_precision():
    """Raise clock precision to 1ms on Windows (only called when throttle is enabled)."""
    global _WIN_TIMER_SET
    if sys.platform != "win32" or _WIN_TIMER_SET:
        return
    try:
        import ctypes
        winmm = ctypes.WinDLL('winmm')
        winmm.timeBeginPeriod(1)
        _WIN_TIMER_SET = True
    except Exception:  # noqa: BLE001
        pass


# ---------- 配置缓存 ----------
# 避免每次 is_enabled/delay/send_throttled 都调 4 次 get_setting 读文件
# 通过 settings_store 的 mtime 缓存间接失效（store 写入会更新 mtime）
# 注：无需加锁——_cfg_cache 写入是原子的（GIL），settings_store.get_setting 内部已线程安全，
# 最多多读一次配置无副作用（最终一致）。
_cfg_cache: dict = {"enabled": False, "latency_ms": 0, "bps_kbps": 0, "drop_pct": 0}
_cfg_loaded: bool = False


def _refresh_config() -> None:
    """Read all weak-network config from settings_store and cache it in this module.

    settings_store already has an mtime cache internally; we cache another copy here
    to avoid repeated dict.get calls.
    """
    global _cfg_cache, _cfg_loaded
    # settings_store.get_setting 内部走 mtime 缓存，文件未变时直接返回内存 dict
    enabled = settings_store.get_setting("throttle_enabled", "0") == "1"
    cfg = {
        "enabled": enabled,
        "latency_ms": int(settings_store.get_setting("throttle_latency_ms", "0") or "0"),
        "bps_kbps": int(settings_store.get_setting("throttle_bps_kbps", "0") or "0"),
        "drop_pct": int(settings_store.get_setting("throttle_drop_pct", "0") or "0"),
    }
    _cfg_cache = cfg
    _cfg_loaded = True
    # 启用时提高 Windows 时钟精度
    if enabled:
        _ensure_win_timer_precision()


def _get_cfg() -> dict:
    """Get current config (with lazy loading)."""
    if not _cfg_loaded:
        _refresh_config()
    return _cfg_cache


def invalidate_cache() -> None:
    """Invalidate this module's config cache. Called by callers after settings change."""
    global _cfg_loaded
    _cfg_loaded = False


def is_enabled() -> bool:
    """Whether weak network simulation is enabled.

    Performance: uses _get_cfg cache to avoid get_setting calls every time.
    """
    return _get_cfg()["enabled"]


def get_config() -> dict:
    """Read weak network config."""
    _refresh_config()
    return dict(_cfg_cache)


def delay() -> None:
    """Called at each connection startup: simulates RTT latency.

    Performance: when disabled, a single _get_cfg returns with no overhead.
    """
    cfg = _get_cfg()
    if not cfg["enabled"]:
        return
    ms = cfg["latency_ms"]
    if ms > 0:
        time.sleep(ms / 1000.0)


def should_drop() -> bool:
    """Whether to drop the packet (by drop_pct probability). Called before each tunneled packet.

    Performance: when disabled, a single _get_cfg returns False with no overhead.
    """
    cfg = _get_cfg()
    if not cfg["enabled"]:
        return False
    pct = cfg["drop_pct"]
    if pct <= 0:
        return False
    return random.randint(1, 100) <= pct


def send_throttled(sock: socket.socket, data: bytes) -> None:
    """Rate-limited send: send in chunks according to throttle_bps_kbps, sleeping between chunks.

    When disabled or bps_kbps=0, sends directly with sendall, no overhead.

    Performance optimizations:
    1. Goes through _get_cfg cache, avoiding 2 get_setting calls each time
    2. On Windows, timeBeginPeriod(1) has already raised clock precision; sleep is accurate to 1ms
    """
    cfg = _get_cfg()
    if not cfg["enabled"]:
        sock.sendall(data)
        return
    bps_kbps = cfg["bps_kbps"]
    if bps_kbps <= 0:
        sock.sendall(data)
        return
    # 字节/秒
    bps = bps_kbps * 1024
    # 性能优化：动态 chunk_size，让每片发送时间约 20ms（避免低带宽时 sleep 精度过低）
    # 同时保证 chunk_size 至少 512 字节，减少 syscall 次数
    target_slice_sec = 0.02
    chunk_size = max(512, int(bps * target_slice_sec))
    per_chunk_sec = chunk_size / bps
    total = len(data)
    for i in range(0, total, chunk_size):
        chunk = data[i:i + chunk_size]
        sock.sendall(chunk)
        # 最后一片不用 sleep
        if i + chunk_size < total:
            time.sleep(per_chunk_sec)
