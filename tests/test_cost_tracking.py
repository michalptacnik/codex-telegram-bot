import tempfile
import unittest
from pathlib import Path

from codex_telegram_bot.persistence.sqlite_store import SqliteRunStore
from codex_telegram_bot.services.cost_tracking import estimate_cost_usd, normalize_usage


class TestCostTracking(unittest.TestCase):
    def test_normalize_usage_supports_multiple_shapes(self):
        prompt, completion, total = normalize_usage({"input_tokens": 10, "output_tokens": 3})
        self.assertEqual((prompt, completion, total), (10, 3, 13))
        prompt2, completion2, total2 = normalize_usage({"prompt_tokens": 7, "completion_tokens": 2, "total_tokens": 9})
        self.assertEqual((prompt2, completion2, total2), (7, 2, 9))

    def test_estimate_cost_returns_value_when_model_known(self):
        value = estimate_cost_usd(
            provider="openai",
            model="gpt-4.1-mini",
            prompt_tokens=1000,
            completion_tokens=2000,
            usage={},
            config_dir=None,
        )
        self.assertIsNotNone(value)
        self.assertGreater(float(value), 0.0)

    def test_usage_events_persist_and_roll_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SqliteRunStore(db_path=Path(tmp) / "state.db")
            store.record_usage_event(
                user_id="42",
                session_id="s1",
                provider="openai",
                model="gpt-4.1-mini",
                prompt_tokens=100,
                completion_tokens=50,
                total_tokens=150,
                cost_usd_estimate=0.001,
                meta={"source": "test"},
            )
            store.record_usage_event(
                user_id="42",
                session_id="s1",
                provider="openai",
                model="gpt-4.1-mini",
                prompt_tokens=20,
                completion_tokens=30,
                total_tokens=50,
                cost_usd_estimate=0.002,
                meta={"source": "test"},
            )
            session_rollup = store.get_session_cost_rollup("s1")
            self.assertEqual(session_rollup["total_tokens"], 200)
            self.assertAlmostEqual(session_rollup["total_cost_usd"], 0.003, places=6)
            daily = store.list_user_daily_cost_rollups(limit=5)
            self.assertTrue(daily)
            self.assertEqual(daily[0]["user_id"], "42")
            self.assertEqual(daily[0]["total_tokens"], 200)


if __name__ == "__main__":
    unittest.main()

