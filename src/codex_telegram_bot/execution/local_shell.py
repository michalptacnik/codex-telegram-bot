import asyncio
from pathlib import Path
from typing import Sequence

from codex_telegram_bot.domain.contracts import CommandResult, ExecutionRunner
from codex_telegram_bot.execution.policy import ExecutionPolicyEngine
from codex_telegram_bot.execution.profiles import ExecutionProfileResolver


class LocalShellRunner(ExecutionRunner):
    def __init__(
        self,
        policy_engine: ExecutionPolicyEngine | None = None,
        profile_resolver: ExecutionProfileResolver | None = None,
    ):
        self._policy_engine = policy_engine or ExecutionPolicyEngine()
        self._profile_resolver = profile_resolver or ExecutionProfileResolver(Path.cwd())

    async def run(
        self,
        argv: Sequence[str],
        stdin_text: str = "",
        timeout_sec: int = 60,
        policy_profile: str = "balanced",
    ) -> CommandResult:
        profile = self._profile_resolver.resolve(policy_profile=policy_profile)
        decision = self._policy_engine.evaluate(argv=argv, policy_profile=policy_profile)
        if not decision.allowed:
            return CommandResult(
                returncode=126,
                stdout="",
                stderr=f"Blocked by execution policy: {decision.reason}",
            )
        constrained_timeout = max(1, min(timeout_sec, profile.max_timeout_sec))
        blocked_path = _find_blocked_path_argument(argv=argv, workspace_root=profile.workspace_root)
        if profile.enforce_workspace_root and blocked_path:
            return CommandResult(
                returncode=126,
                stdout="",
                stderr=(
                    "Blocked by execution policy: "
                    f"path '{blocked_path}' is outside workspace root '{profile.workspace_root}'."
                ),
            )

        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.PIPE,
            cwd=str(profile.workspace_root) if profile.enforce_workspace_root else None,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(stdin_text.encode()),
                timeout=constrained_timeout,
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            return CommandResult(returncode=124, stdout="", stderr="Execution timeout.")

        return CommandResult(
            returncode=proc.returncode or 0,
            stdout=stdout.decode(errors="replace") if stdout else "",
            stderr=stderr.decode(errors="replace") if stderr else "",
        )


def _find_blocked_path_argument(argv: Sequence[str], workspace_root: Path) -> str:
    for idx, token in enumerate(argv):
        if token in {"--cd", "--workdir"} and idx + 1 < len(argv):
            candidate = Path(argv[idx + 1]).expanduser()
            resolved = candidate.resolve() if candidate.is_absolute() else (workspace_root / candidate).resolve()
            if not resolved.is_relative_to(workspace_root):
                return str(candidate)
    return ""
