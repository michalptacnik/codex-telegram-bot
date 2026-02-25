import argparse
import asyncio
import json
import math
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Dict, List, Tuple

from codex_telegram_bot.app_container import build_agent_service


@dataclass
class BenchmarkCase:
    case_id: str
    prompt: str
    expected_contains: List[str]
    forbidden_contains: List[str]
    max_latency_sec: float
    category: str = ""


def _normalize(text: str) -> str:
    return " ".join((text or "").strip().lower().split())


def contains_ratio(output: str, expected_contains: List[str]) -> float:
    if not expected_contains:
        return 1.0
    norm = _normalize(output)
    hits = 0
    for needle in expected_contains:
        if _normalize(needle) and _normalize(needle) in norm:
            hits += 1
    return hits / max(1, len(expected_contains))


def forbidden_ok(output: str, forbidden_contains: List[str]) -> bool:
    if not forbidden_contains:
        return True
    norm = _normalize(output)
    for needle in forbidden_contains:
        token = _normalize(needle)
        if token and token in norm:
            return False
    return True


def text_similarity(a: str, b: str) -> float:
    na = _normalize(a)
    nb = _normalize(b)
    if not na and not nb:
        return 1.0
    return SequenceMatcher(None, na, nb).ratio()


def estimate_user_corrections_required(completed: bool, expected_match: float, forbidden_passed: bool) -> int:
    if not completed:
        return 1
    if expected_match >= 1.0 and forbidden_passed:
        return 0
    return 1


