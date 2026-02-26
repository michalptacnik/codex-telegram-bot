"""Tests for ResponsesApiProvider (PRODUCT BAR)."""
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from codex_telegram_bot.providers.responses_api import (
    ResponsesApiProvider,
    _extract_responses_text,
    _extract_responses_tool_calls,
)


# ---------------------------------------------------------------------------
# Unit tests for parsing helpers
# ---------------------------------------------------------------------------


class TestExtractResponsesText:
    def test_message_type_output_text(self):
        data = {
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": "Hello world"}],
                }
            ]
        }
        assert _extract_responses_text(data) == "Hello world"

    def test_message_type_text_block(self):
        data = {
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "text", "text": "Hi there"}],
                }
            ]
        }
        assert _extract_responses_text(data) == "Hi there"

    def test_top_level_text_type(self):
        data = {"output": [{"type": "text", "text": "Direct text"}]}
        assert _extract_responses_text(data) == "Direct text"

    def test_empty_output(self):
        assert _extract_responses_text({"output": []}) == ""

    def test_missing_output_key(self):
        assert _extract_responses_text({}) == ""

    def test_multiple_messages_joined(self):
        data = {
            "output": [
                {"type": "message", "content": [{"type": "output_text", "text": "Part 1"}]},
                {"type": "message", "content": [{"type": "output_text", "text": "Part 2"}]},
            ]
        }
        result = _extract_responses_text(data)
        assert "Part 1" in result
        assert "Part 2" in result

    def test_function_call_items_skipped(self):
        data = {
            "output": [
                {"type": "function_call", "name": "read_file", "arguments": "{}"},
                {"type": "message", "content": [{"type": "output_text", "text": "Done"}]},
            ]
        }
        result = _extract_responses_text(data)
        assert result == "Done"


class TestExtractResponsesToolCalls:
    def test_function_call(self):
        data = {
            "output": [
                {
                    "type": "function_call",
                    "name": "read_file",
                    "arguments": '{"path": "/tmp/test.txt"}',
                    "call_id": "call_abc123",
                }
            ]
        }
        calls = _extract_responses_tool_calls(data)
        assert len(calls) == 1
        assert calls[0]["name"] == "read_file"
        assert calls[0]["args"] == {"path": "/tmp/test.txt"}
        assert calls[0]["call_id"] == "call_abc123"

    def test_tool_call_type(self):
        data = {
            "output": [
                {
                    "type": "tool_call",
                    "name": "shell_exec",
                    "arguments": '{"command": "ls"}',
                }
            ]
        }
        calls = _extract_responses_tool_calls(data)
        assert len(calls) == 1
        assert calls[0]["name"] == "shell_exec"

    def test_dict_arguments(self):
        data = {
            "output": [
                {
                    "type": "function_call",
                    "name": "write_file",
                    "arguments": {"path": "/out.txt", "content": "hello"},
                }
            ]
        }
        calls = _extract_responses_tool_calls(data)
        assert calls[0]["args"] == {"path": "/out.txt", "content": "hello"}

    def test_malformed_json_arguments(self):
        data = {
            "output": [
                {
                    "type": "function_call",
                    "name": "tool",
                    "arguments": "NOT JSON",
                }
            ]
        }
        calls = _extract_responses_tool_calls(data)
        assert len(calls) == 1
        assert calls[0]["args"] == {}

    def test_no_tool_calls_in_output(self):
        data = {"output": [{"type": "message", "content": [{"type": "output_text", "text": "hi"}]}]}
        assert _extract_responses_tool_calls(data) == []

    def test_empty_name_skipped(self):
        data = {"output": [{"type": "function_call", "name": "", "arguments": "{}"}]}
        assert _extract_responses_tool_calls(data) == []


# ---------------------------------------------------------------------------
# Provider class tests (mocked HTTP)
# ---------------------------------------------------------------------------


class TestResponsesApiProviderInit:
    def test_defaults_from_env(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "test-key")
        monkeypatch.setenv("OPENAI_MODEL", "gpt-4-turbo")
        p = ResponsesApiProvider()
        assert p._api_key == "test-key"
        assert p._model == "gpt-4-turbo"

    def test_explicit_args_override_env(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "env-key")
        p = ResponsesApiProvider(api_key="explicit-key", model="gpt-4o-mini")
        assert p._api_key == "explicit-key"
        assert p._model == "gpt-4o-mini"

    def test_missing_key_is_empty_string(self, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        p = ResponsesApiProvider()
        assert p._api_key == ""

    def test_api_base_customisable(self):
        p = ResponsesApiProvider(api_key="k", api_base="https://my-proxy.example.com")
        assert p._api_base == "https://my-proxy.example.com"


class TestResponsesApiProviderHealth:
    def test_unhealthy_when_no_key(self, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        import asyncio
        p = ResponsesApiProvider()
        result = asyncio.run(p.health())
        assert result["status"] == "unhealthy"
        assert result["reason"] == "missing_api_key"

    def test_healthy_when_key_present(self, monkeypatch):
        import asyncio
        p = ResponsesApiProvider(api_key="test-key")
        result = asyncio.run(p.health())
        assert result["status"] == "healthy"

    def test_capabilities(self):
        p = ResponsesApiProvider(api_key="k")
        caps = p.capabilities()
        assert caps["supports_tool_calls"] is True
        assert caps["provider"] == "responses_api"
        assert caps["max_context_chars"] == 500_000

    def test_version_string(self):
        import asyncio
        p = ResponsesApiProvider(api_key="k", model="gpt-4o-mini")
        v = asyncio.run(p.version())
        assert "responses_api" in v
        assert "gpt-4o-mini" in v


class TestResponsesApiProviderGenerate:
    def test_generate_returns_error_without_key(self, monkeypatch):
        import asyncio
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        p = ResponsesApiProvider()
        result = asyncio.run(p.generate([{"role": "user", "content": "hello"}]))
        assert result.startswith("Error:")

    def test_generate_calls_http_and_extracts_text(self, monkeypatch):
        """Mock the HTTP response and verify text extraction."""
        import asyncio

        fake_response_data = {
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": "The answer is 42."}],
                }
            ]
        }

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json = MagicMock(return_value=fake_response_data)

        mock_client = MagicMock()
        mock_client.post = AsyncMock(return_value=mock_response)

        p = ResponsesApiProvider(api_key="test-key")
        p._http_client = mock_client

        result = asyncio.run(p.generate([{"role": "user", "content": "What is 6*7?"}]))
        assert result == "The answer is 42."

    def test_generate_with_tools_returns_tool_calls(self, monkeypatch):
        import asyncio

        fake_response_data = {
            "output": [
                {
                    "type": "function_call",
                    "name": "read_file",
                    "arguments": '{"path": "/etc/hosts"}',
                    "call_id": "c1",
                }
            ]
        }

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json = MagicMock(return_value=fake_response_data)

        mock_client = MagicMock()
        mock_client.post = AsyncMock(return_value=mock_response)

        p = ResponsesApiProvider(api_key="test-key")
        p._http_client = mock_client

        result = asyncio.run(p.generate_with_tools(
            messages=[{"role": "user", "content": "Read /etc/hosts"}],
            tool_schemas=[{"type": "function", "name": "read_file", "description": "Read a file"}],
        ))
        assert result["tool_calls"][0]["name"] == "read_file"
        assert result["tool_calls"][0]["args"] == {"path": "/etc/hosts"}
