from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

_HEADING_RE = re.compile(r"^[A-Za-z][A-Za-z0-9 /_+-]{1,60}:\s*$")
_ENUM_RE = re.compile(r"^\s*\d+[.)]\s+")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*", flags=re.S)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")

_SAFE_EMOJIS = ("✅", "⚠️", "📌", "🧠", "🔧")
_TELEGRAM_SPECIAL_CHARS = set("_*[]()~`>#+-=|{}.!\\")

ENABLE_POLISH_PROBE = (os.environ.get("ENABLE_POLISH_PROBE", "0") or "0").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}


@dataclass(frozen=True)
class PresentationStyle:
    emoji: str = "light"
    emphasis: str = "light"
    brevity: str = "short"


@dataclass(frozen=True)
class FormattedMessage:
    formatted_text: str
    parse_mode: Optional[str]
    safety_report: Dict[str, Any]


def normalize_style(style: Optional[Dict[str, Any]] = None) -> PresentationStyle:
    payload = style if isinstance(style, dict) else {}
    emoji = str(payload.get("emoji") or "light").strip().lower()
    emphasis = str(payload.get("emphasis") or "light").strip().lower()
    brevity = str(payload.get("brevity") or "short").strip().lower()
    if emoji not in {"off", "light", "on"}:
        emoji = "light"
    if emphasis not in {"plain", "light", "rich"}:
        emphasis = "light"
    if brevity not in {"short", "normal"}:
        brevity = "short"
    return PresentationStyle(emoji=emoji, emphasis=emphasis, brevity=brevity)


def format_message(
    raw_text: str,
    *,
    channel: str,
    style: Optional[Dict[str, Any]] = None,
) -> FormattedMessage:
    source = str(raw_text or "").strip()
    if not source:
        source = "(no output)"
    style_obj = normalize_style(style)
    applied: List[str] = []

    staged = _normalize_whitespace(source)
    if staged != source:
        applied.append("normalize_whitespace")

    paragraphed = _apply_paragraphing(staged)
    if paragraphed != staged:
        applied.append("paragraphing")

    structured = _apply_structure(paragraphed, use_bold=(style_obj.emphasis != "plain"))
    if structured != paragraphed:
        applied.append("structure")

    polished = _apply_brevity(structured, brevity=style_obj.brevity)
    if polished != structured:
        applied.append("brevity")

    with_emoji, emoji_count = _apply_emoji(polished, style_obj.emoji)
    if with_emoji != polished:
        applied.append("emoji")

    if ENABLE_POLISH_PROBE:
        # Reserved for optional low-cost formatting-only probe. Default stays heuristic-only.
        applied.append("polish_probe_skipped")

    channel_key = str(channel or "web").strip().lower()
    if channel_key == "telegram":
        rendered = to_markdown_v2(with_emoji)
        parse_mode: Optional[str] = "MarkdownV2"
    else:
        rendered = with_emoji
        parse_mode = None

    report: Dict[str, Any] = {
        "channel": channel_key,
        "input_chars": len(source),
        "output_chars": len(rendered),
        "emoji_count": int(emoji_count),
        "applied": applied,
        "probe_enabled": bool(ENABLE_POLISH_PROBE),
    }
    return FormattedMessage(formatted_text=rendered, parse_mode=parse_mode, safety_report=report)


def escape_markdown_v2(text: str) -> str:
    out: List[str] = []
    for ch in str(text or ""):
        if ch in _TELEGRAM_SPECIAL_CHARS:
            out.append("\\" + ch)
        else:
            out.append(ch)
    return "".join(out)


def to_markdown_v2(markdown_text: str) -> str:
    text = str(markdown_text or "")
    parts: List[str] = []
    cursor = 0
    for match in _BOLD_RE.finditer(text):
        start, end = match.span()
        if start > cursor:
            parts.append(escape_markdown_v2(text[cursor:start]))
        bold_body = escape_markdown_v2(match.group(1))
        parts.append(f"*{bold_body}*")
        cursor = end
    if cursor < len(text):
        parts.append(escape_markdown_v2(text[cursor:]))
    return "".join(parts)


