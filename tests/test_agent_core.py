import unittest
from unittest.mock import AsyncMock

from codex_telegram_bot.agent_core.agent import Agent
from codex_telegram_bot.agent_core.memory import resolve_memory_config
from codex_telegram_bot.services.continuation_guard import PRELIMINARY_TERMINAL_FALLBACK


class _Session:
    def __init__(self, session_id: str):
        self.session_id = session_id


class _StubService:
    def __init__(self):
        self.appended_user = []
        self.appended_assistant = []
        self.get_or_create_session_calls = []
        self.reset_calls = []
        self.run_prompt_with_tool_loop = AsyncMock(return_value="ok")

    def get_or_create_session(self, chat_id: int, user_id: int):
        self.get_or_create_session_calls.append((chat_id, user_id))
        return _Session("sess-123")

    def append_session_user_message(self, session_id: str, content: str):
        self.appended_user.append((session_id, content))

    def append_session_assistant_message(self, session_id: str, content: str):
        self.appended_assistant.append((session_id, content))

    def enforce_transport_text_contract(self, session_id: str, raw_output: str):
        return str(raw_output or "")

    def reset_session(self, chat_id: int, user_id: int):
        self.reset_calls.append((chat_id, user_id))
        return _Session("sess-reset")


class TestAgentCore(unittest.IsolatedAsyncioTestCase):
    async def test_agent_entrypoint_routes_through_service(self):
        service = _StubService()
        agent = Agent(agent_service=service)

        result = await agent.handle_message(chat_id=1, user_id=2, text="hello")

        self.assertEqual(result.session_id, "sess-123")
        self.assertEqual(result.output, "ok")
        self.assertEqual(service.get_or_create_session_calls, [(1, 2)])
        self.assertEqual(service.appended_user, [("sess-123", "hello")])
        self.assertEqual(service.appended_assistant, [("sess-123", "ok")])
        service.run_prompt_with_tool_loop.assert_awaited_once()

    async def test_agent_auto_continues_preliminary_router_output(self):
        service = _StubService()
        router = type("MockRouter", (), {})()
        router.route_prompt = AsyncMock(
            side_effect=[
                "I'm still working on this; let me check one more thing.",
                "Final: completed with concrete result.",
            ]
        )
        agent = Agent(agent_service=service, router=router)

        result = await agent.handle_message(chat_id=1, user_id=2, text="do work")

        self.assertEqual(result.output, "Final: completed with concrete result.")
        self.assertEqual(router.route_prompt.await_count, 2)
        self.assertEqual(service.appended_assistant, [("sess-123", "Final: completed with concrete result.")])

    async def test_agent_sanitizes_terminal_preliminary_after_retry_budget(self):
        service = _StubService()
        router = type("MockRouter", (), {})()
        router.route_prompt = AsyncMock(
            side_effect=[
                "I'll continue executing this task. Let me check one more thing.",
                "I'll continue executing this task. Let me check one more thing.",
                "I'll continue executing this task. Let me check one more thing.",
            ]
        )
        agent = Agent(agent_service=service, router=router)

        result = await agent.handle_message(chat_id=1, user_id=2, text="do work")

        self.assertEqual(router.route_prompt.await_count, 3)
        self.assertEqual(result.output, PRELIMINARY_TERMINAL_FALLBACK)
        self.assertEqual(service.appended_assistant, [("sess-123", PRELIMINARY_TERMINAL_FALLBACK)])

    async def test_agent_reset_delegates_to_service(self):
        service = _StubService()
        agent = Agent(agent_service=service)

        session = agent.reset_session(chat_id=9, user_id=8)

        self.assertEqual(session.session_id, "sess-reset")
        self.assertEqual(service.reset_calls, [(9, 8)])


class TestMemoryConfig(unittest.TestCase):
    def test_memory_defaults_to_20_turns(self):
        def _read(_name: str, default: int) -> int:
            return default

        cfg = resolve_memory_config(_read)

        self.assertEqual(cfg.max_turns, 20)
        self.assertEqual(cfg.max_messages, 40)
        self.assertEqual(cfg.keep_recent_messages, 20)
