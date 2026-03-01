"""Autonomy policy modes (EPIC 9, issue #89).

Defines four escalating autonomy levels and the tool allowlist matrix that
maps each mode to the set of tool names the agent may call without human
approval.  Runtime mode switches are recorded as audit events.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, FrozenSet, List, Optional


# ---------------------------------------------------------------------------
# Mode constants
# ---------------------------------------------------------------------------

AUTONOMY_OBSERVE_ONLY = "observe_only"
AUTONOMY_PROPOSE = "propose"
AUTONOMY_EXECUTE_LIMITED = "execute_limited"
AUTONOMY_EXECUTE_FULL = "execute_full"

AUTONOMY_MODES: FrozenSet[str] = frozenset(
    [
        AUTONOMY_OBSERVE_ONLY,
        AUTONOMY_PROPOSE,
        AUTONOMY_EXECUTE_LIMITED,
        AUTONOMY_EXECUTE_FULL,
    ]
)

# ---------------------------------------------------------------------------
# Tool allowlist matrix
# Each mode names the tools that may run without a human approval request.
# Tools *not* in the allowlist require an explicit approval or are blocked.
# ---------------------------------------------------------------------------

_READ_ONLY_TOOLS: FrozenSet[str] = frozenset(
    ["read_file", "git_status", "git_diff", "git_log", "ssh_detection", "web_search", "mcp_search"]
)

_LIMITED_WRITE_TOOLS: FrozenSet[str] = frozenset(
    _READ_ONLY_TOOLS | {"git_add", "git_commit", "write_file"}
)

_FULL_TOOLS: FrozenSet[str] = frozenset(
    _LIMITED_WRITE_TOOLS
    | {
        "shell_exec",
        "send_message",
        "github_comment",
        "github_close_issue",
        "github_create_issue",
    }
)

TOOL_ALLOWLIST: Dict[str, FrozenSet[str]] = {
    AUTONOMY_OBSERVE_ONLY: frozenset(),          # no tools auto-approved
    AUTONOMY_PROPOSE: _READ_ONLY_TOOLS,          # read-only only
    AUTONOMY_EXECUTE_LIMITED: _LIMITED_WRITE_TOOLS,
    AUTONOMY_EXECUTE_FULL: _FULL_TOOLS,
}

# Modes ordered from least to most autonomous (for comparison)
_MODE_RANK: Dict[str, int] = {
    AUTONOMY_OBSERVE_ONLY: 0,
    AUTONOMY_PROPOSE: 1,
    AUTONOMY_EXECUTE_LIMITED: 2,
    AUTONOMY_EXECUTE_FULL: 3,
}


def mode_rank(mode: str) -> int:
    return _MODE_RANK.get(mode, -1)


def is_tool_allowed(mode: str, tool_name: str) -> bool:
    """Return True if ``tool_name`` may execute without approval under ``mode``."""
    allowlist = TOOL_ALLOWLIST.get(mode, frozenset())
    return tool_name in allowlist


# ---------------------------------------------------------------------------
# Audit record
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AutonomyModeEvent:
    """Immutable record of a mode switch."""
    id: int
    from_mode: str
    to_mode: str
    actor: str       # user_id or "system"
    reason: str
    created_at: datetime


# ---------------------------------------------------------------------------
# Policy mode manager (in-memory with audit trail, persisted separately)
# ---------------------------------------------------------------------------


class AutonomyPolicyManager:
    """Manages the current autonomy mode and validates tool usage.

    Usage::

        mgr = AutonomyPolicyManager(initial_mode=AUTONOMY_PROPOSE)
        mgr.set_mode(AUTONOMY_EXECUTE_LIMITED, actor="admin", reason="approved by ops")
        allowed = mgr.is_tool_allowed("git_commit")  # True
        allowed = mgr.is_tool_allowed("shell_exec")  # False
    """

    def __init__(self, initial_mode: str = AUTONOMY_PROPOSE) -> None:
        if initial_mode not in AUTONOMY_MODES:
            raise ValueError(f"Unknown autonomy mode: {initial_mode!r}")
        self._mode = initial_mode
        self._history: List[AutonomyModeEvent] = []
        self._seq = 0

    @property
    def current_mode(self) -> str:
        return self._mode

    def set_mode(
        self, new_mode: str, actor: str = "system", reason: str = ""
    ) -> AutonomyModeEvent:
        if new_mode not in AUTONOMY_MODES:
            raise ValueError(f"Unknown autonomy mode: {new_mode!r}")
        self._seq += 1
        event = AutonomyModeEvent(
            id=self._seq,
            from_mode=self._mode,
            to_mode=new_mode,
            actor=actor,
            reason=reason,
            created_at=datetime.now(timezone.utc),
        )
        self._mode = new_mode
        self._history.append(event)
        return event

    def is_tool_allowed(self, tool_name: str) -> bool:
        return is_tool_allowed(self._mode, tool_name)

    def allowed_tools(self) -> FrozenSet[str]:
        return TOOL_ALLOWLIST.get(self._mode, frozenset())

    def mode_history(self) -> List[AutonomyModeEvent]:
        return list(self._history)
