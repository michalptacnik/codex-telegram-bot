import unittest

from codex_telegram_bot.services.continuation_guard import (
    continuation_status_line,
    looks_like_preliminary_report,
)


class TestContinuationGuard(unittest.TestCase):
    def test_detects_preliminary_progress_message(self):
        text = "I'm still working on this and trying a different approach now."
        self.assertTrue(looks_like_preliminary_report(text))

    def test_ignores_final_or_blocking_messages(self):
        self.assertFalse(looks_like_preliminary_report("Final status report: done with concrete outcome."))
        self.assertFalse(looks_like_preliminary_report("Which environment should I use?"))

    def test_status_line_varies_by_context(self):
        self.assertIn("different approach", continuation_status_line("tool failed with error"))
        self.assertIn("search approach", continuation_status_line("I will search next"))
        self.assertIn("verifying", continuation_status_line("let me check one more thing"))


if __name__ == "__main__":
    unittest.main()

