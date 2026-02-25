"""Tests for EPIC 8: Long-Horizon Mission Memory.

Covers:
  #85 – Durable mission memory store
  #86 – Artifact and evidence index
  #87 – Periodic summarisation and compaction
"""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List
from unittest.mock import AsyncMock, MagicMock

from codex_telegram_bot.domain.memory import (
    ARTIFACT_KIND_FILE,
    ARTIFACT_KIND_URL,
    MEMORY_KIND_DECISION,
    MEMORY_KIND_FACT,
    MEMORY_KIND_NOTE,
    ArtifactRecord,
    MemoryEntry,
    MissionSummary,
)
from codex_telegram_bot.domain.missions import MISSION_STATE_RUNNING, MISSION_STATE_COMPLETED
from codex_telegram_bot.persistence.sqlite_store import SqliteRunStore
from codex_telegram_bot.services.artifact_index import ArtifactIndex
from codex_telegram_bot.services.memory_compactor import CompactionConfig, MemoryCompactor
from codex_telegram_bot.services.mission_memory import MissionMemoryService


def _make_store(tmp: str) -> SqliteRunStore:
    return SqliteRunStore(Path(tmp) / "test.db")


# ---------------------------------------------------------------------------
# #85 – Durable mission memory store
# ---------------------------------------------------------------------------


