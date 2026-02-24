from __future__ import annotations

import os
import subprocess
from pathlib import Path

from codex_telegram_bot.tools.base import ToolContext, ToolRequest, ToolResult

_DEFAULT_KEY_NAMES = ("id_ed25519", "id_rsa", "id_ecdsa", "id_dsa")


def _ssh_agent_keys() -> list[str]:
    """Return public key fingerprints from the running SSH agent, if any."""
    agent_sock = os.environ.get("SSH_AUTH_SOCK", "")
    if not agent_sock:
        return []
    try:
        result = subprocess.run(
            ["ssh-add", "-l"],
            capture_output=True,
            text=True,
            timeout=5,
            shell=False,
            check=False,
        )
        if result.returncode == 0:
            lines = [ln.strip() for ln in result.stdout.splitlines() if ln.strip()]
            return lines
    except Exception:
        pass
    return []


def _local_key_files() -> list[str]:
    """Return paths of SSH key files found in ~/.ssh."""
    ssh_dir = Path.home() / ".ssh"
    found: list[str] = []
    if not ssh_dir.is_dir():
        return found
    for name in _DEFAULT_KEY_NAMES:
        priv = ssh_dir / name
        pub = ssh_dir / f"{name}.pub"
        if priv.exists():
            found.append(str(priv))
        if pub.exists():
            found.append(str(pub))
    return found


def _git_remote_uses_ssh(cwd: str) -> bool:
    """Return True if the git remote 'origin' URL uses SSH (git@ or ssh://)."""
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=5,
            shell=False,
            check=False,
        )
        if result.returncode == 0:
            url = result.stdout.strip()
            return url.startswith("git@") or url.startswith("ssh://")
    except Exception:
        pass
    return False


class SshDetectionTool:
    """Detect SSH agent keys and local key files for git authentication."""

    name = "ssh_detect"

    def run(self, request: ToolRequest, context: ToolContext) -> ToolResult:
        report: list[str] = []

        agent_keys = _ssh_agent_keys()
        if agent_keys:
            report.append(f"SSH agent: {len(agent_keys)} key(s) loaded")
            for k in agent_keys:
                report.append(f"  {k}")
        else:
            sock = os.environ.get("SSH_AUTH_SOCK", "")
            if sock:
                report.append("SSH agent: socket present but no keys loaded (or agent is locked)")
            else:
                report.append("SSH agent: not running (SSH_AUTH_SOCK not set)")

        key_files = _local_key_files()
        if key_files:
            report.append(f"Local key files ({len(key_files)}):")
            for f in key_files:
                report.append(f"  {f}")
        else:
            report.append("Local key files: none found in ~/.ssh")

        uses_ssh = _git_remote_uses_ssh(str(context.workspace_root))
        report.append(f"Git remote origin: {'SSH' if uses_ssh else 'non-SSH (HTTP/HTTPS or not configured)'}")

        return ToolResult(ok=True, output="\n".join(report))
