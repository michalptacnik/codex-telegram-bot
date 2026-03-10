"""Profile management for per-chat backend/mode/permission configuration.

Profiles are defined in the bot configuration YAML under the ``profiles:``
section.  Each profile specifies a backend, optional default agent/mode,
permission settings, and optional instruction pack paths.

Example YAML::

    profiles:
      codex-default:
        backend: codex
        default_permissions:
          approval_policy: balanced
          sandbox: workspace-write
      opencode-build:
        backend: opencode
        default_mode: build
        default_permissions:
          rules:
            - allow: "read *"
            - deny: "write /etc/*"
        instruction_packs:
          - /path/to/pack.yaml
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProfilePermissions:
    """Permission settings for a profile."""

    approval_policy: str = "balanced"
    sandbox: str = "workspace-write"
    rules: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class ProfileConfig:
    """A named profile definition from configuration."""

    name: str
    backend: str = "codex"
    default_mode: str = ""
    default_agent: str = ""
    permissions: ProfilePermissions = field(default_factory=ProfilePermissions)
    instruction_packs: List[str] = field(default_factory=list)


def parse_profiles_config(raw: Dict[str, Any]) -> Dict[str, ProfileConfig]:
    """Parse the ``profiles:`` section of the YAML configuration.

    Returns a dict mapping profile name to ``ProfileConfig``.
    """
    result: Dict[str, ProfileConfig] = {}
    profiles_raw = raw.get("profiles") or {}
    if not isinstance(profiles_raw, dict):
        return result

    for name, spec in profiles_raw.items():
        name = str(name).strip()
        if not name or not isinstance(spec, dict):
            continue

        perms_raw = spec.get("default_permissions") or {}
        if not isinstance(perms_raw, dict):
            perms_raw = {}

        rules_raw = perms_raw.get("rules") or []
        rules = []
        if isinstance(rules_raw, list):
            for entry in rules_raw:
                if isinstance(entry, dict):
                    for action, pattern in entry.items():
                        rules.append(f"{action} {pattern}")
                elif isinstance(entry, str):
                    rules.append(entry)

        permissions = ProfilePermissions(
            approval_policy=str(perms_raw.get("approval_policy", "balanced")).strip(),
            sandbox=str(perms_raw.get("sandbox", "workspace-write")).strip(),
            rules=rules,
        )

        packs_raw = spec.get("instruction_packs") or []
        packs = [str(p) for p in packs_raw] if isinstance(packs_raw, list) else []

        result[name] = ProfileConfig(
            name=name,
            backend=str(spec.get("backend", "codex")).strip(),
            default_mode=str(spec.get("default_mode", "")).strip(),
            default_agent=str(spec.get("default_agent", "")).strip(),
            permissions=permissions,
            instruction_packs=packs,
        )

    return result


def load_profiles_yaml(path: Path) -> Dict[str, ProfileConfig]:
    """Load profiles from a YAML configuration file.

    Falls back to an empty dict if the file is missing, unreadable, or invalid.
    """
    if not path.exists():
        return {}
    try:
        import yaml  # noqa: F811

        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except ImportError:
        # Fallback: try JSON if PyYAML not available.
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
    except Exception as exc:
        logger.error("Failed to load profiles config from %s: %s", path, exc)
        return {}

    if not isinstance(raw, dict):
        return {}
    return parse_profiles_config(raw)


class ChatProfileStore:
    """Persistence layer for per-chat active profile selection (SQLite-backed)."""

    def __init__(self, store: Any) -> None:
        self._store = store

    def get_profile(self, chat_id: int) -> Optional[str]:
        """Return the active profile name for a chat, or None if not set."""
        if self._store is None:
            return None
        return self._store.get_chat_profile(chat_id)

    def set_profile(self, chat_id: int, profile_name: str) -> None:
        """Set the active profile for a chat."""
        if self._store is None:
            return
        self._store.set_chat_profile(chat_id, profile_name)

    def clear_profile(self, chat_id: int) -> None:
        """Remove profile selection for a chat (revert to default)."""
        if self._store is None:
            return
        self._store.clear_chat_profile(chat_id)
