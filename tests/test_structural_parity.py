import unittest

from codex_telegram_bot.services.agent_service import (
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

    def test_prompt_echo_detection(self):
        prompt = "Search the internet for the 10 best CRM companies and provide sources."
        output = "Search the internet for the 10 best CRM companies and provide sources."
        self.assertTrue(_looks_like_prompt_echo(prompt, output))

    def test_prompt_handoff_detection(self):
        output = "Here is a prompt you can use:\nPrompt: search the web for company comparisons."
        self.assertTrue(_looks_like_prompt_handoff(output))


if __name__ == "__main__":
    unittest.main()
