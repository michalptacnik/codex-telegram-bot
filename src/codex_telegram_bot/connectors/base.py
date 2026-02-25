"""Lead source connector framework (EPIC 7, issue #81).

Defines the pluggable connector interface, typed lead record schema,
rate-limit helpers, and a connector registry.  Every concrete connector
(e.g. GitHubIssueConnector) implements the Connector protocol and returns
LeadRecord instances that flow into the dedup+scoring pipeline.
"""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, AsyncIterator, Dict, List, Optional, Protocol


# ---------------------------------------------------------------------------
# Core data types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IngestionCursor:
    """Opaque checkpoint stored between sync runs.

    ``value`` is connector-specific (ISO timestamp, page token, etag, etc.).
    """
    connector_id: str
    value: str
    updated_at: datetime


@dataclass(frozen=True)
class LeadRecord:
    """Normalised unit of work produced by any connector.

    Fields follow a minimal common schema; extra metadata lives in
    ``extra`` as a plain dict (JSON-serialisable).
    """
    # Stable identity: sha256(connector_id + ":" + source_id)
    lead_id: str
    connector_id: str
    source_id: str       # e.g. GitHub issue number as string
    title: str
    body: str
    url: str
    priority: int        # 0 = highest, 100 = lowest
    labels: List[str]
    created_at: datetime
    updated_at: datetime
    extra: Dict[str, Any] = field(default_factory=dict)

    def __hash__(self) -> int:  # needed because of mutable field `extra`
        return hash(self.lead_id)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, LeadRecord):
            return NotImplemented
        return self.lead_id == other.lead_id


def make_lead_id(connector_id: str, source_id: str) -> str:
    raw = f"{connector_id}:{source_id}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


# ---------------------------------------------------------------------------
# Connector protocol
# ---------------------------------------------------------------------------


class Connector(Protocol):
    """Pluggable interface for work-intake sources.

    Implementors should be stateless with respect to rate-limiting state;
    use RateLimiter below to wrap calls.
    """

    connector_id: str
    display_name: str

    async def fetch(
        self,
        cursor: Optional[IngestionCursor],
        limit: int = 100,
    ) -> tuple[List[LeadRecord], Optional[IngestionCursor]]:
        """Fetch a batch of leads since the cursor.

        Returns ``(leads, next_cursor)``.  ``next_cursor`` is ``None`` when
        there is nothing more to fetch in this cycle.
        """
        ...

    async def health(self) -> Dict[str, Any]:
        """Return a dict with ``{"ok": bool, ...}``."""
        ...


# ---------------------------------------------------------------------------
# Rate limiter (token-bucket style, per connector)
# ---------------------------------------------------------------------------


@dataclass
class RateLimiter:
    """Simple token-bucket rate limiter for connector calls.

    ``rate_per_sec`` tokens are replenished per second up to ``burst``.
    """
    rate_per_sec: float
    burst: int
    _tokens: float = field(init=False)
    _last_refill: float = field(init=False)

    def __post_init__(self) -> None:
        self._tokens = float(self.burst)
        self._last_refill = time.monotonic()

    def consume(self, n: int = 1) -> bool:
        """Try to consume ``n`` tokens.  Returns True if allowed."""
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(self.burst, self._tokens + elapsed * self.rate_per_sec)
        self._last_refill = now
        if self._tokens >= n:
            self._tokens -= n
            return True
        return False

    async def wait_and_consume(self, n: int = 1) -> None:
        """Async version: sleep until tokens are available, then consume."""
        import asyncio
        while not self.consume(n):
            await asyncio.sleep(1.0 / max(self.rate_per_sec, 0.01))


# ---------------------------------------------------------------------------
# Connector registry
# ---------------------------------------------------------------------------


class ConnectorRegistry:
    """Maintains a set of named connectors.

    Usage::

        registry = ConnectorRegistry()
        registry.register(GitHubIssueConnector(...))
        connector = registry.get("github_issues")
    """

    def __init__(self) -> None:
        self._connectors: Dict[str, Connector] = {}

    def register(self, connector: Connector) -> None:
        cid = (getattr(connector, "connector_id", "") or "").strip()
        if not cid:
            raise ValueError("connector_id is required.")
        self._connectors[cid] = connector

    def get(self, connector_id: str) -> Optional[Connector]:
        return self._connectors.get(connector_id)

    def all(self) -> List[Connector]:
        return list(self._connectors.values())

    def ids(self) -> List[str]:
        return sorted(self._connectors.keys())

    def __len__(self) -> int:
        return len(self._connectors)
