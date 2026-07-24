"""断点管理：手动全局断点 + 暂停队列。

代理工作线程在请求/响应被断点拦截时阻塞等待，由 API 层放行。
支持超时（避免 agent 开了断点忘了放行导致永远阻塞）和 pending 等待时长统计。
"""

import threading
import time


class BreakpointManager:
    """断点管理：手动全局断点 + 暂停队列。"""

    def __init__(self):
        self.break_on_request = False  # 全局请求断点
        self.break_on_response = False  # 全局响应断点
        self._pending: dict[int, threading.Event] = {}
        self._actions: dict[int, str] = {}  # flow_id -> 'release' | 'drop'
        self._timestamps: dict[int, float] = {}  # flow_id -> 入队时间戳
        self._timeout: float = 0.0  # 全局断点超时秒数（0=永不超时）
        self._lock = threading.Lock()

    def should_break_request(self) -> bool:
        return self.break_on_request

    def should_break_response(self) -> bool:
        return self.break_on_response

    def set_request(self, enabled: bool, timeout: float = 0.0):
        self.break_on_request = bool(enabled)
        if timeout is not None:
            self._timeout = max(0.0, float(timeout))

    def set_response(self, enabled: bool, timeout: float = 0.0):
        self.break_on_response = bool(enabled)
        if timeout is not None:
            self._timeout = max(0.0, float(timeout))

    def set_timeout(self, seconds: float):
        """设置全局断点超时（0=永不超时）。影响后续 wait_for_release。"""
        self._timeout = max(0.0, float(seconds))

    def get_timeout(self) -> float:
        return self._timeout

    def wait_for_release(self, flow_id: int, timeout: float | None = None) -> str:
        """请求/响应被断点拦截，阻塞等待用户放行，返回动作（release/drop）。

        超时后自动放行（返回 'release'），避免 agent 忘了 release 导致连接永久阻塞。
        timeout 参数优先于全局 _timeout；None 表示用全局 _timeout；
        全局 _timeout=0 表示永不超时（旧行为，不推荐用于 async 引擎以免线程池耗尽）。
        """
        event = threading.Event()
        now = time.time()
        with self._lock:
            self._pending[flow_id] = event
            self._timestamps[flow_id] = now
            # 显式 timeout 优先，否则用全局 _timeout
            effective_timeout = timeout if timeout is not None else self._timeout
        # 超时为 0 表示永不超时（保持旧行为）；>0 则到点自动放行
        triggered = event.wait(timeout=effective_timeout if effective_timeout > 0 else None)
        with self._lock:
            self._pending.pop(flow_id, None)
            self._timestamps.pop(flow_id, None)
            if not triggered:
                # 超时自动放行
                from .. import logger
                logger.warning("proxy",
                               f"断点 flow_id={flow_id} 等待 {effective_timeout:.0f}s 超时，自动放行",
                               "agent 可能忘了 release，已自动放行避免连接卡死")
                return "release"
            return self._actions.pop(flow_id, "release")

    def release(self, flow_id: int, action: str = "release") -> bool:
        """用户放行指定 flow，返回是否成功找到该 flow。"""
        with self._lock:
            event = self._pending.get(flow_id)
            if event is None:
                return False
            self._actions[flow_id] = action
            self._timestamps.pop(flow_id, None)
            event.set()
            return True

    def pending_flow_ids(self) -> list[int]:
        with self._lock:
            return list(self._pending.keys())

    def status(self) -> dict:
        """断点状态：含每个 pending flow 的等待时长（秒）。"""
        now = time.time()
        with self._lock:
            pending = []
            for fid, ts in self._timestamps.items():
                pending.append({"flow_id": fid, "waiting_seconds": round(now - ts, 1)})
        return {
            "break_on_request": self.break_on_request,
            "break_on_response": self.break_on_response,
            "timeout_seconds": self._timeout,
            "pending": pending,
        }
