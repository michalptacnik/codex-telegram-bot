from __future__ import annotations

import difflib
import hashlib
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple

SOUL_MAX_CHARS = max(
    512,
    int((os.environ.get("SOUL_MAX_CHARS") or "2000").strip() or "2000"),
)
SOUL_MAX_BULLETS = 5
SOUL_MAX_BULLET_CHARS = 90

_ALLOWED_EMOJI = {"off", "light", "on"}
_ALLOWED_EMPHASIS = {"plain", "light", "rich"}
_ALLOWED_BREVITY = {"short", "normal"}

_LINE_RE = re.compile(r"^[\x09\x0A\x0D\x20-\x7E]*$")


@dataclass(frozen=True)
class SoulStyle:
    emoji: str = "light"
    emphasis: str = "light"
    brevity: str = "short"


@dataclass(frozen=True)
class SoulProfile:
    name: str
    voice: str
    principles: List[str] = field(default_factory=list)
    boundaries: List[str] = field(default_factory=list)
    style: SoulStyle = SoulStyle()


@dataclass(frozen=True)
class SoulValidation:
    ok: bool
    warnings: List[str] = field(default_factory=list)


def default_soul_profile() -> SoulProfile:
    return SoulProfile(
        name="Clawlet",
        voice="calm nerdy direct",
        principles=[
            "Be truthful; flag uncertainty.",
            "Optimize for safety + legality.",
            "Prefer small, testable steps.",
            "Keep outputs lean and readable.",
        ],
        boundaries=[
            "No scams, evasion, or covert harm.",
            "Don't run risky tools without approval.",
            "Don't expose secrets or private data.",
            "Don't invent facts.",
        ],
        style=SoulStyle(emoji="light", emphasis="light", brevity="short"),
    )


def render_soul(profile: SoulProfile) -> str:
    lines: List[str] = [
        "# SOUL v1",
        f"name: {profile.name}",
        f"voice: {profile.voice}",
        "principles:",
    ]
    for bullet in profile.principles[:SOUL_MAX_BULLETS]:
        lines.append(f"  - {bullet}")
    lines.append("boundaries:")
    for bullet in profile.boundaries[:SOUL_MAX_BULLETS]:
        lines.append(f"  - {bullet}")
    lines.extend(
        [
            "style:",
            f"  emoji: {profile.style.emoji}",
            f"  emphasis: {profile.style.emphasis}",
            f"  brevity: {profile.style.brevity}",
        ]
    )
    return "\n".join(lines).strip() + "\n"


def _validate_ascii_text(text: str) -> None:
    for line in (text or "").splitlines():
        if not _LINE_RE.match(line):
            raise ValueError("SOUL.md must be ASCII-safe")


def _validate_profile(profile: SoulProfile, *, max_chars: int = SOUL_MAX_CHARS) -> None:
    if not profile.name or len(profile.name) > 40:
        raise ValueError("name must be 1-40 chars")
    voice_word_count = len([w for w in profile.voice.split() if w.strip()])
    if voice_word_count < 3 or voice_word_count > 8:
        raise ValueError("voice must be 3-8 words")

    for section_name, items in (("principles", profile.principles), ("boundaries", profile.boundaries)):
        if len(items) > SOUL_MAX_BULLETS:
            raise ValueError(f"{section_name} exceeds max bullets")
        for item in items:
            if not item.strip():
                raise ValueError(f"{section_name} bullets cannot be empty")
            if len(item) > SOUL_MAX_BULLET_CHARS:
                raise ValueError(f"{section_name} bullet exceeds {SOUL_MAX_BULLET_CHARS} chars")

    if profile.style.emoji not in _ALLOWED_EMOJI:
        raise ValueError("style.emoji must be off|light|on")
    if profile.style.emphasis not in _ALLOWED_EMPHASIS:
        raise ValueError("style.emphasis must be plain|light|rich")
    if profile.style.brevity not in _ALLOWED_BREVITY:
        raise ValueError("style.brevity must be short|normal")

    rendered = render_soul(profile)
    _validate_ascii_text(rendered)
    if len(rendered) > int(max_chars):
        raise ValueError(f"SOUL.md exceeds max size ({len(rendered)} > {max_chars})")


