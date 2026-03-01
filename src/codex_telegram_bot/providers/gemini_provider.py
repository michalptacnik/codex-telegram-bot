from __future__ import annotations

import json
import os
from typing import Any, Dict, Optional, Sequence, List
from urllib import error, parse, request


def _env_int(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return max(1, int(raw))
    except ValueError:
        return default


class GeminiProvider:
    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        timeout_sec: Optional[int] = None,
    ) -> None:
        self._api_key = (
            api_key
            or os.environ.get("GEMINI_API_KEY")
            or os.environ.get("GOOGLE_API_KEY")
            or ""
        ).strip()
        self._model = (model or os.environ.get("GEMINI_MODEL") or "gemini-2.0-flash").strip()
        self._timeout_sec = timeout_sec or _env_int("GEMINI_TIMEOUT_SEC", 120)

    async def generate(
        self,
        messages: Sequence[Dict[str, str]],
        stream: bool = False,
        correlation_id: str = "",
        policy_profile: str = "balanced",
    ) -> str:
        if stream:
            stream = False
        text = _messages_to_text(messages)
        return await self.execute(text, correlation_id=correlation_id, policy_profile=policy_profile)

    async def execute(
        self,
        prompt: str,
        correlation_id: str = "",
        policy_profile: str = "balanced",
    ) -> str:
        if not self._api_key:
            return "Error: GEMINI_API_KEY not configured."
        query = parse.urlencode({"key": self._api_key})
        url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"{self._model}:generateContent?{query}"
        )
        payload = {
            "contents": [
                {
                    "role": "user",
                    "parts": [{"text": prompt or ""}],
                }
            ]
        }
        req = request.Request(
            url=url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            data = self._send_request(req)
        except error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="ignore")[:300]
            except Exception:
                detail = str(exc)
            return f"Error: gemini API HTTP {exc.code}. {detail}".strip()
        except Exception as exc:
            return f"Error: gemini API request failed: {exc}"
        return _extract_gemini_text(data)

    async def version(self) -> str:
        return f"gemini/{self._model}"

    async def health(self) -> Dict[str, Any]:
        if not self._api_key:
            return {
                "provider": "gemini",
                "status": "unhealthy",
                "reason": "missing_api_key",
                "capabilities": self.capabilities(),
            }
        return {
            "provider": "gemini",
            "status": "healthy",
            "model": self._model,
            "capabilities": self.capabilities(),
        }

    def capabilities(self) -> Dict[str, Any]:
        return {
            "provider": "gemini",
            "supports_tool_calls": True,
            "supports_streaming": False,
            "max_context_chars": 800_000,
            "supported_policy_profiles": ["strict", "balanced", "trusted"],
            "reliability_tier": "primary",
            "model": self._model,
        }

    async def generate_with_tools(
        self,
        messages: Sequence[Dict[str, Any]],
        tools: Sequence[Dict[str, Any]],
        system: str = "",
        correlation_id: str = "",
    ) -> Dict[str, Any]:
        if not self._api_key:
            return {
                "content": [{"type": "text", "text": "Error: GEMINI_API_KEY not configured."}],
                "stop_reason": "end_turn",
                "usage": {},
                "model": self._model,
            }
        query = parse.urlencode({"key": self._api_key})
        url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"{self._model}:generateContent?{query}"
        )
        payload: Dict[str, Any] = {
            "contents": _to_gemini_contents(messages),
            "tools": [{"functionDeclarations": _to_gemini_function_declarations(tools)}] if tools else [],
        }
        if system:
            payload["systemInstruction"] = {"parts": [{"text": str(system)}]}
        req = request.Request(
            url=url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            data = self._send_request(req)
        except error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="ignore")[:300]
            except Exception:
                detail = str(exc)
            return {
                "content": [{"type": "text", "text": f"Error: gemini API HTTP {exc.code}. {detail}".strip()}],
                "stop_reason": "end_turn",
                "usage": {},
                "model": self._model,
            }
        except Exception as exc:
            return {
                "content": [{"type": "text", "text": f"Error: gemini API request failed: {exc}"}],
                "stop_reason": "end_turn",
                "usage": {},
                "model": self._model,
            }
        return _convert_gemini_tool_response(data, model=self._model)

    def _send_request(self, req: request.Request) -> Dict[str, Any]:
        with request.urlopen(req, timeout=self._timeout_sec) as response:
            raw = response.read().decode("utf-8")
        data = json.loads(raw or "{}")
        if isinstance(data, dict):
            return data
        return {}


