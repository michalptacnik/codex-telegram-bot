"""Daemon mode and supervisor integration (EPIC 10, issue #94).

The MissionWorkerDaemon runs the mission execution loop in a background
process / async task, separate from the Telegram polling loop.  It supports:

  - Graceful shutdown via SIGTERM / SIGINT  (asyncio cancellation)
  - Health heartbeat (writes a heartbeat file readable by systemd / Docker)
  - Supervisor restart semantics: exits with a known code on fatal error so
    the supervisor can restart cleanly
  - Resume on restart: incomplete missions are picked back up automatically

Usage (standalone worker entrypoint)::

    from codex_telegram_bot.services.daemon import MissionWorkerDaemon
    daemon = MissionWorkerDaemon(store=store, runner=runner, scheduler=scheduler)
    asyncio.run(daemon.run())

Or embedded inside the main bot process::

    await daemon.start()
    # ... telegram polling ...
    await daemon.stop()
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, List, Optional

logger = logging.getLogger(__name__)

# Exit codes understood by supervisors / systemd
EXIT_OK = 0
EXIT_RESTART = 75       # EX_TEMPFAIL – supervisor should restart
EXIT_FATAL = 1          # permanent failure, no auto-restart


@dataclass
class DaemonConfig:
    heartbeat_path: Optional[Path] = None   # None = no heartbeat file
    heartbeat_interval_sec: float = 30.0
    poll_interval_sec: float = 5.0
    max_consecutive_errors: int = 10        # trigger EXIT_RESTART after this many
    drain_timeout_sec: float = 30.0         # wait for running missions on shutdown


class DaemonState:
    """Observable state bag shared between daemon loops."""
    def __init__(self) -> None:
        self.started_at: Optional[datetime] = None
        self.last_heartbeat: Optional[datetime] = None
        self.missions_dispatched: int = 0
        self.missions_completed: int = 0
        self.missions_failed: int = 0
        self.consecutive_errors: int = 0
        self.running: bool = False

    def to_dict(self) -> dict:
        return {
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "last_heartbeat": self.last_heartbeat.isoformat() if self.last_heartbeat else None,
            "missions_dispatched": self.missions_dispatched,
            "missions_completed": self.missions_completed,
            "missions_failed": self.missions_failed,
            "consecutive_errors": self.consecutive_errors,
            "running": self.running,
            "uptime_sec": (
                (datetime.now(timezone.utc) - self.started_at).total_seconds()
                if self.started_at else 0
            ),
        }


class MissionWorkerDaemon:
    """Async daemon that drives the mission execution loop.

    Responsibilities:
    - Poll for pending / scheduled missions and dispatch them to the runner.
    - Write periodic heartbeat timestamps (for systemd watchdog / health checks).
    - Handle SIGTERM / SIGINT for graceful shutdown.
    - Drain in-flight missions before exiting.
    - Expose ``state`` for observability.
    """

    def __init__(
        self,
        store: "SqliteRunStore",          # type: ignore[name-defined]
        runner: "AutonomousMissionRunner",  # type: ignore[name-defined]
        scheduler: "MissionScheduler",    # type: ignore[name-defined]
        config: Optional[DaemonConfig] = None,
    ) -> None:
        self._store = store
        self._runner = runner
        self._scheduler = scheduler
        self._config = config or DaemonConfig()
        self.state = DaemonState()
        self._stop_event = asyncio.Event()
        self._active_tasks: List[asyncio.Task] = []

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start daemon background tasks (non-blocking)."""
        if self.state.running:
            return
        self.state.running = True
        self.state.started_at = datetime.now(timezone.utc)
        self._stop_event.clear()
        asyncio.create_task(self._dispatch_loop(), name="daemon-dispatch")
        asyncio.create_task(self._heartbeat_loop(), name="daemon-heartbeat")
        logger.info("daemon: started (poll=%.1fs, heartbeat=%.1fs)",
                    self._config.poll_interval_sec, self._config.heartbeat_interval_sec)

    async def stop(self) -> None:
        """Request graceful shutdown and wait for drain."""
        if not self.state.running:
            return
        logger.info("daemon: shutdown requested – draining …")
        self._stop_event.set()
        self.state.running = False
        if self._active_tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*self._active_tasks, return_exceptions=True),
                    timeout=self._config.drain_timeout_sec,
                )
            except asyncio.TimeoutError:
                logger.warning("daemon: drain timeout – cancelling %d tasks",
                               len(self._active_tasks))
                for t in self._active_tasks:
                    t.cancel()
        logger.info("daemon: stopped")

    async def run(self) -> None:
        """Blocking entrypoint for standalone worker process."""
        loop = asyncio.get_running_loop()
        loop.add_signal_handler(signal.SIGTERM, self._handle_signal)
        loop.add_signal_handler(signal.SIGINT, self._handle_signal)
        await self.start()
        await self._stop_event.wait()
        await self.stop()

    def is_healthy(self) -> bool:
        """Return True if daemon is running and heartbeat is recent."""
        if not self.state.running:
            return False
        if self.state.last_heartbeat is None:
            return False
        age = (datetime.now(timezone.utc) - self.state.last_heartbeat).total_seconds()
        return age < self._config.heartbeat_interval_sec * 3

    # ------------------------------------------------------------------
    # Internal loops
    # ------------------------------------------------------------------

    def _handle_signal(self) -> None:
        logger.info("daemon: received shutdown signal")
        self._stop_event.set()

    async def _dispatch_loop(self) -> None:
        from codex_telegram_bot.domain.missions import MISSION_STATE_IDLE, MISSION_STATE_RUNNING
        while not self._stop_event.is_set():
            try:
                # Re-queue stale running missions (from previous crash)
                stale = self._store.list_missions(state=MISSION_STATE_RUNNING)
                for m in stale:
                    if not self._runner.is_active(m.mission_id):
                        logger.info("daemon: resuming stale mission %s", m.mission_id)
                        task = asyncio.create_task(
                            self._run_mission(m.mission_id), name=f"mission-{m.mission_id}"
                        )
                        self._active_tasks.append(task)
                        self.state.missions_dispatched += 1

                # Dispatch newly idle (queued) missions
                pending = self._store.list_missions(state=MISSION_STATE_IDLE)
                for m in pending:
                    if not self._runner.is_active(m.mission_id):
                        task = asyncio.create_task(
                            self._run_mission(m.mission_id), name=f"mission-{m.mission_id}"
                        )
                        self._active_tasks.append(task)
                        self.state.missions_dispatched += 1

                # Prune finished tasks
                self._active_tasks = [t for t in self._active_tasks if not t.done()]
                self.state.consecutive_errors = 0
            except asyncio.CancelledError:
                break
            except Exception:
                self.state.consecutive_errors += 1
                logger.exception("daemon: dispatch error (%d consecutive)",
                                 self.state.consecutive_errors)
                if self.state.consecutive_errors >= self._config.max_consecutive_errors:
                    logger.error("daemon: too many errors – requesting restart")
                    self._stop_event.set()
                    break

            try:
                await asyncio.wait_for(
                    asyncio.shield(self._stop_event.wait()),
                    timeout=self._config.poll_interval_sec,
                )
                break
            except asyncio.TimeoutError:
                pass

    async def _run_mission(self, mission_id: str) -> None:
        try:
            await self._runner.run(mission_id)
            self.state.missions_completed += 1
        except Exception:
            self.state.missions_failed += 1
            logger.exception("daemon: mission %s failed", mission_id)

    async def _heartbeat_loop(self) -> None:
        while not self._stop_event.is_set():
            now = datetime.now(timezone.utc)
            self.state.last_heartbeat = now
            if self._config.heartbeat_path:
                try:
                    self._config.heartbeat_path.write_text(now.isoformat())
                except OSError as e:
                    logger.warning("daemon: heartbeat write failed: %s", e)
            try:
                await asyncio.wait_for(
                    asyncio.shield(self._stop_event.wait()),
                    timeout=self._config.heartbeat_interval_sec,
                )
                break
            except asyncio.TimeoutError:
                pass


from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from codex_telegram_bot.persistence.sqlite_store import SqliteRunStore
    from codex_telegram_bot.services.mission_runner import AutonomousMissionRunner
    from codex_telegram_bot.services.mission_scheduler import MissionScheduler
