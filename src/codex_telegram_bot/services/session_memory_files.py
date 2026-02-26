"""Per-session memory MD files.

Manages two plain-Markdown files inside each session workspace:

  facts.md   – stable, reusable facts about the project / user preferences.
               Overwritten entirely when updated.
  worklog.md – append-only log of task outcomes (timestamped entries).

Both files are injected into prompts on-demand only — when they exist and
contain non-trivial content — to avoid bloating short prompts.

Char budgets for injection are intentionally conservative so they leave
room for history, retrieval, and tool schemas.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_FACTS_FILE = "facts.md"
_WORKLOG_FILE = "worklog.md"

# Maximum chars read from each file (guards against huge files).
_MAX_FILE_READ_CHARS = 8_000

# Maximum chars injected into a prompt from each source.
_INJECT_FACTS_CHARS = 600
_INJECT_WORKLOG_CHARS = 400


class SessionMemoryFiles:
    """Read/write facts.md and worklog.md for a single session workspace."""

    def __init__(self, workspace: Path) -> None:
        self._workspace = workspace

    @property
    def facts_path(self) -> Path:
        return self._workspace / _FACTS_FILE

    @property
    def worklog_path(self) -> Path:
        return self._workspace / _WORKLOG_FILE

    # ------------------------------------------------------------------
    # Readers
    # ------------------------------------------------------------------

    def read_facts(self) -> str:
        return self._read(self.facts_path)

    def read_worklog(self) -> str:
        return self._read(self.worklog_path)

    # ------------------------------------------------------------------
    # Writers
    # ------------------------------------------------------------------

    def write_facts(self, content: str) -> None:
        """Overwrite facts.md entirely."""
        self._write(self.facts_path, (content or "").strip())

    def append_worklog(self, outcome: str) -> None:
        """Append a timestamped entry to worklog.md."""
        existing = self.read_worklog()
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        entry = f"\n## {ts}\n{(outcome or '').strip()}\n"
        self._write(self.worklog_path, existing + entry)

    # ------------------------------------------------------------------
    # Prompt injection
    # ------------------------------------------------------------------

    def inject_context(self) -> str:
        """Return memory context string to prepend to a prompt.

        Returns an empty string when both files are empty / absent, so the
        caller can skip the section entirely.
        """
        facts_raw = self.read_facts()[:_INJECT_FACTS_CHARS]
        worklog_raw = self.read_worklog()

        parts: list[str] = []

        if facts_raw.strip():
            parts.append(f"Session facts:\n{facts_raw.strip()}")

        if worklog_raw.strip():
            # Only show the last portion of the worklog to stay within budget.
            lines = worklog_raw.strip().splitlines()
            recent_lines = lines[-25:] if len(lines) > 25 else lines
            recent = "\n".join(recent_lines)[:_INJECT_WORKLOG_CHARS]
            if recent.strip():
                parts.append(f"Recent task log:\n{recent.strip()}")

        return "\n\n".join(parts)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _read(self, path: Path) -> str:
        try:
            if path.exists() and path.is_file():
                return path.read_text(encoding="utf-8", errors="replace")[:_MAX_FILE_READ_CHARS]
        except Exception as exc:
            logger.warning("session_memory_files: read failed %s: %s", path, exc)
        return ""

    def _write(self, path: Path, content: str) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        except Exception as exc:
            logger.warning("session_memory_files: write failed %s: %s", path, exc)
