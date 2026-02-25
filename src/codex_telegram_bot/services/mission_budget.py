"""Mission budgets and kill switches (EPIC 9, issue #90).

Tracks time, action-count, and cost budgets per mission run and enforces
hard stops when thresholds are exceeded.  A KillSwitch provides an
emergency stop that can halt any running mission immediately.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Awaitable, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Budget configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BudgetConfig:
    """Limits applied to a single mission run."""
    max_time_sec: Optional[float] = None     # wall-clock seconds
    max_actions: Optional[int] = None        # number of tool/step invocations
    max_cost_usd: Optional[float] = None     # estimated provider cost


@dataclass
class BudgetUsage:
    """Mutable usage counters for one mission run."""
    mission_id: str
    elapsed_sec: float = 0.0
    actions: int = 0
    cost_usd: float = 0.0
    start_time: float = field(default_factory=time.monotonic)
    breached: bool = False
    breach_reason: str = ""

    def record_action(self, cost_usd: float = 0.0) -> None:
        self.actions += 1
        self.cost_usd += cost_usd
        self.elapsed_sec = time.monotonic() - self.start_time


# ---------------------------------------------------------------------------
# Budget breach types
# ---------------------------------------------------------------------------


class BudgetBreachError(Exception):
    """Raised when a mission exceeds its budget."""
    def __init__(self, mission_id: str, reason: str) -> None:
        super().__init__(reason)
        self.mission_id = mission_id
        self.reason = reason


# ---------------------------------------------------------------------------
# Budget enforcer
# ---------------------------------------------------------------------------


AlertCallback = Callable[[str, str, str], Awaitable[None]]   # (mission_id, kind, detail)


class BudgetEnforcer:
    """Checks budgets before / after each action and raises on breach.

    Usage::

        cfg = BudgetConfig(max_time_sec=300, max_actions=50)
        enforcer = BudgetEnforcer()
        usage = enforcer.start(mission_id="m1", config=cfg)

        # Before each step:
        await enforcer.check("m1")       # raises BudgetBreachError on over-budget
        enforcer.record_action("m1", cost_usd=0.002)
    """

    def __init__(self, alert_callback: Optional[AlertCallback] = None) -> None:
        self._configs: Dict[str, BudgetConfig] = {}
        self._usages: Dict[str, BudgetUsage] = {}
        self._alert = alert_callback

    def start(self, mission_id: str, config: BudgetConfig) -> BudgetUsage:
        self._configs[mission_id] = config
        usage = BudgetUsage(mission_id=mission_id)
        self._usages[mission_id] = usage
        return usage

    def stop(self, mission_id: str) -> None:
        self._configs.pop(mission_id, None)
        self._usages.pop(mission_id, None)

    def record_action(self, mission_id: str, cost_usd: float = 0.0) -> None:
        usage = self._usages.get(mission_id)
        if usage:
            usage.record_action(cost_usd)

    async def check(self, mission_id: str) -> None:
        """Raise BudgetBreachError if any limit is exceeded."""
        usage = self._usages.get(mission_id)
        config = self._configs.get(mission_id)
        if usage is None or config is None:
            return

        usage.elapsed_sec = time.monotonic() - usage.start_time

        if config.max_time_sec is not None and usage.elapsed_sec >= config.max_time_sec:
            await self._breach(usage, f"time budget exceeded ({usage.elapsed_sec:.1f}s / {config.max_time_sec}s)")

        if config.max_actions is not None and usage.actions >= config.max_actions:
            await self._breach(usage, f"action budget exceeded ({usage.actions} / {config.max_actions})")

        if config.max_cost_usd is not None and usage.cost_usd >= config.max_cost_usd:
            await self._breach(usage, f"cost budget exceeded (${usage.cost_usd:.4f} / ${config.max_cost_usd:.4f})")

    async def _breach(self, usage: BudgetUsage, reason: str) -> None:
        if usage.breached:
            return  # only fire once
        usage.breached = True
        usage.breach_reason = reason
        logger.warning("mission=%s budget breach: %s", usage.mission_id, reason)
        if self._alert:
            try:
                await self._alert(usage.mission_id, "budget.breach", reason)
            except Exception:
                pass
        raise BudgetBreachError(usage.mission_id, reason)

    def get_usage(self, mission_id: str) -> Optional[BudgetUsage]:
        return self._usages.get(mission_id)

    def summary(self, mission_id: str) -> Dict:
        usage = self._usages.get(mission_id)
        config = self._configs.get(mission_id)
        if not usage:
            return {"mission_id": mission_id, "error": "not tracked"}
        return {
            "mission_id": mission_id,
            "elapsed_sec": round(usage.elapsed_sec, 2),
            "actions": usage.actions,
            "cost_usd": round(usage.cost_usd, 6),
            "breached": usage.breached,
            "breach_reason": usage.breach_reason,
            "limits": {
                "max_time_sec": config.max_time_sec if config else None,
                "max_actions": config.max_actions if config else None,
                "max_cost_usd": config.max_cost_usd if config else None,
            },
        }


# ---------------------------------------------------------------------------
# Kill switch
# ---------------------------------------------------------------------------


class KillSwitch:
    """Emergency stop for running missions.

    Maintains a set of kill tokens (one per mission).  Any part of the
    system that holds a reference to the KillSwitch can stop a mission by
    calling ``trigger(mission_id)``.  The mission runner polls
    ``is_killed(mission_id)`` before each step.
    """

    def __init__(self, alert_callback: Optional[AlertCallback] = None) -> None:
        self._killed: Dict[str, str] = {}   # mission_id → reason
        self._events: Dict[str, asyncio.Event] = {}
        self._alert = alert_callback

    def arm(self, mission_id: str) -> None:
        """Prepare a kill-switch slot for a mission (call before run)."""
        self._events[mission_id] = asyncio.Event()

    def disarm(self, mission_id: str) -> None:
        """Remove the kill slot after a mission completes."""
        self._killed.pop(mission_id, None)
        self._events.pop(mission_id, None)

    async def trigger(self, mission_id: str, reason: str = "kill switch triggered") -> None:
        """Immediately flag a mission for hard stop."""
        self._killed[mission_id] = reason
        ev = self._events.get(mission_id)
        if ev:
            ev.set()
        logger.warning("kill_switch: mission=%s triggered: %s", mission_id, reason)
        if self._alert:
            try:
                await self._alert(mission_id, "kill_switch.triggered", reason)
            except Exception:
                pass

    def is_killed(self, mission_id: str) -> bool:
        return mission_id in self._killed

    def kill_reason(self, mission_id: str) -> Optional[str]:
        return self._killed.get(mission_id)

    async def wait_for_kill(self, mission_id: str) -> str:
        """Await until the mission is killed; returns the reason."""
        ev = self._events.get(mission_id)
        if ev:
            await ev.wait()
        return self._killed.get(mission_id, "unknown")

    def active_missions(self) -> List[str]:
        return list(self._events.keys())
