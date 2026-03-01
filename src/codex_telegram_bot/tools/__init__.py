import os

from codex_telegram_bot.tools.base import ToolContext, ToolRegistry, ToolRequest, ToolResult
from codex_telegram_bot.tools.email import (
    SendEmailSmtpTool,
    SendEmailTool,
    email_tool_enabled,
    is_email_tool_enabled,
)
from codex_telegram_bot.tools.files import ReadFileTool, WriteFileTool
from codex_telegram_bot.tools.file_transfer import SendFileTool
from codex_telegram_bot.tools.git import (
    GitAddTool,
    GitCommitTool,
    GitDiffTool,
    GitLogTool,
    GitStatusTool,
)
from codex_telegram_bot.tools.heartbeat import (
    HeartbeatGetTool,
    HeartbeatRunOnceTool,
    HeartbeatUpdateTool,
)
from codex_telegram_bot.tools.memory import (
    MemoryAppendDailyTool,
    MemoryGetTool,
    MemoryIndexGetTool,
    MemoryIndexUpdateTool,
    MemoryPageListTool,
    MemoryPointerOpenTool,
    MemorySearchTool,
)
from codex_telegram_bot.tools.message import SendMessageTool
from codex_telegram_bot.tools.provider import ProviderStatusTool, ProviderSwitchTool
from codex_telegram_bot.tools.schedule import ScheduleTaskTool, ListSchedulesTool, CancelScheduleTool
from codex_telegram_bot.tools.soul import SoulApplyPatchTool, SoulGetTool, SoulProposePatchTool
from codex_telegram_bot.tools.skill_market import (
    SkillsMarketDisableTool,
    SkillsMarketEnableTool,
    SkillsMarketInstallTool,
    SkillsMarketRemoveTool,
    SkillsMarketSearchTool,
    SkillsMarketSourcesListTool,
)
from codex_telegram_bot.tools.sessions import (
    SessionsListTool,
    SessionsHistoryTool,
    SessionsSendTool,
    SessionsSpawnTool,
    SessionStatusTool,
)
from codex_telegram_bot.tools.shell import ShellExecTool
from codex_telegram_bot.tools.ssh import SshDetectionTool
from codex_telegram_bot.tools.tasks import TaskCreateTool, TaskDoneTool, TaskListTool
from codex_telegram_bot.tools.web import WebSearchTool, WebFetchTool, web_search_tool_enabled


def build_default_tool_registry(
    provider_registry=None,
    run_store=None,
    mcp_bridge=None,
    process_manager=None,
    access_controller=None,
    proactive_messenger=None,
    skill_marketplace=None,
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
        registry.register(WebFetchTool())
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
        registry.register(
            SendMessageTool(
                run_store=run_store,
                access_controller=access_controller,
                messenger=proactive_messenger,
            )
        )
        registry.register(
            SendFileTool(
                run_store=run_store,
                access_controller=access_controller,
                messenger=proactive_messenger,
            )
        )
        registry.register(ScheduleTaskTool(run_store=run_store, access_controller=access_controller))
        registry.register(ListSchedulesTool(run_store=run_store, access_controller=access_controller))
        registry.register(CancelScheduleTool(run_store=run_store, access_controller=access_controller))
        registry.register(HeartbeatGetTool())
        registry.register(HeartbeatUpdateTool())
        registry.register(
            HeartbeatRunOnceTool(
                run_store=run_store,
                access_controller=access_controller,
                messenger=proactive_messenger,
            )
        )
    # Memory tools (Issue #106)
    registry.register(MemoryGetTool())
    registry.register(MemorySearchTool())
    registry.register(MemoryIndexGetTool())
    registry.register(MemoryPageListTool())
    registry.register(MemoryPointerOpenTool())
    registry.register(MemoryAppendDailyTool())
    registry.register(MemoryIndexUpdateTool())
    registry.register(TaskCreateTool())
    registry.register(TaskListTool())
    registry.register(TaskDoneTool())
    registry.register(SoulGetTool())
    registry.register(SoulProposePatchTool())
    registry.register(SoulApplyPatchTool(run_store=run_store))
    if skill_marketplace is not None:
        registry.register(SkillsMarketSourcesListTool(skill_marketplace))
        registry.register(SkillsMarketSearchTool(skill_marketplace))
        registry.register(SkillsMarketInstallTool(skill_marketplace))
        registry.register(SkillsMarketEnableTool(skill_marketplace))
        registry.register(SkillsMarketDisableTool(skill_marketplace))
        registry.register(SkillsMarketRemoveTool(skill_marketplace))
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
    "WebFetchTool",
    "web_search_tool_enabled",
    "ProviderStatusTool",
    "ProviderSwitchTool",
    "ScheduleTaskTool",
    "ListSchedulesTool",
    "CancelScheduleTool",
    "HeartbeatGetTool",
    "HeartbeatUpdateTool",
    "HeartbeatRunOnceTool",
    "MemoryGetTool",
    "MemorySearchTool",
    "MemoryIndexGetTool",
    "MemoryPageListTool",
    "MemoryPointerOpenTool",
    "MemoryAppendDailyTool",
    "MemoryIndexUpdateTool",
    "TaskCreateTool",
    "TaskListTool",
    "TaskDoneTool",
    "SoulGetTool",
    "SoulProposePatchTool",
    "SoulApplyPatchTool",
    "SkillsMarketSourcesListTool",
    "SkillsMarketSearchTool",
    "SkillsMarketInstallTool",
    "SkillsMarketEnableTool",
    "SkillsMarketDisableTool",
    "SkillsMarketRemoveTool",
    "SendMessageTool",
    "SendFileTool",
    "SessionsListTool",
    "SessionsHistoryTool",
    "SessionsSendTool",
    "SessionsSpawnTool",
    "SessionStatusTool",
    "build_default_tool_registry",
]
