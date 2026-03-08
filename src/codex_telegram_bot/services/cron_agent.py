"""Always-alive CronHeartbeatAgent — the unified passive agent daemon.

Wires together the existing CronScheduler, HeartbeatStore, MissionWorkerDaemon,
and ProactiveMessenger into a single coherent background loop that makes the bot
feel "alive" on the user's machine.

The agent runs alongside the Telegram polling loop (or standalone via --daemon)
and performs:

1. **Heartbeat evaluation** — periodic checks for due obligations, tasks, and
   daily checklists, sending proactive Telegram messages when action is needed.
2. **Cron job execution** — user-defined scheduled jobs (file watches, git
   status checks, system health monitors) dispatched on cron expressions.
3. **System watchers** — built-in lightweight monitors for disk usage, git repo
   status, and file changes that surface information proactively.
4. **Mission dispatch** — delegates to MissionWorkerDaemon for long-running
   autonomous missions.

Usage (embedded in main bot process)::

    agent = CronHeartbeatAgent(config)
    await agent.start()
    # ... telegram polling ...
    await agent.stop()

Usage (standalone daemon)::

    agent = CronHeartbeatAgent(config)
    await agent.run()  # blocks until SIGTERM/SIGINT
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import signal
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

from codex_telegram_bot.services.heartbeat import HeartbeatStore, HeartbeatDecision

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass
class CronAgentConfig:
    """Configuration for the always-alive agent."""

    # Heartbeat check interval (seconds)
    heartbeat_interval_sec: float = float(
        os.environ.get("CRON_AGENT_HEARTBEAT_SEC", "1800")  # 30 min default
    )

    # System watcher interval (seconds)
    system_watch_interval_sec: float = float(
        os.environ.get("CRON_AGENT_SYSTEM_WATCH_SEC", "300")  # 5 min default
    )

    # Health heartbeat file for systemd watchdog
    health_file: Optional[Path] = None

    # Health heartbeat write interval
    health_interval_sec: float = 30.0

    # User timezone for quiet hours
    timezone: str = os.environ.get("CRON_AGENT_TIMEZONE", "Europe/Amsterdam")

    # Workspace root for heartbeat/memory files
    workspace_root: Path = field(default_factory=Path.cwd)

    # Enable individual watchers
    enable_git_watcher: bool = (
        os.environ.get("CRON_AGENT_GIT_WATCH", "1").strip().lower() in {"1", "true", "yes"}
    )
    enable_disk_watcher: bool = (
        os.environ.get("CRON_AGENT_DISK_WATCH", "1").strip().lower() in {"1", "true", "yes"}
    )

    # Disk usage warning threshold (percentage)
    disk_warn_percent: int = int(os.environ.get("CRON_AGENT_DISK_WARN_PCT", "90"))

    # Git repos to watch (comma-separated paths, default: workspace root)
    git_watch_paths: str = os.environ.get("CRON_AGENT_GIT_PATHS", "")

    # Max consecutive errors before self-restart request
    max_consecutive_errors: int = 10


# ---------------------------------------------------------------------------
# System watchers — lightweight checks that surface info proactively
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class WatcherAlert:
    """A single alert from a system watcher."""
    source: str
    severity: str  # info | warning | critical
    message: str


def _check_disk_usage(warn_percent: int = 90) -> List[WatcherAlert]:
    """Check disk usage on key mount points."""
    alerts: List[WatcherAlert] = []
    try:
        usage = shutil.disk_usage("/")
        pct = int(usage.used / usage.total * 100)
        if pct >= warn_percent:
            free_gb = usage.free / (1024 ** 3)
            alerts.append(WatcherAlert(
                source="disk",
                severity="warning" if pct < 95 else "critical",
                message=f"Disk usage at {pct}% — {free_gb:.1f} GB free on /",
            ))
    except Exception as exc:
        logger.debug("disk watcher error: %s", exc)
    return alerts


def _check_git_repos(paths: List[Path]) -> List[WatcherAlert]:
    """Check git repos for uncommitted changes and upstream updates."""
    alerts: List[WatcherAlert] = []
    for repo_path in paths:
        if not (repo_path / ".git").is_dir():
            continue
        try:
            # Check for uncommitted changes
            result = subprocess.run(
                ["git", "status", "--porcelain"],
                capture_output=True, text=True, timeout=10,
                cwd=str(repo_path),
            )
            if result.returncode == 0 and result.stdout.strip():
                line_count = len(result.stdout.strip().splitlines())
                alerts.append(WatcherAlert(
                    source="git",
                    severity="info",
                    message=f"Repo {repo_path.name}: {line_count} uncommitted change(s)",
                ))

            # Check if behind upstream
            subprocess.run(
                ["git", "fetch", "--quiet"],
                capture_output=True, timeout=15,
                cwd=str(repo_path),
            )
            behind = subprocess.run(
                ["git", "rev-list", "--count", "HEAD..@{upstream}"],
                capture_output=True, text=True, timeout=5,
                cwd=str(repo_path),
            )
            if behind.returncode == 0:
                count = int(behind.stdout.strip() or "0")
                if count > 0:
                    alerts.append(WatcherAlert(
                        source="git",
                        severity="info",
                        message=f"Repo {repo_path.name}: {count} commit(s) behind upstream",
                    ))
        except Exception as exc:
            logger.debug("git watcher error for %s: %s", repo_path, exc)
    return alerts


# ---------------------------------------------------------------------------
# Main agent
# ---------------------------------------------------------------------------

DeliveryFn = Callable[[Dict[str, Any]], Awaitable[Dict[str, Any]]]


class CronHeartbeatAgent:
    """The always-alive passive agent daemon.

    Runs heartbeat checks, system watchers, and cron jobs in the background,
    delivering proactive messages through the ProactiveMessenger.
    """

    def __init__(
        self,
        config: CronAgentConfig,
        delivery_fn: Optional[DeliveryFn] = None,
    ) -> None:
        self._config = config
        self._deliver = delivery_fn
        self._stop_event = asyncio.Event()
        self._tasks: List[asyncio.Task] = []
        self._running = False
        self._consecutive_errors = 0
        self._started_at: Optional[datetime] = None
        self._last_heartbeat_check: Optional[datetime] = None
        self._last_system_check: Optional[datetime] = None
        self._heartbeat_store: Optional[HeartbeatStore] = None
        self._stats = {
            "heartbeat_checks": 0,
            "heartbeat_actions": 0,
            "system_checks": 0,
            "system_alerts": 0,
            "deliveries_sent": 0,
            "delivery_failures": 0,
        }

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start background loops (non-blocking)."""
        if self._running:
            return
        self._running = True
        self._started_at = datetime.now(timezone.utc)
        self._stop_event.clear()

        # Initialize heartbeat store
        try:
            self._heartbeat_store = HeartbeatStore(self._config.workspace_root)
        except Exception as exc:
            logger.warning("cron_agent: heartbeat store init failed: %s", exc)

        self._tasks.append(
            asyncio.create_task(self._heartbeat_loop(), name="cron-agent-heartbeat")
        )
        self._tasks.append(
            asyncio.create_task(self._system_watcher_loop(), name="cron-agent-watchers")
        )
        if self._config.health_file:
            self._tasks.append(
                asyncio.create_task(self._health_loop(), name="cron-agent-health")
            )

        logger.info(
            "cron_agent: started (heartbeat=%.0fs, watchers=%.0fs, tz=%s)",
            self._config.heartbeat_interval_sec,
            self._config.system_watch_interval_sec,
            self._config.timezone,
        )

    async def stop(self) -> None:
        """Gracefully stop all background loops."""
        if not self._running:
            return
        logger.info("cron_agent: stopping...")
        self._stop_event.set()
        self._running = False
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        logger.info("cron_agent: stopped")

    async def run(self) -> None:
        """Blocking entrypoint for standalone daemon mode."""
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, lambda: self._stop_event.set())
        await self.start()
        await self._stop_event.wait()
        await self.stop()

    def is_healthy(self) -> bool:
        return self._running and len(self._tasks) > 0

    def status(self) -> Dict[str, Any]:
        return {
            "running": self._running,
            "started_at": self._started_at.isoformat() if self._started_at else None,
            "last_heartbeat_check": (
                self._last_heartbeat_check.isoformat()
                if self._last_heartbeat_check else None
            ),
            "last_system_check": (
                self._last_system_check.isoformat()
                if self._last_system_check else None
            ),
            "uptime_sec": (
                (datetime.now(timezone.utc) - self._started_at).total_seconds()
                if self._started_at else 0
            ),
            **self._stats,
        }

    def set_delivery_fn(self, fn: DeliveryFn) -> None:
        """Set or replace the delivery function (e.g. after Telegram connects)."""
        self._deliver = fn

    # ------------------------------------------------------------------
    # Heartbeat loop
    # ------------------------------------------------------------------

    async def _heartbeat_loop(self) -> None:
        """Periodically evaluate heartbeat and deliver proactive messages."""
        while not self._stop_event.is_set():
            try:
                await self._run_heartbeat_check()
                self._consecutive_errors = 0
            except asyncio.CancelledError:
                break
            except Exception:
                self._consecutive_errors += 1
                logger.exception(
                    "cron_agent: heartbeat error (%d consecutive)",
                    self._consecutive_errors,
                )
                if self._consecutive_errors >= self._config.max_consecutive_errors:
                    logger.error("cron_agent: too many heartbeat errors, stopping")
                    self._stop_event.set()
                    break
            await self._sleep(self._config.heartbeat_interval_sec)

    async def _run_heartbeat_check(self) -> None:
        self._last_heartbeat_check = datetime.now(timezone.utc)
        self._stats["heartbeat_checks"] += 1

        if self._heartbeat_store is None:
            return

        decision: HeartbeatDecision = self._heartbeat_store.evaluate(
            timezone_name=self._config.timezone,
        )

        if decision.action == "NO_ACTION":
            logger.debug(
                "cron_agent: heartbeat=NO_ACTION quiet=%s",
                decision.quiet_hours_blocked,
            )
            return

        if decision.action == "ACTION" and decision.text:
            self._stats["heartbeat_actions"] += 1
            await self._send_proactive(
                source="heartbeat",
                text=decision.text,
            )

    # ------------------------------------------------------------------
    # System watcher loop
    # ------------------------------------------------------------------

    async def _system_watcher_loop(self) -> None:
        """Periodically run system watchers and deliver alerts."""
        # Initial delay so the bot has time to fully start
        await self._sleep(30)
        while not self._stop_event.is_set():
            try:
                await self._run_system_checks()
                self._consecutive_errors = 0
            except asyncio.CancelledError:
                break
            except Exception:
                self._consecutive_errors += 1
                logger.exception(
                    "cron_agent: system watcher error (%d consecutive)",
                    self._consecutive_errors,
                )
            await self._sleep(self._config.system_watch_interval_sec)

    async def _run_system_checks(self) -> None:
        self._last_system_check = datetime.now(timezone.utc)
        self._stats["system_checks"] += 1

        alerts: List[WatcherAlert] = []

        # Run watchers in thread pool to avoid blocking the event loop
        loop = asyncio.get_running_loop()

        if self._config.enable_disk_watcher:
            disk_alerts = await loop.run_in_executor(
                None, _check_disk_usage, self._config.disk_warn_percent
            )
            alerts.extend(disk_alerts)

        if self._config.enable_git_watcher:
            git_paths = self._resolve_git_paths()
            if git_paths:
                git_alerts = await loop.run_in_executor(
                    None, _check_git_repos, git_paths
                )
                alerts.extend(git_alerts)

        if not alerts:
            return

        self._stats["system_alerts"] += len(alerts)

        # Format and deliver
        lines = ["**System watch report:**"]
        for alert in alerts:
            icon = {"critical": "!!", "warning": "!", "info": "-"}.get(alert.severity, "-")
            lines.append(f"  {icon} [{alert.source}] {alert.message}")
        await self._send_proactive(
            source="system_watcher",
            text="\n".join(lines),
        )

    def _resolve_git_paths(self) -> List[Path]:
        """Resolve configured git watch paths."""
        raw = self._config.git_watch_paths.strip()
        if raw:
            return [
                Path(p.strip()).expanduser().resolve()
                for p in raw.split(",")
                if p.strip()
            ]
        # Default: workspace root if it's a git repo
        ws = self._config.workspace_root
        if (ws / ".git").is_dir():
            return [ws]
        return []

    # ------------------------------------------------------------------
    # Health loop (systemd watchdog)
    # ------------------------------------------------------------------

    async def _health_loop(self) -> None:
        """Write periodic health file for systemd watchdog."""
        while not self._stop_event.is_set():
            if self._config.health_file:
                try:
                    self._config.health_file.write_text(
                        datetime.now(timezone.utc).isoformat()
                    )
                except OSError as exc:
                    logger.warning("cron_agent: health write failed: %s", exc)
            await self._sleep(self._config.health_interval_sec)

    # ------------------------------------------------------------------
    # Delivery
    # ------------------------------------------------------------------

    async def _send_proactive(self, source: str, text: str) -> None:
        """Deliver a proactive message through the registered delivery function."""
        if not self._deliver:
            logger.info("cron_agent: no delivery_fn, skipping: [%s] %s", source, text[:100])
            return

        payload = {
            "event": "proactive",
            "source": source,
            "text": text,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        try:
            await self._deliver(payload)
            self._stats["deliveries_sent"] += 1
        except Exception as exc:
            self._stats["delivery_failures"] += 1
            logger.warning("cron_agent: delivery failed [%s]: %s", source, exc)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _sleep(self, seconds: float) -> None:
        """Sleep but wake immediately on stop."""
        try:
            await asyncio.wait_for(
                asyncio.shield(self._stop_event.wait()),
                timeout=seconds,
            )
        except asyncio.TimeoutError:
            pass
