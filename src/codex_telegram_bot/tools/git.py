from __future__ import annotations

import subprocess
from pathlib import Path

from codex_telegram_bot.tools.base import ToolContext, ToolRequest, ToolResult


MAX_DIFF_BYTES = 40_000
MAX_LOG_ENTRIES = 50


def _run_git(argv: list[str], cwd: str, timeout: int = 15) -> tuple[int, str, str]:
    try:
        result = subprocess.run(
            argv,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            shell=False,
            check=False,
        )
        return result.returncode, (result.stdout or "").strip(), (result.stderr or "").strip()
    except Exception as exc:
        return -1, "", str(exc)


def _git_repo_root(workspace_root: Path) -> tuple[bool, Path, str]:
    ws = workspace_root.expanduser().resolve()
    try:
        probe = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=str(ws),
            capture_output=True,
            text=True,
            timeout=5,
            shell=False,
            check=False,
        )
    except Exception as exc:
        return False, ws, str(exc)
    if probe.returncode != 0:
        err = (probe.stderr or probe.stdout or "").strip()
        return False, ws, err or "not a git repository"
    raw = (probe.stdout or "").strip()
    if not raw:
        return False, ws, "failed to resolve git repo root"
    repo_root = Path(raw).expanduser().resolve()
    return True, repo_root, ""


class GitStatusTool:
    name = "git_status"

    def run(self, request: ToolRequest, context: ToolContext) -> ToolResult:
        ok_repo, repo_root, err_repo = _git_repo_root(context.workspace_root)
        if not ok_repo:
            return ToolResult(ok=False, output=f"Error: git_status unavailable ({err_repo}).")
        short = bool(request.args.get("short", True))
        argv = ["git", "status", "--short"] if short else ["git", "status"]
        rc, out, err = _run_git(argv, str(repo_root))
        if rc != 0:
            return ToolResult(ok=False, output=f"Error: git_status rc={rc} {err}".strip())
        return ToolResult(ok=True, output=out or "Clean working tree.")


class GitDiffTool:
    name = "git_diff"

    def run(self, request: ToolRequest, context: ToolContext) -> ToolResult:
        ok_repo, repo_root, err_repo = _git_repo_root(context.workspace_root)
        if not ok_repo:
            return ToolResult(ok=False, output=f"Error: git_diff unavailable ({err_repo}).")
        staged = bool(request.args.get("staged", False))
        argv = ["git", "diff", "--staged"] if staged else ["git", "diff"]
        rc, out, err = _run_git(argv, str(repo_root))
        if rc != 0:
            return ToolResult(ok=False, output=f"Error: git_diff rc={rc} {err}".strip())
        truncated = out[:MAX_DIFF_BYTES]
        suffix = f"\n[truncated to {MAX_DIFF_BYTES} bytes]" if len(out) > MAX_DIFF_BYTES else ""
        return ToolResult(ok=True, output=(truncated + suffix) or "No changes.")


class GitLogTool:
    name = "git_log"

    def run(self, request: ToolRequest, context: ToolContext) -> ToolResult:
        ok_repo, repo_root, err_repo = _git_repo_root(context.workspace_root)
        if not ok_repo:
            return ToolResult(ok=False, output=f"Error: git_log unavailable ({err_repo}).")
        try:
            n = max(1, min(int(request.args.get("n", 10)), MAX_LOG_ENTRIES))
        except (TypeError, ValueError):
            n = 10
        argv = ["git", "log", f"--max-count={n}", "--oneline", "--no-decorate"]
        rc, out, err = _run_git(argv, str(repo_root))
        if rc != 0:
            return ToolResult(ok=False, output=f"Error: git_log rc={rc} {err}".strip())
        return ToolResult(ok=True, output=out or "No commits yet.")


class GitAddTool:
    name = "git_add"

    def run(self, request: ToolRequest, context: ToolContext) -> ToolResult:
        ok_repo, repo_root, err_repo = _git_repo_root(context.workspace_root)
        if not ok_repo:
            return ToolResult(ok=False, output=f"Error: git_add unavailable ({err_repo}).")
        raw_paths = request.args.get("paths", [])
        if isinstance(raw_paths, str):
            raw_paths = [raw_paths]
        paths = [str(p).strip() for p in raw_paths if str(p).strip()]
        if not paths:
            return ToolResult(ok=False, output="Error: 'paths' arg is required (list of files to stage).")
        # Guard against path traversal
        for p in paths:
            if ".." in p or p.startswith("/"):
                return ToolResult(ok=False, output=f"Error: path '{p}' is not allowed.")
        argv = ["git", "add", "--"] + paths
        rc, out, err = _run_git(argv, str(repo_root))
        if rc != 0:
            return ToolResult(ok=False, output=f"Error: git_add rc={rc} {err}".strip())
        return ToolResult(ok=True, output=out or f"Staged: {', '.join(paths)}")


class GitCommitTool:
    name = "git_commit"

    def run(self, request: ToolRequest, context: ToolContext) -> ToolResult:
        ok_repo, repo_root, err_repo = _git_repo_root(context.workspace_root)
        if not ok_repo:
            return ToolResult(ok=False, output=f"Error: git_commit unavailable ({err_repo}).")
        message = str(request.args.get("message") or "").strip()
        if not message:
            return ToolResult(ok=False, output="Error: 'message' arg is required.")
        if len(message) > 2000:
            return ToolResult(ok=False, output="Error: commit message exceeds 2000 characters.")
        argv = ["git", "commit", "--no-verify", "-m", message]
        rc, out, err = _run_git(argv, str(repo_root), timeout=30)
        if rc != 0:
            combined = (out + "\n" + err).strip()
            return ToolResult(ok=False, output=f"Error: git_commit rc={rc}\n{combined}".strip())
        return ToolResult(ok=True, output=out or "Committed.")
