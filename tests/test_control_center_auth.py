"""Tests for Control Center optional auth (_opt_api_scope) and readiness endpoint."""
import os
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
    from codex_telegram_bot.control_center.app import create_app
except Exception:
    TestClient = None
    create_app = None


class _FakeRunner:
    def __init__(self, result):
        self._result = result

    async def run(self, argv, stdin_text="", timeout_sec=60, policy_profile="balanced", workspace_root=""):
        return self._result


def _make_service():
    tmp = tempfile.TemporaryDirectory()
    db_path = Path(tmp.name) / "state.db"
    store = SqliteRunStore(db_path=db_path)
    bus = EventBus()
    runner = _FakeRunner(CommandResult(0, "ok", ""))
    provider = CodexCliProvider(runner=runner)
    service = AgentService(provider=provider, run_store=store, event_bus=bus)
    return service, tmp


@unittest.skipIf(TestClient is None or create_app is None, "fastapi deps unavailable")
class TestOptApiScope(unittest.TestCase):
    """When LOCAL_API_KEYS is not set, all /api/* endpoints remain open."""

    def setUp(self):
        self.service, self.tmp = _make_service()
        os.environ.pop("LOCAL_API_KEYS", None)

    def tearDown(self):
        self.tmp.cleanup()
        os.environ.pop("LOCAL_API_KEYS", None)

    def test_open_access_without_local_api_keys(self):
        app = create_app(self.service)
        client = TestClient(app)
        self.assertEqual(client.get("/api/metrics").status_code, 200)
        self.assertEqual(client.get("/api/runs").status_code, 200)
        self.assertEqual(client.get("/api/sessions").status_code, 200)
        self.assertEqual(client.get("/health").status_code, 200)

    def test_health_always_accessible(self):
        os.environ["LOCAL_API_KEYS"] = "tok:admin:*"
        try:
            app = create_app(self.service)
            client = TestClient(app)
            self.assertEqual(client.get("/health").status_code, 200)
        finally:
            del os.environ["LOCAL_API_KEYS"]


@unittest.skipIf(TestClient is None or create_app is None, "fastapi deps unavailable")
class TestOptApiScopeEnforced(unittest.TestCase):
    """When LOCAL_API_KEYS is set, /api/* endpoints require a valid token."""

    def setUp(self):
        self.service, self.tmp = _make_service()
        self.old = os.environ.get("LOCAL_API_KEYS")
        os.environ["LOCAL_API_KEYS"] = "readtoken:api:read;writetoken:api:read,api:write;admintoken:admin:*"

    def tearDown(self):
        self.tmp.cleanup()
        if self.old is None:
            os.environ.pop("LOCAL_API_KEYS", None)
        else:
            os.environ["LOCAL_API_KEYS"] = self.old

    def test_get_without_token_returns_401(self):
        app = create_app(self.service)
        client = TestClient(app)
        self.assertEqual(client.get("/api/runs").status_code, 401)
        self.assertEqual(client.get("/api/metrics").status_code, 401)
        self.assertEqual(client.get("/api/sessions").status_code, 401)

    def test_get_with_read_token_returns_200(self):
        app = create_app(self.service)
        client = TestClient(app)
        self.assertEqual(client.get("/api/runs", headers={"x-local-api-key": "readtoken"}).status_code, 200)
        self.assertEqual(client.get("/api/metrics", headers={"x-local-api-key": "readtoken"}).status_code, 200)

    def test_admin_only_post_with_read_token_returns_403(self):
        app = create_app(self.service)
        client = TestClient(app)
        r = client.post(
            "/api/plugins/install",
            headers={"x-local-api-key": "readtoken"},
            json={"manifest_path": "/nonexistent/plugin.json"},
        )
        self.assertEqual(r.status_code, 403)

    def test_admin_only_post_with_admin_token_returns_non_401(self):
        """Admin token should pass auth (may get 400 for bad manifest, but not 401/403)."""
        app = create_app(self.service)
        client = TestClient(app)
        r = client.post(
            "/api/plugins/install",
            headers={"x-local-api-key": "admintoken"},
            json={"manifest_path": "/nonexistent/plugin.json"},
        )
        self.assertNotIn(r.status_code, {401, 403})

    def test_invalid_token_returns_401(self):
        app = create_app(self.service)
        client = TestClient(app)
        self.assertEqual(client.get("/api/runs", headers={"x-local-api-key": "wrongtoken"}).status_code, 401)


