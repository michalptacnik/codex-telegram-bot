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
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from codex_telegram_bot.services.thin_memory import ThinMemoryStore
from codex_telegram_bot.tools.base import ToolContext, ToolRequest, ToolResult

_PRELOAD_BUDGET_CHARS = 8000
_MAX_SEARCH_RESULTS = 20
_MAX_POINTER_OPEN_CHARS = 20_000


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


class MemoryIndexGetTool:
    """Tool: memory_index_get — return the thin memory index (budgeted)."""

    name = "memory_index_get"
    description = "Return MEMORY_INDEX.md content within strict size limits."

    def run(self, request: ToolRequest, context: ToolContext) -> ToolResult:
        store = ThinMemoryStore(workspace_root=context.workspace_root)
        return ToolResult(ok=True, output=store.read_index_text())


class MemoryPageListTool:
    """Tool: memory_page_list — list curated pages and pointers."""

    name = "memory_page_list"
    description = "List memory/pages markdown files and pointer IDs."

    def run(self, request: ToolRequest, context: ToolContext) -> ToolResult:
        store = ThinMemoryStore(workspace_root=context.workspace_root)
        prefix = str(request.args.get("prefix") or "").strip()
        pages = store.list_pages(prefix=prefix)
        index = store.load_index()
        lines: List[str] = []
        lines.append("Pointers:")
        pointer_rows = sorted(index.pointers.items())
        if pointer_rows:
            for pointer_id, target in pointer_rows:
                blob = f"{pointer_id} -> {target}"
                if prefix and prefix.lower() not in blob.lower():
                    continue
                lines.append(f"- {blob}")
        else:
            lines.append("- (none)")
        lines.append("")
        lines.append("Pages:")
        if pages:
            for page in pages:
                path = str(page.get("path") or "")
                pointer_ids = list(page.get("pointer_ids") or [])
                lines.append(f"- {path} | pointers: {','.join(pointer_ids) if pointer_ids else '-'}")
        else:
            lines.append("- (none)")
        return ToolResult(ok=True, output="\n".join(lines))


class MemoryPointerOpenTool:
    """Tool: memory_pointer_open — open a pointer target and return excerpt."""

    name = "memory_pointer_open"
    description = "Open a pointer from MEMORY_INDEX and return bounded markdown excerpt."

    def run(self, request: ToolRequest, context: ToolContext) -> ToolResult:
        store = ThinMemoryStore(workspace_root=context.workspace_root)
        pointer_id = str(
            request.args.get("pointer_id")
            or request.args.get("pointerId")
            or ""
        ).strip()
        if not pointer_id:
            return ToolResult(ok=False, output="pointer_id is required.")
        try:
            max_chars = int(request.args.get("max_chars", 12_000) or 12_000)
        except Exception:
            max_chars = 12_000
        max_chars = max(100, min(max_chars, _MAX_POINTER_OPEN_CHARS))
        try:
            opened = store.open_pointer(pointer_id=pointer_id, max_chars=max_chars)
        except Exception as exc:
            return ToolResult(ok=False, output=f"Failed to open pointer: {exc}")
        output = (
            f"pointer_id: {opened['pointer_id']}\n"
            f"target: {opened['target']}\n\n"
            f"{opened['excerpt']}"
        )
        return ToolResult(ok=True, output=output)


class MemoryAppendDailyTool:
    """Tool: memory_append_daily — append a daily memory note."""

    name = "memory_append_daily"
    description = "Append text to memory/daily/YYYY-MM-DD.md and refresh date pointer."

    def run(self, request: ToolRequest, context: ToolContext) -> ToolResult:
        text = str(request.args.get("text") or "").strip()
        if not text:
            return ToolResult(ok=False, output="text is required.")
        raw_date = str(request.args.get("date") or "").strip()
        parsed: Optional[date] = None
        if raw_date:
            try:
                parsed = datetime.strptime(raw_date, "%Y-%m-%d").date()
            except ValueError:
                return ToolResult(ok=False, output="date must be YYYY-MM-DD.")
        store = ThinMemoryStore(workspace_root=context.workspace_root)
        try:
            p = store.append_daily(text=text, on_date=parsed)
        except Exception as exc:
            return ToolResult(ok=False, output=f"Failed to append daily memory: {exc}")
        return ToolResult(ok=True, output=f"Appended daily memory at {p}.")


class MemoryIndexUpdateTool:
    """Tool: memory_index_update — apply bounded structured patch to index."""

    name = "memory_index_update"
    description = "Apply a structured patch to MEMORY_INDEX sections with cap enforcement."

    def run(self, request: ToolRequest, context: ToolContext) -> ToolResult:
        patch = request.args.get("patch")
        if patch is None:
            return ToolResult(ok=False, output="patch is required.")
        if isinstance(patch, str):
            raw = patch.strip()
            if not raw:
                return ToolResult(ok=False, output="patch cannot be empty.")
            try:
                parsed = json.loads(raw)
            except Exception as exc:
                return ToolResult(ok=False, output=f"patch must be valid JSON object: {exc}")
            patch = parsed
        if not isinstance(patch, dict):
            return ToolResult(ok=False, output="patch must be an object.")
        if len(json.dumps(patch, ensure_ascii=True)) > 24_000:
            return ToolResult(ok=False, output="patch too large.")
        store = ThinMemoryStore(workspace_root=context.workspace_root)
        try:
            index = store.update_index_patch(patch)
        except Exception as exc:
            return ToolResult(ok=False, output=f"Failed to update index: {exc}")
        return ToolResult(
            ok=True,
            output=(
                "Updated MEMORY_INDEX.\n"
                f"identity={len(index.identity)} "
                f"active_projects={len(index.active_projects)} "
                f"obligations={len(index.obligations)} "
                f"preferences={len(index.preferences)} "
                f"pointers={len(index.pointers)}"
            ),
        )
