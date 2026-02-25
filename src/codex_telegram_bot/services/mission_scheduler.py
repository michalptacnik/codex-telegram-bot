"""Recurring mission scheduler with retry/backoff (EPIC 6, issue #76).

Responsibilities:
- Schedule missions to run once or on a recurring interval.
- Enforce per-mission concurrency limits via asyncio.Semaphore.
- Retry failed runs with exponential backoff + jitter.
- Lease semantics: a mission can only have one active worker at a time.
"""
from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Awaitable, Callable, Dict, Optional

logger = logging.getLogger(__name__)

MissionExecutorFn = Callable[[str], Awaitable[str]]

_BASE_BACKOFF_SEC = 2.0
_MAX_BACKOFF_SEC = 300.0
_JITTER_FRACTION = 0.25


def _backoff_seconds(attempt: int) -> float:
    """Return exponential backoff with ±25 % jitter."""
    base = min(_BASE_BACKOFF_SEC * (2 ** attempt), _MAX_BACKOFF_SEC)
    jitter = base * _JITTER_FRACTION * (2 * random.random() - 1)
    return max(0.1, base + jitter)


@dataclass
class _MissionJob:
    mission_id: str
    interval_sec: Optional[int]
    retry_limit: int
    max_concurrency: int
    cancelled: bool = False
    task: Optional[asyncio.Task] = field(default=None, compare=False)


class MissionScheduler:
    """Schedule missions and manage their execution lifecycle.

    Usage::

        async def my_executor(mission_id: str) -> str:
            # Run the mission, return output or "Error: ..."
            ...

        scheduler = MissionScheduler(executor=my_executor)
        await scheduler.schedule(
            mission_id="abc",
            interval_sec=3600,   # recurring every hour; None = run once
            retry_limit=3,
            max_concurrency=1,
        )
        # Later...
        await scheduler.cancel("abc")
        await scheduler.shutdown()
    """

    def __init__(self, executor: MissionExecutorFn) -> None:
        self._executor = executor
        self._jobs: Dict[str, _MissionJob] = {}
        self._semaphores: Dict[str, asyncio.Semaphore] = {}
        # Per-mission lock: prevents two scheduling loops from running the
        # same mission simultaneously (lease semantics).
        self._leases: Dict[str, asyncio.Lock] = {}

    async def schedule(
        self,
        mission_id: str,
        interval_sec: Optional[int] = None,
        retry_limit: int = 3,
        max_concurrency: int = 1,
    ) -> None:
        """Begin scheduling a mission.  Idempotent if already running."""
        if mission_id in self._jobs and not self._jobs[mission_id].cancelled:
            return  # already scheduled
        job = _MissionJob(
            mission_id=mission_id,
            interval_sec=interval_sec,
            retry_limit=retry_limit,
            max_concurrency=max(1, max_concurrency),
        )
        self._jobs[mission_id] = job
        self._semaphores[mission_id] = asyncio.Semaphore(job.max_concurrency)
        self._leases[mission_id] = asyncio.Lock()
        task = asyncio.create_task(
            self._scheduling_loop(job),
            name=f"mission-scheduler-{mission_id}",
        )
        job.task = task

    async def cancel(self, mission_id: str) -> bool:
        """Cancel a scheduled mission.  Returns True if it was running."""
        job = self._jobs.get(mission_id)
        if not job or job.cancelled:
            return False
        job.cancelled = True
        if job.task and not job.task.done():
            job.task.cancel()
            try:
                await job.task
            except asyncio.CancelledError:
                pass
        return True

    def is_scheduled(self, mission_id: str) -> bool:
        job = self._jobs.get(mission_id)
        return bool(job and not job.cancelled)

    async def shutdown(self) -> None:
        """Cancel all scheduled missions and wait for them to stop."""
        for mission_id in list(self._jobs.keys()):
            await self.cancel(mission_id)

    # ------------------------------------------------------------------
    # Internal scheduling logic
    # ------------------------------------------------------------------

    async def _scheduling_loop(self, job: _MissionJob) -> None:
        attempt = 0
        while not job.cancelled:
            async with self._leases[job.mission_id]:
                if job.cancelled:
                    break
                sem = self._semaphores[job.mission_id]
                async with sem:
                    if job.cancelled:
                        break
                    output = await self._run_with_retry(job, attempt)
                    failed = output.startswith("Error:")
                    if failed:
                        attempt += 1
                        if attempt > job.retry_limit:
                            logger.warning(
                                "mission=%s exhausted retries (%d), giving up",
                                job.mission_id,
                                job.retry_limit,
                            )
                            break
                        backoff = _backoff_seconds(attempt - 1)
                        logger.info(
                            "mission=%s failed (attempt %d/%d), retrying in %.1fs",
                            job.mission_id,
                            attempt,
                            job.retry_limit,
                            backoff,
                        )
                        await asyncio.sleep(backoff)
                        continue
                    else:
                        attempt = 0  # reset on success

            # Single-run mission (interval_sec=None): stop after first success.
            if job.interval_sec is None:
                break

            # Recurring mission: wait for the next interval (0 = tight loop).
            try:
                await asyncio.sleep(max(0, job.interval_sec))
            except asyncio.CancelledError:
                break

    async def _run_with_retry(self, job: _MissionJob, attempt: int) -> str:
        try:
            return await self._executor(job.mission_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("mission=%s unexpected error", job.mission_id)
            return f"Error: unexpected exception: {exc}"
