import tempfile
import unittest
from pathlib import Path

from codex_telegram_bot.persistence.sqlite_store import SqliteRunStore
from codex_telegram_bot.services.access_control import AccessController, UserProfile
from codex_telegram_bot.services.proactive_messenger import ProactiveMessenger
from codex_telegram_bot.tools.base import ToolContext, ToolRequest
from codex_telegram_bot.tools.file_transfer import SendFileTool


class TestSendFileTool(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addAsyncCleanup(self._cleanup_tmp)

        db_path = Path(self.tmp.name) / "state.db"
        self.store = SqliteRunStore(db_path=db_path)
        self.access = AccessController()
        self.messenger = ProactiveMessenger()
        self.deliveries = []

        async def _capture(payload):
            self.deliveries.append(payload)

        self.messenger.register("capture", _capture)
        self.tool = SendFileTool(
            run_store=self.store,
            access_controller=self.access,
            messenger=self.messenger,
        )

        self.s1 = self.store.create_session(chat_id=101, user_id=1001, current_agent_id="default")
        self.s2 = self.store.create_session(chat_id=101, user_id=2002, current_agent_id="default")

        self.workspace_root = Path(self.tmp.name) / "workspace"
        self.workspace_root.mkdir(parents=True, exist_ok=True)

    async def _cleanup_tmp(self):
        self.tmp.cleanup()

    async def test_send_file_refuses_missing_file(self):
        result = await self.tool.arun(
            ToolRequest(name="send_file", args={"session_id": self.s1.session_id, "path": "missing.txt"}),
            ToolContext(workspace_root=self.workspace_root, chat_id=101, user_id=1001, session_id=self.s1.session_id),
        )
        self.assertFalse(result.ok)
        self.assertIn("File does not exist", result.output)

    async def test_send_file_refuses_absolute_path_outside_workspace(self):
        outside = Path(self.tmp.name) / "outside.txt"
        outside.write_text("nope", encoding="utf-8")
        result = await self.tool.arun(
            ToolRequest(name="send_file", args={"session_id": self.s1.session_id, "path": str(outside)}),
            ToolContext(
                workspace_root=self.workspace_root,
                policy_profile="trusted",
                chat_id=101,
                user_id=1001,
                session_id=self.s1.session_id,
            ),
        )
        self.assertFalse(result.ok)
        self.assertIn("outside workspace root", result.output)

    async def test_send_file_denies_cross_user_session(self):
        path = self.workspace_root / "note.txt"
        path.write_text("hello", encoding="utf-8")
        result = await self.tool.arun(
            ToolRequest(name="send_file", args={"session_id": self.s2.session_id, "path": "note.txt"}),
            ToolContext(workspace_root=self.workspace_root, chat_id=101, user_id=1001, session_id=self.s1.session_id),
        )
        self.assertFalse(result.ok)
        self.assertIn("Access denied", result.output)

    async def test_send_file_allows_admin_cross_user_session(self):
        self.access.set_profile(UserProfile(user_id=1001, chat_id=101, roles=["admin"], spend_limit_usd=10.0))
        path = self.workspace_root / "admin.txt"
        path.write_text("admin", encoding="utf-8")
        result = await self.tool.arun(
            ToolRequest(name="send_file", args={"session_id": self.s2.session_id, "path": "admin.txt"}),
            ToolContext(workspace_root=self.workspace_root, chat_id=101, user_id=1001, session_id=self.s1.session_id),
        )
        self.assertTrue(result.ok)
        self.assertEqual(len(self.deliveries), 1)

    async def test_send_file_success_records_attachment_and_message(self):
        path = self.workspace_root / "report.txt"
        path.write_text("report", encoding="utf-8")
        result = await self.tool.arun(
            ToolRequest(
                name="send_file",
                args={"session_id": self.s1.session_id, "path": "report.txt", "caption": "Here is the report"},
            ),
            ToolContext(workspace_root=self.workspace_root, chat_id=101, user_id=1001, session_id=self.s1.session_id),
        )
        self.assertTrue(result.ok)
        self.assertEqual(len(self.deliveries), 1)
        self.assertEqual(self.deliveries[0]["session_id"], self.s1.session_id)

        attachments = self.store.list_session_attachments(self.s1.session_id)
        self.assertEqual(len(attachments), 1)
        self.assertEqual(attachments[0]["filename"], "report.txt")
        self.assertTrue(attachments[0]["message_id"])

        with self.store._connect() as conn:
            row = conn.execute("SELECT * FROM messages WHERE id = ?", (attachments[0]["message_id"],)).fetchone()
        self.assertIsNotNone(row)


if __name__ == "__main__":
    unittest.main()
