"""Anthropic Claude API provider adapter (EPIC 3, issue #66).

Implements ProviderAdapter using the Anthropic Messages API directly.
Works with or without the ``anthropic`` SDK installed:
  - If ``anthropic`` is installed → uses the official async client.
  - Otherwise → falls back to plain ``httpx`` HTTP calls.

Streaming is fully supported: ``generate(..., stream=True)`` returns a plain
string (buffered), while ``generate_stream()`` returns an async generator of
text chunks (used by the StreamingUpdater in EPIC 4).

Configuration via environment variables (or explicit constructor args):
  ANTHROPIC_API_KEY      – required
  ANTHROPIC_MODEL        – default: claude-opus-4-6
  ANTHROPIC_MAX_TOKENS   – default: 4096
  ANTHROPIC_TIMEOUT_SEC  – default: 120
"""
from __future__ import annotations

import asyncio
import json as _json_mod
import logging
import os
from typing import Any, AsyncIterator, Dict, List, Optional, Sequence

from codex_telegram_bot.observability.structured_log import log_json

logger = logging.getLogger(__name__)

_DEFAULT_MODEL = "claude-opus-4-6"
_DEFAULT_MAX_TOKENS = 4096
_DEFAULT_TIMEOUT_SEC = 120

# Maximum characters for a single tool result before truncation.
_TOOL_RESULT_MAX_CHARS = 4000

# Try to import the official SDK; fall back to httpx for environments
# where the package is not installed.
try:
    import anthropic as _anthropic_sdk  # type: ignore[import]
    _SDK_AVAILABLE = True
except ImportError:
    _anthropic_sdk = None  # type: ignore[assignment]
    _SDK_AVAILABLE = False

try:
    import httpx as _httpx  # type: ignore[import]
    _HTTPX_AVAILABLE = True
except ImportError:
    _httpx = None  # type: ignore[assignment]
    _HTTPX_AVAILABLE = False