def format_tool_result(
    *,
    ok: bool,
    output: str,
    max_chars: int = 480,
    saved_to_file: str = "",
) -> str:
    raw = str(output or "").strip()
    if not raw:
        return "✅ Done: (no output)." if ok else "⚠️ Tool error (E_TOOL): no output."

    one_line = " ".join([part.strip() for part in raw.splitlines() if part.strip()])
    summary = one_line if one_line else raw
    truncated = False
    if len(summary) > max(80, int(max_chars)):
        summary = summary[: max(80, int(max_chars))].rstrip() + "..."
        truncated = True

    if truncated and saved_to_file:
        summary = f"{summary} (saved to file: {saved_to_file})"
    elif truncated:
        summary = f"{summary} (output truncated)"

    if ok:
        return f"✅ Done: {summary}"
    code = _extract_error_code(raw)
    return f"⚠️ Tool error ({code}): {summary}"


def _normalize_whitespace(text: str) -> str:
    cleaned = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    cleaned = "\n".join(line.rstrip() for line in cleaned.split("\n"))
    return cleaned.strip()


def _apply_paragraphing(text: str) -> str:
    blocks = text.split("\n\n")
    out_blocks: List[str] = []
    for block in blocks:
        lines = [ln for ln in block.splitlines() if ln.strip()]
        if not lines:
            continue
        if len(lines) == 1 and len(lines[0]) > 260:
            out_blocks.extend(_split_long_line(lines[0]))
            continue
        if len(lines) > 5 and not any(_is_list_line(ln) for ln in lines):
            grouped: List[str] = []
            for idx in range(0, len(lines), 3):
                grouped.append("\n".join(lines[idx : idx + 3]))
            out_blocks.extend(grouped)
            continue
        out_blocks.append("\n".join(lines))
    return "\n\n".join(out_blocks).strip()


def _split_long_line(line: str) -> List[str]:
    chunks: List[str] = []
    sentences = [s.strip() for s in _SENTENCE_SPLIT_RE.split(line.strip()) if s.strip()]
    if len(sentences) <= 1:
        return [line.strip()]
    current: List[str] = []
    current_len = 0
    for sentence in sentences:
        sentence_len = len(sentence) + (1 if current else 0)
        if current and current_len + sentence_len > 190:
            chunks.append(" ".join(current).strip())
            current = [sentence]
            current_len = len(sentence)
        else:
            current.append(sentence)
            current_len += sentence_len
    if current:
        chunks.append(" ".join(current).strip())
    return chunks


def _apply_structure(text: str, *, use_bold: bool) -> str:
    out: List[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            out.append("")
            continue
        if _ENUM_RE.match(line):
            out.append(f"- {line}")
            continue
        if line.startswith("* "):
            out.append(f"- {line[2:].strip()}")
            continue
        if use_bold and _HEADING_RE.match(line):
            out.append(f"**{line}**")
            continue
        out.append(line)
    return "\n".join(out).strip()


def _apply_brevity(text: str, *, brevity: str) -> str:
    if brevity != "short":
        return text
    lines = text.splitlines()
    out: List[str] = []
    blank_run = 0
    for line in lines:
        if line.strip():
            blank_run = 0
            out.append(line)
            continue
        blank_run += 1
        if blank_run <= 1:
            out.append("")
    return "\n".join(out).strip()


def _apply_emoji(text: str, mode: str) -> Tuple[str, int]:
    if mode == "off":
        return text, 0
    lowered = text.lower()
    max_count = 2 if mode == "light" else 5
    emoji = ""
    if any(term in lowered for term in ("error", "failed", "warning", "blocked")):
        emoji = "⚠️"
    elif any(term in lowered for term in ("done", "completed", "success")):
        emoji = "✅"
    if not emoji:
        return text, 0
    if text.startswith(emoji):
        return text, 1
    if max_count <= 0:
        return text, 0
    if emoji not in _SAFE_EMOJIS:
        return text, 0
    return f"{emoji} {text}", 1


def _is_list_line(line: str) -> bool:
    text = str(line or "").strip()
    return text.startswith("- ") or bool(_ENUM_RE.match(text))


def _extract_error_code(raw: str) -> str:
    source = str(raw or "")
    upper = re.search(r"\b([A-Z][A-Z0-9_]{2,32})\b", source)
    if upper:
        return upper.group(1)
    if source.lower().startswith("error:"):
        return "ERROR"
    return "E_TOOL"