class TestMissionMemoryPersistence(unittest.TestCase):
    def test_upsert_and_recall(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            entry = store.upsert_memory_entry(
                mission_id="m1", kind="fact", key="language", value="Python",
                tags=["tech"], importance=7,
            )
            self.assertIsInstance(entry, MemoryEntry)
            self.assertEqual(entry.key, "language")
            entries = store.list_memory_entries("m1")
            self.assertEqual(len(entries), 1)

    def test_filter_by_kind(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            store.upsert_memory_entry("m1", "fact", "k1", "v1")
            store.upsert_memory_entry("m1", "decision", "k2", "v2")
            facts = store.list_memory_entries("m1", kind="fact")
            self.assertEqual(len(facts), 1)
            self.assertEqual(facts[0].kind, "fact")

    def test_filter_by_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            store.upsert_memory_entry("m1", "fact", "lang", "Python")
            store.upsert_memory_entry("m1", "fact", "lang", "Go")
            results = store.list_memory_entries("m1", key="lang")
            self.assertEqual(len(results), 2)

    def test_filter_by_tag(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            store.upsert_memory_entry("m1", "fact", "k", "v", tags=["infra"])
            store.upsert_memory_entry("m1", "fact", "k2", "v2", tags=["frontend"])
            results = store.list_memory_entries("m1", tag="infra")
            self.assertEqual(len(results), 1)

    def test_expires_at_filters_expired(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            past = datetime.now(timezone.utc) - timedelta(seconds=1)
            store.upsert_memory_entry("m1", "note", "k", "v", expires_at=past)
            store.upsert_memory_entry("m1", "note", "k2", "v2")
            live = store.list_memory_entries("m1", include_expired=False)
            self.assertEqual(len(live), 1)
            all_entries = store.list_memory_entries("m1", include_expired=True)
            self.assertEqual(len(all_entries), 2)

    def test_delete_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            e = store.upsert_memory_entry("m1", "fact", "k", "v")
            self.assertTrue(store.delete_memory_entry(e.entry_id))
            self.assertEqual(store.count_memory_entries("m1"), 0)

    def test_delete_mission_memory(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            for i in range(5):
                store.upsert_memory_entry("m1", "fact", f"k{i}", "v")
            deleted = store.delete_mission_memory("m1")
            self.assertEqual(deleted, 5)
            self.assertEqual(store.count_memory_entries("m1"), 0)

    def test_trim_drops_lowest_importance(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            store.upsert_memory_entry("m1", "fact", "high", "v", importance=9)
            store.upsert_memory_entry("m1", "fact", "low", "v", importance=1)
            store.upsert_memory_entry("m1", "fact", "mid", "v", importance=5)
            store.trim_memory_entries("m1", drop_count=1)
            remaining = store.list_memory_entries("m1")
            keys = {e.key for e in remaining}
            self.assertNotIn("low", keys)  # lowest importance should be dropped
            self.assertEqual(len(remaining), 2)

    def test_expire_memory_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            past = datetime.now(timezone.utc) - timedelta(seconds=10)
            store.upsert_memory_entry("m1", "fact", "k", "v", expires_at=past)
            store.upsert_memory_entry("m1", "fact", "k2", "v2")
            removed = store.expire_memory_entries(datetime.now(timezone.utc).isoformat())
            self.assertEqual(removed, 1)


class TestMissionMemoryService(unittest.TestCase):
    def test_remember_and_recall(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            svc = MissionMemoryService(store=store)
            svc.remember("m1", "fact", "lang", "Python", importance=8)
            entries = svc.recall("m1", kind="fact")
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0].value, "Python")

    def test_unknown_kind_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            svc = MissionMemoryService(store=store)
            with self.assertRaises(ValueError):
                svc.remember("m1", "gossip", "key", "value")

    def test_retention_limit_enforced(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            svc = MissionMemoryService(store=store, max_entries_per_mission=5)
            for i in range(8):
                svc.remember("m1", "note", f"k{i}", f"v{i}")
            self.assertLessEqual(svc.entry_count("m1"), 5)

    def test_forget_removes_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            svc = MissionMemoryService(store=store)
            e = svc.remember("m1", "fact", "k", "v")
            self.assertTrue(svc.forget(e.entry_id))
            self.assertEqual(svc.entry_count("m1"), 0)

    def test_forget_mission_clears_all(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            svc = MissionMemoryService(store=store)
            for _ in range(3):
                svc.remember("m1", "note", "k", "v")
            deleted = svc.forget_mission("m1")
            self.assertEqual(deleted, 3)


# ---------------------------------------------------------------------------
# #86 – Artifact and evidence index
# ---------------------------------------------------------------------------


class TestArtifactIndex(unittest.TestCase):
    def test_register_text_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            idx = ArtifactIndex(store=store)
            aid = idx.register_text("m1", kind="diff", name="patch", content="--- a\n+++ b")
            self.assertIsNotNone(aid)
            a = idx.get(aid)
            self.assertIsNotNone(a)
            self.assertEqual(a.kind, "diff")
            self.assertEqual(a.name, "patch")

    def test_register_url_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            idx = ArtifactIndex(store=store)
            aid = idx.register_url("m1", url="https://example.com", name="ref")
            a = idx.get(aid)
            self.assertEqual(a.kind, "url")
            self.assertEqual(a.uri, "https://example.com")

    def test_register_file_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            idx = ArtifactIndex(store=store)
            # Create a real temp file
            p = Path(tmp) / "output.txt"
            p.write_text("hello artifact")
            aid = idx.register_file("m1", path=p, tags=["output"])
            a = idx.get(aid)
            self.assertEqual(a.kind, "file")
            self.assertGreater(a.size_bytes, 0)
            self.assertNotEqual(a.sha256, "")
            self.assertIn("output", a.tags)

    def test_find_by_kind(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            idx = ArtifactIndex(store=store)
            idx.register_text("m1", "diff", "patch1", "diff content")
            idx.register_url("m1", "https://x.com", "link")
            diffs = idx.find("m1", kind="diff")
            self.assertEqual(len(diffs), 1)

    def test_find_by_tag(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            idx = ArtifactIndex(store=store)
            idx.register_text("m1", "log", "log1", "...", tags=["ci"])
            idx.register_text("m1", "log", "log2", "...", tags=["local"])
            ci = idx.find("m1", tag="ci")
            self.assertEqual(len(ci), 1)

    def test_remove_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            idx = ArtifactIndex(store=store)
            aid = idx.register_text("m1", "log", "l", "content")
            self.assertTrue(idx.remove(aid))
            self.assertIsNone(idx.get(aid))

    def test_remove_for_mission(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            idx = ArtifactIndex(store=store)
            idx.register_text("m1", "log", "l1", "c")
            idx.register_text("m1", "log", "l2", "c")
            removed = idx.remove_for_mission("m1")
            self.assertEqual(removed, 2)

    def test_unknown_kind_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            idx = ArtifactIndex(store=store)
            with self.assertRaises(ValueError):
                idx.register_text("m1", "magic_kind", "n", "c")

    def test_count_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            idx = ArtifactIndex(store=store)
            for i in range(4):
                idx.register_text("m1", "log", f"l{i}", "c")
            self.assertEqual(store.count_artifacts("m1"), 4)


# ---------------------------------------------------------------------------
# #87 – Periodic summarisation and compaction
# ---------------------------------------------------------------------------


class TestMissionSummaryPersistence(unittest.TestCase):
    def test_save_and_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            s = store.save_mission_summary("m1", "summary text", memory_count=5)
            self.assertIsInstance(s, MissionSummary)
            summaries = store.list_mission_summaries("m1")
            self.assertEqual(len(summaries), 1)
            self.assertEqual(summaries[0].text, "summary text")
            self.assertEqual(summaries[0].memory_count, 5)

    def test_get_latest_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            store.save_mission_summary("m1", "first")
            store.save_mission_summary("m1", "second")
            latest = store.get_latest_summary("m1")
            self.assertEqual(latest.text, "second")

    def test_no_summary_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            self.assertIsNone(store.get_latest_summary("nonexistent"))


class TestMemoryCompactor(unittest.IsolatedAsyncioTestCase):
    async def test_compact_produces_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            mid = store.create_mission(title="T", goal="g")
            store.transition_mission(mid, "running")
            store.transition_mission(mid, "completed")
            # Add enough entries to trigger compaction
            for i in range(12):
                store.upsert_memory_entry(mid, "fact", f"k{i}", f"value {i}")

            provider = MagicMock()
            provider.generate = AsyncMock(return_value="Compact summary text.")
            cfg = CompactionConfig(min_entries=5, delete_after_compact=False)
            compactor = MemoryCompactor(store=store, provider=provider, config=cfg)
            summary = await compactor.compact_mission(mid)

            self.assertIsNotNone(summary)
            self.assertEqual(summary.mission_id, mid)
            self.assertIn("Compact", summary.text)
            self.assertGreater(summary.memory_count, 0)

    async def test_compact_skips_below_min_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            mid = store.create_mission(title="T", goal="g")
            store.upsert_memory_entry(mid, "fact", "k", "v")  # only 1 entry

            provider = MagicMock()
            provider.generate = AsyncMock(return_value="summary")
            cfg = CompactionConfig(min_entries=10)
            compactor = MemoryCompactor(store=store, provider=provider, config=cfg)
            result = await compactor.compact_mission(mid)
            self.assertIsNone(result)
            provider.generate.assert_not_called()

    async def test_compact_deletes_entries_when_configured(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            mid = store.create_mission(title="T", goal="g")
            store.transition_mission(mid, "running")
            store.transition_mission(mid, "completed")
            for i in range(10):
                store.upsert_memory_entry(mid, "note", f"k{i}", "v")

            provider = MagicMock()
            provider.generate = AsyncMock(return_value="summary")
            cfg = CompactionConfig(min_entries=5, delete_after_compact=True)
            compactor = MemoryCompactor(store=store, provider=provider, config=cfg)
            await compactor.compact_mission(mid)

            self.assertEqual(store.count_memory_entries(mid), 0)

    async def test_compact_fallback_on_provider_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            mid = store.create_mission(title="T", goal="g")
            store.transition_mission(mid, "running")
            store.transition_mission(mid, "completed")
            for i in range(10):
                store.upsert_memory_entry(mid, "fact", f"k{i}", f"val{i}")

            provider = MagicMock()
            provider.generate = AsyncMock(side_effect=RuntimeError("provider down"))
            cfg = CompactionConfig(min_entries=5, delete_after_compact=False)
            compactor = MemoryCompactor(store=store, provider=provider, config=cfg)
            summary = await compactor.compact_mission(mid)
            # Should have a fallback summary
            self.assertIsNotNone(summary)
            self.assertIn("unavailable", summary.text)

    async def test_compactor_start_and_stop(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            provider = MagicMock()
            cfg = CompactionConfig(interval_sec=9999)
            compactor = MemoryCompactor(store=store, provider=provider, config=cfg)
            await compactor.start()
            self.assertTrue(compactor._running)
            await compactor.stop()
            self.assertFalse(compactor._running)

    async def test_compact_all_covers_completed_missions(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            # Create two completed missions with enough memory
            mids = []
            for _ in range(2):
                mid = store.create_mission(title="T", goal="g")
                store.transition_mission(mid, "running")
                store.transition_mission(mid, "completed")
                mids.append(mid)
                for i in range(10):
                    store.upsert_memory_entry(mid, "fact", f"k{i}", "v")

            provider = MagicMock()
            provider.generate = AsyncMock(return_value="summary")
            cfg = CompactionConfig(min_entries=5, delete_after_compact=False)
            compactor = MemoryCompactor(store=store, provider=provider, config=cfg)
            summaries = await compactor.compact_all()
            self.assertEqual(len(summaries), 2)
