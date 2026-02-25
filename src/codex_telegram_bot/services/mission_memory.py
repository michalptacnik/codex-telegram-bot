"""Durable mission memory store (EPIC 8, issue #85).

Provides a clean service boundary over the raw SQLite persistence for
mission memory entries.  Handles retention policy (max entries per mission,
TTL expiry) and a query API for retrieval by key / kind / tag.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import List, Optional

from codex_telegram_bot.domain.memory import (
    MEMORY_KINDS,
    MemoryEntry,
)

logger = logging.getLogger(__name__)

_DEFAULT_MAX_ENTRIES_PER_MISSION = 200
_DEFAULT_MIN_IMPORTANCE_TO_KEEP = 0   # keep everything by default; compaction raises this


class MissionMemoryService:
    """CRUD + query operations over the mission memory store.

    This service talks to SqliteRunStore via the store reference; it does
    not own its own DB connection so it can be composed freely.

    Usage::

        svc = MissionMemoryService(store=store)
        svc.remember(mission_id="m1", kind="fact", key="lang", value="Python")
        entries = svc.recall(mission_id="m1", kind="fact")
    """

    def __init__(
        self,
        store: "SqliteRunStore",  # type: ignore[name-defined]
        max_entries_per_mission: int = _DEFAULT_MAX_ENTRIES_PER_MISSION,
    ) -> None:
        self._store = store
        self._max = max_entries_per_mission

    def remember(
        self,
        mission_id: str,
        kind: str,
        key: str,
        value: str,
        tags: Optional[List[str]] = None,
        importance: int = 5,
        expires_at: Optional[datetime] = None,
    ) -> MemoryEntry:
        """Persist a memory entry and enforce retention limit."""
        if kind not in MEMORY_KINDS:
            raise ValueError(f"Unknown memory kind: {kind!r}")
        entry = self._store.upsert_memory_entry(
            mission_id=mission_id,
            kind=kind,
            key=key,
            value=value,
            tags=tags or [],
            importance=importance,
            expires_at=expires_at,
        )
        self._enforce_limit(mission_id)
        return entry

    def recall(
        self,
        mission_id: str,
        kind: Optional[str] = None,
        key: Optional[str] = None,
        tag: Optional[str] = None,
        limit: int = 50,
        include_expired: bool = False,
    ) -> List[MemoryEntry]:
        """Retrieve memory entries with optional filters."""
        return self._store.list_memory_entries(
            mission_id=mission_id,
            kind=kind,
            key=key,
            tag=tag,
            limit=limit,
            include_expired=include_expired,
        )

    def forget(self, entry_id: str) -> bool:
        """Delete a single entry.  Returns True if it existed."""
        return self._store.delete_memory_entry(entry_id)

    def forget_mission(self, mission_id: str) -> int:
        """Delete all memory entries for a mission.  Returns count deleted."""
        return self._store.delete_mission_memory(mission_id)

    def expire_old(self) -> int:
        """Purge entries whose expires_at is in the past.  Returns count removed."""
        return self._store.expire_memory_entries(datetime.now(timezone.utc).isoformat())

    def entry_count(self, mission_id: str) -> int:
        return self._store.count_memory_entries(mission_id)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _enforce_limit(self, mission_id: str) -> None:
        """If over limit, drop the lowest-importance expired entries first,
        then lowest-importance oldest."""
        count = self._store.count_memory_entries(mission_id)
        if count <= self._max:
            return
        excess = count - self._max
        self._store.trim_memory_entries(mission_id, drop_count=excess)


# Avoid circular import
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from codex_telegram_bot.persistence.sqlite_store import SqliteRunStore
