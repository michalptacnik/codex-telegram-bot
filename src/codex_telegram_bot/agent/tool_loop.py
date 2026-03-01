from __future__ import annotations

from typing import Any, Awaitable, Dict, Optional, TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from codex_telegram_bot.services.agent_service import AgentService


async def run_native_tool_loop(
    service: "AgentService",
    user_message: str,
    chat_id: int,
    user_id: int,
    session_id: str,
    agent_id: str = "default",
    progress_callback: Optional[Callable[[Dict[str, Any]], Awaitable[None]]] = None,
) -> str:
    """Module-level adapter for the native tool loop implementation."""
    return await service._run_native_tool_loop_impl(
        user_message=user_message,
        chat_id=chat_id,
        user_id=user_id,
        session_id=session_id,
        agent_id=agent_id,
        progress_callback=progress_callback,
    )


async def run_prompt_with_tool_loop(
    service: "AgentService",
    prompt: str,
    chat_id: int,
    user_id: int,
    session_id: str,
    agent_id: str = "default",
    progress_callback: Optional[Callable[[Dict[str, Any]], Awaitable[None]]] = None,
    autonomy_depth: int = 0,
) -> str:
    """Module-level adapter for the main tool-loop orchestration."""
    return await service._run_prompt_with_tool_loop_impl(
        prompt=prompt,
        chat_id=chat_id,
        user_id=user_id,
        session_id=session_id,
        agent_id=agent_id,
        progress_callback=progress_callback,
        autonomy_depth=autonomy_depth,
    )

