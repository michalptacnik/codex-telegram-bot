"""Tests for EPICs 3, 4, and 5.

EPIC 3 (#66) – Multi-Provider Architecture:
  - AnthropicProvider capabilities/health/version
  - ProviderRegistry register/switch/list/delegate/history

EPIC 4 (#67) – Streaming and CLI-like Feedback:
  - StreamingUpdater with a mock async generator
  - stream_prompt_to_telegram helper (streaming & non-streaming paths)

EPIC 5 (#68) – Lightweight Web Control Center extensions:
  - /api/providers  (list)
  - /api/providers/switch (switch)
  - /api/providers/health
  - /api/mission-metrics
  - /api/mission-metrics/text
  - /api/logs/stream (SSE)

EPIC 3 tool tests:
  - ProviderStatusTool
  - ProviderSwitchTool
"""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from codex_telegram_bot.providers.anthropic_provider import AnthropicProvider
from codex_telegram_bot.providers.registry import ProviderNotFoundError, ProviderRegistry
from codex_telegram_bot.services.streaming import StreamingUpdater, stream_prompt_to_telegram
from codex_telegram_bot.tools.provider import ProviderStatusTool, ProviderSwitchTool
from codex_telegram_bot.tools.base import ToolContext, ToolRequest


def _fake_provider(name: str = "fake", streaming: bool = False) -> MagicMock:
    p = MagicMock()
    p.capabilities.return_value = {
        "provider": name,
        "supports_streaming": streaming,
        "supports_tool_calls": False,
        "max_context_chars": 100_000,
        "supported_policy_profiles": ["balanced"],
    }
    p.generate = AsyncMock(return_value=f"response from {name}")
    p.execute = AsyncMock(return_value=f"response from {name}")
    p.version = AsyncMock(return_value=f"{name}/v1")
    p.health = AsyncMock(return_value={"status": "healthy", "provider": name})
    return p


def _make_store_and_service(tmp: str):
    from codex_telegram_bot.persistence.sqlite_store import SqliteRunStore
    from codex_telegram_bot.services.agent_service import AgentService
    store = SqliteRunStore(Path(tmp) / "test.db")
    provider = _fake_provider()
    service = AgentService(store=store, provider=provider)
    return store, service


# ---------------------------------------------------------------------------
# EPIC 3 – AnthropicProvider
# ---------------------------------------------------------------------------

class TestAnthropicProvider(unittest.IsolatedAsyncioTestCase):
    def test_capabilities_structure(self):
        p = AnthropicProvider(api_key="sk-test")
        caps = p.capabilities()
        self.assertEqual(caps["provider"], "anthropic")
        self.assertTrue(caps["supports_streaming"])
        self.assertGreater(caps["max_context_chars"], 0)

    async def test_health_missing_key(self):
        p = AnthropicProvider(api_key="")
        h = await p.health()
        self.assertEqual(h["status"], "unhealthy")
        self.assertEqual(h["reason"], "missing_api_key")

    async def test_health_with_key(self):
        p = AnthropicProvider(api_key="sk-test", model="claude-opus-4-6")
        h = await p.health()
        self.assertEqual(h["status"], "healthy")
        self.assertIn("model", h)

    async def test_version(self):
        p = AnthropicProvider(api_key="sk-test", model="claude-opus-4-6")
        v = await p.version()
        self.assertIn("anthropic", v)

    async def test_generate_no_key_returns_error(self):
        p = AnthropicProvider(api_key="")
        result = await p.generate([{"role": "user", "content": "hello"}])
        self.assertTrue(result.startswith("Error:"))

    async def test_generate_stream_no_key(self):
        p = AnthropicProvider(api_key="")
        chunks = []
        async for chunk in p.generate_stream([{"role": "user", "content": "hi"}]):
            chunks.append(chunk)
        combined = "".join(chunks)
        self.assertIn("Error", combined)

    async def test_generate_buffered_via_stream_flag(self):
        p = AnthropicProvider(api_key="")
        # stream=True should still work (returns string)
        result = await p.generate([{"role": "user", "content": "x"}], stream=True)
        self.assertIsInstance(result, str)

    async def test_execute_delegates_to_generate(self):
        p = AnthropicProvider(api_key="")
        result = await p.execute("hello")
        self.assertIsInstance(result, str)


# ---------------------------------------------------------------------------
# EPIC 3 – ProviderRegistry
# ---------------------------------------------------------------------------