def parse_soul(text: str) -> SoulProfile:
    if len(text or "") > SOUL_MAX_CHARS:
        raise ValueError(f"SOUL.md exceeds max size ({len(text)} > {SOUL_MAX_CHARS})")
    _validate_ascii_text(text or "")
    lines = (text or "").splitlines()
    if not lines or lines[0].strip() != "# SOUL v1":
        raise ValueError("SOUL.md must start with '# SOUL v1'")

    section = "root"
    name = ""
    voice = ""
    principles: List[str] = []
    boundaries: List[str] = []
    style: Dict[str, str] = {}

    for raw in lines[1:]:
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped:
            continue
        if stripped == "principles:":
            section = "principles"
            continue
        if stripped == "boundaries:":
            section = "boundaries"
            continue
        if stripped == "style:":
            section = "style"
            continue

        if section in {"principles", "boundaries"}:
            if not stripped.startswith("- "):
                raise ValueError(f"invalid bullet in {section}")
            item = stripped[2:].strip()
            if section == "principles":
                principles.append(item)
            else:
                boundaries.append(item)
            continue

        if ":" not in stripped:
            raise ValueError("invalid key/value line")
        key, _, value = stripped.partition(":")
        key = key.strip().lower()
        value = value.strip()
        if section == "style":
            if key not in {"emoji", "emphasis", "brevity"}:
                raise ValueError("invalid style key")
            style[key] = value
            continue
        if key == "name":
            name = value
            continue
        if key == "voice":
            voice = value
            continue
        raise ValueError(f"unexpected key '{key}'")

    profile = SoulProfile(
        name=name,
        voice=voice,
        principles=principles,
        boundaries=boundaries,
        style=SoulStyle(
            emoji=(style.get("emoji") or "light").strip().lower(),
            emphasis=(style.get("emphasis") or "light").strip().lower(),
            brevity=(style.get("brevity") or "short").strip().lower(),
        ),
    )
    _validate_profile(profile)
    return profile


def ensure_soul_file(workspace_root: Path) -> Path:
    ws = Path(workspace_root).expanduser().resolve()
    memory_dir = ws / "memory"
    memory_dir.mkdir(parents=True, exist_ok=True)
    soul_path = memory_dir / "SOUL.md"
    if not soul_path.exists():
        soul_path.write_text(render_soul(default_soul_profile()), encoding="utf-8")
    return soul_path


def _apply_bullet_patch(current: List[str], patch: Any, section_name: str) -> List[str]:
    values = list(current)
    if isinstance(patch, list):
        values = [str(item or "").strip() for item in patch if str(item or "").strip()]
        return values
    if not isinstance(patch, dict):
        raise ValueError(f"{section_name} patch must be a list or object")
    if "set_all" in patch:
        payload = patch.get("set_all")
        if not isinstance(payload, list):
            raise ValueError(f"{section_name}.set_all must be a list")
        values = [str(item or "").strip() for item in payload if str(item or "").strip()]
    for item in list(patch.get("add") or []):
        value = str(item or "").strip()
        if value and value not in values:
            values.append(value)
    remove_set = {str(item or "").strip() for item in list(patch.get("remove") or []) if str(item or "").strip()}
    if remove_set:
        values = [item for item in values if item not in remove_set]
    return values


def apply_patch(profile: SoulProfile, patch: Dict[str, Any]) -> SoulProfile:
    if not isinstance(patch, dict):
        raise ValueError("patch must be an object")
    allowed = {"name", "voice", "principles", "boundaries", "style"}
    extras = sorted(set(patch.keys()) - allowed)
    if extras:
        raise ValueError(f"unsupported patch keys: {', '.join(extras)}")

    name = str(patch.get("name", profile.name) or "").strip()
    voice = str(patch.get("voice", profile.voice) or "").strip()
    principles = (
        _apply_bullet_patch(profile.principles, patch["principles"], "principles")
        if "principles" in patch
        else list(profile.principles)
    )
    boundaries = (
        _apply_bullet_patch(profile.boundaries, patch["boundaries"], "boundaries")
        if "boundaries" in patch
        else list(profile.boundaries)
    )
    style_patch = patch.get("style") if isinstance(patch.get("style"), dict) else {}
    if patch.get("style") is not None and not isinstance(patch.get("style"), dict):
        raise ValueError("style patch must be an object")
    style = SoulStyle(
        emoji=str(style_patch.get("emoji", profile.style.emoji) or "").strip().lower(),
        emphasis=str(style_patch.get("emphasis", profile.style.emphasis) or "").strip().lower(),
        brevity=str(style_patch.get("brevity", profile.style.brevity) or "").strip().lower(),
    )
    updated = SoulProfile(
        name=name,
        voice=voice,
        principles=principles,
        boundaries=boundaries,
        style=style,
    )
    _validate_profile(updated)
    return updated


