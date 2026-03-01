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
class TaskItem:
    task_id: str
    done: bool
    title: str
    due: str = ""
    tags: List[str] = field(default_factory=list)
    details: str = ""
    created_at: str = ""
    done_at: str = ""


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
    heartbeat = memory_dir / "HEARTBEAT.md"
    if not heartbeat.exists():
        heartbeat.write_text(
            "# HEARTBEAT v1\n"
            "## Daily (active hours only)\n"
            "- [ ] Review today's obligations\n"
            "- [ ] Summarize ongoing missions\n"
            "## Weekly\n"
            "- [ ] Weekly review\n"
            "## Monitors\n"
            "- [ ] Check GitHub issues assigned to me\n"
            "## Waiting on\n"
            "- [ ] Replies from clients\n"
            "## Quiet Hours\n"
            "- start: 22:00\n"
            "- end: 08:00\n",
            encoding="utf-8",
        )

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
        index = self.load_index()
        pointer_id = "D" + d.isoformat()
        target = f"memory/daily/{d.isoformat()}.md"
        _upsert_pointer(index=index, pointer_id=pointer_id, target=target)
        self.save_index(index)
        return p

    def list_pages(self, prefix: str = "") -> List[Dict[str, object]]:
        normalized_prefix = (prefix or "").strip().lower()
        index = self.load_index()
        pointer_map: Dict[str, List[str]] = {}
        for pointer_id, target in index.pointers.items():
            path_only = target.split("#", 1)[0].strip()
            pointer_map.setdefault(path_only, []).append(pointer_id)
        out: List[Dict[str, object]] = []
        for path in sorted(self._layout.pages_dir.glob("**/*.md")):
            rel = str(path.relative_to(self._layout.root))
            pointer_ids = sorted(pointer_map.get(rel, []))
            bucket = f"{rel} {' '.join(pointer_ids)}".lower()
            if normalized_prefix and normalized_prefix not in bucket:
                continue
            out.append(
                {
                    "path": rel,
                    "pointer_ids": pointer_ids,
                }
            )
        return out

    def tasks_page_path(self) -> Path:
        path = self._layout.pages_dir / "tasks.md"
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("# Tasks\n\n", encoding="utf-8")
        return path

    def list_tasks(self, filter_text: str = "") -> List[TaskItem]:
        text = self.tasks_page_path().read_text(encoding="utf-8", errors="replace")
        tasks = _parse_tasks_md(text)
        query = (filter_text or "").strip().lower()
        if not query:
            return tasks
        out: List[TaskItem] = []
        for task in tasks:
            blob = " ".join(
                [
                    task.task_id,
                    task.title,
                    task.due,
                    ",".join(task.tags),
                    task.details,
                    ("done" if task.done else "open"),
                ]
            ).lower()
            if query in blob:
                out.append(task)
        return out

    def create_task(
        self,
        *,
        title: str,
        due: str = "",
        details: str = "",
        tags: Optional[List[str]] = None,
    ) -> TaskItem:
        normalized_title = (title or "").strip()
        if not normalized_title:
            raise ValueError("title is required")
        clean_tags = [str(x).strip().lower() for x in list(tags or []) if str(x).strip()]
        task_id = _next_task_id(self.list_tasks(), on_date=date.today())
        created_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        line = _render_task_line(
            TaskItem(
                task_id=task_id,
                done=False,
                title=normalized_title,
                due=(due or "").strip(),
                tags=clean_tags,
                details=(details or "").strip(),
                created_at=created_at,
                done_at="",
            )
        )
        path = self.tasks_page_path()
        existing = path.read_text(encoding="utf-8", errors="replace")
        path.write_text(existing.rstrip() + "\n" + line + "\n", encoding="utf-8")

        pointer_target = "memory/pages/tasks.md#" + task_id.lower()
        patch = {
            "obligations": {
                "upsert": [
                    {
                        "obligation_id": task_id,
                        "text": normalized_title,
                        "due": (due or "").strip(),
                        "ref": pointer_target,
                    }
                ]
            },
            "pointers": {"set": {task_id: pointer_target}},
        }
        self.update_index_patch(patch)
        return TaskItem(
            task_id=task_id,
            done=False,
            title=normalized_title,
            due=(due or "").strip(),
            tags=clean_tags,
            details=(details or "").strip(),
            created_at=created_at,
            done_at="",
        )

    def mark_task_done(self, task_id: str) -> TaskItem:
        normalized = (task_id or "").strip()
        if not normalized:
            raise ValueError("task_id is required")
        path = self.tasks_page_path()
        tasks = self.list_tasks()
        target = next((t for t in tasks if t.task_id == normalized), None)
        if target is None:
            raise ValueError("task not found")
        if target.done:
            return target
        done_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        updated = TaskItem(
            task_id=target.task_id,
            done=True,
            title=target.title,
            due=target.due,
            tags=list(target.tags),
            details=target.details,
            created_at=target.created_at,
            done_at=done_at,
        )
        refreshed: List[TaskItem] = []
        for task in tasks:
            if task.task_id == normalized:
                refreshed.append(updated)
            else:
                refreshed.append(task)
        lines = ["# Tasks", ""]
        for task in refreshed:
            lines.append(_render_task_line(task))
        path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
        self.update_index_patch(
            {
                "obligations": {"remove": [normalized]},
            }
        )
        return updated

    def open_pointer(self, pointer_id: str, max_chars: int = 12_000) -> Dict[str, str]:
        normalized_pointer = (pointer_id or "").strip()
        if not normalized_pointer:
            raise ValueError("pointer_id is required")
        index = self.load_index()
        target = index.pointers.get(normalized_pointer)
        if not target:
            raise ValueError(f"unknown pointer_id: {normalized_pointer}")
        target_path, anchor = _split_target(target)
        resolved = (self._layout.root / target_path).resolve()
        if not _is_within(resolved, self._layout.memory_dir):
            raise ValueError("pointer target escapes memory root")
        if not resolved.exists() or not resolved.is_file():
            raise ValueError("pointer target does not exist")
        raw = resolved.read_text(encoding="utf-8", errors="replace")
        excerpt = _extract_anchor_excerpt(raw, anchor=anchor)
        cap = max(100, min(int(max_chars or 12_000), 20_000))
        return {
            "pointer_id": normalized_pointer,
            "target": target,
            "excerpt": excerpt[:cap],
        }

    def update_index_patch(self, patch: Dict[str, object]) -> ThinMemoryIndex:
        if not isinstance(patch, dict):
            raise ValueError("patch must be an object")
        index = self.load_index()

        _apply_kv_section_patch(index.identity, patch.get("identity"), max_items=50)
        _apply_kv_section_patch(index.preferences, patch.get("preferences"), max_items=MEMORY_INDEX_MAX_PREFERENCES)
        _apply_kv_section_patch(index.pointers, patch.get("pointers"), max_items=MEMORY_INDEX_MAX_POINTERS)

        projects_patch = patch.get("active_projects")
        if projects_patch is not None:
            index.active_projects = _apply_projects_patch(index.active_projects, projects_patch)

        obligations_patch = patch.get("obligations")
        if obligations_patch is not None:
            index.obligations = _apply_obligations_patch(index.obligations, obligations_patch)

        # Enforce that pointers are declared only through the Pointers section.
        known_project_ids = {p.project_id for p in index.active_projects}
        for pid in known_project_ids:
            if pid not in index.pointers:
                project = next((p for p in index.active_projects if p.project_id == pid), None)
                if project is not None:
                    _upsert_pointer(index=index, pointer_id=pid, target=project.path)

        self.save_index(index)
        return index


