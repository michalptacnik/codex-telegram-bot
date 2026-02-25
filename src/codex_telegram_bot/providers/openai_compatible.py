from __future__ import annotations

import asyncio
import json
import os
from typing import Any, Dict, Optional, Sequence
from urllib import error, request


def _env_int(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return max(1, int(raw))
    except ValueError:
        return default


def _normalize_base_url(value: str) -> str:
    url = (value or "").strip().rstrip("/")
    if not url:
        return ""
    return url


def _normalize_messages(messages: Sequence[Dict[str, str]]) -> list[Dict[str, str]]:
    out: list[Dict[str, str]] = []
    for item in messages or []:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "user").strip().lower() or "user"
        content = str(item.get("content") or "").strip()
        if not content:
            continue
        if role not in {"system", "user", "assistant", "tool"}:
            role = "user"
        out.append({"role": role, "content": content})
    return out or [{"role": "user", "content": ""}]


class OpenAICompatibleProvider:
    """Generic OpenAI-compatible chat-completions provider."""

    def __init__(
        self,
        provider_name: str,
        api_key_env: str,
        default_base_url: str,
        default_model: str,
        model_env: Optional[str] = None,
        base_url_env: Optional[str] = None,
        timeout_env: Optional[str] = None,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        base_url: Optional[str] = None,
        timeout_sec: Optional[int] = None,
    ) -> None:
        self._provider_name = provider_name
        self._api_key = (api_key or os.environ.get(api_key_env) or "").strip()
        self._model = (
            model
            or os.environ.get(model_env or f"{provider_name.upper()}_MODEL")
            or default_model
        ).strip()
        self._base_url = _normalize_base_url(
            base_url
            or os.environ.get(base_url_env or f"{provider_name.upper()}_BASE_URL")
            or default_base_url
        )
        self._timeout_sec = timeout_sec or _env_int(timeout_env or f"{provider_name.upper()}_TIMEOUT_SEC", 120)

    async def generate(
        self,
        messages: Sequence[Dict[str, str]],
        stream: bool = False,
        correlation_id: str = "",
        policy_profile: str = "balanced",
    ) -> str:
        if stream:
            stream = False
        payload = {
            "model": self._model,
            "messages": _normalize_messages(messages),
        }
        return await self._chat_completion(payload)

    async def execute(
        self,
        prompt: str,
        correlation_id: str = "",
        policy_profile: str = "balanced",
    ) -> str:
        return await self.generate([{"role": "user", "content": prompt}], stream=False)

    async def version(self) -> str:
        return f"{self._provider_name}/{self._model}"

    async def health(self) -> Dict[str, Any]:
        if not self._api_key:
            return {
                "provider": self._provider_name,
                "status": "unhealthy",
                "reason": "missing_api_key",
                "capabilities": self.capabilities(),
            }
        return {
            "provider": self._provider_name,
            "status": "healthy",
            "model": self._model,
            "base_url": self._base_url,
            "capabilities": self.capabilities(),
        }

    def capabilities(self) -> Dict[str, Any]:
        return {
            "provider": self._provider_name,
            "supports_tool_calls": False,
            "supports_streaming": False,
            "max_context_chars": 500_000,
            "supported_policy_profiles": ["strict", "balanced", "trusted"],
            "reliability_tier": "primary",
            "model": self._model,
        }

    async def _chat_completion(self, payload: Dict[str, Any]) -> str:
        if not self._api_key:
            return f"Error: {self._provider_name.upper()} API key not configured."
        if not self._base_url:
            return f"Error: {self._provider_name.upper()} base URL not configured."
        url = f"{self._base_url}/chat/completions"
        body = json.dumps(payload).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        req = request.Request(url=url, data=body, headers=headers, method="POST")

        try:
            data = await asyncio.to_thread(self._send_request, req)
        except error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="ignore")[:300]
            except Exception:
                detail = str(exc)
            return f"Error: {self._provider_name} API HTTP {exc.code}. {detail}".strip()
        except Exception as exc:
            return f"Error: {self._provider_name} API request failed: {exc}"
        return _extract_completion_text(data)

    def _send_request(self, req: request.Request) -> Dict[str, Any]:
        with request.urlopen(req, timeout=self._timeout_sec) as response:
            raw = response.read().decode("utf-8")
        data = json.loads(raw or "{}")
        if not isinstance(data, dict):
            return {}
        return data


def _extract_completion_text(data: Dict[str, Any]) -> str:
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    first = choices[0] if isinstance(choices[0], dict) else {}
    message = first.get("message") if isinstance(first, dict) else {}
    if isinstance(message, dict):
        content = message.get("content")
        if isinstance(content, str):
            return content
    content = first.get("text") if isinstance(first, dict) else ""
    if isinstance(content, str):
        return content
    return ""
