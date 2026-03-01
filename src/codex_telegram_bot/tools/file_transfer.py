from __future__ import annotations

import hashlib
import mimetypes
import os
from pathlib import Path
from typing import Any

from codex_telegram_bot.services.access_control import SpendLimitExceeded, UnauthorizedAction
from codex_telegram_bot.tools.base import ToolContext, ToolRequest, ToolResult


def _resolve_send_path(context: ToolContext, raw_path: str) -> Path:
    value = str(raw_path or "").strip()
    if not value:
        raise ValueError("path is required")
    root = context.workspace_root.resolve()
    candidate = Path(value).expanduser()
    if candidate.is_absolute():
        resolved = candidate.resolve()
        if str(getattr(context, "policy_profile", "") or "").strip().lower() != "trusted":
            raise ValueError("absolute paths require trusted policy profile")
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise ValueError("absolute path outside workspace root") from exc
        return resolved
    resolved = (root / candidate).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError("path escapes workspace root") from exc
    return resolved


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _file_send_cost_usd() -> float:
    raw = (os.environ.get("FILE_SEND_COST_USD") or "0").strip()
    try:
        value = float(raw)
    except ValueError:
        return 0.0
    return max(0.0, value)


class SendFileTool:
    name = "send_file"
    description = "Send a workspace file as Telegram attachment and audit it."

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
        raw_path = str(request.args.get("path") or "").strip()
        caption = str(request.args.get("caption") or "").strip()
        explicit_kind = str(request.args.get("kind") or "").strip().lower()
        if explicit_kind and explicit_kind not in {"document", "photo", "video", "audio"}:
            return ToolResult(ok=False, output="kind must be one of: document, photo, video, audio.")
        try:
            resolved = _resolve_send_path(context, raw_path)
        except Exception as exc:
            return ToolResult(ok=False, output=f"Invalid path: {exc}")
        if not resolved.exists() or not resolved.is_file():
            return ToolResult(ok=False, output="File does not exist.")
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
            return ToolResult(ok=False, output="Access denied: cannot send files to another user's session.")
        if self._access is not None and requester_user_id:
            try:
                self._access.record_spend(
                    user_id=requester_user_id,
                    amount_usd=_file_send_cost_usd(),
                    chat_id=requester_chat_id,
                )
            except SpendLimitExceeded as exc:
                return ToolResult(ok=False, output=f"Spend ceiling reached: {exc}")
        mime, _ = mimetypes.guess_type(str(resolved))
        mime = mime or "application/octet-stream"
        kind = explicit_kind or "document"
        if not explicit_kind:
            if mime.startswith("image/"):
                kind = "photo"
            elif mime.startswith("video/"):
                kind = "video"
            elif mime.startswith("audio/"):
                kind = "audio"
        if self._messenger is None:
            return ToolResult(ok=False, output="No proactive messenger configured.")
        result = await self._messenger.deliver(
            {
                "session_id": session_id,
                "chat_id": int(session.chat_id),
                "user_id": int(session.user_id),
                "file_path": str(resolved),
                "filename": resolved.name,
                "kind": kind,
                "caption": caption,
            }
        )
        failed = result.get("failed") or {}
        if failed and not result.get("delivered"):
            return ToolResult(ok=False, output=f"File delivery failed: {failed}")
        self._store.append_session_message(
            session_id=session_id,
            role="assistant",
            content=(caption or f"Sent file: {resolved.name}"),
            run_id="",
        )
        message_id = self._store.create_channel_message(
            session_id=session_id,
            user_id=int(session.user_id),
            channel="telegram",
            channel_message_id="",
            sender="assistant",
            text=caption,
        )
        self._store.create_attachment(
            message_id=message_id,
            session_id=session_id,
            user_id=int(session.user_id),
            channel="telegram",
            kind=kind,
            filename=resolved.name,
            mime=mime,
            size_bytes=int(resolved.stat().st_size),
            sha256=_sha256(resolved),
            local_path=str(resolved),
            remote_file_id="",
        )
        return ToolResult(
            ok=True,
            output=(
                f"Sent file {resolved.name} ({kind}) from {resolved}. "
                f"transports={','.join(result.get('delivered', [])) or 'none'}"
            ),
        )

    def run(self, request: ToolRequest, context: ToolContext) -> ToolResult:
        return ToolResult(ok=False, output="send_file requires async execution.")
