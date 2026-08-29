"""实时规则命中追踪模块。

使用 threading.local 存储进程内实时命中记录，支持：
- 规则执行耗时热力图统计
- 最近命中时间线（最近 100 次）
- 排行榜 TOP 10
- SSE 实时推送

Usage:
    from .hit_tracker import hit_tracker, record_hit, get_stats, clear_stats

    # 规则命中时记录
    record_hit("rule_id", "规则名称", 12.5, flow_id=123)

    # 获取统计数据
    stats = get_stats()
"""

import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class HitRecord:
    """单次命中记录。"""
    ts: float  # Unix 时间戳
    rule_id: str
    rule_name: str
    duration_ms: float
    flow_id: Optional[int] = None


@dataclass
class RuleStats:
    """单个规则的统计信息。"""
    rule_id: str
    rule_name: str
    hit_count: int = 0
    total_duration_ms: float = 0.0
    min_duration_ms: float = float('inf')
    max_duration_ms: float = 0.0
    last_hit_at: str = ""

    @property
    def avg_duration_ms(self) -> float:
        return self.total_duration_ms / self.hit_count if self.hit_count > 0 else 0.0


class HitTracker:
    """规则命中追踪器（线程安全）。"""

    def __init__(self, max_timeline: int = 100):
        self._lock = threading.Lock()
        self._max_timeline = max_timeline
        # 规则统计: rule_id -> RuleStats
        self._rule_stats: dict[str, RuleStats] = {}
        # 时间线: 按时间倒序的最近 N 次命中
        self._timeline: list[HitRecord] = []

    def record_hit(
        self,
        rule_id: str,
        rule_name: str,
        duration_ms: float,
        flow_id: Optional[int] = None,
    ) -> None:
        """记录一次规则命中。"""
        now = time.time()
        now_iso = datetime.fromtimestamp(now).isoformat()

        record = HitRecord(
            ts=now,
            rule_id=rule_id,
            rule_name=rule_name,
            duration_ms=duration_ms,
            flow_id=flow_id,
        )

        with self._lock:
            # 更新规则统计
            if rule_id not in self._rule_stats:
                self._rule_stats[rule_id] = RuleStats(
                    rule_id=rule_id,
                    rule_name=rule_name,
                )

            stats = self._rule_stats[rule_id]
            stats.hit_count += 1
            stats.total_duration_ms += duration_ms
            stats.min_duration_ms = min(stats.min_duration_ms, duration_ms)
            stats.max_duration_ms = max(stats.max_duration_ms, duration_ms)
            stats.last_hit_at = now_iso

            # 更新时间线
            self._timeline.insert(0, record)
            if len(self._timeline) > self._max_timeline:
                self._timeline = self._timeline[: self._max_timeline]

    def get_stats(self) -> dict:
        """获取完整统计数据。"""
        with self._lock:
            # 热力图数据
            heatmap = []
            for rule_id, stats in self._rule_stats.items():
                heatmap.append({
                    "rule_id": stats.rule_id,
                    "rule_name": stats.rule_name,
                    "hit_count": stats.hit_count,
                    "avg_ms": round(stats.avg_duration_ms, 2),
                    "min_ms": round(stats.min_duration_ms, 2) if stats.min_duration_ms != float('inf') else 0,
                    "max_ms": round(stats.max_duration_ms, 2),
                })

            # 时间线数据
            timeline = []
            for record in self._timeline:
                timeline.append({
                    "ts": datetime.fromtimestamp(record.ts).isoformat() + "Z",
                    "rule_id": record.rule_id,
                    "rule_name": record.rule_name,
                    "duration_ms": round(record.duration_ms, 2),
                    "flow_id": record.flow_id,
                })

            # 排行榜 TOP 10
            leaderboard = sorted(
                self._rule_stats.values(),
                key=lambda x: x.hit_count,
                reverse=True,
            )[:10]

            # 总命中次数
            total_hits = sum(s.hit_count for s in self._rule_stats.values())

            return {
                "total_hits": total_hits,
                "heatmap": heatmap,
                "timeline": timeline,
                "leaderboard": [
                    {
                        "rule_id": s.rule_id,
                        "rule_name": s.rule_name,
                        "hit_count": s.hit_count,
                        "avg_ms": round(s.avg_duration_ms, 2),
                        "last_hit_at": s.last_hit_at,
                    }
                    for s in leaderboard
                ],
            }

    def get_timeline(self) -> list[dict]:
        """获取最近命中时间线（用于 SSE 推送增量）。"""
        with self._lock:
            return [
                {
                    "ts": datetime.fromtimestamp(r.ts).isoformat() + "Z",
                    "rule_id": r.rule_id,
                    "rule_name": r.rule_name,
                    "duration_ms": round(r.duration_ms, 2),
                    "flow_id": r.flow_id,
                }
                for r in self._timeline
            ]

    def clear(self) -> None:
        """清除所有统计数据。"""
        with self._lock:
            self._rule_stats.clear()
            self._timeline.clear()


# 全局单例
hit_tracker = HitTracker()


def record_hit(
    rule_id: str,
    rule_name: str,
    duration_ms: float,
    flow_id: Optional[int] = None,
) -> None:
    """全局记录规则命中。"""
    hit_tracker.record_hit(rule_id, rule_name, duration_ms, flow_id)


def get_stats() -> dict:
    """获取完整统计数据。"""
    return hit_tracker.get_stats()


def get_timeline() -> list[dict]:
    """获取最近命中时间线。"""
    return hit_tracker.get_timeline()


def clear_stats() -> None:
    """清除所有统计数据。"""
    hit_tracker.clear()