@unittest.skipIf(TestClient is None or create_app is None, "fastapi deps unavailable")
class TestUiAuth(unittest.TestCase):
    """CONTROL_CENTER_UI_SECRET gates all HTML pages."""

    def setUp(self):
        self.service, self.tmp = _make_service()
        self.old = os.environ.get("CONTROL_CENTER_UI_SECRET")

    def tearDown(self):
        self.tmp.cleanup()
        if self.old is None:
            os.environ.pop("CONTROL_CENTER_UI_SECRET", None)
        else:
            os.environ["CONTROL_CENTER_UI_SECRET"] = self.old

    def test_no_secret_pages_open(self):
        os.environ.pop("CONTROL_CENTER_UI_SECRET", None)
        app = create_app(self.service)
        client = TestClient(app, follow_redirects=False)
        self.assertEqual(client.get("/").status_code, 200)
        self.assertEqual(client.get("/runs").status_code, 200)
        self.assertEqual(client.get("/sessions").status_code, 200)
        self.assertEqual(client.get("/settings").status_code, 200)

    def test_with_secret_unauthenticated_redirects_to_login(self):
        os.environ["CONTROL_CENTER_UI_SECRET"] = "s3cr3t"
        app = create_app(self.service)
        client = TestClient(app, follow_redirects=False)
        for path in ["/", "/runs", "/sessions", "/agents", "/settings", "/plugins", "/approvals"]:
            r = client.get(path)
            self.assertEqual(r.status_code, 303, f"{path} should redirect")
            self.assertIn("/login", r.headers["location"], f"{path} should redirect to /login")

    def test_login_page_renders(self):
        os.environ["CONTROL_CENTER_UI_SECRET"] = "s3cr3t"
        app = create_app(self.service)
        client = TestClient(app, follow_redirects=False)
        r = client.get("/login")
        self.assertEqual(r.status_code, 200)
        self.assertIn("secret", r.text.lower())

    def test_correct_secret_sets_cookie_and_redirects(self):
        os.environ["CONTROL_CENTER_UI_SECRET"] = "s3cr3t"
        app = create_app(self.service)
        client = TestClient(app, follow_redirects=False)
        r = client.post("/login", data={"secret": "s3cr3t", "next": "/"})
        self.assertEqual(r.status_code, 303)
        self.assertIn("cc_ui_token", r.cookies)

    def test_wrong_secret_returns_401(self):
        os.environ["CONTROL_CENTER_UI_SECRET"] = "s3cr3t"
        app = create_app(self.service)
        client = TestClient(app, follow_redirects=False)
        r = client.post("/login", data={"secret": "wrong", "next": "/"})
        self.assertEqual(r.status_code, 401)

    def test_valid_cookie_allows_access(self):
        os.environ["CONTROL_CENTER_UI_SECRET"] = "s3cr3t"
        app = create_app(self.service)
        client = TestClient(app, follow_redirects=False)
        # login to get cookie
        client.post("/login", data={"secret": "s3cr3t", "next": "/"})
        # now access dashboard with cookie
        r = client.get("/")
        self.assertEqual(r.status_code, 200)

    def test_logout_clears_cookie_and_redirects(self):
        os.environ["CONTROL_CENTER_UI_SECRET"] = "s3cr3t"
        app = create_app(self.service)
        client = TestClient(app, follow_redirects=False)
        client.post("/login", data={"secret": "s3cr3t", "next": "/"})
        r = client.get("/logout")
        self.assertEqual(r.status_code, 303)
        self.assertIn("/login", r.headers["location"])

    def test_health_always_accessible_regardless_of_ui_secret(self):
        os.environ["CONTROL_CENTER_UI_SECRET"] = "s3cr3t"
        app = create_app(self.service)
        client = TestClient(app, follow_redirects=False)
        self.assertEqual(client.get("/health").status_code, 200)


@unittest.skipIf(TestClient is None or create_app is None, "fastapi deps unavailable")
class TestOnboardingReadiness(unittest.TestCase):
    """Verify the /api/onboarding/readiness endpoint structure."""

    def setUp(self):
        self.service, self.tmp = _make_service()
        os.environ.pop("LOCAL_API_KEYS", None)

    def tearDown(self):
        self.tmp.cleanup()

    def test_readiness_returns_200_with_expected_structure(self):
        app = create_app(self.service)
        client = TestClient(app)
        r = client.get("/api/onboarding/readiness")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertIn("ready", data)
        self.assertIn("checks", data)
        checks = data["checks"]
        self.assertIn("codex_cli", checks)
        self.assertIn("telegram_token", checks)
        self.assertIn("pass", data["checks"]["codex_cli"])

    def test_codex_cli_check_reflects_availability(self):
        import shutil
        app = create_app(self.service)
        client = TestClient(app)
        r = client.get("/api/onboarding/readiness")
        self.assertEqual(r.status_code, 200)
        codex_available = shutil.which("codex") is not None
        self.assertEqual(r.json()["checks"]["codex_cli"]["pass"], codex_available)

    def test_telegram_token_check_respects_env(self):
        old = os.environ.get("TELEGRAM_BOT_TOKEN")
        os.environ["TELEGRAM_BOT_TOKEN"] = "1234567890:AABBCCDDEEFFaabbccddeeff12345678901"
        try:
            app = create_app(self.service)
            client = TestClient(app)
            r = client.get("/api/onboarding/readiness")
            self.assertEqual(r.status_code, 200)
            self.assertTrue(r.json()["checks"]["telegram_token"]["pass"])
        finally:
            if old is None:
                os.environ.pop("TELEGRAM_BOT_TOKEN", None)
            else:
                os.environ["TELEGRAM_BOT_TOKEN"] = old
