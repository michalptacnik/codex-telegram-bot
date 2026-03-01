from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from codex_telegram_bot.services.agent_service import AgentService


def build_session_prompt(
    service: "AgentService",
    session_id: str,
    user_prompt: str,
    max_turns: int = 8,
) -> str:
    """Module-level adapter for prompt/context assembly."""
    return service._build_session_prompt_impl(
        session_id=session_id,
        user_prompt=user_prompt,
        max_turns=max_turns,
    )

