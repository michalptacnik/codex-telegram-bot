import tempfile
import unittest
from pathlib import Path

from codex_telegram_bot.domain.contracts import CommandResult
from codex_telegram_bot.providers.fallback import EchoFallbackProvider
from codex_telegram_bot.events.event_bus import EventBus
from codex_telegram_bot.persistence.sqlite_store import SqliteRunStore
from codex_telegram_bot.providers.codex_cli import CodexCliProvider
from codex_telegram_bot.providers.router import ProviderRouter, ProviderRouterConfig
from codex_telegram_bot.services.agent_service import AgentService


class FakeRunner:
    def __init__(self, result: CommandResult):
        self._results = [result]
        self.last_argv = None
        self.last_stdin = None

    def set_results(self, results):
        self._results = list(results)

    async def run(self, argv, stdin_text="", timeout_sec=60):
        self.last_argv = list(argv)
        self.last_stdin = stdin_text
        if len(self._results) > 1:
            return self._results.pop(0)
        return self._results[0]


class TestCodexCliProvider(unittest.IsolatedAsyncioTestCase):
    async def test_execute_success(self):
        runner = FakeRunner(CommandResult(returncode=0, stdout="hello", stderr=""))
        provider = CodexCliProvider(runner=runner)

        output = await provider.execute("prompt")

        self.assertEqual(output, "hello")
        self.assertEqual(runner.last_argv[0:3], ["codex", "exec", "-"])

    async def test_execute_nonzero_return_code(self):
        runner = FakeRunner(CommandResult(returncode=2, stdout="", stderr="bad args"))
        provider = CodexCliProvider(runner=runner)

        output = await provider.execute("prompt")

        self.assertIn("Error: codex exited with code 2.", output)
        self.assertIn("bad args", output)

    async def test_version(self):
        runner = FakeRunner(CommandResult(returncode=0, stdout="codex 1.2.3", stderr=""))
        provider = CodexCliProvider(runner=runner)

        version = await provider.version()

        self.assertEqual(version, "codex 1.2.3")
        self.assertEqual(runner.last_argv, ["codex", "--version"])


