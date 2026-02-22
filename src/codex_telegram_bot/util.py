import os
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Iterable, List

DEFAULT_REPLACEMENT = "REDACTED"
_DEFAULT_PATTERNS = (
    (r"sk-[A-Za-z0-9_-]{10,}", "sk-REDACTED"),
    (r"gh[pousr]_[A-Za-z0-9]{20,}", "gh-REDACTED"),
    (r"github_pat_[A-Za-z0-9_]{20,}", "github_pat_REDACTED"),
    (r"AKIA[0-9A-Z]{16}", "AKIA_REDACTED"),
    (r"(?i)\bBearer\s+[A-Za-z0-9\-._~+/]+=*\b", "Bearer REDACTED"),
    (
        r"(?i)\b([A-Z0-9_]*(?:api[_-]?key|token|secret|password))\b\s*[:=]\s*([^\s,;]+)",
        r"\1=REDACTED",
    ),
)
_EXTRA_PATTERNS_ENV = "REDACTION_EXTRA_PATTERNS"


@dataclass(frozen=True)
class RedactionResult:
    text: str
    redacted: bool
    replacements: int


def redact(text: str) -> str:
    return redact_with_audit(text).text


def redact_with_audit(text: str) -> RedactionResult:
    value = text or ""
    total = 0
    for regex, replacement in _compiled_patterns():
        value, count = regex.subn(replacement, value)
        total += count
    return RedactionResult(text=value, redacted=total > 0, replacements=total)


@lru_cache(maxsize=2)
def _compiled_patterns() -> List[tuple[re.Pattern[str], str]]:
    items: List[tuple[re.Pattern[str], str]] = [
        (re.compile(pattern), replacement) for pattern, replacement in _DEFAULT_PATTERNS
    ]
    extra_raw = (os.environ.get(_EXTRA_PATTERNS_ENV) or "").strip()
    if not extra_raw:
        return items
    for token in extra_raw.split(";;"):
        pattern = token.strip()
        if not pattern:
            continue
        try:
            items.append((re.compile(pattern), DEFAULT_REPLACEMENT))
        except re.error:
            continue
    return items


def chunk_text(text: str, max_len: int) -> List[str]:
    if max_len <= 0:
        return [text]
    return [text[i:i + max_len] for i in range(0, len(text), max_len)]


def iter_lines(text: str) -> Iterable[str]:
    for line in text.splitlines():
        yield line
