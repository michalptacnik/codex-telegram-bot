import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from codex_telegram_bot.app_container import _read_provider_backend, build_agent_service
from codex_telegram_bot.events.event_bus import EventBus
from codex_telegram_bot.persistence.sqlite_store import SqliteRunStore
from codex_telegram_bot.providers.registry import ProviderRegistry
from codex_telegram_bot.services.agent_service import AgentService


class _FakeProvider:
    def __init__(self, name: str):
        self._name = name
        self.calls = 0

    async def generate(self, messages, stream=False, correlation_id="", policy_profile="balanced") -> str:
        self.calls += 1
        return f"{self._name}:ok"

    async def execute(self, prompt: str, correlation_id: str = "", policy_profile: str = "balanced") -> str:
        self.calls += 1
        return f"{self._name}:ok"

    async def version(self) -> str:
        return f"{self._name}/v1"

    async def health(self):
        return {"status": "healthy", "provider": self._name}

    def capabilities(self):
        return {"provider": self._name}


class TestMultiProviderEnablement(unittest.IsolatedAsyncioTestCase):
    def test_default_agent_is_seeded_as_trusted(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SqliteRunStore(Path(tmp) / "state.db")
            default = store.get_agent("default")
            self.assertIsNotNone(default)
            assert default is not None
            self.assertEqual(default.policy_profile, "trusted")

    def test_recover_interrupted_runs_marks_stale_running_as_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SqliteRunStore(Path(tmp) / "state.db")
            run_id = store.create_run("hello")
            store.mark_running(run_id)
            old_started = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
            with store._connect() as conn:  # test-only direct DB tweak
                conn.execute("UPDATE runs SET started_at=? WHERE run_id=?", (old_started, run_id))
            fixed = store.recover_interrupted_runs(stale_after_sec=5)
            self.assertEqual(fixed, 1)
            run = store.get_run(run_id)
            self.assertIsNotNone(run)
            assert run is not None
            self.assertEqual(run.status, "failed")
            self.assertIn("Recovered after restart", run.error)

    async def test_agent_validation_accepts_supported_providers(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SqliteRunStore(Path(tmp) / "state.db")
            service = AgentService(provider=_FakeProvider("fallback"), run_store=store, event_bus=EventBus())
            for provider in ("codex_cli", "openai", "anthropic", "gemini", "deepseek", "qwen", "quen"):
                saved = service.upsert_agent(
                    agent_id=f"agent_{provider.replace('-', '_')}",
                    name=f"Agent {provider}",
                    provider=provider,
                    policy_profile="balanced",
                    max_concurrency=1,
                    enabled=True,
                )
                expected = "qwen" if provider == "quen" else provider
                self.assertEqual(saved.provider, expected)

    async def test_service_routes_by_agent_provider_when_registry_available(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SqliteRunStore(Path(tmp) / "state.db")
            registry = ProviderRegistry(default_provider_name="codex_cli")
            codex = _FakeProvider("codex_cli")
            openai = _FakeProvider("openai")
            registry.register("codex_cli", codex, make_active=True)
            registry.register("openai", openai)
            service = AgentService(
                provider=registry,
                provider_registry=registry,
                run_store=store,
                event_bus=EventBus(),
            )
            service.upsert_agent(
                agent_id="writer",
                name="Writer",
                provider="openai",
                policy_profile="balanced",
                max_concurrency=1,
                enabled=True,
            )

            out_writer = await service.run_prompt("hello", agent_id="writer")
            out_default = await service.run_prompt("hello", agent_id="default")

            self.assertEqual(out_writer, "openai:ok")
            self.assertEqual(out_default, "codex_cli:ok")
            self.assertEqual(openai.calls, 1)
            self.assertEqual(codex.calls, 1)

    async def test_build_agent_service_honors_backend_alias(self):
        with patch.dict("os.environ", {"PROVIDER_BACKEND": "quen"}, clear=False):
            service = build_agent_service()
            registry = service.provider_registry()
            self.assertIsNotNone(registry)
            self.assertEqual(registry.get_active_name(), "qwen")
            await service.shutdown()

    def test_provider_backend_alias_quen(self):
        with patch.dict("os.environ", {"PROVIDER_BACKEND": "quen"}, clear=False):
            self.assertEqual(_read_provider_backend(), "qwen")
