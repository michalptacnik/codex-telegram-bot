import tempfile
import unittest
from pathlib import Path

from codex_telegram_bot.persistence.sqlite_store import SqliteRunStore
from codex_telegram_bot.services.whatsapp_bridge import WhatsAppBridge


class _Profile:
    def __init__(self, roles):
        self.roles = roles


class _Access:
    def __init__(self, roles=None):
        self._roles = roles or ["user"]

    def get_profile(self, user_id, chat_id):  # noqa: ARG002
        return _Profile(self._roles)


class _FakeService:
    def __init__(self, store, *, admin=False, warning=""):
        self._store = store
        self.access_controller = _Access(["admin"] if admin else ["user"])
        self._warning = warning

    def execution_profile_warning(self):
        return self._warning

    def get_or_create_session(self, chat_id, user_id):
        return self._store.get_or_create_active_session(chat_id=chat_id, user_id=user_id)

    async def run_prompt_with_tool_loop(
        self, prompt, chat_id, user_id, session_id, agent_id="default", progress_callback=None  # noqa: ARG002
    ):
        return f"echo:{prompt}"

    def list_pending_tool_approvals(self, chat_id, user_id, limit=20):
        return self._store.list_pending_tool_approvals(chat_id=chat_id, user_id=user_id, limit=limit)

    async def approve_tool_action(self, approval_id, chat_id, user_id):  # noqa: ARG002
        self._store.set_tool_approval_status(approval_id, "approved")
        return f"Approved {approval_id[:8]}"

    def deny_tool_action(self, approval_id, chat_id, user_id):  # noqa: ARG002
        self._store.set_tool_approval_status(approval_id, "denied")
        return f"Denied {approval_id[:8]}"


class TestWhatsAppBridge(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = SqliteRunStore(db_path=Path(self.tmp.name) / "state.db")
        self.service = _FakeService(self.store)
        self.sent = []

        async def _sender(external_user_id: str, text: str):
            self.sent.append((external_user_id, text))

        self.bridge = WhatsAppBridge(
            agent_service=self.service,
            run_store=self.store,
            sender=_sender,
        )

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def test_link_code_and_chat_flow(self):
        code_data = self.bridge.create_link_code(chat_id=77, user_id=88)
        reply = await self.bridge.handle_inbound(
            external_user_id="whatsapp:+15551234567",
            text=f"/link {code_data['code']}",
        )
        self.assertIn("Linked successfully", reply)

        reply2 = await self.bridge.handle_inbound(
            external_user_id="whatsapp:+15551234567",
            text="hello world",
        )
        self.assertEqual(reply2, "echo:hello world")

    async def test_unlinked_number_rejected(self):
        out = await self.bridge.handle_inbound(
            external_user_id="whatsapp:+18885550123",
            text="hey",
        )
        self.assertIn("not linked", out.lower())

    async def test_approve_and_deny_commands(self):
        code_data = self.bridge.create_link_code(chat_id=100, user_id=200)
        await self.bridge.handle_inbound(
            external_user_id="whatsapp:+19998887777",
            text=f"/link {code_data['code']}",
        )
        session = self.store.get_or_create_active_session(chat_id=100, user_id=200)
        approval_id = self.store.create_tool_approval(
            chat_id=100,
            user_id=200,
            session_id=session.session_id,
            agent_id=session.current_agent_id,
            run_id="",
            argv=["echo", "hello"],
            stdin_text="",
            timeout_sec=10,
            risk_tier="high",
        )
        approve_reply = await self.bridge.handle_inbound(
            external_user_id="whatsapp:+19998887777",
            text=f"/approve {approval_id[:8]}",
        )
        self.assertIn("Approved", approve_reply)
        approved = self.store.get_tool_approval(approval_id)
        self.assertEqual(approved["status"], "approved")

    async def test_proactive_delivery_fans_out_to_linked_number(self):
        code_data = self.bridge.create_link_code(chat_id=12, user_id=34)
        await self.bridge.handle_inbound(
            external_user_id="whatsapp:+17775550123",
            text=f"/link {code_data['code']}",
        )
        await self.bridge.deliver_proactive(
            {"chat_id": 12, "user_id": 34, "text": "scheduled reminder"}
        )
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0][0], "whatsapp:+17775550123")
        self.assertEqual(self.sent[0][1], "scheduled reminder")

    async def test_admin_receives_unsafe_warning(self):
        service = _FakeService(self.store, admin=True, warning="UNSAFE MODE ENABLED")
        bridge = WhatsAppBridge(agent_service=service, run_store=self.store, sender=None)
        code_data = bridge.create_link_code(chat_id=3, user_id=4)
        await bridge.handle_inbound(
            external_user_id="whatsapp:+11111111111",
            text=f"/link {code_data['code']}",
        )
        out = await bridge.handle_inbound(
            external_user_id="whatsapp:+11111111111",
            text="/pending",
        )
        self.assertIn("UNSAFE MODE ENABLED", out)


if __name__ == "__main__":
    unittest.main()
