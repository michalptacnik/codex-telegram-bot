"""Provider management tools (EPIC 3, issue #66).

Two tools that can be invoked via the tool loop:

  ``provider_status`` – list registered providers and show which is active.
  ``provider_switch``  – switch the active provider by name.

Both tools require a ``ProviderRegistry`` to be injected via their constructor.
They are registered in the tool registry by ``build_default_tool_registry`` when
a registry instance is available.

Usage in tool loop::

    !tool provider_status
    !tool provider_switch name=anthropic
"""
from __future__ import annotations

import json
from typing import Any, Optional

from codex_telegram_bot.tools.base import ToolContext, ToolRequest, ToolResult


class ProviderStatusTool:
    """List all registered providers and report which is active."""

    name = "provider_status"

    def __init__(self, registry: "ProviderRegistry") -> None:  # type: ignore[name-defined]
        self._registry = registry

    def run(self, request: ToolRequest, context: ToolContext) -> ToolResult:
        try:
            providers = self._registry.list_providers()
            active = self._registry.get_active_name()
            lines = [f"Active provider: {active}", "Registered providers:"]
            for p in providers:
                marker = "→" if p["active"] else " "
                caps = p.get("capabilities") or {}
                streaming = "streaming" if caps.get("supports_streaming") else "buffered"
                lines.append(f"  {marker} {p['name']}  [{streaming}]")
            history = self._registry.switch_history[-3:]
            if history:
                lines.append("Recent switches:")
                for h in reversed(history):
                    lines.append(f"  {h['from']} → {h['to']}  at {h['at']}")
            return ToolResult(ok=True, output="\n".join(lines))
        except Exception as exc:
            return ToolResult(ok=False, output=f"Error: {exc}")


class ProviderSwitchTool:
    """Switch the active provider by name.

    Args:
        name (str): provider name to activate
    """

    name = "provider_switch"

    def __init__(self, registry: "ProviderRegistry") -> None:  # type: ignore[name-defined]
        self._registry = registry

    def run(self, request: ToolRequest, context: ToolContext) -> ToolResult:
        provider_name = str(request.args.get("name") or "").strip()
        if not provider_name:
            available = [p["name"] for p in self._registry.list_providers()]
            return ToolResult(
                ok=False,
                output=f"Error: 'name' argument required. Available: {available}",
            )
        try:
            msg = self._registry.switch(provider_name)
            return ToolResult(ok=True, output=msg)
        except KeyError as exc:
            return ToolResult(ok=False, output=f"Error: {exc}")
        except Exception as exc:
            return ToolResult(ok=False, output=f"Error: {exc}")


from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from codex_telegram_bot.providers.registry import ProviderRegistry
