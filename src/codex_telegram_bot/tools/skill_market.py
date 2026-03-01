from __future__ import annotations

import json
from typing import Any

from codex_telegram_bot.tools.base import ToolContext, ToolRequest, ToolResult


class SkillsMarketSearchTool:
    name = "skills_market_search"
    description = "Search marketplace catalog entries for installable skills."

    def __init__(self, marketplace: Any = None) -> None:
        self._marketplace = marketplace

    def run(self, request: ToolRequest, context: ToolContext) -> ToolResult:
        if self._marketplace is None:
            return ToolResult(ok=False, output="Skill marketplace is not configured.")
        query = str(request.args.get("query") or "").strip()
        source = str(request.args.get("source") or "").strip()
        refresh = bool(request.args.get("refresh", False))
        rows = self._marketplace.search(query=query, source=source, refresh=refresh, limit=50)
        return ToolResult(ok=True, output=json.dumps({"items": rows}, ensure_ascii=True))


class SkillsMarketInstallTool:
    name = "skills_market_install"
    description = "Install an instruction-only marketplace skill to workspace/global packs."

    def __init__(self, marketplace: Any = None) -> None:
        self._marketplace = marketplace

    def run(self, request: ToolRequest, context: ToolContext) -> ToolResult:
        if self._marketplace is None:
            return ToolResult(ok=False, output="Skill marketplace is not configured.")
        skill_ref = str(
            request.args.get("skill_id")
            or request.args.get("install_ref")
            or ""
        ).strip()
        if not skill_ref:
            return ToolResult(ok=False, output="skill_id or install_ref is required.")
        target = str(request.args.get("target") or "workspace").strip().lower()
        try:
            installed = self._marketplace.install(skill_ref=skill_ref, target=target)
        except Exception as exc:
            return ToolResult(ok=False, output=f"Install failed: {exc}")
        return ToolResult(ok=True, output=json.dumps(installed, ensure_ascii=True))


class SkillsMarketEnableTool:
    name = "skills_market_enable"
    description = "Enable an installed marketplace skill after hash verification."

    def __init__(self, marketplace: Any = None) -> None:
        self._marketplace = marketplace

    def run(self, request: ToolRequest, context: ToolContext) -> ToolResult:
        if self._marketplace is None:
            return ToolResult(ok=False, output="Skill marketplace is not configured.")
        skill_id = str(request.args.get("name") or request.args.get("skill_id") or "").strip()
        if not skill_id:
            return ToolResult(ok=False, output="name is required.")
        try:
            payload = self._marketplace.enable(skill_id=skill_id)
        except Exception as exc:
            return ToolResult(ok=False, output=f"Enable failed: {exc}")
        return ToolResult(ok=True, output=json.dumps(payload, ensure_ascii=True))


class SkillsMarketDisableTool:
    name = "skills_market_disable"
    description = "Disable an installed marketplace skill."

    def __init__(self, marketplace: Any = None) -> None:
        self._marketplace = marketplace

    def run(self, request: ToolRequest, context: ToolContext) -> ToolResult:
        if self._marketplace is None:
            return ToolResult(ok=False, output="Skill marketplace is not configured.")
        skill_id = str(request.args.get("name") or request.args.get("skill_id") or "").strip()
        if not skill_id:
            return ToolResult(ok=False, output="name is required.")
        try:
            payload = self._marketplace.disable(skill_id=skill_id)
        except Exception as exc:
            return ToolResult(ok=False, output=f"Disable failed: {exc}")
        return ToolResult(ok=True, output=json.dumps(payload, ensure_ascii=True))


class SkillsMarketRemoveTool:
    name = "skills_market_remove"
    description = "Remove installed marketplace skill files from local packs."

    def __init__(self, marketplace: Any = None) -> None:
        self._marketplace = marketplace

    def run(self, request: ToolRequest, context: ToolContext) -> ToolResult:
        if self._marketplace is None:
            return ToolResult(ok=False, output="Skill marketplace is not configured.")
        skill_id = str(request.args.get("name") or request.args.get("skill_id") or "").strip()
        if not skill_id:
            return ToolResult(ok=False, output="name is required.")
        try:
            payload = self._marketplace.remove(skill_id=skill_id)
        except Exception as exc:
            return ToolResult(ok=False, output=f"Remove failed: {exc}")
        return ToolResult(ok=True, output=json.dumps(payload, ensure_ascii=True))


class SkillsMarketSourcesListTool:
    name = "skills_market_sources_list"
    description = "List configured marketplace catalog sources."

    def __init__(self, marketplace: Any = None) -> None:
        self._marketplace = marketplace

    def run(self, request: ToolRequest, context: ToolContext) -> ToolResult:
        if self._marketplace is None:
            return ToolResult(ok=False, output="Skill marketplace is not configured.")
        return ToolResult(ok=True, output=json.dumps({"sources": self._marketplace.sources_list()}, ensure_ascii=True))
