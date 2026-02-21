import asyncio
import itertools
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Awaitable, Callable, Dict, Optional, Tuple


@dataclass
class _Job:
    job_id: str
    agent_id: str
    prompt: str
    created_at: datetime
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    status: str = "queued"
    cancelled: bool = False


ExecutorFn = Callable[[str, str, str], Awaitable[str]]
ConcurrencyFn = Callable[[str], int]


class AgentScheduler:
    def __init__(self, executor: ExecutorFn, get_agent_concurrency: ConcurrencyFn):
        self._executor = executor
        self._get_agent_concurrency = get_agent_concurrency
        self._queue: asyncio.PriorityQueue[Tuple[int, int, str]] = asyncio.PriorityQueue()
        self._jobs: Dict[str, _Job] = {}
        self._futures: Dict[str, asyncio.Future[str]] = {}
        self._agent_semaphores: Dict[str, asyncio.Semaphore] = {}
        self._counter = itertools.count()
        self._dispatcher_task: Optional[asyncio.Task] = None
        self._started = False

    def _ensure_started(self) -> None:
        if self._started:
            return
        loop = asyncio.get_running_loop()
        self._dispatcher_task = loop.create_task(self._dispatcher(), name="agent-scheduler-dispatcher")
        self._started = True

    async def shutdown(self) -> None:
        if self._dispatcher_task is None:
            return
        self._dispatcher_task.cancel()
        try:
            await self._dispatcher_task
        except asyncio.CancelledError:
            pass
        self._dispatcher_task = None
        self._started = False

    async def enqueue(self, agent_id: str, prompt: str, priority: int = 100) -> str:
        self._ensure_started()
        job_id = f"job-{next(self._counter)}"
        job = _Job(
            job_id=job_id,
            agent_id=agent_id,
            prompt=prompt,
            created_at=datetime.now(timezone.utc),
        )
        self._jobs[job_id] = job
        self._futures[job_id] = asyncio.get_running_loop().create_future()
        await self._queue.put((priority, next(self._counter), job_id))
        return job_id

    async def wait_result(self, job_id: str) -> str:
        fut = self._futures.get(job_id)
        if fut is None:
            return "Error: unknown scheduled job."
        return await fut

    def job_status(self, job_id: str) -> str:
        job = self._jobs.get(job_id)
        if not job:
            return "unknown"
        return job.status

    def cancel(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        fut = self._futures.get(job_id)
        if not job or not fut:
            return False
        if fut.done():
            return False
        job.cancelled = True
        job.status = "cancelled"
        fut.set_result("Error: scheduled job was cancelled.")
        return True

    async def _dispatcher(self) -> None:
        while True:
            _, _, job_id = await self._queue.get()
            job = self._jobs.get(job_id)
            fut = self._futures.get(job_id)
            if not job or not fut or fut.done() or job.cancelled:
                self._queue.task_done()
                continue
            asyncio.create_task(self._run_job(job, fut), name=f"agent-job-{job_id}")
            self._queue.task_done()

    async def _run_job(self, job: _Job, fut: asyncio.Future[str]) -> None:
        sem = self._semaphore_for_agent(job.agent_id)
        async with sem:
            if fut.done() or job.cancelled:
                return
            job.status = "running"
            job.started_at = datetime.now(timezone.utc)
            try:
                output = await self._executor(job.agent_id, job.prompt, job.job_id)
            except Exception as exc:  # pragma: no cover - defensive safety net
                output = f"Error: scheduler execution failure: {exc}"
            job.completed_at = datetime.now(timezone.utc)
            job.status = "failed" if output.startswith("Error:") else "completed"
            if not fut.done():
                fut.set_result(output)

    def _semaphore_for_agent(self, agent_id: str) -> asyncio.Semaphore:
        current = self._agent_semaphores.get(agent_id)
        desired = max(1, self._get_agent_concurrency(agent_id))
        if current is None:
            current = asyncio.Semaphore(desired)
            self._agent_semaphores[agent_id] = current
        return current
