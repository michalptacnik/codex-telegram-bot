from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

MEMORY_INDEX_MAX_CHARS = max(
    1024,
    int((os.environ.get("MEMORY_INDEX_MAX_CHARS") or "8000").strip() or "8000"),
)

MEMORY_INDEX_MAX_ACTIVE_PROJECTS = 10
MEMORY_INDEX_MAX_OBLIGATIONS = 20
MEMORY_INDEX_MAX_PREFERENCES = 15
MEMORY_INDEX_MAX_POINTERS = 50

MEMORY_DAILY_MAX_CHARS = max(
    10_000,
    int((os.environ.get("MEMORY_DAILY_MAX_CHARS") or "200000").strip() or "200000"),
)

_SAFE_POINTER_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")


@dataclass(frozen=True)
class ActiveProject:
    project_id: str
    title: str
    path: str


@dataclass(frozen=True)
class Obligation:
    obligation_id: str
    text: str
    due: str = ""
    ref: str = ""


@dataclass
class ThinMemoryIndex:
    identity: Dict[str, str] = field(default_factory=dict)
    active_projects: List[ActiveProject] = field(default_factory=list)
    obligations: List[Obligation] = field(default_factory=list)
    preferences: Dict[str, str] = field(default_factory=dict)
    pointers: Dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class MemoryLayout:
    root: Path
    memory_dir: Path
    index_path: Path
    daily_dir: Path
    pages_dir: Path


def ensure_memory_layout(workspace_root: Path) -> MemoryLayout:
    ws = Path(workspace_root).expanduser().resolve()
    memory_dir = ws / "memory"
    daily_dir = memory_dir / "daily"
    pages_dir = memory_dir / "pages"
    index_path = memory_dir / "MEMORY_INDEX.md"

    daily_dir.mkdir(parents=True, exist_ok=True)
    pages_dir.mkdir(parents=True, exist_ok=True)
    (pages_dir / "projects").mkdir(parents=True, exist_ok=True)
    if not index_path.exists():
        index_path.write_text(render_index(ThinMemoryIndex()), encoding="utf-8")

    return MemoryLayout(
        root=ws,
        memory_dir=memory_dir,
        index_path=index_path,
        daily_dir=daily_dir,
        pages_dir=pages_dir,
    )


