from codex_telegram_bot.tools.base import ToolContext, ToolRegistry, ToolRequest, ToolResult
from codex_telegram_bot.tools.email import SendEmailSmtpTool
from codex_telegram_bot.tools.files import ReadFileTool, WriteFileTool
from codex_telegram_bot.tools.git import (
    GitAddTool,
    GitCommitTool,
    GitDiffTool,
    GitLogTool,
    GitStatusTool,
)
from codex_telegram_bot.tools.provider import ProviderStatusTool, ProviderSwitchTool
from codex_telegram_bot.tools.shell import ShellExecTool
from codex_telegram_bot.tools.ssh import SshDetectionTool


def build_default_tool_registry(provider_registry=None) -> ToolRegistry:
    """Build the default tool registry.

    Pass a ``ProviderRegistry`` instance to also register
    ``provider_status`` and ``provider_switch`` tools.
    """
    registry = ToolRegistry()
    registry.register(ReadFileTool())
    registry.register(WriteFileTool())
    registry.register(GitStatusTool())
    registry.register(GitDiffTool())
    registry.register(GitLogTool())
    registry.register(GitAddTool())
    registry.register(GitCommitTool())
    registry.register(ShellExecTool())
    registry.register(SshDetectionTool())
    if provider_registry is not None:
        registry.register(ProviderStatusTool(provider_registry))
        registry.register(ProviderSwitchTool(provider_registry))
    return registry


__all__ = [
    "ToolContext",
    "ToolRegistry",
    "ToolRequest",
    "ToolResult",
    "SendEmailSmtpTool",
    "ReadFileTool",
    "WriteFileTool",
    "GitStatusTool",
    "GitDiffTool",
    "GitLogTool",
    "GitAddTool",
    "GitCommitTool",
    "ShellExecTool",
    "SshDetectionTool",
    "ProviderStatusTool",
    "ProviderSwitchTool",
    "build_default_tool_registry",
]
