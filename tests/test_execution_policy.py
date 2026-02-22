import unittest
from pathlib import Path

from codex_telegram_bot.execution.local_shell import LocalShellRunner
from codex_telegram_bot.execution.policy import ExecutionPolicyEngine
from codex_telegram_bot.execution.profiles import ExecutionProfileResolver


class TestExecutionPolicyEngine(unittest.TestCase):
    def test_balanced_blocks_high_risk_flag(self):
        engine = ExecutionPolicyEngine()
        decision = engine.evaluate(
            ["codex", "exec", "-", "--dangerously-bypass-approvals-and-sandbox"],
            policy_profile="balanced",
        )
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.risk_tier, "high")

    def test_strict_blocks_medium_risk(self):
        engine = ExecutionPolicyEngine()
        decision = engine.evaluate(
            ["codex", "exec", "-", "--sandbox=workspace-write"],
            policy_profile="strict",
        )
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.risk_tier, "medium")

    def test_trusted_allows_non_codex_commands(self):
        engine = ExecutionPolicyEngine()
        decision = engine.evaluate(["/bin/echo", "ok"], policy_profile="trusted")
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.risk_tier, "low")


class TestLocalShellRunner(unittest.IsolatedAsyncioTestCase):
    async def test_runner_returns_blocked_result_when_policy_denies(self):
        runner = LocalShellRunner()
        result = await runner.run(
            ["codex", "exec", "-", "--dangerously-bypass-approvals-and-sandbox"],
            policy_profile="balanced",
        )
        self.assertEqual(result.returncode, 126)
        self.assertIn("Blocked by execution policy", result.stderr)

    async def test_runner_blocks_workspace_escape_for_balanced(self):
        runner = LocalShellRunner(profile_resolver=ExecutionProfileResolver(Path("/tmp/workspace")))
        result = await runner.run(
            ["codex", "exec", "-", "--cd", "/etc"],
            policy_profile="balanced",
        )
        self.assertEqual(result.returncode, 126)
        self.assertIn("outside workspace root", result.stderr)

    async def test_runner_blocks_workspace_override_outside_root(self):
        runner = LocalShellRunner(profile_resolver=ExecutionProfileResolver(Path("/tmp/workspace")))
        result = await runner.run(
            ["/bin/echo", "ok"],
            policy_profile="balanced",
            workspace_root="/etc",
        )
        self.assertEqual(result.returncode, 126)
        self.assertIn("workspace root", result.stderr)


class TestExecutionProfiles(unittest.TestCase):
    def test_profile_limits_differ_by_policy(self):
        resolver = ExecutionProfileResolver(Path("/tmp/workspace"))
        strict = resolver.resolve("strict")
        balanced = resolver.resolve("balanced")
        trusted = resolver.resolve("trusted")

        self.assertLess(strict.max_timeout_sec, balanced.max_timeout_sec)
        self.assertLess(balanced.max_timeout_sec, trusted.max_timeout_sec)
        self.assertTrue(strict.enforce_workspace_root)
        self.assertTrue(balanced.enforce_workspace_root)
        self.assertFalse(trusted.enforce_workspace_root)
