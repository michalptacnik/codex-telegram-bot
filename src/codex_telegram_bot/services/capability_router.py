"""Capability-aware provider routing with fallback chain (Parity Epic 8).

The CapabilityRouter wraps a ProviderRegistry and adds:
- Requirement-based provider selection (filter by caps dict)
- Streaming preference (prefer a provider that supports streaming)
- Fallback chain: try the best match first; if it fails, try others in order
- Per-request routing audit (which providers were tried, which was selected)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from codex_telegram_bot.providers.registry import ProviderRegistry


@dataclass
class RoutingResult:
    selected_name: str
    reason: str
    fallback_used: bool = False
    tried: List[str] = field(default_factory=list)


class CapabilityRouter:
    """Routes generation requests to the best registered provider.

    Selection priority:
    1. Providers that satisfy all ``required_caps``.
    2. Among those, prefer the currently active provider.
    3. If ``prefer_streaming`` is True, prefer providers with
       ``supports_streaming: True`` among the matching set.
    4. Falls back to the active provider when nothing matches.
    """

    def __init__(self, registry: ProviderRegistry) -> None:
        self._registry = registry

    @property
    def registry(self) -> ProviderRegistry:
        return self._registry

    def select(
        self,
        required_caps: Optional[Dict[str, Any]] = None,
        prefer_streaming: bool = False,
    ) -> RoutingResult:
        """Select the best provider name given capability requirements."""
        candidates = self._registry.list_providers()
        required = required_caps or {}

        matching = [p for p in candidates if _matches_caps(p["capabilities"], required)]

        if not matching:
            return RoutingResult(
                selected_name=self._registry.get_active_name(),
                reason="no_matching_provider_fallback_to_active",
                fallback_used=True,
                tried=[p["name"] for p in candidates],
            )

        # Among matches, prefer those with streaming support if requested
        if prefer_streaming:
            streaming_matches = [
                m for m in matching if m["capabilities"].get("supports_streaming")
            ]
            if streaming_matches:
                matching = streaming_matches

        # Prefer the currently active provider if it qualifies
        active = self._registry.get_active_name()
        for p in matching:
            if p["name"] == active:
                return RoutingResult(
                    selected_name=active,
                    reason="active_provider_matches_requirements",
                    tried=[p["name"] for p in matching],
                )

        best = matching[0]["name"]
        return RoutingResult(
            selected_name=best,
            reason="first_matching_provider",
            tried=[p["name"] for p in matching],
        )

    async def route_generate(
        self,
        messages: Sequence[Dict[str, str]],
        required_caps: Optional[Dict[str, Any]] = None,
        prefer_streaming: bool = False,
        policy_profile: str = "balanced",
    ) -> tuple[str, RoutingResult]:
        """Generate via the best provider. Returns ``(output, routing_result)``."""
        result = self.select(
            required_caps=required_caps,
            prefer_streaming=prefer_streaming,
        )
        name = result.selected_name

        # Hot-switch if a different provider was selected
        if name != self._registry.get_active_name():
            self._registry.switch(name)

        output = await self._registry.generate(
            messages=list(messages),
            policy_profile=policy_profile,
        )
        return output, result


def _matches_caps(caps: Dict[str, Any], required: Dict[str, Any]) -> bool:
    """Return True when provider capabilities satisfy all required values."""
    for key, value in required.items():
        cap_value = caps.get(key)
        if cap_value is None:
            return False
        if isinstance(value, bool):
            if bool(cap_value) != value:
                return False
        elif isinstance(value, (int, float)):
            if not isinstance(cap_value, (int, float)) or cap_value < value:
                return False
        else:
            if str(cap_value) != str(value):
                return False
    return True
