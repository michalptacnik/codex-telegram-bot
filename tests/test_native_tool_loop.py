"""Tests for the native function-calling agentic loop.

Validates that:
1. ToolRegistry.tool_schemas() returns correct Anthropic-style schemas
2. AnthropicProvider.generate_with_tools() extracts structured responses
3. OpenAICompatibleProvider response conversion works
4. AgentService.run_native_tool_loop() executes tools and loops correctly
5. The router prefers native loop when NATIVE_TOOL_LOOP=1
6. Tools are actually executed (not printed as text) in the native loop
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List, Sequence
from unittest.mock import AsyncMock, patch

from codex_telegram_bot.tools.base import (
    NATIVE_TOOL_SCHEMAS,
    ToolContext,
    ToolRegistry,
    ToolRequest,
    ToolResult,
)
from codex_telegram_bot.services.continuation_guard import PRELIMINARY_CONTINUE_HANDOFF
from codex_telegram_bot.tools.files import ReadFileTool, WriteFileTool
from codex_telegram_bot.tools.git import GitStatusTool
from codex_telegram_bot.providers.openai_compatible import _convert_openai_response_to_anthropic


# ---------------------------------------------------------------------------
# Tool schema tests
# ---------------------------------------------------------------------------


class TestToolSchemas(unittest.TestCase):
    def test_native_schemas_have_required_fields(self):
        for name, schema in NATIVE_TOOL_SCHEMAS.items():
            self.assertEqual(schema["name"], name)
            self.assertIn("description", schema)
            self.assertIn("input_schema", schema)
            self.assertIsInstance(schema["input_schema"], dict)
            self.assertEqual(schema["input_schema"]["type"], "object")
            self.assertIn("properties", schema["input_schema"])
            self.assertIn("required", schema["input_schema"])

    def test_registry_tool_schemas_returns_registered_tools(self):
        registry = ToolRegistry()
        registry.register(ReadFileTool())
        registry.register(WriteFileTool())
        registry.register(GitStatusTool())

        schemas = registry.tool_schemas()

        names = {s["name"] for s in schemas}
        self.assertIn("read_file", names)
        self.assertIn("write_file", names)
        self.assertIn("git_status", names)

    def test_registry_tool_schemas_excludes_unregistered(self):
        registry = ToolRegistry()
        registry.register(ReadFileTool())
        schemas = registry.tool_schemas()
        names = {s["name"] for s in schemas}
        self.assertIn("read_file", names)
        self.assertNotIn("write_file", names)

    def test_tool_schemas_are_valid_anthropic_format(self):
        registry = ToolRegistry()
        registry.register(ReadFileTool())
        schemas = registry.tool_schemas()
        for schema in schemas:
            # Anthropic requires these top-level keys
            self.assertIn("name", schema)
            self.assertIn("description", schema)
            self.assertIn("input_schema", schema)
            # input_schema must be a JSON Schema object
            input_schema = schema["input_schema"]
            self.assertEqual(input_schema["type"], "object")


# ---------------------------------------------------------------------------
# Provider response conversion tests
# ---------------------------------------------------------------------------


class TestOpenAIResponseConversion(unittest.TestCase):
    def test_text_only_response(self):
        openai_data = {
            "choices": [
                {
                    "message": {"role": "assistant", "content": "Hello!"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }
        result = _convert_openai_response_to_anthropic(openai_data)
        self.assertEqual(result["stop_reason"], "end_turn")
        self.assertEqual(len(result["content"]), 1)
        self.assertEqual(result["content"][0]["type"], "text")
        self.assertEqual(result["content"][0]["text"], "Hello!")

    def test_tool_call_response(self):
        openai_data = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_123",
                                "type": "function",
                                "function": {
                                    "name": "read_file",
                                    "arguments": '{"path": "test.txt"}',
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
        }
        result = _convert_openai_response_to_anthropic(openai_data)
        self.assertEqual(result["stop_reason"], "tool_use")
        tool_blocks = [b for b in result["content"] if b["type"] == "tool_use"]
        self.assertEqual(len(tool_blocks), 1)
        self.assertEqual(tool_blocks[0]["name"], "read_file")
        self.assertEqual(tool_blocks[0]["id"], "call_123")
        self.assertEqual(tool_blocks[0]["input"], {"path": "test.txt"})

    def test_mixed_text_and_tool_calls(self):
        openai_data = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "Let me check that file.",
                        "tool_calls": [
                            {
                                "id": "call_456",
                                "type": "function",
                                "function": {
                                    "name": "git_status",
                                    "arguments": "{}",
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
        }
        result = _convert_openai_response_to_anthropic(openai_data)
        text_blocks = [b for b in result["content"] if b["type"] == "text"]
        tool_blocks = [b for b in result["content"] if b["type"] == "tool_use"]
        self.assertEqual(len(text_blocks), 1)
        self.assertEqual(len(tool_blocks), 1)

    def test_empty_response(self):
        result = _convert_openai_response_to_anthropic({})
        self.assertEqual(result["content"], [])
        self.assertEqual(result["stop_reason"], "end_turn")


# ---------------------------------------------------------------------------
# Native agentic loop tests (with mock provider)
# ---------------------------------------------------------------------------


class _FakeProvider:
    """A fake provider that simulates native function calling."""

    def __init__(self, responses: List[Dict[str, Any]]):
        self._responses = list(responses)
        self._call_count = 0
        self.calls: List[Dict[str, Any]] = []

    async def generate_with_tools(
        self,
        messages: Sequence[Dict[str, Any]],
        tools: Sequence[Dict[str, Any]],
        system: str = "",
        correlation_id: str = "",
    ) -> Dict[str, Any]:
        self.calls.append({
            "messages": list(messages),
            "tools": list(tools),
            "system": system,
        })
        if self._call_count < len(self._responses):
            resp = self._responses[self._call_count]
            self._call_count += 1
            return resp
        return {"content": [{"type": "text", "text": "Done."}], "stop_reason": "end_turn", "usage": {}}

    async def version(self) -> str:
        return "fake/1.0"

    async def health(self) -> Dict[str, Any]:
        return {"status": "healthy"}

    def capabilities(self) -> Dict[str, Any]:
        return {"supports_tool_calls": True}


class _Session:
    def __init__(self, session_id: str):
        self.session_id = session_id


class TestNativeToolLoop(unittest.IsolatedAsyncioTestCase):
    def _make_service(self, provider, tmp_path):
        """Create a minimal AgentService with a fake provider."""
        from codex_telegram_bot.services.agent_service import AgentService
        from codex_telegram_bot.tools import build_default_tool_registry

        registry = build_default_tool_registry()
        return AgentService(
            provider=provider,
            tool_registry=registry,
            session_workspaces_root=tmp_path,
        )

    async def test_simple_text_response_no_tools(self):
        """If the model returns text only, no tools should be called."""
        provider = _FakeProvider([
            {
                "content": [{"type": "text", "text": "The answer is 4."}],
                "stop_reason": "end_turn",
                "usage": {},
            }
        ])
        with tempfile.TemporaryDirectory() as tmp:
            service = self._make_service(provider, Path(tmp))
            result = await service.run_native_tool_loop(
                user_message="What is 2+2?",
                chat_id=1,
                user_id=1,
                session_id="test-sess",
            )
        self.assertEqual(result, "The answer is 4.")
        self.assertEqual(len(provider.calls), 1)

    async def test_preliminary_text_triggers_auto_continue_pass(self):
        provider = _FakeProvider([
            {
                "content": [{"type": "text", "text": "I'm still working on it. Let me check one more thing."}],
                "stop_reason": "end_turn",
                "usage": {},
            },
            {
                "content": [{"type": "text", "text": "Completed: voice checks finished."}],
                "stop_reason": "end_turn",
                "usage": {},
            },
        ])
        with tempfile.TemporaryDirectory() as tmp:
            service = self._make_service(provider, Path(tmp))
            result = await service.run_native_tool_loop(
                user_message="run voice checks",
                chat_id=1,
                user_id=1,
                session_id="test-sess",
            )
        self.assertEqual(result, "Completed: voice checks finished.")
        self.assertEqual(len(provider.calls), 2)
        second_messages = provider.calls[1]["messages"]
        self.assertEqual(second_messages[-1]["role"], "user")
        self.assertIn("preliminary progress update", str(second_messages[-1]["content"]).lower())

    async def test_preliminary_text_is_sanitized_when_retry_budget_exhausted(self):
        preliminary = (
            "I'll continue executing the task to set up the DeepSeek bot. "
            "Let me check what's been done so far and continue with the setup."
        )
        provider = _FakeProvider([
            {"content": [{"type": "text", "text": preliminary}], "stop_reason": "end_turn", "usage": {}},
            {"content": [{"type": "text", "text": preliminary}], "stop_reason": "end_turn", "usage": {}},
            {"content": [{"type": "text", "text": preliminary}], "stop_reason": "end_turn", "usage": {}},
        ])
        with tempfile.TemporaryDirectory() as tmp:
            service = self._make_service(provider, Path(tmp))
            result = await service.run_native_tool_loop(
                user_message="continue setup",
                chat_id=1,
                user_id=1,
                session_id="test-sess",
            )
        self.assertIn(preliminary, result)
        self.assertIn(PRELIMINARY_CONTINUE_HANDOFF, result)
        self.assertEqual(len(provider.calls), 3)

    async def test_native_loop_emits_action_started_with_all_tools(self):
        provider = _FakeProvider([
            {
                "content": [
                    {"type": "tool_use", "id": "toolu_1", "name": "read_file", "input": {"path": "missing.txt"}},
                    {"type": "tool_use", "id": "toolu_2", "name": "git_status", "input": {}},
                ],
                "stop_reason": "tool_use",
                "usage": {},
            },
            {
                "content": [{"type": "text", "text": "Completed."}],
                "stop_reason": "end_turn",
                "usage": {},
            },
        ])
        progress_events = []

        async def _progress(payload):
            progress_events.append(dict(payload))

        with tempfile.TemporaryDirectory() as tmp:
            service = self._make_service(provider, Path(tmp))
            await service.run_native_tool_loop(
                user_message="inspect and report",
                chat_id=1,
                user_id=1,
                session_id="test-sess",
                progress_callback=_progress,
            )

        action_events = [e for e in progress_events if e.get("event") == "loop.action.started"]
        self.assertEqual(len(action_events), 1)
        self.assertEqual(action_events[0].get("tools"), ["read_file", "git_status"])
        self.assertEqual(int(action_events[0].get("steps_total") or 0), 2)

    async def test_native_loop_pauses_when_action_budget_reached(self):
        from codex_telegram_bot.services.agent_service import AUTONOMOUS_ACTION_MAX_BATCHES_ENV

        provider = _FakeProvider([
            {
                "content": [{"type": "tool_use", "id": "toolu_1", "name": "git_status", "input": {}}],
                "stop_reason": "tool_use",
                "usage": {},
            },
            {
                "content": [{"type": "tool_use", "id": "toolu_2", "name": "git_diff", "input": {}}],
                "stop_reason": "tool_use",
                "usage": {},
            },
        ])
        with patch.dict(os.environ, {AUTONOMOUS_ACTION_MAX_BATCHES_ENV: "1"}, clear=False):
            with tempfile.TemporaryDirectory() as tmp:
                service = self._make_service(provider, Path(tmp))
                result = await service.run_native_tool_loop(
                    user_message="keep iterating",
                    chat_id=1,
                    user_id=1,
                    session_id="test-sess",
                )
        self.assertIn("Preliminary report", result)
        self.assertIn("Do you want me to continue?", result)
        self.assertIn("git_status", result)
        self.assertIn(PRELIMINARY_CONTINUE_HANDOFF, result)

    async def test_tool_call_then_final_reply(self):
        """Model calls a tool, gets result, then gives final reply."""
        provider = _FakeProvider([
            # Turn 1: model requests git_status
            {
                "content": [
                    {"type": "tool_use", "id": "toolu_1", "name": "git_status", "input": {}},
                ],
                "stop_reason": "tool_use",
                "usage": {},
            },
            # Turn 2: model gives final reply after seeing tool result
            {
                "content": [{"type": "text", "text": "The repo is clean."}],
                "stop_reason": "end_turn",
                "usage": {},
            },
        ])
        with tempfile.TemporaryDirectory() as tmp:
            service = self._make_service(provider, Path(tmp))
            result = await service.run_native_tool_loop(
                user_message="Check the git status",
                chat_id=1,
                user_id=1,
                session_id="test-sess",
            )
        self.assertEqual(result, "The repo is clean.")
        # Provider should have been called twice (initial + after tool result)
        self.assertEqual(len(provider.calls), 2)
        # Second call should include tool results
        second_messages = provider.calls[1]["messages"]
        # Last message should be tool results
        last_msg = second_messages[-1]
        self.assertEqual(last_msg["role"], "user")
        self.assertIsInstance(last_msg["content"], list)
        self.assertEqual(last_msg["content"][0]["type"], "tool_result")
        self.assertEqual(last_msg["content"][0]["tool_use_id"], "toolu_1")

    async def test_multiple_tool_calls_in_sequence(self):
        """Model calls multiple tools across turns."""
        provider = _FakeProvider([
            # Turn 1: read_file
            {
                "content": [
                    {"type": "tool_use", "id": "toolu_1", "name": "git_status", "input": {}},
                ],
                "stop_reason": "tool_use",
                "usage": {},
            },
            # Turn 2: another tool call
            {
                "content": [
                    {"type": "tool_use", "id": "toolu_2", "name": "git_diff", "input": {}},
                ],
                "stop_reason": "tool_use",
                "usage": {},
            },
            # Turn 3: final reply
            {
                "content": [{"type": "text", "text": "No changes found."}],
                "stop_reason": "end_turn",
                "usage": {},
            },
        ])
        with tempfile.TemporaryDirectory() as tmp:
            service = self._make_service(provider, Path(tmp))
            result = await service.run_native_tool_loop(
                user_message="Show me the changes",
                chat_id=1,
                user_id=1,
                session_id="test-sess",
            )
        self.assertEqual(result, "No changes found.")
        self.assertEqual(len(provider.calls), 3)

    async def test_tool_result_truncation(self):
        """Large tool results should be truncated."""
        # Create a provider that calls read_file on a large file
        provider = _FakeProvider([
            {
                "content": [
                    {"type": "tool_use", "id": "toolu_1", "name": "read_file", "input": {"path": "big.txt"}},
                ],
                "stop_reason": "tool_use",
                "usage": {},
            },
            {
                "content": [{"type": "text", "text": "File is large."}],
                "stop_reason": "end_turn",
                "usage": {},
            },
        ])
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            service = self._make_service(provider, tmp_path)
            # Create workspace dir and big file
            ws = service.session_workspace("test-sess")
            big_file = ws / "big.txt"
            big_file.write_text("x" * 50000, encoding="utf-8")
            result = await service.run_native_tool_loop(
                user_message="Read the big file",
                chat_id=1,
                user_id=1,
                session_id="test-sess",
            )
        self.assertEqual(result, "File is large.")
        # Verify the tool result sent to the provider was truncated
        second_call = provider.calls[1]["messages"]
        tool_result_msg = second_call[-1]
        tool_result_content = tool_result_msg["content"][0]["content"]
        self.assertLessEqual(len(tool_result_content), 4200)  # 4000 + truncation message

    async def test_fallback_to_legacy_when_provider_lacks_tools(self):
        """If the provider doesn't have generate_with_tools, fall back to legacy."""

        class _LegacyProvider:
            async def generate(self, messages, stream=False, correlation_id="", policy_profile="balanced"):
                return "Legacy response"

            async def execute(self, prompt, correlation_id="", policy_profile="balanced"):
                return "Legacy response"

            async def version(self):
                return "legacy/1.0"

            async def health(self):
                return {"status": "healthy"}

        provider = _LegacyProvider()
        with tempfile.TemporaryDirectory() as tmp:
            service = self._make_service(provider, Path(tmp))
            # This should fall back to legacy loop which calls run_prompt_with_tool_loop
            # Mock run_prompt_with_tool_loop to verify fallback
            service.run_prompt_with_tool_loop = AsyncMock(return_value="Legacy fallback result")
            result = await service.run_native_tool_loop(
                user_message="Hello",
                chat_id=1,
                user_id=1,
                session_id="test-sess",
            )
        self.assertEqual(result, "Legacy fallback result")
        service.run_prompt_with_tool_loop.assert_awaited_once()

    async def test_tool_schemas_passed_to_provider(self):
        """Verify that tool schemas are passed in the tools parameter."""
        provider = _FakeProvider([
            {
                "content": [{"type": "text", "text": "Done."}],
                "stop_reason": "end_turn",
                "usage": {},
            }
        ])
        with tempfile.TemporaryDirectory() as tmp:
            service = self._make_service(provider, Path(tmp))
            await service.run_native_tool_loop(
                user_message="Hello",
                chat_id=1,
                user_id=1,
                session_id="test-sess",
            )
        # Verify tools were passed to the provider
        call = provider.calls[0]
        self.assertIsInstance(call["tools"], list)
        self.assertTrue(len(call["tools"]) > 0)
        # Verify each tool has the required schema fields
        for tool in call["tools"]:
            self.assertIn("name", tool)
            self.assertIn("description", tool)
            self.assertIn("input_schema", tool)


