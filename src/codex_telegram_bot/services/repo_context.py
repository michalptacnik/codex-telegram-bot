from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import re
from typing import Dict, Iterable, List


_TEXT_EXTENSIONS = {
    ".py",
    ".md",
    ".txt",
    ".json",
    ".toml",
    ".yaml",
    ".yml",
    ".ini",
    ".cfg",
    ".sh",
    ".js",
    ".ts",
    ".tsx",
    ".html",
    ".css",
    ".sql",
}
_SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", ".mypy_cache", ".pytest_cache", "dist", "node_modules"}


@dataclass(frozen=True)
class RepoSnippet:
    path: str
    score: int
    snippet: str


@dataclass(frozen=True)
class IndexedFile:
    path: str
    content: str
    symbols: List[str]
    mtime_ns: int


class RepositoryContextRetriever:
    def __init__(
        self,
        root: Path,
        max_scan_files: int = 3000,
        max_file_bytes: int = 120_000,
        auto_refresh_sec: int = 30,
    ):
        self._root = root.expanduser().resolve()
        self._max_scan_files = max(100, int(max_scan_files))
        self._max_file_bytes = max(4096, int(max_file_bytes))
        self._auto_refresh_sec = max(0, int(auto_refresh_sec))
        self._index: Dict[str, IndexedFile] = {}
        self._last_refresh_unix = 0.0
        self.refresh_index(force=True)

    def retrieve(self, query: str, limit: int = 5) -> List[RepoSnippet]:
        self.refresh_index()
        tokens = _query_tokens(query)
        if not tokens:
            return []
        results: List[RepoSnippet] = []
        for rel, entry in self._index.items():
            score, snippet = self._score_entry(rel_path=rel, entry=entry, tokens=tokens)
            if score <= 0 or not snippet:
                continue
            results.append(RepoSnippet(path=rel, score=score, snippet=snippet))
        results.sort(key=lambda x: x.score, reverse=True)
        return results[: max(1, limit)]

    def refresh_index(self, force: bool = False) -> Dict[str, int]:
        now_unix = datetime.now(timezone.utc).timestamp()
        if not force and self._auto_refresh_sec > 0 and (now_unix - self._last_refresh_unix) < self._auto_refresh_sec:
            return {"indexed_files": len(self._index), "changed_files": 0, "removed_files": 0}

        seen: Dict[str, IndexedFile] = {}
        changed = 0
        for file_path in self._iter_candidate_files():
            rel = str(file_path.relative_to(self._root))
            try:
                stat = file_path.stat()
            except OSError:
                continue
            existing = self._index.get(rel)
            if existing and existing.mtime_ns == int(stat.st_mtime_ns):
                seen[rel] = existing
                continue
            try:
                raw = file_path.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            content = raw[: self._max_file_bytes]
            symbols = _extract_symbols(content)
            seen[rel] = IndexedFile(
                path=rel,
                content=content,
                symbols=symbols,
                mtime_ns=int(stat.st_mtime_ns),
            )
            changed += 1

        removed = 0
        for rel in list(self._index.keys()):
            if rel not in seen:
                removed += 1
        self._index = seen
        self._last_refresh_unix = now_unix
        return {"indexed_files": len(self._index), "changed_files": changed, "removed_files": removed}

    def stats(self) -> Dict[str, int]:
        return {
            "indexed_files": len(self._index),
            "max_scan_files": self._max_scan_files,
            "max_file_bytes": self._max_file_bytes,
            "auto_refresh_sec": self._auto_refresh_sec,
            "last_refresh_unix": int(self._last_refresh_unix),
        }

    def _iter_candidate_files(self) -> Iterable[Path]:
        count = 0
        for p in self._root.rglob("*"):
            if count >= self._max_scan_files:
                break
            if p.is_dir():
                continue
            if any(part in _SKIP_DIRS for part in p.parts):
                continue
            if p.suffix.lower() not in _TEXT_EXTENSIONS:
                continue
            count += 1
            yield p

    def _score_entry(self, rel_path: str, entry: IndexedFile, tokens: List[str]) -> tuple[int, str]:
        path_low = rel_path.lower()
        path_hits = sum(1 for t in tokens if t in path_low)
        score = path_hits * 6
        content_low = entry.content.lower()
        content_hits = sum(content_low.count(t) for t in tokens)
        score += min(content_hits, 120)
        symbol_hits = sum(1 for t in tokens if any(t in sym for sym in entry.symbols))
        score += symbol_hits * 15
        if score <= 0:
            return 0, ""
        snippet = _best_snippet(content=entry.content, tokens=tokens, max_chars=700)
        if entry.symbols:
            snippet = "symbols: " + ", ".join(entry.symbols[:8]) + "\n" + snippet
        return score, snippet


def _query_tokens(query: str) -> List[str]:
    tokens = re.findall(r"[a-zA-Z0-9_/-]{3,}", query or "")
    out: List[str] = []
    seen = set()
    for t in tokens:
        key = t.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out[:12]


def _extract_symbols(content: str) -> List[str]:
    symbols: List[str] = []
    for line in content.splitlines():
        m_py_def = re.match(r"\s*def\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(", line)
        if m_py_def:
            symbols.append(m_py_def.group(1).lower())
            continue
        m_py_cls = re.match(r"\s*class\s+([A-Za-z_][A-Za-z0-9_]*)\s*[:(]", line)
        if m_py_cls:
            symbols.append(m_py_cls.group(1).lower())
            continue
        m_ts_fn = re.match(r"\s*(?:export\s+)?function\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(", line)
        if m_ts_fn:
            symbols.append(m_ts_fn.group(1).lower())
            continue
    uniq: List[str] = []
    seen = set()
    for s in symbols:
        if s in seen:
            continue
        seen.add(s)
        uniq.append(s)
    return uniq[:40]


def _best_snippet(content: str, tokens: List[str], max_chars: int) -> str:
    lines = content.splitlines()
    best_idx = 0
    best_score = -1
    for i, line in enumerate(lines):
        low = line.lower()
        score = sum(low.count(t) for t in tokens)
        if score > best_score:
            best_score = score
            best_idx = i
    start = max(0, best_idx - 3)
    end = min(len(lines), best_idx + 4)
    snippet = "\n".join(lines[start:end]).strip()
    return snippet[:max_chars]
