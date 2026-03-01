from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from codex_telegram_bot.services.access_control import SpendLimitExceeded, UnauthorizedAction
from codex_telegram_bot.services.heartbeat import HeartbeatStore
from codex_telegram_bot.tools.base import ToolContext, ToolRequest, ToolResult


def _default_timezone() -> str:
    return "Europe/Amsterdam"


def _is_admin(access_controller: Any, user_id: int, chat_id: int) -> bool:
    if access_controller is None:
        return False
    profile = access_controller.get_profile(user_id, chat_id)
    return "admin" in {str(x).strip().lower() for x in profile.roles}


class HeartbeatGetTool:
    name = "heartbeat_get"
    description = "Read HEARTBEAT.md template/configuration."

    def run(self, request: ToolRequest, context: ToolContext) -> ToolResult:
        store = HeartbeatStore(context.workspace_root)
        return ToolResult(ok=True, output=store.get_text())


class HeartbeatUpdateTool:
    name = "heartbeat_update"
    description = "Update HEARTBEAT.md with full text or structured patch."

    def run(self, request: ToolRequest, context: ToolContext) -> ToolResult:
        store = HeartbeatStore(context.workspace_root)
        if "text" in request.args and str(request.args.get("text") or "").strip():
            try:
                out = store.update_text(str(request.args.get("text") or ""))
            except Exception as exc:
                return ToolResult(ok=False, output=f"Failed to update heartbeat text: {exc}")
            return ToolResult(ok=True, output=out)
        patch = request.args.get("patch")
        if patch is None:
            return ToolResult(ok=False, output="Provide either text or patch.")
        if not isinstance(patch, dict):
            return ToolResult(ok=False, output="patch must be an object.")
        try:
            out = store.update_patch(patch)
        except Exception as exc:
            return ToolResult(ok=False, output=f"Failed to update heartbeat patch: {exc}")
        return ToolResult(ok=True, output=out)


class HeartbeatRunOnceTool:
    name = "heartbeat_run_once"
    description = "Run one heartbeat probe and optionally deliver proactive message."

    def __init__(self, run_store: Any = None, access_controller: Any = None, messenger: Any = None) -> None:
        self._store = run_store
        self._access = access_controller
        self._messenger = messenger

    async def arun(self, request: ToolRequest, context: ToolContext) -> ToolResult:
        if self._store is None:
            return ToolResult(ok=False, output="No session store configured.")
        session_id = str(request.args.get("session_id") or context.session_id or "").strip()
        if not session_id:
            return ToolResult(ok=False, output="session_id is required.")
        dry_run = bool(request.args.get("dry_run", False))
        session = self._store.get_session(session_id)
        if session is None:
            return ToolResult(ok=False, output="Session not found.")
        requester = int(context.user_id or 0)
        request_chat = int(context.chat_id or 0)
        is_admin = _is_admin(self._access, requester, request_chat)
        if requester and not is_admin and int(session.user_id) != requester:
            return ToolResult(ok=False, output="Access denied: cannot run heartbeat for another user's session.")
        if self._access is not None and requester:
            try:
                self._access.check_action(requester, "send_prompt", request_chat)
            except UnauthorizedAction as exc:
                return ToolResult(ok=False, output=f"Access denied: {exc}")
        hb = HeartbeatStore(context.workspace_root)
        try:
            decision = hb.evaluate(
                timezone_name=_default_timezone(),
                now_utc=datetime.now(timezone.utc),
            )
        except Exception as exc:
            return ToolResult(ok=False, output=f"Heartbeat evaluation failed: {exc}")
        if decision.quiet_hours_blocked:
            return ToolResult(ok=True, output='{"action":"NO_ACTION","reason":"quiet_hours"}')
        if decision.action != "ACTION" or not decision.text.strip():
            return ToolResult(ok=True, output='{"action":"NO_ACTION"}')
        if dry_run:
            return ToolResult(
                ok=True,
                output=json.dumps(
                    {
                        "action": "ACTION",
                        "type": "message",
                        "dry_run": True,
                        "text": decision.text,
                    },
                    ensure_ascii=True,
                ),
            )
        if self._access is not None and requester:
            try:
                self._access.record_spend(user_id=requester, amount_usd=0.0, chat_id=request_chat)
            except SpendLimitExceeded as exc:
                return ToolResult(ok=False, output=f"Spend ceiling reached: {exc}")
        if self._messenger is not None:
            await self._messenger.deliver(
                {
                    "session_id": session_id,
                    "chat_id": int(session.chat_id),
                    "user_id": int(session.user_id),
                    "text": decision.text,
                    "silent": True,
                }
            )
        self._store.append_session_message(session_id=session_id, role="assistant", content=decision.text, run_id="")
        return ToolResult(ok=True, output='{"action":"ACTION","type":"message","delivered":true}')

    def run(self, request: ToolRequest, context: ToolContext) -> ToolResult:
        return ToolResult(ok=False, output="heartbeat_run_once requires async execution.")
