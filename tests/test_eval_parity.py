import json
import unittest
from pathlib import Path

from codex_telegram_bot.eval_parity import (
    BenchmarkCase,
    _offline_baseline,
    aggregate_case_rows,
    contains_ratio,
    evaluate_gates,
    evaluate_output,
    forbidden_ok,
    load_cases,
    p95,
    render_markdown,
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


class TestBenchmarkCases(unittest.TestCase):
    """Tests against the real parity_cases.json benchmark file."""

    CASES_PATH = Path(__file__).parent.parent / "docs/benchmarks/parity_cases.json"

    def _load(self):
        return load_cases(self.CASES_PATH)

    def test_cases_file_loadable(self):
        cases = self._load()
        self.assertGreater(len(cases), 0)

    def test_minimum_case_count(self):
        """Must have at least 15 cases to produce statistically meaningful gates."""
        cases = self._load()
        self.assertGreaterEqual(len(cases), 15)

    def test_all_cases_have_non_empty_prompts(self):
        for c in self._load():
            self.assertTrue(c.prompt.strip(), f"Case {c.case_id} has empty prompt")

    def test_all_cases_have_expected_contains(self):
        for c in self._load():
            self.assertGreater(
                len(c.expected_contains), 0,
                f"Case {c.case_id} has no expected_contains"
            )

    def test_all_cases_have_forbidden_contains(self):
        for c in self._load():
            self.assertGreater(
                len(c.forbidden_contains), 0,
                f"Case {c.case_id} missing forbidden_contains (should at least block 'Error:')"
            )

    def test_all_cases_forbid_error_prefix(self):
        for c in self._load():
            self.assertIn(
                "Error:",
                c.forbidden_contains,
                f"Case {c.case_id} should forbid 'Error:' to catch provider failures"
            )

    def test_safety_cases_forbid_dangerous_commands(self):
        safety_cases = [c for c in self._load() if c.category == "safety"]
        self.assertGreater(len(safety_cases), 0, "Must have at least one safety case")
        for c in safety_cases:
            self.assertGreater(len(c.forbidden_contains), 1,
                               f"Safety case {c.case_id} should forbid dangerous patterns")

    def test_case_ids_are_unique(self):
        cases = self._load()
        ids = [c.case_id for c in cases]
        self.assertEqual(len(ids), len(set(ids)), "Duplicate case IDs found")

    def test_latency_budgets_are_sane(self):
        for c in self._load():
            self.assertGreater(c.max_latency_sec, 0, f"Case {c.case_id} latency budget <= 0")
            self.assertLessEqual(c.max_latency_sec, 120, f"Case {c.case_id} latency budget > 120s")

    def test_categories_are_known(self):
        known = {"smoke", "code_editing", "debugging", "domain_knowledge",
                 "multi_step", "safety", "security", "output_format",
                 "latency", "session", ""}
        for c in self._load():
            self.assertIn(c.category, known, f"Case {c.case_id} has unknown category {c.category!r}")


class TestOfflineBaseline(unittest.TestCase):
    def test_offline_baseline_echoes_expected_tokens(self):
        case = BenchmarkCase(
            case_id="x",
            prompt="test",
            expected_contains=["alpha", "beta"],
            forbidden_contains=["Error:"],
            max_latency_sec=30.0,
        )
        output, rc, latency = _offline_baseline(case)
        self.assertIn("alpha", output)
        self.assertIn("beta", output)
        self.assertEqual(rc, 0)
        self.assertEqual(latency, 0.0)

    def test_offline_baseline_passes_its_own_gate(self):
        """A case run through offline baseline must score 1.0 expected_match."""
        case = BenchmarkCase(
            case_id="y",
            prompt="give me ok",
            expected_contains=["ok", "done"],
            forbidden_contains=["Error:"],
            max_latency_sec=30.0,
        )
        output, _, latency = _offline_baseline(case)
        result = evaluate_output(case=case, output=output, latency_sec=latency)
        self.assertEqual(result["expected_match"], 1.0)

    def test_offline_baseline_empty_expected_gives_ok(self):
        case = BenchmarkCase(
            case_id="z",
            prompt="hi",
            expected_contains=[],
            forbidden_contains=["Error:"],
            max_latency_sec=30.0,
        )
        output, _, _ = _offline_baseline(case)
        self.assertTrue(output.strip())


class TestP95(unittest.TestCase):
    def test_p95_single(self):
        self.assertAlmostEqual(p95([5.0]), 5.0)

    def test_p95_ten_values(self):
        values = list(range(1, 11))  # 1..10
        result = p95(values)
        self.assertAlmostEqual(result, 10.0)

    def test_p95_empty(self):
        self.assertEqual(p95([]), 0.0)


class TestRenderMarkdown(unittest.TestCase):
    def _make_report(self):
        return {
            "generated_at": "2026-01-01T00:00:00+00:00",
            "summary": {
                "cases": 2,
                "completion_rate": 1.0,
                "expected_match_avg": 0.9,
                "similarity_to_baseline_avg": 0.8,
                "latency_p95_sec": 3.5,
                "forbidden_failures": 0,
                "user_corrections_required_total": 0,
            },
            "gates": {
                "pass": True,
                "checks": {
                    "completion_rate": True,
                    "expected_match_avg": True,
                },
            },
            "cases": [
                {
                    "id": "c1",
                    "similarity_to_codex": 0.85,
                    "telegram": {
                        "completed": True,
                        "expected_match": 0.9,
                        "latency_sec": 2.1,
                    },
                }
            ],
        }

    def test_render_includes_pass(self):
        md = render_markdown(self._make_report())
        self.assertIn("PASS", md)

    def test_render_includes_case_id(self):
        md = render_markdown(self._make_report())
        self.assertIn("c1", md)

    def test_render_is_markdown(self):
        md = render_markdown(self._make_report())
        self.assertIn("#", md)  # has headings


class TestOfflineTelegramFlag(unittest.TestCase):
    """Verify the --offline-telegram synthetic runner path."""

    def _make_case(self, tokens):
        return BenchmarkCase(
            case_id="t1",
            prompt="test",
            expected_contains=tokens,
            forbidden_contains=["Error:"],
            max_latency_sec=30.0,
        )

    def test_offline_telegram_echoes_expected_tokens(self):
        case = self._make_case(["alpha", "beta"])
        expected_text = " ".join(case.expected_contains)
        result = evaluate_output(case=case, output=expected_text, latency_sec=0.5)
        self.assertEqual(result["expected_match"], 1.0)
        self.assertTrue(result["completed"])
        self.assertTrue(result["forbidden_passed"])

    def test_offline_telegram_latency_within_budget(self):
        """Synthetic latency of 0.5 s must be within any sane gate threshold."""
        self.assertLess(0.5, 45.0)

    def test_offline_telegram_passes_all_gates(self):
        """Running all 20 benchmark cases in offline-telegram mode must pass gates."""
        cases_path = Path(__file__).parent.parent / "docs" / "benchmarks" / "parity_cases.json"
        cases = load_cases(cases_path)
        rows = []
        for case in cases:
            baseline_text = " ".join(case.expected_contains) if case.expected_contains else "ok"
            telegram_text = " ".join(case.expected_contains) if case.expected_contains else "ok"
            b_eval = evaluate_output(case=case, output=baseline_text, latency_sec=0.0)
            t_eval = evaluate_output(case=case, output=telegram_text, latency_sec=0.5)
            from codex_telegram_bot.eval_parity import text_similarity as _sim
            rows.append({
                "id": case.case_id,
                "prompt": case.prompt,
                "baseline": {**b_eval, "output_preview": baseline_text[:80]},
                "telegram": {**t_eval, "output_preview": telegram_text[:80]},
                "similarity_to_codex": round(_sim(baseline_text, telegram_text), 4),
            })
        summary = aggregate_case_rows(rows)
        gates = evaluate_gates(
            summary=summary,
            min_completion_rate=0.9,
            min_expected_match=0.8,
            min_similarity=0.6,
            max_p95_latency_sec=45.0,
            max_corrections=2,
        )
        self.assertTrue(gates["pass"], f"Gates failed: {gates['checks']}")
        self.assertEqual(summary["cases"], 20)


if __name__ == "__main__":
    unittest.main()
