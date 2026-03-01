import os

from codex_telegram_bot.tools.base import ToolContext, ToolRegistry, ToolRequest, ToolResult
from codex_telegram_bot.tools.email import (
    SendEmailSmtpTool,
    SendEmailTool,
    email_tool_enabled,
    is_email_tool_enabled,
)
from codex_telegram_bot.tools.files import ReadFileTool, WriteFileTool
from codex_telegram_bot.tools.git import (
    GitAddTool,
    GitCommitTool,
    GitDiffTool,
    GitLogTool,
    GitStatusTool,
)
from codex_telegram_bot.tools.memory import MemoryGetTool, MemorySearchTool
from codex_telegram_bot.tools.provider import ProviderStatusTool, ProviderSwitchTool
from codex_telegram_bot.tools.sessions import (
    SessionsListTool,
    SessionsHistoryTool,
    SessionsSendTool,
    SessionsSpawnTool,
    SessionStatusTool,
)
from codex_telegram_bot.tools.shell import ShellExecTool
from codex_telegram_bot.tools.ssh import SshDetectionTool
from codex_telegram_bot.tools.web import WebSearchTool, web_search_tool_enabled


def build_default_tool_registry(
    provider_registry=None,
    run_store=None,
    mcp_bridge=None,
    process_manager=None,
) -> ToolRegistry:
    """Build the default tool registry.

    Pass a ``ProviderRegistry`` instance to also register
    ``provider_status`` and ``provider_switch`` tools.
    Pass a ``SqliteRunStore`` to register session tools.
    Pass an ``McpBridge`` to register MCP tools.
    """
    registry = ToolRegistry()
    registry.register(ReadFileTool())
    registry.register(WriteFileTool())
    registry.register(GitStatusTool())
    registry.register(GitDiffTool())
    registry.register(GitLogTool())
    registry.register(GitAddTool())
    registry.register(GitCommitTool())
    registry.register(ShellExecTool(process_manager=process_manager))
    registry.register(SshDetectionTool())
    if web_search_tool_enabled(os.environ):
        registry.register(WebSearchTool())
    if email_tool_enabled(os.environ):
        registry.register(SendEmailSmtpTool())
    if is_email_tool_enabled(os.environ):
        registry.register(SendEmailTool())
    if provider_registry is not None:
        registry.register(ProviderStatusTool(provider_registry))
        registry.register(ProviderSwitchTool(provider_registry))
    # Session tools (Issue #105)
    if run_store is not None:
        registry.register(SessionsListTool(run_store))
        registry.register(SessionsHistoryTool(run_store))
        registry.register(SessionsSendTool(run_store))
        registry.register(SessionsSpawnTool(run_store))
        registry.register(SessionStatusTool(run_store))
    # Memory tools (Issue #106)
    registry.register(MemoryGetTool())
    registry.register(MemorySearchTool())
    # MCP tools (Issue #103)
    if mcp_bridge is not None:
        from codex_telegram_bot.services.mcp_bridge import McpSearchTool, McpCallTool
        registry.register(McpSearchTool(mcp_bridge))
        registry.register(McpCallTool(mcp_bridge))
    return registry


__all__ = [
    "ToolContext",
    "ToolRegistry",
    "ToolRequest",
    "ToolResult",
    "SendEmailSmtpTool",
    "SendEmailTool",
    "email_tool_enabled",
    "is_email_tool_enabled",
    "ReadFileTool",
    "WriteFileTool",
    "GitStatusTool",
    "GitDiffTool",
    "GitLogTool",
    "GitAddTool",
    "GitCommitTool",
    "ShellExecTool",
    "SshDetectionTool",
    "WebSearchTool",
    "web_search_tool_enabled",
    "ProviderStatusTool",
    "ProviderSwitchTool",
    "MemoryGetTool",
    "MemorySearchTool",
    "SessionsListTool",
    "SessionsHistoryTool",
    "SessionsSendTool",
    "SessionsSpawnTool",
    "SessionStatusTool",
    "build_default_tool_registry",
]
