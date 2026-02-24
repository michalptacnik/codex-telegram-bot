"""Tests for EPIC 9: Unattended Safety, Budgets, and Watchdogs.

Covers:
  #89 – Autonomy policy modes
  #90 – Mission budgets and kill switches
  #91 – Watchdog + auto-recovery
  #92 – Escalation and notifications
"""
from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Tuple
from unittest.mock import AsyncMock

from codex_telegram_bot.domain.autonomy import (
    AUTONOMY_EXECUTE_FULL,
    AUTONOMY_EXECUTE_LIMITED,
    AUTONOMY_OBSERVE_ONLY,
    AUTONOMY_PROPOSE,
    AutonomyModeEvent,
    AutonomyPolicyManager,
    TOOL_ALLOWLIST,
    is_tool_allowed,
    mode_rank,
)
from codex_telegram_bot.domain.missions import (
    MISSION_STATE_FAILED,
    MISSION_STATE_IDLE,
    MISSION_STATE_RUNNING,
)
from codex_telegram_bot.persistence.sqlite_store import SqliteRunStore
from codex_telegram_bot.services.escalation import (
    SEVERITY_CRITICAL,
    SEVERITY_INFO,
    SEVERITY_WARN,
    EscalationDispatcher,
    EscalationRule,
    WebhookChannel,
    event_severity,
)
from codex_telegram_bot.services.mission_budget import (
    BudgetBreachError,
    BudgetConfig,
    BudgetEnforcer,
    KillSwitch,
)
from codex_telegram_bot.services.mission_watchdog import MissionWatchdog, WatchdogConfig


def _make_store(tmp: str) -> SqliteRunStore:
    return SqliteRunStore(Path(tmp) / "test.db")


# ---------------------------------------------------------------------------
# #89 – Autonomy policy modes
# ---------------------------------------------------------------------------


