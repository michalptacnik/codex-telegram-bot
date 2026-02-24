"""Long-horizon mission memory domain model (EPIC 8).

Defines the data types used by the memory store, artifact index, and
summarisation service.  All records are frozen dataclasses so they can
be safely cached and compared.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

# ---------------------------------------------------------------------------
# Memory facts
# ---------------------------------------------------------------------------

MEMORY_KIND_FACT = "fact"           # observed fact (e.g. "repo uses pytest")
MEMORY_KIND_DECISION = "decision"   # explicit decision made during a mission
MEMORY_KIND_CONTEXT = "context"     # background context injected at mission start
MEMORY_KIND_NOTE = "note"           # free-form note

MEMORY_KINDS = frozenset([
    MEMORY_KIND_FACT,
    MEMORY_KIND_DECISION,
    MEMORY_KIND_CONTEXT,
    MEMORY_KIND_NOTE,
])


@dataclass(frozen=True)
class MemoryEntry:
    """One persisted memory fact / decision linked to a mission."""
    entry_id: str
    mission_id: str
    kind: str           # see MEMORY_KINDS
    key: str            # short label used for dedup / retrieval (e.g. "test_framework")
    value: str          # human-readable content
    tags: List[str]     # free-form tags for filtering
    importance: int     # 0–10, higher = kept longer under compaction
    created_at: datetime
    expires_at: Optional[datetime]  # None = never expires


# ---------------------------------------------------------------------------
# Artifacts
# ---------------------------------------------------------------------------

ARTIFACT_KIND_FILE = "file"
ARTIFACT_KIND_DIFF = "diff"
ARTIFACT_KIND_REPORT = "report"
ARTIFACT_KIND_LOG = "log"
ARTIFACT_KIND_URL = "url"

ARTIFACT_KINDS = frozenset([
    ARTIFACT_KIND_FILE,
    ARTIFACT_KIND_DIFF,
    ARTIFACT_KIND_REPORT,
    ARTIFACT_KIND_LOG,
    ARTIFACT_KIND_URL,
])


@dataclass(frozen=True)
class ArtifactRecord:
    """Indexed pointer to a piece of evidence produced during a mission."""
    artifact_id: str
    mission_id: str
    step_index: Optional[int]     # which step produced it (None = mission-level)
    kind: str                     # see ARTIFACT_KINDS
    name: str                     # display name
    uri: str                      # local path or URL
    size_bytes: int
    sha256: str                   # hex digest (empty string if unavailable)
    tags: List[str]
    created_at: datetime
    meta_json: str                # JSON blob for kind-specific metadata


# ---------------------------------------------------------------------------
# Summaries
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MissionSummary:
    """Compact narrative summary of a completed or long-running mission."""
    summary_id: str
    mission_id: str
    text: str           # prose summary ≤ ~500 words
    memory_count: int   # entries this summary covers
    artifact_count: int
    created_at: datetime
    compacted: bool     # True if source entries have been deleted
