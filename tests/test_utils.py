import unittest

from codex_telegram_bot.util import redact, chunk_text
from codex_telegram_bot.config import parse_allowlist


class TestRedaction(unittest.TestCase):
    def test_redacts_sk_tokens(self):
        text = "key sk-abcdef1234567890xyz and more"
        self.assertIn("sk-REDACTED", redact(text))
        self.assertNotIn("sk-abcdef1234567890xyz", redact(text))


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
