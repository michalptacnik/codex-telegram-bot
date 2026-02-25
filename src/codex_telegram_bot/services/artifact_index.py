"""Artifact and evidence index (EPIC 8, issue #86).

Provides a searchable index of files, diffs, reports, and URLs produced
during mission execution.  Artifacts are content-addressed by SHA-256 so
duplicate outputs from re-runs are automatically deduplicated.
"""
from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path
from typing import List, Optional

from codex_telegram_bot.domain.memory import (
    ARTIFACT_KINDS,
    ArtifactRecord,
)

logger = logging.getLogger(__name__)


def _sha256_of_file(path: Path) -> str:
    h = hashlib.sha256()
    try:
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return ""


def _sha256_of_text(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class ArtifactIndex:
    """Index artifacts produced by missions and query them by kind/tag.

    Usage::

        index = ArtifactIndex(store=store)
        artifact_id = index.register_file(
            mission_id="m1", step_index=2,
            path=Path("/workspace/report.txt"),
            name="final report", tags=["report", "output"],
        )
        artifacts = index.find(mission_id="m1", kind="file")
    """

    def __init__(self, store: "SqliteRunStore") -> None:  # type: ignore[name-defined]
        self._store = store

    def register_file(
        self,
        mission_id: str,
        path: Path,
        name: Optional[str] = None,
        step_index: Optional[int] = None,
        tags: Optional[List[str]] = None,
        meta: Optional[dict] = None,
    ) -> str:
        """Register a local file as an artifact.  Returns artifact_id."""
        path = Path(path)
        size = path.stat().st_size if path.exists() else 0
        sha = _sha256_of_file(path)
        return self._store.upsert_artifact(
            mission_id=mission_id,
            step_index=step_index,
            kind="file",
            name=name or path.name,
            uri=str(path),
            size_bytes=size,
            sha256=sha,
            tags=tags or [],
            meta=meta or {},
        )

    def register_text(
        self,
        mission_id: str,
        kind: str,
        name: str,
        content: str,
        step_index: Optional[int] = None,
        tags: Optional[List[str]] = None,
        meta: Optional[dict] = None,
    ) -> str:
        """Register inline text content (diff, log, report).  Returns artifact_id."""
        if kind not in ARTIFACT_KINDS:
            raise ValueError(f"Unknown artifact kind: {kind!r}")
        sha = _sha256_of_text(content)
        return self._store.upsert_artifact(
            mission_id=mission_id,
            step_index=step_index,
            kind=kind,
            name=name,
            uri=f"inline:{sha[:12]}",
            size_bytes=len(content.encode()),
            sha256=sha,
            tags=tags or [],
            meta={**(meta or {}), "content": content[:4096]},
        )

    def register_url(
        self,
        mission_id: str,
        url: str,
        name: str,
        step_index: Optional[int] = None,
        tags: Optional[List[str]] = None,
        meta: Optional[dict] = None,
    ) -> str:
        """Register an external URL as an artifact reference."""
        sha = _sha256_of_text(url)
        return self._store.upsert_artifact(
            mission_id=mission_id,
            step_index=step_index,
            kind="url",
            name=name,
            uri=url,
            size_bytes=0,
            sha256=sha,
            tags=tags or [],
            meta=meta or {},
        )

    def find(
        self,
        mission_id: str,
        kind: Optional[str] = None,
        tag: Optional[str] = None,
        limit: int = 100,
    ) -> List[ArtifactRecord]:
        """Query artifacts for a mission."""
        return self._store.list_artifacts(
            mission_id=mission_id,
            kind=kind,
            tag=tag,
            limit=limit,
        )

    def get(self, artifact_id: str) -> Optional[ArtifactRecord]:
        return self._store.get_artifact(artifact_id)

    def remove(self, artifact_id: str) -> bool:
        return self._store.delete_artifact(artifact_id)

    def remove_for_mission(self, mission_id: str) -> int:
        return self._store.delete_mission_artifacts(mission_id)


# Avoid circular import
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from codex_telegram_bot.persistence.sqlite_store import SqliteRunStore
