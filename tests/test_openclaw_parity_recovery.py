from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codex_telegram_bot.persistence.sqlite_store import SqliteRunStore
from codex_telegram_bot.services.agent_service import (
    CONTEXT_BUDGET_TOTAL_CHARS,
    AgentService,
    _autonomous_tool_followup_required,
    _extract_loop_actions,
    _parse_tool_directive,
    _browser_capability_warning_lines,
    _render_tool_schema_lines,
    _trim_lines_from_end,
    _tool_schema_for_prompt,
)
from codex_telegram_bot.services.browser_bridge import BrowserBridge
from codex_telegram_bot.tools.base import NATIVE_TOOL_SCHEMAS


class _Provider:
    async def generate(self, messages, stream=False, correlation_id="", policy_profile="balanced"):
        return "ok"

    async def execute(self, prompt, correlation_id="", policy_profile="balanced"):
        return "ok"

    async def version(self):
        return "v1"

    async def health(self):
        return {"status": "ok"}

    def capabilities(self):
        return {"provider": "fake"}


class TestToolContractParity(unittest.TestCase):
    def test_all_runtime_native_tools_have_agent_prompt_schema(self):
        missing = []
        for name in sorted(NATIVE_TOOL_SCHEMAS.keys()):
            schema = _tool_schema_for_prompt(name)
            if not isinstance(schema, dict):
                missing.append(name)
                continue
            self.assertEqual(str(schema.get("name") or "").strip(), name)
        self.assertEqual(missing, [], msg=f"Missing prompt schema coverage for: {missing}")
        self.assertIsNotNone(_tool_schema_for_prompt("browser_snapshot"))
        self.assertIsNotNone(_tool_schema_for_prompt("browser_screenshot"))

    def test_need_tools_snapshot_schema_never_falls_back_to_exec(self):
        lines = _render_tool_schema_lines(["browser_snapshot"])
        rendered = "\n".join(lines)
        self.assertIn('"name": "browser_snapshot"', rendered)
        self.assertNotIn('"name": "exec"', rendered)
        self.assertIn('"max_elements"', rendered)

    def test_browser_action_schema_preserves_action_or_steps_constraint(self):
        schema = _tool_schema_for_prompt("browser_action")
        self.assertIsInstance(schema, dict)
        description = str(schema.get("description") or "").lower()
        self.assertIn("at least one of", description)
        args = schema.get("args") if isinstance(schema, dict) else {}
        self.assertIn("action", args)
        self.assertIn("steps", args)

    def test_parse_tool_directive_supports_kwargs_alias_and_top_level_args(self):
        action = _parse_tool_directive(
            '{"tool":"browser_navigate","kwargs":{"url":"https://x.com/michal_ptacnik"},"tab_id":1271303867}'
        )
        self.assertIsNotNone(action)
        assert action is not None
        self.assertEqual(action.kind, "tool")
        self.assertEqual(action.tool_name, "browser_navigate")
        self.assertEqual(action.tool_args.get("url"), "https://x.com/michal_ptacnik")
        self.assertEqual(int(action.tool_args.get("tab_id") or 0), 1271303867)

    def test_parse_tool_directive_preserves_top_level_action_arg_when_tool_is_explicit(self):
        action = _parse_tool_directive(
            '{"tool":"browser_action","action":"click","selector":"button[data-testid=\\"tweetButton\\"]"}'
        )
        self.assertIsNotNone(action)
        assert action is not None
        self.assertEqual(action.kind, "tool")
        self.assertEqual(action.tool_name, "browser_action")
        self.assertEqual(action.tool_args.get("action"), "click")
        self.assertIn("selector", action.tool_args)

    def test_extract_loop_actions_supports_tool_keyword_with_multiline_json_body(self):
        raw = (
            "!tool\n"
            "{\n"
            '  "tool": "browser_snapshot",\n'
            '  "client_id": "default"\n'
            "}"
        )
        actions, keep, final_prompt = _extract_loop_actions(raw, preferred_tools=["browser_snapshot"])
        self.assertEqual(keep, "")
        self.assertEqual(final_prompt, "")
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].kind, "tool")
        self.assertEqual(actions[0].tool_name, "browser_snapshot")
        self.assertEqual(actions[0].tool_args.get("client_id"), "default")


