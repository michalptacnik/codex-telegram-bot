from __future__ import annotations

from pathlib import Path

from codex_telegram_bot.tools.base import ToolContext, ToolRequest, ToolResult


MAX_READ_BYTES = 50_000
MAX_WRITE_BYTES = 80_000


def _is_trusted_profile(context: ToolContext) -> bool:
    return str(getattr(context, "policy_profile", "") or "").strip().lower() == "trusted"


def _resolve_workspace_path(workspace_root: Path, raw_path: str, allow_full_machine: bool = False) -> Path:
    raw = (raw_path or "").strip()
    candidate = Path(raw).expanduser()
    target = candidate.resolve() if candidate.is_absolute() else (workspace_root / raw).resolve()
    if allow_full_machine:
        return target
    workspace = workspace_root.resolve()
    if not str(target).startswith(str(workspace) + "/") and target != workspace:
        raise ValueError("Path escapes workspace root.")
    return target


class ReadFileTool:
    name = "read_file"

    def run(self, request: ToolRequest, context: ToolContext) -> ToolResult:
        raw_path = str(request.args.get("path") or "").strip()
        if not raw_path:
            return ToolResult(ok=False, output="Error: missing required arg 'path'.")
        try:
            target = _resolve_workspace_path(
                context.workspace_root,
                raw_path,
                allow_full_machine=_is_trusted_profile(context),
            )
        except ValueError as exc:
            return ToolResult(ok=False, output=f"Error: {exc}")
        if not target.exists() or not target.is_file():
            return ToolResult(ok=False, output="Error: file not found.")
        max_bytes = int(request.args.get("max_bytes") or MAX_READ_BYTES)
        max_bytes = max(1, min(max_bytes, MAX_READ_BYTES))
        data = target.read_bytes()[:max_bytes]
        return ToolResult(ok=True, output=data.decode("utf-8", errors="replace"))


class WriteFileTool:
    name = "write_file"

    def run(self, request: ToolRequest, context: ToolContext) -> ToolResult:
        raw_path = str(request.args.get("path") or "").strip()
        content = str(request.args.get("content") or "")
        if not raw_path:
            return ToolResult(ok=False, output="Error: missing required arg 'path'.")
        if len(content.encode("utf-8")) > MAX_WRITE_BYTES:
            return ToolResult(ok=False, output="Error: content exceeds max bytes.")
        try:
            target = _resolve_workspace_path(
                context.workspace_root,
                raw_path,
                allow_full_machine=_is_trusted_profile(context),
            )
        except ValueError as exc:
            return ToolResult(ok=False, output=f"Error: {exc}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        if not target.exists() or not target.is_file():
            return ToolResult(ok=False, output="Error: write failed verification (file missing).")
        try:
            stat = target.stat()
            size = stat.st_size
        except OSError:
            size = -1
        return ToolResult(
            ok=True,
            output=(
                f"Wrote {len(content)} chars to {str(target)}\n"
                f"Verified file exists: {str(target)} (size={size} bytes)"
            ),
        )
