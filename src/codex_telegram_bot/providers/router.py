import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Sequence

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

    async def generate(
        self,
        messages: Sequence[Dict[str, str]],
        stream: bool = False,
        correlation_id: str = "",
        policy_profile: str = "balanced",
    ) -> str:
        prompt = "\n".join(
            str(m.get("content") or "").strip()
            for m in messages or []
            if isinstance(m, dict) and str(m.get("content") or "").strip()
        )
        return await self.execute(
            prompt=prompt,
            correlation_id=correlation_id,
            policy_profile=policy_profile,
        )

    async def execute(
        self,
        prompt: str,
        correlation_id: str = "",
        policy_profile: str = "balanced",
    ) -> str:
        requirements = _required_capabilities(prompt=prompt, policy_profile=policy_profile)
        primary_caps = _provider_capabilities(self._primary)
        fallback_caps = _provider_capabilities(self._fallback)

        if not _supports_requirements(primary_caps, requirements):
            if self._fallback and _supports_requirements(fallback_caps, requirements):
                self._active_provider = "fallback"
                return await self._fallback.execute(
                    prompt,
                    correlation_id=correlation_id,
                    policy_profile=policy_profile,
                )
            return _capability_mismatch_message(requirements=requirements, provider="primary")

        if self._circuit_is_open():
            if self._fallback:
                if not _supports_requirements(fallback_caps, requirements):
                    return _capability_mismatch_message(requirements=requirements, provider="fallback")
                self._active_provider = "fallback"
                return await self._fallback.execute(
                    prompt,
                    correlation_id=correlation_id,
                    policy_profile=policy_profile,
                )
            return "Error: primary provider is temporarily unhealthy."

        attempts = max(1, self._cfg.retry_attempts)
        last_output = ""
        for _ in range(attempts):
            self._active_provider = "primary"
            output = await self._primary.execute(
                prompt,
                correlation_id=correlation_id,
                policy_profile=policy_profile,
            )
            last_output = output
            if not _is_error_output(output):
                self._on_success()
                return output
            self._on_failure()
            if self._circuit_is_open():
                break

        if self._fallback:
            if not _supports_requirements(fallback_caps, requirements):
                return _capability_mismatch_message(requirements=requirements, provider="fallback")
            self._active_provider = "fallback"
            return await self._fallback.execute(
                prompt,
                correlation_id=correlation_id,
                policy_profile=policy_profile,
            )
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
            "capabilities": self.capabilities(),
        }

    def capabilities(self) -> Dict[str, Any]:
        primary_caps = _provider_capabilities(self._primary)
        fallback_caps = _provider_capabilities(self._fallback)
        max_context = max(
            int(primary_caps.get("max_context_chars", 0) or 0),
            int(fallback_caps.get("max_context_chars", 0) or 0),
        )
        return {
            "provider": "router",
            "supports_tool_calls": bool(primary_caps.get("supports_tool_calls")) or bool(
                fallback_caps.get("supports_tool_calls")
            ),
            "supports_streaming": bool(primary_caps.get("supports_streaming")) or bool(
                fallback_caps.get("supports_streaming")
            ),
            "max_context_chars": max_context,
            "supported_policy_profiles": sorted(
                set(_supported_profiles(primary_caps)) | set(_supported_profiles(fallback_caps))
            ),
            "route_policy": {
                "fallback_enabled": self._fallback is not None,
                "retry_attempts": self._cfg.retry_attempts,
                "failure_threshold": self._cfg.failure_threshold,
                "recovery_sec": self._cfg.recovery_sec,
            },
            "providers": {
                "primary": primary_caps,
                "fallback": fallback_caps,
            },
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


def _provider_capabilities(provider: Optional[ProviderAdapter]) -> Dict[str, Any]:
    if provider is None:
        return {}
    getter = getattr(provider, "capabilities", None)
    if callable(getter):
        try:
            caps = getter()
        except Exception:
            return {}
        if isinstance(caps, dict):
            return caps
    return {}


def _required_capabilities(prompt: str, policy_profile: str) -> Dict[str, Any]:
    text = prompt or ""
    tool_requested = any(line.strip().startswith(("!exec ", "!loop ")) for line in text.splitlines())
    return {
        "requires_tool_calls": tool_requested,
        "min_context_chars": len(text),
        "policy_profile": (policy_profile or "balanced").strip().lower(),
    }


def _supports_requirements(capabilities: Dict[str, Any], requirements: Dict[str, Any]) -> bool:
    if not capabilities:
        return True
    if requirements.get("requires_tool_calls") and not capabilities.get("supports_tool_calls", False):
        return False
    max_context = int(capabilities.get("max_context_chars", 0) or 0)
    if max_context and int(requirements.get("min_context_chars", 0) or 0) > max_context:
        return False
    allowed_profiles = _supported_profiles(capabilities)
    policy_profile = str(requirements.get("policy_profile") or "balanced")
    if allowed_profiles and policy_profile not in allowed_profiles:
        return False
    return True


def _supported_profiles(capabilities: Dict[str, Any]) -> list[str]:
    raw = capabilities.get("supported_policy_profiles")
    if not isinstance(raw, list):
        return []
    return [str(v).strip().lower() for v in raw if str(v).strip()]


def _capability_mismatch_message(requirements: Dict[str, Any], provider: str) -> str:
    bits = [
        f"provider={provider}",
        f"policy={requirements.get('policy_profile', 'balanced')}",
        f"min_context={requirements.get('min_context_chars', 0)}",
        f"tool_calls={'yes' if requirements.get('requires_tool_calls') else 'no'}",
    ]
    return "Error: provider capability mismatch. " + ", ".join(bits)
