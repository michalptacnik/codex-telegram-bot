from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, Optional

from codex_telegram_bot.execution.process_manager import ProcessManager
from codex_telegram_bot.tools.base import ToolContext, ToolRequest, ToolResult
from codex_telegram_bot.util import redact_with_audit

# Commands that are safe to run in short, synchronous mode.
SAFE_COMMANDS: frozenset[str] = frozenset(
    [
        "cat",
        "echo",
        "find",
        "grep",
        "head",
        "ls",
        "mkdir",
        "sed",
        "sort",
        "tail",
        "touch",
        "tree",
        "wc",
        "which",
        "basename",
        "dirname",
        "pwd",
        "env",
        "printenv",
        "date",
        "python3",
        "python",
        "pip",
        "pip3",
        "node",
        "npm",
        "yarn",
        "make",
        "cmake",
        "cargo",
        "go",
        "javac",
        "java",
        "mvn",
        "gradle",
        "pytest",
        "mypy",
        "ruff",
        "flake8",
        "black",
        "isort",
        "sh",
        "bash",
    ]
)

MAX_OUTPUT_CHARS = 4000
DEFAULT_TIMEOUT_SEC = 30


class ShellExecTool:
    """Shell execution tool with short and persistent session modes."""

    name = "shell_exec"

    def __init__(self, process_manager: Optional[ProcessManager] = None) -> None:
        self._process_manager = process_manager or ProcessManager()

    def run(self, request: ToolRequest, context: ToolContext) -> ToolResult:
        args = dict(request.args or {})
        action = str(args.get("action") or "").strip().lower()
        mode = str(args.get("mode") or "short").strip().lower()

        # Compatibility: legacy short invocation accepts only cmd + timeout_sec.
        if mode not in {"short", "session"} and action == "terminate" and mode in {"interrupt", "kill"}:
            args["terminate_mode"] = mode
            mode = "session"
        if mode not in {"short", "session"}:
            mode = "short"

        if mode == "short":
            return self._run_short(request=request, context=context, args=args)

        # Session mode defaults to start when no explicit action is given.
        resolved_action = action or "start"
        return self._run_session(action=resolved_action, context=context, args=args)

    def _run_short(self, request: ToolRequest, context: ToolContext, args: Dict[str, Any]) -> ToolResult:
        cmd = str(args.get("cmd") or "").strip()
        if not cmd:
            return self._result(ok=False, output="Error: 'cmd' arg is required.")

        if re.search(r"(^|[^A-Za-z0-9_])find\s+/(?:\s|$)", cmd.lower()):
            return self._result(ok=False, output="Error: unsafe search scope. Use paths inside WORKSPACE_ROOT only.")

        binary = Path(_first_token(cmd)).name
        if binary not in SAFE_COMMANDS:
            return self._result(ok=False, output=f"Error: '{binary}' is not in the safe command allowlist.")

        timeout = _coerce_int(args.get("timeout_sec"), DEFAULT_TIMEOUT_SEC, minimum=1, maximum=120)
        outcome = self._process_manager.run_short_command(
            cmd=cmd,
            workspace_root=context.workspace_root,
            timeout_sec=timeout,
        )
        output = str(outcome.get("output") or "(no output)")
        if outcome.get("returncode", 1) != 0:
            output = f"Error: exit code {outcome.get('returncode')}\n{output}".strip()
            return self._result(ok=False, output=output)
        return self._result(ok=True, output=output)

    def _run_session(self, action: str, context: ToolContext, args: Dict[str, Any]) -> ToolResult:
        chat_id = _coerce_int(args.get("_chat_id") or args.get("chat_id"), context.chat_id, minimum=0)
        user_id = _coerce_int(args.get("_user_id") or args.get("user_id"), context.user_id, minimum=0)

        session_id = str(args.get("session_id") or args.get("process_session_id") or "").strip()
        cursor = _coerce_optional_int(args.get("cursor"))

        if action == "start":
            cmd = str(args.get("cmd") or "").strip()
            if not cmd:
                return self._result(ok=False, output="Error: 'cmd' arg is required for session start.")
            pty_enabled = _coerce_bool(args.get("pty"), default=True)
            result = self._process_manager.start_session(
                chat_id=chat_id,
                user_id=user_id,
                cmd=cmd,
                workspace_root=context.workspace_root,
                policy_profile=context.policy_profile,
                pty=pty_enabled,
            )
            if not result.get("ok"):
                return self._result(ok=False, output=str(result.get("error") or "Error: failed to start session."))
            return self._result(
                ok=True,
                output=(
                    f"Session started: {result.get('session_id')}\n"
                    f"status={result.get('status')} pty={result.get('pty')} cursor={result.get('cursor')}"
                ),
            )

        if action == "list":
            sessions = self._process_manager.list_sessions(chat_id=chat_id, user_id=user_id, limit=20)
            if not sessions:
                return self._result(ok=True, output="No process sessions for this chat/user.")
            lines = ["Process sessions:"]
            for item in sessions[:20]:
                sid = str(item.get("process_session_id") or "")
                cmd_preview = " ".join(list(item.get("argv") or []))[:80]
                lines.append(f"- {sid[:16]} status={item.get('status')} pty={bool(item.get('pty_enabled'))} cmd={cmd_preview}")
            return self._result(ok=True, output="\n".join(lines))

        if action in {"poll", "tail"}:
            if not session_id:
                active = self._process_manager.get_active_session(chat_id=chat_id, user_id=user_id)
                session_id = str((active or {}).get("process_session_id") or "")
            if not session_id:
                return self._result(ok=False, output="Error: 'session_id' is required.")
            result = self._process_manager.poll_session(process_session_id=session_id, cursor=cursor)
            if not result.get("ok"):
                return self._result(ok=False, output=str(result.get("error") or "Error: poll failed."))
            output = str(result.get("output") or "(no new output)")
            meta = f"session={session_id} status={result.get('status')} cursor_next={result.get('cursor_next')}"
            return self._result(ok=True, output=f"{meta}\n{output}")

        if action == "write":
            if not session_id:
                return self._result(ok=False, output="Error: 'session_id' is required for write.")
            stdin_text = str(args.get("stdin") or "")
            result = self._process_manager.write_session(
                process_session_id=session_id,
                stdin_text=stdin_text,
                cursor=cursor,
            )
            if not result.get("ok"):
                return self._result(ok=False, output=str(result.get("error") or "Error: write failed."))
            output = str(result.get("output") or "(no new output)")
            return self._result(
                ok=True,
                output=f"session={session_id} cursor_next={result.get('cursor_next')}\n{output}",
            )

        if action == "terminate":
            if not session_id:
                active = self._process_manager.get_active_session(chat_id=chat_id, user_id=user_id)
                session_id = str((active or {}).get("process_session_id") or "")
            if not session_id:
                return self._result(ok=False, output="Error: 'session_id' is required for terminate.")
            terminate_mode = str(args.get("terminate_mode") or args.get("signal") or "interrupt").strip().lower()
            result = self._process_manager.terminate_session(process_session_id=session_id, mode=terminate_mode)
            if not result.get("ok"):
                return self._result(ok=False, output=str(result.get("error") or "Error: terminate failed."))
            return self._result(
                ok=True,
                output=f"session={session_id} status={result.get('status')} exit_code={result.get('exit_code')}",
            )

        if action == "status":
            if not session_id:
                active = self._process_manager.get_active_session(chat_id=chat_id, user_id=user_id)
                session_id = str((active or {}).get("process_session_id") or "")
            if not session_id:
                return self._result(ok=False, output="Error: 'session_id' is required for status.")
            result = self._process_manager.status(process_session_id=session_id)
            if not result.get("ok"):
                return self._result(ok=False, output=str(result.get("error") or "Error: status failed."))
            return self._result(
                ok=True,
                output=(
                    f"session={session_id} status={result.get('status')} exit_code={result.get('exit_code')} "
                    f"age={result.get('age_sec')}s idle={result.get('idle_sec')}s "
                    f"bytes={result.get('output_bytes')} pty={result.get('pty')}"
                ),
            )

        if action == "search":
            if not session_id:
                return self._result(ok=False, output="Error: 'session_id' is required for search.")
            query = str(args.get("query") or "").strip()
            if not query:
                return self._result(ok=False, output="Error: 'query' is required for search.")
            max_results = _coerce_int(args.get("max_results"), 5, minimum=1, maximum=10)
            context_lines = _coerce_int(args.get("context_lines"), 2, minimum=0, maximum=6)
            result = self._process_manager.search_log(
                process_session_id=session_id,
                query=query,
                max_results=max_results,
                context_lines=context_lines,
                cursor=int(cursor or 0),
            )
            if not result.get("ok"):
                return self._result(ok=False, output=str(result.get("error") or "Error: search failed."))
            matches = list(result.get("matches") or [])
            if not matches:
                return self._result(
                    ok=True,
                    output=(
                        f"No matches for '{query}' in session {session_id}. "
                        f"cursor_next={result.get('cursor_next')}"
                    ),
                )
            lines = [f"Search '{query}' in {session_id}:"]
            for item in matches:
                lines.append(f"- offset={item.get('offset')} line={item.get('line')}")
                lines.append(str(item.get("excerpt") or "")[:700])
            lines.append(f"cursor_next={result.get('cursor_next')} log={result.get('log_path')}")
            return self._result(ok=True, output="\n".join(lines))

        return self._result(ok=False, output=f"Error: unsupported session action '{action}'.")

    def _result(self, ok: bool, output: str) -> ToolResult:
        redacted = redact_with_audit(str(output or ""))
        text = redacted.text
        if len(text) > MAX_OUTPUT_CHARS:
            text = text[: MAX_OUTPUT_CHARS - 64]
            text += "\n(output clipped; use poll/search with cursor for more)"
        return ToolResult(ok=ok, output=text)


def _coerce_int(value: Any, default: int, minimum: int = 0, maximum: Optional[int] = None) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = int(default)
    parsed = max(minimum, parsed)
    if maximum is not None:
        parsed = min(maximum, parsed)
    return parsed


def _coerce_optional_int(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _coerce_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return bool(default)
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return bool(default)


def _first_token(cmd: str) -> str:
    for token in str(cmd or "").split():
        if token:
            return token
    return ""
