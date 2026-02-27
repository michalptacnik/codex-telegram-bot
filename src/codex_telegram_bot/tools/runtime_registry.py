from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from codex_telegram_bot.tools.base import NATIVE_TOOL_SCHEMAS, Tool, ToolRegistry


@dataclass(frozen=True)
class WorkspaceInvariants:
    repo_root: Path
    cwd: Path
    is_git_repo: bool


@dataclass(frozen=True)
class ToolRegistrySnapshot:
    tools: Dict[str, Tool]
    disabled: Dict[str, str]
    schemas: List[Dict[str, Any]]
    invariants: WorkspaceInvariants

    def names(self) -> List[str]:
        return sorted(self.tools.keys())

    def get(self, name: str) -> Optional[Tool]:
        return self.tools.get((name or "").strip().lower())


def build_runtime_tool_registry(
    registry: ToolRegistry,
    *,
    workspace_root: Path,
    extra_tools: Optional[Dict[str, Tool]] = None,
) -> ToolRegistrySnapshot:
    ws = Path(workspace_root).expanduser().resolve()
    inv = _workspace_invariants(ws)
    disabled: Dict[str, str] = {}
    tools: Dict[str, Tool] = {}

    candidates: Dict[str, Tool] = {}
    for name in registry.names():
        tool = registry.get(name)
        if tool is None:
            continue
        candidates[name] = tool
    for name, tool in (extra_tools or {}).items():
        normalized = str(name or "").strip().lower()
        if normalized:
            candidates[normalized] = tool

    for name in sorted(candidates.keys()):
        if name.startswith("git_") and not inv.is_git_repo:
            disabled[name] = "disabled: workspace is not a git repository"
            continue
        tools[name] = candidates[name]

    schemas: List[Dict[str, Any]] = []
    for name in sorted(tools.keys()):
        schema = NATIVE_TOOL_SCHEMAS.get(name)
        if schema is not None:
            schemas.append(dict(schema))
    return ToolRegistrySnapshot(tools=tools, disabled=disabled, schemas=schemas, invariants=inv)


def _workspace_invariants(workspace_root: Path) -> WorkspaceInvariants:
    repo_root = workspace_root
    is_git_repo = False
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=str(workspace_root),
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if proc.returncode == 0:
            raw = (proc.stdout or "").strip()
            if raw:
                repo_root = Path(raw).expanduser().resolve()
                is_git_repo = True
    except Exception:
        is_git_repo = False
    return WorkspaceInvariants(repo_root=repo_root, cwd=workspace_root, is_git_repo=is_git_repo)
