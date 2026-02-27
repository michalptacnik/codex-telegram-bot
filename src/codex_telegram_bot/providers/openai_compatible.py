from __future__ import annotations

import asyncio
import json
import os
from typing import Any, Dict, List, Optional, Sequence
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
            "supports_tool_calls": True,
            "supports_streaming": False,
            "max_context_chars": 500_000,
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
        """Call the OpenAI chat completions API with native tool definitions.

        Accepts Anthropic-style tool schemas and converts them to OpenAI format.
        Returns an Anthropic-style response dict (content blocks with tool_use).
        """
        if not self._api_key:
            return {
                "content": [{"type": "text", "text": f"Error: {self._provider_name.upper()} API key not configured."}],
                "stop_reason": "end_turn",
                "usage": {},
            }
        # Convert Anthropic tool schemas to OpenAI function format
        openai_tools: List[Dict[str, Any]] = []
        for tool in tools:
            openai_tools.append({
                "type": "function",
                "function": {
                    "name": tool.get("name", ""),
                    "description": tool.get("description", ""),
                    "parameters": tool.get("input_schema", {"type": "object", "properties": {}}),
                },
            })
        # Build messages, injecting system message if provided
        oai_messages: List[Dict[str, Any]] = []
        if system:
            oai_messages.append({"role": "system", "content": system})
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content")
            if role == "user" and isinstance(content, list):
                # Convert Anthropic tool_result blocks to OpenAI tool messages
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        oai_messages.append({
                            "role": "tool",
                            "tool_call_id": block.get("tool_use_id", ""),
                            "content": str(block.get("content", "")),
                        })
            elif role == "assistant" and isinstance(content, list):
                # Convert Anthropic assistant content blocks to OpenAI format
                text_parts = []
                tool_calls = []
                for block in content:
                    if isinstance(block, dict):
                        if block.get("type") == "text":
                            text_parts.append(block.get("text", ""))
                        elif block.get("type") == "tool_use":
                            tool_calls.append({
                                "id": block.get("id", ""),
                                "type": "function",
                                "function": {
                                    "name": block.get("name", ""),
                                    "arguments": json.dumps(block.get("input", {})),
                                },
                            })
                oai_msg: Dict[str, Any] = {"role": "assistant"}
                oai_msg["content"] = "\n".join(text_parts) if text_parts else None
                if tool_calls:
                    oai_msg["tool_calls"] = tool_calls
                oai_messages.append(oai_msg)
            else:
                oai_messages.append({"role": role, "content": content})

        payload: Dict[str, Any] = {
            "model": self._model,
            "messages": oai_messages,
            "tools": openai_tools,
        }
        try:
            data = await asyncio.to_thread(
                self._send_request,
                request.Request(
                    url=f"{self._base_url}/chat/completions",
                    data=json.dumps(payload).encode("utf-8"),
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "Content-Type": "application/json",
                    },
                    method="POST",
                ),
            )
        except error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="ignore")[:300]
            except Exception:
                detail = str(exc)
            return {
                "content": [{"type": "text", "text": f"Error: {self._provider_name} API HTTP {exc.code}. {detail}".strip()}],
                "stop_reason": "end_turn",
                "usage": {},
            }
        except Exception as exc:
            return {
                "content": [{"type": "text", "text": f"Error: {self._provider_name} API request failed: {exc}"}],
                "stop_reason": "end_turn",
                "usage": {},
            }
        return _convert_openai_response_to_anthropic(data)

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


def _convert_openai_response_to_anthropic(data: Dict[str, Any]) -> Dict[str, Any]:
    """Convert an OpenAI chat completion response to Anthropic-style format."""
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        return {"content": [], "stop_reason": "end_turn", "usage": {}}

    first = choices[0] if isinstance(choices[0], dict) else {}
    message = first.get("message") if isinstance(first, dict) else {}
    if not isinstance(message, dict):
        message = {}

    content: List[Dict[str, Any]] = []
    text = message.get("content")
    if isinstance(text, str) and text.strip():
        content.append({"type": "text", "text": text})

    tool_calls = message.get("tool_calls")
    if isinstance(tool_calls, list):
        for tc in tool_calls:
            if not isinstance(tc, dict):
                continue
            func = tc.get("function") or {}
            if not isinstance(func, dict):
                continue
            try:
                args = json.loads(func.get("arguments", "{}"))
            except (json.JSONDecodeError, TypeError):
                args = {}
            content.append({
                "type": "tool_use",
                "id": tc.get("id", ""),
                "name": func.get("name", ""),
                "input": args,
            })

    finish_reason = first.get("finish_reason", "stop") or "stop"
    stop_reason = "tool_use" if finish_reason == "tool_calls" else "end_turn"

    usage = data.get("usage") or {}
    return {"content": content, "stop_reason": stop_reason, "usage": usage}
