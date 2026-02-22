import tempfile
import unittest
from pathlib import Path

from codex_telegram_bot.domain.contracts import CommandResult
from codex_telegram_bot.events.event_bus import EventBus
from codex_telegram_bot.persistence.sqlite_store import SqliteRunStore
from codex_telegram_bot.providers.codex_cli import CodexCliProvider
from codex_telegram_bot.services.agent_service import AgentService

try:
    from fastapi.testclient import TestClient
    from codex_telegram_bot.control_center.app import create_app, create_app_with_config
except Exception:  # pragma: no cover - optional for environments without fastapi
    TestClient = None
    create_app = None
    create_app_with_config = None


class _FakeRunner:
    def __init__(self, result: CommandResult):
        self._results = [result]

    def set_results(self, results):
        self._results = list(results)

    async def run(self, argv, stdin_text="", timeout_sec=60, policy_profile="balanced", workspace_root=""):
        if len(self._results) > 1:
            return self._results.pop(0)
        return self._results[0]


@unittest.skipIf(TestClient is None or create_app is None, "fastapi test deps unavailable")
class TestControlCenter(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db_path = Path(self.tmp.name) / "state.db"
        store = SqliteRunStore(db_path=db_path)
        bus = EventBus()
        provider = CodexCliProvider(runner=_FakeRunner(CommandResult(0, "ok-output", "")))
        self.service = AgentService(provider=provider, run_store=store, event_bus=bus)
        await self.service.run_prompt("hello")

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def test_health_and_runs_endpoints(self):
        app = create_app(self.service)
        client = TestClient(app)

        health = client.get("/health")
        self.assertEqual(health.status_code, 200)
        self.assertEqual(health.json()["status"], "ok")
        self.assertIn("provider_version", health.json())
        self.assertIn("provider_health", health.json())
        self.assertIn("metrics", health.json())

        metrics = client.get("/api/metrics")
        self.assertEqual(metrics.status_code, 200)
        self.assertIn("total_runs", metrics.json())

        runs = client.get("/api/runs")
        self.assertEqual(runs.status_code, 200)
        data = runs.json()
        self.assertTrue(len(data) >= 1)
        self.assertEqual(data[0]["status"], "completed")

        run_id = data[0]["run_id"]
        run_detail = client.get(f"/api/runs/{run_id}")
        self.assertEqual(run_detail.status_code, 200)
        self.assertEqual(run_detail.json()["run_id"], run_id)

        events = client.get(f"/api/runs/{run_id}/events")
        self.assertEqual(events.status_code, 200)
        self.assertTrue(len(events.json()) >= 2)

        artifact = client.get(f"/api/runs/{run_id}/artifact.txt")
        self.assertEqual(artifact.status_code, 200)
        self.assertIn("attachment;", artifact.headers.get("content-disposition", ""))
        self.assertIn(run_id, artifact.text)

        handoff = client.post(
            "/api/handoffs",
            json={
                "from_agent_id": "default",
                "to_agent_id": "default",
                "prompt": "handoff check",
                "parent_run_id": run_id,
            },
        )
        self.assertEqual(handoff.status_code, 200)
        self.assertIn("status", handoff.json())

    async def test_html_pages_render(self):
        app = create_app(self.service)
        client = TestClient(app)

        dashboard = client.get("/")
        self.assertEqual(dashboard.status_code, 200)
        self.assertIn("Dashboard", dashboard.text)

        runs_page = client.get("/runs")
        self.assertEqual(runs_page.status_code, 200)
        self.assertIn("Execution history", runs_page.text)

        run_id = self.service.list_recent_runs(limit=1)[0].run_id
        detail_page = client.get(f"/runs/{run_id}")
        self.assertEqual(detail_page.status_code, 200)
        self.assertIn(run_id, detail_page.text)

        settings = client.get("/settings")
        self.assertEqual(settings.status_code, 200)
        self.assertIn("Provider Version", settings.text)

        agents = client.get("/agents")
        self.assertEqual(agents.status_code, 200)
        self.assertIn("Registered Agents", agents.text)

    async def test_error_catalog_and_recovery_api(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            db_path = Path(tmp.name) / "state.db"
            store = SqliteRunStore(db_path=db_path)
            bus = EventBus()
            runner = _FakeRunner(CommandResult(2, "", "bad args"))
            provider = CodexCliProvider(runner=runner)
            service = AgentService(provider=provider, run_store=store, event_bus=bus)
            await service.run_prompt("should fail")
            failed_run = service.list_recent_runs(limit=1)[0]
            self.assertEqual(failed_run.status, "failed")

            app = create_app(service)
            client = TestClient(app)

            catalog = client.get("/api/error-catalog")
            self.assertEqual(catalog.status_code, 200)
            self.assertTrue(len(catalog.json()) >= 10)

            run_payload = client.get(f"/api/runs/{failed_run.run_id}")
            self.assertEqual(run_payload.status_code, 200)
            self.assertEqual(run_payload.json()["error_code"], "ERR_CODEX_EXIT_NONZERO")

            recovery_options = client.get(f"/api/runs/{failed_run.run_id}/recovery-options")
            self.assertEqual(recovery_options.status_code, 200)
            self.assertTrue(any(a["action_id"] == "retry_same_agent" for a in recovery_options.json()["actions"]))

            recover = client.post(
                f"/api/runs/{failed_run.run_id}/recover",
                json={"action_id": "retry_same_agent"},
            )
            self.assertEqual(recover.status_code, 200)
            self.assertEqual(recover.json()["status"], "queued")
            self.assertIn("job_id", recover.json())

            events = client.get(f"/api/runs/{failed_run.run_id}/events")
            self.assertEqual(events.status_code, 200)
            event_types = [e["event_type"] for e in events.json()]
            self.assertIn("recovery.attempted", event_types)
            self.assertIn("recovery.queued", event_types)
        finally:
            tmp.cleanup()

    async def test_onboarding_status_endpoint(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            app = create_app_with_config(self.service, config_dir=Path(tmp.name))
            client = TestClient(app)
            status_before = client.get("/api/onboarding/status")
            self.assertEqual(status_before.status_code, 200)
            self.assertFalse(status_before.json()["completed"])

            page = client.get("/onboarding")
            self.assertEqual(page.status_code, 200)
            self.assertIn("Onboarding Wizard", page.text)

            status_after = client.get("/api/onboarding/status")
            self.assertEqual(status_after.status_code, 200)
            self.assertIn("wizard.view:visit", status_after.json()["telemetry"]["steps"])
        finally:
            tmp.cleanup()

    async def test_sessions_endpoint_and_page(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            db_path = Path(tmp.name) / "state.db"
            store = SqliteRunStore(db_path=db_path)
            bus = EventBus()
            provider = CodexCliProvider(runner=_FakeRunner(CommandResult(0, "ok", "")))
            service = AgentService(provider=provider, run_store=store, event_bus=bus)
            service.get_or_create_session(chat_id=1, user_id=2)

            app = create_app(service)
            client = TestClient(app)
            api = client.get("/api/sessions")
            self.assertEqual(api.status_code, 200)
            self.assertTrue(len(api.json()) >= 1)

            page = client.get("/sessions")
            self.assertEqual(page.status_code, 200)
            self.assertIn("Telegram chat session registry", page.text)

            approvals_api = client.get("/api/approvals")
            self.assertEqual(approvals_api.status_code, 200)
            deny_api = client.post(
                "/api/approvals/deny",
                json={"approval_id": "missing", "chat_id": 1, "user_id": 2},
            )
            self.assertEqual(deny_api.status_code, 200)
            self.assertIn("Error:", deny_api.json()["output"])
            retrieval_stats = client.get("/api/retrieval/stats")
            self.assertEqual(retrieval_stats.status_code, 200)
            retrieval_refresh = client.post("/api/retrieval/refresh")
            self.assertEqual(retrieval_refresh.status_code, 200)

            approvals_page = client.get("/approvals")
            self.assertEqual(approvals_page.status_code, 200)
            self.assertIn("Pending high-risk tool actions", approvals_page.text)
        finally:
            tmp.cleanup()