class TestNativeLoopRouter(unittest.IsolatedAsyncioTestCase):
    async def test_router_uses_native_loop_when_enabled(self):
        from codex_telegram_bot.agent_core.router import AgentRouter

        service = type("MockService", (), {
            "run_native_tool_loop": AsyncMock(return_value="native result"),
            "run_prompt_with_tool_loop": AsyncMock(return_value="legacy result"),
        })()
        router = AgentRouter(agent_service=service)

        with patch.dict("os.environ", {"NATIVE_TOOL_LOOP": "1"}):
            result = await router.route_prompt(
                prompt="test", chat_id=1, user_id=1, session_id="s1",
            )

        self.assertEqual(result, "native result")
        service.run_native_tool_loop.assert_awaited_once()
        service.run_prompt_with_tool_loop.assert_not_awaited()

    async def test_router_uses_legacy_loop_by_default(self):
        from codex_telegram_bot.agent_core.router import AgentRouter

        service = type("MockService", (), {
            "run_native_tool_loop": AsyncMock(return_value="native result"),
            "run_prompt_with_tool_loop": AsyncMock(return_value="legacy result"),
        })()
        router = AgentRouter(agent_service=service)

        with patch.dict("os.environ", {}, clear=False):
            # Ensure NATIVE_TOOL_LOOP is not set
            import os
            os.environ.pop("NATIVE_TOOL_LOOP", None)
            result = await router.route_prompt(
                prompt="test", chat_id=1, user_id=1, session_id="s1",
            )

        self.assertEqual(result, "legacy result")
        service.run_prompt_with_tool_loop.assert_awaited_once()
        service.run_native_tool_loop.assert_not_awaited()


