"""OpenAI Responses API provider adapter.

Uses the OpenAI Responses API (POST /v1/responses) for iterative tool calling.
Supports function-calling tools natively via the ``tools`` parameter.

Requires ``httpx`` to be installed (``pip install httpx``).

Configuration via environment variables (or explicit constructor args):
  OPENAI_API_KEY       – required
  OPENAI_MODEL         – default: gpt-4o
  OPENAI_MAX_TOKENS    – default: 4096
  OPENAI_TIMEOUT_SEC   – default: 120
  OPENAI_API_BASE      – default: https://api.openai.com
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional, Sequence

from codex_telegram_bot.observability.structured_log import log_json

logger = logging.getLogger(__name__)

_DEFAULT_MODEL = "gpt-4o"
_DEFAULT_MAX_TOKENS = 4096
_DEFAULT_TIMEOUT_SEC = 120
_API_BASE = "https://api.openai.com"

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


class ResponsesApiProvider:
    """OpenAI Responses API adapter with iterative tool-calling support.

    Implements ProviderAdapter.  When ``tools`` are passed to
    ``generate_with_tools()``, the Responses API's native function-calling
    is used, returning both text and tool_calls in a single dict.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        max_tokens: Optional[int] = None,
        timeout_sec: Optional[int] = None,
        api_base: Optional[str] = None,
    ) -> None:
        self._api_key: str = api_key or os.environ.get("OPENAI_API_KEY") or ""
        self._model: str = model or os.environ.get("OPENAI_MODEL") or _DEFAULT_MODEL
        self._max_tokens: int = max_tokens or _env_int("OPENAI_MAX_TOKENS", _DEFAULT_MAX_TOKENS)
        self._timeout_sec: int = timeout_sec or _env_int("OPENAI_TIMEOUT_SEC", _DEFAULT_TIMEOUT_SEC)
        self._api_base: str = (api_base or os.environ.get("OPENAI_API_BASE") or _API_BASE).rstrip("/")
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
        if not self._api_key:
            return "Error: OPENAI_API_KEY not configured."
        if not _HTTPX_AVAILABLE:
            return "Error: 'httpx' is required for responses_api provider (pip install httpx)."
        log_json(logger, "provider.generate.start", provider="responses_api",
                 run_id=correlation_id, model=self._model)
        try:
            return await self._call_responses_api(list(messages), tools=[])
        except Exception as exc:
            logger.exception("ResponsesApiProvider generate error")
            log_json(logger, "provider.generate.error", provider="responses_api",
                     run_id=correlation_id, kind=type(exc).__name__)
            return f"Error: {exc}"

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
        return f"responses_api/{self._model}"

    async def health(self) -> Dict[str, Any]:
        if not self._api_key:
            return {
                "provider": "responses_api",
                "status": "unhealthy",
                "reason": "missing_api_key",
                "capabilities": self.capabilities(),
            }
        if not _HTTPX_AVAILABLE:
            return {
                "provider": "responses_api",
                "status": "unhealthy",
                "reason": "httpx_not_installed",
                "capabilities": self.capabilities(),
            }
        return {
            "provider": "responses_api",
            "status": "healthy",
            "model": self._model,
            "capabilities": self.capabilities(),
        }

    def capabilities(self) -> Dict[str, Any]:
        return {
            "provider": "responses_api",
            "supports_tool_calls": True,
            "supports_streaming": False,
            "max_context_chars": 500_000,
            "supported_policy_profiles": ["strict", "balanced", "trusted"],
            "reliability_tier": "primary",
            "model": self._model,
        }

    # ------------------------------------------------------------------
    # Extended API: tool-aware generation
    # ------------------------------------------------------------------

    async def generate_with_tools(
        self,
        messages: Sequence[Dict[str, str]],
        tool_schemas: List[Dict[str, Any]],
        correlation_id: str = "",
    ) -> Dict[str, Any]:
        """Generate with tool schemas; return both text and tool_calls.

        Returns a dict with keys:
          text (str)         – assistant text response
          tool_calls (list)  – list of {"name": str, "args": dict, "call_id": str}
        """
        if not self._api_key:
            return {"text": "Error: OPENAI_API_KEY not configured.", "tool_calls": []}
        if not _HTTPX_AVAILABLE:
            return {"text": "Error: httpx not installed.", "tool_calls": []}
        try:
            client = self._get_http_client()
            payload: Dict[str, Any] = {
                "model": self._model,
                "input": list(messages),
                "max_output_tokens": self._max_tokens,
            }
            if tool_schemas:
                payload["tools"] = tool_schemas
            response = await client.post("/v1/responses", json=payload)
            response.raise_for_status()
            data = response.json()
            text = _extract_responses_text(data)
            tool_calls = _extract_responses_tool_calls(data)
            return {"text": text, "tool_calls": tool_calls}
        except Exception as exc:
            logger.exception("ResponsesApiProvider tool call error")
            return {"text": f"Error: {exc}", "tool_calls": []}

    # ------------------------------------------------------------------
    # Internal HTTP helpers
    # ------------------------------------------------------------------

    def _get_http_client(self) -> Any:
        if self._http_client is None:
            self._http_client = _httpx.AsyncClient(
                base_url=self._api_base,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                timeout=float(self._timeout_sec),
            )
        return self._http_client

    async def _call_responses_api(
        self,
        messages: List[Dict[str, str]],
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> str:
        client = self._get_http_client()
        payload: Dict[str, Any] = {
            "model": self._model,
            "input": messages,
            "max_output_tokens": self._max_tokens,
        }
        if tools:
            payload["tools"] = tools
        response = await client.post("/v1/responses", json=payload)
        response.raise_for_status()
        data = response.json()
        return _extract_responses_text(data)


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def _extract_responses_text(data: Dict[str, Any]) -> str:
    """Extract text from OpenAI Responses API response."""
    output = data.get("output") or []
    texts: List[str] = []
    for item in output:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type", "")
        if item_type == "message":
            content = item.get("content") or []
            for block in content:
                if isinstance(block, dict) and block.get("type") in ("output_text", "text"):
                    t = block.get("text") or ""
                    if t:
                        texts.append(t)
        elif item_type == "text":
            t = item.get("text") or ""
            if t:
                texts.append(t)
    return "\n".join(texts).strip()


def _extract_responses_tool_calls(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Extract function_call items from the Responses API output."""
    output = data.get("output") or []
    calls: List[Dict[str, Any]] = []
    for item in output:
        if not isinstance(item, dict):
            continue
        if item.get("type") in ("function_call", "tool_call"):
            name = item.get("name") or ""
            arguments = item.get("arguments") or "{}"
            if isinstance(arguments, str):
                try:
                    args = json.loads(arguments)
                except Exception:
                    args = {}
            else:
                args = arguments if isinstance(arguments, dict) else {}
            call_id = item.get("call_id") or item.get("id") or ""
            if name:
                calls.append({"name": name, "args": args, "call_id": call_id})
    return calls
