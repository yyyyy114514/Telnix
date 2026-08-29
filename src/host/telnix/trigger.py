"""触发式捕获（Trigger Capture）状态管理。

当 trigger 启用时，只有匹配触发条件的 flow 才会被记录，且一旦触发，
后续所有 flow 都会被记录（直到手动停止或 trigger 失效）。

触发条件 DSL 复用 flowfilter 语法（简化版），支持：
- host=example.com        host 包含匹配
- status=500              状态码精确匹配
- method=POST             方法匹配
- path=/api               路径包含匹配
- process=chrome          进程名包含匹配

多个条件用 & 或 AND 连接（全部满足才触发）。
"""

import threading
from typing import Any


class TriggerManager:
    """触发式捕获状态管理器（线程安全）。条件通过 DB 持久化。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._conditions: list[dict[str, str]] = []
        self._enabled = False
        self._triggered = False
        # 从 DB 恢复上次保存的条件（不论 enabled 状态）
        try:
            import telnix.db as _db
            saved = _db.get_trigger_config()
            if saved:
                self._conditions = saved
                self._enabled = bool(saved)
        except Exception:  # noqa: BLE001
            pass

    @property
    def enabled(self) -> bool:
        with self._lock:
            return self._enabled

    @property
    def triggered(self) -> bool:
        with self._lock:
            return self._triggered

    def configure(self, conditions: list[dict[str, str]] | None) -> None:
        """配置触发条件（持久化到 DB，不论 enabled 状态）。"""
        with self._lock:
            self._conditions = conditions or []
            # 始终持久化到 DB（即使条件为空，也保存空列表用于清除）
            try:
                import telnix.db as _db
                _db.set_trigger_config(self._conditions)
            except Exception:  # noqa: BLE001
                pass
            self._enabled = bool(self._conditions)
            self._triggered = False

    def reset(self) -> None:
        """重置触发状态（停止抓包时调用）。"""
        with self._lock:
            self._triggered = False

    def clear(self) -> None:
        """清除触发配置。"""
        with self._lock:
            self._conditions = []
            self._enabled = False
            self._triggered = False

    def get_state(self) -> dict:
        """获取当前状态（用于 API 返回）。"""
        with self._lock:
            return {
                "enabled": self._enabled,
                "triggered": self._triggered,
                "conditions": list(self._conditions),
            }

    def should_record(self, flow: dict) -> bool:
        """判断该 flow 是否应被记录。

        - 触发式捕获未启用：返回 True（正常记录）
        - 已触发：返回 True（触发后记录所有）
        - 未触发：检查是否匹配条件，匹配则触发并返回 True，否则返回 False
        """
        with self._lock:
            if not self._enabled:
                return True
            if self._triggered:
                return True
            conditions = list(self._conditions)

        # 检查是否匹配所有条件
        matched = self._match_flow(flow, conditions)
        if matched:
            with self._lock:
                self._triggered = True
            return True
        return False

    def _match_flow(self, flow: dict, conditions: list[dict[str, str]]) -> bool:
        """检查 flow 是否匹配所有触发条件。

        对 status 字段，如果 flow 中 status_code 为 None（请求阶段），
        跳过该条件（放行到响应阶段再检查）。
        """
        for cond in conditions:
            field = cond.get("field", "")
            value = cond.get("value", "")
            if not field or not value:
                continue
            # 请求阶段没有 status_code，跳过 status 条件
            if field == "status" and flow.get("status_code") is None:
                continue
            if not self._match_field(flow, field, value):
                return False
        return True

    def _match_field(self, flow: dict, field: str, value: str) -> bool:
        """单字段匹配。"""
        fv = ""
        if field == "host":
            fv = flow.get("host", "") or ""
            return value.lower() in fv.lower()
        elif field == "method":
            fv = flow.get("method", "") or ""
            return fv.upper() == value.upper()
        elif field == "status":
            sc = flow.get("status_code")
            if sc is None:
                return False
            # 支持 status>=500 语法
            if value.startswith(">="):
                try:
                    return int(sc) >= int(value[2:])
                except ValueError:
                    return False
            if value.startswith("<="):
                try:
                    return int(sc) <= int(value[2:])
                except ValueError:
                    return False
            try:
                return int(sc) == int(value)
            except ValueError:
                return False
        elif field == "path":
            fv = flow.get("path", "") or ""
            return value.lower() in fv.lower()
        elif field == "process":
            fv = flow.get("process_name", "") or ""
            return value.lower() in fv.lower()
        elif field == "url":
            fv = flow.get("url", "") or ""
            return value.lower() in fv.lower()
        elif field == "protocol":
            fv = flow.get("protocol", "") or ""
            return fv.lower() == value.lower()
        return False


# 全局单例
_trigger_manager = TriggerManager()


def get_trigger_manager() -> TriggerManager:
    return _trigger_manager


def parse_trigger_dsl(dsl: str) -> list[dict[str, str]]:
    """解析触发条件 DSL 字符串为条件列表。

    支持语法：
    - "host=example.com"
    - "host=example.com & status=500"
    - "host=example.com AND method=POST"
    - "status>=500 & host=example.com"

    返回 [{"field": "host", "value": "example.com"}, ...]
    status 字段的 value 可以带运算符前缀：>=500 / <=500 / 500
    """
    if not dsl or not dsl.strip():
        return []
    conditions = []
    # 用 & 或 AND 分割（不区分大小写）
    parts = dsl.replace(" AND ", " & ").replace(" and ", " & ").split("&")
    for part in parts:
        part = part.strip()
        if not part:
            continue
        # 优先匹配 >= 和 <= 运算符，避免被 = 分割错误
        import re
        # 运算符：>= / <= / ~（正则）优先
        m = re.match(r'^(\w+)\s*(>=|<=|~)\s*(.+)$', part)
        if m:
            field = m.group(1).strip().lower()
            op = m.group(2)
            value = op + m.group(3).strip()
        elif "=" in part:
            field, _, value = part.partition("=")
            field = field.strip().lower()
            value = value.strip()
        else:
            continue
        if field and value:
            conditions.append({"field": field, "value": value})
    return conditions