class TestAgentService(unittest.IsolatedAsyncioTestCase):
    async def test_service_delegates_to_provider(self):
        runner = FakeRunner(CommandResult(returncode=0, stdout="ok", stderr=""))
        provider = CodexCliProvider(runner=runner)
        service = AgentService(provider=provider)

        result = await service.run_prompt("ping")

        self.assertEqual(result, "ok")

    async def test_service_persists_run_lifecycle(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "state.db"
            store = SqliteRunStore(db_path=db_path)
            bus = EventBus()
            runner = FakeRunner(CommandResult(returncode=0, stdout="done", stderr=""))
            provider = CodexCliProvider(runner=runner)
            service = AgentService(provider=provider, run_store=store, event_bus=bus)

            output = await service.run_prompt("hello")

            self.assertEqual(output, "done")
            recent = service.list_recent_runs(limit=5)
            self.assertEqual(len(recent), 1)
            self.assertEqual(recent[0].status, "completed")
            self.assertEqual(recent[0].output, "done")
            events = service.list_run_events(recent[0].run_id, limit=10)
            self.assertEqual(len(events), 2)
            self.assertEqual(events[0].event_type, "run.started")
            self.assertEqual(events[1].event_type, "run.completed")

    async def test_service_marks_failed_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "state.db"
            store = SqliteRunStore(db_path=db_path)
            bus = EventBus()
            runner = FakeRunner(CommandResult(returncode=2, stdout="", stderr="boom"))
            provider = CodexCliProvider(runner=runner)
            service = AgentService(provider=provider, run_store=store, event_bus=bus)

            output = await service.run_prompt("hello")

            self.assertIn("Error: codex exited with code 2.", output)
            recent = service.list_recent_runs(limit=5)
            self.assertEqual(len(recent), 1)
            self.assertEqual(recent[0].status, "failed")
            self.assertIn("Error:", recent[0].error)

    async def test_agent_registry_upsert_and_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "state.db"
            store = SqliteRunStore(db_path=db_path)
            bus = EventBus()
            runner = FakeRunner(CommandResult(returncode=0, stdout="ok", stderr=""))
            provider = CodexCliProvider(runner=runner)
            service = AgentService(provider=provider, run_store=store, event_bus=bus)

            agents_before = service.list_agents()
            self.assertTrue(any(a.agent_id == "default" for a in agents_before))

            saved = service.upsert_agent(
                agent_id="planner",
                name="Planner Agent",
                provider="codex_cli",
                policy_profile="balanced",
                max_concurrency=1,
                enabled=True,
            )
            self.assertEqual(saved.agent_id, "planner")
            fetched = service.get_agent("planner")
            self.assertIsNotNone(fetched)
            self.assertEqual(fetched.name, "Planner Agent")

            deleted = service.delete_agent("planner")
            self.assertTrue(deleted)

            with self.assertRaises(ValueError):
                service.upsert_agent(
                    agent_id="BAD ID",
                    name="Invalid",
                    provider="codex_cli",
                    policy_profile="balanced",
                    max_concurrency=1,
                    enabled=True,
                )

    async def test_handoff_protocol_events_and_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "state.db"
            store = SqliteRunStore(db_path=db_path)
            bus = EventBus()
            runner = FakeRunner(CommandResult(returncode=0, stdout="ok", stderr=""))
            provider = CodexCliProvider(runner=runner)
            service = AgentService(provider=provider, run_store=store, event_bus=bus)

            service.upsert_agent(
                agent_id="researcher",
                name="Researcher",
                provider="codex_cli",
                policy_profile="balanced",
                max_concurrency=1,
                enabled=True,
            )
            await service.run_prompt("seed")
            parent_run = service.list_recent_runs(limit=1)[0]

            res_ok = await service.handoff_prompt(
                from_agent_id="default",
                to_agent_id="researcher",
                prompt="investigate",
                parent_run_id=parent_run.run_id,
            )
            self.assertEqual(res_ok["status"], "completed")
            events_ok = service.list_run_events(parent_run.run_id, limit=50)
            event_types_ok = [e.event_type for e in events_ok]
            self.assertIn("handoff.requested", event_types_ok)
            self.assertIn("handoff.accepted", event_types_ok)
            self.assertIn("handoff.completed", event_types_ok)

            service.upsert_agent(
                agent_id="researcher",
                name="Researcher",
                provider="codex_cli",
                policy_profile="balanced",
                max_concurrency=1,
                enabled=False,
            )
            res_recover = await service.handoff_prompt(
                from_agent_id="default",
                to_agent_id="researcher",
                prompt="recover",
                parent_run_id=parent_run.run_id,
            )
            self.assertEqual(res_recover["target_agent_id"], "default")
            self.assertEqual(res_recover["status"], "completed")
            events_recover = service.list_run_events(parent_run.run_id, limit=100)
            event_types_recover = [e.event_type for e in events_recover]
            self.assertIn("handoff.recovered", event_types_recover)


class TestProviderRouter(unittest.IsolatedAsyncioTestCase):
    async def test_retry_then_success_on_primary(self):
        runner = FakeRunner(CommandResult(returncode=2, stdout="", stderr="tmp fail"))
        runner.set_results(
            [
                CommandResult(returncode=2, stdout="", stderr="tmp fail"),
                CommandResult(returncode=0, stdout="ok", stderr=""),
            ]
        )
        primary = CodexCliProvider(runner=runner)
        router = ProviderRouter(
            primary=primary,
            fallback=EchoFallbackProvider(),
            config=ProviderRouterConfig(retry_attempts=2, failure_threshold=3, recovery_sec=2),
        )

        out = await router.execute("hello")

        self.assertEqual(out, "ok")
        health = await router.health()
        self.assertEqual(health["active_provider"], "primary")

    async def test_fallback_kicks_in_after_threshold(self):
        runner = FakeRunner(CommandResult(returncode=2, stdout="", stderr="bad"))
        primary = CodexCliProvider(runner=runner)
        fallback = EchoFallbackProvider()
        router = ProviderRouter(
            primary=primary,
            fallback=fallback,
            config=ProviderRouterConfig(retry_attempts=1, failure_threshold=1, recovery_sec=60),
        )

        out1 = await router.execute("hello")
        out2 = await router.execute("hello again")

        self.assertIn("Fallback mode active", out1)
        self.assertIn("Fallback mode active", out2)
        health = await router.health()
        self.assertTrue(health["circuit_open"])
        self.assertEqual(health["active_provider"], "fallback")
