from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol


@dataclass(frozen=True)
class ToolRequest:
    name: str
    args: Dict[str, object]


@dataclass(frozen=True)
class ToolContext:
    workspace_root: Path
    policy_profile: str = "balanced"
    chat_id: int = 0
    user_id: int = 0
    session_id: str = ""


@dataclass(frozen=True)
class ToolResult:
    ok: bool
    output: str


class Tool(Protocol):
    name: str

    def run(self, request: ToolRequest, context: ToolContext) -> ToolResult:
        ...


# ---------------------------------------------------------------------------
# Native function-calling tool schema catalogue
# ---------------------------------------------------------------------------
# Each entry maps tool name -> Anthropic-style tool definition.
# These are passed via the ``tools`` parameter of the Messages API.
# Tools not listed here still work in the legacy text-parsing path but
# won't be offered for native function calling.
NATIVE_TOOL_SCHEMAS: Dict[str, Dict[str, Any]] = {
    "read_file": {
        "name": "read_file",
        "description": "Read a file from the workspace. Returns file contents.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path relative to workspace root"},
                "max_bytes": {"type": "integer", "description": "Maximum bytes to read (default 50000)"},
            },
            "required": ["path"],
        },
    },
    "write_file": {
        "name": "write_file",
        "description": "Write content to a file in the workspace.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path relative to workspace root"},
                "content": {"type": "string", "description": "Content to write to the file"},
            },
            "required": ["path", "content"],
        },
    },
    "shell_exec": {
        "name": "shell_exec",
        "description": "Execute a shell command. Use for running scripts, builds, tests, etc.",
        "input_schema": {
            "type": "object",
            "properties": {
                "cmd": {"type": "string", "description": "Shell command to execute"},
                "timeout_sec": {"type": "integer", "description": "Timeout in seconds (default 30, max 120)"},
            },
            "required": ["cmd"],
        },
    },
    "git_status": {
        "name": "git_status",
        "description": "Show git working tree status.",
        "input_schema": {
            "type": "object",
            "properties": {
                "short": {"type": "boolean", "description": "Use short format (default true)"},
            },
            "required": [],
        },
    },
    "git_diff": {
        "name": "git_diff",
        "description": "Show git diff of changes.",
        "input_schema": {
            "type": "object",
            "properties": {
                "staged": {"type": "boolean", "description": "Show staged changes only (default false)"},
            },
            "required": [],
        },
    },
    "git_log": {
        "name": "git_log",
        "description": "Show recent git commit log.",
        "input_schema": {
            "type": "object",
            "properties": {
                "n": {"type": "integer", "description": "Number of commits to show (default 10, max 50)"},
            },
            "required": [],
        },
    },
    "git_add": {
        "name": "git_add",
        "description": "Stage files for git commit.",
        "input_schema": {
            "type": "object",
            "properties": {
                "paths": {
                    "description": "File paths to stage",
                    "oneOf": [
                        {"type": "string"},
                        {"type": "array", "items": {"type": "string"}},
                    ],
                },
            },
            "required": ["paths"],
        },
    },
    "git_commit": {
        "name": "git_commit",
        "description": "Create a git commit with the staged changes.",
        "input_schema": {
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "Commit message"},
            },
            "required": ["message"],
        },
    },
    "memory_get": {
        "name": "memory_get",
        "description": "Retrieve content from a memory file by path and optional line range.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Memory file path"},
                "startLine": {"type": "integer", "description": "Start line (1-based)"},
                "endLine": {"type": "integer", "description": "End line (1-based)"},
            },
            "required": ["path"],
        },
    },
    "memory_search": {
        "name": "memory_search",
        "description": "Search memory files by query, returning ranked results.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
                "k": {"type": "integer", "description": "Maximum number of results (default 10)"},
            },
            "required": ["query"],
        },
    },
    "web_search": {
        "name": "web_search",
        "description": "Search the public web and return source links with snippets.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Web query string"},
                "k": {"type": "integer", "description": "Maximum results to return (default 5, max 10)"},
                "timeout_sec": {"type": "integer", "description": "Network timeout in seconds (default 15)"},
            },
            "required": ["query"],
        },
    },
    "send_message": {
        "name": "send_message",
        "description": "Send a proactive message to a target session owner.",
        "input_schema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "Target session ID. Defaults to current session"},
                "text": {"type": "string", "description": "Message text to deliver"},
                "markdown": {"type": "boolean", "description": "Render as markdown where supported"},
                "silent": {"type": "boolean", "description": "Suppress notifications where supported"},
            },
            "required": ["text"],
        },
    },
    "schedule_task": {
        "name": "schedule_task",
        "description": "Create a one-shot or recurring schedule for reminder delivery.",
        "input_schema": {
            "type": "object",
            "properties": {
                "when": {"type": "string", "description": "Natural language time (e.g. 'next Monday at 23:45')"},
                "message": {"type": "string", "description": "Reminder text"},
                "repeat": {"type": "string", "description": "none|hourly|daily|weekly|cron:<expr>"},
                "session_id": {"type": "string", "description": "Target session ID; defaults to current session"},
            },
            "required": ["message"],
        },
    },
    "list_schedules": {
        "name": "list_schedules",
        "description": "List active schedules.",
        "input_schema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "Optional session filter"},
            },
            "required": [],
        },
    },
    "cancel_schedule": {
        "name": "cancel_schedule",
        "description": "Cancel a schedule by id.",
        "input_schema": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "Schedule id"},
            },
            "required": ["id"],
        },
    },
    "send_email_smtp": {
        "name": "send_email_smtp",
        "description": "Send an email via SMTP.",
        "input_schema": {
            "type": "object",
            "properties": {
                "to": {"type": "string", "description": "Recipient email address"},
                "subject": {"type": "string", "description": "Email subject"},
                "body": {"type": "string", "description": "Email body text"},
                "dry_run": {"type": "boolean", "description": "If true, simulate without sending"},
            },
            "required": ["to", "subject", "body"],
        },
    },
    "send_email": {
        "name": "send_email",
        "description": "Send an email.",
        "input_schema": {
            "type": "object",
            "properties": {
                "to": {"type": "string", "description": "Recipient email address"},
                "subject": {"type": "string", "description": "Email subject"},
                "body": {"type": "string", "description": "Email body text"},
                "dry_run": {"type": "boolean", "description": "If true, simulate without sending"},
            },
            "required": ["to", "subject", "body"],
        },
    },
    "ssh_detect": {
        "name": "ssh_detect",
        "description": "Detect available SSH keys and agent status.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    "provider_status": {
        "name": "provider_status",
        "description": "Show current LLM provider status and health.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    "provider_switch": {
        "name": "provider_switch",
        "description": "Switch the active LLM provider.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Provider name to switch to"},
            },
            "required": ["name"],
        },
    },
}


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: Dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        name = (getattr(tool, "name", "") or "").strip().lower()
        if not name:
            raise ValueError("Tool name is required.")
        self._tools[name] = tool

    def get(self, name: str) -> Optional[Tool]:
        return self._tools.get((name or "").strip().lower())

    def names(self) -> List[str]:
        return sorted(self._tools.keys())

    def tool_schemas(self) -> List[Dict[str, Any]]:
        """Return Anthropic-style tool definitions for all registered tools
        that have a native schema defined in NATIVE_TOOL_SCHEMAS."""
        schemas: List[Dict[str, Any]] = []
        for name in sorted(self._tools.keys()):
            schema = NATIVE_TOOL_SCHEMAS.get(name)
            if schema is not None:
                schemas.append(dict(schema))
        return schemas