class TestAutonomyModes(unittest.TestCase):
    def test_observe_only_allows_nothing(self):
        mgr = AutonomyPolicyManager(AUTONOMY_OBSERVE_ONLY)
        self.assertFalse(mgr.is_tool_allowed("read_file"))
        self.assertFalse(mgr.is_tool_allowed("shell_exec"))

    def test_propose_allows_read_only(self):
        mgr = AutonomyPolicyManager(AUTONOMY_PROPOSE)
        self.assertTrue(mgr.is_tool_allowed("read_file"))
        self.assertTrue(mgr.is_tool_allowed("git_status"))
        self.assertFalse(mgr.is_tool_allowed("shell_exec"))
        self.assertFalse(mgr.is_tool_allowed("write_file"))

    def test_execute_limited_allows_writes(self):
        mgr = AutonomyPolicyManager(AUTONOMY_EXECUTE_LIMITED)
        self.assertTrue(mgr.is_tool_allowed("write_file"))
        self.assertTrue(mgr.is_tool_allowed("git_commit"))
        self.assertFalse(mgr.is_tool_allowed("shell_exec"))
        self.assertFalse(mgr.is_tool_allowed("github_comment"))

    def test_execute_full_allows_all(self):
        mgr = AutonomyPolicyManager(AUTONOMY_EXECUTE_FULL)
        self.assertTrue(mgr.is_tool_allowed("shell_exec"))
        self.assertTrue(mgr.is_tool_allowed("github_comment"))
        self.assertTrue(mgr.is_tool_allowed("github_create_issue"))

    def test_set_mode_records_audit_event(self):
        mgr = AutonomyPolicyManager(AUTONOMY_OBSERVE_ONLY)
        evt = mgr.set_mode(AUTONOMY_PROPOSE, actor="ops", reason="approved")
        self.assertIsInstance(evt, AutonomyModeEvent)
        self.assertEqual(evt.from_mode, AUTONOMY_OBSERVE_ONLY)
        self.assertEqual(evt.to_mode, AUTONOMY_PROPOSE)
        self.assertEqual(evt.actor, "ops")
        self.assertEqual(evt.reason, "approved")

    def test_mode_history_accumulates(self):
        mgr = AutonomyPolicyManager(AUTONOMY_OBSERVE_ONLY)
        mgr.set_mode(AUTONOMY_PROPOSE)
        mgr.set_mode(AUTONOMY_EXECUTE_LIMITED)
        history = mgr.mode_history()
        self.assertEqual(len(history), 2)
        self.assertEqual(history[-1].to_mode, AUTONOMY_EXECUTE_LIMITED)

    def test_invalid_mode_raises(self):
        with self.assertRaises(ValueError):
            AutonomyPolicyManager("super_mode")

    def test_set_invalid_mode_raises(self):
        mgr = AutonomyPolicyManager(AUTONOMY_PROPOSE)
        with self.assertRaises(ValueError):
            mgr.set_mode("godmode")

    def test_mode_rank_ordering(self):
        self.assertLess(mode_rank(AUTONOMY_OBSERVE_ONLY), mode_rank(AUTONOMY_PROPOSE))
        self.assertLess(mode_rank(AUTONOMY_PROPOSE), mode_rank(AUTONOMY_EXECUTE_LIMITED))
        self.assertLess(mode_rank(AUTONOMY_EXECUTE_LIMITED), mode_rank(AUTONOMY_EXECUTE_FULL))

    def test_tool_allowlist_escalates_with_mode(self):
        observe = len(TOOL_ALLOWLIST[AUTONOMY_OBSERVE_ONLY])
        propose = len(TOOL_ALLOWLIST[AUTONOMY_PROPOSE])
        limited = len(TOOL_ALLOWLIST[AUTONOMY_EXECUTE_LIMITED])
        full = len(TOOL_ALLOWLIST[AUTONOMY_EXECUTE_FULL])
        self.assertLess(observe, propose)
        self.assertLess(propose, limited)
        self.assertLess(limited, full)

    def test_allowed_tools_returns_frozenset(self):
        mgr = AutonomyPolicyManager(AUTONOMY_PROPOSE)
        tools = mgr.allowed_tools()
        self.assertIsInstance(tools, frozenset)
        self.assertIn("read_file", tools)


# ---------------------------------------------------------------------------
# #90 – Mission budgets and kill switches
# ---------------------------------------------------------------------------


class TestBudgetEnforcer(unittest.IsolatedAsyncioTestCase):
    async def test_no_breach_within_limits(self):
        enforcer = BudgetEnforcer()
        enforcer.start("m1", BudgetConfig(max_actions=10))
        enforcer.record_action("m1")
        await enforcer.check("m1")  # should not raise

    async def test_action_budget_breach(self):
        enforcer = BudgetEnforcer()
        enforcer.start("m1", BudgetConfig(max_actions=2))
        enforcer.record_action("m1")
        enforcer.record_action("m1")
        with self.assertRaises(BudgetBreachError) as ctx:
            await enforcer.check("m1")
        self.assertIn("action", ctx.exception.reason)

    async def test_cost_budget_breach(self):
        enforcer = BudgetEnforcer()
        enforcer.start("m1", BudgetConfig(max_cost_usd=0.01))
        enforcer.record_action("m1", cost_usd=0.02)
        with self.assertRaises(BudgetBreachError) as ctx:
            await enforcer.check("m1")
        self.assertIn("cost", ctx.exception.reason)

    async def test_time_budget_breach(self):
        enforcer = BudgetEnforcer()
        usage = enforcer.start("m1", BudgetConfig(max_time_sec=0.0))
        # Elapsed will be >= 0, so it should breach immediately
        await asyncio.sleep(0.01)
        with self.assertRaises(BudgetBreachError) as ctx:
            await enforcer.check("m1")
        self.assertIn("time", ctx.exception.reason)

    async def test_breach_fires_alert(self):
        alerts: List[Tuple] = []

        async def alert(mid, kind, detail):
            alerts.append((mid, kind, detail))

        enforcer = BudgetEnforcer(alert_callback=alert)
        enforcer.start("m2", BudgetConfig(max_actions=1))
        enforcer.record_action("m2")
        try:
            await enforcer.check("m2")
        except BudgetBreachError:
            pass
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0][1], "budget.breach")

    async def test_breach_only_fires_once(self):
        alerts: List = []

        async def alert(mid, kind, detail):
            alerts.append(kind)

        enforcer = BudgetEnforcer(alert_callback=alert)
        enforcer.start("m3", BudgetConfig(max_actions=0))
        for _ in range(3):
            try:
                await enforcer.check("m3")
            except BudgetBreachError:
                pass
        self.assertEqual(len(alerts), 1)

    async def test_summary_returns_usage(self):
        enforcer = BudgetEnforcer()
        enforcer.start("m4", BudgetConfig(max_actions=5, max_cost_usd=1.0))
        enforcer.record_action("m4", cost_usd=0.1)
        summary = enforcer.summary("m4")
        self.assertEqual(summary["actions"], 1)
        self.assertAlmostEqual(summary["cost_usd"], 0.1, places=4)
        self.assertFalse(summary["breached"])

    async def test_stop_removes_tracking(self):
        enforcer = BudgetEnforcer()
        enforcer.start("m5", BudgetConfig(max_actions=5))
        enforcer.stop("m5")
        self.assertIsNone(enforcer.get_usage("m5"))