def _env_int(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    try:
        return max(1, int(raw)) if raw else default
    except ValueError:
        return default


class AnthropicProvider:
    """Anthropic Claude API adapter.

    Supports both buffered and streaming generation.  When the official
    ``anthropic`` SDK is available it is preferred; otherwise raw HTTP via
    ``httpx`` is used.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        max_tokens: Optional[int] = None,
        timeout_sec: Optional[int] = None,
    ) -> None:
        self._api_key: str = api_key or os.environ.get("ANTHROPIC_API_KEY") or ""
        self._model: str = model or os.environ.get("ANTHROPIC_MODEL") or _DEFAULT_MODEL
        self._max_tokens: int = max_tokens or _env_int("ANTHROPIC_MAX_TOKENS", _DEFAULT_MAX_TOKENS)
        self._timeout_sec: int = timeout_sec or _env_int("ANTHROPIC_TIMEOUT_SEC", _DEFAULT_TIMEOUT_SEC)
        self._sdk_client: Any = None
        self._http_client: Any = None

    # ------------------------------------------------------------------
    # ProviderAdapter protocol
    # ------------------------------------------------------------------

    async def generate(
        self,
        messages: Sequence[Dict[str, str]],
        stream: bool = False,
        correlation_id: str = "",
        policy_profile: str = "balanced",
    ) -> str:
        if stream:
            chunks: list[str] = []
            async for chunk in self.generate_stream(messages, correlation_id=correlation_id):
                chunks.append(chunk)
            return "".join(chunks)
        return await self._generate_buffered(messages, correlation_id=correlation_id)

    async def execute(
        self,
        prompt: str,
        correlation_id: str = "",
        policy_profile: str = "balanced",
    ) -> str:
        messages = [{"role": "user", "content": prompt}]
        return await self.generate(messages, correlation_id=correlation_id,
                                   policy_profile=policy_profile)

    async def version(self) -> str:
        return f"anthropic/{self._model}"

    async def health(self) -> Dict[str, Any]:
        if not self._api_key:
            return {
                "provider": "anthropic",
                "status": "unhealthy",
                "reason": "missing_api_key",
                "capabilities": self.capabilities(),
            }
        return {
            "provider": "anthropic",
            "status": "healthy",
            "model": self._model,
            "capabilities": self.capabilities(),
        }

    def capabilities(self) -> Dict[str, Any]:
        return {
            "provider": "anthropic",
            "supports_tool_calls": True,
            "supports_streaming": True,
            "max_context_chars": 800_000,
            "supported_policy_profiles": ["strict", "balanced", "trusted"],
            "reliability_tier": "primary",
            "model": self._model,
        }

    # ------------------------------------------------------------------
    # Native function calling interface
    # ------------------------------------------------------------------

    async def generate_with_tools(
        self,
        messages: Sequence[Dict[str, Any]],
        tools: Sequence[Dict[str, Any]],
        system: str = "",
        correlation_id: str = "",
    ) -> Dict[str, Any]:
        """Call the Anthropic Messages API with native tool definitions.

        Returns a dict with:
          - "content": list of content blocks (text and tool_use)
          - "stop_reason": "end_turn" | "tool_use" | ...
          - "usage": token usage dict
        """
        if not self._api_key:
            return {
                "content": [{"type": "text", "text": "Error: ANTHROPIC_API_KEY not configured."}],
                "stop_reason": "end_turn",
                "usage": {},
            }
        log_json(
            logger, "provider.generate_with_tools.start",
            provider="anthropic", run_id=correlation_id,
            model=self._model, tool_count=len(tools),
        )
        try:
            if _SDK_AVAILABLE:
                return await self._call_with_tools_sdk(messages, tools, system)
            elif _HTTPX_AVAILABLE:
                return await self._call_with_tools_httpx(messages, tools, system)
            else:
                return {
                    "content": [{"type": "text", "text": "Error: neither 'anthropic' SDK nor 'httpx' is installed."}],
                    "stop_reason": "end_turn",
                    "usage": {},
                }
        except Exception as exc:
            logger.exception("AnthropicProvider generate_with_tools error")
            log_json(
                logger, "provider.generate_with_tools.error",
                provider="anthropic", run_id=correlation_id, kind=type(exc).__name__,
            )
            return {
                "content": [{"type": "text", "text": f"Error: {exc}"}],
                "stop_reason": "end_turn",
                "usage": {},
            }

    async def _call_with_tools_sdk(
        self,
        messages: Sequence[Dict[str, Any]],
        tools: Sequence[Dict[str, Any]],
        system: str = "",
    ) -> Dict[str, Any]:
        client = self._get_sdk_client()
        kwargs: Dict[str, Any] = {
            "model": self._model,
            "max_tokens": self._max_tokens,
            "messages": list(messages),
            "tools": list(tools),
        }
        if system:
            kwargs["system"] = system
        msg = await client.messages.create(**kwargs)
        return _extract_sdk_full_response(msg)

    async def _call_with_tools_httpx(
        self,
        messages: Sequence[Dict[str, Any]],
        tools: Sequence[Dict[str, Any]],
        system: str = "",
    ) -> Dict[str, Any]:
        client = self._get_http_client()
        payload: Dict[str, Any] = {
            "model": self._model,
            "max_tokens": self._max_tokens,
            "messages": list(messages),
            "tools": list(tools),
        }
        if system:
            payload["system"] = system
        response = await client.post("/v1/messages", json=payload)
        response.raise_for_status()
        data = response.json()
        return _extract_httpx_full_response(data)

    # ------------------------------------------------------------------
    # Streaming interface (EPIC 4)
    # ------------------------------------------------------------------

    async def generate_stream(
        self,
        messages: Sequence[Dict[str, str]],
        correlation_id: str = "",
    ) -> AsyncIterator[str]:
        """Yield text chunks as they arrive from the API."""
        if not self._api_key:
            yield "Error: ANTHROPIC_API_KEY not configured."
            return
        log_json(logger, "provider.stream.start", provider="anthropic",
                 run_id=correlation_id, model=self._model)
        try:
            if _SDK_AVAILABLE:
                async for chunk in self._stream_via_sdk(messages):
                    yield chunk
            elif _HTTPX_AVAILABLE:
                async for chunk in self._stream_via_httpx(messages):
                    yield chunk
            else:
                yield "Error: neither 'anthropic' SDK nor 'httpx' is installed."
                return
        except Exception as exc:
            logger.exception("AnthropicProvider stream error")
            log_json(logger, "provider.stream.error", provider="anthropic",
                     run_id=correlation_id, kind=type(exc).__name__)
            yield f"Error: {exc}"

    # ------------------------------------------------------------------
    # Internal: buffered generation
    # ------------------------------------------------------------------

    async def _generate_buffered(
        self,
        messages: Sequence[Dict[str, str]],
        correlation_id: str = "",
    ) -> str:
        if not self._api_key:
            return "Error: ANTHROPIC_API_KEY not configured."
        log_json(logger, "provider.generate.start", provider="anthropic",
                 run_id=correlation_id, model=self._model)
        try:
            if _SDK_AVAILABLE:
                return await self._call_via_sdk(messages)
            elif _HTTPX_AVAILABLE:
                return await self._call_via_httpx(messages)
            else:
                return "Error: neither 'anthropic' SDK nor 'httpx' is installed."
        except Exception as exc:
            logger.exception("AnthropicProvider generate error")
            log_json(logger, "provider.generate.error", provider="anthropic",
                     run_id=correlation_id, kind=type(exc).__name__)
            return f"Error: {exc}"

    # ------------------------------------------------------------------
    # SDK path
    # ------------------------------------------------------------------

    def _get_sdk_client(self) -> Any:
        if self._sdk_client is None:
            self._sdk_client = _anthropic_sdk.AsyncAnthropic(
                api_key=self._api_key,
                timeout=float(self._timeout_sec),
            )
        return self._sdk_client

    async def _call_via_sdk(self, messages: Sequence[Dict[str, str]]) -> str:
        client = self._get_sdk_client()
        msg = await client.messages.create(
            model=self._model,
            max_tokens=self._max_tokens,
            messages=list(messages),
        )
        return _extract_sdk_text(msg)

    async def _stream_via_sdk(
        self, messages: Sequence[Dict[str, str]]
    ) -> AsyncIterator[str]:
        client = self._get_sdk_client()
        async with client.messages.stream(
            model=self._model,
            max_tokens=self._max_tokens,
            messages=list(messages),
        ) as stream:
            async for text in stream.text_stream:
                yield text

    # ------------------------------------------------------------------
    # httpx (fallback) path
    # ------------------------------------------------------------------

    def _get_http_client(self) -> Any:
        if self._http_client is None:
            self._http_client = _httpx.AsyncClient(
                base_url="https://api.anthropic.com",
                headers={
                    "x-api-key": self._api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                timeout=float(self._timeout_sec),
            )
        return self._http_client

    async def _call_via_httpx(self, messages: Sequence[Dict[str, str]]) -> str:
        client = self._get_http_client()
        payload = {
            "model": self._model,
            "max_tokens": self._max_tokens,
            "messages": list(messages),
        }
        response = await client.post("/v1/messages", json=payload)
        response.raise_for_status()
        data = response.json()
        return _extract_httpx_text(data)

    async def _stream_via_httpx(
        self, messages: Sequence[Dict[str, str]]
    ) -> AsyncIterator[str]:
        """Consume the Anthropic SSE stream via httpx."""
        import json as _json

        client = self._get_http_client()
        payload = {
            "model": self._model,
            "max_tokens": self._max_tokens,
            "messages": list(messages),
            "stream": True,
        }
        async with client.stream("POST", "/v1/messages", json=payload) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                line = line.strip()
                if not line.startswith("data:"):
                    continue
                raw = line[len("data:"):].strip()
                if raw == "[DONE]":
                    break
                try:
                    event = _json.loads(raw)
                except Exception:
                    continue
                delta = (
                    event.get("delta") or {}
                )
                text = delta.get("text") or ""
                if text:
                    yield text


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_sdk_text(msg: Any) -> str:
    try:
        for block in msg.content or []:
            if getattr(block, "type", None) == "text":
                return block.text or ""
    except Exception:
        pass
    return str(msg)


def _extract_httpx_text(data: Dict[str, Any]) -> str:
    for block in data.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "text":
            return block.get("text") or ""
    return ""


def _extract_sdk_full_response(msg: Any) -> Dict[str, Any]:
    """Convert an SDK Message object to a plain dict with content blocks."""
    content: List[Dict[str, Any]] = []
    try:
        for block in msg.content or []:
            block_type = getattr(block, "type", None)
            if block_type == "text":
                content.append({"type": "text", "text": block.text or ""})
            elif block_type == "tool_use":
                content.append({
                    "type": "tool_use",
                    "id": getattr(block, "id", ""),
                    "name": getattr(block, "name", ""),
                    "input": getattr(block, "input", {}),
                })
    except Exception:
        content = [{"type": "text", "text": str(msg)}]

    stop_reason = getattr(msg, "stop_reason", "end_turn") or "end_turn"
    usage_obj = getattr(msg, "usage", None)
    usage: Dict[str, Any] = {}
    if usage_obj is not None:
        usage = {
            "input_tokens": getattr(usage_obj, "input_tokens", 0),
            "output_tokens": getattr(usage_obj, "output_tokens", 0),
        }
    return {"content": content, "stop_reason": stop_reason, "usage": usage}


def _extract_httpx_full_response(data: Dict[str, Any]) -> Dict[str, Any]:
    """Convert a raw httpx JSON response to a plain dict with content blocks."""
    content: List[Dict[str, Any]] = []
    for block in data.get("content") or []:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type", "")
        if block_type == "text":
            content.append({"type": "text", "text": block.get("text") or ""})
        elif block_type == "tool_use":
            content.append({
                "type": "tool_use",
                "id": block.get("id", ""),
                "name": block.get("name", ""),
                "input": block.get("input", {}),
            })
    stop_reason = data.get("stop_reason", "end_turn") or "end_turn"
    usage = data.get("usage") or {}
    return {"content": content, "stop_reason": stop_reason, "usage": usage}