class TestProviderRegistry(unittest.IsolatedAsyncioTestCase):
    def test_register_and_list(self):
        reg = ProviderRegistry()
        p1 = _fake_provider("p1")
        p2 = _fake_provider("p2")
        reg.register("p1", p1, make_active=True)
        reg.register("p2", p2)
        providers = reg.list_providers()
        names = [p["name"] for p in providers]
        self.assertIn("p1", names)
        self.assertIn("p2", names)

    def test_active_provider(self):
        reg = ProviderRegistry()
        p1 = _fake_provider("p1")
        reg.register("p1", p1, make_active=True)
        self.assertEqual(reg.get_active_name(), "p1")

    def test_switch_provider(self):
        reg = ProviderRegistry()
        reg.register("a", _fake_provider("a"), make_active=True)
        reg.register("b", _fake_provider("b"))
        msg = reg.switch("b")
        self.assertIn("b", msg)
        self.assertEqual(reg.get_active_name(), "b")

    def test_switch_nonexistent_raises(self):
        reg = ProviderRegistry()
        reg.register("a", _fake_provider("a"), make_active=True)
        with self.assertRaises(ProviderNotFoundError):
            reg.switch("nonexistent")

    def test_switch_history_recorded(self):
        reg = ProviderRegistry()
        reg.register("a", _fake_provider("a"), make_active=True)
        reg.register("b", _fake_provider("b"))
        reg.switch("b")
        reg.switch("a")
        self.assertEqual(len(reg.switch_history), 2)
        self.assertEqual(reg.switch_history[-1]["to"], "a")

    async def test_generate_delegates_to_active(self):
        reg = ProviderRegistry()
        p = _fake_provider("active")
        reg.register("active", p, make_active=True)
        result = await reg.generate([{"role": "user", "content": "hi"}])
        self.assertEqual(result, "response from active")

    async def test_execute_delegates(self):
        reg = ProviderRegistry()
        p = _fake_provider("active")
        reg.register("active", p, make_active=True)
        result = await reg.execute("hi")
        self.assertEqual(result, "response from active")

    async def test_health_aggregates_all(self):
        reg = ProviderRegistry()
        reg.register("a", _fake_provider("a"), make_active=True)
        reg.register("b", _fake_provider("b"))
        health = await reg.health()
        self.assertIn("providers", health)
        self.assertIn("a", health["providers"])
        self.assertIn("b", health["providers"])

    async def test_version_delegates(self):
        reg = ProviderRegistry()
        p = _fake_provider("myprov")
        reg.register("myprov", p, make_active=True)
        v = await reg.version()
        self.assertEqual(v, "myprov/v1")

    def test_unregister(self):
        reg = ProviderRegistry()
        reg.register("a", _fake_provider("a"), make_active=True)
        reg.register("b", _fake_provider("b"))
        reg.unregister("b")
        names = [p["name"] for p in reg.list_providers()]
        self.assertNotIn("b", names)

    def test_capabilities_reflect_active(self):
        reg = ProviderRegistry()
        reg.register("a", _fake_provider("a"), make_active=True)
        caps = reg.capabilities()
        self.assertIn("active", caps)
        self.assertEqual(caps["active"], "a")

    def test_active_marker_in_list(self):
        reg = ProviderRegistry()
        reg.register("x", _fake_provider("x"), make_active=True)
        reg.register("y", _fake_provider("y"))
        providers = reg.list_providers()
        active_entries = [p for p in providers if p["active"]]
        self.assertEqual(len(active_entries), 1)
        self.assertEqual(active_entries[0]["name"], "x")

    def test_no_providers_raises_on_active(self):
        reg = ProviderRegistry()
        with self.assertRaises(RuntimeError):
            asyncio.get_event_loop().run_until_complete(reg.generate([]))


# ---------------------------------------------------------------------------
# EPIC 3 – Provider tools
# ---------------------------------------------------------------------------

class TestProviderTools(unittest.TestCase):
    def _ctx(self):
        return ToolContext(workspace_root=Path("/tmp"))

    def test_status_tool_lists_providers(self):
        reg = ProviderRegistry()
        reg.register("codex_cli", _fake_provider("codex_cli"), make_active=True)
        reg.register("anthropic", _fake_provider("anthropic"))
        tool = ProviderStatusTool(reg)
        result = tool.run(ToolRequest(name="provider_status", args={}), self._ctx())
        self.assertTrue(result.ok)
        self.assertIn("codex_cli", result.output)
        self.assertIn("anthropic", result.output)
        self.assertIn("Active provider:", result.output)

    def test_switch_tool_switches(self):
        reg = ProviderRegistry()
        reg.register("a", _fake_provider("a"), make_active=True)
        reg.register("b", _fake_provider("b"))
        tool = ProviderSwitchTool(reg)
        result = tool.run(ToolRequest(name="provider_switch", args={"name": "b"}), self._ctx())
        self.assertTrue(result.ok)
        self.assertEqual(reg.get_active_name(), "b")

    def test_switch_tool_missing_name(self):
        reg = ProviderRegistry()
        reg.register("a", _fake_provider("a"), make_active=True)
        tool = ProviderSwitchTool(reg)
        result = tool.run(ToolRequest(name="provider_switch", args={}), self._ctx())
        self.assertFalse(result.ok)
        self.assertIn("required", result.output)

    def test_switch_tool_bad_name(self):
        reg = ProviderRegistry()
        reg.register("a", _fake_provider("a"), make_active=True)
        tool = ProviderSwitchTool(reg)
        result = tool.run(ToolRequest(name="provider_switch", args={"name": "nope"}), self._ctx())
        self.assertFalse(result.ok)

    def test_status_tool_shows_switch_history(self):
        reg = ProviderRegistry()
        reg.register("a", _fake_provider("a"), make_active=True)
        reg.register("b", _fake_provider("b"))
        reg.switch("b")
        tool = ProviderStatusTool(reg)
        result = tool.run(ToolRequest(name="provider_status", args={}), self._ctx())
        self.assertTrue(result.ok)
        self.assertIn("Recent switches", result.output)


