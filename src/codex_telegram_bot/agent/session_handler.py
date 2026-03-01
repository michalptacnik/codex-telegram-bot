from __future__ import annotations

from typing import Any, Awaitable, Callable, Dict, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from codex_telegram_bot.services.agent_service import AgentService
    from codex_telegram_bot.services.agent_service import TurnResult


async def run_turn(
    service: "AgentService",
    prompt: str,
    chat_id: int,
    user_id: int,
    session_id: str,
    agent_id: str = "default",
    progress_callback: Optional[Callable[[Dict[str, Any]], Awaitable[None]]] = None,
) -> "TurnResult":
    """Module-level adapter for turn/session orchestration."""
    return await service._run_turn_impl(
        prompt=prompt,
        chat_id=chat_id,
        user_id=user_id,
        session_id=session_id,
        agent_id=agent_id,
        progress_callback=progress_callback,
    )