def _apply_kv_section_patch(target: Dict[str, str], patch: object, max_items: int) -> None:
    if patch is None:
        return
    if not isinstance(patch, dict):
        raise ValueError("section patch must be an object")
    replace_obj = patch.get("set_all")
    if replace_obj is not None:
        if not isinstance(replace_obj, dict):
            raise ValueError("set_all must be an object")
        target.clear()
        for key, value in replace_obj.items():
            k = str(key or "").strip()
            if not k:
                continue
            target[k] = str(value or "").strip()
    set_obj = patch.get("set")
    if set_obj is not None:
        if not isinstance(set_obj, dict):
            raise ValueError("set must be an object")
        for key, value in set_obj.items():
            k = str(key or "").strip()
            if not k:
                continue
            target[k] = str(value or "").strip()
    remove_obj = patch.get("remove")
    if remove_obj is not None:
        if not isinstance(remove_obj, list):
            raise ValueError("remove must be a list")
        for key in remove_obj:
            target.pop(str(key or "").strip(), None)
    if len(target) > max_items:
        raise ValueError("section exceeds max items")


def _apply_projects_patch(
    current: List[ActiveProject],
    patch: object,
) -> List[ActiveProject]:
    if not isinstance(patch, dict):
        raise ValueError("active_projects patch must be an object")
    rows = list(current)
    set_all = patch.get("set_all")
    if set_all is not None:
        if not isinstance(set_all, list):
            raise ValueError("active_projects.set_all must be a list")
        rows = [_project_from_dict(x) for x in set_all]
    upsert = patch.get("upsert")
    if upsert is not None:
        if not isinstance(upsert, list):
            raise ValueError("active_projects.upsert must be a list")
        by_id = {row.project_id: row for row in rows}
        for item in upsert:
            row = _project_from_dict(item)
            by_id[row.project_id] = row
        rows = [by_id[k] for k in sorted(by_id.keys())]
    remove = patch.get("remove")
    if remove is not None:
        if not isinstance(remove, list):
            raise ValueError("active_projects.remove must be a list")
        remove_set = {str(x or "").strip() for x in remove if str(x or "").strip()}
        rows = [row for row in rows if row.project_id not in remove_set]
    dedup: Dict[str, ActiveProject] = {}
    for row in rows:
        dedup[row.project_id] = row
    if len(dedup) > MEMORY_INDEX_MAX_ACTIVE_PROJECTS:
        raise ValueError("Active Projects section exceeds max items")
    result = list(dedup.values())
    return sorted(result, key=lambda x: x.project_id)


