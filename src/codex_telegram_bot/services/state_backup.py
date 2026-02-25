"""State backup and restore (EPIC 10, issue #96).

Provides a StateBackupService that:
  - Copies the SQLite DB file to a timestamped backup directory.
  - Verifies backup integrity with a SHA-256 checksum.
  - Lists available backups.
  - Restores from a chosen backup (with safety pre-check).
  - Runs periodic auto-backups via an async background loop.

Usage::

    svc = StateBackupService(db_path=Path("data/bot.db"),
                             backup_dir=Path("data/backups"))
    # One-shot backup
    record = svc.backup()
    # Restore
    svc.restore(record.backup_id)

    # Periodic auto-backup
    await svc.start_auto_backup(interval_sec=3600)
    # ...
    await svc.stop_auto_backup()
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

_CHECKSUM_FILENAME = "checksum.sha256"
_MANIFEST_FILENAME = "manifest.json"


@dataclass(frozen=True)
class BackupRecord:
    backup_id: str          # ISO timestamp used as directory name
    path: Path              # path to the .db copy
    size_bytes: int
    sha256: str
    created_at: datetime
    source_path: Path

    def is_valid(self) -> bool:
        """Return True if backup file exists and its checksum matches."""
        if not self.path.exists():
            return False
        return _sha256_file(self.path) == self.sha256


class StateBackupService:
    """Backup and restore the SQLite state database."""

    def __init__(
        self,
        db_path: Path,
        backup_dir: Path,
        max_backups: int = 20,
    ) -> None:
        self._db_path = Path(db_path)
        self._backup_dir = Path(backup_dir)
        self._max_backups = max_backups
        self._task: Optional[asyncio.Task] = None
        self._stop_event: Optional[asyncio.Event] = None

    # ------------------------------------------------------------------
    # Core operations
    # ------------------------------------------------------------------

    def backup(self) -> BackupRecord:
        """Create a timestamped backup.  Returns a BackupRecord."""
        self._backup_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
        dest_dir = self._backup_dir / ts
        dest_dir.mkdir(exist_ok=True)
        dest_file = dest_dir / self._db_path.name

        # Use SQLite's online backup API for a consistent snapshot
        _sqlite_backup(self._db_path, dest_file)

        sha = _sha256_file(dest_file)
        size = dest_file.stat().st_size
        (dest_dir / _CHECKSUM_FILENAME).write_text(f"{sha}  {self._db_path.name}\n")

        record = BackupRecord(
            backup_id=ts,
            path=dest_file,
            size_bytes=size,
            sha256=sha,
            created_at=datetime.now(timezone.utc),
            source_path=self._db_path,
        )
        logger.info("backup: created %s (%.1f KB, sha256=%s…)", ts, size / 1024, sha[:12])
        self._prune()
        return record

    def list_backups(self) -> List[BackupRecord]:
        """Return available backups sorted newest-first."""
        if not self._backup_dir.exists():
            return []
        records: List[BackupRecord] = []
        for entry in sorted(self._backup_dir.iterdir(), reverse=True):
            if not entry.is_dir():
                continue
            db_file = entry / self._db_path.name
            if not db_file.exists():
                continue
            checksum_file = entry / _CHECKSUM_FILENAME
            sha = checksum_file.read_text().split()[0] if checksum_file.exists() else ""
            try:
                size = db_file.stat().st_size
                records.append(BackupRecord(
                    backup_id=entry.name,
                    path=db_file,
                    size_bytes=size,
                    sha256=sha,
                    created_at=_parse_ts(entry.name),
                    source_path=self._db_path,
                ))
            except OSError:
                pass
        return records

    def restore(self, backup_id: str, force: bool = False) -> Path:
        """Restore the DB from ``backup_id``.

        The current DB is moved to ``<db>.pre-restore`` before overwriting.
        Pass ``force=True`` to skip the integrity check.
        Returns the path of the pre-restore backup.
        """
        target_dir = self._backup_dir / backup_id
        src = target_dir / self._db_path.name
        if not src.exists():
            raise FileNotFoundError(f"Backup not found: {backup_id}")

        checksum_file = target_dir / _CHECKSUM_FILENAME
        if not force and checksum_file.exists():
            expected = checksum_file.read_text().split()[0]
            actual = _sha256_file(src)
            if actual != expected:
                raise ValueError(
                    f"Integrity check failed for {backup_id}: "
                    f"expected {expected[:12]}… got {actual[:12]}…"
                )

        # Preserve current state
        pre_restore = self._db_path.with_suffix(".pre-restore.db")
        if self._db_path.exists():
            shutil.copy2(self._db_path, pre_restore)
            logger.info("restore: current DB saved to %s", pre_restore)

        shutil.copy2(src, self._db_path)
        logger.info("restore: restored %s from backup %s", self._db_path, backup_id)
        return pre_restore

    def verify(self, backup_id: str) -> bool:
        """Return True if the backup file is present and its checksum is valid."""
        target_dir = self._backup_dir / backup_id
        src = target_dir / self._db_path.name
        checksum_file = target_dir / _CHECKSUM_FILENAME
        if not src.exists() or not checksum_file.exists():
            return False
        expected = checksum_file.read_text().split()[0]
        return _sha256_file(src) == expected

    # ------------------------------------------------------------------
    # Async auto-backup
    # ------------------------------------------------------------------

    async def start_auto_backup(self, interval_sec: float = 3600.0) -> None:
        if self._task and not self._task.done():
            return
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(
            self._backup_loop(interval_sec), name="state-backup"
        )
        logger.info("backup: auto-backup started (interval=%.0fs)", interval_sec)

    async def stop_auto_backup(self) -> None:
        if self._stop_event:
            self._stop_event.set()
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _backup_loop(self, interval_sec: float) -> None:
        while not (self._stop_event and self._stop_event.is_set()):
            try:
                self.backup()
            except Exception:
                logger.exception("backup: auto-backup failed")
            try:
                await asyncio.wait_for(
                    asyncio.shield(self._stop_event.wait()),  # type: ignore[arg-type]
                    timeout=interval_sec,
                )
                break
            except asyncio.TimeoutError:
                pass

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _prune(self) -> None:
        """Remove oldest backups beyond max_backups."""
        backups = self.list_backups()
        for old in backups[self._max_backups:]:
            try:
                shutil.rmtree(old.path.parent)
                logger.debug("backup: pruned old backup %s", old.backup_id)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _sqlite_backup(src: Path, dest: Path) -> None:
    """Use SQLite online backup API for a crash-consistent copy."""
    src_conn = sqlite3.connect(str(src))
    dst_conn = sqlite3.connect(str(dest))
    try:
        src_conn.backup(dst_conn)
    finally:
        dst_conn.close()
        src_conn.close()


def _parse_ts(name: str) -> datetime:
    """Parse backup_id like 20240101T120000_123456Z into datetime."""
    for fmt in ("%Y%m%dT%H%M%S_%fZ", "%Y%m%dT%H%M%SZ"):
        try:
            return datetime.strptime(name, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return datetime.now(timezone.utc)
