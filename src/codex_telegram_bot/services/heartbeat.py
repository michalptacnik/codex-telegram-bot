from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

from codex_telegram_bot.services.thin_memory import ThinMemoryStore

HEARTBEAT_FILENAME = "memory/HEARTBEAT.md"
HEARTBEAT_TEMPLATE = """# HEARTBEAT v1
## Daily (active hours only)
- [ ] Review today's obligations
- [ ] Summarize ongoing missions
## Weekly
- [ ] Weekly review
## Monitors
- [ ] Check GitHub issues assigned to me
## Waiting on
- [ ] Replies from clients
## Quiet Hours
- start: 22:00
- end: 08:00
"""


@dataclass
class HeartbeatConfig:
    daily: List[str] = field(default_factory=list)
    weekly: List[str] = field(default_factory=list)
    monitors: List[str] = field(default_factory=list)
    waiting_on: List[str] = field(default_factory=list)
    quiet_start: str = "22:00"
    quiet_end: str = "08:00"


@dataclass(frozen=True)
class HeartbeatDecision:
    action: str
    text: str
    quiet_hours_blocked: bool = False


def ensure_heartbeat_file(workspace_root: Path) -> Path:
    ws = Path(workspace_root).expanduser().resolve()
    target = ws / HEARTBEAT_FILENAME
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        target.write_text(HEARTBEAT_TEMPLATE, encoding="utf-8")
    return target


