"""Session retention and pruning policy (Parity Epic 1).

Rules applied in order during each ``apply()`` sweep:
- Sessions that have not been updated in ``archive_after_idle_days`` days
  and are still ``active`` are transitioned to ``archived``.
- Archived sessions older than ``delete_after_days`` days are hard-deleted
  together with their message history.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from codex_telegram_bot.persistence.sqlite_store import SqliteRunStore


@dataclass
class RetentionResult:
    archived_idle: int
    pruned_old: int
    elapsed_ms: float


class SessionRetentionPolicy:
    """Applies configurable retention rules to Telegram sessions."""

    def __init__(
        self,
        store: "SqliteRunStore",
        archive_after_idle_days: int = 30,
        delete_after_days: int = 90,
    ) -> None:
        self._store = store
        self._archive_after_idle_days = max(1, int(archive_after_idle_days))
        self._delete_after_days = max(1, int(delete_after_days))

    @property
    def archive_after_idle_days(self) -> int:
        return self._archive_after_idle_days

    @property
    def delete_after_days(self) -> int:
        return self._delete_after_days

    def apply(self) -> RetentionResult:
        """Run the retention sweep and return counts of sessions affected."""
        t0 = datetime.now(timezone.utc).timestamp()
        archived = self._store.archive_idle_sessions(
            idle_days=self._archive_after_idle_days,
        )
        pruned = self._store.prune_archived_sessions(
            older_than_days=self._delete_after_days,
        )
        elapsed_ms = (datetime.now(timezone.utc).timestamp() - t0) * 1000
        return RetentionResult(
            archived_idle=archived,
            pruned_old=pruned,
            elapsed_ms=elapsed_ms,
        )
