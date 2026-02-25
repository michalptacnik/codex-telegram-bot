"""Mission observability dashboard (EPIC 10, issue #95).

Provides a MetricsCollector that accumulates counters and latencies from
mission execution, plus a DashboardSnapshot that represents a point-in-time
view suitable for rendering in a Telegram message, a web endpoint, or a CLI.

Key metrics tracked:
  - Mission states (pending / running / completed / failed / paused)
  - Throughput (missions started and completed in sliding windows)
  - Error rate (failed / (completed + failed))
  - Step latency (mean, p95)
  - Queue depth (pending missions)
  - Recent failures with drill-down detail

Usage::

    collector = MetricsCollector(store=store)
    snapshot = collector.snapshot()
    print(snapshot.format_text())
"""
from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from statistics import mean, quantiles
from typing import Deque, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_WINDOW_MINUTES = 60   # sliding window for rate metrics


@dataclass
class FailureDetail:
    mission_id: str
    title: str
    failed_at: datetime
    error_hint: str    # last error message or empty string


@dataclass
class DashboardSnapshot:
    """Point-in-time observability snapshot."""
    captured_at: datetime

    # State counters
    pending: int
    running: int
    completed: int
    failed: int
    paused: int

    # Throughput (within window)
    window_minutes: int
    started_in_window: int
    completed_in_window: int
    failed_in_window: int

    # Rates
    error_rate: float           # 0.0–1.0
    throughput_per_hour: float  # completed missions / hour

    # Latency (seconds)
    step_latency_mean_sec: float
    step_latency_p95_sec: float

    # Queue depth
    queue_depth: int

    # Failures drill-down
    recent_failures: List[FailureDetail]

    def format_text(self) -> str:
        """Format as a plain-text dashboard for Telegram / CLI."""
        lines = [
            f"Mission Dashboard  [{self.captured_at.strftime('%H:%M:%S UTC')}]",
            "─" * 40,
            f"  Pending  : {self.pending:4d}  Running  : {self.running:4d}",
            f"  Done     : {self.completed:4d}  Failed   : {self.failed:4d}",
            f"  Paused   : {self.paused:4d}",
            "",
            f"  Window ({self.window_minutes}m)",
            f"    Started   : {self.started_in_window}",
            f"    Completed : {self.completed_in_window}",
            f"    Failed    : {self.failed_in_window}",
            f"    Error rate: {self.error_rate*100:.1f}%",
            f"    Throughput: {self.throughput_per_hour:.1f}/h",
            "",
            f"  Step latency  mean={self.step_latency_mean_sec:.2f}s  "
            f"p95={self.step_latency_p95_sec:.2f}s",
        ]
        if self.recent_failures:
            lines += ["", "  Recent failures:"]
            for f in self.recent_failures[:5]:
                ts = f.failed_at.strftime("%H:%M")
                lines.append(f"    [{ts}] {f.title[:30]} — {f.error_hint[:40]}")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "captured_at": self.captured_at.isoformat(),
            "pending": self.pending,
            "running": self.running,
            "completed": self.completed,
            "failed": self.failed,
            "paused": self.paused,
            "window_minutes": self.window_minutes,
            "started_in_window": self.started_in_window,
            "completed_in_window": self.completed_in_window,
            "failed_in_window": self.failed_in_window,
            "error_rate": self.error_rate,
            "throughput_per_hour": self.throughput_per_hour,
            "step_latency_mean_sec": self.step_latency_mean_sec,
            "step_latency_p95_sec": self.step_latency_p95_sec,
            "queue_depth": self.queue_depth,
            "recent_failures": [
                {
                    "mission_id": f.mission_id,
                    "title": f.title,
                    "failed_at": f.failed_at.isoformat(),
                    "error_hint": f.error_hint,
                }
                for f in self.recent_failures
            ],
        }


