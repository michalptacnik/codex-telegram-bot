from dataclasses import dataclass
from typing import Any, Dict, Protocol, Sequence


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


class ExecutionRunner(Protocol):
    async def run(
        self,
        argv: Sequence[str],
        stdin_text: str = "",
        timeout_sec: int = 60,
        policy_profile: str = "balanced",
    ) -> CommandResult:
        ...


class ProviderAdapter(Protocol):
    async def execute(
        self,
        prompt: str,
        correlation_id: str = "",
        policy_profile: str = "balanced",
    ) -> str:
        ...

    async def version(self) -> str:
        ...

    async def health(self) -> Dict[str, Any]:
        ...
