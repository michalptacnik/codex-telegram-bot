import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from codex_telegram_bot.app_container import build_agent_service
from codex_telegram_bot.services.access_control import UserProfile
from codex_telegram_bot.services.thin_memory import ThinMemoryStore


class TestHeartbeatScheduler(unittest.IsolatedAsyncioTestCase):
    async def test_dry_run_returns_action_without_sending(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"ENABLE_HEARTBEAT": "1"}, clear=False):
                service = build_agent_service(
                    state_db_path=Path(tmp) / "state.db",
                    config_dir=Path(tmp),
                )
            session = service.get_or_create_session(chat_id=21, user_id=22)
            tm = ThinMemoryStore(service.session_workspace(session.session_id))
            today = datetime.now(timezone.utc).date().isoformat()
            tm.update_index_patch(
                {
                    "obligations": {
                        "upsert": [
                            {
                                "obligation_id": "O001",
                                "text": "Send invoice",
                                "due": today,
                                "ref": "memory/pages/tasks.md#invoices",
                            }
                        ]
                    }
                }
            )
            service.set_heartbeat_enabled(
                session_id=session.session_id,
                enabled=True,
                interval_min=60,
                timezone_name="UTC",
            )
            outcome = await service.run_heartbeat_once(session_id=session.session_id, dry_run=True)
            self.assertTrue(outcome.get("ok"))
            self.assertEqual(outcome.get("action"), "ACTION")
            self.assertTrue(outcome.get("dry_run"))

    async def test_quiet_hours_gates_proactive_delivery(self):
        with tempfile.TemporaryDirectory() as tmp:
            sent = []

            async def _capture(payload):
                sent.append(payload)

            with patch.dict(os.environ, {"ENABLE_HEARTBEAT": "1"}, clear=False):
                service = build_agent_service(
                    state_db_path=Path(tmp) / "state.db",
                    config_dir=Path(tmp),
                )
            service.register_proactive_transport("capture", _capture)
            session = service.get_or_create_session(chat_id=31, user_id=32)
            ws = service.session_workspace(session.session_id)
            # Force quiet hours to cover current UTC time.
            now = datetime.now(timezone.utc)
            start = (now - timedelta(hours=1)).strftime("%H:%M")
            end = (now + timedelta(hours=1)).strftime("%H:%M")
            (ws / "memory" / "HEARTBEAT.md").write_text(
                "# HEARTBEAT v1\n"
                "## Daily (active hours only)\n"
                "- [ ] Review today's obligations\n"
                "## Weekly\n"
                "- [ ] Weekly review\n"
                "## Monitors\n"
                "- [ ] Check GitHub issues assigned to me\n"
                "## Waiting on\n"
                "- [ ] Replies from clients\n"
                "## Quiet Hours\n"
                f"- start: {start}\n"
                f"- end: {end}\n",
                encoding="utf-8",
            )
            service.set_heartbeat_enabled(
                session_id=session.session_id,
                enabled=True,
                interval_min=60,
                timezone_name="UTC",
            )
            state = service.run_store.get_heartbeat_state(session.session_id)
            service.run_store.set_heartbeat_state(
                session_id=session.session_id,
                heartbeat_enabled=True,
                heartbeat_interval_min=60,
                timezone_name="UTC",
                next_heartbeat_at=(datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(),
                last_heartbeat_at=state.get("last_heartbeat_at") or None,
            )
            stats = await service.run_cron_tick_once()
            self.assertGreaterEqual(stats.get("heartbeat_ran", 0), 1)
            self.assertEqual(len(sent), 0)

    async def test_spend_ceiling_gates_delivery(self):
        with tempfile.TemporaryDirectory() as tmp:
            sent = []

            async def _capture(payload):
                sent.append(payload)

            with patch.dict(
                os.environ,
                {
                    "ENABLE_HEARTBEAT": "1",
                    "MESSAGE_SEND_COST_USD": "5",
                },
                clear=False,
            ):
                service = build_agent_service(
                    state_db_path=Path(tmp) / "state.db",
                    config_dir=Path(tmp),
                )
                service.register_proactive_transport("capture", _capture)
                session = service.get_or_create_session(chat_id=41, user_id=42)
                tm = ThinMemoryStore(service.session_workspace(session.session_id))
                (service.session_workspace(session.session_id) / "memory" / "HEARTBEAT.md").write_text(
                    "# HEARTBEAT v1\n"
                    "## Daily (active hours only)\n"
                    "- [ ] Review today's obligations\n"
                    "## Weekly\n"
                    "- [ ] Weekly review\n"
                    "## Monitors\n"
                    "- [ ] Check GitHub issues assigned to me\n"
                    "## Waiting on\n"
                    "- [ ] Replies from clients\n"
                    "## Quiet Hours\n"
                    "- start: 00:00\n"
                    "- end: 00:00\n",
                    encoding="utf-8",
                )
                today = datetime.now(timezone.utc).date().isoformat()
                tm.update_index_patch(
                    {
                        "obligations": {
                            "upsert": [
                                {
                                    "obligation_id": "O001",
                                    "text": "Send invoice",
                                    "due": today,
                                    "ref": "memory/pages/tasks.md#invoices",
                                }
                            ]
                        }
                    }
                )
                access = service.access_controller
                access.set_profile(
                    UserProfile(
                        user_id=42,
                        chat_id=41,
                        roles=["user"],
                        spend_limit_usd=1.0,
                    )
                )
                service.set_heartbeat_enabled(
                    session_id=session.session_id,
                    enabled=True,
                    interval_min=60,
                    timezone_name="UTC",
                )
                outcome = await service.run_heartbeat_once(session_id=session.session_id, dry_run=False)
                self.assertTrue(outcome.get("action") == "ACTION")
                self.assertFalse(bool(outcome.get("delivery_ok", True)))
                self.assertEqual(len(sent), 0)


if __name__ == "__main__":
    unittest.main()