def parse_index(content: str) -> ThinMemoryIndex:
    section = ""
    index = ThinMemoryIndex()
    for raw_line in (content or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("## "):
            heading = line[3:].strip().lower()
            if heading.startswith("identity"):
                section = "identity"
            elif heading.startswith("active projects"):
                section = "active_projects"
            elif heading.startswith("obligations"):
                section = "obligations"
            elif heading.startswith("preferences"):
                section = "preferences"
            elif heading.startswith("pointers"):
                section = "pointers"
            else:
                section = ""
            continue
        if not line.startswith("- "):
            continue
        item = line[2:].strip()
        if not item:
            continue
        if section in {"identity", "preferences"} and ":" in item:
            key, _, value = item.partition(":")
            key = key.strip()
            value = value.strip()
            if key:
                if section == "identity":
                    index.identity[key] = value
                else:
                    index.preferences[key] = value
            continue
        if section == "active_projects":
            parts = [p.strip() for p in item.split("|")]
            if len(parts) >= 3 and parts[0] and parts[1] and parts[2]:
                index.active_projects.append(
                    ActiveProject(project_id=parts[0], title=parts[1], path=parts[2])
                )
            continue
        if section == "obligations":
            parts = [p.strip() for p in item.split("|")]
            if len(parts) < 2:
                continue
            obligation_id = parts[0]
            text = parts[1]
            due = ""
            ref = ""
            for extra in parts[2:]:
                lowered = extra.lower()
                if lowered.startswith("due:"):
                    due = extra[4:].strip()
                elif lowered.startswith("ref:"):
                    ref = extra[4:].strip()
            if obligation_id and text:
                index.obligations.append(
                    Obligation(
                        obligation_id=obligation_id,
                        text=text,
                        due=due,
                        ref=ref,
                    )
                )
            continue
        if section == "pointers" and "->" in item:
            pointer_id, _, target = item.partition("->")
            pointer_id = pointer_id.strip()
            target = target.strip()
            if pointer_id and target:
                index.pointers[pointer_id] = target

    _validate_index(index=index, max_chars=max(MEMORY_INDEX_MAX_CHARS, len(content or "")))
    return index


def render_index(index: ThinMemoryIndex) -> str:
    lines: List[str] = [
        "# MEMORY_INDEX v1",
        "## Identity",
    ]
    for key, value in sorted(index.identity.items()):
        lines.append(f"- {key}: {value}")

    lines.append("")
    lines.append(f"## Active Projects (max {MEMORY_INDEX_MAX_ACTIVE_PROJECTS})")
    for item in index.active_projects[:MEMORY_INDEX_MAX_ACTIVE_PROJECTS]:
        lines.append(f"- {item.project_id} | {item.title} | {item.path}")

    lines.append("")
    lines.append(f"## Obligations (max {MEMORY_INDEX_MAX_OBLIGATIONS})")
    for item in index.obligations[:MEMORY_INDEX_MAX_OBLIGATIONS]:
        extras: List[str] = []
        if item.due:
            extras.append(f"due: {item.due}")
        if item.ref:
            extras.append(f"ref: {item.ref}")
        suffix = " | " + " | ".join(extras) if extras else ""
        lines.append(f"- {item.obligation_id} | {item.text}{suffix}")

    lines.append("")
    lines.append(f"## Preferences (max {MEMORY_INDEX_MAX_PREFERENCES})")
    for key, value in sorted(index.preferences.items()):
        lines.append(f"- {key}: {value}")

    lines.append("")
    lines.append(f"## Pointers (max {MEMORY_INDEX_MAX_POINTERS})")
    for pointer_id, target in sorted(index.pointers.items()):
        lines.append(f"- {pointer_id} -> {target}")

    return "\n".join(lines).strip() + "\n"


def _validate_memory_target(target: str) -> None:
    normalized = (target or "").strip()
    if not normalized:
        raise ValueError("target path cannot be empty")
    base_target = normalized.split("#", 1)[0].strip()
    p = Path(base_target)
    if p.is_absolute():
        raise ValueError("memory targets must be relative paths")
    if ".." in p.parts:
        raise ValueError("memory targets cannot use parent traversal")
    if not str(p).startswith("memory/"):
        raise ValueError("memory targets must stay under memory/")


def _validate_index(index: ThinMemoryIndex, max_chars: int = MEMORY_INDEX_MAX_CHARS) -> None:
    if len(index.active_projects) > MEMORY_INDEX_MAX_ACTIVE_PROJECTS:
        raise ValueError("Active Projects section exceeds max items")
    if len(index.obligations) > MEMORY_INDEX_MAX_OBLIGATIONS:
        raise ValueError("Obligations section exceeds max items")
    if len(index.preferences) > MEMORY_INDEX_MAX_PREFERENCES:
        raise ValueError("Preferences section exceeds max items")
    if len(index.pointers) > MEMORY_INDEX_MAX_POINTERS:
        raise ValueError("Pointers section exceeds max items")

    project_ids = set()
    for item in index.active_projects:
        if not _SAFE_POINTER_ID_RE.match(item.project_id):
            raise ValueError("invalid active project id")
        if item.project_id in project_ids:
            raise ValueError("duplicate active project id")
        project_ids.add(item.project_id)
        _validate_memory_target(item.path)

    obligation_ids = set()
    for item in index.obligations:
        if not _SAFE_POINTER_ID_RE.match(item.obligation_id):
            raise ValueError("invalid obligation id")
        if item.obligation_id in obligation_ids:
            raise ValueError("duplicate obligation id")
        obligation_ids.add(item.obligation_id)
        if item.ref:
            _validate_memory_target(item.ref)

    for pointer_id, target in index.pointers.items():
        if not _SAFE_POINTER_ID_RE.match(pointer_id):
            raise ValueError("invalid pointer id")
        _validate_memory_target(target)

    text = render_index(index)
    if len(text) > max_chars:
        raise ValueError(
            f"MEMORY_INDEX exceeds max size ({len(text)} > {max_chars} chars)"
        )


class ThinMemoryStore:
    def __init__(self, workspace_root: Path, max_index_chars: int = MEMORY_INDEX_MAX_CHARS) -> None:
        self._layout = ensure_memory_layout(workspace_root=workspace_root)
        self._max_index_chars = max(1024, int(max_index_chars))

    @property
    def layout(self) -> MemoryLayout:
        return self._layout

    def read_index_text(self) -> str:
        raw = self._layout.index_path.read_text(encoding="utf-8", errors="replace")
        return raw[: self._max_index_chars]

    def load_index(self) -> ThinMemoryIndex:
        return parse_index(self._layout.index_path.read_text(encoding="utf-8", errors="replace"))

    def save_index(self, index: ThinMemoryIndex) -> ThinMemoryIndex:
        _validate_index(index=index, max_chars=self._max_index_chars)
        self._layout.index_path.write_text(render_index(index), encoding="utf-8")
        return index

    def append_daily(self, text: str, on_date: Optional[date] = None) -> Path:
        d = on_date or date.today()
        p = self._layout.daily_dir / f"{d.isoformat()}.md"
        existing = p.read_text(encoding="utf-8", errors="replace") if p.exists() else ""
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        block = f"\n## {ts}\n{(text or '').strip()}\n"
        candidate = existing + block
        if len(candidate) > MEMORY_DAILY_MAX_CHARS:
            raise ValueError("daily memory log size limit exceeded")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(candidate, encoding="utf-8")
        return p
