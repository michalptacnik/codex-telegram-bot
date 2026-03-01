import tempfile
import unittest
from pathlib import Path

from codex_telegram_bot.services.agent_service import (
    _compact_tool_output,
    _persist_tool_output_for_chat,
)


class TestAgentServiceToolOutput(unittest.TestCase):
    def test_persist_tool_output_only_when_long(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            short = _persist_tool_output_for_chat(
                output="ok",
                workspace_root=root,
                action_id="tool-1",
                kind="tool",
                max_inline_chars=20,
            )
            self.assertEqual(short, "")

            long_text = "A" * 500
            rel = _persist_tool_output_for_chat(
                output=long_text,
                workspace_root=root,
                action_id="tool-2",
                kind="exec",
                max_inline_chars=80,
            )
            self.assertTrue(rel.startswith("logs/tool_outputs/"))
            target = root / rel
            self.assertTrue(target.exists())
            self.assertEqual(target.read_text(encoding="utf-8"), long_text)

    def test_compact_tool_output_templates(self):
        success = _compact_tool_output("Completed action.")
        self.assertTrue(success.startswith("✅ Done:"))
        failure = _compact_tool_output("Error: BLOCKED")
        self.assertTrue(failure.startswith("⚠️ Tool error"))


if __name__ == "__main__":
    unittest.main()

