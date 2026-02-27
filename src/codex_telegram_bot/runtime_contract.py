from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Sequence, Set, Union


@dataclass(frozen=True)
class AssistantText:
    content: str


@dataclass(frozen=True)
class ToolCall:
    name: str
    args: Dict[str, Any]
    call_id: str


@dataclass(frozen=True)
class ToolResult:
    call_id: str
    output: str


@dataclass(frozen=True)
class RuntimeError:
    kind: str
    detail: str


RuntimeEvent = Union[AssistantText, ToolCall, ToolResult, RuntimeError]

_PROTOCOL_RE = re.compile(r"(?is)(^|\n)\s*!(exec|tool|loop)\b")


def has_protocol_bytes(raw: str) -> bool:
    text = str(raw or "")
    if not text.strip():
        return False
    if _PROTOCOL_RE.search(text):
        return True
    if re.search(r"(?is)\btool\s*\{", text):
        return True
    if re.search(r"(?is)\{[^{}]{0,500}\"(name|tool|args|arguments|input)\"\s*:", text):
        return True
    return False


def decode_provider_response(
    response: Dict[str, Any],
    *,
    allowed_tools: Iterable[str],
) -> List[RuntimeEvent]:
    allowed = {str(x or "").strip().lower() for x in allowed_tools}
    if isinstance(response, dict) and isinstance(response.get("stream_chunks"), list):
        streamed = merge_stream_chunks(response.get("stream_chunks") or [], allowed_tools=allowed)
        if streamed:
            return streamed
    content = response.get("content") if isinstance(response, dict) else []
    if (not isinstance(content, list)) and isinstance(response, dict):
        # Compatibility path for providers that return {"text": ..., "tool_calls": [...]}
        text = str(response.get("text") or "").strip()
        tool_calls = response.get("tool_calls")
        compat_events: List[RuntimeEvent] = []
        if text:
            compat_events.append(AssistantText(content=text))
        if isinstance(tool_calls, list):
            for idx, item in enumerate(tool_calls, start=1):
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name") or "").strip().lower()
                if not name:
                    return [RuntimeError(kind="decode_error", detail="tool_calls item missing name")]
                args = item.get("args") if isinstance(item.get("args"), dict) else {}
                call_id = str(item.get("call_id") or item.get("id") or f"toolcall-{idx}")
                compat_events.append(ToolCall(name=name, args=dict(args), call_id=call_id))
        if compat_events:
            return compat_events
    if not isinstance(content, list):
        return [RuntimeError(kind="decode_error", detail="provider content is not a list")]

    events: List[RuntimeEvent] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = str(block.get("type") or "").strip().lower()
        if btype == "text":
            text = str(block.get("text") or "")
            if text.strip():
                events.append(AssistantText(content=text.strip()))
            continue
        if btype == "tool_use":
            name = str(block.get("name") or "").strip().lower()
            call_id = str(block.get("id") or "").strip()
            raw_input = block.get("input")
            args = raw_input if isinstance(raw_input, dict) else {}
            if not name:
                return [RuntimeError(kind="decode_error", detail="tool_use block missing name")]
            if not call_id:
                call_id = f"toolcall-{len(events) + 1}"
            events.append(ToolCall(name=name, args=args, call_id=call_id))
            continue
        return [RuntimeError(kind="decode_error", detail=f"unsupported block type '{btype}'")]

    if not events:
        return [RuntimeError(kind="decode_error", detail="provider response had no decodable content blocks")]
    return events


def decode_text_response(
    text: str,
    *,
    allowed_tools: Iterable[str],
) -> List[RuntimeEvent]:
    raw = str(text or "").strip()
    if not raw:
        return [AssistantText(content="")]
    if not has_protocol_bytes(raw):
        return [AssistantText(content=raw)]

    parsed = _parse_tool_directive(raw, allowed_tools=set(str(x or "").strip().lower() for x in allowed_tools))
    if parsed is not None:
        return [parsed]
    return [RuntimeError(kind="decode_error", detail="protocol-like output could not be decoded")]


def to_telegram_text(events: Sequence[RuntimeEvent], *, safe_fallback: str) -> str:
    parts: List[str] = []
    for event in events:
        if isinstance(event, AssistantText):
            if event.content.strip():
                parts.append(event.content.strip())
    if parts:
        return "\n".join(parts).strip()
    return safe_fallback


def _parse_tool_directive(raw: str, *, allowed_tools: Set[str]) -> ToolCall | None:
    line = raw.splitlines()[0].strip()
    if not line.lower().startswith("!tool "):
        return None
    body = line[len("!tool ") :].strip()
    try:
        payload = json.loads(body)
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    name = str(payload.get("name") or payload.get("tool") or "").strip().lower()
    if not name or name not in allowed_tools:
        return None
    args = payload.get("args") if isinstance(payload.get("args"), dict) else {}
    call_id = str(payload.get("call_id") or payload.get("id") or f"toolcall-{name}")
    return ToolCall(name=name, args=dict(args), call_id=call_id)


def merge_stream_chunks(chunks: Sequence[Dict[str, Any]], *, allowed_tools: Iterable[str]) -> List[RuntimeEvent]:
    allowed = {str(x or "").strip().lower() for x in allowed_tools}
    texts: List[str] = []
    tool_buf: Dict[str, Dict[str, str]] = {}
    for chunk in chunks:
        if not isinstance(chunk, dict):
            continue
        ctype = str(chunk.get("type") or "").strip().lower()
        if ctype == "text_delta":
            delta = str(chunk.get("text") or "")
            if delta:
                texts.append(delta)
            continue
        if ctype == "tool_use_delta":
            call_id = str(chunk.get("id") or "").strip() or "toolcall-stream"
            item = tool_buf.setdefault(call_id, {"name": "", "input": ""})
            name = str(chunk.get("name") or "").strip().lower()
            if name:
                item["name"] = name
            delta = str(chunk.get("input_delta") or "")
            if delta:
                item["input"] = item["input"] + delta
    events: List[RuntimeEvent] = []
    if texts:
        events.append(AssistantText(content="".join(texts).strip()))
    for call_id, raw in tool_buf.items():
        name = str(raw.get("name") or "").strip().lower()
        if not name:
            return [RuntimeError(kind="decode_error", detail=f"streamed tool call '{call_id}' missing name")]
        args: Dict[str, Any] = {}
        blob = str(raw.get("input") or "").strip()
        if blob:
            try:
                parsed = json.loads(blob)
                if isinstance(parsed, dict):
                    args = parsed
            except Exception:
                return [RuntimeError(kind="decode_error", detail=f"invalid streamed tool args for '{name}'")]
        events.append(ToolCall(name=name, args=args, call_id=call_id))
    return events
