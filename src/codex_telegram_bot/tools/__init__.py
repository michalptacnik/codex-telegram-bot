from codex_telegram_bot.tools.base import ToolContext, ToolRegistry, ToolRequest, ToolResult
from codex_telegram_bot.tools.files import ReadFileTool, WriteFileTool
from codex_telegram_bot.tools.git import GitStatusTool


def build_default_tool_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(ReadFileTool())
    registry.register(WriteFileTool())
    registry.register(GitStatusTool())
    return registry


__all__ = [
    "ToolContext",
    "ToolRegistry",
    "ToolRequest",
    "ToolResult",
    "ReadFileTool",
    "WriteFileTool",
    "GitStatusTool",
    "build_default_tool_registry",
]
