"""Breakpoint management: manual global breakpoint + pause queue.

Proxy worker threads block waiting when request/response is intercepted by breakpoint, released by API layer.
Supports timeout (to avoid agent enabling breakpoint and forgetting to release causing permanent block) and pending wait duration statistics.
"""

import threading
import time


class BreakpointManager:
    """Breakpoint management: manual global breakpoint + pause queue."""

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
        """Set global breakpoint timeout (0=never timeout). Affects subsequent wait_for_release."""
        self._timeout = max(0.0, float(seconds))

    def get_timeout(self) -> float:
        return self._timeout

    def wait_for_release(self, flow_id: int, timeout: float | None = None) -> str:
        """Request/response intercepted by breakpoint, blocks waiting for user release, returns action (release/drop).

        Automatically releases after timeout (returns 'release'), to avoid agent forgetting to release causing permanent connection block.
        timeout parameter takes precedence over global _timeout; None means use global _timeout;
        global _timeout=0 means never timeout (legacy behavior, not recommended for async engine to avoid thread pool exhaustion).
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
                # 超时自动放行。超时判定与并发 release() 之间存在窄竞态窗口：
                # event.wait() 返回 False 后，release() 可能已设置 _actions[flow_id]。
                # 此处主动 pop 一次，避免 _actions[flow_id] 残留泄漏。
                self._actions.pop(flow_id, None)
                # 超时自动放行
                from .. import logger
                logger.warning("proxy",
                               f"Breakpoint flow_id={flow_id} waited {effective_timeout:.0f}s timeout, auto-released",
                               "Agent may have forgotten to release, auto-released to avoid connection stuck")
                return "release"
            return self._actions.pop(flow_id, "release")

    def release(self, flow_id: int, action: str = "release") -> bool:
        """User releases specified flow, returns whether the flow was successfully found."""
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
        """Breakpoint status: includes wait duration (seconds) for each pending flow."""
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
