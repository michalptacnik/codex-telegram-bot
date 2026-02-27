from __future__ import annotations

import asyncio
import sqlite3
import tempfile
import unittest
from pathlib import Path

from codex_telegram_bot.agent_core.agent import Agent
from codex_telegram_bot.persistence.sqlite_store import SqliteRunStore
from codex_telegram_bot.runtime_contract import RuntimeError, ToolCall, decode_provider_response, merge_stream_chunks
from codex_telegram_bot.services.agent_scheduler import AgentScheduler
from codex_telegram_bot.services.agent_service import AgentService
from codex_telegram_bot.services.message_updater import MessageUpdater
from codex_telegram_bot.tools import build_default_tool_registry
from codex_telegram_bot.tools.base import NATIVE_TOOL_SCHEMAS, ToolContext, ToolRequest
from codex_telegram_bot.tools.git import GitStatusTool
from codex_telegram_bot.tools.runtime_registry import build_runtime_tool_registry


class _FakeProvider:
    async def generate(self, messages, stream=False, correlation_id="", policy_profile="balanced"):
        return "ok"

    async def execute(self, prompt, correlation_id="", policy_profile="balanced"):
        return "ok"

    async def version(self):
        return "fake/1"

    async def health(self):
        return {"status": "healthy"}


class _RawRouter:
    def __init__(self, output: str):
        self._output = output

    async def route_prompt(self, **kwargs):
        return self._output


class _FakeBot:
    def __init__(self):
        self.edit_calls = 0
        self.sent_calls = 0
        self.last_text = ""

    async def edit_message_text(self, *, chat_id: int, message_id: int, text: str):
        self.edit_calls += 1
        self.last_text = text

    async def send_message(self, *, chat_id: int, text: str):
        self.sent_calls += 1
        self.last_text = text


class TestRuntimeContractRegressions(unittest.IsolatedAsyncioTestCase):
    async def test_no_raw_tool_dialect_reaches_agent_response(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "state.db"
            store = SqliteRunStore(db_path=db_path)
            service = AgentService(
                provider=_FakeProvider(),
                run_store=store,
                session_workspaces_root=Path(tmp) / "ws",
            )
            router = _RawRouter('!tool {"name":"read_file","args":{"path":"x"}}')
            agent = Agent(agent_service=service, router=router)
            response = await agent.handle_message(chat_id=1, user_id=2, text="go")
            self.assertNotIn("!tool", response.output)
            self.assertIn("could not safely decode", response.output.lower())

    def test_tool_registry_consistency_model_vs_executor(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = build_default_tool_registry()
            snapshot = build_runtime_tool_registry(base, workspace_root=Path(tmp))
            model_tools = {schema["name"] for schema in snapshot.schemas}
            executor_tools = {name for name in snapshot.names() if name in NATIVE_TOOL_SCHEMAS}
            self.assertEqual(model_tools, executor_tools)

    def test_streaming_tool_call_spanning_chunks_is_assembled(self):
        chunks = [
            {"type": "tool_use_delta", "id": "call_1", "name": "read_file", "input_delta": '{"path":"REA'},
            {"type": "tool_use_delta", "id": "call_1", "input_delta": 'DME.md"}'},
        ]
        events = merge_stream_chunks(chunks, allowed_tools={"read_file"})
        self.assertEqual(len(events), 1)
        self.assertIsInstance(events[0], ToolCall)
        self.assertEqual(events[0].name, "read_file")
        self.assertEqual(events[0].args, {"path": "README.md"})

        decoded = decode_provider_response({"stream_chunks": chunks}, allowed_tools={"read_file"})
        self.assertFalse(any(isinstance(e, RuntimeError) for e in decoded))
        self.assertTrue(any(isinstance(e, ToolCall) for e in decoded))

    def test_workspace_invariant_disables_git_tools_outside_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = build_default_tool_registry()
            snapshot = build_runtime_tool_registry(base, workspace_root=root)
            self.assertFalse(snapshot.invariants.is_git_repo)
            self.assertNotIn("git_status", snapshot.names())
            self.assertIn("git_status", snapshot.disabled)

            result = GitStatusTool().run(ToolRequest(name="git_status", args={}), ToolContext(workspace_root=root))
            self.assertFalse(result.ok)
            self.assertIn("unavailable", result.output.lower())

    def test_migration_upgrades_old_schema_and_sets_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "legacy.db"
            conn = sqlite3.connect(str(db_path))
            conn.execute(
                """
                CREATE TABLE agents (
                    agent_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    policy_profile TEXT NOT NULL,
                    enabled INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE tool_approvals (
                    approval_id TEXT PRIMARY KEY,
                    chat_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    session_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    argv_json TEXT NOT NULL,
                    stdin_text TEXT NOT NULL,
                    timeout_sec INTEGER NOT NULL,
                    risk_tier TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.commit()
            conn.close()

            store = SqliteRunStore(db_path=db_path)
            with store._connect() as upgraded:
                cols = {row["name"] for row in upgraded.execute("PRAGMA table_info(agents)").fetchall()}
                self.assertIn("max_concurrency", cols)
                tool_cols = {row["name"] for row in upgraded.execute("PRAGMA table_info(tool_approvals)").fetchall()}
                self.assertIn("run_id", tool_cols)
                version_row = upgraded.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
                self.assertGreaterEqual(int(version_row["v"] or 0), 2)

    async def test_telegram_edit_idempotency_same_text_only_once(self):
        updater = MessageUpdater(debounce_sec=0.0)
        bot = _FakeBot()
        for _ in range(5):
            await updater.update(bot=bot, chat_id=1, message_id=1, text="same text")
        await updater.flush(chat_id=1, message_id=1)
        self.assertEqual(bot.edit_calls, 1)

    async def test_scheduler_shutdown_awaits_pending_jobs(self):
        started = asyncio.Event()

        async def executor(agent_id: str, prompt: str, correlation_id: str) -> str:
            started.set()
            await asyncio.sleep(5)
            return "ok"

        scheduler = AgentScheduler(executor=executor, get_agent_concurrency=lambda _aid: 1)
        await scheduler.enqueue("default", "work")
        await started.wait()
        await scheduler.shutdown()
        self.assertEqual(len(scheduler._job_tasks), 0)  # type: ignore[attr-defined]
        dangling = [t for t in asyncio.all_tasks() if t.get_name().startswith("agent-job-") and not t.done()]
        self.assertEqual(dangling, [])
