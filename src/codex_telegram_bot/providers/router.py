import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

from codex_telegram_bot.domain.contracts import ProviderAdapter


@dataclass
class ProviderRouterConfig:
    retry_attempts: int = 1
    failure_threshold: int = 2
    recovery_sec: int = 30


class ProviderRouter(ProviderAdapter):
    def __init__(
        self,
        primary: ProviderAdapter,
        fallback: Optional[ProviderAdapter] = None,
        config: Optional[ProviderRouterConfig] = None,
    ):
        self._primary = primary
        self._fallback = fallback
        self._cfg = config or ProviderRouterConfig()
        self._consecutive_failures = 0
        self._circuit_open_until = 0.0
        self._active_provider = "primary"

    async def execute(self, prompt: str, correlation_id: str = "") -> str:
        if self._circuit_is_open():
            if self._fallback:
                self._active_provider = "fallback"
                return await self._fallback.execute(prompt, correlation_id=correlation_id)
            return "Error: primary provider is temporarily unhealthy."

        attempts = max(1, self._cfg.retry_attempts)
        last_output = ""
        for _ in range(attempts):
            self._active_provider = "primary"
            output = await self._primary.execute(prompt, correlation_id=correlation_id)
            last_output = output
            if not _is_error_output(output):
                self._on_success()
                return output
            self._on_failure()
            if self._circuit_is_open():
                break

        if self._fallback:
            self._active_provider = "fallback"
            return await self._fallback.execute(prompt, correlation_id=correlation_id)
        return last_output or "Error: provider execution failed."

    async def version(self) -> str:
        if self._active_provider == "fallback" and self._fallback is not None:
            return await self._fallback.version()
        return await self._primary.version()

    async def health(self) -> Dict[str, Any]:
        primary_health = await self._primary.health()
        fallback_health = await self._fallback.health() if self._fallback else None
        now = time.time()
        return {
            "active_provider": self._active_provider,
            "consecutive_failures": self._consecutive_failures,
            "circuit_open": self._circuit_open_until > now,
            "circuit_open_until_unix": self._circuit_open_until,
            "config": {
                "retry_attempts": self._cfg.retry_attempts,
                "failure_threshold": self._cfg.failure_threshold,
                "recovery_sec": self._cfg.recovery_sec,
            },
            "primary": primary_health,
            "fallback": fallback_health,
        }

    def _on_success(self) -> None:
        self._consecutive_failures = 0
        self._circuit_open_until = 0.0

    def _on_failure(self) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures >= max(1, self._cfg.failure_threshold):
            self._circuit_open_until = time.time() + max(1, self._cfg.recovery_sec)

    def _circuit_is_open(self) -> bool:
        if self._circuit_open_until <= 0:
            return False
        if time.time() >= self._circuit_open_until:
            self._circuit_open_until = 0.0
            return False
        return True


def _is_error_output(output: str) -> bool:
    return (output or "").startswith("Error:")

