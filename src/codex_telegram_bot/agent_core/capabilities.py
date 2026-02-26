from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Sequence


@dataclass(frozen=True)
class CapabilitySummary:
    name: str
    summary: str


class MarkdownCapabilityRegistry:
    """Lean capability registry that injects only selective summaries."""

    def __init__(self, root: Path):
        self._root = root
        self._keyword_map: Dict[str, List[str]] = {
            "system": ["system", "policy", "instruction"],
            "git": ["git", "commit", "branch", "diff", "repo"],
            "files": ["file", "read", "write", "path", "directory"],
        }
        self._tool_cluster_map: Dict[str, str] = {
            "read_file": "files",
            "write_file": "files",
            "git_status": "git",
            "git_diff": "git",
            "git_log": "git",
            "git_add": "git",
            "git_commit": "git",
            "shell_exec": "shell",
            "exec": "shell",
            "ssh_detect": "ssh_vps",
            "send_email_smtp": "email",
            "provider_status": "provider_model",
            "provider_switch": "provider_model",
        }
        self._cluster_summaries: Dict[str, tuple[str, Sequence[str]]] = {
            "files": (
                "Files capability",
                (
                    "Use `!tool` read/write calls for workspace-local file operations.",
                    "Prefer minimal reads/writes and report concrete paths changed.",
                ),
            ),
            "git": (
                "Git capability",
                (
                    "Use git status/diff/log first; only stage/commit when needed for the goal.",
                    "Describe repository impact briefly with exact file references.",
                ),
            ),
            "shell": (
                "Shell capability",
                (
                    "Use `!exec` for deterministic shell actions and keep commands explicit.",
                    "Avoid destructive commands unless explicitly requested or approved.",
                ),
            ),
            "ssh_vps": (
                "SSH/VPS capability",
                (
                    "Use SSH connectivity checks before remote-changing operations.",
                    "Surface missing key/agent prerequisites only when they block progress.",
                ),
            ),
            "email": (
                "Email capability",
                (
                    "Use `!tool` send_email_smtp with explicit `to`, `subject`, and `body`.",
                    "Treat outbound send as approval-gated and confirm result concisely.",
                ),
            ),
            "provider_model": (
                "Provider/model capability",
                (
                    "Use provider status/switch tools when reliability or capability mismatch blocks work.",
                    "Prefer minimal provider changes and state why the switch was needed.",
                ),
            ),
        }

    def summarize_for_prompt(self, prompt: str, max_capabilities: int = 2) -> List[CapabilitySummary]:
        candidates = self._select_capabilities(prompt=prompt, limit=max_capabilities)
        out: List[CapabilitySummary] = []
        for name in candidates:
            summary = self._summarize_capability(name=name)
            if summary:
                out.append(CapabilitySummary(name=name, summary=summary))
        return out

    def summarize_for_tools(self, tool_names: Sequence[str], max_capabilities: int = 4) -> List[CapabilitySummary]:
        clusters: List[str] = []
        for raw in tool_names or []:
            name = str(raw or "").strip().lower()
            if not name:
                continue
            cluster = self._tool_cluster_map.get(name)
            if cluster and cluster not in clusters:
                clusters.append(cluster)
            if len(clusters) >= max_capabilities:
                break
        out: List[CapabilitySummary] = []
        for cluster in clusters[:max_capabilities]:
            summary = self._summarize_cluster(cluster)
            if summary:
                out.append(CapabilitySummary(name=cluster, summary=summary))
        return out

    def _select_capabilities(self, prompt: str, limit: int) -> List[str]:
        low = (prompt or "").lower()
        scored: List[tuple[int, str]] = []
        for name, keywords in self._keyword_map.items():
            score = sum(1 for kw in keywords if kw in low)
            if score > 0:
                scored.append((score, name))
        scored.sort(key=lambda x: (-x[0], x[1]))
        if scored:
            return [name for _, name in scored[:limit]]
        return ["system"][:limit]

    def _summarize_capability(self, name: str) -> str:
        path = self._root / f"{name}.md"
        if not path.exists() or not path.is_file():
            return ""
        text = path.read_text(encoding="utf-8", errors="replace")[:1800]
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        if not lines:
            return ""
        header = ""
        bullets: List[str] = []
        for ln in lines:
            if ln.startswith("#") and not header:
                header = re.sub(r"^#+\s*", "", ln).strip()
                continue
            if ln.startswith("- "):
                bullets.append(ln[2:].strip())
            if len(bullets) >= 3:
                break
        summary_bits = [f"{name} capability"]
        if header:
            summary_bits.append(header)
        if bullets:
            summary_bits.append("; ".join(bullets[:3]))
        return " - ".join(summary_bits)

    def _summarize_cluster(self, name: str) -> str:
        packed = self._cluster_summaries.get(name)
        if not packed:
            return ""
        header, bullets = packed
        chosen = [b.strip() for b in bullets if b.strip()][:2]
        if not chosen:
            return header
        return "\n".join([header, *(f"- {item}" for item in chosen)])
