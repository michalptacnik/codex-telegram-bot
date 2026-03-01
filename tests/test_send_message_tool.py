import tempfile
import unittest
from pathlib import Path

from codex_telegram_bot.persistence.sqlite_store import SqliteRunStore
from codex_telegram_bot.services.access_control import AccessController, UserProfile
from codex_telegram_bot.services.proactive_messenger import ProactiveMessenger
from codex_telegram_bot.tools import build_default_tool_registry
from codex_telegram_bot.tools.base import ToolContext, ToolRequest
from codex_telegram_bot.tools.message import SendMessageTool


class TestSendMessageTool(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addAsyncCleanup(self._cleanup_tmp)
        db_path = Path(self.tmp.name) / "state.db"
        self.store = SqliteRunStore(db_path=db_path)
        self.access = AccessController()
        self.messenger = ProactiveMessenger()
        self.sent = []

        async def _capture(payload):
            self.sent.append(payload)

        self.messenger.register("capture", _capture)
        self.tool = SendMessageTool(
            run_store=self.store,
            access_controller=self.access,
            messenger=self.messenger,
        )

        self.s1 = self.store.create_session(chat_id=101, user_id=1001, current_agent_id="default")
        self.s2 = self.store.create_session(chat_id=101, user_id=2002, current_agent_id="default")

    async def _cleanup_tmp(self):
        self.tmp.cleanup()

    async def test_send_message_to_own_session(self):
        result = await self.tool.arun(
            ToolRequest(name="send_message", args={"session_id": self.s1.session_id, "text": "hello"}),
            ToolContext(workspace_root=Path(self.tmp.name), chat_id=101, user_id=1001, session_id=self.s1.session_id),
        )
        self.assertTrue(result.ok)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0]["session_id"], self.s1.session_id)

    async def test_send_message_denies_cross_user_session_for_non_admin(self):
        result = await self.tool.arun(
            ToolRequest(name="send_message", args={"session_id": self.s2.session_id, "text": "hello"}),
            ToolContext(workspace_root=Path(self.tmp.name), chat_id=101, user_id=1001, session_id=self.s1.session_id),
        )
        self.assertFalse(result.ok)
        self.assertIn("Access denied", result.output)
        self.assertEqual(self.sent, [])

    async def test_send_message_allows_admin_cross_user_session(self):
        self.access.set_profile(UserProfile(user_id=1001, chat_id=101, roles=["admin"], spend_limit_usd=10.0))
        result = await self.tool.arun(
            ToolRequest(name="send_message", args={"session_id": self.s2.session_id, "text": "admin note"}),
            ToolContext(workspace_root=Path(self.tmp.name), chat_id=101, user_id=1001, session_id=self.s1.session_id),
        )
        self.assertTrue(result.ok)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0]["session_id"], self.s2.session_id)


class TestSendMessageRegistry(unittest.TestCase):
    def test_tool_registered_when_run_store_available(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SqliteRunStore(db_path=Path(tmp) / "state.db")
            registry = build_default_tool_registry(run_store=store)
            self.assertIsNotNone(registry.get("send_message"))


if __name__ == "__main__":
    unittest.main()

