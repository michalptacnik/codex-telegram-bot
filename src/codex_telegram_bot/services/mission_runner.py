"""Autonomous mission execution loop (EPIC 6, issue #78).

The AutonomousMissionRunner:
- Accepts a MissionRecord and runs it to completion.
- Uses MissionPlanner to decompose the goal into steps.
- Executes each step via the AgentService tool loop.
- Transitions the MissionRecord through its state machine.
- Supports pause, resume, and stop via asyncio.Event signals.
- Checkpoints progress so re-runs skip already-completed steps.
- Integrates with MissionScheduler for recurring missions.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Dict, Optional

from codex_telegram_bot.domain.missions import (
    MISSION_STATE_BLOCKED,
    MISSION_STATE_COMPLETED,
    MISSION_STATE_FAILED,
    MISSION_STATE_PAUSED,
    MISSION_STATE_RUNNING,
    MissionPlan,
)
from codex_telegram_bot.persistence.sqlite_store import SqliteRunStore
from codex_telegram_bot.services.mission_planner import MissionPlanner
from codex_telegram_bot.services.mission_scheduler import MissionScheduler

logger = logging.getLogger(__name__)

# Callback type: (mission_id, event_type, detail) -> None (fire and forget)
ProgressCallback = Callable[[str, str, str], Awaitable[None]]

_MAX_STEP_RETRIES = 2


@dataclass
class _StepState:
    index: int
    status: str = "pending"   # pending | running | completed | failed
    output: str = ""


@dataclass
class MissionRunContext:
    """Runtime control handles for a running mission."""
    mission_id: str
    pause_event: asyncio.Event = field(default_factory=asyncio.Event)
    resume_event: asyncio.Event = field(default_factory=asyncio.Event)
    stop_event: asyncio.Event = field(default_factory=asyncio.Event)

    def __post_init__(self) -> None:
        # resume_event starts set (not paused).
        self.resume_event.set()


class AutonomousMissionRunner:
    """Run missions autonomously, managing state transitions and retry."""

    def __init__(
        self,
        store: SqliteRunStore,
        planner: MissionPlanner,
        scheduler: Optional[MissionScheduler] = None,
        progress_callback: Optional[ProgressCallback] = None,
    ) -> None:
        self._store = store
        self._planner = planner
        self._scheduler = scheduler
        self._progress_callback = progress_callback
        # In-memory step state per mission run (keyed by mission_id).
        self._run_states: Dict[str, Dict[int, _StepState]] = {}
        # Per-mission control handles.
        self._contexts: Dict[str, MissionRunContext] = {}

    # ------------------------------------------------------------------
    # Public control API
    # ------------------------------------------------------------------

    def get_or_create_context(self, mission_id: str) -> MissionRunContext:
        if mission_id not in self._contexts:
            self._contexts[mission_id] = MissionRunContext(mission_id=mission_id)
        return self._contexts[mission_id]

    def pause(self, mission_id: str) -> bool:
        ctx = self._contexts.get(mission_id)
        if not ctx:
            return False
        ctx.pause_event.set()
        ctx.resume_event.clear()
        return True

    def resume(self, mission_id: str) -> bool:
        ctx = self._contexts.get(mission_id)
        if not ctx:
            return False
        ctx.pause_event.clear()
        ctx.resume_event.set()
        return True

    def stop(self, mission_id: str) -> bool:
        ctx = self._contexts.get(mission_id)
        if not ctx:
            return False
        ctx.stop_event.set()
        ctx.resume_event.set()  # unblock any wait
        return True

    # ------------------------------------------------------------------
    # Execution entry point (used by MissionScheduler executor callback)
    # ------------------------------------------------------------------

    async def execute_mission(self, mission_id: str) -> str:
        """Execute a mission from idle through completion.

        Returns the final output string or "Error: ..." on failure.
        This method is designed to be passed as the executor to MissionScheduler.
        """
        mission = self._store.get_mission(mission_id)
        if mission is None:
            return f"Error: mission {mission_id} not found."

        ctx = self.get_or_create_context(mission_id)
        ctx.pause_event.clear()
        ctx.resume_event.set()

        # Honour a pre-set stop (e.g. cancelled before execution started).
        if ctx.stop_event.is_set():
            try:
                self._store.transition_mission(mission_id, MISSION_STATE_FAILED, "stopped before start")
            except Exception:
                pass
            return "Error: mission stopped before execution."

        # Transition to running.
        try:
            self._store.transition_mission(mission_id, MISSION_STATE_RUNNING, "execution started")
        except Exception as exc:
            return f"Error: could not start mission: {exc}"

        await self._emit(mission_id, "mission.started", "")

        # Plan the mission.
        context_data = {}
        try:
            import json
            context_data = json.loads(mission.context_json or "{}")
        except Exception:
            pass

        try:
            plan: MissionPlan = await self._planner.plan(
                mission_id=mission_id, goal=mission.goal, context=context_data
            )
        except Exception as exc:
            logger.exception("mission=%s planning failed", mission_id)
            self._store.transition_mission(mission_id, MISSION_STATE_FAILED, f"planning error: {exc}")
            await self._emit(mission_id, "mission.failed", f"planning error: {exc}")
            return f"Error: planning failed: {exc}"

        await self._emit(mission_id, "mission.planned", f"{len(plan.steps)} steps")

        # Initialise step states (skip already completed from prior run).
        if mission_id not in self._run_states:
            self._run_states[mission_id] = {}
        step_states = self._run_states[mission_id]

        for step in plan.steps:
            if step.index not in step_states:
                step_states[step.index] = _StepState(index=step.index)

        # Execute steps in order.
        all_outputs = []
        for step in plan.steps:
            ss = step_states[step.index]
            if ss.status == "completed":
                all_outputs.append(ss.output)
                await self._emit(mission_id, "step.skipped", f"step {step.index} already done")
                continue

            # --- Pause/stop check ---
            if ctx.stop_event.is_set():
                self._store.transition_mission(mission_id, MISSION_STATE_FAILED, "stopped by operator")
                await self._emit(mission_id, "mission.stopped", "")
                return "Error: mission stopped by operator."

            if ctx.pause_event.is_set():
                self._store.transition_mission(mission_id, MISSION_STATE_PAUSED, "paused by operator")
                await self._emit(mission_id, "mission.paused", f"before step {step.index}")
                # Wait until resumed or stopped.
                await ctx.resume_event.wait()
                if ctx.stop_event.is_set():
                    self._store.transition_mission(mission_id, MISSION_STATE_FAILED, "stopped while paused")
                    await self._emit(mission_id, "mission.stopped", "")
                    return "Error: mission stopped while paused."
                self._store.transition_mission(mission_id, MISSION_STATE_RUNNING, "resumed")
                await self._emit(mission_id, "mission.resumed", f"resuming at step {step.index}")

            # --- Execute step with retries ---
            ss.status = "running"
            step_output = await self._execute_step_with_retry(
                mission_id=mission_id,
                step_index=step.index,
                step_description=step.description,
                tool_hint=step.tool_hint,
                ctx=ctx,
            )

            if step_output.startswith("Error:"):
                ss.status = "failed"
                ss.output = step_output
                self._store.transition_mission(
                    mission_id, MISSION_STATE_BLOCKED,
                    f"step {step.index} failed: {step_output[:120]}"
                )
                await self._emit(mission_id, "step.failed", f"step={step.index}")
                # Transition back to failed (blocked is a transient state here).
                self._store.transition_mission(mission_id, MISSION_STATE_FAILED, step_output[:200])
                await self._emit(mission_id, "mission.failed", step_output[:200])
                return step_output

            ss.status = "completed"
            ss.output = step_output
            all_outputs.append(step_output)
            await self._emit(mission_id, "step.completed", f"step={step.index}")

        # All steps completed successfully.
        self._store.transition_mission(mission_id, MISSION_STATE_COMPLETED, "all steps done")
        # Reset step states so a recurring run starts fresh.
        self._run_states.pop(mission_id, None)
        self._store.reset_mission_retry(mission_id)
        await self._emit(mission_id, "mission.completed", f"{len(plan.steps)} steps")
        return "\n".join(all_outputs).strip() or "Mission completed."

    # ------------------------------------------------------------------
    # Scheduler integration helpers
    # ------------------------------------------------------------------

    async def schedule_mission(self, mission_id: str) -> None:
        """Register the mission with the MissionScheduler (if attached)."""
        if self._scheduler is None:
            return
        mission = self._store.get_mission(mission_id)
        if mission is None:
            return
        await self._scheduler.schedule(
            mission_id=mission_id,
            interval_sec=mission.schedule_interval_sec,
            retry_limit=mission.retry_limit,
            max_concurrency=mission.max_concurrency,
        )

    async def unschedule_mission(self, mission_id: str) -> None:
        if self._scheduler is None:
            return
        await self._scheduler.cancel(mission_id)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _execute_step_with_retry(
        self,
        mission_id: str,
        step_index: int,
        step_description: str,
        tool_hint: str,
        ctx: MissionRunContext,
    ) -> str:
        """Run a single step, retrying up to _MAX_STEP_RETRIES times."""
        for attempt in range(_MAX_STEP_RETRIES + 1):
            if ctx.stop_event.is_set():
                return "Error: stopped."
            await self._emit(
                mission_id,
                "step.started",
                f"step={step_index} attempt={attempt} hint={tool_hint}",
            )
            output = await self._run_step(
                mission_id=mission_id,
                step_description=step_description,
                tool_hint=tool_hint,
            )
            if not output.startswith("Error:") or attempt >= _MAX_STEP_RETRIES:
                return output
            logger.info(
                "mission=%s step=%d attempt %d/%d failed, retrying",
                mission_id, step_index, attempt + 1, _MAX_STEP_RETRIES,
            )
        return output  # type: ignore[return-value]

    async def _run_step(self, mission_id: str, step_description: str, tool_hint: str) -> str:
        """Concrete step execution.

        Subclasses or injection points can override this to plug in the real
        AgentService tool loop.  The default implementation is a no-op stub
        that returns success so the runner can be tested in isolation.
        """
        # This default stub is intentionally minimal so tests can run
        # without a live provider.  Production code should subclass or
        # inject a real executor via the factory pattern used in app_container.
        return f"[stub] executed: {step_description}"

    async def _emit(self, mission_id: str, event_type: str, detail: str) -> None:
        if self._progress_callback is None:
            return
        try:
            await self._progress_callback(mission_id, event_type, detail)
        except Exception:
            logger.debug("progress callback error for mission=%s event=%s", mission_id, event_type)


class AgentServiceMissionRunner(AutonomousMissionRunner):
    """AutonomousMissionRunner that delegates step execution to AgentService.

    This concrete subclass is the production-ready version that wires
    the step descriptions through the full agent loop.
    """

    ExecutorFn = Callable[[str, str], Awaitable[str]]

    def __init__(
        self,
        store: SqliteRunStore,
        planner: MissionPlanner,
        step_executor: "AgentServiceMissionRunner.ExecutorFn",
        scheduler: Optional[MissionScheduler] = None,
        progress_callback: Optional[ProgressCallback] = None,
    ) -> None:
        super().__init__(
            store=store,
            planner=planner,
            scheduler=scheduler,
            progress_callback=progress_callback,
        )
        self._step_executor = step_executor

    async def _run_step(self, mission_id: str, step_description: str, tool_hint: str) -> str:
        prompt = step_description
        if tool_hint:
            prompt = f"[tool_hint={tool_hint}] {step_description}"
        try:
            return await self._step_executor(mission_id, prompt)
        except Exception as exc:
            logger.exception("mission=%s step executor error", mission_id)
            return f"Error: step executor exception: {exc}"
