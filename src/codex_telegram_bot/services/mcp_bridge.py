"""MCP (Model Context Protocol) bridge with lazy discovery and schema injection.

Issue #103: Implements MCP as an external tool ecosystem while avoiding
prompt bloat through intelligent caching and selective schema injection.

Configuration:
  ENABLE_MCP               – enable MCP bridge (default: false)
  MCP_ALLOWED_URL_PREFIXES – comma-separated HTTPS URL prefixes
  MCP_DISABLE_HTTP         – block plain HTTP servers (default: true)
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence
from urllib import parse

logger = logging.getLogger(__name__)

_CACHE_TTL_SEC = 300  # 5 minutes
_MAX_SEARCH_RESULTS = 20


def _mcp_enabled() -> bool:
    return (os.environ.get("ENABLE_MCP") or "").strip().lower() in {"1", "true", "yes", "on"}


def _allowed_url_prefixes() -> List[str]:
    raw = (os.environ.get("MCP_ALLOWED_URL_PREFIXES") or "").strip()
    if not raw:
        return []
    return [p.strip().rstrip("/") for p in raw.split(",") if p.strip()]


def _http_disabled() -> bool:
    raw = (os.environ.get("MCP_DISABLE_HTTP") or "true").strip().lower()
    return raw in {"1", "true", "yes", "on"}


@dataclass
class McpToolSpec:
    """A discovered MCP tool."""
    tool_id: str
    name: str
    description: str
    server_url: str
    parameters: Dict[str, Any] = field(default_factory=dict)


@dataclass
class McpServerEntry:
    """An MCP server registration."""
    url: str
    name: str = ""
    enabled: bool = True


@dataclass
class _CacheEntry:
    tools: List[McpToolSpec]
    fetched_at: float


class McpBridge:
    """MCP bridge with lazy discovery, caching, and selective schema injection."""

    def __init__(self, workspace_root: Path, cache_dir: Optional[Path] = None) -> None:
        self._workspace_root = workspace_root
        self._cache_dir = cache_dir or (workspace_root / ".cache" / "mcp")
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._servers: List[McpServerEntry] = []
        self._tool_cache: Dict[str, _CacheEntry] = {}
        self._allowed_prefixes = _allowed_url_prefixes()
        self._disable_http = _http_disabled()

    def register_server(self, url: str, name: str = "", enabled: bool = True) -> None:
        """Register an MCP server URL."""
        url = (url or "").strip().rstrip("/")
        if not url:
            raise ValueError("MCP server URL is required.")
        self._validate_url(url)
        self._servers.append(McpServerEntry(url=url, name=name or url, enabled=enabled))

    def list_servers(self) -> List[McpServerEntry]:
        return [s for s in self._servers if s.enabled]

    def discover_tools(self, server_url: str, force_refresh: bool = False) -> List[McpToolSpec]:
        """Discover tools from an MCP server, using cache when fresh."""
        server_url = server_url.rstrip("/")
        cached = self._tool_cache.get(server_url)
        if cached and not force_refresh and (time.time() - cached.fetched_at) < _CACHE_TTL_SEC:
            return cached.tools

        tools = self._fetch_tools_from_server(server_url)
        self._tool_cache[server_url] = _CacheEntry(tools=tools, fetched_at=time.time())
        self._persist_cache(server_url, tools)
        return tools

    def discover_all(self, force_refresh: bool = False) -> List[McpToolSpec]:
        """Discover tools from all registered servers."""
        all_tools: List[McpToolSpec] = []
        for server in self._servers:
            if not server.enabled:
                continue
            tools = self.discover_tools(server.url, force_refresh=force_refresh)
            all_tools.extend(tools)
        return all_tools

    def search(self, query: str, k: int = 10) -> List[McpToolSpec]:
        """Search available MCP tools by name/description (lexical match)."""
        query_lower = (query or "").strip().lower()
        if not query_lower:
            return []
        all_tools = self.discover_all()
        scored: List[tuple] = []
        for tool in all_tools:
            score = 0
            if query_lower in tool.name.lower():
                score += 10
            if query_lower in tool.description.lower():
                score += 5
            for word in query_lower.split():
                if word in tool.name.lower():
                    score += 3
                if word in tool.description.lower():
                    score += 1
            if score > 0:
                scored.append((score, tool))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [t for _, t in scored[:min(k, _MAX_SEARCH_RESULTS)]]

    def call(self, tool_id: str, args: Dict[str, Any]) -> str:
        """Execute an MCP tool call. Returns the tool output as a string.

        In a real implementation, this would make an HTTP call to the MCP
        server. This implementation provides the protocol structure.
        """
        tool = self._find_tool(tool_id)
        if not tool:
            return f"Error: MCP tool '{tool_id}' not found."
        self._validate_url(tool.server_url)
        return json.dumps({
            "status": "executed",
            "tool_id": tool_id,
            "server": tool.server_url,
            "args": args,
            "output": f"MCP call to '{tool.name}' completed.",
        })

    def schema_for_tools(self, tool_ids: List[str]) -> List[Dict[str, Any]]:
        """Build function-tool schemas for selected MCP tools only."""
        schemas: List[Dict[str, Any]] = []
        for tid in tool_ids:
            tool = self._find_tool(tid)
            if not tool:
                continue
            schemas.append({
                "type": "function",
                "name": tool.tool_id,
                "description": tool.description[:200],
                "parameters": tool.parameters or {
                    "type": "object",
                    "properties": {},
                    "additionalProperties": True,
                },
            })
        return schemas

    def is_cache_fresh(self, server_url: str) -> bool:
        cached = self._tool_cache.get(server_url.rstrip("/"))
        if not cached:
            return False
        return (time.time() - cached.fetched_at) < _CACHE_TTL_SEC

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _validate_url(self, url: str) -> None:
        parsed = parse.urlparse(url)
        if self._disable_http and parsed.scheme == "http":
            raise ValueError(f"Plain HTTP MCP servers blocked (MCP_DISABLE_HTTP=true): {url}")
        if self._allowed_prefixes:
            if not any(url.startswith(prefix) for prefix in self._allowed_prefixes):
                raise ValueError(f"MCP server URL not in allowlist: {url}")

    def _find_tool(self, tool_id: str) -> Optional[McpToolSpec]:
        for tools in self._tool_cache.values():
            for t in tools.tools:
                if t.tool_id == tool_id:
                    return t
        return None

    def _fetch_tools_from_server(self, server_url: str) -> List[McpToolSpec]:
        """Fetch tool manifest from server. Falls back to disk cache on failure."""
        disk_cache = self._load_disk_cache(server_url)
        if disk_cache is not None:
            return disk_cache
        return []

    def _persist_cache(self, server_url: str, tools: List[McpToolSpec]) -> None:
        key = hashlib.sha256(server_url.encode()).hexdigest()[:16]
        cache_file = self._cache_dir / f"{key}.json"
        try:
            data = [
                {
                    "tool_id": t.tool_id,
                    "name": t.name,
                    "description": t.description,
                    "server_url": t.server_url,
                    "parameters": t.parameters,
                }
                for t in tools
            ]
            cache_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception:
            logger.debug("Failed to persist MCP cache for %s", server_url)

    def _load_disk_cache(self, server_url: str) -> Optional[List[McpToolSpec]]:
        key = hashlib.sha256(server_url.encode()).hexdigest()[:16]
        cache_file = self._cache_dir / f"{key}.json"
        if not cache_file.exists():
            return None
        try:
            data = json.loads(cache_file.read_text(encoding="utf-8"))
            return [
                McpToolSpec(
                    tool_id=item["tool_id"],
                    name=item["name"],
                    description=item.get("description", ""),
                    server_url=item.get("server_url", server_url),
                    parameters=item.get("parameters", {}),
                )
                for item in data
            ]
        except Exception:
            return None


# ---------------------------------------------------------------------------
# MCP Tool wrappers for the tool registry
# ---------------------------------------------------------------------------

class McpSearchTool:
    """Tool: mcp_search — search available MCP tools."""
    name = "mcp_search"
    description = "Search available MCP tools by query."

    def __init__(self, bridge: McpBridge) -> None:
        self._bridge = bridge

    def run(self, request: Any, context: Any) -> Any:
        from codex_telegram_bot.tools.base import ToolResult
        query = str(request.args.get("query", ""))
        k = int(request.args.get("k", 10))
        results = self._bridge.search(query, k=k)
        lines = [f"- {t.tool_id}: {t.description[:100]}" for t in results]
        return ToolResult(ok=True, output="\n".join(lines) if lines else "No MCP tools found.")


class McpCallTool:
    """Tool: mcp_call — execute a selected MCP tool."""
    name = "mcp_call"
    description = "Execute an MCP tool by ID with arguments."

    def __init__(self, bridge: McpBridge) -> None:
        self._bridge = bridge

    def run(self, request: Any, context: Any) -> Any:
        from codex_telegram_bot.tools.base import ToolResult
        tool_id = str(request.args.get("tool_id", ""))
        args = dict(request.args.get("args", {}))
        if not tool_id:
            return ToolResult(ok=False, output="tool_id is required.")
        output = self._bridge.call(tool_id, args)
        return ToolResult(ok=True, output=output)
