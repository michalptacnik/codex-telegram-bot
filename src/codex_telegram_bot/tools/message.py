from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from codex_telegram_bot.events.event_bus import RunEvent
from codex_telegram_bot.services.access_control import SpendLimitExceeded, UnauthorizedAction
from codex_telegram_bot.tools.base import ToolContext, ToolRequest, ToolResult

logger = logging.getLogger(__name__)


def _message_send_cost_usd() -> float:
    raw = (os.environ.get("MESSAGE_SEND_COST_USD") or "0").strip()
    try:
        value = float(raw)
    except ValueError:
        return 0.0
    return max(0.0, value)


class SendMessageTool:
    """Deliver a proactive message to a target session owner."""

    name = "send_message"
    description = "Send a proactive message to a session owner via configured transports."

    def __init__(
        self,
        run_store: Any = None,
        access_controller: Any = None,
        messenger: Any = None,
    ) -> None:
        self._store = run_store
        self._access = access_controller
        self._messenger = messenger

    async def arun(self, request: ToolRequest, context: ToolContext) -> ToolResult:
        if self._store is None:
            return ToolResult(ok=False, output="No session store configured.")
        session_id = str(request.args.get("session_id") or context.session_id or "").strip()
        text = str(request.args.get("text") or "").strip()
        markdown = bool(request.args.get("markdown", False))
        silent = bool(request.args.get("silent", False))
        if not session_id:
            return ToolResult(ok=False, output="session_id is required.")
        if not text:
            return ToolResult(ok=False, output="text is required.")
        session = self._store.get_session(session_id)
        if session is None:
            return ToolResult(ok=False, output="Session not found.")

        requester_user_id = int(getattr(context, "user_id", 0) or 0)
        requester_chat_id = int(getattr(context, "chat_id", 0) or 0)
        is_admin = False
        if self._access is not None and requester_user_id:
            try:
                self._access.check_action(requester_user_id, "send_prompt", requester_chat_id)
            except UnauthorizedAction as exc:
                return ToolResult(ok=False, output=f"Access denied: {exc}")
            profile = self._access.get_profile(requester_user_id, requester_chat_id)
            is_admin = "admin" in {str(x).strip().lower() for x in profile.roles}
        if requester_user_id and (not is_admin) and int(session.user_id) != requester_user_id:
            return ToolResult(ok=False, output="Access denied: cannot message another user's session.")
        if self._access is not None and requester_user_id:
            try:
                self._access.record_spend(
                    user_id=requester_user_id,
                    amount_usd=_message_send_cost_usd(),
                    chat_id=requester_chat_id,
                )
            except SpendLimitExceeded as exc:
                return ToolResult(ok=False, output=f"Spend ceiling reached: {exc}")

        self._store.append_session_message(session_id=session_id, role="assistant", content=text, run_id="")
        payload: Dict[str, Any] = {
            "session_id": session_id,
            "chat_id": int(session.chat_id),
            "user_id": int(session.user_id),
            "text": text,
            "markdown": markdown,
            "silent": silent,
        }
        audit_run_id = self._store.create_run(f"proactive send_message session={session_id}")
        self._store.mark_running(audit_run_id)
        self._store.append_event(
            RunEvent(
                run_id=audit_run_id,
                event_type="message_send.requested",
                payload=(
                    f"session_id={session_id} requester={requester_user_id} "
                    f"markdown={int(markdown)} silent={int(silent)}"
                ),
                created_at=datetime.now(timezone.utc),
            )
        )
        result = {"attempted": [], "delivered": [], "failed": {}}
        if self._messenger is not None:
            result = await self._messenger.deliver(payload)
        self._store.append_event(
            RunEvent(
                run_id=audit_run_id,
                event_type="message_send.result",
                payload=(
                    f"session_id={session_id} delivered={','.join(result.get('delivered', [])) or '-'} "
                    f"failed={','.join(sorted((result.get('failed') or {}).keys())) or '-'}"
                ),
                created_at=datetime.now(timezone.utc),
            )
        )
        self._store.mark_completed(
            audit_run_id,
            f"send_message delivered={','.join(result.get('delivered', [])) or '-'}",
        )
        failed = result.get("failed") or {}
        if failed and not result.get("delivered"):
            logger.warning("send_message delivery failed: %s", failed)
            return ToolResult(ok=False, output=f"Message queued in session but transport delivery failed: {failed}")
        return ToolResult(
            ok=True,
            output=(
                f"Delivered proactive message to session {session_id}. "
                f"transports={','.join(result.get('delivered', [])) or 'none'}"
            ),
        )

    def run(self, request: ToolRequest, context: ToolContext) -> ToolResult:
        return ToolResult(ok=False, output="send_message requires async execution.")
