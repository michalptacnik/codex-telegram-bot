"""Backend protocol abstraction for pluggable AI execution backends.

Defines a unified ``Backend`` protocol, a set of event dataclasses that every
backend must emit, and a ``BackendRegistry`` that loads and manages configured
backends at runtime.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import (
    Any,
    AsyncIterator,
    Dict,
    List,
    Optional,
    Protocol,
    runtime_checkable,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Unified event types
# ---------------------------------------------------------------------------


class EventKind(str, Enum):
    TEXT_DELTA = "text_delta"
    RUN_STARTED = "run_started"
    RUN_FINISHED = "run_finished"
    ERROR = "error"
    APPROVAL_REQUESTED = "approval_requested"
    TOOL_EVENT = "tool_event"


@dataclass(frozen=True)
class TextDelta:
    """Incremental text chunk produced by the backend."""

    kind: str = field(default=EventKind.TEXT_DELTA, init=False)
    run_id: str = ""
    delta: str = ""


@dataclass(frozen=True)
class RunStarted:
    """Emitted when a new run begins execution."""

    kind: str = field(default=EventKind.RUN_STARTED, init=False)
    run_id: str = ""
    backend_name: str = ""
    started_at: Optional[datetime] = None


@dataclass(frozen=True)
class RunFinished:
    """Emitted when a run completes (successfully or not)."""

    kind: str = field(default=EventKind.RUN_FINISHED, init=False)
    run_id: str = ""
    output: str = ""
    exit_code: int = 0
    finished_at: Optional[datetime] = None


@dataclass(frozen=True)
class Error:
    """Emitted when a backend encounters an error."""

    kind: str = field(default=EventKind.ERROR, init=False)
    run_id: str = ""
    message: str = ""
    recoverable: bool = False


@dataclass(frozen=True)
class ApprovalRequested:
    """Emitted when a backend requires user approval to continue.

    Mirrors the existing approvals UX (Accept once / Accept similar / Reject).
    """

    kind: str = field(default=EventKind.APPROVAL_REQUESTED, init=False)
    run_id: str = ""
    approval_id: str = ""
    command: str = ""
    risk_tier: str = "medium"
    explanation: str = ""


@dataclass(frozen=True)
class ToolEvent:
    """Minimal event describing tool invocation or result inside a run."""

    kind: str = field(default=EventKind.TOOL_EVENT, init=False)
    run_id: str = ""
    tool_name: str = ""
    tool_input: str = ""
    tool_output: str = ""


# Union of all event types for type-hint convenience.
BackendEvent = TextDelta | RunStarted | RunFinished | Error | ApprovalRequested | ToolEvent


# ---------------------------------------------------------------------------
# Approval actions (mirrors existing UX)
# ---------------------------------------------------------------------------


class ApprovalAction(str, Enum):
    ACCEPT_ONCE = "accept_once"
    ACCEPT_SIMILAR = "accept_similar"
    REJECT = "reject"


# ---------------------------------------------------------------------------
# Backend Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class Backend(Protocol):
    """Minimal protocol every AI execution backend must satisfy."""

    @property
    def name(self) -> str:
        """Unique backend identifier (e.g. ``"codex"``, ``"opencode"``)."""
        ...

    async def start_run(
        self,
        prompt: str,
        *,
        correlation_id: str = "",
        policy_profile: str = "balanced",
        workspace_root: str = "",
        instruction_paths: Optional[List[str]] = None,
    ) -> str:
        """Start a new run and return a ``run_id``."""
        ...

    async def stream_events(self, run_id: str) -> AsyncIterator[BackendEvent]:
        """Yield events for an active run.

        The iterator may block until new events are available and should
        terminate when the run finishes or is cancelled.
        """
        ...
        yield  # pragma: no cover – required for async generator protocol

    async def send_approval(
        self,
        run_id: str,
        approval_id: str,
        action: ApprovalAction,
    ) -> None:
        """Respond to an ``ApprovalRequested`` event."""
        ...

    async def cancel_run(self, run_id: str) -> None:
        """Cancel an active run. Idempotent if already finished."""
        ...

    async def close(self) -> None:
        """Release any resources held by the backend."""
        ...


# ---------------------------------------------------------------------------
# Backend Registry
# ---------------------------------------------------------------------------


class BackendNotFoundError(KeyError):
    """Raised when a requested backend name is not registered."""


class BackendRegistry:
    """Holds all known backends and tracks the active default.

    Usage::

        registry = BackendRegistry()
        registry.register(codex_backend)
        registry.set_active("codex")
        backend = registry.active()
    """

    def __init__(self, default_name: str = "codex") -> None:
        self._backends: Dict[str, Backend] = {}
        self._active_name: str = default_name

    def register(self, backend: Backend, *, make_active: bool = False) -> None:
        self._backends[backend.name] = backend
        if make_active or (backend.name == self._active_name and self._active_name not in self._backends):
            self._active_name = backend.name
        logger.info("backend_registry.register name=%s make_active=%s", backend.name, make_active)

    def unregister(self, name: str) -> None:
        if name not in self._backends:
            return
        del self._backends[name]
        if self._active_name == name:
            self._active_name = next(iter(self._backends), "")

    def get(self, name: str) -> Backend:
        try:
            return self._backends[name]
        except KeyError:
            raise BackendNotFoundError(
                f"Backend '{name}' not registered. Available: {list(self._backends.keys())}"
            )

    def active(self) -> Backend:
        """Return the currently active backend."""
        if self._active_name and self._active_name in self._backends:
            return self._backends[self._active_name]
        if self._backends:
            return next(iter(self._backends.values()))
        raise BackendNotFoundError("No backends registered.")

    @property
    def active_name(self) -> str:
        return self._active_name

    def set_active(self, name: str) -> None:
        if name not in self._backends:
            raise BackendNotFoundError(
                f"Backend '{name}' not registered. Available: {list(self._backends.keys())}"
            )
        self._active_name = name

    def list_backends(self) -> List[Dict[str, Any]]:
        return [
            {"name": name, "active": name == self._active_name}
            for name in self._backends
        ]

    async def close_all(self) -> None:
        for backend in self._backends.values():
            try:
                await backend.close()
            except Exception:
                logger.exception("Error closing backend %s", backend.name)
