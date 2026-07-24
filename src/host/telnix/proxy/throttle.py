"""弱网模拟（Throttle）：模拟高延迟、低带宽、丢包等网络环境。

设计：
- 全局开关 + 配置存在 settings.json（throttle_enabled / throttle_latency_ms / throttle_bps_kbps）
- 在代理转发处调用 `delay()` 加 RTT，调用 `send_throttled()` 限速发送
- 仅作用于代理转发的流量（HTTP/HTTPS），不影响本机其他网络

配置项：
- throttle_enabled (0/1)：是否启用
- throttle_latency_ms (int)：每连接延迟毫秒数（模拟 RTT）
- throttle_bps_kbps (int)：限速 KB/s（0 = 不限速）
- throttle_drop_pct (int)：丢包率 0-100（模拟弱网丢包，仅对隧道流量生效）

性能优化：
1. 进程级配置缓存（_cfg_cache）+ 懒加载，未启用时单次 bool 检查即返回，零开销
2. Windows 上启用 timeBeginPeriod(1) 提高时钟精度到 1ms（默认 ~15ms），
   避免 time.sleep(0.5ms) 实际睡 15ms 导致限速远低于配置值
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
    """Windows 上提高时钟精度到 1ms（仅启用 throttle 时调用）。"""
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
_cfg_lock = None  # 延迟初始化，避免 import 时创建锁
_cfg_cache: dict = {"enabled": False, "latency_ms": 0, "bps_kbps": 0, "drop_pct": 0}
_cfg_loaded: bool = False


def _refresh_config() -> None:
    """从 settings_store 读全部弱网配置，缓存到本模块。

    settings_store 内部已有 mtime 缓存，这里再缓存一份避免重复 dict.get 调用。
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
    """获取当前配置（带懒加载）。"""
    if not _cfg_loaded:
        _refresh_config()
    return _cfg_cache


def invalidate_cache() -> None:
    """失效本模块配置缓存。设置变更后由调用方调用。"""
    global _cfg_loaded
    _cfg_loaded = False


def is_enabled() -> bool:
    """弱网模拟是否启用。

    性能优化：用 _get_cfg 走缓存，避免每次 get_setting 调用。
    """
    return _get_cfg()["enabled"]


def get_config() -> dict:
    """读取弱网配置。"""
    _refresh_config()
    return dict(_cfg_cache)


def delay() -> None:
    """每连接启动时调用：模拟 RTT 延迟。

    性能优化：未启用时单次 _get_cfg 即返回，无开销。
    """
    cfg = _get_cfg()
    if not cfg["enabled"]:
        return
    ms = cfg["latency_ms"]
    if ms > 0:
        time.sleep(ms / 1000.0)


def should_drop() -> bool:
    """是否丢包（按 drop_pct 概率）。在隧道转发每包前调用。

    性能优化：未启用时单次 _get_cfg 即返回 False，无开销。
    """
    cfg = _get_cfg()
    if not cfg["enabled"]:
        return False
    pct = cfg["drop_pct"]
    if pct <= 0:
        return False
    return random.randint(1, 100) <= pct


def send_throttled(sock: socket.socket, data: bytes) -> None:
    """限速发送：按 throttle_bps_kbps 分片发送，每片之间 sleep。

    未启用或 bps_kbps=0 时直接 sendall，无开销。

    性能优化：
    1. 走 _get_cfg 缓存，避免每次 2 次 get_setting 调用
    2. Windows 上已通过 timeBeginPeriod(1) 提高时钟精度，sleep 准确到 1ms
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
    # 每片 4KB，按带宽算 sleep 时间
    chunk_size = 4096
    per_chunk_sec = chunk_size / bps
    for i in range(0, len(data), chunk_size):
        chunk = data[i:i + chunk_size]
        sock.sendall(chunk)
        # 最后一片不用 sleep
        if i + chunk_size < len(data):
            time.sleep(per_chunk_sec)