class TestKillSwitch(unittest.IsolatedAsyncioTestCase):
    async def test_arm_and_trigger(self):
        ks = KillSwitch()
        ks.arm("m1")
        self.assertFalse(ks.is_killed("m1"))
        await ks.trigger("m1", "testing")
        self.assertTrue(ks.is_killed("m1"))
        self.assertEqual(ks.kill_reason("m1"), "testing")

    async def test_not_killed_before_trigger(self):
        ks = KillSwitch()
        ks.arm("m2")
        self.assertFalse(ks.is_killed("m2"))

    async def test_disarm_clears_state(self):
        ks = KillSwitch()
        ks.arm("m3")
        await ks.trigger("m3", "test")
        ks.disarm("m3")
        self.assertFalse(ks.is_killed("m3"))
        self.assertNotIn("m3", ks.active_missions())

    async def test_wait_for_kill(self):
        ks = KillSwitch()
        ks.arm("m4")

        async def do_trigger():
            await asyncio.sleep(0.02)
            await ks.trigger("m4", "async kill")

        asyncio.create_task(do_trigger())
        reason = await ks.wait_for_kill("m4")
        self.assertEqual(reason, "async kill")

    async def test_trigger_fires_alert(self):
        alerts: List = []

        async def alert(mid, kind, detail):
            alerts.append(kind)

        ks = KillSwitch(alert_callback=alert)
        ks.arm("m5")
        await ks.trigger("m5")
        self.assertIn("kill_switch.triggered", alerts)

    async def test_active_missions(self):
        ks = KillSwitch()
        ks.arm("m1")
        ks.arm("m2")
        self.assertIn("m1", ks.active_missions())
        self.assertIn("m2", ks.active_missions())
        ks.disarm("m1")
        self.assertNotIn("m1", ks.active_missions())


# ---------------------------------------------------------------------------
# #91 – Watchdog + auto-recovery
# ---------------------------------------------------------------------------


