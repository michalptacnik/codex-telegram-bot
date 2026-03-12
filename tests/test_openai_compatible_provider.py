from __future__ import annotations

import unittest

from codex_telegram_bot.providers.openai_compatible import _is_transient_exception


class TestOpenAICompatibleProviderTransientDetection(unittest.TestCase):
    def test_incomplete_read_is_transient(self):
        self.assertTrue(_is_transient_exception(Exception("IncompleteRead(0 bytes read)")))

