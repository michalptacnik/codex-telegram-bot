import asyncio
import unittest

from codex_telegram_bot.services.agent_scheduler import AgentScheduler


class TestAgentScheduler(unittest.IsolatedAsyncioTestCase):
    async def test_serializes_jobs_per_agent_when_concurrency_is_one(self):
        state = {"running": 0, "max_running": 0, "order": []}

        async def executor(agent_id: str, prompt: str, correlation_id: str) -> str:
            state["running"] += 1
            state["max_running"] = max(state["max_running"], state["running"])
            await asyncio.sleep(0.02)
            state["order"].append(prompt)
            state["running"] -= 1
            return f"ok:{prompt}"

        scheduler = AgentScheduler(executor=executor, get_agent_concurrency=lambda _aid: 1)
        j1 = await scheduler.enqueue("default", "a")
        j2 = await scheduler.enqueue("default", "b")
        r1 = await scheduler.wait_result(j1)
        r2 = await scheduler.wait_result(j2)

        self.assertEqual(r1, "ok:a")
        self.assertEqual(r2, "ok:b")
        self.assertEqual(state["max_running"], 1)
        self.assertEqual(state["order"], ["a", "b"])
        await scheduler.shutdown()

    async def test_cancelled_job_returns_cancel_message(self):
        async def executor(agent_id: str, prompt: str, correlation_id: str) -> str:
            await asyncio.sleep(0.05)
            return "ok"

        scheduler = AgentScheduler(executor=executor, get_agent_concurrency=lambda _aid: 1)
        job = await scheduler.enqueue("default", "cancel-me")
        cancelled = scheduler.cancel(job)
        result = await scheduler.wait_result(job)

        self.assertTrue(cancelled)
        self.assertTrue(result.startswith("Error: scheduled job was cancelled."))
        self.assertEqual(scheduler.job_status(job), "cancelled")
        await scheduler.shutdown()