class TestMissionWatchdog(unittest.IsolatedAsyncioTestCase):
    async def test_auto_recovers_failed_missions_with_retries(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            mid = store.create_mission(title="T", goal="g", retry_limit=3)
            store.transition_mission(mid, MISSION_STATE_RUNNING)
            store.transition_mission(mid, MISSION_STATE_FAILED, "initial fail")
            # retry_count is 0, retry_limit is 3 → should recover

            cfg = WatchdogConfig(poll_interval_sec=999, auto_recover_failed=True)
            wd = MissionWatchdog(store=store, config=cfg)
            await wd._scan()

            m = store.get_mission(mid)
            self.assertEqual(m.state, MISSION_STATE_IDLE)
            self.assertEqual(m.retry_count, 1)

    async def test_no_recovery_when_retries_exhausted(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            mid = store.create_mission(title="T", goal="g", retry_limit=2)
            store.transition_mission(mid, MISSION_STATE_RUNNING)
            store.transition_mission(mid, MISSION_STATE_FAILED, "fail")
            store.increment_mission_retry(mid)
            store.increment_mission_retry(mid)  # retry_count == retry_limit

            cfg = WatchdogConfig(poll_interval_sec=999, auto_recover_failed=True)
            wd = MissionWatchdog(store=store, config=cfg)
            await wd._scan()

            m = store.get_mission(mid)
            self.assertEqual(m.state, MISSION_STATE_FAILED)  # not recovered

    async def test_stale_running_mission_marked_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            mid = store.create_mission(title="T", goal="g", retry_limit=0)
            store.transition_mission(mid, MISSION_STATE_RUNNING)

            # Seed last_seen to simulate a mission that hasn't updated
            cfg = WatchdogConfig(stale_threshold_sec=0, auto_recover_failed=False)
            wd = MissionWatchdog(store=store, config=cfg)
            m = store.get_mission(mid)
            wd._last_seen[mid] = m.updated_at.isoformat()  # pre-seed as stale

            await wd._scan()
            m = store.get_mission(mid)
            self.assertEqual(m.state, MISSION_STATE_FAILED)

    async def test_watchdog_start_and_stop(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            cfg = WatchdogConfig(poll_interval_sec=60)
            wd = MissionWatchdog(store=store, config=cfg)
            await wd.start()
            self.assertTrue(wd._running)
            await wd.stop()
            self.assertFalse(wd._running)

    async def test_watchdog_idempotent_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            wd = MissionWatchdog(store=store, config=WatchdogConfig(poll_interval_sec=60))
            await wd.start()
            task1 = wd._task
            await wd.start()  # second start should be no-op
            self.assertIs(wd._task, task1)
            await wd.stop()

    async def test_recovery_alert_fires(self):
        alerts: List = []

        async def alert(mid, kind, detail):
            alerts.append(kind)

        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            mid = store.create_mission(title="T", goal="g", retry_limit=2)
            store.transition_mission(mid, MISSION_STATE_RUNNING)
            store.transition_mission(mid, MISSION_STATE_FAILED)

            cfg = WatchdogConfig(poll_interval_sec=999, auto_recover_failed=True)
            wd = MissionWatchdog(store=store, config=cfg, alert_callback=alert)
            await wd._scan()
            self.assertIn("watchdog.recovered", alerts)


# ---------------------------------------------------------------------------
# #92 – Escalation and notifications
# ---------------------------------------------------------------------------


class TestEventSeverity(unittest.TestCase):
    def test_known_events(self):
        self.assertEqual(event_severity("mission.failed"), SEVERITY_CRITICAL)
        self.assertEqual(event_severity("budget.breach"), SEVERITY_CRITICAL)
        self.assertEqual(event_severity("kill_switch.triggered"), SEVERITY_CRITICAL)
        self.assertEqual(event_severity("watchdog.stalled"), SEVERITY_WARN)
        self.assertEqual(event_severity("mission.completed"), SEVERITY_INFO)

    def test_unknown_event_defaults_to_info(self):
        self.assertEqual(event_severity("custom.thing"), SEVERITY_INFO)


class TestEscalationRule(unittest.TestCase):
    def test_matches_by_severity(self):
        rule = EscalationRule(min_severity=SEVERITY_WARN, channel_ids=["c1"])
        self.assertTrue(rule.matches("mission.failed", SEVERITY_CRITICAL))
        self.assertTrue(rule.matches("watchdog.stalled", SEVERITY_WARN))
        self.assertFalse(rule.matches("mission.completed", SEVERITY_INFO))

    def test_matches_with_event_filter(self):
        rule = EscalationRule(
            min_severity=SEVERITY_INFO,
            channel_ids=["c1"],
            event_filter={"mission.failed", "budget.breach"},
        )
        self.assertTrue(rule.matches("mission.failed", SEVERITY_CRITICAL))
        self.assertFalse(rule.matches("mission.completed", SEVERITY_INFO))


class TestEscalationDispatcher(unittest.IsolatedAsyncioTestCase):
    async def test_dispatches_to_channel(self):
        received: List = []

        async def notify(channel_id, subject, body):
            received.append((channel_id, subject))

        dispatcher = EscalationDispatcher()
        dispatcher.add_channel("ch1", notify)
        dispatcher.add_rule(EscalationRule(min_severity=SEVERITY_CRITICAL, channel_ids=["ch1"]))

        await dispatcher.dispatch("m1", "mission.failed", "fatal error")
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0][0], "ch1")
        self.assertIn("CRITICAL", received[0][1])

    async def test_below_threshold_not_dispatched(self):
        received: List = []

        async def notify(channel_id, subject, body):
            received.append(subject)

        dispatcher = EscalationDispatcher()
        dispatcher.add_channel("ch1", notify)
        dispatcher.add_rule(EscalationRule(min_severity=SEVERITY_CRITICAL, channel_ids=["ch1"]))

        await dispatcher.dispatch("m1", "mission.completed", "done")
        self.assertEqual(len(received), 0)

    async def test_cooldown_suppresses_duplicates(self):
        received: List = []

        async def notify(channel_id, subject, body):
            received.append(subject)

        dispatcher = EscalationDispatcher()
        dispatcher.add_channel("ch1", notify)
        dispatcher.add_rule(EscalationRule(
            min_severity=SEVERITY_CRITICAL, channel_ids=["ch1"], cooldown_sec=60
        ))

        await dispatcher.dispatch("m1", "mission.failed", "a")
        await dispatcher.dispatch("m1", "mission.failed", "a")  # within cooldown
        self.assertEqual(len(received), 1)

    async def test_multiple_channels(self):
        received: List = []

        async def notify(cid, sub, body):
            received.append(cid)

        dispatcher = EscalationDispatcher()
        dispatcher.add_channel("ch1", notify)
        dispatcher.add_channel("ch2", notify)
        dispatcher.add_rule(EscalationRule(
            min_severity=SEVERITY_WARN, channel_ids=["ch1", "ch2"]
        ))

        await dispatcher.dispatch("m1", "watchdog.stalled", "stalled")
        self.assertIn("ch1", received)
        self.assertIn("ch2", received)

    async def test_missing_channel_skipped_gracefully(self):
        dispatcher = EscalationDispatcher()
        # Rule references channel that doesn't exist
        dispatcher.add_rule(EscalationRule(
            min_severity=SEVERITY_INFO, channel_ids=["nonexistent"]
        ))
        # Should not raise
        await dispatcher.dispatch("m1", "mission.completed", "ok")

    async def test_notify_exception_doesnt_propagate(self):
        async def bad_notify(cid, sub, body):
            raise RuntimeError("channel down")

        dispatcher = EscalationDispatcher()
        dispatcher.add_channel("ch_bad", bad_notify)
        dispatcher.add_rule(EscalationRule(min_severity=SEVERITY_INFO, channel_ids=["ch_bad"]))
        # Should not raise
        await dispatcher.dispatch("m1", "mission.completed", "ok")

    async def test_webhook_channel_calls_injected_post(self):
        posted: List = []

        async def fake_post(url, headers, payload):
            posted.append(payload)

        channel = WebhookChannel(url="https://example.com/hook", http_post_fn=fake_post)
        await channel.notify("ch1", "subject", "body")
        self.assertEqual(len(posted), 1)
        self.assertEqual(posted[0]["subject"], "subject")
        self.assertIn("channel_id", posted[0])
