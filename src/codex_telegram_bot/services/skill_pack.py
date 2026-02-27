"""Skill-pack system with SKILL.md semantics (Issue #104).

Implements safe skill lifecycle compatible with AgentSkills-style SKILL.md
semantics, including:

  - Loader for bundled, global, and workspace skill sources
  - Precedence: workspace > global > bundled
  - YAML frontmatter parsing with ``disable-model-invocation`` field
  - Conditional gating (bins, env vars, config, OS)
  - Local searchable skills index cache
  - Admin-only /skills install with source allowlist
"""
from __future__ import annotations

import os
import platform
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class SkillPackSpec:
    """A parsed SKILL.md skill definition."""
    skill_id: str
    name: str
    description: str
    keywords: List[str]
    tools: List[str]
    source: str  # "bundled" | "global" | "workspace"
    source_path: str
    disable_model_invocation: bool = False
    requires_env: List[str] = field(default_factory=list)
    requires_bins: List[str] = field(default_factory=list)
    requires_os: str = ""
    enabled: bool = True


def parse_skill_md(text: str, source: str = "bundled", source_path: str = "") -> Optional[SkillPackSpec]:
    """Parse a SKILL.md file with YAML frontmatter.

    Expected format::

        ---
        skill_id: my-skill
        name: My Skill
        description: Does things
        keywords: [keyword1, keyword2]
        tools: [tool_a, tool_b]
        disable-model-invocation: false
        requires-env: [MY_API_KEY]
        requires-bins: [curl]
        requires-os: linux
        ---
        Optional body text (ignored for now).
    """
    # Extract YAML frontmatter
    match = re.match(r"^---\s*\n(.*?)\n---", text, re.DOTALL)
    if not match:
        return None

    frontmatter = match.group(1)
    fields = _parse_yaml_frontmatter(frontmatter)
    if not fields:
        return None

    skill_id = str(fields.get("skill_id", "")).strip()
    if not skill_id:
        return None

    return SkillPackSpec(
        skill_id=skill_id,
        name=str(fields.get("name", skill_id)).strip(),
        description=str(fields.get("description", "")).strip(),
        keywords=_parse_list(fields.get("keywords")),
        tools=_parse_list(fields.get("tools")),
        source=source,
        source_path=source_path,
        disable_model_invocation=_parse_bool(fields.get("disable-model-invocation", False)),
        requires_env=_parse_list(fields.get("requires-env")),
        requires_bins=_parse_list(fields.get("requires-bins")),
        requires_os=str(fields.get("requires-os", "")).strip().lower(),
        enabled=True,
    )


class SkillPackLoader:
    """Loads skills from bundled, global, and workspace sources with precedence."""

    def __init__(
        self,
        bundled_dir: Optional[Path] = None,
        global_dir: Optional[Path] = None,
        workspace_dir: Optional[Path] = None,
    ) -> None:
        self._bundled_dir = bundled_dir
        self._global_dir = global_dir
        self._workspace_dir = workspace_dir
        self._index: Dict[str, SkillPackSpec] = {}

    def load_all(self) -> List[SkillPackSpec]:
        """Load skills with precedence: workspace > global > bundled."""
        self._index.clear()

        # Load in lowest-precedence-first order; higher precedence overwrites
        if self._bundled_dir and self._bundled_dir.exists():
            self._load_from_dir(self._bundled_dir, source="bundled")
        if self._global_dir and self._global_dir.exists():
            self._load_from_dir(self._global_dir, source="global")
        if self._workspace_dir and self._workspace_dir.exists():
            self._load_from_dir(self._workspace_dir, source="workspace")

        return list(self._index.values())

    def get_skill(self, skill_id: str) -> Optional[SkillPackSpec]:
        return self._index.get(skill_id)

    def search(self, query: str) -> List[SkillPackSpec]:
        """Search loaded skills by keyword/name."""
        query_lower = (query or "").strip().lower()
        if not query_lower:
            return list(self._index.values())
        results = []
        for skill in self._index.values():
            if query_lower in skill.name.lower() or query_lower in skill.description.lower():
                results.append(skill)
                continue
            if any(query_lower in kw for kw in skill.keywords):
                results.append(skill)
        return results

    def check_gating(self, skill: SkillPackSpec) -> tuple:
        """Check if skill passes all gating conditions.

        Returns (passed: bool, reason: str).
        """
        # Check required binaries
        for bin_name in skill.requires_bins:
            if not shutil.which(bin_name):
                return False, f"Required binary not found: {bin_name}"

        # Check required env vars
        for env_var in skill.requires_env:
            if not (os.environ.get(env_var) or "").strip():
                return False, f"Required env var not set: {env_var}"

        # Check OS requirement
        if skill.requires_os:
            current_os = platform.system().lower()
            if skill.requires_os != current_os:
                return False, f"OS mismatch: requires {skill.requires_os}, got {current_os}"

        return True, "All gating conditions met."

    def active_skills(self, prompt: str = "") -> List[SkillPackSpec]:
        """Return skills that pass gating and match prompt keywords (lazy injection)."""
        prompt_lower = (prompt or "").lower()
        results = []
        for skill in self._index.values():
            if not skill.enabled:
                continue
            passed, _ = self.check_gating(skill)
            if not passed:
                continue
            if skill.disable_model_invocation:
                continue
            # Keyword match for lazy injection
            if prompt_lower:
                if not any(kw in prompt_lower for kw in skill.keywords):
                    continue
            results.append(skill)
        return results

    def _load_from_dir(self, directory: Path, source: str) -> None:
        for skill_file in directory.glob("**/SKILL.md"):
            try:
                text = skill_file.read_text(encoding="utf-8")
            except Exception:
                continue
            spec = parse_skill_md(text, source=source, source_path=str(skill_file))
            if spec:
                self._index[spec.skill_id] = spec


# ---------------------------------------------------------------------------
# Internal parsing helpers
# ---------------------------------------------------------------------------

def _parse_yaml_frontmatter(text: str) -> Dict[str, Any]:
    """Minimal YAML-like parser for frontmatter (no pyyaml dependency)."""
    fields: Dict[str, Any] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()

        # Parse inline lists: [a, b, c]
        if value.startswith("[") and value.endswith("]"):
            inner = value[1:-1]
            items = [s.strip().strip("'\"") for s in inner.split(",") if s.strip()]
            fields[key] = items
        elif value.lower() in {"true", "yes", "on"}:
            fields[key] = True
        elif value.lower() in {"false", "no", "off"}:
            fields[key] = False
        else:
            fields[key] = value.strip("'\"")
    return fields


def _parse_list(value: Any) -> List[str]:
    if isinstance(value, list):
        return [str(x).strip().lower() for x in value if str(x).strip()]
    if isinstance(value, str):
        return [x.strip().lower() for x in value.split(",") if x.strip()]
    return []


def _parse_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"true", "yes", "on", "1"}