def _messages_to_text(messages: Sequence[Dict[str, str]]) -> str:
    lines: list[str] = []
    for item in messages or []:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "user").strip().lower()
        content = str(item.get("content") or "").strip()
        if not content:
            continue
        if role == "system":
            lines.append(f"[system] {content}")
        elif role == "assistant":
            lines.append(f"[assistant] {content}")
        else:
            lines.append(content if role == "user" else f"[{role}] {content}")
    return "\n\n".join(lines)


def _extract_gemini_text(data: Dict[str, Any]) -> str:
    candidates = data.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        return ""
    first = candidates[0] if isinstance(candidates[0], dict) else {}
    content = first.get("content") if isinstance(first, dict) else {}
    parts = content.get("parts") if isinstance(content, dict) else []
    texts: list[str] = []
    if isinstance(parts, list):
        for p in parts:
            if isinstance(p, dict):
                text = p.get("text")
                if isinstance(text, str) and text:
                    texts.append(text)
    return "".join(texts).strip()


def _to_gemini_function_declarations(tools: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    declarations: List[Dict[str, Any]] = []
    for tool in tools or []:
        if not isinstance(tool, dict):
            continue
        declarations.append(
            {
                "name": str(tool.get("name") or ""),
                "description": str(tool.get("description") or ""),
                "parameters": tool.get("input_schema") or {"type": "object", "properties": {}},
            }
        )
    return [d for d in declarations if d.get("name")]


def _to_gemini_contents(messages: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    contents: List[Dict[str, Any]] = []
    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        role_raw = str(msg.get("role") or "user").strip().lower()
        role = "model" if role_raw == "assistant" else "user"
        content = msg.get("content")
        parts: List[Dict[str, Any]] = []
        if isinstance(content, str):
            if content.strip():
                parts.append({"text": content})
        elif isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                b_type = str(block.get("type") or "").strip().lower()
                if b_type == "text":
                    text = str(block.get("text") or "").strip()
                    if text:
                        parts.append({"text": text})
                elif b_type == "tool_result":
                    text = str(block.get("content") or "").strip()
                    if text:
                        parts.append({"text": f"Tool result: {text}"})
        if parts:
            contents.append({"role": role, "parts": parts})
    if not contents:
        contents = [{"role": "user", "parts": [{"text": ""}]}]
    return contents


def _convert_gemini_tool_response(data: Dict[str, Any], model: str) -> Dict[str, Any]:
    candidates = data.get("candidates")
    first = candidates[0] if isinstance(candidates, list) and candidates and isinstance(candidates[0], dict) else {}
    content = first.get("content") if isinstance(first, dict) else {}
    parts = content.get("parts") if isinstance(content, dict) else []
    out_blocks: List[Dict[str, Any]] = []
    tool_idx = 0
    if isinstance(parts, list):
        for part in parts:
            if not isinstance(part, dict):
                continue
            text = part.get("text")
            if isinstance(text, str) and text.strip():
                out_blocks.append({"type": "text", "text": text.strip()})
            fn = part.get("functionCall")
            if isinstance(fn, dict):
                tool_idx += 1
                args = fn.get("args")
                if not isinstance(args, dict):
                    args = {}
                out_blocks.append(
                    {
                        "type": "tool_use",
                        "id": f"gemini-tool-{tool_idx}",
                        "name": str(fn.get("name") or ""),
                        "input": args,
                    }
                )
    usage_meta = data.get("usageMetadata") if isinstance(data, dict) else {}
    usage = {
        "input_tokens": int((usage_meta or {}).get("promptTokenCount") or 0),
        "output_tokens": int((usage_meta or {}).get("candidatesTokenCount") or 0),
        "total_tokens": int((usage_meta or {}).get("totalTokenCount") or 0),
    }
    stop_reason = "tool_use" if any(b.get("type") == "tool_use" for b in out_blocks) else "end_turn"
    return {
        "content": out_blocks,
        "stop_reason": stop_reason,
        "usage": usage,
        "model": model,
    }
