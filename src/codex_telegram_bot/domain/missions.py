"""Mission domain model and state machine for EPIC 6."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, FrozenSet, List, Optional, Tuple


# ---------------------------------------------------------------------------
# State constants
# ---------------------------------------------------------------------------

MISSION_STATE_IDLE = "idle"
MISSION_STATE_RUNNING = "running"
MISSION_STATE_BLOCKED = "blocked"
MISSION_STATE_FAILED = "failed"
MISSION_STATE_COMPLETED = "completed"
MISSION_STATE_PAUSED = "paused"

MISSION_STATES: FrozenSet[str] = frozenset(
    [
        MISSION_STATE_IDLE,
        MISSION_STATE_RUNNING,
        MISSION_STATE_BLOCKED,
        MISSION_STATE_FAILED,
        MISSION_STATE_COMPLETED,
        MISSION_STATE_PAUSED,
    ]
)

# Allowed (from_state, to_state) transitions.
_ALLOWED_TRANSITIONS: FrozenSet[Tuple[str, str]] = frozenset(
    [
        (MISSION_STATE_IDLE, MISSION_STATE_RUNNING),
        (MISSION_STATE_IDLE, MISSION_STATE_FAILED),
        (MISSION_STATE_RUNNING, MISSION_STATE_BLOCKED),
        (MISSION_STATE_RUNNING, MISSION_STATE_FAILED),
        (MISSION_STATE_RUNNING, MISSION_STATE_COMPLETED),
        (MISSION_STATE_RUNNING, MISSION_STATE_PAUSED),
        (MISSION_STATE_BLOCKED, MISSION_STATE_RUNNING),
        (MISSION_STATE_BLOCKED, MISSION_STATE_FAILED),
        (MISSION_STATE_PAUSED, MISSION_STATE_RUNNING),
        (MISSION_STATE_PAUSED, MISSION_STATE_FAILED),
        # Completed/failed are terminal; allow re-queuing by going back to idle.
        (MISSION_STATE_COMPLETED, MISSION_STATE_IDLE),
        (MISSION_STATE_FAILED, MISSION_STATE_IDLE),
    ]
)

# ---------------------------------------------------------------------------
# Domain records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MissionRecord:
    mission_id: str
    title: str
    goal: str
    state: str
    # Scheduling: None means run once; positive int = repeat interval in seconds.
    schedule_interval_sec: Optional[int]
    # Retry policy
    retry_limit: int
    retry_count: int
    # Concurrency: max parallel workers for this mission.
    max_concurrency: int
    # Timestamps
    created_at: datetime
    updated_at: datetime
    started_at: Optional[datetime]
    completed_at: Optional[datetime]
    # Optional context for the planner
    context_json: str  # JSON-encoded dict

    def is_terminal(self) -> bool:
        return self.state in {MISSION_STATE_COMPLETED, MISSION_STATE_FAILED}

    def is_recurring(self) -> bool:
        return self.schedule_interval_sec is not None and self.schedule_interval_sec > 0


@dataclass(frozen=True)
class MissionEventRecord:
    id: int
    mission_id: str
    from_state: str
    to_state: str
    reason: str
    created_at: datetime


@dataclass(frozen=True)
class MissionStep:
    index: int
    description: str
    tool_hint: str  # Optional hint for which tool to use, e.g. "shell_exec"


@dataclass(frozen=True)
class MissionPlan:
    mission_id: str
    goal: str
    steps: List[MissionStep]


# ---------------------------------------------------------------------------
# Transition validator
# ---------------------------------------------------------------------------


class MissionTransitionError(ValueError):
    pass


def validate_transition(from_state: str, to_state: str) -> None:
    """Raise MissionTransitionError if the transition is not allowed."""
    if from_state not in MISSION_STATES:
        raise MissionTransitionError(f"Unknown source state: '{from_state}'")
    if to_state not in MISSION_STATES:
        raise MissionTransitionError(f"Unknown target state: '{to_state}'")
    if (from_state, to_state) not in _ALLOWED_TRANSITIONS:
        raise MissionTransitionError(
            f"Transition '{from_state}' -> '{to_state}' is not allowed."
        )


def allowed_next_states(state: str) -> List[str]:
    """Return the list of states reachable from the given state."""
    return sorted(to for (frm, to) in _ALLOWED_TRANSITIONS if frm == state)
