from __future__ import annotations

import json
from typing import Any

from codex_telegram_bot.services.soul import SoulStore
from codex_telegram_bot.tools.base import ToolContext, ToolRequest, ToolResult


class SoulGetTool:
    name = "soul_get"
    description = "Read SOUL.md identity kernel with validation report."

    def run(self, request: ToolRequest, context: ToolContext) -> ToolResult:
        store = SoulStore(workspace_root=context.workspace_root)
        profile, report = store.load_profile_with_report()
        payload = {
            "ok": report.ok,
            "warnings": list(report.warnings),
            "text": store.read_text(),
            "name": profile.name,
            "voice": profile.voice,
            "style": {
                "emoji": profile.style.emoji,
                "emphasis": profile.style.emphasis,
                "brevity": profile.style.brevity,
            },
        }
        return ToolResult(ok=True, output=json.dumps(payload, ensure_ascii=True))


class SoulProposePatchTool:
    name = "soul_propose_patch"
    description = "Validate a structured SOUL patch and return unified diff preview."

    def run(self, request: ToolRequest, context: ToolContext) -> ToolResult:
        patch = request.args.get("patch")
        if not isinstance(patch, dict):
            return ToolResult(ok=False, output="patch must be an object.")
        store = SoulStore(workspace_root=context.workspace_root)
        try:
            proposal = store.propose_patch(patch)
        except Exception as exc:
            return ToolResult(ok=False, output=f"Invalid SOUL patch: {exc}")
        return ToolResult(ok=True, output=json.dumps(proposal, ensure_ascii=True))


class SoulApplyPatchTool:
    name = "soul_apply_patch"
    description = "Apply a structured SOUL patch and write a version snapshot."

    def __init__(self, run_store: Any = None) -> None:
        self._store = run_store

    def run(self, request: ToolRequest, context: ToolContext) -> ToolResult:
        patch = request.args.get("patch")
        reason = str(request.args.get("reason") or "").strip()
        if not isinstance(patch, dict):
            return ToolResult(ok=False, output="patch must be an object.")
        if not reason:
            return ToolResult(ok=False, output="reason is required.")
        store = SoulStore(workspace_root=context.workspace_root)
        try:
            result = store.apply_patch(
                patch,
                reason=reason,
                changed_by=str(getattr(context, "user_id", 0) or ""),
                session_id=str(getattr(context, "session_id", "") or ""),
                run_store=self._store,
            )
        except Exception as exc:
            return ToolResult(ok=False, output=f"Failed to apply SOUL patch: {exc}")
        return ToolResult(ok=True, output=json.dumps(result, ensure_ascii=True))
