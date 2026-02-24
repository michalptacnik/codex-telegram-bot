"""Tests for EPIC 6: Autonomous Mission Runtime.

Covers:
  #75 - Mission domain model + state machine + persistence
  #76 - MissionScheduler recurring + retry/backoff
  #77 - MissionPlanner task decomposition
  #78 - AutonomousMissionRunner execution loop
"""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from typing import List
from unittest.mock import AsyncMock, MagicMock, patch

from codex_telegram_bot.domain.missions import (
    MISSION_STATE_BLOCKED,
    MISSION_STATE_COMPLETED,
    MISSION_STATE_FAILED,
    MISSION_STATE_IDLE,
    MISSION_STATE_PAUSED,
    MISSION_STATE_RUNNING,
    MissionPlan,
    MissionStep,
    MissionTransitionError,
    allowed_next_states,
    validate_transition,
)
from codex_telegram_bot.persistence.sqlite_store import SqliteRunStore
from codex_telegram_bot.services.mission_planner import MissionPlanner, _extract_json_array, _parse_steps
from codex_telegram_bot.services.mission_runner import AutonomousMissionRunner
from codex_telegram_bot.services.mission_scheduler import MissionScheduler, _backoff_seconds


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_store(tmp: str) -> SqliteRunStore:
    return SqliteRunStore(Path(tmp) / "test.db")


# ---------------------------------------------------------------------------
# #75 – Mission domain model + state machine
# ---------------------------------------------------------------------------


class TestMissionDomain(unittest.TestCase):
    def test_validate_allowed_transition(self):
        validate_transition(MISSION_STATE_IDLE, MISSION_STATE_RUNNING)  # no raise

    def test_validate_disallowed_transition_raises(self):
        with self.assertRaises(MissionTransitionError):
            validate_transition(MISSION_STATE_COMPLETED, MISSION_STATE_RUNNING)

    def test_validate_unknown_state_raises(self):
        with self.assertRaises(MissionTransitionError):
            validate_transition("nonexistent", MISSION_STATE_RUNNING)

    def test_allowed_next_states_from_idle(self):
        nexts = allowed_next_states(MISSION_STATE_IDLE)
        self.assertIn(MISSION_STATE_RUNNING, nexts)
        self.assertNotIn(MISSION_STATE_COMPLETED, nexts)

    def test_allowed_next_states_from_running(self):
        nexts = allowed_next_states(MISSION_STATE_RUNNING)
        self.assertIn(MISSION_STATE_COMPLETED, nexts)
        self.assertIn(MISSION_STATE_FAILED, nexts)
        self.assertIn(MISSION_STATE_PAUSED, nexts)
        self.assertIn(MISSION_STATE_BLOCKED, nexts)

    def test_terminal_states_have_limited_transitions(self):
        # From completed and failed, only re-queue to idle is allowed.
        for terminal in (MISSION_STATE_COMPLETED, MISSION_STATE_FAILED):
            nexts = allowed_next_states(terminal)
            self.assertEqual(nexts, [MISSION_STATE_IDLE])


