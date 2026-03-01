from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Dict, Optional
from zoneinfo import ZoneInfo

from codex_telegram_bot.services.cron_utils import cron_next_run

logger = logging.getLogger(__name__)


ExecuteFn = Callable[[dict], Awaitable[bool]]


def _backoff_seconds(failure_count: int) -> int:
    return min(3600, 30 * (2 ** max(0, int(failure_count) - 1)))


class CronScheduler:
    def __init__(
        self,
        store: Any,
        execute_fn: ExecuteFn,
        tick_interval_sec: int = 60,
        max_failures: int = 3,
    ) -> None:
        self._store = store
        self._execute_fn = execute_fn
        self._tick_interval = max(5, int(tick_interval_sec))
        self._max_failures = max(1, int(max_failures))
        self._task: Optional[asyncio.Task] = None
        self._stopped = asyncio.Event()

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stopped.clear()
        self._task = asyncio.create_task(self._run_loop(), name="cron-scheduler")

    async def stop(self) -> None:
        self._stopped.set()
        task = self._task
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        self._task = None

    async def tick_once(self) -> Dict[str, int]:
        now = datetime.now(timezone.utc)
        due = self._store.list_due_cron_jobs(now.isoformat(), limit=50)
        ran = 0
        failed = 0
        for job in due:
            ran += 1
            try:
                ok = await self._execute_fn(job)
                if ok:
                    await self._mark_success(job, now=now)
                else:
                    failed += 1
                    await self._mark_failure(job, error="job returned failure", now=now)
            except Exception as exc:
                failed += 1
                logger.exception("cron job execution failed id=%s", job.get("id"))
                await self._mark_failure(job, error=str(exc), now=now)
        return {"due": len(due), "ran": ran, "failed": failed}

    async def _run_loop(self) -> None:
        while not self._stopped.is_set():
            try:
                await self.tick_once()
            except Exception:
                logger.exception("cron scheduler tick failed")
            await asyncio.sleep(self._tick_interval)

    async def _mark_success(self, job: dict, now: datetime) -> None:
        last_run = now.isoformat()
        next_run: Optional[str] = None
        if not bool(job.get("one_shot")):
            cron_expr = str(job.get("cron_expr") or "").strip()
            tz_name = str(job.get("tz") or "Europe/Amsterdam")
            local_now = now.astimezone(ZoneInfo(tz_name))
            next_local = cron_next_run(cron_expr, local_now)
            next_run = next_local.astimezone(timezone.utc).isoformat()
        self._store.mark_cron_job_success(job.get("id", ""), last_run=last_run, next_run=next_run)

    async def _mark_failure(self, job: dict, error: str, now: datetime) -> None:
        failure_count = int(job.get("failure_count") or 0) + 1
        delay = _backoff_seconds(failure_count)
        next_run = (now + timedelta(seconds=delay)).isoformat()
        self._store.mark_cron_job_failure(
            job.get("id", ""),
            error=error,
            next_run=next_run,
            failure_count=failure_count,
            max_failures=self._max_failures,
        )

