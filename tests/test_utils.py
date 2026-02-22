import os
import unittest

from codex_telegram_bot.util import redact, chunk_text, redact_with_audit, _compiled_patterns
from codex_telegram_bot.config import parse_allowlist


class TestRedaction(unittest.TestCase):
    def test_redacts_sk_tokens(self):
        text = "key sk-abcdef1234567890xyz and more"
        self.assertIn("sk-REDACTED", redact(text))
        self.assertNotIn("sk-abcdef1234567890xyz", redact(text))

    def test_redacts_key_value_and_bearer(self):
        text = "OPENAI_API_KEY=supersecret Authorization: Bearer abcdefghijklmnopqrstuvwxyz"
        out = redact(text)
        self.assertIn("OPENAI_API_KEY=REDACTED", out)
        self.assertIn("Bearer REDACTED", out)
        self.assertNotIn("supersecret", out)

    def test_custom_redaction_pattern_from_env(self):
        os.environ["REDACTION_EXTRA_PATTERNS"] = "foo_[A-Za-z0-9]{6,}"
        _compiled_patterns.cache_clear()
        result = redact_with_audit("token foo_abcdef123")
        self.assertTrue(result.redacted)
        self.assertIn("REDACTED", result.text)
        del os.environ["REDACTION_EXTRA_PATTERNS"]
        _compiled_patterns.cache_clear()


class TestChunking(unittest.TestCase):
    def test_chunk_size(self):
        text = "a" * 9000
        chunks = chunk_text(text, 3800)
        self.assertEqual(len(chunks), 3)
        self.assertTrue(all(len(c) <= 3800 for c in chunks))


class TestAllowlist(unittest.TestCase):
    def test_parse_allowlist(self):
        raw = "123, 456,abc, ,789"
        self.assertEqual(parse_allowlist(raw), [123, 456, 789])


if __name__ == "__main__":
    unittest.main()
