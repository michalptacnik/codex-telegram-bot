from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import List

INDEX_STRIDE_BYTES = 8 * 1024


@dataclass(frozen=True)
class ChunkRecord:
    process_session_id: str
    seq: int
    created_at: str
    start_offset: int
    end_offset: int
    preview: str


class SessionLogIndexer:
    """Append-only log writer with lightweight chunk indexing.

    The index is written to a JSONL file and can also be mirrored into SQLite.
    """

    def __init__(
        self,
        process_session_id: str,
        log_path: Path,
        chunks_path: Path,
        stride_bytes: int = INDEX_STRIDE_BYTES,
    ) -> None:
        self.process_session_id = process_session_id
        self.log_path = Path(log_path)
        self.chunks_path = Path(chunks_path)
        self._stride_bytes = max(1024, int(stride_bytes))
        self._offset = 0
        self._seq = 0
        self._last_chunk_at = 0

    @property
    def offset(self) -> int:
        return self._offset

    def initialize(self) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_path.touch(exist_ok=True)
        self.chunks_path.touch(exist_ok=True)
        self._offset = int(self.log_path.stat().st_size)
        self._last_chunk_at = self._offset

    def append_text(self, text: str) -> tuple[int, List[ChunkRecord]]:
        payload = (text or "").encode("utf-8", errors="replace")
        if not payload:
            return 0, []

        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("ab") as fh:
            fh.write(payload)

        start = self._offset
        self._offset += len(payload)
        created: List[ChunkRecord] = []

        # Record coarse chunk markers every stride to support fast retrieval.
        while self._offset - self._last_chunk_at >= self._stride_bytes:
            self._seq += 1
            rec = ChunkRecord(
                process_session_id=self.process_session_id,
                seq=self._seq,
                created_at=_utc_now(),
                start_offset=self._last_chunk_at,
                end_offset=min(self._offset, self._last_chunk_at + self._stride_bytes),
                preview=_preview_text(text),
            )
            created.append(rec)
            with self.chunks_path.open("a", encoding="utf-8") as index_fh:
                index_fh.write(json.dumps(rec.__dict__, ensure_ascii=True) + "\n")
            self._last_chunk_at = rec.end_offset

        return len(payload), created


def search_log_file(
    log_path: Path,
    query: str,
    max_results: int = 5,
    context_lines: int = 2,
    min_offset: int = 0,
) -> List[dict]:
    """Return small, offset-addressable excerpts for a text query."""
    needle = (query or "").strip().lower()
    if not needle:
        return []

    path = Path(log_path)
    if not path.exists() or not path.is_file():
        return []

    raw = path.read_text(encoding="utf-8", errors="replace")
    if not raw:
        return []

    max_results = max(1, min(int(max_results), 20))
    context_lines = max(0, min(int(context_lines), 6))
    min_offset = max(0, int(min_offset))

    lines = raw.splitlines()
    starts: List[int] = []
    off = 0
    for line in lines:
        starts.append(off)
        off += len(line) + 1

    found: List[dict] = []
    for idx, line in enumerate(lines):
        pos = line.lower().find(needle)
        if pos < 0:
            continue
        offset = starts[idx] + pos
        if offset < min_offset:
            continue

        lo = max(0, idx - context_lines)
        hi = min(len(lines), idx + context_lines + 1)
        excerpt = "\n".join(lines[lo:hi]).strip()
        found.append(
            {
                "offset": offset,
                "line": idx + 1,
                "excerpt": excerpt,
            }
        )
        if len(found) >= max_results:
            break

    return found


def _preview_text(text: str, max_chars: int = 180) -> str:
    one_line = " ".join((text or "").split())
    return one_line[:max_chars]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