# ---------------------------------------------------------------------------
# EPIC 4 – StreamingUpdater
# ---------------------------------------------------------------------------

class TestStreamingUpdater(unittest.IsolatedAsyncioTestCase):
    def _make_bot(self):
        bot = MagicMock()
        sent_msg = MagicMock()
        sent_msg.message_id = 42
        bot.send_message = AsyncMock(return_value=sent_msg)
        bot.edit_message_text = AsyncMock(return_value=None)
        return bot

    async def test_streams_chunks_and_returns_full_text(self):
        async def _gen():
            for word in ["Hello", " ", "world", "!"]:
                yield word

        bot = self._make_bot()
        updater = StreamingUpdater(bot=bot, chat_id=1)
        result = await updater.run(_gen())
        self.assertEqual(result, "Hello world!")

    async def test_sends_placeholder_when_no_message_id(self):
        async def _gen():
            yield "hi"

        bot = self._make_bot()
        updater = StreamingUpdater(bot=bot, chat_id=1)
        await updater.run(_gen())
        bot.send_message.assert_called_once()

    async def test_edits_existing_message_id(self):
        async def _gen():
            yield "x"

        bot = self._make_bot()
        updater = StreamingUpdater(bot=bot, chat_id=1, message_id=99)
        await updater.run(_gen())
        bot.send_message.assert_not_called()
        bot.edit_message_text.assert_called()

    async def test_non_streaming_coroutine_handled(self):
        async def _coro():
            return "done"

        bot = self._make_bot()
        updater = StreamingUpdater(bot=bot, chat_id=1)
        result = await updater.run(_coro())
        self.assertEqual(result, "done")

    async def test_final_edit_has_no_cursor(self):
        edits = []

        async def _gen():
            for ch in "abc":
                yield ch

        bot = MagicMock()
        sent_msg = MagicMock()
        sent_msg.message_id = 1
        bot.send_message = AsyncMock(return_value=sent_msg)

        async def _edit(**kwargs):
            edits.append(kwargs["text"])

        bot.edit_message_text = AsyncMock(side_effect=_edit)
        updater = StreamingUpdater(bot=bot, chat_id=1, suffix="▌")
        await updater.run(_gen())
        # The very last edit should NOT have the cursor suffix
        self.assertFalse(edits[-1].endswith("▌"))

    async def test_on_final_callback(self):
        received = []

        async def _gen():
            yield "hello"

        bot = self._make_bot()
        updater = StreamingUpdater(
            bot=bot, chat_id=1, on_final=lambda t: received.append(t)
        )
        await updater.run(_gen())
        self.assertEqual(received, ["hello"])

    async def test_edit_error_not_modified_ignored(self):
        async def _gen():
            yield "text"

        bot = self._make_bot()
        bot.edit_message_text = AsyncMock(side_effect=Exception("Message is not modified"))
        updater = StreamingUpdater(bot=bot, chat_id=1)
        # Should not raise
        result = await updater.run(_gen())
        self.assertEqual(result, "text")


class TestStreamPromptToTelegram(unittest.IsolatedAsyncioTestCase):
    def _make_bot(self):
        bot = MagicMock()
        sent_msg = MagicMock()
        sent_msg.message_id = 1
        bot.send_message = AsyncMock(return_value=sent_msg)
        bot.edit_message_text = AsyncMock(return_value=None)
        return bot

    async def test_non_streaming_provider(self):
        provider = _fake_provider("np", streaming=False)
        bot = self._make_bot()
        result = await stream_prompt_to_telegram(
            provider=provider,
            messages=[{"role": "user", "content": "hi"}],
            bot=bot,
            chat_id=1,
        )
        self.assertEqual(result, "response from np")

    async def test_streaming_provider_uses_generate_stream(self):
        async def _stream(*args, **kwargs):
            for ch in "streamed":
                yield ch

        provider = _fake_provider("sp", streaming=True)
        provider.generate_stream = _stream
        bot = self._make_bot()
        result = await stream_prompt_to_telegram(
            provider=provider,
            messages=[{"role": "user", "content": "hi"}],
            bot=bot,
            chat_id=1,
        )
        self.assertEqual(result, "streamed")