def parse_heartbeat(content: str) -> HeartbeatConfig:
    section = ""
    cfg = HeartbeatConfig()
    for raw in (content or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("## "):
            title = line[3:].strip().lower()
            if title.startswith("daily"):
                section = "daily"
            elif title.startswith("weekly"):
                section = "weekly"
            elif title.startswith("monitors"):
                section = "monitors"
            elif title.startswith("waiting on"):
                section = "waiting_on"
            elif title.startswith("quiet hours"):
                section = "quiet"
            else:
                section = ""
            continue
        if line.startswith("- [ ]"):
            item = line[5:].strip()
            if not item:
                continue
            if section == "daily":
                cfg.daily.append(item)
            elif section == "weekly":
                cfg.weekly.append(item)
            elif section == "monitors":
                cfg.monitors.append(item)
            elif section == "waiting_on":
                cfg.waiting_on.append(item)
            continue
        if section == "quiet" and line.startswith("- "):
            item = line[2:].strip()
            if ":" not in item:
                continue
            key, _, value = item.partition(":")
            key = key.strip().lower()
            value = value.strip()
            if key == "start" and _is_valid_hhmm(value):
                cfg.quiet_start = value
            elif key == "end" and _is_valid_hhmm(value):
                cfg.quiet_end = value
    return cfg


def render_heartbeat(cfg: HeartbeatConfig) -> str:
    lines = ["# HEARTBEAT v1", "## Daily (active hours only)"]
    lines.extend([f"- [ ] {x}" for x in cfg.daily])
    lines.append("## Weekly")
    lines.extend([f"- [ ] {x}" for x in cfg.weekly])
    lines.append("## Monitors")
    lines.extend([f"- [ ] {x}" for x in cfg.monitors])
    lines.append("## Waiting on")
    lines.extend([f"- [ ] {x}" for x in cfg.waiting_on])
    lines.append("## Quiet Hours")
    lines.append(f"- start: {cfg.quiet_start}")
    lines.append(f"- end: {cfg.quiet_end}")
    return "\n".join(lines).strip() + "\n"


class HeartbeatStore:
    def __init__(self, workspace_root: Path) -> None:
        self._workspace = Path(workspace_root).expanduser().resolve()
        self._path = ensure_heartbeat_file(self._workspace)

    @property
    def path(self) -> Path:
        return self._path

    def get_text(self) -> str:
        return self._path.read_text(encoding="utf-8", errors="replace")

    def get_config(self) -> HeartbeatConfig:
        return parse_heartbeat(self.get_text())

    def update_text(self, text: str) -> str:
        payload = (text or "").strip()
        if not payload:
            raise ValueError("heartbeat text cannot be empty")
        self._path.write_text(payload + "\n", encoding="utf-8")
        return payload + "\n"

    def update_patch(self, patch: Dict[str, object]) -> str:
        if not isinstance(patch, dict):
            raise ValueError("patch must be an object")
        cfg = self.get_config()
        for field_name in ("daily", "weekly", "monitors", "waiting_on"):
            value = patch.get(field_name)
            if value is None:
                continue
            if not isinstance(value, list):
                raise ValueError(f"{field_name} must be a list")
            cleaned = [str(x).strip() for x in value if str(x).strip()]
            setattr(cfg, field_name, cleaned[:20])
        qh = patch.get("quiet_hours")
        if qh is not None:
            if not isinstance(qh, dict):
                raise ValueError("quiet_hours must be an object")
            start = str(qh.get("start") or cfg.quiet_start).strip()
            end = str(qh.get("end") or cfg.quiet_end).strip()
            if not _is_valid_hhmm(start) or not _is_valid_hhmm(end):
                raise ValueError("quiet hours must be HH:MM format")
            cfg.quiet_start = start
            cfg.quiet_end = end
        text = render_heartbeat(cfg)
        self._path.write_text(text, encoding="utf-8")
        return text

    def evaluate(
        self,
        *,
        timezone_name: str,
        now_utc: Optional[datetime] = None,
    ) -> HeartbeatDecision:
        cfg = self.get_config()
        now = now_utc or datetime.now(timezone.utc)
        local_now = now.astimezone(ZoneInfo(timezone_name))
        if is_quiet_hours(local_now, cfg.quiet_start, cfg.quiet_end):
            return HeartbeatDecision(action="NO_ACTION", text="", quiet_hours_blocked=True)
        memory = ThinMemoryStore(self._workspace)
        index = memory.load_index()
        due_obligations: List[str] = []
        today = local_now.date().isoformat()
        for item in index.obligations:
            if item.due and item.due <= today:
                due_obligations.append(
                    f"{item.obligation_id}: {item.text}" + (f" (due {item.due})" if item.due else "")
                )
        if due_obligations:
            lines = ["Heartbeat check: obligations due or overdue:"]
            for row in due_obligations[:5]:
                lines.append(f"- {row}")
            return HeartbeatDecision(action="ACTION", text="\n".join(lines))
        open_due_tasks = []
        for task in memory.list_tasks(filter_text=""):
            if task.done:
                continue
            if task.due and task.due <= today:
                open_due_tasks.append(f"{task.task_id}: {task.title} (due {task.due})")
        if open_due_tasks:
            lines = ["Heartbeat check: tasks due or overdue:"]
            for row in open_due_tasks[:5]:
                lines.append(f"- {row}")
            return HeartbeatDecision(action="ACTION", text="\n".join(lines))
        if cfg.daily:
            lines = ["Heartbeat check: daily checklist"]
            for row in cfg.daily[:4]:
                lines.append(f"- [ ] {row}")
            return HeartbeatDecision(action="ACTION", text="\n".join(lines))
        return HeartbeatDecision(action="NO_ACTION", text="")


def _is_valid_hhmm(value: str) -> bool:
    return bool(re.match(r"^(?:[01]\d|2[0-3]):[0-5]\d$", (value or "").strip()))


def is_quiet_hours(local_now: datetime, start: str, end: str) -> bool:
    if not _is_valid_hhmm(start) or not _is_valid_hhmm(end):
        return False
    s_h, s_m = [int(x) for x in start.split(":", 1)]
    e_h, e_m = [int(x) for x in end.split(":", 1)]
    now_min = local_now.hour * 60 + local_now.minute
    start_min = s_h * 60 + s_m
    end_min = e_h * 60 + e_m
    if start_min == end_min:
        return False
    if start_min < end_min:
        return start_min <= now_min < end_min
    return now_min >= start_min or now_min < end_min
