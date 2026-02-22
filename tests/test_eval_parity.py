import unittest

from codex_telegram_bot.eval_parity import (
    BenchmarkCase,
    aggregate_case_rows,
    contains_ratio,
    evaluate_gates,
    evaluate_output,
    forbidden_ok,
    text_similarity,
)


class TestEvalParity(unittest.TestCase):
    def test_contains_ratio(self):
        score = contains_ratio("hello world alpha", ["hello", "alpha", "missing"])
        self.assertAlmostEqual(score, 2 / 3, places=4)

    def test_forbidden_ok(self):
        self.assertTrue(forbidden_ok("all clear", ["error"]))
        self.assertFalse(forbidden_ok("error happened", ["error"]))

    def test_text_similarity(self):
        self.assertGreater(text_similarity("alpha beta", "alpha beta"), 0.99)
        self.assertLess(text_similarity("alpha beta", "omega"), 0.5)

    def test_evaluate_output(self):
        case = BenchmarkCase(
            case_id="c1",
            prompt="x",
            expected_contains=["ok"],
            forbidden_contains=["error"],
            max_latency_sec=2.0,
        )
        result = evaluate_output(case=case, output="ok done", latency_sec=1.2)
        self.assertTrue(result["completed"])
        self.assertEqual(result["expected_match"], 1.0)
        self.assertTrue(result["forbidden_passed"])
        self.assertEqual(result["user_corrections_required"], 0)

    def test_gate_eval(self):
        rows = [
            {
                "telegram": {
                    "completed": True,
                    "expected_match": 1.0,
                    "forbidden_passed": True,
                    "latency_sec": 2.0,
                    "user_corrections_required": 0,
                },
                "similarity_to_codex": 0.9,
            },
            {
                "telegram": {
                    "completed": True,
                    "expected_match": 0.8,
                    "forbidden_passed": True,
                    "latency_sec": 3.0,
                    "user_corrections_required": 1,
                },
                "similarity_to_codex": 0.7,
            },
        ]
        summary = aggregate_case_rows(rows)
        gates = evaluate_gates(
            summary=summary,
            min_completion_rate=0.9,
            min_expected_match=0.8,
            min_similarity=0.6,
            max_p95_latency_sec=5.0,
            max_corrections=2,
        )
        self.assertTrue(gates["pass"])


if __name__ == "__main__":
    unittest.main()