class MetricsCollector:
    """Collect and aggregate mission metrics from the store.

    The collector is stateless between snapshots – it queries the store each
    time.  For high-frequency dashboards you can cache the snapshot for a few
    seconds.
    """

    def __init__(
        self,
        store: "SqliteRunStore",  # type: ignore[name-defined]
        window_minutes: int = _WINDOW_MINUTES,
        max_recent_failures: int = 10,
    ) -> None:
        self._store = store
        self._window = window_minutes
        self._max_failures = max_recent_failures
        # In-process ring buffer for step latencies (seconds)
        self._step_latencies: Deque[float] = deque(maxlen=1000)

    def record_step_latency(self, seconds: float) -> None:
        """Call from MissionRunner after each step completes."""
        self._step_latencies.append(seconds)

    def snapshot(self) -> DashboardSnapshot:
        """Compute and return a fresh DashboardSnapshot."""
        from codex_telegram_bot.domain.missions import (
            MISSION_STATE_IDLE, MISSION_STATE_RUNNING,
            MISSION_STATE_COMPLETED, MISSION_STATE_FAILED, MISSION_STATE_PAUSED,
        )
        now = datetime.now(timezone.utc)
        window_start = now - timedelta(minutes=self._window)

        # State counts
        def _count(state: str) -> int:
            return len(self._store.list_missions(state=state))

        pending   = _count(MISSION_STATE_IDLE)
        running   = _count(MISSION_STATE_RUNNING)
        completed = _count(MISSION_STATE_COMPLETED)
        failed    = _count(MISSION_STATE_FAILED)
        paused    = _count(MISSION_STATE_PAUSED)

        # Windowed throughput via events table
        started_in_w   = self._store.count_mission_events_since(
            "idle\u2192running", window_start.isoformat()
        )
        completed_in_w = self._store.count_mission_events_since(
            "running→completed", window_start.isoformat()
        )
        failed_in_w    = self._store.count_mission_events_since(
            "running→failed", window_start.isoformat()
        )

        total_terminal = completed_in_w + failed_in_w
        error_rate = (failed_in_w / total_terminal) if total_terminal > 0 else 0.0
        hours = max(self._window / 60.0, 1 / 3600)
        throughput_per_hour = completed_in_w / hours

        # Step latency stats
        lats = list(self._step_latencies)
        if lats:
            lat_mean = mean(lats)
            lat_p95 = quantiles(lats, n=20)[18] if len(lats) >= 20 else max(lats)
        else:
            lat_mean = lat_p95 = 0.0

        # Recent failures
        recent_failures = self._build_failure_details(window_start)

        return DashboardSnapshot(
            captured_at=now,
            pending=pending,
            running=running,
            completed=completed,
            failed=failed,
            paused=paused,
            window_minutes=self._window,
            started_in_window=started_in_w,
            completed_in_window=completed_in_w,
            failed_in_window=failed_in_w,
            error_rate=error_rate,
            throughput_per_hour=throughput_per_hour,
            step_latency_mean_sec=lat_mean,
            step_latency_p95_sec=lat_p95,
            queue_depth=pending,
            recent_failures=recent_failures,
        )

    def _build_failure_details(self, since: datetime) -> List[FailureDetail]:
        from codex_telegram_bot.domain.missions import MISSION_STATE_FAILED
        missions = self._store.list_missions(state=MISSION_STATE_FAILED)
        details: List[FailureDetail] = []
        for m in missions:
            events = self._store.list_mission_events(m.mission_id)
            fail_events = [e for e in events if e.to_state == MISSION_STATE_FAILED]
            if not fail_events:
                continue
            latest = max(fail_events, key=lambda e: e.created_at)
            if latest.created_at < since:
                continue
            details.append(FailureDetail(
                mission_id=m.mission_id,
                title=m.title,
                failed_at=latest.created_at,
                error_hint=latest.reason or "",
            ))
        details.sort(key=lambda d: d.failed_at, reverse=True)
        return details[:self._max_failures]


from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from codex_telegram_bot.persistence.sqlite_store import SqliteRunStore
