from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Dict, Optional

from codex_telegram_bot.agent_core.router import AgentRouter
from codex_telegram_bot.domain.sessions import TelegramSessionRecord

if TYPE_CHECKING:
    from codex_telegram_bot.services.agent_service import AgentService


@dataclass(frozen=True)
class AgentResponse:
    session_id: str
    output: str


class Agent:
    """Agent Core entrypoint for transport layers."""

    def __init__(self, agent_service: "AgentService", router: Optional[AgentRouter] = None):
        self._agent_service = agent_service
        self._router = router or AgentRouter(agent_service=agent_service)

    def reset_session(self, chat_id: int, user_id: int) -> TelegramSessionRecord:
        return self._agent_service.reset_session(chat_id=chat_id, user_id=user_id)

    async def handle_message(
        self,
        chat_id: int,
        user_id: int,
        text: str,
        agent_id: str = "default",
        progress_callback: Optional[Callable[[Dict[str, Any]], Awaitable[None]]] = None,
    ) -> AgentResponse:
        session = self._agent_service.get_or_create_session(chat_id=chat_id, user_id=user_id)
        self._agent_service.append_session_user_message(session.session_id, text)
        output = await self._router.route_prompt(
            prompt=text,
            chat_id=chat_id,
            user_id=user_id,
            session_id=session.session_id,
            agent_id=agent_id,
            progress_callback=progress_callback,
        )
        self._agent_service.append_session_assistant_message(session.session_id, output)
        return AgentResponse(session_id=session.session_id, output=output)
