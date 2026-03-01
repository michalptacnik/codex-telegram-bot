import re
import unittest

from codex_telegram_bot.presentation.formatter import format_message


def _canonical(text: str) -> str:
    raw = str(text or "")
    raw = raw.replace("**", "").replace("*", "")
    raw = raw.replace("✅ ", "").replace("⚠️ ", "").replace("📌 ", "").replace("🧠 ", "").replace("🔧 ", "")
    raw = re.sub(r"\s+", " ", raw)
    return raw.strip().lower()


class TestPresentationFormatter(unittest.TestCase):
    def test_telegram_markdownv2_escaping(self):
        source = "Plan:\n1) fix parser\nPath: foo_bar(baz).md #1"
        result = format_message(
            source,
            channel="telegram",
            style={"emoji": "off", "emphasis": "light", "brevity": "short"},
        )
        self.assertEqual(result.parse_mode, "MarkdownV2")
        self.assertIn("*Plan:*", result.formatted_text)
        self.assertIn("\\-", result.formatted_text)
        self.assertIn("foo\\_bar\\(baz\\)\\.md", result.formatted_text)
        self.assertIn("\\#1", result.formatted_text)

    def test_web_structure_and_lists(self):
        source = "Plan:\n1) first step\n2) second step"
        result = format_message(
            source,
            channel="web",
            style={"emoji": "off", "emphasis": "light", "brevity": "short"},
        )
        self.assertIsNone(result.parse_mode)
        self.assertIn("**Plan:**", result.formatted_text)
        self.assertIn("- 1) first step", result.formatted_text)
        self.assertIn("- 2) second step", result.formatted_text)

    def test_emoji_limits_respected(self):
        result_on = format_message(
            "Done. Completed successfully.",
            channel="web",
            style={"emoji": "light", "emphasis": "plain", "brevity": "short"},
        )
        self.assertTrue(result_on.formatted_text.startswith("✅ "))
        self.assertLessEqual(result_on.safety_report.get("emoji_count", 0), 2)

        result_off = format_message(
            "Done. Completed successfully.",
            channel="web",
            style={"emoji": "off", "emphasis": "plain", "brevity": "short"},
        )
        self.assertFalse(result_off.formatted_text.startswith("✅ "))
        self.assertEqual(result_off.safety_report.get("emoji_count", 0), 0)

    def test_no_content_drift_except_whitespace(self):
        source = "First sentence. Second sentence! Third sentence?\n\nPlan:\n1) ship patch"
        result = format_message(
            source,
            channel="web",
            style={"emoji": "off", "emphasis": "light", "brevity": "short"},
        )
        canon_formatted = _canonical(result.formatted_text)
        for fragment in ("First sentence.", "Second sentence!", "Third sentence?", "Plan:", "1) ship patch"):
            self.assertIn(_canonical(fragment), canon_formatted)


if __name__ == "__main__":
    unittest.main()