class SoulStore:
    def __init__(self, workspace_root: Path, max_chars: int = SOUL_MAX_CHARS) -> None:
        self._workspace_root = Path(workspace_root).expanduser().resolve()
        self._max_chars = max(512, int(max_chars))
        self._soul_path = ensure_soul_file(self._workspace_root)

    @property
    def soul_path(self) -> Path:
        return self._soul_path

    def read_raw_text(self) -> str:
        if not self._soul_path.exists():
            self._soul_path = ensure_soul_file(self._workspace_root)
        return self._soul_path.read_text(encoding="utf-8")

    def read_text(self) -> str:
        raw = self.read_raw_text()
        profile, _ = self.load_profile_with_report()
        rendered = render_soul(profile)
        if rendered != raw:
            self._soul_path.write_text(rendered, encoding="utf-8")
        return rendered

    def load_profile_with_report(self) -> Tuple[SoulProfile, SoulValidation]:
        warnings: List[str] = []
        raw = self.read_raw_text()
        try:
            profile = parse_soul(raw)
            _validate_profile(profile, max_chars=self._max_chars)
            return profile, SoulValidation(ok=True, warnings=[])
        except Exception as exc:
            warnings.append(f"invalid_soul_fallback: {exc}")
            profile = default_soul_profile()
            return profile, SoulValidation(ok=False, warnings=warnings)

    def load_profile(self) -> SoulProfile:
        profile, _ = self.load_profile_with_report()
        return profile

    def propose_patch(self, patch: Dict[str, Any]) -> Dict[str, Any]:
        current_text = self.read_text()
        current_profile = parse_soul(current_text)
        updated_profile = apply_patch(current_profile, patch)
        updated_text = render_soul(updated_profile)
        _validate_profile(updated_profile, max_chars=self._max_chars)
        diff = "\n".join(
            difflib.unified_diff(
                current_text.splitlines(),
                updated_text.splitlines(),
                fromfile="memory/SOUL.md",
                tofile="memory/SOUL.md",
                lineterm="",
            )
        )
        return {
            "ok": True,
            "diff": diff,
            "changed": current_text != updated_text,
            "result_chars": len(updated_text),
            "warnings": [],
        }

    def apply_patch(
        self,
        patch: Dict[str, Any],
        *,
        reason: str,
        changed_by: str,
        session_id: str,
        run_store: Any = None,
    ) -> Dict[str, Any]:
        current_text = self.read_text()
        current_profile = parse_soul(current_text)
        updated_profile = apply_patch(current_profile, patch)
        updated_text = render_soul(updated_profile)
        _validate_profile(updated_profile, max_chars=self._max_chars)

        changed = current_text != updated_text
        history_path = ""
        version_id = ""
        if changed:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            sha = hashlib.sha256(current_text.encode("utf-8")).hexdigest()[:12]
            history_dir = self._workspace_root / "memory" / ".soul_history"
            history_dir.mkdir(parents=True, exist_ok=True)
            snapshot = history_dir / f"{stamp}_{sha}.md"
            snapshot.write_text(current_text, encoding="utf-8")
            history_path = str(snapshot)
            self._soul_path.write_text(updated_text, encoding="utf-8")
            if run_store is not None and hasattr(run_store, "create_soul_version"):
                try:
                    version_id = str(
                        run_store.create_soul_version(
                            session_id=session_id,
                            sha256=hashlib.sha256(current_text.encode("utf-8")).hexdigest(),
                            changed_by=str(changed_by or ""),
                            reason=str(reason or "").strip()[:240],
                            snapshot_path=history_path,
                        )
                    )
                except Exception:
                    version_id = ""

        return {
            "ok": True,
            "changed": changed,
            "result_chars": len(updated_text),
            "history_path": history_path,
            "version_id": version_id,
        }