class TestBrowserCapabilityAdaptiveParity(unittest.TestCase):
    def test_capability_filter_keeps_snapshot_when_supported(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge = BrowserBridge(heartbeat_ttl_sec=60)
            bridge.register_client(
                instance_id="chrome-1",
                label="Chrome",
                extension_version="2.0.0",
                supported_commands=["open_url", "navigate_url", "run_script", "snapshot", "screenshot"],
            )
            service = AgentService(
                provider=_Provider(),
                browser_bridge=bridge,
                session_workspaces_root=Path(tmp) / "ws",
            )
            snapshot = service.runtime_tool_snapshot(session_id="sess-supports", refresh=True)
            names = snapshot.names()
            self.assertIn("browser_snapshot", names)
            self.assertIn("browser_screenshot", names)

    def test_capability_filter_keeps_snapshot_via_legacy_run_script_emulation(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge = BrowserBridge(heartbeat_ttl_sec=60)
            bridge.register_client(
                instance_id="chrome-legacy",
                label="Chrome",
                extension_version="1.0.0",
                supported_commands=["open_url", "navigate_url", "run_script"],
            )
            service = AgentService(
                provider=_Provider(),
                browser_bridge=bridge,
                session_workspaces_root=Path(tmp) / "ws",
            )
            snapshot = service.runtime_tool_snapshot(session_id="sess-legacy", refresh=True)
            names = snapshot.names()
            self.assertIn("browser_snapshot", names)
            self.assertIn("browser_action", names)
            self.assertIn("browser_extract", names)
            self.assertNotIn("browser_screenshot", names)
            self.assertIn("browser_screenshot", snapshot.disabled)

    def test_capability_filter_handles_legacy_handshake_without_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge = BrowserBridge(heartbeat_ttl_sec=60)
            bridge.register_client(
                instance_id="chrome-legacy",
                label="Chrome",
                version="0.1.1",
                extension_version="",
                supported_commands=[],
            )
            service = AgentService(
                provider=_Provider(),
                browser_bridge=bridge,
                session_workspaces_root=Path(tmp) / "ws",
            )
            snapshot = service.runtime_tool_snapshot(session_id="sess-legacy-no-metadata", refresh=True)
            names = snapshot.names()
            self.assertIn("browser_snapshot", names)
            self.assertIn("browser_action", names)
            self.assertIn("browser_extract", names)
            self.assertNotIn("browser_screenshot", names)

    def test_unsupported_snapshot_error_is_session_suppressed_after_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge = BrowserBridge(heartbeat_ttl_sec=60)
            bridge.register_client(
                instance_id="chrome-2",
                label="Chrome",
                extension_version="2.0.0",
                supported_commands=["open_url", "navigate_url", "run_script", "snapshot", "screenshot"],
            )
            service = AgentService(
                provider=_Provider(),
                browser_bridge=bridge,
                session_workspaces_root=Path(tmp) / "ws",
            )
            session_id = "sess-fallback"
            before = service.runtime_tool_snapshot(session_id=session_id, refresh=True)
            self.assertIn("browser_snapshot", before.names())

            service._record_browser_tool_outcome(  # type: ignore[attr-defined]
                session_id=session_id,
                tool_name="browser_snapshot",
                ok=False,
                output="tool-x status=error Unsupported command type: snapshot",
            )
            service._record_browser_tool_outcome(  # duplicate should not fan out.
                session_id=session_id,
                tool_name="browser_snapshot",
                ok=False,
                output="tool-x status=error Unsupported command type: snapshot",
            )
            after = service.runtime_tool_snapshot(session_id=session_id, refresh=True)
            self.assertNotIn("browser_snapshot", after.names())
            cached = service._session_browser_unsupported_tools.get(session_id, set())  # type: ignore[attr-defined]
            self.assertEqual(cached, {"browser_snapshot"})

    def test_warning_lines_include_current_tab_and_snapshot_fallback_guidance(self):
        lines = _browser_capability_warning_lines(
            prompt="comment in this tab in my style",
            selected_tools=["browser_action", "browser_extract"],
        )
        rendered = "\n".join(lines).lower()
        self.assertIn("browser_snapshot is unavailable", rendered)
        self.assertIn("current-tab intent", rendered)


class TestPromptCompactionParity(unittest.TestCase):
    def test_trim_lines_from_end_preserves_single_oversized_line(self):
        oversized = "x" * (CONTEXT_BUDGET_TOTAL_CHARS + 5000)
        trimmed = _trim_lines_from_end([oversized], CONTEXT_BUDGET_TOTAL_CHARS)
        self.assertTrue(trimmed)
        self.assertLessEqual(len(trimmed[-1]), CONTEXT_BUDGET_TOTAL_CHARS)
        self.assertTrue(trimmed[-1].startswith("x"))

    def test_build_session_prompt_never_returns_empty_after_compaction(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "state.db"
            store = SqliteRunStore(db_path=db_path)
            service = AgentService(
                provider=_Provider(),
                run_store=store,
                session_workspaces_root=Path(tmp) / "ws",
            )
            session = service.get_or_create_session(chat_id=1, user_id=1)
            service.append_session_user_message(session_id=session.session_id, content="seed")
            huge_user_prompt = "y" * (CONTEXT_BUDGET_TOTAL_CHARS + 10000)
            prompt = service.build_session_prompt(session.session_id, huge_user_prompt)
            self.assertTrue(prompt.strip())
            self.assertLessEqual(len(prompt), CONTEXT_BUDGET_TOTAL_CHARS)

    def test_social_publish_requires_followup_after_extract_without_permalink(self):
        prompt = "In this tab, write and publish a post on x.com, then return post URL."
        tool_output = '✅ Done: tool-abc tool=browser_extract status=ok {"ok": true, "url":"https://x.com/compose/post"}'
        self.assertTrue(_autonomous_tool_followup_required(prompt, tool_output))

    def test_social_publish_no_followup_after_permalink_is_present(self):
        prompt = "Post it now on X and return post URL."
        tool_output = (
            '✅ Done: tool-xyz tool=browser_script status=ok {"posted": true, '
            '"post_url":"https://x.com/michal_ptacnik/status/1234567890"}'
        )
        self.assertFalse(_autonomous_tool_followup_required(prompt, tool_output))
