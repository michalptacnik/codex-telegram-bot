"""Periodic summarisation and compaction (EPIC 8, issue #87).

The MemoryCompactor periodically:
  1. Reads all memory entries for each mission.
  2. Calls the provider to produce a compact narrative summary.
  3. Stores the summary in mission_summaries.
  4. Optionally deletes the raw entries it covered (compaction).

This keeps the memory store bounded while preserving key context.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Awaitable, Callable, List, Optional

from codex_telegram_bot.domain.memory import MemoryEntry, MissionSummary

logger = logging.getLogger(__name__)

_SUMMARISE_SYSTEM_PROMPT = """\
You are a mission memory summariser.  Given a list of key facts, decisions, and
notes gathered during a mission, write a concise prose summary (≤ 300 words).
Focus on outcomes, key decisions, and important context for future runs.
Return ONLY the summary text — no headings, no bullet points.
""".strip()

_MAX_ENTRIES_PER_SUMMARY = 100   # cap to keep prompts manageable


@dataclass(frozen=True)
class CompactionConfig:
    interval_sec: float = 3600.0    # how often to run
    min_entries: int = 10           # don't summarise unless at least this many entries
    delete_after_compact: bool = True


class MemoryCompactor:
    """Async background compactor for mission memory.

    Usage::

        compactor = MemoryCompactor(store=store, provider=provider)
        await compactor.start()
        # ...
        await compactor.stop()

        # Or trigger manually for a single mission:
        summary = await compactor.compact_mission("m1")
    """

    def __init__(
        self,
        store: "SqliteRunStore",        # type: ignore[name-defined]
        provider: "ProviderAdapter",    # type: ignore[name-defined]
        config: Optional[CompactionConfig] = None,
    ) -> None:
        self._store = store
        self._provider = provider
        self._config = config or CompactionConfig()
        self._task: Optional[asyncio.Task] = None
        self._running = False

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="memory-compactor")
        logger.info("memory_compactor: started (interval=%.0fs)", self._config.interval_sec)

    async def stop(self) -> None:
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None

    async def compact_mission(self, mission_id: str) -> Optional[MissionSummary]:
        """Summarise and optionally compact memory for one mission."""
        entries = self._store.list_memory_entries(
            mission_id=mission_id,
            limit=_MAX_ENTRIES_PER_SUMMARY,
        )
        if len(entries) < self._config.min_entries:
            logger.debug(
                "memory_compactor: mission=%s only %d entries, skipping",
                mission_id, len(entries),
            )
            return None

        text = await self._summarise(entries)
        artifact_count = self._store.count_artifacts(mission_id)
        summary = self._store.save_mission_summary(
            mission_id=mission_id,
            text=text,
            memory_count=len(entries),
            artifact_count=artifact_count,
            compacted=self._config.delete_after_compact,
        )

        if self._config.delete_after_compact:
            deleted = self._store.delete_mission_memory(mission_id)
            logger.info(
                "memory_compactor: mission=%s compacted %d entries into summary %s",
                mission_id, deleted, summary.summary_id,
            )

        return summary

    async def compact_all(self) -> List[MissionSummary]:
        """Run compaction across all missions with enough entries."""
        from codex_telegram_bot.domain.missions import MISSION_STATE_COMPLETED, MISSION_STATE_FAILED
        summaries: List[MissionSummary] = []
        for state in (MISSION_STATE_COMPLETED, MISSION_STATE_FAILED):
            missions = self._store.list_missions(state=state)
            for mission in missions:
                if self._store.count_memory_entries(mission.mission_id) >= self._config.min_entries:
                    try:
                        s = await self.compact_mission(mission.mission_id)
                        if s:
                            summaries.append(s)
                    except Exception as exc:
                        logger.warning(
                            "memory_compactor: mission=%s compaction failed: %s",
                            mission.mission_id, exc,
                        )
        return summaries

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _loop(self) -> None:
        while self._running:
            try:
                await self.compact_all()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("memory_compactor: cycle error")
            try:
                await asyncio.sleep(self._config.interval_sec)
            except asyncio.CancelledError:
                break

    async def _summarise(self, entries: List[MemoryEntry]) -> str:
        lines = []
        for e in entries:
            lines.append(f"[{e.kind}] {e.key}: {e.value}")
        user_content = "\n".join(lines[:_MAX_ENTRIES_PER_SUMMARY])
        messages = [
            {"role": "system", "content": _SUMMARISE_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ]
        try:
            return await self._provider.generate(messages=messages, stream=False) or ""
        except Exception as exc:
            logger.warning("memory_compactor: provider summarise failed: %s", exc)
            # Fallback: join the first 10 entries as plain text
            return "Summary unavailable. Key entries:\n" + "\n".join(lines[:10])


from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from codex_telegram_bot.domain.contracts import ProviderAdapter
    from codex_telegram_bot.persistence.sqlite_store import SqliteRunStore
