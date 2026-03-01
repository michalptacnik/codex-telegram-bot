from __future__ import annotations

import os
import re

AUTO_CONTINUE_PRELIMINARY_ENV = "AUTO_CONTINUE_PRELIMINARY"
AUTO_CONTINUE_PRELIMINARY_MAX_PASSES_ENV = "AUTO_CONTINUE_PRELIMINARY_MAX_PASSES"

PRELIMINARY_CONTINUE_PROMPT = (
    "Your previous message was a preliminary progress update, not a final outcome. "
    "Continue executing the same task now. Do not stop at status notes. "
    "Return only when either (a) the task is done with concrete results, or (b) you are blocked and ask one short question."
)

PRELIMINARY_TERMINAL_FALLBACK = (
    "I have not finished this task yet. I paused to avoid sending another preliminary progress report. "
    "Reply with 'continue' and I will resume immediately from the current state."
)

_PRELIM_MARKERS = (
    "still working",
    "i'm working",
    "i am working",
    "i didn't stop",
    "i was just trying",
    "continuing",
    "trying different approach",
    "let me check",
    "let me try",
    "let me first",
    "i'll check",
    "i will check",
    "i'll search",
    "i will search",
    "i'll continue",
    "i will continue",
    "next i'll",
    "next i will",
    "working on it",
)

_FINAL_MARKERS = (
    "final status",
    "what i discovered",
    "what i found",
    "here's what i found",
    "done:",
    "completed",
    "task complete",
    "summary:",
)


def auto_continue_preliminary_enabled() -> bool:
    raw = (os.environ.get(AUTO_CONTINUE_PRELIMINARY_ENV) or "1").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def auto_continue_preliminary_max_passes() -> int:
    raw = (os.environ.get(AUTO_CONTINUE_PRELIMINARY_MAX_PASSES_ENV) or "2").strip()
    try:
        value = int(raw)
    except Exception:
        return 2
    return min(4, max(0, value))


def looks_like_preliminary_report(text: str) -> bool:
    raw = str(text or "").strip()
    if not raw:
        return False
    low = raw.lower()
    if low.startswith("error:"):
        return False
    if "approval required" in low or "approve once: /approve" in low:
        return False
    if _looks_like_blocking_question(raw):
        return False
    if any(marker in low for marker in _FINAL_MARKERS):
        return False
    if any(marker in low for marker in _PRELIM_MARKERS):
        # "let me know" is usually a closing phrase, not progress continuation.
        if "let me know" in low and not any(m in low for m in ("still working", "continuing", "trying")):
            return False
        return True
    # Fallback: detect explicit future-action phrasing without completion markers.
    if re.search(r"\b(i'll|i will|let me|next i(?:'ll| will))\b", low) and "completed" not in low and "done" not in low:
        return True
    return False


def continuation_status_line(previous_text: str) -> str:
    low = str(previous_text or "").lower()
    if any(token in low for token in ("error", "failed", "blocked")):
        return "I am continuing and trying a different approach."
    if any(token in low for token in ("search", "find", "lookup")):
        return "I am continuing and trying a different search approach."
    if any(token in low for token in ("check", "inspect", "verify")):
        return "I am continuing and verifying the next path."
    return "I am continuing the task now."


def sanitize_terminal_output(text: str) -> str:
    raw = str(text or "").strip()
    if not raw:
        return raw
    if looks_like_preliminary_report(raw):
        return PRELIMINARY_TERMINAL_FALLBACK
    return raw


def _looks_like_blocking_question(text: str) -> bool:
    raw = str(text or "").strip()
    if not raw:
        return False
    if raw.endswith("?"):
        return True
    first = raw.splitlines()[0].strip().lower()
    starters = ("can you", "should i", "do you want", "which ", "what ", "where ")
    return any(first.startswith(marker) for marker in starters)
