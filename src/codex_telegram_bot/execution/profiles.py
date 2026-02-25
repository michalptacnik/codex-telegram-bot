from dataclasses import dataclass
from pathlib import Path

from codex_telegram_bot.execution.policy import VALID_POLICY_PROFILES


@dataclass(frozen=True)
class ExecutionProfile:
    name: str
    max_timeout_sec: int
    enforce_workspace_root: bool
    workspace_root: Path


class ExecutionProfileResolver:
    def __init__(self, workspace_root: Path):
        self._workspace_root = workspace_root.expanduser().resolve()

    def resolve(self, policy_profile: str) -> ExecutionProfile:
        profile = self._normalize(policy_profile)
        if profile == "strict":
            return ExecutionProfile(
                name=profile,
                max_timeout_sec=45,
                enforce_workspace_root=True,
                workspace_root=self._workspace_root,
            )
        if profile == "trusted":
            return ExecutionProfile(
                name=profile,
                max_timeout_sec=1800,
                enforce_workspace_root=False,
                workspace_root=self._workspace_root,
            )
        return ExecutionProfile(
            name="balanced",
            max_timeout_sec=120,
            enforce_workspace_root=True,
            workspace_root=self._workspace_root,
        )

    def _normalize(self, policy_profile: str) -> str:
        value = (policy_profile or "").strip().lower()
        if value not in VALID_POLICY_PROFILES:
            return "balanced"
        return value
