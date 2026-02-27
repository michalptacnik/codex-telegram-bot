from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Protocol


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