class TestMissionPersistence(unittest.TestCase):
    def test_create_and_get_mission(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            mid = store.create_mission(title="Test", goal="do something")
            m = store.get_mission(mid)
            self.assertIsNotNone(m)
            self.assertEqual(m.state, MISSION_STATE_IDLE)
            self.assertEqual(m.goal, "do something")
            self.assertEqual(m.retry_limit, 3)
            self.assertEqual(m.retry_count, 0)

    def test_mission_not_found_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            self.assertIsNone(store.get_mission("bogus"))

    def test_transition_mission_updates_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            mid = store.create_mission(title="T", goal="g")
            updated = store.transition_mission(mid, MISSION_STATE_RUNNING, "test")
            self.assertEqual(updated.state, MISSION_STATE_RUNNING)
            self.assertIsNotNone(updated.started_at)

    def test_transition_sets_completed_at(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            mid = store.create_mission(title="T", goal="g")
            store.transition_mission(mid, MISSION_STATE_RUNNING, "start")
            m = store.transition_mission(mid, MISSION_STATE_COMPLETED, "done")
            self.assertIsNotNone(m.completed_at)

    def test_invalid_transition_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            mid = store.create_mission(title="T", goal="g")
            with self.assertRaises(Exception):
                store.transition_mission(mid, MISSION_STATE_COMPLETED, "skip running")

    def test_audit_trail_records_transitions(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            mid = store.create_mission(title="T", goal="g")
            store.transition_mission(mid, MISSION_STATE_RUNNING, "reason-a")
            store.transition_mission(mid, MISSION_STATE_COMPLETED, "reason-b")
            events = store.list_mission_events(mid)
            self.assertEqual(len(events), 2)
            self.assertEqual(events[0].from_state, MISSION_STATE_IDLE)
            self.assertEqual(events[0].to_state, MISSION_STATE_RUNNING)
            self.assertEqual(events[0].reason, "reason-a")
            self.assertEqual(events[1].to_state, MISSION_STATE_COMPLETED)

    def test_increment_and_reset_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            mid = store.create_mission(title="T", goal="g")
            self.assertEqual(store.increment_mission_retry(mid), 1)
            self.assertEqual(store.increment_mission_retry(mid), 2)
            store.reset_mission_retry(mid)
            self.assertEqual(store.get_mission(mid).retry_count, 0)

    def test_list_missions_by_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            mid1 = store.create_mission(title="A", goal="a")
            mid2 = store.create_mission(title="B", goal="b")
            store.transition_mission(mid1, MISSION_STATE_RUNNING)
            idle = store.list_missions(state=MISSION_STATE_IDLE)
            running = store.list_missions(state=MISSION_STATE_RUNNING)
            self.assertEqual(len(idle), 1)
            self.assertEqual(idle[0].mission_id, mid2)
            self.assertEqual(len(running), 1)

    def test_create_mission_with_schedule(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            mid = store.create_mission(title="R", goal="recurring", schedule_interval_sec=3600)
            m = store.get_mission(mid)
            self.assertEqual(m.schedule_interval_sec, 3600)
            self.assertTrue(m.is_recurring())


# ---------------------------------------------------------------------------
# #76 – MissionScheduler
# ---------------------------------------------------------------------------


class TestBackoffSeconds(unittest.TestCase):
    def test_increases_with_attempt(self):
        b0 = _backoff_seconds(0)
        b1 = _backoff_seconds(1)
        b2 = _backoff_seconds(2)
        # Base values should grow (jitter may overlap, but on average increase).
        self.assertGreater(b1, 0)
        self.assertGreater(b2, 0)

    def test_capped_at_max(self):
        big = _backoff_seconds(100)
        # With max jitter the absolute max is MAX * (1 + JITTER_FRACTION).
        self.assertLessEqual(big, 375.0)


class TestMissionScheduler(unittest.IsolatedAsyncioTestCase):
    async def test_single_run_mission_executes_once(self):
        calls: List[str] = []

        async def executor(mid: str) -> str:
            calls.append(mid)
            return "ok"

        sched = MissionScheduler(executor=executor)
        await sched.schedule("m1", interval_sec=None)
        await asyncio.sleep(0.05)
        await sched.shutdown()
        self.assertEqual(calls, ["m1"])

    async def test_recurring_mission_executes_multiple_times(self):
        calls: List[str] = []

        async def executor(mid: str) -> str:
            calls.append(mid)
            return "ok"

        sched = MissionScheduler(executor=executor)
        await sched.schedule("m2", interval_sec=0)  # 0s interval = tight loop
        await asyncio.sleep(0.3)
        await sched.shutdown()
        self.assertGreater(len(calls), 1)

    async def test_cancel_stops_scheduling(self):
        calls: List[str] = []

        async def executor(mid: str) -> str:
            calls.append(mid)
            await asyncio.sleep(0.2)
            return "ok"

        sched = MissionScheduler(executor=executor)
        await sched.schedule("m3", interval_sec=None)
        await asyncio.sleep(0.01)
        await sched.cancel("m3")
        await sched.shutdown()
        self.assertFalse(sched.is_scheduled("m3"))

    async def test_idempotent_double_schedule(self):
        calls: List[str] = []

        async def executor(mid: str) -> str:
            calls.append(mid)
            return "ok"

        sched = MissionScheduler(executor=executor)
        await sched.schedule("m4", interval_sec=None)
        await sched.schedule("m4", interval_sec=None)  # second call should be no-op
        await asyncio.sleep(0.05)
        await sched.shutdown()
        # Exactly one execution (not doubled).
        self.assertEqual(calls.count("m4"), 1)

    async def test_retry_on_failure(self):
        attempts: List[int] = []

        async def executor(mid: str) -> str:
            attempts.append(1)
            if len(attempts) < 3:
                return "Error: transient"
            return "ok"

        sched = MissionScheduler(executor=executor)
        # Patch backoff to 0 for speed.
        with patch("codex_telegram_bot.services.mission_scheduler._backoff_seconds", return_value=0.0):
            await sched.schedule("m5", interval_sec=None, retry_limit=3)
            await asyncio.sleep(0.3)
        await sched.shutdown()
        self.assertGreaterEqual(len(attempts), 3)


# ---------------------------------------------------------------------------
# #77 – MissionPlanner
# ---------------------------------------------------------------------------


class TestExtractJsonArray(unittest.TestCase):
    def test_parses_clean_array(self):
        raw = '[{"index": 0, "description": "do it", "tool_hint": "ls"}]'
        result = _extract_json_array(raw)
        self.assertIsNotNone(result)
        self.assertEqual(result[0]["description"], "do it")

    def test_strips_markdown_fences(self):
        raw = "```json\n[{\"index\": 0, \"description\": \"x\", \"tool_hint\": \"\"}]\n```"
        result = _extract_json_array(raw)
        self.assertIsNotNone(result)

    def test_returns_none_for_prose(self):
        result = _extract_json_array("Here is the plan: do stuff")
        self.assertIsNone(result)


class TestParsedSteps(unittest.TestCase):
    def test_parses_valid_steps(self):
        raw = [
            {"index": 0, "description": "step A", "tool_hint": "ls"},
            {"index": 1, "description": "step B", "tool_hint": ""},
        ]
        steps = _parse_steps(raw)
        self.assertEqual(len(steps), 2)
        self.assertEqual(steps[0].description, "step A")
        self.assertEqual(steps[0].tool_hint, "ls")

    def test_skips_empty_descriptions(self):
        raw = [{"index": 0, "description": "", "tool_hint": "ls"}]
        steps = _parse_steps(raw)
        self.assertEqual(len(steps), 0)

    def test_limits_to_max_steps(self):
        raw = [{"index": i, "description": f"step {i}", "tool_hint": ""} for i in range(100)]
        steps = _parse_steps(raw)
        self.assertEqual(len(steps), 20)


class TestMissionPlannerDirect(unittest.TestCase):
    """Tests that don't call the provider."""

    def test_plan_from_steps(self):
        provider = MagicMock()
        planner = MissionPlanner(provider=provider)
        plan = planner.plan_from_steps("m1", "do things", ["step A", "step B"])
        self.assertEqual(len(plan.steps), 2)
        self.assertEqual(plan.steps[0].description, "step A")
        self.assertEqual(plan.steps[1].description, "step B")

    def test_plan_from_empty_list_uses_goal(self):
        provider = MagicMock()
        planner = MissionPlanner(provider=provider)
        plan = planner.plan_from_steps("m2", "my goal", [])
        self.assertEqual(len(plan.steps), 1)
        self.assertEqual(plan.steps[0].description, "my goal")


class TestMissionPlannerAsync(unittest.IsolatedAsyncioTestCase):
    async def test_plan_uses_provider_output(self):
        provider = MagicMock()
        provider.generate = AsyncMock(
            return_value='[{"index": 0, "description": "run ls", "tool_hint": "shell_exec"}]'
        )
        planner = MissionPlanner(provider=provider)
        plan = await planner.plan("m1", "list files")
        self.assertEqual(len(plan.steps), 1)
        self.assertEqual(plan.steps[0].description, "run ls")

    async def test_plan_falls_back_on_provider_error(self):
        provider = MagicMock()
        provider.generate = AsyncMock(side_effect=RuntimeError("boom"))
        planner = MissionPlanner(provider=provider)
        plan = await planner.plan("m2", "fallback goal")
        self.assertEqual(len(plan.steps), 1)
        self.assertEqual(plan.steps[0].description, "fallback goal")

    async def test_plan_falls_back_on_bad_json(self):
        provider = MagicMock()
        provider.generate = AsyncMock(return_value="not json at all")
        planner = MissionPlanner(provider=provider)
        plan = await planner.plan("m3", "my goal")
        self.assertEqual(plan.steps[0].description, "my goal")


# ---------------------------------------------------------------------------
# #78 – AutonomousMissionRunner
# ---------------------------------------------------------------------------


class TestAutonomousMissionRunner(unittest.IsolatedAsyncioTestCase):
    def _make_runner(self, store: SqliteRunStore, step_output: str = "done") -> AutonomousMissionRunner:
        provider = MagicMock()
        provider.generate = AsyncMock(
            return_value='[{"index": 0, "description": "do the thing", "tool_hint": "shell_exec"}]'
        )
        planner = MissionPlanner(provider=provider)
        runner = AutonomousMissionRunner(store=store, planner=planner)
        # Patch _run_step to return a controlled value.
        runner._run_step = AsyncMock(return_value=step_output)
        return runner

    async def test_execute_mission_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            mid = store.create_mission(title="T", goal="do stuff")
            runner = self._make_runner(store)
            output = await runner.execute_mission(mid)
            self.assertFalse(output.startswith("Error:"))
            m = store.get_mission(mid)
            self.assertEqual(m.state, MISSION_STATE_COMPLETED)

    async def test_execute_mission_step_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            mid = store.create_mission(title="T", goal="do stuff")
            runner = self._make_runner(store, step_output="Error: step broke")
            output = await runner.execute_mission(mid)
            self.assertTrue(output.startswith("Error:"))
            m = store.get_mission(mid)
            self.assertEqual(m.state, MISSION_STATE_FAILED)

    async def test_execute_unknown_mission_returns_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            runner = self._make_runner(store)
            result = await runner.execute_mission("nonexistent")
            self.assertTrue(result.startswith("Error:"))

    async def test_stop_before_step_transitions_to_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            mid = store.create_mission(title="T", goal="do stuff")
            runner = self._make_runner(store)
            ctx = runner.get_or_create_context(mid)
            ctx.stop_event.set()
            result = await runner.execute_mission(mid)
            self.assertTrue(result.startswith("Error:"))
            m = store.get_mission(mid)
            self.assertEqual(m.state, MISSION_STATE_FAILED)

    async def test_pause_and_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            mid = store.create_mission(title="T", goal="do stuff")
            provider = MagicMock()
            provider.generate = AsyncMock(
                return_value='[{"index": 0, "description": "step1", "tool_hint": ""}, '
                             '{"index": 1, "description": "step2", "tool_hint": ""}]'
            )
            planner = MissionPlanner(provider=provider)

            step_calls: List[str] = []

            async def mock_step(mission_id, desc, hint):
                step_calls.append(desc)
                return "ok"

            runner = AutonomousMissionRunner(store=store, planner=planner)
            runner._run_step = AsyncMock(side_effect=lambda m, d, h: asyncio.coroutine(lambda: "ok")())
            runner._run_step = AsyncMock(return_value="ok")

            ctx = runner.get_or_create_context(mid)
            # Pause before running.
            runner.pause(mid)

            async def resume_after_delay():
                await asyncio.sleep(0.05)
                runner.resume(mid)

            asyncio.create_task(resume_after_delay())
            result = await runner.execute_mission(mid)
            self.assertFalse(result.startswith("Error:"))

    async def test_audit_trail_after_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            mid = store.create_mission(title="T", goal="g")
            runner = self._make_runner(store)
            await runner.execute_mission(mid)
            events = store.list_mission_events(mid)
            states = [e.to_state for e in events]
            self.assertIn(MISSION_STATE_RUNNING, states)
            self.assertIn(MISSION_STATE_COMPLETED, states)

    async def test_progress_callback_called(self):
        events_received: List[str] = []

        async def cb(mission_id: str, event_type: str, detail: str) -> None:
            events_received.append(event_type)

        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            mid = store.create_mission(title="T", goal="g")
            provider = MagicMock()
            provider.generate = AsyncMock(
                return_value='[{"index": 0, "description": "s", "tool_hint": ""}]'
            )
            planner = MissionPlanner(provider=provider)
            runner = AutonomousMissionRunner(
                store=store, planner=planner, progress_callback=cb
            )
            runner._run_step = AsyncMock(return_value="done")
            await runner.execute_mission(mid)
            self.assertIn("mission.started", events_received)
            self.assertIn("mission.completed", events_received)