# ---------------------------------------------------------------------------
# EPIC 5 – Control Center provider/metrics/SSE routes
# ---------------------------------------------------------------------------

class TestControlCenterEpic5(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()

    def _make_client(self, with_registry=True, with_metrics=True):
        from codex_telegram_bot.persistence.sqlite_store import SqliteRunStore
        from codex_telegram_bot.control_center.app import create_app

        store = SqliteRunStore(Path(self._tmpdir) / "test.db")

        # Use a partial mock so we don't need a fully wired AgentService
        service = MagicMock()
        service.list_recent_runs = MagicMock(return_value=[])
        service.provider_version = AsyncMock(return_value="test/v1")
        service.provider_health = AsyncMock(return_value={"status": "healthy"})
        service.list_recent_sessions = MagicMock(return_value=[])
        service.list_all_pending_tool_approvals = MagicMock(return_value=[])
        service.list_agents = MagicMock(return_value=[])

        reg = None
        if with_registry:
            reg = ProviderRegistry()
            reg.register("codex_cli", _fake_provider("codex_cli"), make_active=True)
            reg.register("anthropic", _fake_provider("anthropic"))

        metrics = None
        if with_metrics:
            from codex_telegram_bot.services.observability import MetricsCollector
            metrics = MetricsCollector(store=store)

        app = create_app(
            agent_service=service,
            provider_registry=reg,
            metrics_collector=metrics,
        )
        return TestClient(app, raise_server_exceptions=True)

    def test_api_providers_list(self):
        client = self._make_client()
        resp = client.get("/api/providers")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("providers", data)
        names = [p["name"] for p in data["providers"]]
        self.assertIn("codex_cli", names)
        self.assertIn("anthropic", names)

    def test_api_providers_active_field(self):
        client = self._make_client()
        resp = client.get("/api/providers")
        data = resp.json()
        self.assertEqual(data["active"], "codex_cli")

    def test_api_providers_switch(self):
        client = self._make_client()
        resp = client.post("/api/providers/switch", json={"name": "anthropic"})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["active"], "anthropic")

    def test_api_providers_switch_bad_name(self):
        client = self._make_client()
        resp = client.post("/api/providers/switch", json={"name": "nope"})
        self.assertEqual(resp.status_code, 404)

    def test_api_providers_switch_missing_name(self):
        client = self._make_client()
        resp = client.post("/api/providers/switch", json={})
        self.assertEqual(resp.status_code, 400)

    def test_api_providers_no_registry(self):
        client = self._make_client(with_registry=False)
        resp = client.get("/api/providers")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["providers"], [])

    def test_api_providers_health(self):
        client = self._make_client()
        resp = client.get("/api/providers/health")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("providers", data)

    def test_api_metrics(self):
        client = self._make_client()
        resp = client.get("/api/mission-metrics")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("pending", data)
        self.assertIn("error_rate", data)

    def test_api_metrics_text(self):
        client = self._make_client()
        resp = client.get("/api/mission-metrics/text")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Dashboard", resp.text)

    def test_api_metrics_no_collector(self):
        client = self._make_client(with_metrics=False)
        resp = client.get("/api/mission-metrics")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("error", resp.json())

    def test_api_logs_stream_returns_sse(self):
        client = self._make_client()
        # max_polls=1 makes the generator exit after one polling iteration
        with client.stream("GET", "/api/logs/stream?max_polls=1") as resp:
            self.assertEqual(resp.status_code, 200)
            self.assertIn("text/event-stream", resp.headers.get("content-type", ""))


# ---------------------------------------------------------------------------
# EPIC 3 – build_default_tool_registry with provider_registry
# ---------------------------------------------------------------------------

class TestBuildDefaultToolRegistry(unittest.TestCase):
    def test_without_registry(self):
        from codex_telegram_bot.tools import build_default_tool_registry
        reg = build_default_tool_registry()
        self.assertIsNone(reg.get("provider_status"))
        self.assertIsNone(reg.get("provider_switch"))

    def test_with_registry(self):
        from codex_telegram_bot.tools import build_default_tool_registry
        pr = ProviderRegistry()
        pr.register("a", _fake_provider("a"), make_active=True)
        reg = build_default_tool_registry(provider_registry=pr)
        self.assertIsNotNone(reg.get("provider_status"))
        self.assertIsNotNone(reg.get("provider_switch"))
