"""Per-session workspace provisioning with disk quotas (Parity Epic 5).

Each Telegram session gets an isolated directory under a configurable root.
The manager enforces soft limits on disk usage and file count, exposes quota
status, and cleans up workspaces on session archival.
"""
from __future__ import annotations

import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

_SAFE_RE = re.compile(r"[^a-zA-Z0-9_-]")
_DEFAULT_MAX_BYTES = 100 * 1024 * 1024  # 100 MiB
_DEFAULT_MAX_FILES = 5_000


class WorkspaceQuotaExceeded(Exception):
    """Raised when a session workspace exceeds its configured quota."""


@dataclass
class WorkspaceInfo:
    session_id: str
    path: Path
    disk_bytes: int
    file_count: int
    within_quota: bool
    created_at: datetime


class WorkspaceManager:
    """Manages per-session workspaces with disk quotas and lifecycle tracking."""

    def __init__(
        self,
        root: Path,
        max_disk_bytes: int = _DEFAULT_MAX_BYTES,
        max_file_count: int = _DEFAULT_MAX_FILES,
    ) -> None:
        self._root = Path(root).expanduser().resolve()
        self._root.mkdir(parents=True, exist_ok=True)
        self._max_disk_bytes = max(1024 * 1024, int(max_disk_bytes))
        self._max_file_count = max(1, int(max_file_count))
        self._registry: Dict[str, datetime] = {}

    @property
    def root(self) -> Path:
        return self._root

    @property
    def max_disk_bytes(self) -> int:
        return self._max_disk_bytes

    @property
    def max_file_count(self) -> int:
        return self._max_file_count

    def _session_path(self, session_id: str) -> Path:
        safe = _SAFE_RE.sub("_", (session_id or "").strip())[:64] or "default"
        return self._root / safe

    def provision(self, session_id: str) -> Path:
        """Create and return the workspace path for a session."""
        p = self._session_path(session_id)
        p.mkdir(parents=True, exist_ok=True)
        if session_id not in self._registry:
            self._registry[session_id] = datetime.now(timezone.utc)
        return p

    def quota_status(self, session_id: str) -> WorkspaceInfo:
        """Return current disk usage and quota compliance for a session."""
        p = self._session_path(session_id)
        disk_bytes = 0
        file_count = 0
        if p.exists():
            for f in p.rglob("*"):
                if f.is_file():
                    try:
                        disk_bytes += f.stat().st_size
                    except OSError:
                        pass
                    file_count += 1
        within_quota = (
            disk_bytes <= self._max_disk_bytes and file_count <= self._max_file_count
        )
        return WorkspaceInfo(
            session_id=session_id,
            path=p,
            disk_bytes=disk_bytes,
            file_count=file_count,
            within_quota=within_quota,
            created_at=self._registry.get(session_id, datetime.now(timezone.utc)),
        )

    def enforce_quota(self, session_id: str) -> bool:
        """Return True if within quota; raise WorkspaceQuotaExceeded if not."""
        info = self.quota_status(session_id)
        if not info.within_quota:
            raise WorkspaceQuotaExceeded(
                f"Session {session_id!r} exceeds quota: "
                f"{info.disk_bytes} bytes / {info.file_count} files "
                f"(limits: {self._max_disk_bytes} bytes / {self._max_file_count} files)"
            )
        return True

    def cleanup(self, session_id: str) -> dict:
        """Remove workspace directory and deregister the session."""
        p = self._session_path(session_id)
        removed_bytes = 0
        removed_files = 0
        if p.exists():
            for f in p.rglob("*"):
                if f.is_file():
                    try:
                        removed_bytes += f.stat().st_size
                    except OSError:
                        pass
                    removed_files += 1
            shutil.rmtree(str(p), ignore_errors=True)
        self._registry.pop(session_id, None)
        return {
            "session_id": session_id,
            "removed_bytes": removed_bytes,
            "removed_files": removed_files,
        }

    def list_workspaces(self) -> List[WorkspaceInfo]:
        """Return quota info for all currently provisioned workspaces."""
        return [self.quota_status(sid) for sid in list(self._registry)]

    def cleanup_all(self) -> List[dict]:
        """Remove all provisioned workspaces. Returns cleanup summaries."""
        return [self.cleanup(sid) for sid in list(self._registry)]
