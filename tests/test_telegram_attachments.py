import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from codex_telegram_bot.telegram_bot import handle_attachment


class _DummyAgentService:
    def __init__(self, workspace: Path):
        self._workspace = workspace
        self.run_store = SimpleNamespace(count_attachments_for_session_day=lambda _session_id, _day: 0)

    def get_or_create_session(self, *, chat_id: int, user_id: int):
        return SimpleNamespace(session_id="s-1")

    def initialize_session_workspace(self, session_id: str):
        return None

    def session_workspace(self, session_id: str) -> Path:
        self._workspace.mkdir(parents=True, exist_ok=True)
        return self._workspace

    def record_channel_message(self, **_kwargs):
        return "m-1"

    def record_attachment(self, **_kwargs):
        raise AssertionError("record_attachment should not be called for oversized upload")


class TestTelegramAttachmentLimits(unittest.IsolatedAsyncioTestCase):
    async def test_oversized_attachment_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent_service = _DummyAgentService(workspace=Path(tmp) / "workspace")

            message = SimpleNamespace(
                from_user=SimpleNamespace(id=111),
                message_id=222,
                caption="",
                document=SimpleNamespace(
                    file_id="file-1",
                    file_name="too-big.bin",
                    mime_type="application/octet-stream",
                    file_size=999,
                ),
                photo=None,
                audio=None,
                video=None,
                reply_text=AsyncMock(),
            )
            update = SimpleNamespace(message=message, effective_chat=SimpleNamespace(id=333))
            context = SimpleNamespace(
                bot_data={"allowlist": None, "agent_service": agent_service},
                application=SimpleNamespace(bot_data={}),
                bot=SimpleNamespace(get_file=AsyncMock()),
            )

            with (
                patch("codex_telegram_bot.telegram_bot.MAX_TELEGRAM_ATTACHMENT_BYTES", 100),
                patch("codex_telegram_bot.telegram_bot.MAX_TELEGRAM_ATTACHMENTS_PER_DAY", 50),
                patch("codex_telegram_bot.telegram_bot._process_prompt", new=AsyncMock()) as process_prompt,
            ):
                await handle_attachment(update, context)

            context.bot.get_file.assert_not_called()
            process_prompt.assert_not_awaited()
            message.reply_text.assert_awaited()
            sent_text = str(message.reply_text.await_args.args[0])
            self.assertIn("No valid attachments were stored", sent_text)


if __name__ == "__main__":
    unittest.main()