def p95(values: List[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = max(0, math.ceil(0.95 * len(ordered)) - 1)
    return ordered[idx]


def evaluate_output(case: BenchmarkCase, output: str, latency_sec: float) -> Dict[str, Any]:
    expected_match = contains_ratio(output, case.expected_contains)
    forbidden_passed = forbidden_ok(output, case.forbidden_contains)
    completed = bool(output.strip()) and not output.strip().startswith("Error:")
    corrections = estimate_user_corrections_required(
        completed=completed,
        expected_match=expected_match,
        forbidden_passed=forbidden_passed,
    )
    return {
        "completed": completed,
        "expected_match": round(expected_match, 4),
        "forbidden_passed": forbidden_passed,
        "latency_sec": round(latency_sec, 4),
        "within_latency_budget": latency_sec <= case.max_latency_sec,
        "user_corrections_required": corrections,
    }


def load_cases(path: Path) -> List[BenchmarkCase]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError("Cases file must be a JSON array.")
    out: List[BenchmarkCase] = []
    for i, row in enumerate(raw, start=1):
        if not isinstance(row, dict):
            raise ValueError(f"Case #{i} is not an object.")
        case_id = str(row.get("id") or f"case-{i}")
        prompt = str(row.get("prompt") or "").strip()
        if not prompt:
            raise ValueError(f"Case {case_id} has empty prompt.")
        expected_contains = [str(x) for x in (row.get("expected_contains") or [])]
        forbidden_contains = [str(x) for x in (row.get("forbidden_contains") or [])]
        max_latency_sec = float(row.get("max_latency_sec") or 45.0)
        category = str(row.get("category") or "")
        out.append(
            BenchmarkCase(
                case_id=case_id,
                prompt=prompt,
                expected_contains=expected_contains,
                forbidden_contains=forbidden_contains,
                max_latency_sec=max_latency_sec,
                category=category,
            )
        )
    return out


async def run_codex_direct(prompt: str, timeout_sec: float = 90.0) -> Tuple[str, int, float]:
    started = asyncio.get_running_loop().time()
    proc = await asyncio.create_subprocess_exec(
        "codex",
        "exec",
        "-",
        "--color",
        "never",
        "--skip-git-repo-check",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(input=prompt.encode("utf-8")),
            timeout=timeout_sec,
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        elapsed = asyncio.get_running_loop().time() - started
        return "Error: codex direct timeout.", 124, elapsed
    elapsed = asyncio.get_running_loop().time() - started
    out = (stdout or b"").decode("utf-8", errors="replace").strip()
    err = (stderr or b"").decode("utf-8", errors="replace").strip()
    if proc.returncode != 0:
        msg = f"Error: codex direct exited with code {proc.returncode}."
        if err:
            msg += f" {err[:400]}"
        elif out:
            msg += f" {out[:400]}"
        return msg, int(proc.returncode), elapsed
    return out or "(no output)", int(proc.returncode), elapsed


class TelegramAgentRunner:
    def __init__(self, workspace_root: Path, policy_profile: str):
        self._workspace_root = workspace_root
        self._policy_profile = policy_profile
        self._tmp: tempfile.TemporaryDirectory[str] | None = None
        self._service = None
        self._chat_id = 94001
        self._user_id = 94001

    async def __aenter__(self):
        self._tmp = tempfile.TemporaryDirectory()
        db_path = Path(self._tmp.name) / "parity_eval_state.db"
        old_workspace = os.environ.get("EXECUTION_WORKSPACE_ROOT")
        os.environ["EXECUTION_WORKSPACE_ROOT"] = str(self._workspace_root)
        try:
            self._service = build_agent_service(state_db_path=db_path)
        finally:
            if old_workspace is None:
                os.environ.pop("EXECUTION_WORKSPACE_ROOT", None)
            else:
                os.environ["EXECUTION_WORKSPACE_ROOT"] = old_workspace
        self._service.upsert_agent(
            agent_id="default",
            name="Default Agent",
            provider="codex_cli",
            policy_profile=self._policy_profile,
            max_concurrency=1,
            enabled=True,
        )
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if self._service:
            await self._service.shutdown()
        if self._tmp:
            self._tmp.cleanup()
        self._service = None
        self._tmp = None

    async def run_prompt(self, prompt: str) -> Tuple[str, float]:
        started = asyncio.get_running_loop().time()
        session = self._service.reset_session(chat_id=self._chat_id, user_id=self._user_id)
        self._service.append_session_user_message(session_id=session.session_id, content=prompt)
        output = await self._service.run_prompt_with_tool_loop(
            prompt=prompt,
            chat_id=self._chat_id,
            user_id=self._user_id,
            session_id=session.session_id,
            agent_id="default",
        )
        self._service.append_session_assistant_message(session_id=session.session_id, content=output)
        elapsed = asyncio.get_running_loop().time() - started
        return output, elapsed


def aggregate_case_rows(case_rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not case_rows:
        return {
            "cases": 0,
            "completion_rate": 0.0,
            "expected_match_avg": 0.0,
            "similarity_to_baseline_avg": 0.0,
            "latency_p95_sec": 0.0,
            "forbidden_failures": 0,
            "user_corrections_required_total": 0,
        }
    completion_rate = sum(1 for c in case_rows if c["telegram"]["completed"]) / len(case_rows)
    expected_avg = sum(c["telegram"]["expected_match"] for c in case_rows) / len(case_rows)
    sim_avg = sum(c["similarity_to_codex"] for c in case_rows) / len(case_rows)
    lat_p95 = p95([c["telegram"]["latency_sec"] for c in case_rows])
    forbidden_failures = sum(1 for c in case_rows if not c["telegram"]["forbidden_passed"])
    corrections = sum(c["telegram"]["user_corrections_required"] for c in case_rows)
    return {
        "cases": len(case_rows),
        "completion_rate": round(completion_rate, 4),
        "expected_match_avg": round(expected_avg, 4),
        "similarity_to_baseline_avg": round(sim_avg, 4),
        "latency_p95_sec": round(lat_p95, 4),
        "forbidden_failures": forbidden_failures,
        "user_corrections_required_total": corrections,
    }


def evaluate_gates(
    summary: Dict[str, Any],
    min_completion_rate: float,
    min_expected_match: float,
    min_similarity: float,
    max_p95_latency_sec: float,
    max_corrections: int,
) -> Dict[str, Any]:
    checks = {
        "completion_rate": summary["completion_rate"] >= min_completion_rate,
        "expected_match_avg": summary["expected_match_avg"] >= min_expected_match,
        "similarity_to_baseline_avg": summary["similarity_to_baseline_avg"] >= min_similarity,
        "latency_p95_sec": summary["latency_p95_sec"] <= max_p95_latency_sec,
        "user_corrections_required_total": summary["user_corrections_required_total"] <= max_corrections,
        "forbidden_failures": summary["forbidden_failures"] == 0,
    }
    return {
        "pass": all(checks.values()),
        "checks": checks,
        "thresholds": {
            "min_completion_rate": min_completion_rate,
            "min_expected_match": min_expected_match,
            "min_similarity": min_similarity,
            "max_p95_latency_sec": max_p95_latency_sec,
            "max_corrections": max_corrections,
        },
    }


def render_markdown(report: Dict[str, Any]) -> str:
    lines = []
    lines.append(f"# Parity Report ({report['generated_at']})")
    lines.append("")
    lines.append(f"- Cases: {report['summary']['cases']}")
    lines.append(f"- Overall pass: **{report['gates']['pass']}**")
    lines.append("")
    lines.append("## Summary (Telegram Runner)")
    for key, value in report["summary"].items():
        lines.append(f"- {key}: {value}")
    lines.append("")
    lines.append("## Gate Checks")
    for key, ok in report["gates"]["checks"].items():
        lines.append(f"- {key}: {'PASS' if ok else 'FAIL'}")
    lines.append("")
    lines.append("## Case Results")
    for case in report["cases"]:
        lines.append(
            f"- {case['id']}: similarity={case['similarity_to_codex']}, "
            f"completion={case['telegram']['completed']}, "
            f"expected_match={case['telegram']['expected_match']}, "
            f"latency={case['telegram']['latency_sec']}s"
        )
    lines.append("")
    return "\n".join(lines)


def _offline_baseline(case: BenchmarkCase) -> Tuple[str, int, float]:
    """Return a synthetic baseline output for offline/CI mode.

    The synthetic output simply echoes back the expected_contains tokens joined
    by spaces, giving a baseline that always passes its own gate checks.  The
    similarity score against this baseline will reflect how well the Telegram
    runner reproduces the expected tokens.
    """
    text = " ".join(case.expected_contains) if case.expected_contains else "ok"
    return text, 0, 0.0


async def run_parity_eval(args: argparse.Namespace) -> Dict[str, Any]:
    cases = load_cases(Path(args.cases))
    category_filter = getattr(args, "category", "")
    if category_filter:
        cases = [c for c in cases if c.category == category_filter]
    if args.max_cases and args.max_cases > 0:
        cases = cases[: args.max_cases]

    offline = getattr(args, "offline_baseline", False)
    offline_telegram = getattr(args, "offline_telegram", False)

    rows: List[Dict[str, Any]] = []

    async def _collect_rows(telegram_runner) -> None:
        for case in cases:
            if offline:
                baseline_output, _, baseline_latency = _offline_baseline(case)
            else:
                baseline_output, _, baseline_latency = await run_codex_direct(
                    prompt=case.prompt,
                    timeout_sec=args.timeout_sec,
                )
            if offline_telegram:
                telegram_output = " ".join(case.expected_contains) if case.expected_contains else "ok"
                telegram_latency = 0.5
            else:
                telegram_output, telegram_latency = await telegram_runner.run_prompt(case.prompt)
            baseline_eval = evaluate_output(case=case, output=baseline_output, latency_sec=baseline_latency)
            telegram_eval = evaluate_output(case=case, output=telegram_output, latency_sec=telegram_latency)
            sim = text_similarity(baseline_output, telegram_output)
            rows.append(
                {
                    "id": case.case_id,
                    "prompt": case.prompt,
                    "baseline": {
                        **baseline_eval,
                        "output_preview": baseline_output[:280],
                    },
                    "telegram": {
                        **telegram_eval,
                        "output_preview": telegram_output[:280],
                    },
                    "similarity_to_codex": round(sim, 4),
                }
            )

    if offline_telegram:
        await _collect_rows(None)
    else:
        async with TelegramAgentRunner(
            workspace_root=Path(args.workspace_root).expanduser().resolve(),
            policy_profile=args.policy_profile,
        ) as telegram_runner:
            await _collect_rows(telegram_runner)

    summary = aggregate_case_rows(rows)
    gates = evaluate_gates(
        summary=summary,
        min_completion_rate=args.min_completion_rate,
        min_expected_match=args.min_expected_match,
        min_similarity=args.min_similarity,
        max_p95_latency_sec=args.max_p95_latency_sec,
        max_corrections=args.max_corrections,
    )
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "cases_file": str(Path(args.cases).resolve()),
        "workspace_root": str(Path(args.workspace_root).expanduser().resolve()),
        "policy_profile": args.policy_profile,
        "offline_baseline": offline,
        "offline_telegram": offline_telegram,
        "category_filter": category_filter,
        "summary": summary,
        "gates": gates,
        "cases": rows,
    }
    return report


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run parity benchmark (Telegram agent vs direct codex).")
    parser.add_argument("--cases", default="docs/benchmarks/parity_cases.json")
    parser.add_argument("--workspace-root", default=".")
    parser.add_argument("--output-dir", default="docs/reports")
    parser.add_argument("--policy-profile", default="balanced", choices=["strict", "balanced", "trusted"])
    parser.add_argument("--timeout-sec", type=float, default=90.0)
    parser.add_argument("--max-cases", type=int, default=0)
    parser.add_argument("--min-completion-rate", type=float, default=0.9)
    parser.add_argument("--min-expected-match", type=float, default=0.8)
    parser.add_argument("--min-similarity", type=float, default=0.6)
    parser.add_argument("--max-p95-latency-sec", type=float, default=45.0)
    parser.add_argument("--max-corrections", type=int, default=2)
    parser.add_argument(
        "--offline-baseline",
        action="store_true",
        default=False,
        help=(
            "Skip codex CLI baseline calls and use synthetic expected-token "
            "output as the baseline. Useful in CI environments where the codex "
            "CLI is not installed."
        ),
    )
    parser.add_argument(
        "--offline-telegram",
        action="store_true",
        default=False,
        help=(
            "Skip the live TelegramAgentRunner and use synthetic expected-token "
            "output as the telegram runner output. Combine with --offline-baseline "
            "for a fully offline CI run that validates gate logic without any "
            "external dependencies."
        ),
    )
    parser.add_argument(
        "--category",
        default="",
        help="If set, only run cases whose 'category' field matches this value.",
    )
    return parser


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()
    report = asyncio.run(run_parity_eval(args))

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    json_path = out_dir / f"parity-report-{ts}.json"
    md_path = out_dir / f"parity-report-{ts}.md"
    json_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    md_path.write_text(render_markdown(report), encoding="utf-8")
    print(f"wrote: {json_path}")
    print(f"wrote: {md_path}")
    print(f"pass: {report['gates']['pass']}")
    return 0 if report["gates"]["pass"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
