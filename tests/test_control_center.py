import tempfile
import unittest
import os
from pathlib import Path

from codex_telegram_bot.domain.contracts import CommandResult
from codex_telegram_bot.events.event_bus import EventBus
from codex_telegram_bot.persistence.sqlite_store import SqliteRunStore
from codex_telegram_bot.providers.codex_cli import CodexCliProvider
from codex_telegram_bot.providers.registry import ProviderRegistry
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


class _FakeProvider:
    def __init__(self, name: str):
        self._name = name

    async def generate(self, messages, stream=False, correlation_id="", policy_profile="balanced") -> str:
        return f"{self._name}:ok"

    async def execute(self, prompt: str, correlation_id: str = "", policy_profile: str = "balanced") -> str:
        return f"{self._name}:ok"

    async def version(self) -> str:
        return f"{self._name}/v1"

    async def health(self):
        return {"status": "healthy", "provider": self._name}

    def capabilities(self):
        return {"provider": self._name}


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
        self.assertIn("runtime", health.json())
        self.assertIn("metrics", health.json())

        metrics = client.get("/api/metrics")
        self.assertEqual(metrics.status_code, 200)
        self.assertIn("total_runs", metrics.json())
        reliability = client.get("/api/reliability")
        self.assertEqual(reliability.status_code, 200)
        self.assertIn("failure_rate", reliability.json())
        runtime_caps = client.get("/api/runtime/capabilities")
        self.assertEqual(runtime_caps.status_code, 200)
        self.assertIn("execution_backend", runtime_caps.json())

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

        playbook = client.get("/api/recovery/playbook")
        self.assertEqual(playbook.status_code, 200)
        self.assertIn("actions", playbook.json())
        self.assertIn("status", handoff.json())

    async def test_html_pages_render(self):
        app = create_app(self.service)
        client = TestClient(app)

        dashboard = client.get("/")
        self.assertEqual(dashboard.status_code, 200)
        self.assertIn("Dashboard", dashboard.text)
        self.assertIn("Skip to content", dashboard.text)
        self.assertIn('aria-current="page"', dashboard.text)

        runs_page = client.get("/runs")
        self.assertEqual(runs_page.status_code, 200)
        self.assertIn("Execution history", runs_page.text)

        run_id = self.service.list_recent_runs(limit=1)[0].run_id
        detail_page = client.get(f"/runs/{run_id}")
        self.assertEqual(detail_page.status_code, 200)
        self.assertIn(run_id, detail_page.text)
        self.assertIn("run.provider.selected", detail_page.text)
        self.assertIn('role="status"', detail_page.text)

        settings = client.get("/settings")
        self.assertEqual(settings.status_code, 200)
        self.assertIn("Provider Version", settings.text)

        agents = client.get("/agents")
        self.assertEqual(agents.status_code, 200)
        self.assertIn("Registered Agents", agents.text)

        plugins = client.get("/plugins")
        self.assertEqual(plugins.status_code, 200)
        self.assertIn("Plugins", plugins.text)

        chat = client.get("/chat")
        self.assertEqual(chat.status_code, 200)
        self.assertIn("Realtime Chat", chat.text)

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
            costs_api = client.get("/api/costs/daily")
            self.assertEqual(costs_api.status_code, 200)
            self.assertIn("items", costs_api.json())
            profile_api = client.get("/api/execution-profile")
            self.assertEqual(profile_api.status_code, 200)
            self.assertEqual(profile_api.json().get("profile"), "safe")
            set_profile = client.post(
                "/api/execution-profile/set",
                json={"profile": "power_user", "user_id": 0},
            )
            self.assertEqual(set_profile.status_code, 200)
            self.assertEqual(set_profile.json().get("profile"), "power_user")
            start_unlock = client.post("/api/execution-profile/start-unsafe-unlock", params={"user_id": 0})
            self.assertEqual(start_unlock.status_code, 200)
            self.assertTrue(start_unlock.json().get("code"))
            costs_page = client.get("/costs")
            self.assertEqual(costs_page.status_code, 200)
            self.assertIn("Costs", costs_page.text)

            approvals_page = client.get("/approvals")
            self.assertEqual(approvals_page.status_code, 200)
            self.assertIn("Pending high-risk tool actions", approvals_page.text)
        finally:
            tmp.cleanup()

    async def test_session_detail_includes_attachments_and_download(self):
        session = self.service.get_or_create_session(chat_id=901, user_id=902)
        ws = self.service.session_workspace(session.session_id)
        path = ws / "attachments" / "m1" / "sample.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("sample-data", encoding="utf-8")

        message_id = self.service.record_channel_message(
            session_id=session.session_id,
            user_id=902,
            channel="telegram",
            channel_message_id="m1",
            sender="user",
            text="uploaded",
        )
        attachment_id = self.service.record_attachment(
            message_id=message_id,
            session_id=session.session_id,
            user_id=902,
            channel="telegram",
            kind="document",
            filename="sample.txt",
            mime="text/plain",
            size_bytes=11,
            sha256="deadbeef",
            local_path=str(path),
            remote_file_id="tg-1",
        )

        app = create_app(self.service)
        client = TestClient(app)

        detail = client.get(f"/api/sessions/{session.session_id}/detail")
        self.assertEqual(detail.status_code, 200)
        attachments = detail.json().get("attachments", [])
        self.assertTrue(any(a.get("id") == attachment_id for a in attachments))

        download = client.get(f"/api/attachments/{attachment_id}/download")
        self.assertEqual(download.status_code, 200)
        self.assertEqual(download.content, b"sample-data")

    async def test_local_api_v1_scoped_auth(self):
        app = None
        old_keys = os.environ.get("LOCAL_API_KEYS")
        os.environ["LOCAL_API_KEYS"] = "reader-token:meta:read,runs:read,jobs:read;writer-token:prompts:write,jobs:write"
        try:
            app = create_app(self.service)
            client = TestClient(app)
            denied = client.get("/api/v1/meta")
            self.assertEqual(denied.status_code, 401)

            meta = client.get("/api/v1/meta", headers={"x-local-api-key": "reader-token"})
            self.assertEqual(meta.status_code, 200)
            self.assertEqual(meta.json()["api_version"], "v1")

            runs = client.get("/api/v1/runs", headers={"authorization": "Bearer reader-token"})
            self.assertEqual(runs.status_code, 200)
            self.assertIn("items", runs.json())

            prompt = client.post(
                "/api/v1/prompts",
                headers={"x-local-api-key": "writer-token"},
                json={"prompt": "hello from api", "agent_id": "default"},
            )
            self.assertEqual(prompt.status_code, 200)
            job_id = prompt.json()["job_id"]

            job_status = client.get(f"/api/v1/jobs/{job_id}", headers={"x-local-api-key": "reader-token"})
            self.assertEqual(job_status.status_code, 200)
            self.assertIn("status", job_status.json())

            forbidden = client.post(f"/api/v1/jobs/{job_id}/cancel", headers={"x-local-api-key": "reader-token"})
            self.assertEqual(forbidden.status_code, 403)
        finally:
            if old_keys is None:
                os.environ.pop("LOCAL_API_KEYS", None)
            else:
                os.environ["LOCAL_API_KEYS"] = old_keys

    async def test_websocket_chat_streaming_and_approval_events(self):
        app = create_app(self.service)
        client = TestClient(app)

        with client.websocket_connect("/ws/chat") as ws:
            ws.send_json(
                {
                    "type": "user_message",
                    "text": "hello from web chat",
                    "chat_id": 1001,
                    "user_id": 2002,
                }
            )
            first_events = []
            for _ in range(60):
                payload = ws.receive_json()
                first_events.append(payload)
                if payload.get("type") == "done":
                    break
            self.assertTrue(any(item.get("type") == "assistant_chunk" for item in first_events))
            done_payload = next(item for item in first_events if item.get("type") == "done")
            session_id = str(done_payload.get("session_id") or "")
            self.assertTrue(session_id)

            ws.send_json(
                {
                    "type": "user_message",
                    "session_id": session_id,
                    "text": "!exec codex --danger-full-access --help",
                }
            )
            second_events = []
            for _ in range(80):
                payload = ws.receive_json()
                second_events.append(payload)
                if payload.get("type") == "done":
                    break
            pending = [
                item
                for item in second_events
                if item.get("type") == "tool_event" and item.get("status") == "awaiting_approval"
            ]
            self.assertTrue(pending)
            detail = pending[0].get("detail") or {}
            approval_id = str(detail.get("approval_id") or "")
            self.assertTrue(approval_id)

            ws.send_json(
                {
                    "type": "deny",
                    "session_id": session_id,
                    "approval_id": approval_id,
                    "chat_id": detail.get("chat_id"),
                    "user_id": detail.get("user_id"),
                }
            )
            deny_event = ws.receive_json()
            self.assertEqual(deny_event.get("type"), "tool_event")
            self.assertEqual(deny_event.get("status"), "result")
            self.assertIn("Denied", str((deny_event.get("detail") or {}).get("output") or ""))

    async def test_whatsapp_link_code_and_webhook(self):
        old_enabled = os.environ.get("WHATSAPP_ENABLED")
        old_token = os.environ.get("WHATSAPP_WEBHOOK_TOKEN")
        try:
            os.environ["WHATSAPP_ENABLED"] = "1"
            os.environ.pop("WHATSAPP_WEBHOOK_TOKEN", None)
            app = create_app(self.service)
            client = TestClient(app)
            link = client.post(
                "/api/whatsapp/link-code",
                json={"chat_id": 101, "user_id": 202, "ttl_sec": 600},
            )
            self.assertEqual(link.status_code, 200)
            code = str(link.json().get("code") or "")
            self.assertTrue(code)

            linked = client.post(
                "/whatsapp/webhook",
                data={"From": "whatsapp:+15551234567", "Body": f"/link {code}"},
            )
            self.assertEqual(linked.status_code, 200)
            self.assertIn("Linked successfully", linked.text)

            reply = client.post(
                "/whatsapp/webhook",
                data={"From": "whatsapp:+15551234567", "Body": "hello from whatsapp"},
            )
            self.assertEqual(reply.status_code, 200)
            self.assertIn("<Response>", reply.text)
            self.assertIn("hello from whatsapp", reply.text)
        finally:
            if old_enabled is None:
                os.environ.pop("WHATSAPP_ENABLED", None)
            else:
                os.environ["WHATSAPP_ENABLED"] = old_enabled
            if old_token is None:
                os.environ.pop("WHATSAPP_WEBHOOK_TOKEN", None)
            else:
                os.environ["WHATSAPP_WEBHOOK_TOKEN"] = old_token

    async def test_agents_page_uses_registry_provider_options(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            db_path = Path(tmp.name) / "state.db"
            store = SqliteRunStore(db_path=db_path)
            bus = EventBus()
            registry = ProviderRegistry(default_provider_name="codex_cli")
            registry.register("codex_cli", _FakeProvider("codex_cli"), make_active=True)
            registry.register("llama", _FakeProvider("llama"))
            service = AgentService(
                provider=registry,
                provider_registry=registry,
                run_store=store,
                event_bus=bus,
            )
            app = create_app(service)
            client = TestClient(app)
            page = client.get("/agents")
            self.assertEqual(page.status_code, 200)
            self.assertIn('option value="llama"', page.text)
        finally:
            tmp.cleanup()
