from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Dict, Optional

logger = logging.getLogger(__name__)

OutboundSender = Callable[[str, str], Awaitable[None]]


def _resolve_pending_by_prefix(items: list[dict], prefix: str) -> Optional[dict]:
    wanted = str(prefix or "").strip().lower()
    if not wanted:
        return None
    for item in items:
        approval_id = str(item.get("approval_id") or "").strip().lower()
        if approval_id.startswith(wanted):
            return item
    return None


class WhatsAppBridge:
    """Bridge WhatsApp inbound messages into the existing session/tool loop."""

    def __init__(
        self,
        *,
        agent_service: Any,
        run_store: Any,
        sender: OutboundSender | None = None,
        channel: str = "whatsapp",
        link_ttl_sec: int = 900,
    ) -> None:
        self._agent_service = agent_service
        self._run_store = run_store
        self._sender = sender
        self._channel = str(channel or "whatsapp").strip().lower()
        self._link_ttl_sec = max(60, int(link_ttl_sec or 900))

    @property
    def channel(self) -> str:
        return self._channel

    def create_link_code(self, *, chat_id: int, user_id: int, ttl_sec: int | None = None) -> dict:
        ttl = self._link_ttl_sec if ttl_sec is None else max(60, int(ttl_sec))
        return self._run_store.create_channel_link_code(
            channel=self._channel,
            chat_id=int(chat_id),
            user_id=int(user_id),
            ttl_sec=ttl,
        )

    async def deliver_proactive(self, payload: Dict[str, Any]) -> None:
        if self._sender is None:
            raise RuntimeError("whatsapp sender is not configured")
        text = str(payload.get("text") or "").strip()
        if not text:
            return
        chat_id = int(payload.get("chat_id") or 0)
        user_id = int(payload.get("user_id") or 0)
        if not chat_id or not user_id:
            return
        links = self._run_store.list_channel_links(
            channel=self._channel,
            chat_id=chat_id,
            user_id=user_id,
            limit=10,
        )
        if not links:
            return
        for link in links:
            external_id = str(link.get("external_user_id") or "").strip()
            if not external_id:
                continue
            await self._sender(external_id, text)

    async def handle_inbound(self, *, external_user_id: str, text: str) -> str:
        normalized_external = str(external_user_id or "").strip()
        inbound = str(text or "").strip()
        if not normalized_external:
            return "Missing sender identity."
        if not inbound:
            return "Please send a message."
        if inbound.lower().startswith("/link "):
            code = inbound.split(None, 1)[1].strip() if " " in inbound else ""
            linked = self._run_store.consume_channel_link_code(
                channel=self._channel,
                code=code,
                external_user_id=normalized_external,
            )
            if not linked:
                return "Invalid or expired link code."
            return "Linked successfully. You can chat now."

        link = self._run_store.get_channel_link(
            channel=self._channel,
            external_user_id=normalized_external,
        )
        if not link:
            return (
                "This number is not linked. Generate a WhatsApp link code in Control Center "
                "and send: /link <code>"
            )

        chat_id = int(link.get("chat_id") or 0)
        user_id = int(link.get("user_id") or 0)
        if not chat_id or not user_id:
            return "Link record is invalid."

        lowered = inbound.lower()
        if lowered.startswith("/pending"):
            pending = self._agent_service.list_pending_tool_approvals(
                chat_id=chat_id,
                user_id=user_id,
                limit=20,
            )
            if not pending:
                return self._with_profile_warning(chat_id=chat_id, user_id=user_id, text="No pending approvals.")
            lines = ["Pending approvals:"]
            for item in pending[:10]:
                aid = str(item.get("approval_id") or "")[:8]
                risk = str(item.get("risk_tier") or "high")
                lines.append(f"- {aid} risk={risk}")
            return self._with_profile_warning(chat_id=chat_id, user_id=user_id, text="\n".join(lines))

        if lowered.startswith("/approve "):
            prefix = inbound.split(None, 1)[1].strip()
            pending = self._agent_service.list_pending_tool_approvals(
                chat_id=chat_id,
                user_id=user_id,
                limit=50,
            )
            match = _resolve_pending_by_prefix(pending, prefix)
            if not match:
                return "Approval ID not found."
            out = await self._agent_service.approve_tool_action(
                approval_id=match["approval_id"],
                chat_id=chat_id,
                user_id=user_id,
            )
            return self._with_profile_warning(chat_id=chat_id, user_id=user_id, text=out)

        if lowered.startswith("/deny "):
            prefix = inbound.split(None, 1)[1].strip()
            pending = self._agent_service.list_pending_tool_approvals(
                chat_id=chat_id,
                user_id=user_id,
                limit=50,
            )
            match = _resolve_pending_by_prefix(pending, prefix)
            if not match:
                return "Approval ID not found."
            out = self._agent_service.deny_tool_action(
                approval_id=match["approval_id"],
                chat_id=chat_id,
                user_id=user_id,
            )
            return self._with_profile_warning(chat_id=chat_id, user_id=user_id, text=out)

        session = self._agent_service.get_or_create_session(chat_id=chat_id, user_id=user_id)
        out = await self._agent_service.run_prompt_with_tool_loop(
            prompt=inbound,
            chat_id=chat_id,
            user_id=user_id,
            session_id=session.session_id,
            agent_id=session.current_agent_id,
            progress_callback=None,
        )
        return self._with_profile_warning(chat_id=chat_id, user_id=user_id, text=out)

    def _with_profile_warning(self, *, chat_id: int, user_id: int, text: str) -> str:
        warning = self._unsafe_warning_for_admin(chat_id=chat_id, user_id=user_id)
        if not warning:
            return text
        clean = str(text or "").strip()
        if not clean:
            return warning
        return f"{warning}\n\n{clean}"

    def _unsafe_warning_for_admin(self, *, chat_id: int, user_id: int) -> str:
        access = getattr(self._agent_service, "access_controller", None)
        if access is None:
            return ""
        try:
            profile = access.get_profile(user_id, chat_id)
        except Exception:
            return ""
        roles = {str(role).strip().lower() for role in list(getattr(profile, "roles", []) or [])}
        if "admin" not in roles:
            return ""
        return str(self._agent_service.execution_profile_warning() or "").strip()
