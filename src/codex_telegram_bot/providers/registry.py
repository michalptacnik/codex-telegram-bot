"""Provider registry for runtime provider management (EPIC 3, issue #66).

The ProviderRegistry holds all known named providers and tracks which one is
currently "active".  It exposes an interface that satisfies ProviderAdapter so
it can drop-in replace the existing ProviderRouter when multiple providers need
to be managed at runtime (via Telegram commands or the Control Center).

Features:
  - Register / unregister named providers
  - Switch active provider at runtime (by name)
  - Delegate generate/execute/health/version to the active provider
  - Emit structured log events on every switch for auditability
  - ``list_providers()`` returns a summary suitable for UI rendering

Usage::

    registry = ProviderRegistry()
    registry.register("codex_cli", codex_provider)
    registry.register("anthropic", anthropic_provider, make_active=True)

    # Drop into the existing router as the "primary":
    router = ProviderRouter(primary=registry, fallback=echo_fallback)
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

from codex_telegram_bot.domain.contracts import ProviderAdapter
from codex_telegram_bot.observability.structured_log import log_json

logger = logging.getLogger(__name__)


class ProviderNotFoundError(KeyError):
    pass


class ProviderRegistry(ProviderAdapter):
    """Runtime provider registry with hot-switch support."""

    def __init__(self, default_provider_name: str = "codex_cli") -> None:
        self._providers: Dict[str, ProviderAdapter] = {}
        self._active_name: str = default_provider_name
        self._switch_history: List[Dict[str, Any]] = []

    # ------------------------------------------------------------------
    # Registry management
    # ------------------------------------------------------------------

    def register(
        self,
        name: str,
        provider: ProviderAdapter,
        make_active: bool = False,
    ) -> None:
        """Register a named provider.  Optionally make it the active one."""
        self._providers[name] = provider
        if make_active or name == self._active_name:
            self._active_name = name
        log_json(logger, "provider_registry.register", name=name, make_active=make_active)

    def unregister(self, name: str) -> None:
        if name not in self._providers:
            return
        del self._providers[name]
        log_json(logger, "provider_registry.unregister", name=name)
        if self._active_name == name:
            # Fall back to first available or empty string
            self._active_name = next(iter(self._providers), "")

    def switch(self, name: str) -> str:
        """Switch the active provider to ``name``.  Returns a status message."""
        if name not in self._providers:
            available = list(self._providers.keys())
            raise ProviderNotFoundError(
                f"Provider '{name}' not registered. Available: {available}"
            )
        previous = self._active_name
        self._active_name = name
        self._switch_history.append({
            "from": previous,
            "to": name,
            "at": datetime.now(timezone.utc).isoformat(),
        })
        # Keep history bounded
        if len(self._switch_history) > 100:
            self._switch_history = self._switch_history[-100:]
        log_json(logger, "provider_registry.switch", from_provider=previous, to=name)
        return f"Switched active provider: {previous} → {name}"

    def list_providers(self) -> List[Dict[str, Any]]:
        """Return a list of provider summaries for the UI/API."""
        result: List[Dict[str, Any]] = []
        for name, provider in self._providers.items():
            caps = _safe_capabilities(provider)
            result.append({
                "name": name,
                "active": name == self._active_name,
                "capabilities": caps,
            })
        return result

    def get_active_name(self) -> str:
        return self._active_name

    def get_provider(self, name: str) -> ProviderAdapter:
        if name not in self._providers:
            raise ProviderNotFoundError(name)
        return self._providers[name]

    @property
    def switch_history(self) -> List[Dict[str, Any]]:
        return list(self._switch_history)

    # ------------------------------------------------------------------
    # ProviderAdapter delegation
    # ------------------------------------------------------------------

    def _active(self) -> ProviderAdapter:
        provider = self._providers.get(self._active_name)
        if provider is None:
            # Try first registered
            provider = next(iter(self._providers.values()), None)
        if provider is None:
            raise RuntimeError("No providers registered in ProviderRegistry.")
        return provider

    async def generate(
        self,
        messages: Sequence[Dict[str, str]],
        stream: bool = False,
        correlation_id: str = "",
        policy_profile: str = "balanced",
    ) -> str:
        return await self._active().generate(
            messages, stream=stream, correlation_id=correlation_id,
            policy_profile=policy_profile,
        )

    async def execute(
        self,
        prompt: str,
        correlation_id: str = "",
        policy_profile: str = "balanced",
    ) -> str:
        return await self._active().execute(
            prompt, correlation_id=correlation_id, policy_profile=policy_profile,
        )

    async def version(self) -> str:
        return await self._active().version()

    async def health(self) -> Dict[str, Any]:
        results: Dict[str, Any] = {}
        for name, provider in self._providers.items():
            try:
                results[name] = await provider.health()
            except Exception as exc:
                results[name] = {"status": "error", "reason": str(exc)}
        return {
            "active_provider": self._active_name,
            "providers": results,
            "switch_history": self._switch_history[-5:],
        }

    def capabilities(self) -> Dict[str, Any]:
        try:
            active_caps = _safe_capabilities(self._active())
        except Exception:
            active_caps = {}
        return {
            "provider": f"registry/{self._active_name}",
            "active": self._active_name,
            "registered": list(self._providers.keys()),
            **active_caps,
        }


def _safe_capabilities(provider: ProviderAdapter) -> Dict[str, Any]:
    getter = getattr(provider, "capabilities", None)
    if callable(getter):
        try:
            caps = getter()
            return caps if isinstance(caps, dict) else {}
        except Exception:
            pass
    return {}
