"""Tests for EPIC 10: 24/7 Operations and Reliability.

Covers:
  #94 – Daemon mode and supervisor integration
  #95 – Mission observability dashboard
  #96 – State backup and restore
  #97 – Autonomous runbooks and chaos tests
"""
from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from codex_telegram_bot.persistence.sqlite_store import SqliteRunStore
from codex_telegram_bot.services.daemon import DaemonConfig, DaemonState, MissionWorkerDaemon
from codex_telegram_bot.services.observability import DashboardSnapshot, MetricsCollector
from codex_telegram_bot.services.runbooks import (
    RUNBOOK_HIGH_ERROR_RATE,
    RUNBOOK_STUCK_MISSION,
    ChaosChecklist,
    RunbookRegistry,
    chaos_provider_failure,
    chaos_store_failure,
)
from codex_telegram_bot.services.state_backup import StateBackupService


def _make_store(tmp: str) -> SqliteRunStore:
    return SqliteRunStore(Path(tmp) / "test.db")


# ---------------------------------------------------------------------------
# #94 – Daemon mode
# ---------------------------------------------------------------------------


class TestDaemonState(unittest.TestCase):
    def test_initial_state(self):
        s = DaemonState()
        self.assertFalse(s.running)
        self.assertIsNone(s.started_at)
        self.assertEqual(s.missions_dispatched, 0)

    def test_to_dict(self):
        s = DaemonState()
        s.running = True
        s.started_at = datetime.now(timezone.utc)
        d = s.to_dict()
        self.assertIn("running", d)
        self.assertTrue(d["running"])
        self.assertIn("uptime_sec", d)
        self.assertGreaterEqual(d["uptime_sec"], 0)


