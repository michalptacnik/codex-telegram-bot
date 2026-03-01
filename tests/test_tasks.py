import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from codex_telegram_bot.services.heartbeat import HeartbeatStore
from codex_telegram_bot.services.thin_memory import ThinMemoryStore
from codex_telegram_bot.tools.base import ToolContext, ToolRequest
from codex_telegram_bot.tools.tasks import TaskCreateTool, TaskDoneTool, TaskListTool


class TestTaskTools(unittest.TestCase):
    def test_task_create_list_done_updates_memory_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            create = TaskCreateTool()
            listing = TaskListTool()
            done = TaskDoneTool()
            ctx = ToolContext(workspace_root=root)

            out = create.run(
                ToolRequest(
                    name="task_create",
                    args={
                        "title": "Send invoice",
                        "due": "2026-03-05",
                        "details": "Client X",
                        "tags": ["finance", "urgent"],
                    },
                ),
                ctx,
            )
            self.assertTrue(out.ok, out.output)
            self.assertIn("Created task T", out.output)

            listed = listing.run(ToolRequest(name="task_list", args={}), ctx)
            self.assertTrue(listed.ok)
            self.assertIn("Send invoice", listed.output)

            task_id = listed.output.split()[1]
            done_out = done.run(
                ToolRequest(name="task_done", args={"task_id": task_id}),
                ctx,
            )
            self.assertTrue(done_out.ok, done_out.output)
            self.assertIn("Task completed", done_out.output)

            tm = ThinMemoryStore(root)
            index = tm.load_index()
            self.assertFalse(any(o.obligation_id == task_id for o in index.obligations))

    def test_heartbeat_surfaces_due_tasks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tm = ThinMemoryStore(root)
            today = datetime.now(timezone.utc).date().isoformat()
            tm.create_task(title="Pay hosting", due=today, details="", tags=["ops"])
            hb = HeartbeatStore(root)
            decision = hb.evaluate(timezone_name="UTC", now_utc=datetime.now(timezone.utc))
            self.assertEqual(decision.action, "ACTION")
            self.assertTrue(
                ("tasks due or overdue" in decision.text) or ("obligations due or overdue" in decision.text)
            )


if __name__ == "__main__":
    unittest.main()
