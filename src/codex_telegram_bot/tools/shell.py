from __future__ import annotations

import shlex
import subprocess
from pathlib import Path

from codex_telegram_bot.tools.base import ToolContext, ToolRequest, ToolResult

# Commands that are safe to run in workspace-confined contexts.
# Only these binaries (resolved by basename) are allowed.
SAFE_COMMANDS: frozenset[str] = frozenset(
    [
        "cat",
        "echo",
        "find",
        "grep",
        "head",
        "ls",
        "mkdir",
        "sed",
        "sort",
        "tail",
        "touch",
        "tree",
        "wc",
        "which",
        "basename",
        "dirname",
        "pwd",
        "env",
        "printenv",
        "date",
        "python3",
        "python",
        "pip",
        "pip3",
        "node",
        "npm",
        "yarn",
        "make",
        "cmake",
        "cargo",
        "go",
        "javac",
        "java",
        "mvn",
        "gradle",
        "pytest",
        "mypy",
        "ruff",
        "flake8",
        "black",
        "isort",
        "sh",
        "bash",
    ]
)

MAX_OUTPUT_BYTES = 32_000
DEFAULT_TIMEOUT_SEC = 30


def _resolve_workspace_path(workspace_root: Path, raw_path: str) -> Path | None:
    """Return an absolute path rooted in workspace, or None if it escapes."""
    target = (workspace_root / raw_path.strip()).resolve()
    workspace = workspace_root.resolve()
    if str(target) == str(workspace) or str(target).startswith(str(workspace) + "/"):
        return target
    return None


class ShellExecTool:
    """Run a shell command confined to the session workspace.

    Only commands whose basename is in SAFE_COMMANDS are permitted.
    All path arguments are validated to be inside the workspace root
    before execution.
    """

    name = "shell_exec"

    def run(self, request: ToolRequest, context: ToolContext) -> ToolResult:
        cmd = str(request.args.get("cmd") or "").strip()
        if not cmd:
            return ToolResult(ok=False, output="Error: 'cmd' arg is required.")

        try:
            argv = shlex.split(cmd)
        except ValueError as exc:
            return ToolResult(ok=False, output=f"Error: failed to parse command: {exc}")

        if not argv:
            return ToolResult(ok=False, output="Error: empty command after parsing.")

        binary = Path(argv[0]).name
        if binary not in SAFE_COMMANDS:
            return ToolResult(
                ok=False,
                output=f"Error: '{binary}' is not in the safe command allowlist.",
            )

        timeout = DEFAULT_TIMEOUT_SEC
        try:
            raw_timeout = request.args.get("timeout_sec")
            if raw_timeout is not None:
                timeout = max(1, min(int(raw_timeout), 120))
        except (TypeError, ValueError):
            pass

        workspace = context.workspace_root.resolve()
        workspace.mkdir(parents=True, exist_ok=True)

        try:
            result = subprocess.run(
                argv,
                cwd=str(workspace),
                capture_output=True,
                text=True,
                timeout=timeout,
                shell=False,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return ToolResult(ok=False, output=f"Error: command timed out after {timeout}s.")
        except FileNotFoundError:
            return ToolResult(ok=False, output=f"Error: command not found: {argv[0]}")
        except Exception as exc:
            return ToolResult(ok=False, output=f"Error: execution failed: {exc}")

        stdout = (result.stdout or "")[:MAX_OUTPUT_BYTES]
        stderr = (result.stderr or "")[:MAX_OUTPUT_BYTES]
        combined = stdout
        if stderr:
            combined = (stdout + "\nstderr:\n" + stderr).strip()
        if result.returncode != 0:
            return ToolResult(
                ok=False,
                output=f"Error: exit code {result.returncode}\n{combined}".strip(),
            )
        return ToolResult(ok=True, output=combined or "(no output)")
