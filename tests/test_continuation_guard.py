import unittest

from codex_telegram_bot.services.continuation_guard import (
    PRELIMINARY_TERMINAL_FALLBACK,
    continuation_status_line,
    looks_like_preliminary_report,
    sanitize_terminal_output,
)


class TestContinuationGuard(unittest.TestCase):
    def test_detects_preliminary_progress_message(self):
        text = "I'm still working on this and trying a different approach now."
        self.assertTrue(looks_like_preliminary_report(text))

    def test_ignores_final_or_blocking_messages(self):
        self.assertFalse(looks_like_preliminary_report("Final status report: done with concrete outcome."))
        self.assertFalse(looks_like_preliminary_report("Which environment should I use?"))

    def test_detects_real_world_continue_phrase(self):
        text = (
            "I'll continue executing the task to set up the DeepSeek bot. "
            "Let me check what's been done so far and continue with the setup."
        )
        self.assertTrue(looks_like_preliminary_report(text))

    def test_status_line_varies_by_context(self):
        self.assertIn("different approach", continuation_status_line("tool failed with error"))
        self.assertIn("search approach", continuation_status_line("I will search next"))
        self.assertIn("verifying", continuation_status_line("let me check one more thing"))

    def test_terminal_sanitizer_rewrites_preliminary_output(self):
        text = "I'll continue and check one more thing."
        self.assertEqual(sanitize_terminal_output(text), PRELIMINARY_TERMINAL_FALLBACK)
        self.assertEqual(
            sanitize_terminal_output("Final status report: completed with concrete outcome."),
            "Final status report: completed with concrete outcome.",
        )


if __name__ == "__main__":
    unittest.main()
