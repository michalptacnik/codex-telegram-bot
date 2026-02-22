from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List


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

    def summarize_for_prompt(self, prompt: str, max_capabilities: int = 2) -> List[CapabilitySummary]:
        candidates = self._select_capabilities(prompt=prompt, limit=max_capabilities)
        out: List[CapabilitySummary] = []
        for name in candidates:
            summary = self._summarize_capability(name=name)
            if summary:
                out.append(CapabilitySummary(name=name, summary=summary))
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
