from __future__ import annotations

import asyncio
import json
import os
from typing import Any, Dict, Optional, Sequence
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
            data = await asyncio.to_thread(self._send_request, req)
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
            "supports_tool_calls": False,
            "supports_streaming": False,
            "max_context_chars": 800_000,
            "supported_policy_profiles": ["strict", "balanced", "trusted"],
            "reliability_tier": "primary",
            "model": self._model,
        }

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
