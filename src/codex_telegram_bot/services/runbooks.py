"""Autonomous mission runbooks and chaos tests (EPIC 10, issue #97).

This module provides:

1. ``Runbook`` – a named sequence of remediation steps with a trigger
   condition (e.g. "mission stuck in running for > 30 min").

2. ``RunbookRegistry`` – holds all known runbooks; evaluates which ones
   should fire for the current system state.

3. Chaos scenario helpers – callables that inject faults into a test
   environment so reliability properties can be verified.

Usage (runbooks)::

    registry = RunbookRegistry(store=store)
    registry.register(RUNBOOK_STUCK_MISSION)
    fired = await registry.evaluate_all()

Usage (chaos in tests)::

    async with chaos_watchdog_failure(runner, mission_id):
        # watchdog kill is injected; assert the mission lands in 'failed'
        ...
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator, Callable, Coroutine, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Runbook model
# ---------------------------------------------------------------------------

@dataclass
class RunbookResult:
    runbook_name: str
    triggered: bool
    actions_taken: List[str]
    error: Optional[str] = None


@dataclass
class Runbook:
    """A named remediation procedure.

    ``check`` receives the store and returns True if the runbook should fire.
    ``remedy`` performs the actual remediation steps and returns a list of
    human-readable action strings.
    """
    name: str
    description: str
    check: Callable[["SqliteRunStore"], bool]        # type: ignore[name-defined]
    remedy: Callable[["SqliteRunStore"], List[str]]  # type: ignore[name-defined]


class RunbookRegistry:
    """Evaluate and execute runbooks against the current system state."""

    def __init__(self, store: "SqliteRunStore") -> None:  # type: ignore[name-defined]
        self._store = store
        self._runbooks: List[Runbook] = []

    def register(self, runbook: Runbook) -> None:
        self._runbooks.append(runbook)

    def evaluate_all(self) -> List[RunbookResult]:
        """Check every registered runbook and execute any that trigger."""
        results: List[RunbookResult] = []
        for rb in self._runbooks:
            try:
                triggered = rb.check(self._store)
            except Exception as exc:
                results.append(RunbookResult(rb.name, False, [], str(exc)))
                continue
            if not triggered:
                results.append(RunbookResult(rb.name, False, []))
                continue
            try:
                actions = rb.remedy(self._store)
                logger.info("runbook %r fired: %s", rb.name, "; ".join(actions))
                results.append(RunbookResult(rb.name, True, actions))
            except Exception as exc:
                logger.exception("runbook %r remedy failed", rb.name)
                results.append(RunbookResult(rb.name, True, [], str(exc)))
        return results


# ---------------------------------------------------------------------------
# Built-in runbooks
# ---------------------------------------------------------------------------

def _stuck_mission_check(store: "SqliteRunStore", threshold_min: int = 60) -> bool:  # type: ignore[name-defined]
    """Return True if any mission has been 'running' for > threshold_min."""
    from codex_telegram_bot.domain.missions import MISSION_STATE_RUNNING
    missions = store.list_missions(state=MISSION_STATE_RUNNING)
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=threshold_min)
    for m in missions:
        events = store.list_mission_events(m.mission_id)
        running_events = [e for e in events if e.to_state == MISSION_STATE_RUNNING]
        if running_events:
            latest = max(running_events, key=lambda e: e.created_at)
            if latest.created_at < cutoff:
                return True
    return False


def _stuck_mission_remedy(store: "SqliteRunStore") -> List[str]:  # type: ignore[name-defined]
    """Mark stuck running missions as failed."""
    from codex_telegram_bot.domain.missions import MISSION_STATE_RUNNING
    missions = store.list_missions(state=MISSION_STATE_RUNNING)
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=60)
    actions: List[str] = []
    for m in missions:
        events = store.list_mission_events(m.mission_id)
        running_events = [e for e in events if e.to_state == MISSION_STATE_RUNNING]
        if running_events:
            latest = max(running_events, key=lambda e: e.occurred_at)
            if latest.occurred_at < cutoff:
                store.transition_mission(m.mission_id, "failed",
                                         reason="runbook: stuck-mission timeout")
                actions.append(f"failed stuck mission {m.mission_id[:8]}")
    return actions or ["no stuck missions found"]


RUNBOOK_STUCK_MISSION = Runbook(
    name="stuck-mission",
    description="Fail missions stuck in 'running' for more than 60 minutes.",
    check=_stuck_mission_check,
    remedy=_stuck_mission_remedy,
)


def _high_error_rate_check(store: "SqliteRunStore", threshold: float = 0.5) -> bool:  # type: ignore[name-defined]
    """Return True if > threshold of recent terminal missions failed."""
    from codex_telegram_bot.domain.missions import MISSION_STATE_COMPLETED, MISSION_STATE_FAILED
    window = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    completed = store.count_mission_events_since("running\u2192completed", window)
    failed = store.count_mission_events_since("running\u2192failed", window)
    total = completed + failed
    return (total > 0) and (failed / total) > threshold


def _high_error_rate_remedy(store: "SqliteRunStore") -> List[str]:  # type: ignore[name-defined]
    """Log and surface the alert (human investigation required)."""
    return ["high-error-rate alert raised – manual investigation required"]


RUNBOOK_HIGH_ERROR_RATE = Runbook(
    name="high-error-rate",
    description="Alert when >50% of missions in the last hour failed.",
    check=_high_error_rate_check,
    remedy=_high_error_rate_remedy,
)


# ---------------------------------------------------------------------------
# Chaos scenario helpers (for use in tests)
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def chaos_provider_failure(provider: Any, after_calls: int = 1):
    """Context manager that makes a mock provider raise after N calls."""
    call_count = 0
    original = provider.generate

    async def _flaky(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count > after_calls:
            raise RuntimeError("chaos: provider failure injected")
        return await original(*args, **kwargs)

    provider.generate = _flaky
    try:
        yield
    finally:
        provider.generate = original


@contextlib.contextmanager
def chaos_store_failure(store: Any, method: str = "upsert_memory_entry", after_calls: int = 0):
    """Context manager that makes a store method raise after N calls."""
    call_count = 0
    original = getattr(store, method)

    def _flaky(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count > after_calls:
            raise RuntimeError(f"chaos: {method} failure injected")
        return original(*args, **kwargs)

    setattr(store, method, _flaky)
    try:
        yield
    finally:
        setattr(store, method, original)


class ChaosChecklist:
    """Reliability checklist: collect pass/fail results for chaos scenarios."""

    def __init__(self) -> None:
        self._results: List[dict] = []

    def record(self, scenario: str, passed: bool, note: str = "") -> None:
        self._results.append({"scenario": scenario, "passed": passed, "note": note})
        status = "PASS" if passed else "FAIL"
        logger.info("chaos-checklist: [%s] %s  %s", status, scenario, note)

    def all_passed(self) -> bool:
        return all(r["passed"] for r in self._results)

    def summary(self) -> str:
        passed = sum(1 for r in self._results if r["passed"])
        total = len(self._results)
        lines = [f"Reliability checklist: {passed}/{total} passed"]
        for r in self._results:
            mark = "✓" if r["passed"] else "✗"
            lines.append(f"  {mark} {r['scenario']}" + (f" — {r['note']}" if r["note"] else ""))
        return "\n".join(lines)

    @property
    def results(self) -> List[dict]:
        return list(self._results)


from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from codex_telegram_bot.persistence.sqlite_store import SqliteRunStore
