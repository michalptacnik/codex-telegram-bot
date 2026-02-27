"""Markdown-first memory tools (Issue #106).

Implements OpenClaw-style memory semantics using markdown files and retrieval:

  memory/YYYY-MM-DD.md  — daily logs
  MEMORY.md             — optional curated memory

Tools:
  memory_get(path, startLine, endLine)   — retrieve specific sections
  memory_search(query, k)                — lexical search with result limit
"""
from __future__ import annotations

import os
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from codex_telegram_bot.tools.base import ToolContext, ToolRequest, ToolResult

_PRELOAD_BUDGET_CHARS = 8000
_MAX_SEARCH_RESULTS = 20


class MemoryStore:
    """File-backed markdown memory with daily logs and curated memory."""

    def __init__(self, workspace_root: Path) -> None:
        self._workspace = workspace_root
        self._memory_dir = workspace_root / "memory"
        self._index_dir = workspace_root / ".cache" / "memory"
        self._memory_dir.mkdir(parents=True, exist_ok=True)
        self._index_dir.mkdir(parents=True, exist_ok=True)

    @property
    def memory_dir(self) -> Path:
        return self._memory_dir

    def daily_log_path(self, d: Optional[date] = None) -> Path:
        d = d or date.today()
        return self._memory_dir / f"{d.isoformat()}.md"

    def curated_path(self) -> Path:
        return self._workspace / "MEMORY.md"

    def read_file(self, path: str, start_line: int = 0, end_line: int = 0) -> str:
        """Read a memory file, returning empty string for missing files."""
        resolved = self._resolve_path(path)
        if not resolved or not resolved.exists():
            return ""
        try:
            lines = resolved.read_text(encoding="utf-8").splitlines()
        except Exception:
            return ""
        if start_line or end_line:
            start = max(0, start_line - 1) if start_line else 0
            end = end_line if end_line else len(lines)
            lines = lines[start:end]
        return "\n".join(lines)

    def write_daily(self, content: str, d: Optional[date] = None) -> Path:
        """Append content to today's daily log."""
        path = self.daily_log_path(d)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(content.rstrip("\n") + "\n")
        return path

    def preload(self, budget_chars: int = _PRELOAD_BUDGET_CHARS) -> str:
        """Bootstrap: load today and yesterday only, within budget."""
        today = date.today()
        yesterday = today - timedelta(days=1)
        parts: List[str] = []
        used = 0

        for d in [today, yesterday]:
            path = self.daily_log_path(d)
            if path.exists():
                try:
                    text = path.read_text(encoding="utf-8")
                    if used + len(text) <= budget_chars:
                        parts.append(f"## {d.isoformat()}\n{text}")
                        used += len(text)
                except Exception:
                    pass

        curated = self.curated_path()
        if curated.exists():
            try:
                text = curated.read_text(encoding="utf-8")
                if used + len(text) <= budget_chars:
                    parts.append(f"## MEMORY.md\n{text}")
            except Exception:
                pass

        return "\n\n".join(parts)

    def search(self, query: str, k: int = 10) -> List[Dict[str, Any]]:
        """Lexical search across all memory files."""
        query_lower = (query or "").strip().lower()
        if not query_lower:
            return []
        results: List[Dict[str, Any]] = []
        query_words = query_lower.split()

        files = list(self._memory_dir.glob("*.md"))
        curated = self.curated_path()
        if curated.exists():
            files.append(curated)

        for f in files:
            try:
                text = f.read_text(encoding="utf-8")
            except Exception:
                continue
            lines = text.splitlines()
            for i, line in enumerate(lines):
                line_lower = line.lower()
                score = 0
                for word in query_words:
                    if word in line_lower:
                        score += 1
                if score > 0:
                    results.append({
                        "file": str(f.relative_to(self._workspace)),
                        "line": i + 1,
                        "content": line[:200],
                        "score": score,
                    })

        results.sort(key=lambda x: x["score"], reverse=True)
        return results[:min(k, _MAX_SEARCH_RESULTS)]

    def _resolve_path(self, path: str) -> Optional[Path]:
        """Resolve a path relative to workspace, safe against escapes."""
        p = Path(path)
        if p.is_absolute():
            resolved = p
        else:
            resolved = (self._workspace / p).resolve()
        ws_resolved = self._workspace.resolve()
        try:
            resolved.relative_to(ws_resolved)
        except ValueError:
            return None
        return resolved


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

class MemoryGetTool:
    """Tool: memory_get — retrieve specific sections from memory files."""
    name = "memory_get"
    description = "Retrieve content from a memory file by path and optional line range."

    def __init__(self, store: Optional[MemoryStore] = None) -> None:
        self._store = store

    def run(self, request: ToolRequest, context: ToolContext) -> ToolResult:
        store = self._store or MemoryStore(context.workspace_root)
        path = str(request.args.get("path", ""))
        start_line = int(request.args.get("startLine", 0) or 0)
        end_line = int(request.args.get("endLine", 0) or 0)
        if not path:
            return ToolResult(ok=False, output="path is required.")
        content = store.read_file(path, start_line=start_line, end_line=end_line)
        return ToolResult(ok=True, output=content)


class MemorySearchTool:
    """Tool: memory_search — lexical search across memory files."""
    name = "memory_search"
    description = "Search memory files by query, returning ranked results."

    def __init__(self, store: Optional[MemoryStore] = None) -> None:
        self._store = store

    def run(self, request: ToolRequest, context: ToolContext) -> ToolResult:
        store = self._store or MemoryStore(context.workspace_root)
        query = str(request.args.get("query", ""))
        k = int(request.args.get("k", 10) or 10)
        if not query:
            return ToolResult(ok=False, output="query is required.")
        results = store.search(query, k=k)
        if not results:
            return ToolResult(ok=True, output="No results found.")
        lines = []
        for r in results:
            lines.append(f"{r['file']}:{r['line']} — {r['content']}")
        return ToolResult(ok=True, output="\n".join(lines))
