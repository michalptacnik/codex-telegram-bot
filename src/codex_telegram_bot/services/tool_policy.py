"""Tool policy groups, wildcard allow/deny, and /elevated session state (Issue #107).

Upgrades the policy model with group-based controls and elevated-mode semantics
on a per-session basis.

Group aliases:
  filesystem  — read_file, write_file
  runtime     — shell_exec, exec
  sessions    — sessions_list, sessions_history, sessions_send, sessions_spawn, session_status
  memory      — memory_get, memory_search
  web         — web_search, mcp_search, mcp_call
  git         — git_status, git_diff, git_log, git_add, git_commit

Wildcard patterns:
  *           — matches all tools
  git_*       — matches all tools starting with "git_"
"""
from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

# ---------------------------------------------------------------------------
# Group definitions
# ---------------------------------------------------------------------------

TOOL_GROUPS: Dict[str, List[str]] = {
    "filesystem": ["read_file", "write_file"],
    "runtime": ["shell_exec"],
    "sessions": ["sessions_list", "sessions_history", "sessions_send", "sessions_spawn", "session_status"],
    "memory": ["memory_get", "memory_search"],
    "web": ["web_search", "mcp_search", "mcp_call"],
    "git": ["git_status", "git_diff", "git_log", "git_add", "git_commit"],
}

VALID_ELEVATED_MODES = {"on", "off", "ask", "full"}


@dataclass
class ToolPolicyConfig:
    """Policy configuration for a session."""
    allow_patterns: List[str] = field(default_factory=lambda: ["*"])
    deny_patterns: List[str] = field(default_factory=list)
    elevated_mode: str = "off"  # on | off | ask | full
    per_provider_restrictions: Dict[str, List[str]] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolPolicyDecision:
    allowed: bool
    reason: str


class ToolPolicyEngine:
    """Evaluates tool access using group aliases, wildcards, and elevated mode."""

    def __init__(self, default_config: Optional[ToolPolicyConfig] = None) -> None:
        self._default = default_config or ToolPolicyConfig()
        self._session_configs: Dict[str, ToolPolicyConfig] = {}

    def set_session_config(self, session_id: str, config: ToolPolicyConfig) -> None:
        self._session_configs[session_id] = config

    def get_session_config(self, session_id: str) -> ToolPolicyConfig:
        return self._session_configs.get(session_id, self._default)

    def set_elevated(self, session_id: str, mode: str) -> str:
        """Set the elevated mode for a session. Returns the new mode."""
        mode = (mode or "").strip().lower()
        if mode not in VALID_ELEVATED_MODES:
            return self.get_session_config(session_id).elevated_mode
        config = self.get_session_config(session_id)
        new_config = ToolPolicyConfig(
            allow_patterns=config.allow_patterns,
            deny_patterns=config.deny_patterns,
            elevated_mode=mode,
            per_provider_restrictions=config.per_provider_restrictions,
        )
        self._session_configs[session_id] = new_config
        return mode

    def evaluate(
        self,
        tool_name: str,
        session_id: str = "",
        provider_name: str = "",
        is_admin: bool = False,
    ) -> ToolPolicyDecision:
        """Evaluate whether a tool call is allowed."""
        config = self.get_session_config(session_id)

        # Expand tool name if it's a group reference
        expanded = self._expand_tool(tool_name)

        # Check deny patterns first (deny takes precedence)
        for pattern in config.deny_patterns:
            deny_expanded = self._expand_pattern(pattern)
            for name in expanded:
                if any(fnmatch.fnmatch(name, dp) for dp in deny_expanded):
                    return ToolPolicyDecision(
                        allowed=False,
                        reason=f"Tool '{name}' denied by pattern '{pattern}'.",
                    )

        # Check allow patterns
        allowed = False
        for pattern in config.allow_patterns:
            allow_expanded = self._expand_pattern(pattern)
            for name in expanded:
                if any(fnmatch.fnmatch(name, ap) for ap in allow_expanded):
                    allowed = True
                    break
            if allowed:
                break

        if not allowed:
            return ToolPolicyDecision(
                allowed=False,
                reason=f"Tool '{tool_name}' not matched by any allow pattern.",
            )

        # Check per-provider restrictions
        if provider_name and config.per_provider_restrictions:
            restricted = config.per_provider_restrictions.get(provider_name, [])
            if restricted:
                for name in expanded:
                    for pattern in restricted:
                        if fnmatch.fnmatch(name, pattern):
                            return ToolPolicyDecision(
                                allowed=False,
                                reason=f"Tool '{name}' restricted for provider '{provider_name}'.",
                            )

        # Elevated mode checks
        if config.elevated_mode == "off" and not is_admin:
            # In non-elevated mode, restrict runtime/web tools for non-admins
            for name in expanded:
                if name in TOOL_GROUPS.get("runtime", []):
                    return ToolPolicyDecision(
                        allowed=False,
                        reason=f"Tool '{name}' requires elevated mode.",
                    )

        return ToolPolicyDecision(allowed=True, reason="Allowed.")

    def expand_group(self, group_name: str) -> List[str]:
        """Expand a group alias to its member tool names."""
        return list(TOOL_GROUPS.get(group_name, []))

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _expand_tool(self, tool_name: str) -> List[str]:
        """If tool_name is a group name, expand it; otherwise return as-is."""
        if tool_name in TOOL_GROUPS:
            return TOOL_GROUPS[tool_name]
        return [tool_name]

    def _expand_pattern(self, pattern: str) -> List[str]:
        """Expand group references in patterns."""
        # If pattern matches a group name exactly, expand to member tools
        if pattern in TOOL_GROUPS:
            return TOOL_GROUPS[pattern]
        return [pattern]
