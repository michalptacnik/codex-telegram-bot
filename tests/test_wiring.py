"""Tests for service wiring: AgentService parity service integration (Task C).

Covers:
- session_workspace() delegates to WorkspaceManager when wired
- run_retention_sweep() delegates to SessionRetentionPolicy
- scan_for_secrets() delegates to AccessController
- access control checks in deny_tool_action / approve_tool_action / run_prompt_with_tool_loop
- app_container.build_agent_service() produces a service with parity services wired
- capability_router and access_controller properties
"""
import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from codex_telegram_bot.services.access_control import (
    AccessController,
    UnauthorizedAction,
    UserProfile,
    ROLE_VIEWER,
)
from codex_telegram_bot.services.capability_router import CapabilityRouter
from codex_telegram_bot.services.session_retention import SessionRetentionPolicy
from codex_telegram_bot.services.workspace_manager import WorkspaceManager


# ---------------------------------------------------------------------------
# Minimal fake provider (no network calls)
# ---------------------------------------------------------------------------

class _FakeProvider:
    async def generate(self, messages, stream=False, correlation_id="", policy_profile="balanced"):
        return "ok"

    async def version(self):
        return "fake/0"

    async def health(self):
        return {"status": "ok"}

    def capabilities(self):
        return {"provider": "fake"}


# ---------------------------------------------------------------------------
# AgentService construction helper
# ---------------------------------------------------------------------------

def _make_service(**kwargs):
    from codex_telegram_bot.services.agent_service import AgentService
    return AgentService(provider=_FakeProvider(), **kwargs)


# ---------------------------------------------------------------------------
# session_workspace delegation
# ---------------------------------------------------------------------------

