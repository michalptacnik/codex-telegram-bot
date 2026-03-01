import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from codex_telegram_bot.app_container import build_agent_service
from codex_telegram_bot.persistence.sqlite_store import SqliteRunStore
import codex_telegram_bot.services.cron_utils as cron_utils
from codex_telegram_bot.services.cron_utils import cron_next_run, parse_natural_when
from codex_telegram_bot.tools.base import ToolContext, ToolRequest
from codex_telegram_bot.tools.schedule import ScheduleTaskTool


class TestCronUtils(unittest.TestCase):
    def test_parse_next_monday_at_time(self):
        tz_name = "Europe/Amsterdam"
        base = datetime(2026, 3, 1, 10, 0, tzinfo=ZoneInfo(tz_name))  # Sunday
        parsed = parse_natural_when("next Monday at 23:45", tz_name=tz_name, now=base)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.weekday(), 0)
        self.assertEqual(parsed.hour, 23)
        self.assertEqual(parsed.minute, 45)

    def test_cron_next_run_every_five_minutes(self):
        base = datetime(2026, 3, 1, 10, 2, tzinfo=ZoneInfo("Europe/Amsterdam"))
        nxt = cron_next_run("*/5 * * * *", base)
        self.assertEqual((nxt.hour, nxt.minute), (10, 5))

    def test_fallback_parses_tomorrow_without_dateparser(self):
        tz_name = "Europe/Amsterdam"
        base = datetime(2026, 3, 1, 16, 0, tzinfo=ZoneInfo(tz_name))
        original = cron_utils.dateparser
        cron_utils.dateparser = None
        try:
            parsed = parse_natural_when("tomorrow 9:00", tz_name=tz_name, now=base)
            self.assertIsNotNone(parsed)
            self.assertEqual((parsed.year, parsed.month, parsed.day), (2026, 3, 2))
            self.assertEqual((parsed.hour, parsed.minute), (9, 0))
        finally:
            cron_utils.dateparser = original

    def test_fallback_parses_relative_offset_without_dateparser(self):
        tz_name = "Europe/Amsterdam"
        base = datetime(2026, 3, 1, 16, 0, tzinfo=ZoneInfo(tz_name))
        original = cron_utils.dateparser
        cron_utils.dateparser = None
        try:
            parsed = parse_natural_when("in 2 hours", tz_name=tz_name, now=base)
            self.assertIsNotNone(parsed)
            self.assertEqual((parsed.hour, parsed.minute), (18, 0))
        finally:
            cron_utils.dateparser = original


class TestCronSchedulingFlow(unittest.IsolatedAsyncioTestCase):
    async def test_due_job_ticks_and_delivers(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "state.db"
            service = build_agent_service(state_db_path=db_path, config_dir=Path(tmp))
            sent = []

            async def _capture(payload):
                sent.append(payload)

            service.register_proactive_transport("capture", _capture)
            session = service.get_or_create_session(chat_id=9001, user_id=9001)
            now = datetime.now(timezone.utc)
            job_id = service._run_store.create_cron_job(  # type: ignore[attr-defined]
                owner_user_id=str(session.user_id),
                session_id=session.session_id,
                one_shot=True,
                cron_expr="",
                next_run=(now - timedelta(minutes=1)).isoformat(),
                tz="Europe/Amsterdam",
                payload={"message": "Reminder ping"},
            )

            stats = await service.run_cron_tick_once()
            self.assertEqual(stats["ran"], 1)
            self.assertEqual(len(sent), 1)
            job = service._run_store.get_cron_job(job_id)  # type: ignore[attr-defined]
            self.assertEqual(job["status"], "canceled")

    async def test_schedule_task_tool_creates_job(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SqliteRunStore(db_path=Path(tmp) / "state.db")
            session = store.create_session(chat_id=10, user_id=11, current_agent_id="default")
            tool = ScheduleTaskTool(run_store=store, access_controller=None)
            result = tool.run(
                ToolRequest(
                    name="schedule_task",
                    args={
                        "when": "next Monday at 23:45",
                        "message": "Submit report",
                        "repeat": "none",
                        "session_id": session.session_id,
                    },
                ),
                ToolContext(workspace_root=Path(tmp), chat_id=10, user_id=11, session_id=session.session_id),
            )
            self.assertTrue(result.ok, result.output)
            jobs = store.list_cron_jobs(session_id=session.session_id, owner_user_id=str(session.user_id), include_non_active=True)
            self.assertEqual(len(jobs), 1)


if __name__ == "__main__":
    unittest.main()