# ---------------------------------------------------------------------------
# Anthropic provider response extraction tests
# ---------------------------------------------------------------------------


class TestAnthropicFullResponseExtraction(unittest.TestCase):
    def test_extract_httpx_full_response_text_only(self):
        from codex_telegram_bot.providers.anthropic_provider import _extract_httpx_full_response

        data = {
            "content": [{"type": "text", "text": "Hello!"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }
        result = _extract_httpx_full_response(data)
        self.assertEqual(len(result["content"]), 1)
        self.assertEqual(result["content"][0]["type"], "text")
        self.assertEqual(result["content"][0]["text"], "Hello!")
        self.assertEqual(result["stop_reason"], "end_turn")

    def test_extract_httpx_full_response_tool_use(self):
        from codex_telegram_bot.providers.anthropic_provider import _extract_httpx_full_response

        data = {
            "content": [
                {"type": "text", "text": "I'll check that."},
                {
                    "type": "tool_use",
                    "id": "toolu_abc",
                    "name": "read_file",
                    "input": {"path": "test.txt"},
                },
            ],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 20, "output_tokens": 15},
        }
        result = _extract_httpx_full_response(data)
        self.assertEqual(len(result["content"]), 2)
        self.assertEqual(result["content"][0]["type"], "text")
        self.assertEqual(result["content"][1]["type"], "tool_use")
        self.assertEqual(result["content"][1]["id"], "toolu_abc")
        self.assertEqual(result["content"][1]["name"], "read_file")
        self.assertEqual(result["content"][1]["input"], {"path": "test.txt"})
        self.assertEqual(result["stop_reason"], "tool_use")