class TestSessionWorkspaceDelegation(unittest.TestCase):
    def test_delegates_to_workspace_manager_when_wired(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            wm = WorkspaceManager(root=Path(tmpdir))
            svc = _make_service(workspace_manager=wm)
            path = svc.session_workspace("test-session-abc")
            self.assertTrue(path.exists())
            self.assertIn("test-session-abc", str(path))

    def test_falls_back_to_default_when_no_manager(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            svc = _make_service(session_workspaces_root=Path(tmpdir))
            path = svc.session_workspace("fallback-session")
            self.assertTrue(path.exists())
            self.assertIn("fallback-session", str(path))

    def test_workspace_manager_path_is_inside_root(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            wm = WorkspaceManager(root=root)
            svc = _make_service(workspace_manager=wm)
            path = svc.session_workspace("mysession")
            self.assertTrue(str(path).startswith(str(root)))


# ---------------------------------------------------------------------------
# run_retention_sweep
# ---------------------------------------------------------------------------

class TestRunRetentionSweep(unittest.TestCase):
    def test_skipped_when_no_policy(self):
        svc = _make_service()
        result = svc.run_retention_sweep()
        self.assertTrue(result.get("skipped"))
        self.assertEqual(result.get("reason"), "no_retention_policy")

    def test_returns_counts_when_policy_set(self):
        mock_store = MagicMock()
        mock_store.archive_idle_sessions.return_value = 3
        mock_store.prune_archived_sessions.return_value = 1
        policy = SessionRetentionPolicy(
            store=mock_store,
            archive_after_idle_days=7,
            delete_after_days=30,
        )
        svc = _make_service(retention_policy=policy)
        result = svc.run_retention_sweep()
        self.assertEqual(result["archived_idle"], 3)
        self.assertEqual(result["pruned_old"], 1)
        self.assertIn("elapsed_ms", result)
        self.assertNotIn("skipped", result)


# ---------------------------------------------------------------------------
# scan_for_secrets
# ---------------------------------------------------------------------------

class TestScanForSecrets(unittest.TestCase):
    def test_returns_empty_when_no_controller(self):
        svc = _make_service()
        found = svc.scan_for_secrets("AKIA1234567890123456")
        self.assertEqual(found, [])

    def test_detects_aws_key_when_controller_wired(self):
        ac = AccessController()
        svc = _make_service(access_controller=ac)
        found = svc.scan_for_secrets("key: AKIAIOSFODNN7EXAMPLE123")
        self.assertIn("aws_access_key", found)

    def test_clean_text_returns_empty(self):
        ac = AccessController()
        svc = _make_service(access_controller=ac)
        found = svc.scan_for_secrets("nothing secret here")
        self.assertEqual(found, [])


# ---------------------------------------------------------------------------
# Access control in deny_tool_action
# ---------------------------------------------------------------------------

class TestDenyToolActionAccessControl(unittest.TestCase):
    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    def test_viewer_cannot_deny(self):
        ac = AccessController()
        ac.set_profile(UserProfile(user_id=99, chat_id=1, roles=[ROLE_VIEWER]))
        svc = _make_service(access_controller=ac)
        result = svc.deny_tool_action("any-id", chat_id=1, user_id=99)
        self.assertTrue(result.startswith("Error:"))
        self.assertIn("not authorized", result)

    def test_user_role_allowed_to_deny(self):
        from codex_telegram_bot.services.access_control import ROLE_USER
        ac = AccessController()
        ac.set_profile(UserProfile(user_id=10, chat_id=2, roles=[ROLE_USER]))
        mock_store = MagicMock()
        mock_store.list_pending_tool_approvals.return_value = []
        mock_store.get_tool_approval.return_value = None  # no approval found
        svc = _make_service(access_controller=ac, run_store=mock_store)
        result = svc.deny_tool_action("nonexistent", chat_id=2, user_id=10)
        # Should fail on "not found", not on "not authorized"
        self.assertNotIn("not authorized", result)
        self.assertIn("Error: approval id not found", result)


# ---------------------------------------------------------------------------
# Access control in approve_tool_action
# ---------------------------------------------------------------------------

class TestApproveToolActionAccessControl(unittest.TestCase):
    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    def test_viewer_cannot_approve(self):
        ac = AccessController()
        ac.set_profile(UserProfile(user_id=99, chat_id=1, roles=[ROLE_VIEWER]))
        svc = _make_service(access_controller=ac)
        result = self._run(svc.approve_tool_action("any-id", chat_id=1, user_id=99))
        self.assertTrue(result.startswith("Error:"))
        self.assertIn("not authorized", result)

    def test_user_role_allowed_to_approve(self):
        from codex_telegram_bot.services.access_control import ROLE_USER
        ac = AccessController()
        ac.set_profile(UserProfile(user_id=10, chat_id=2, roles=[ROLE_USER]))
        mock_store = MagicMock()
        mock_store.get_tool_approval.return_value = None
        svc = _make_service(access_controller=ac, run_store=mock_store)
        result = self._run(svc.approve_tool_action("nonexistent", chat_id=2, user_id=10))
        self.assertNotIn("not authorized", result)
        self.assertIn("Error: approval id not found", result)


# ---------------------------------------------------------------------------
# Access control in run_prompt_with_tool_loop
# ---------------------------------------------------------------------------

class TestRunPromptAccessControl(unittest.TestCase):
    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    def test_viewer_cannot_send_prompt(self):
        ac = AccessController()
        ac.set_profile(UserProfile(user_id=77, chat_id=5, roles=[ROLE_VIEWER]))
        svc = _make_service(access_controller=ac)
        result = self._run(
            svc.run_prompt_with_tool_loop(
                prompt="hello",
                chat_id=5,
                user_id=77,
                session_id="s1",
            )
        )
        self.assertTrue(result.startswith("Error:"))
        self.assertIn("not authorized", result)

    def test_user_role_can_send_prompt(self):
        from codex_telegram_bot.services.access_control import ROLE_USER
        ac = AccessController()
        ac.set_profile(UserProfile(user_id=20, chat_id=3, roles=[ROLE_USER]))
        svc = _make_service(access_controller=ac)
        result = self._run(
            svc.run_prompt_with_tool_loop(
                prompt="hello",
                chat_id=3,
                user_id=20,
                session_id="s1",
            )
        )
        # Should get a response from the fake provider, not an authorization error
        self.assertNotIn("not authorized", result)


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------

class TestAgentServiceProperties(unittest.TestCase):
    def test_capability_router_property_is_none_by_default(self):
        svc = _make_service()
        self.assertIsNone(svc.capability_router)

    def test_access_controller_property_is_none_by_default(self):
        svc = _make_service()
        self.assertIsNone(svc.access_controller)

    def test_capability_router_property_when_wired(self):
        from codex_telegram_bot.providers.registry import ProviderRegistry
        registry = ProviderRegistry()
        registry.register("fake", _FakeProvider(), make_active=True)
        router = CapabilityRouter(registry)
        svc = _make_service(capability_router=router)
        self.assertIs(svc.capability_router, router)

    def test_access_controller_property_when_wired(self):
        ac = AccessController()
        svc = _make_service(access_controller=ac)
        self.assertIs(svc.access_controller, ac)


# ---------------------------------------------------------------------------
# app_container.build_agent_service wiring
# ---------------------------------------------------------------------------

class TestAppContainerWiring(unittest.TestCase):
    def test_build_without_db_has_parity_services(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict("os.environ", {
                "EXECUTION_WORKSPACE_ROOT": tmpdir,
                "SESSION_WORKSPACES_ROOT": tmpdir,
            }):
                from codex_telegram_bot.app_container import build_agent_service
                svc = build_agent_service(state_db_path=None)
        self.assertIsNotNone(svc.access_controller)
        self.assertIsNotNone(svc.capability_router)
        self.assertIsNone(svc._retention_policy)  # no DB → no retention

    def test_build_with_db_has_all_parity_services(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "state.db"
            with patch.dict("os.environ", {
                "EXECUTION_WORKSPACE_ROOT": tmpdir,
                "SESSION_WORKSPACES_ROOT": tmpdir,
            }):
                from codex_telegram_bot.app_container import build_agent_service
                svc = build_agent_service(state_db_path=db_path)
        self.assertIsNotNone(svc.access_controller)
        self.assertIsNotNone(svc.capability_router)
        self.assertIsNotNone(svc._retention_policy)

    def test_workspace_manager_integrated(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ws_root = Path(tmpdir) / "workspaces"
            ws_root.mkdir()
            with patch.dict("os.environ", {
                "EXECUTION_WORKSPACE_ROOT": tmpdir,
                "SESSION_WORKSPACES_ROOT": str(ws_root),
            }):
                from codex_telegram_bot.app_container import build_agent_service
                svc = build_agent_service(state_db_path=None)
        # session_workspace() should delegate to WorkspaceManager
        path = svc.session_workspace("wiring-test-session")
        self.assertTrue(path.exists())


if __name__ == "__main__":
    unittest.main()
