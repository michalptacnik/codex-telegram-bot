"""Mission watchdog and auto-recovery (EPIC 9, issue #91).

The MissionWatchdog runs as an asyncio background task and periodically
scans the mission store for:
  - Stalled missions: running missions that have not made progress
    within a configurable threshold (stale_threshold_sec).
  - Failed missions with retries remaining: automatically re-queues them
    back to idle so MissionScheduler can pick them up.

Auto-recovery actions are logged as audit events via the store.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Awaitable, Callable, Dict, List, Optional

from codex_telegram_bot.domain.missions import (
    MISSION_STATE_FAILED,
    MISSION_STATE_IDLE,
    MISSION_STATE_RUNNING,
)

logger = logging.getLogger(__name__)

AlertCallback = Callable[[str, str, str], Awaitable[None]]


@dataclass(frozen=True)
class WatchdogConfig:
    poll_interval_sec: float = 30.0       # how often to scan
    stale_threshold_sec: float = 300.0    # running but no update for this long = stale
    auto_recover_failed: bool = True      # re-queue failed missions with retries left
    max_auto_recoveries: int = 3          # cap on how many per cycle to avoid runaway


class MissionWatchdog:
    """Monitors missions for staleness and drives auto-recovery.

    Usage::

        watchdog = MissionWatchdog(store=store, config=WatchdogConfig())
        await watchdog.start()
        # ... app runs ...
        await watchdog.stop()
    """

    def __init__(
        self,
        store: "SqliteRunStore",   # type: ignore[name-defined]  # avoid circular at runtime
        config: Optional[WatchdogConfig] = None,
        alert_callback: Optional[AlertCallback] = None,
    ) -> None:
        self._store = store
        self._config = config or WatchdogConfig()
        self._alert = alert_callback
        self._task: Optional[asyncio.Task] = None
        self._running = False

        # Tracks last-known updated_at per mission to detect staleness.
        self._last_seen: Dict[str, str] = {}

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="mission-watchdog")
        logger.info("watchdog: started (poll=%.0fs stale=%.0fs)", self._config.poll_interval_sec, self._config.stale_threshold_sec)

    async def stop(self) -> None:
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _loop(self) -> None:
        while self._running:
            try:
                await self._scan()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("watchdog: scan error")
            try:
                await asyncio.sleep(self._config.poll_interval_sec)
            except asyncio.CancelledError:
                break

    async def _scan(self) -> None:
        now = datetime.now(timezone.utc)
        recovered = 0

        # --- Check stalled running missions ---
        running = self._store.list_missions(state=MISSION_STATE_RUNNING)
        for mission in running:
            last_update = mission.updated_at
            if last_update.tzinfo is None:
                last_update = last_update.replace(tzinfo=timezone.utc)
            age_sec = (now - last_update).total_seconds()

            prev = self._last_seen.get(mission.mission_id)
            current = mission.updated_at.isoformat()

            if prev == current and age_sec >= self._config.stale_threshold_sec:
                logger.warning(
                    "watchdog: mission=%s stalled (age=%.0fs), marking failed",
                    mission.mission_id, age_sec,
                )
                try:
                    self._store.transition_mission(
                        mission.mission_id,
                        MISSION_STATE_FAILED,
                        f"watchdog: stalled for {age_sec:.0f}s",
                    )
                except Exception as exc:
                    logger.debug("watchdog: could not mark mission failed: %s", exc)
                await self._alert_maybe(
                    mission.mission_id, "watchdog.stalled", f"stalled {age_sec:.0f}s"
                )
            else:
                self._last_seen[mission.mission_id] = current

        # --- Auto-recover failed missions with retries remaining ---
        if self._config.auto_recover_failed:
            failed = self._store.list_missions(state=MISSION_STATE_FAILED)
            for mission in failed:
                if recovered >= self._config.max_auto_recoveries:
                    break
                if mission.retry_count < mission.retry_limit:
                    try:
                        self._store.transition_mission(
                            mission.mission_id,
                            MISSION_STATE_IDLE,
                            f"watchdog: auto-recovery attempt {mission.retry_count + 1}/{mission.retry_limit}",
                        )
                        self._store.increment_mission_retry(mission.mission_id)
                        recovered += 1
                        logger.info(
                            "watchdog: auto-recovered mission=%s (attempt %d/%d)",
                            mission.mission_id,
                            mission.retry_count + 1,
                            mission.retry_limit,
                        )
                        await self._alert_maybe(
                            mission.mission_id,
                            "watchdog.recovered",
                            f"retry {mission.retry_count + 1}/{mission.retry_limit}",
                        )
                    except Exception as exc:
                        logger.debug("watchdog: recovery failed for %s: %s", mission.mission_id, exc)

        if running or (self._config.auto_recover_failed and failed):
            logger.debug(
                "watchdog: scan complete — stalled_check=%d failed_checked=%d recovered=%d",
                len(running),
                len(failed) if self._config.auto_recover_failed else 0,
                recovered,
            )

    async def _alert_maybe(self, mission_id: str, kind: str, detail: str) -> None:
        if self._alert:
            try:
                await self._alert(mission_id, kind, detail)
            except Exception:
                pass


# Avoid circular import at runtime (store imports connectors, watchdog needs store)
from typing import TYPE_CHECKING
if not TYPE_CHECKING:
    try:
        from codex_telegram_bot.persistence.sqlite_store import SqliteRunStore  # noqa: F401
    except ImportError:
        pass
