import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from codex_telegram_bot.persistence.sqlite_store import SqliteRunStore
from codex_telegram_bot.services.heartbeat import HeartbeatStore
from codex_telegram_bot.services.thin_memory import ThinMemoryStore
from codex_telegram_bot.tools.base import ToolContext, ToolRequest
from codex_telegram_bot.tools.heartbeat import HeartbeatGetTool, HeartbeatRunOnceTool, HeartbeatUpdateTool


class TestHeartbeatTools(unittest.IsolatedAsyncioTestCase):
    async def test_heartbeat_get_update_and_run_once_dry_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = SqliteRunStore(db_path=root / "state.db")
            session = db.create_session(chat_id=10, user_id=11, current_agent_id="default")
            ws = root / "workspace"
            ws.mkdir(parents=True, exist_ok=True)
            hb_store = HeartbeatStore(ws)
            self.assertTrue(hb_store.path.exists())

            update = HeartbeatUpdateTool()
            get = HeartbeatGetTool()
            run_once = HeartbeatRunOnceTool(run_store=db, access_controller=None, messenger=None)

            patched = update.run(
                ToolRequest(
                    name="heartbeat_update",
                    args={
                        "patch": {
                            "daily": ["Review obligations"],
                            "quiet_hours": {"start": "23:00", "end": "07:00"},
                        }
                    },
                ),
                ToolContext(workspace_root=ws, chat_id=10, user_id=11, session_id=session.session_id),
            )
            self.assertTrue(patched.ok, patched.output)
            loaded = get.run(
                ToolRequest(name="heartbeat_get", args={}),
                ToolContext(workspace_root=ws, chat_id=10, user_id=11, session_id=session.session_id),
            )
            self.assertTrue(loaded.ok)
            self.assertIn("# HEARTBEAT v1", loaded.output)
            self.assertIn("Review obligations", loaded.output)

            tm = ThinMemoryStore(ws)
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
            dry = await run_once.arun(
                ToolRequest(
                    name="heartbeat_run_once",
                    args={"session_id": session.session_id, "dry_run": True},
                ),
                ToolContext(workspace_root=ws, chat_id=10, user_id=11, session_id=session.session_id),
            )
            self.assertTrue(dry.ok, dry.output)
            payload = json.loads(dry.output)
            self.assertEqual(payload.get("action"), "ACTION")
            self.assertTrue(payload.get("dry_run"))


if __name__ == "__main__":
    unittest.main()