def _apply_obligations_patch(
    current: List[Obligation],
    patch: object,
) -> List[Obligation]:
    if not isinstance(patch, dict):
        raise ValueError("obligations patch must be an object")
    rows = list(current)
    set_all = patch.get("set_all")
    if set_all is not None:
        if not isinstance(set_all, list):
            raise ValueError("obligations.set_all must be a list")
        rows = [_obligation_from_dict(x) for x in set_all]
    upsert = patch.get("upsert")
    if upsert is not None:
        if not isinstance(upsert, list):
            raise ValueError("obligations.upsert must be a list")
        by_id = {row.obligation_id: row for row in rows}
        for item in upsert:
            row = _obligation_from_dict(item)
            by_id[row.obligation_id] = row
        rows = [by_id[k] for k in sorted(by_id.keys())]
    remove = patch.get("remove")
    if remove is not None:
        if not isinstance(remove, list):
            raise ValueError("obligations.remove must be a list")
        remove_set = {str(x or "").strip() for x in remove if str(x or "").strip()}
        rows = [row for row in rows if row.obligation_id not in remove_set]
    dedup: Dict[str, Obligation] = {}
    for row in rows:
        dedup[row.obligation_id] = row
    if len(dedup) > MEMORY_INDEX_MAX_OBLIGATIONS:
        raise ValueError("Obligations section exceeds max items")
    result = list(dedup.values())
    return sorted(result, key=lambda x: x.obligation_id)


def _project_from_dict(value: object) -> ActiveProject:
    if not isinstance(value, dict):
        raise ValueError("project row must be an object")
    project_id = str(
        value.get("project_id")
        or value.get("id")
        or ""
    ).strip()
    title = str(value.get("title") or "").strip()
    path = str(value.get("path") or "").strip()
    if not project_id or not title or not path:
        raise ValueError("project row requires project_id/title/path")
    return ActiveProject(project_id=project_id, title=title, path=path)


def _obligation_from_dict(value: object) -> Obligation:
    if not isinstance(value, dict):
        raise ValueError("obligation row must be an object")
    obligation_id = str(
        value.get("obligation_id")
        or value.get("id")
        or ""
    ).strip()
    text = str(value.get("text") or value.get("title") or "").strip()
    due = str(value.get("due") or "").strip()
    ref = str(value.get("ref") or "").strip()
    if not obligation_id or not text:
        raise ValueError("obligation row requires obligation_id/text")
    return Obligation(obligation_id=obligation_id, text=text, due=due, ref=ref)


