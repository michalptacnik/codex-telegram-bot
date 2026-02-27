from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Dict, Optional

if TYPE_CHECKING:
    from codex_telegram_bot.services.agent_service import AgentService


# Set NATIVE_TOOL_LOOP=1 (or true/yes/on) to prefer the native
# function-calling agentic loop over the legacy text-parsing loop.
_NATIVE_LOOP_ENV = "NATIVE_TOOL_LOOP"


def _native_loop_enabled() -> bool:
    return (os.environ.get(_NATIVE_LOOP_ENV) or "").strip().lower() in {
        "1", "true", "yes", "on",
    }


class AgentRouter:
    """Routes requests from Agent entrypoint into the service layer."""

    def __init__(self, agent_service: "AgentService"):
        self._agent_service = agent_service

    async def route_prompt(
        self,
        prompt: str,
        chat_id: int,
        user_id: int,
        session_id: str,
        agent_id: str = "default",
        progress_callback: Optional[Callable[[Dict[str, Any]], Awaitable[None]]] = None,
    ) -> str:
        # Prefer native function-calling loop when enabled and provider supports it
        if _native_loop_enabled():
            return await self._agent_service.run_native_tool_loop(
                user_message=prompt,
                chat_id=chat_id,
                user_id=user_id,
                session_id=session_id,
                agent_id=agent_id,
                progress_callback=progress_callback,
            )
        return await self._agent_service.run_prompt_with_tool_loop(
            prompt=prompt,
            chat_id=chat_id,
            user_id=user_id,
            session_id=session_id,
            agent_id=agent_id,
            progress_callback=progress_callback,
        )
