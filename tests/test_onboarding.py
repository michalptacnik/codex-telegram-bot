import tempfile
import unittest
from pathlib import Path

from codex_telegram_bot.services.onboarding import OnboardingStore


class TestOnboardingStore(unittest.TestCase):
    def test_record_and_complete_persist(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = OnboardingStore(config_dir=Path(tmp))

            state1 = store.record("wizard.view", "visit")
            self.assertFalse(state1["completed"])
            self.assertEqual(state1["last_step"], "wizard.view")

            state2 = store.complete()
            self.assertTrue(state2["completed"])
            self.assertTrue(state2["completed_at"])

            reloaded = store.load()
            self.assertTrue(reloaded["completed"])
            self.assertEqual(
                reloaded["telemetry"]["steps"].get("wizard.view:visit"),
                1,
            )