def _is_within(candidate: Path, root: Path) -> bool:
    try:
        candidate.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _split_target(target: str) -> tuple[str, str]:
    raw = (target or "").strip()
    if "#" not in raw:
        return raw, ""
    base, anchor = raw.split("#", 1)
    return base.strip(), anchor.strip()


def _slugify_heading(value: str) -> str:
    base = re.sub(r"[^\w\s-]", "", (value or "").strip().lower())
    return re.sub(r"\s+", "-", base).strip("-")


def _extract_anchor_excerpt(content: str, anchor: str = "") -> str:
    if not anchor:
        return content
    lines = content.splitlines()
    desired = _slugify_heading(anchor)
    start = 0
    level = 7
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if not stripped.startswith("#"):
            continue
        heading_level = len(stripped) - len(stripped.lstrip("#"))
        heading_text = stripped.lstrip("#").strip()
        if _slugify_heading(heading_text) == desired:
            start = idx
            level = heading_level
            break
    else:
        return content
    end = len(lines)
    for idx in range(start + 1, len(lines)):
        stripped = lines[idx].strip()
        if not stripped.startswith("#"):
            continue
        heading_level = len(stripped) - len(stripped.lstrip("#"))
        if heading_level <= level:
            end = idx
            break
    return "\n".join(lines[start:end]).strip()


def _upsert_pointer(index: ThinMemoryIndex, pointer_id: str, target: str) -> None:
    pid = (pointer_id or "").strip()
    tgt = (target or "").strip()
    if not pid or not tgt:
        raise ValueError("pointer id and target are required")
    if pid not in index.pointers and len(index.pointers) >= MEMORY_INDEX_MAX_POINTERS:
        daily_keys = sorted([k for k in index.pointers.keys() if k.startswith("D")])
        if daily_keys:
            index.pointers.pop(daily_keys[0], None)
        else:
            raise ValueError("pointer section is full")
    index.pointers[pid] = tgt


def _parse_tasks_md(content: str) -> List[TaskItem]:
    out: List[TaskItem] = []
    for raw_line in (content or "").splitlines():
        line = raw_line.strip()
        m = re.match(r"^- \[( |x)\]\s+([A-Za-z0-9_-]+)\s+\|\s+(.+)$", line)
        if not m:
            continue
        done = m.group(1) == "x"
        task_id = m.group(2).strip()
        tail = m.group(3).strip()
        parts = [p.strip() for p in tail.split("|")]
        title = parts[0] if parts else ""
        due = ""
        tags: List[str] = []
        details = ""
        created_at = ""
        done_at = ""
        for part in parts[1:]:
            lowered = part.lower()
            if lowered.startswith("due:"):
                due = part[4:].strip()
            elif lowered.startswith("tags:"):
                tags = [x.strip().lower() for x in part[5:].split(",") if x.strip()]
            elif lowered.startswith("details:"):
                details = part[8:].strip()
            elif lowered.startswith("created:"):
                created_at = part[8:].strip()
            elif lowered.startswith("done:"):
                done_at = part[5:].strip()
        out.append(
            TaskItem(
                task_id=task_id,
                done=done,
                title=title,
                due=due,
                tags=tags,
                details=details,
                created_at=created_at,
                done_at=done_at,
            )
        )
    return out


def _next_task_id(tasks: List[TaskItem], on_date: date) -> str:
    prefix = "T" + on_date.strftime("%Y%m%d") + "-"
    max_num = 0
    for task in tasks:
        if not task.task_id.startswith(prefix):
            continue
        tail = task.task_id[len(prefix):]
        if not tail.isdigit():
            continue
        max_num = max(max_num, int(tail))
    return prefix + f"{max_num + 1:04d}"


def _render_task_line(task: TaskItem) -> str:
    check = "x" if task.done else " "
    parts = [task.title]
    if task.due:
        parts.append(f"due: {task.due}")
    if task.tags:
        parts.append(f"tags: {','.join(task.tags)}")
    if task.details:
        parts.append(f"details: {task.details}")
    if task.created_at:
        parts.append(f"created: {task.created_at}")
    if task.done and task.done_at:
        parts.append(f"done: {task.done_at}")
    return f"- [{check}] {task.task_id} | " + " | ".join(parts)
