import unittest

from codex_telegram_bot.services.continuation_guard import (
    PRELIMINARY_CONTINUE_HANDOFF,
    build_preliminary_action_report,
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
        out = sanitize_terminal_output(text)
        self.assertIn(text, out)
        self.assertIn(PRELIMINARY_CONTINUE_HANDOFF, out)
        self.assertEqual(
            sanitize_terminal_output("Final status report: completed with concrete outcome."),
            "Final status report: completed with concrete outcome.",
        )

    def test_terminal_sanitizer_does_not_duplicate_continue_handoff(self):
        text = "I'll continue and check one more thing.\n\nReply with 'continue' to run the next step."
        out = sanitize_terminal_output(text)
        self.assertEqual(out, text)

    def test_build_preliminary_action_report_contains_tools_and_continue_prompt(self):
        report = build_preliminary_action_report(
            action_summaries=[
                {"goal": "verify skill marketplace flow", "tools": ["skills_market_sources_list", "skills_market_search"]},
                {"goal": "check workspace files", "tools": ["read_file"]},
            ],
            max_actions=2,
        )
        self.assertIn("Preliminary report", report)
        self.assertIn("skills_market_sources_list, skills_market_search", report)
        self.assertIn("Do you want me to continue?", report)
        self.assertIn(PRELIMINARY_CONTINUE_HANDOFF, report)


if __name__ == "__main__":
    unittest.main()
