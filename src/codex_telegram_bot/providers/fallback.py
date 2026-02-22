from typing import Any, Dict

from codex_telegram_bot.domain.contracts import ProviderAdapter


class EchoFallbackProvider(ProviderAdapter):
    """Minimal fallback provider for degraded mode."""

    async def execute(
        self,
        prompt: str,
        correlation_id: str = "",
        policy_profile: str = "balanced",
    ) -> str:
        trimmed = (prompt or "").strip()
        if len(trimmed) > 240:
            trimmed = trimmed[:240] + "..."
        return (
            "Fallback mode active: primary provider is unhealthy.\n"
            f"Prompt received (truncated): {trimmed}"
        )

    async def version(self) -> str:
        return "fallback-echo-1"

    async def health(self) -> Dict[str, Any]:
        return {
            "provider": "fallback_echo",
            "status": "healthy",
            "degraded_mode": True,
        }
