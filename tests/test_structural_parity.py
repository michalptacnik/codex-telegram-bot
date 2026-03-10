import unittest
from unittest.mock import patch
from pathlib import Path

from codex_telegram_bot.services.agent_service import (
    AgentService,
    LoopAction,
    ProbeDecision,
    _apply_probe_intent_guardrails,
    _browser_tool_args_error,
    _default_probe_tools_for_prompt,
    _looks_like_prompt_echo,
    _looks_like_prompt_handoff,
)


class TestStructuralParityHeuristics(unittest.TestCase):
    def test_probe_defaults_include_web_tool_for_search_intent(self):
        picks = _default_probe_tools_for_prompt(
            "Search the web for latest AI coding agents and cite sources",
            available_tool_names=["shell_exec", "read_file", "web_search", "mcp_search"],
        )
        self.assertIn("web_search", picks)

    def test_probe_defaults_include_browser_tools_for_browser_intent(self):
        picks = _default_probe_tools_for_prompt(
            "Open a new tab in chrome and navigate to example.com",
            available_tool_names=["shell_exec", "browser_status", "browser_open", "browser_navigate"],
        )
        self.assertIn("browser_open", picks)

    def test_probe_defaults_prefer_non_navigation_tools_for_social_intent(self):
        picks = _default_probe_tools_for_prompt(
            "Find a post on x.com and draft a reply comment in my style",
            available_tool_names=[
                "shell_exec",
                "browser_status",
                "browser_open",
                "browser_navigate",
                "browser_script",
                "browser_extract",
            ],
        )
        self.assertIn("browser_extract", picks)
        self.assertIn("browser_open", picks)
        self.assertLess(picks.index("browser_extract"), picks.index("browser_open"))

    def test_probe_defaults_prioritize_script_for_browser_actuation(self):
        picks = _default_probe_tools_for_prompt(
            "Use this tab and post a comment on that tweet",
            available_tool_names=[
                "shell_exec",
                "browser_status",
                "browser_open",
                "browser_navigate",
                "browser_script",
                "browser_extract",
            ],
        )
        self.assertIn("browser_script", picks)
        self.assertIn("browser_extract", picks)
        self.assertLess(picks.index("browser_script"), picks.index("browser_extract"))

    def test_probe_defaults_prioritize_browser_action_when_available(self):
        picks = _default_probe_tools_for_prompt(
            "Use this tab and post a comment on that tweet",
            available_tool_names=[
                "shell_exec",
                "browser_status",
                "browser_open",
                "browser_navigate",
                "browser_script",
                "browser_action",
                "browser_extract",
            ],
        )
        self.assertIn("browser_action", picks)
        self.assertIn("browser_script", picks)
        self.assertLess(picks.index("browser_action"), picks.index("browser_script"))

    def test_probe_guardrails_upgrade_browser_actuation_toolset(self):
        probe = ProbeDecision(
            mode="NEED_TOOLS",
            reply="",
            tools=["web_search"],
            goal="",
            max_steps=1,
        )
        guarded = _apply_probe_intent_guardrails(
            prompt="Now post a comment on that X post in this tab",
            probe=probe,
            available_tool_names=[
                "web_search",
                "browser_status",
                "browser_script",
                "browser_extract",
                "browser_open",
            ],
        )
        self.assertEqual("NEED_TOOLS", guarded.mode)
        self.assertIn("browser_script", guarded.tools)
        self.assertIn("browser_status", guarded.tools)
        self.assertNotIn("web_search", guarded.tools)
        self.assertGreaterEqual(guarded.max_steps, 2)

    def test_probe_guardrails_handle_implicit_social_context(self):
        probe = ProbeDecision(
            mode="NEED_TOOLS",
            reply="",
            tools=["web_search"],
            goal="",
            max_steps=1,
        )
        guarded = _apply_probe_intent_guardrails(
            prompt="so now post a comment which will be smart on that post",
            probe=probe,
            available_tool_names=[
                "web_search",
                "browser_status",
                "browser_script",
                "browser_extract",
                "browser_open",
            ],
        )
        self.assertIn("browser_script", guarded.tools)
        self.assertIn("browser_extract", guarded.tools)
        self.assertNotIn("web_search", guarded.tools)

    def test_browser_script_contract_requires_non_empty_script_arg(self):
        self.assertIn("requires", _browser_tool_args_error("browser_script", {}))
        self.assertEqual("", _browser_tool_args_error("browser_script", {"script": "return 1;"}))

    def test_browser_open_contract_requires_url_or_query(self):
        self.assertIn("requires", _browser_tool_args_error("browser_open", {}))
        self.assertEqual("", _browser_tool_args_error("browser_open", {"url": "https://example.com"}))
        self.assertEqual("", _browser_tool_args_error("browser_open", {"query": "example"}))

    def test_validate_actions_rejects_empty_browser_script_call(self):
        class _Provider:
            async def generate(self, messages, stream=False, correlation_id="", policy_profile="balanced"):
                return "ok"

            async def execute(self, prompt: str, correlation_id: str = "", policy_profile: str = "balanced"):
                return "ok"

            async def version(self):
                return "v1"

            async def health(self):
                return {"status": "ok"}

            def capabilities(self):
                return {"provider": "fake"}

        svc = AgentService(provider=_Provider())
        actions, error = svc._validate_actions(
            actions=[LoopAction(kind="tool", argv=[], tool_name="browser_script", tool_args={})],
            workspace_root=Path.cwd(),
            available_tool_names=["browser_script"],
        )
        self.assertEqual([], actions)
        self.assertIn("browser_script requires non-empty args.script", error)

    def test_prompt_echo_detection(self):
        prompt = "Search the internet for the 10 best CRM companies and provide sources."
        output = "Search the internet for the 10 best CRM companies and provide sources."
        self.assertTrue(_looks_like_prompt_echo(prompt, output))

    def test_prompt_handoff_detection(self):
        output = "Here is a prompt you can use:\nPrompt: search the web for company comparisons."
        self.assertTrue(_looks_like_prompt_handoff(output))

    def test_email_approval_can_be_disabled_by_env(self):
        class _Provider:
            async def generate(self, messages, stream=False, correlation_id="", policy_profile="balanced"):
                return "ok"

            async def execute(self, prompt: str, correlation_id: str = "", policy_profile: str = "balanced"):
                return "ok"

            async def version(self):
                return "v1"

            async def health(self):
                return {"status": "ok"}

            def capabilities(self):
                return {"provider": "fake"}

        svc = AgentService(provider=_Provider())
        with patch.dict("os.environ", {"EMAIL_SEND_REQUIRE_APPROVAL": "0"}, clear=False):
            self.assertFalse(svc._tool_action_requires_approval("send_email_smtp"))


if __name__ == "__main__":
    unittest.main()
