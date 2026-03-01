import unittest

from codex_telegram_bot.agent.prompt_builder import build_session_prompt
from codex_telegram_bot.agent.session_handler import run_turn
from codex_telegram_bot.agent.tool_loop import run_native_tool_loop, run_prompt_with_tool_loop


class _StubService:
    def __init__(self) -> None:
        self.calls = []

    async def _run_native_tool_loop_impl(self, **kwargs):
        self.calls.append(("native", kwargs))
        return "native-ok"

    async def _run_prompt_with_tool_loop_impl(self, **kwargs):
        self.calls.append(("prompt", kwargs))
        return "prompt-ok"

    async def _run_turn_impl(self, **kwargs):
        self.calls.append(("turn", kwargs))
        return "turn-ok"

    def _build_session_prompt_impl(self, **kwargs):
        self.calls.append(("prompt_builder", kwargs))
        return "session-prompt-ok"


class TestAgentModuleShims(unittest.IsolatedAsyncioTestCase):
    async def test_tool_loop_shims_delegate_to_service_impls(self):
        service = _StubService()
        out_native = await run_native_tool_loop(
            service=service,
            user_message="hello",
            chat_id=1,
            user_id=2,
            session_id="s1",
        )
        out_prompt = await run_prompt_with_tool_loop(
            service=service,
            prompt="hello",
            chat_id=1,
            user_id=2,
            session_id="s1",
        )
        self.assertEqual(out_native, "native-ok")
        self.assertEqual(out_prompt, "prompt-ok")
        self.assertEqual(service.calls[0][0], "native")
        self.assertEqual(service.calls[1][0], "prompt")

    async def test_session_handler_shim_delegates(self):
        service = _StubService()
        out = await run_turn(
            service=service,
            prompt="hello",
            chat_id=1,
            user_id=2,
            session_id="s1",
        )
        self.assertEqual(out, "turn-ok")
        self.assertEqual(service.calls[0][0], "turn")

    def test_prompt_builder_shim_delegates(self):
        service = _StubService()
        out = build_session_prompt(service=service, session_id="s1", user_prompt="hello")
        self.assertEqual(out, "session-prompt-ok")
        self.assertEqual(service.calls[0][0], "prompt_builder")


if __name__ == "__main__":
    unittest.main()

