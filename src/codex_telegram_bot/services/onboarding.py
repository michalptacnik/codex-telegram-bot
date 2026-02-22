import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict


@dataclass
class OnboardingStore:
    config_dir: Path | None = None

    def __post_init__(self) -> None:
        self._memory: Dict[str, Any] = _default_state()

    @property
    def file_path(self) -> Path | None:
        if self.config_dir is None:
            return None
        return self.config_dir / "onboarding.json"

    def load(self) -> Dict[str, Any]:
        fp = self.file_path
        if fp is None or not fp.exists():
            return dict(self._memory)
        try:
            raw = json.loads(fp.read_text(encoding="utf-8"))
            state = _default_state()
            state.update(raw if isinstance(raw, dict) else {})
            return state
        except Exception:
            return dict(self._memory)

    def save(self, state: Dict[str, Any]) -> None:
        fp = self.file_path
        if fp is None:
            self._memory = dict(state)
            return
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def record(self, step: str, outcome: str) -> Dict[str, Any]:
        state = self.load()
        telemetry = state.get("telemetry") or {}
        steps = telemetry.get("steps") or {}
        key = f"{step}:{outcome}"
        steps[key] = int(steps.get(key, 0)) + 1
        telemetry["steps"] = steps
        telemetry["last_event_at"] = _utc_now()
        state["telemetry"] = telemetry
        state["last_step"] = step
        self.save(state)
        return state

    def complete(self) -> Dict[str, Any]:
        state = self.load()
        state["completed"] = True
        state["completed_at"] = _utc_now()
        self.save(state)
        return state


def _default_state() -> Dict[str, Any]:
    return {
        "completed": False,
        "completed_at": "",
        "last_step": "",
        "telemetry": {
            "steps": {},
            "last_event_at": "",
        },
    }


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
