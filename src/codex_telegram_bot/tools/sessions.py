"""Agent-facing session tools (Issue #105).

Exposes sessions as first-class tools so the agent can delegate and track
background work safely, with tree-based visibility controls.

Tools:
  sessions_list    – enumerate available sessions
  sessions_history – retrieve session activity logs
  sessions_send    – transmit messages to active sessions
  sessions_spawn   – initiate new background sessions
  session_status   – query current session state
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from codex_telegram_bot.tools.base import ToolContext, ToolRequest, ToolResult


class SessionsListTool:
    """List sessions visible to the current user/chat."""
    name = "sessions_list"
    description = "List available sessions for the current user."

    def __init__(self, run_store: Any = None) -> None:
        self._store = run_store

    def run(self, request: ToolRequest, context: ToolContext) -> ToolResult:
        chat_id = int(request.args.get("chat_id", 0))
        user_id = int(request.args.get("user_id", 0))
        if not self._store:
            return ToolResult(ok=False, output="No session store configured.")
        if not chat_id or not user_id:
            return ToolResult(ok=False, output="chat_id and user_id are required.")
        try:
            sessions = self._store.list_sessions_for_chat_user(chat_id, user_id)
            lines = []
            for s in sessions:
                lines.append(f"- {s.session_id} [{s.status}] created={s.created_at}")
            return ToolResult(ok=True, output="\n".join(lines) if lines else "No sessions found.")
        except Exception as exc:
            return ToolResult(ok=False, output=f"Error: {exc}")


class SessionsHistoryTool:
    """Retrieve message history for a session with visibility enforcement."""
    name = "sessions_history"
    description = "Retrieve session message history."

    def __init__(self, run_store: Any = None) -> None:
        self._store = run_store

    def run(self, request: ToolRequest, context: ToolContext) -> ToolResult:
        session_id = str(request.args.get("session_id", ""))
        chat_id = int(request.args.get("chat_id", 0))
        user_id = int(request.args.get("user_id", 0))
        limit = int(request.args.get("limit", 20))
        if not self._store:
            return ToolResult(ok=False, output="No session store configured.")
        if not session_id:
            return ToolResult(ok=False, output="session_id is required.")
        # Visibility enforcement: verify session belongs to user
        try:
            session = self._store.get_session(session_id)
        except Exception:
            session = None
        if not session:
            return ToolResult(ok=False, output="Session not found.")
        if chat_id and session.chat_id != chat_id:
            return ToolResult(ok=False, output="Access denied: session belongs to a different chat.")
        if user_id and session.user_id != user_id:
            return ToolResult(ok=False, output="Access denied: session belongs to a different user.")
        try:
            messages = self._store.list_session_messages(session_id, limit=limit)
            lines = []
            for m in messages:
                lines.append(f"[{m.role}] {m.content[:200]}")
            return ToolResult(ok=True, output="\n".join(lines) if lines else "No messages.")
        except Exception as exc:
            return ToolResult(ok=False, output=f"Error: {exc}")


class SessionsSendTool:
    """Send a message to an active session."""
    name = "sessions_send"
    description = "Send a message to an active session."

    def __init__(self, run_store: Any = None) -> None:
        self._store = run_store

    def run(self, request: ToolRequest, context: ToolContext) -> ToolResult:
        session_id = str(request.args.get("session_id", ""))
        content = str(request.args.get("content", ""))
        chat_id = int(request.args.get("chat_id", 0))
        user_id = int(request.args.get("user_id", 0))
        if not self._store:
            return ToolResult(ok=False, output="No session store configured.")
        if not session_id or not content:
            return ToolResult(ok=False, output="session_id and content are required.")
        # Visibility check
        try:
            session = self._store.get_session(session_id)
        except Exception:
            session = None
        if not session:
            return ToolResult(ok=False, output="Session not found.")
        if chat_id and session.chat_id != chat_id:
            return ToolResult(ok=False, output="Access denied: session belongs to a different chat.")
        if user_id and session.user_id != user_id:
            return ToolResult(ok=False, output="Access denied: session belongs to a different user.")
        try:
            self._store.append_session_message(session_id, "user", content)
            return ToolResult(ok=True, output=f"Message sent to session {session_id}.")
        except Exception as exc:
            return ToolResult(ok=False, output=f"Error: {exc}")


class SessionsSpawnTool:
    """Spawn a new background session."""
    name = "sessions_spawn"
    description = "Spawn a new background session."

    def __init__(self, run_store: Any = None) -> None:
        self._store = run_store

    def run(self, request: ToolRequest, context: ToolContext) -> ToolResult:
        chat_id = int(request.args.get("chat_id", 0))
        user_id = int(request.args.get("user_id", 0))
        summary = str(request.args.get("summary", "background session"))
        if not self._store:
            return ToolResult(ok=False, output="No session store configured.")
        if not chat_id or not user_id:
            return ToolResult(ok=False, output="chat_id and user_id are required.")
        try:
            session = self._store.create_session(chat_id, user_id, agent_id="default")
            return ToolResult(ok=True, output=f"Spawned session {session.session_id}.")
        except Exception as exc:
            return ToolResult(ok=False, output=f"Error: {exc}")


class SessionStatusTool:
    """Query the status of a session."""
    name = "session_status"
    description = "Get the current status of a session."

    def __init__(self, run_store: Any = None) -> None:
        self._store = run_store

    def run(self, request: ToolRequest, context: ToolContext) -> ToolResult:
        session_id = str(request.args.get("session_id", ""))
        chat_id = int(request.args.get("chat_id", 0))
        user_id = int(request.args.get("user_id", 0))
        if not self._store:
            return ToolResult(ok=False, output="No session store configured.")
        if not session_id:
            return ToolResult(ok=False, output="session_id is required.")
        try:
            session = self._store.get_session(session_id)
        except Exception:
            session = None
        if not session:
            return ToolResult(ok=False, output="Session not found.")
        if chat_id and session.chat_id != chat_id:
            return ToolResult(ok=False, output="Access denied: session belongs to a different chat.")
        if user_id and session.user_id != user_id:
            return ToolResult(ok=False, output="Access denied: session belongs to a different user.")
        return ToolResult(ok=True, output=(
            f"session_id={session.session_id}\n"
            f"status={session.status}\n"
            f"agent={session.current_agent_id}\n"
            f"created={session.created_at}\n"
            f"updated={session.updated_at}"
        ))
