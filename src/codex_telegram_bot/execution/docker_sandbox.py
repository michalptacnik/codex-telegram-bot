import asyncio
import os
import shlex
import shutil
from pathlib import Path
from typing import Sequence

from codex_telegram_bot.domain.contracts import CommandResult, ExecutionRunner
from codex_telegram_bot.execution.policy import ExecutionPolicyEngine
from codex_telegram_bot.execution.profiles import ExecutionProfileResolver


class DockerSandboxRunner(ExecutionRunner):
    """Execute commands inside a Docker sandbox with workspace bind-mounting."""

    def __init__(
        self,
        policy_engine: ExecutionPolicyEngine | None = None,
        profile_resolver: ExecutionProfileResolver | None = None,
        image: str | None = None,
        network_mode: str | None = None,
        container_workdir: str | None = None,
    ):
        self._policy_engine = policy_engine or ExecutionPolicyEngine()
        self._profile_resolver = profile_resolver or ExecutionProfileResolver(Path.cwd())
        self._image = (image or os.environ.get("DOCKER_SANDBOX_IMAGE") or "python:3.12-slim").strip()
        self._network_mode = (network_mode or os.environ.get("DOCKER_SANDBOX_NETWORK") or "none").strip()
        self._container_workdir = (container_workdir or os.environ.get("DOCKER_SANDBOX_WORKDIR") or "/workspace").strip()
        self._dry_run = (os.environ.get("DOCKER_SANDBOX_DRY_RUN") or "").strip().lower() in {"1", "true", "yes", "on"}

    async def run(
        self,
        argv: Sequence[str],
        stdin_text: str = "",
        timeout_sec: int = 60,
        policy_profile: str = "balanced",
        workspace_root: str = "",
    ) -> CommandResult:
        profile = self._profile_resolver.resolve(policy_profile=policy_profile)
        effective_workspace = profile.workspace_root
        if workspace_root:
            candidate = Path(workspace_root).expanduser().resolve()
            if profile.enforce_workspace_root and not candidate.is_relative_to(profile.workspace_root):
                return CommandResult(
                    returncode=126,
                    stdout="",
                    stderr=(
                        "Blocked by execution policy: "
                        f"workspace root '{candidate}' is outside '{profile.workspace_root}'."
                    ),
                )
            effective_workspace = candidate

        decision = self._policy_engine.evaluate(argv=argv, policy_profile=policy_profile)
        if not decision.allowed:
            return CommandResult(
                returncode=126,
                stdout="",
                stderr=f"Blocked by execution policy: {decision.reason}",
            )

        constrained_timeout = max(1, min(timeout_sec, profile.max_timeout_sec))
        blocked_path = _find_blocked_path_argument(argv=argv, workspace_root=effective_workspace)
        if profile.enforce_workspace_root and blocked_path:
            return CommandResult(
                returncode=126,
                stdout="",
                stderr=(
                    "Blocked by execution policy: "
                    f"path '{blocked_path}' is outside workspace root '{effective_workspace}'."
                ),
            )

        docker_bin = shutil.which("docker")
        if not docker_bin:
            return CommandResult(
                returncode=126,
                stdout="",
                stderr="Blocked by execution policy: docker binary is not available for sandbox backend.",
            )

        effective_workspace.mkdir(parents=True, exist_ok=True)
        shell_command = shlex.join([str(x) for x in argv])
        docker_argv = [
            docker_bin,
            "run",
            "--rm",
            "--network",
            self._network_mode,
            "--workdir",
            self._container_workdir,
            "--volume",
            f"{str(effective_workspace)}:{self._container_workdir}:rw",
            self._image,
            "sh",
            "-lc",
            shell_command,
        ]
        if self._dry_run:
            return CommandResult(returncode=0, stdout=" ".join(docker_argv), stderr="")

        proc = await asyncio.create_subprocess_exec(
            *docker_argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.PIPE,
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