class TestMissionWorkerDaemon(unittest.IsolatedAsyncioTestCase):
    async def test_start_and_stop(self):
        store = MagicMock()
        store.list_missions.return_value = []
        runner = MagicMock()
        runner.is_active.return_value = False
        scheduler = MagicMock()
        cfg = DaemonConfig(poll_interval_sec=0.05, heartbeat_interval_sec=0.05)
        daemon = MissionWorkerDaemon(store=store, runner=runner, scheduler=scheduler, config=cfg)
        await daemon.start()
        self.assertTrue(daemon.state.running)
        await asyncio.sleep(0.1)
        await daemon.stop()
        self.assertFalse(daemon.state.running)

    async def test_is_healthy_after_heartbeat(self):
        store = MagicMock()
        store.list_missions.return_value = []
        runner = MagicMock()
        runner.is_active.return_value = False
        scheduler = MagicMock()
        cfg = DaemonConfig(poll_interval_sec=0.05, heartbeat_interval_sec=0.05)
        daemon = MissionWorkerDaemon(store=store, runner=runner, scheduler=scheduler, config=cfg)
        await daemon.start()
        await asyncio.sleep(0.15)
        self.assertTrue(daemon.is_healthy())
        await daemon.stop()

    async def test_heartbeat_writes_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            hb_path = Path(tmp) / "heartbeat"
            store = MagicMock()
            store.list_missions.return_value = []
            runner = MagicMock()
            runner.is_active.return_value = False
            scheduler = MagicMock()
            cfg = DaemonConfig(
                heartbeat_path=hb_path,
                poll_interval_sec=0.05,
                heartbeat_interval_sec=0.05,
            )
            daemon = MissionWorkerDaemon(store=store, runner=runner, scheduler=scheduler, config=cfg)
            await daemon.start()
            await asyncio.sleep(0.15)
            await daemon.stop()
            self.assertTrue(hb_path.exists())
            content = hb_path.read_text()
            # Should be an ISO timestamp
            datetime.fromisoformat(content.strip())

    async def test_not_healthy_when_stopped(self):
        store = MagicMock()
        store.list_missions.return_value = []
        runner = MagicMock()
        runner.is_active.return_value = False
        scheduler = MagicMock()
        daemon = MissionWorkerDaemon(store=store, runner=runner, scheduler=scheduler)
        # Never started
        self.assertFalse(daemon.is_healthy())

    async def test_dispatches_pending_missions(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            mid = store.create_mission(title="T", goal="g")
            # keep it in idle (don't transition — daemon should pick it up)

            runner = MagicMock()
            runner.is_active.return_value = False
            runner.run = AsyncMock(return_value=None)
            scheduler = MagicMock()
            cfg = DaemonConfig(poll_interval_sec=0.05, heartbeat_interval_sec=9999)
            daemon = MissionWorkerDaemon(store=store, runner=runner, scheduler=scheduler, config=cfg)
            await daemon.start()
            await asyncio.sleep(0.2)
            await daemon.stop()
            self.assertGreater(daemon.state.missions_dispatched, 0)

    async def test_consecutive_errors_trigger_stop(self):
        store = MagicMock()
        store.list_missions.side_effect = RuntimeError("DB gone")
        runner = MagicMock()
        runner.is_active.return_value = False
        scheduler = MagicMock()
        cfg = DaemonConfig(
            poll_interval_sec=0.01,
            heartbeat_interval_sec=9999,
            max_consecutive_errors=3,
        )
        daemon = MissionWorkerDaemon(store=store, runner=runner, scheduler=scheduler, config=cfg)
        await daemon.start()
        # Give it time to accumulate errors and self-stop
        await asyncio.sleep(0.3)
        # The stop event should have been set
        self.assertTrue(daemon._stop_event.is_set())
        await daemon.stop()

    async def test_double_start_is_noop(self):
        store = MagicMock()
        store.list_missions.return_value = []
        runner = MagicMock()
        runner.is_active.return_value = False
        scheduler = MagicMock()
        cfg = DaemonConfig(poll_interval_sec=9999, heartbeat_interval_sec=9999)
        daemon = MissionWorkerDaemon(store=store, runner=runner, scheduler=scheduler, config=cfg)
        await daemon.start()
        await daemon.start()  # should be no-op
        self.assertTrue(daemon.state.running)
        await daemon.stop()


# ---------------------------------------------------------------------------
# #95 – Observability dashboard
# ---------------------------------------------------------------------------


class TestMetricsCollector(unittest.TestCase):
    def test_snapshot_zero_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            collector = MetricsCollector(store=store, window_minutes=60)
            snap = collector.snapshot()
            self.assertIsInstance(snap, DashboardSnapshot)
            self.assertEqual(snap.pending, 0)
            self.assertEqual(snap.running, 0)
            self.assertEqual(snap.completed, 0)
            self.assertEqual(snap.error_rate, 0.0)

    def test_snapshot_counts_states(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            # pending
            store.create_mission(title="P1", goal="g")
            # running
            mid2 = store.create_mission(title="R1", goal="g")
            store.transition_mission(mid2, "running")
            # completed
            mid3 = store.create_mission(title="C1", goal="g")
            store.transition_mission(mid3, "running")
            store.transition_mission(mid3, "completed")

            collector = MetricsCollector(store=store)
            snap = collector.snapshot()
            self.assertEqual(snap.pending, 1)
            self.assertEqual(snap.running, 1)
            self.assertEqual(snap.completed, 1)

    def test_step_latency_recorded(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            collector = MetricsCollector(store=store)
            for t in [1.0, 2.0, 3.0, 4.0, 5.0]:
                collector.record_step_latency(t)
            snap = collector.snapshot()
            self.assertAlmostEqual(snap.step_latency_mean_sec, 3.0)
            self.assertGreater(snap.step_latency_p95_sec, 0)

    def test_error_rate_calculation(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            # 1 completed, 1 failed
            for state in ("completed", "failed"):
                mid = store.create_mission(title="T", goal="g")
                store.transition_mission(mid, "running")
                store.transition_mission(mid, state)

            collector = MetricsCollector(store=store)
            snap = collector.snapshot()
            self.assertAlmostEqual(snap.error_rate, 0.5)

    def test_format_text_is_string(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            snap = MetricsCollector(store=store).snapshot()
            text = snap.format_text()
            self.assertIsInstance(text, str)
            self.assertIn("Dashboard", text)

    def test_to_dict_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            snap = MetricsCollector(store=store).snapshot()
            d = snap.to_dict()
            for key in ("pending", "running", "completed", "failed",
                        "error_rate", "throughput_per_hour",
                        "step_latency_mean_sec", "recent_failures"):
                self.assertIn(key, d)

    def test_recent_failures_populated(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            mid = store.create_mission(title="FailMe", goal="g")
            store.transition_mission(mid, "running")
            store.transition_mission(mid, "failed", reason="boom")

            collector = MetricsCollector(store=store)
            snap = collector.snapshot()
            self.assertEqual(len(snap.recent_failures), 1)
            self.assertEqual(snap.recent_failures[0].title, "FailMe")
            self.assertEqual(snap.recent_failures[0].error_hint, "boom")

    def test_windowed_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            # Create 2 completed missions so they appear in the window
            for _ in range(2):
                mid = store.create_mission(title="T", goal="g")
                store.transition_mission(mid, "running")
                store.transition_mission(mid, "completed")

            collector = MetricsCollector(store=store, window_minutes=60)
            snap = collector.snapshot()
            self.assertEqual(snap.completed_in_window, 2)


# ---------------------------------------------------------------------------
# #96 – State backup and restore
# ---------------------------------------------------------------------------


class TestStateBackupService(unittest.TestCase):
    def test_backup_creates_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            db_path = Path(tmp) / "test.db"
            svc = StateBackupService(db_path=db_path, backup_dir=Path(tmp) / "backups")
            record = svc.backup()
            self.assertTrue(record.path.exists())
            self.assertGreater(record.size_bytes, 0)
            self.assertNotEqual(record.sha256, "")

    def test_backup_checksum_valid(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            db_path = Path(tmp) / "test.db"
            svc = StateBackupService(db_path=db_path, backup_dir=Path(tmp) / "backups")
            record = svc.backup()
            self.assertTrue(record.is_valid())

    def test_list_backups(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            db_path = Path(tmp) / "test.db"
            svc = StateBackupService(db_path=db_path, backup_dir=Path(tmp) / "backups")
            svc.backup()
            svc.backup()
            backups = svc.list_backups()
            self.assertEqual(len(backups), 2)

    def test_verify_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            db_path = Path(tmp) / "test.db"
            svc = StateBackupService(db_path=db_path, backup_dir=Path(tmp) / "backups")
            record = svc.backup()
            self.assertTrue(svc.verify(record.backup_id))

    def test_verify_nonexistent_returns_false(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            # write a minimal sqlite db
            import sqlite3; sqlite3.connect(str(db_path)).close()
            svc = StateBackupService(db_path=db_path, backup_dir=Path(tmp) / "backups")
            self.assertFalse(svc.verify("nonexistent-ts"))

    def test_restore_replaces_db(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            db_path = Path(tmp) / "test.db"
            backup_dir = Path(tmp) / "backups"
            svc = StateBackupService(db_path=db_path, backup_dir=backup_dir)
            record = svc.backup()

            # Corrupt the current DB
            db_path.write_bytes(b"corrupted")
            # Restore
            pre_restore = svc.restore(record.backup_id)
            self.assertTrue(pre_restore.exists())  # pre-restore copy saved
            # DB should be valid SQLite again
            import sqlite3
            conn = sqlite3.connect(str(db_path))
            conn.execute("PRAGMA integrity_check").fetchone()
            conn.close()

    def test_restore_missing_backup_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            import sqlite3; sqlite3.connect(str(db_path)).close()
            svc = StateBackupService(db_path=db_path, backup_dir=Path(tmp) / "backups")
            with self.assertRaises(FileNotFoundError):
                svc.restore("does-not-exist")

    def test_max_backups_prune(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            db_path = Path(tmp) / "test.db"
            svc = StateBackupService(db_path=db_path, backup_dir=Path(tmp) / "backups",
                                     max_backups=3)
            for _ in range(5):
                time.sleep(0.01)  # ensure different timestamps
                svc.backup()
            backups = svc.list_backups()
            self.assertLessEqual(len(backups), 3)

    def test_list_empty_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            import sqlite3; sqlite3.connect(str(db_path)).close()
            svc = StateBackupService(db_path=db_path, backup_dir=Path(tmp) / "backups")
            self.assertEqual(svc.list_backups(), [])


class TestAutoBackup(unittest.IsolatedAsyncioTestCase):
    async def test_auto_backup_creates_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            db_path = Path(tmp) / "test.db"
            svc = StateBackupService(db_path=db_path, backup_dir=Path(tmp) / "backups")
            await svc.start_auto_backup(interval_sec=0.05)
            await asyncio.sleep(0.15)
            await svc.stop_auto_backup()
            self.assertGreater(len(svc.list_backups()), 0)


# ---------------------------------------------------------------------------
# #97 – Runbooks and chaos tests
# ---------------------------------------------------------------------------


class TestRunbooks(unittest.TestCase):
    def test_stuck_mission_runbook_not_triggered_fresh(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            mid = store.create_mission(title="T", goal="g")
            store.transition_mission(mid, "running")
            # Just transitioned – not stuck yet
            triggered = RUNBOOK_STUCK_MISSION.check(store)
            self.assertFalse(triggered)

    def test_high_error_rate_not_triggered_when_no_missions(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            triggered = RUNBOOK_HIGH_ERROR_RATE.check(store)
            self.assertFalse(triggered)

    def test_high_error_rate_triggered(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            # 3 failed, 1 completed → 75% error rate > 50% threshold
            for _ in range(3):
                mid = store.create_mission(title="T", goal="g")
                store.transition_mission(mid, "running")
                store.transition_mission(mid, "failed")
            mid = store.create_mission(title="T", goal="g")
            store.transition_mission(mid, "running")
            store.transition_mission(mid, "completed")

            triggered = RUNBOOK_HIGH_ERROR_RATE.check(store)
            self.assertTrue(triggered)

    def test_runbook_registry_evaluate(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            registry = RunbookRegistry(store=store)
            registry.register(RUNBOOK_STUCK_MISSION)
            registry.register(RUNBOOK_HIGH_ERROR_RATE)
            results = registry.evaluate_all()
            self.assertEqual(len(results), 2)
            # Neither should trigger on empty state
            for r in results:
                self.assertFalse(r.triggered)

    def test_runbook_remedy_on_trigger(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            # 2 failed missions → high error rate
            for _ in range(2):
                mid = store.create_mission(title="T", goal="g")
                store.transition_mission(mid, "running")
                store.transition_mission(mid, "failed")

            registry = RunbookRegistry(store=store)
            registry.register(RUNBOOK_HIGH_ERROR_RATE)
            results = registry.evaluate_all()
            self.assertEqual(len(results), 1)
            r = results[0]
            self.assertTrue(r.triggered)
            self.assertGreater(len(r.actions_taken), 0)

    def test_runbook_check_exception_captured(self):
        def bad_check(store):
            raise RuntimeError("oops")
        def noop_remedy(store):
            return []

        from codex_telegram_bot.services.runbooks import Runbook
        rb = Runbook(name="bad", description="", check=bad_check, remedy=noop_remedy)
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            registry = RunbookRegistry(store=store)
            registry.register(rb)
            results = registry.evaluate_all()
            self.assertEqual(len(results), 1)
            self.assertIsNotNone(results[0].error)
            self.assertFalse(results[0].triggered)


class TestChaosHelpers(unittest.IsolatedAsyncioTestCase):
    async def test_chaos_provider_failure_injects_error(self):
        call_count = 0

        async def _generate(**kw):
            nonlocal call_count
            call_count += 1
            return "ok"

        provider = MagicMock()
        provider.generate = _generate

        errors = 0
        with chaos_provider_failure(provider, after_calls=1):
            for _ in range(3):
                try:
                    await provider.generate(messages=[])
                except RuntimeError:
                    errors += 1

        self.assertGreater(errors, 0)

    def test_chaos_store_failure_injects_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            errors = 0
            with chaos_store_failure(store, method="upsert_memory_entry", after_calls=1):
                for i in range(4):
                    try:
                        store.upsert_memory_entry("m1", "fact", f"k{i}", "v")
                    except RuntimeError:
                        errors += 1
            self.assertGreater(errors, 0)


class TestChaosChecklist(unittest.TestCase):
    def test_all_passed(self):
        cl = ChaosChecklist()
        cl.record("scenario-a", True)
        cl.record("scenario-b", True)
        self.assertTrue(cl.all_passed())

    def test_not_all_passed(self):
        cl = ChaosChecklist()
        cl.record("ok", True)
        cl.record("bad", False)
        self.assertFalse(cl.all_passed())

    def test_summary_contains_results(self):
        cl = ChaosChecklist()
        cl.record("watchdog-kill", True, "mission ended in failed")
        cl.record("provider-down", False, "mission hung")
        summary = cl.summary()
        self.assertIn("watchdog-kill", summary)
        self.assertIn("provider-down", summary)
        self.assertIn("1/2", summary)

    def test_results_list(self):
        cl = ChaosChecklist()
        cl.record("s1", True)
        cl.record("s2", False)
        self.assertEqual(len(cl.results), 2)
        self.assertEqual(cl.results[0]["scenario"], "s1")
        self.assertTrue(cl.results[0]["passed"])
