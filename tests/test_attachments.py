import tempfile
import unittest
from pathlib import Path

from codex_telegram_bot.persistence.sqlite_store import SqliteRunStore
from codex_telegram_bot.services.workspace_manager import WorkspaceManager


class TestAttachmentStorage(unittest.TestCase):
    def test_message_attachment_relation_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "state.db"
            store = SqliteRunStore(db_path=db_path)
            session = store.create_session(chat_id=10, user_id=20, current_agent_id="default")

            message_id = store.create_channel_message(
                session_id=session.session_id,
                user_id=20,
                channel="telegram",
                channel_message_id="123",
                sender="user",
                text="uploaded",
            )
            attachment_id = store.create_attachment(
                message_id=message_id,
                session_id=session.session_id,
                user_id=20,
                channel="telegram",
                kind="document",
                filename="invoice.pdf",
                mime="application/pdf",
                size_bytes=123,
                sha256="abc123",
                local_path="/tmp/invoice.pdf",
                remote_file_id="tg-file-1",
            )

            item = store.get_attachment(attachment_id)
            self.assertIsNotNone(item)
            self.assertEqual(item["message_id"], message_id)
            rows = store.list_session_attachments(session.session_id)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["id"], attachment_id)


class TestWorkspaceResolvePath(unittest.TestCase):
    def test_resolve_path_rejects_traversal(self):
        with tempfile.TemporaryDirectory() as tmp:
            mgr = WorkspaceManager(root=Path(tmp) / "ws")
            with self.assertRaises(ValueError):
                mgr.resolve_path("s1", "../../etc/passwd")

    def test_resolve_path_rejects_absolute_outside_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "ws"
            outside = Path(tmp) / "outside.txt"
            outside.write_text("x", encoding="utf-8")
            mgr = WorkspaceManager(root=root)
            with self.assertRaises(ValueError):
                mgr.resolve_path("s1", str(outside), allow_absolute=False)


if __name__ == "__main__":
    unittest.main()
