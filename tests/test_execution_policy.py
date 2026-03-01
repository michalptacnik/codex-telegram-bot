import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from codex_telegram_bot.execution.docker_sandbox import DockerSandboxRunner
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


class _FakeProc:
    def __init__(self, returncode: int = 0, stdout: bytes = b"", stderr: bytes = b""):
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr

    async def communicate(self, _stdin: bytes = b""):
        return self._stdout, self._stderr

    def kill(self):
        return None


class TestDockerSandboxRunner(unittest.IsolatedAsyncioTestCase):
    async def test_runner_reports_missing_docker_binary(self):
        runner = DockerSandboxRunner(profile_resolver=ExecutionProfileResolver(Path("/tmp/workspace")))
        with patch("shutil.which", return_value=None):
            result = await runner.run(
                ["codex", "exec", "-", "--sandbox=workspace-write"],
                workspace_root="/tmp/workspace",
            )
        self.assertEqual(result.returncode, 126)
        self.assertIn("docker binary is not available", result.stderr)

    async def test_runner_dry_run_builds_docker_command(self):
        with patch.dict("os.environ", {"DOCKER_SANDBOX_DRY_RUN": "1"}, clear=False):
            runner = DockerSandboxRunner(profile_resolver=ExecutionProfileResolver(Path("/tmp/workspace")))
            with patch("shutil.which", return_value="/usr/bin/docker"):
                result = await runner.run(
                    ["codex", "exec", "-", "--sandbox=workspace-write"],
                    workspace_root="/tmp/workspace",
                )
        self.assertEqual(result.returncode, 0)
        self.assertIn("/usr/bin/docker run --rm", result.stdout)
        self.assertIn("--network none", result.stdout)
        self.assertIn("sh -lc", result.stdout)
        self.assertIn("codex exec - --sandbox=workspace-write", result.stdout)

    async def test_runner_executes_docker_subprocess(self):
        runner = DockerSandboxRunner(profile_resolver=ExecutionProfileResolver(Path("/tmp/workspace")))
        proc = _FakeProc(returncode=0, stdout=b"ok\n", stderr=b"")
        with patch("shutil.which", return_value="/usr/bin/docker"):
            with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)) as mocked_exec:
                result = await runner.run(
                    ["codex", "exec", "-", "--sandbox=workspace-write"],
                    workspace_root="/tmp/workspace",
                )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "ok\n")
        args = mocked_exec.await_args.args
        self.assertEqual(args[0], "/usr/bin/docker")
        self.assertEqual(args[1], "run")
        self.assertIn("--network", args)
        self.assertIn("sh", args)
