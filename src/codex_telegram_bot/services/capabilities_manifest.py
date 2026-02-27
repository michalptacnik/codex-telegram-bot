from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Dict, List

from codex_telegram_bot.tools.runtime_registry import ToolRegistrySnapshot


def write_capabilities_manifest(
    *,
    workspace_root: Path,
    snapshot: ToolRegistrySnapshot,
) -> Dict[str, str]:
    ws = Path(workspace_root).expanduser().resolve()
    ws.mkdir(parents=True, exist_ok=True)
    binaries = _binary_presence(["git", "sqlite3", "codex", "python3"])
    tools = snapshot.names()
    disabled = dict(snapshot.disabled)

    payload = {
        "workspace_root": str(ws),
        "repo_root": str(snapshot.invariants.repo_root),
        "cwd": str(snapshot.invariants.cwd),
        "is_git_repo": bool(snapshot.invariants.is_git_repo),
        "tools": tools,
        "disabled_tools": disabled,
        "binaries": binaries,
        "permissions": {
            "workspace_write": True,
            "outside_workspace_write": False,
        },
        "guidance": "If you need a tool not in the list, ask or degrade gracefully.",
    }

    json_path = ws / "AGENTS_INDEX.json"
    md_path = ws / "CAPABILITIES.md"
    json_path.write_text(json.dumps(payload, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    md_path.write_text(_markdown_manifest(payload), encoding="utf-8")
    return {"json_path": str(json_path), "markdown_path": str(md_path)}


def build_system_capabilities_chunk(snapshot: ToolRegistrySnapshot) -> str:
    tools = ", ".join(snapshot.names()) or "(none)"
    disabled_bits = [f"{k}: {v}" for k, v in sorted(snapshot.disabled.items())]
    disabled = "; ".join(disabled_bits) if disabled_bits else "none"
    binaries = _binary_presence(["git", "sqlite3", "codex", "python3"])
    return (
        "Runtime capabilities manifest:\n"
        f"- Workspace root: {snapshot.invariants.cwd}\n"
        f"- Repo root: {snapshot.invariants.repo_root}\n"
        f"- Git repo: {'yes' if snapshot.invariants.is_git_repo else 'no'}\n"
        f"- Allowed tools: {tools}\n"
        f"- Disabled tools: {disabled}\n"
        f"- Binaries: {json.dumps(binaries, ensure_ascii=True)}\n"
        "- If you need a tool not in the list, ask or degrade gracefully.\n"
    )


def _binary_presence(names: List[str]) -> Dict[str, bool]:
    present: Dict[str, bool] = {}
    for name in names:
        present[name] = bool(shutil.which(name))
    return present


def _markdown_manifest(payload: Dict[str, object]) -> str:
    tools = payload.get("tools") if isinstance(payload.get("tools"), list) else []
    disabled = payload.get("disabled_tools") if isinstance(payload.get("disabled_tools"), dict) else {}
    binaries = payload.get("binaries") if isinstance(payload.get("binaries"), dict) else {}
    lines = [
        "# Capabilities Manifest",
        "",
        f"- workspace_root: `{payload.get('workspace_root', '')}`",
        f"- repo_root: `{payload.get('repo_root', '')}`",
        f"- cwd: `{payload.get('cwd', '')}`",
        f"- is_git_repo: `{payload.get('is_git_repo', False)}`",
        "",
        "## Allowed Tools",
    ]
    if tools:
        for name in tools:
            lines.append(f"- `{name}`")
    else:
        lines.append("- (none)")
    lines.append("")
    lines.append("## Disabled Tools")
    if disabled:
        for name, reason in sorted(disabled.items()):
            lines.append(f"- `{name}`: {reason}")
    else:
        lines.append("- (none)")
    lines.append("")
    lines.append("## Binary Presence")
    for name, state in sorted(binaries.items()):
        lines.append(f"- `{name}`: {'present' if state else 'missing'}")
    lines.append("")
    lines.append("If you need a tool not in the list, ask or degrade gracefully.")
    lines.append("")
    return "\n".join(lines)

