from __future__ import annotations

import subprocess

from codex_telegram_bot.tools.base import ToolContext, ToolRequest, ToolResult


class GitStatusTool:
    name = "git_status"

    def run(self, request: ToolRequest, context: ToolContext) -> ToolResult:
        short = bool(request.args.get("short", True))
        argv = ["git", "status", "--short"] if short else ["git", "status"]
        try:
            result = subprocess.run(
                argv,
                cwd=str(context.workspace_root),
                capture_output=True,
                text=True,
                timeout=10,
                shell=False,
                check=False,
            )
        except Exception as exc:
            return ToolResult(ok=False, output=f"Error: git_status failed: {exc}")
        out = (result.stdout or "").strip()
        err = (result.stderr or "").strip()
        if result.returncode != 0:
            return ToolResult(ok=False, output=f"Error: git_status rc={result.returncode} {err}".strip())
        return ToolResult(ok=True, output=out or "Clean working tree.")
