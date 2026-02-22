import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from codex_telegram_bot.domain.contracts import CommandResult
from codex_telegram_bot.providers.fallback import EchoFallbackProvider
from codex_telegram_bot.events.event_bus import EventBus
from codex_telegram_bot.observability.alerts import AlertDispatcher
from codex_telegram_bot.persistence.sqlite_store import SqliteRunStore
from codex_telegram_bot.providers.codex_cli import CodexCliProvider
from codex_telegram_bot.providers.router import ProviderRouter, ProviderRouterConfig
from codex_telegram_bot.services.repo_context import RepositoryContextRetriever
from codex_telegram_bot.services.agent_service import AgentService


class FakeRunner:
    def __init__(self, result: CommandResult):
        self._results = [result]
        self.last_argv = None
        self.last_stdin = None
        self.last_policy_profile = None
        self.last_workspace_root = None
        self.calls = []

    def set_results(self, results):
        self._results = list(results)

    async def run(self, argv, stdin_text="", timeout_sec=60, policy_profile="balanced", workspace_root=""):
        self.last_argv = list(argv)
        self.last_stdin = stdin_text
        self.last_policy_profile = policy_profile
        self.last_workspace_root = workspace_root
        self.calls.append(
            {
                "argv": list(argv),
                "stdin_text": stdin_text,
                "timeout_sec": timeout_sec,
                "policy_profile": policy_profile,
                "workspace_root": workspace_root,
            }
        )
        if len(self._results) > 1:
            return self._results.pop(0)
        return self._results[0]


class TestCodexCliProvider(unittest.IsolatedAsyncioTestCase):
    async def test_execute_success(self):
        runner = FakeRunner(CommandResult(returncode=0, stdout="hello", stderr=""))
        provider = CodexCliProvider(runner=runner)

        output = await provider.execute("prompt")

        self.assertEqual(output, "hello")
        self.assertEqual(runner.last_argv[0:3], ["codex", "exec", "-"])
        self.assertEqual(runner.last_policy_profile, "balanced")

    async def test_execute_uses_policy_profile(self):
        runner = FakeRunner(CommandResult(returncode=0, stdout="hello", stderr=""))
        provider = CodexCliProvider(runner=runner)

        output = await provider.execute("prompt", policy_profile="strict")

        self.assertEqual(output, "hello")
        self.assertEqual(runner.last_policy_profile, "strict")

    async def test_execute_nonzero_return_code(self):
        runner = FakeRunner(CommandResult(returncode=2, stdout="", stderr="bad args"))
        provider = CodexCliProvider(runner=runner)

        output = await provider.execute("prompt")

        self.assertIn("Error: codex exited with code 2.", output)
        self.assertIn("bad args", output)

    async def test_version(self):
        runner = FakeRunner(CommandResult(returncode=0, stdout="codex 1.2.3", stderr=""))
        provider = CodexCliProvider(runner=runner)

        version = await provider.version()

        self.assertEqual(version, "codex 1.2.3")
        self.assertEqual(runner.last_argv, ["codex", "--version"])


class TestAgentService(unittest.IsolatedAsyncioTestCase):
    async def test_service_delegates_to_provider(self):
        runner = FakeRunner(CommandResult(returncode=0, stdout="ok", stderr=""))
        provider = CodexCliProvider(runner=runner)
        service = AgentService(provider=provider)

        result = await service.run_prompt("ping")

        self.assertEqual(result, "ok")

    async def test_service_persists_run_lifecycle(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "state.db"
            store = SqliteRunStore(db_path=db_path)
            bus = EventBus()
            runner = FakeRunner(CommandResult(returncode=0, stdout="done", stderr=""))
            provider = CodexCliProvider(runner=runner)
            service = AgentService(provider=provider, run_store=store, event_bus=bus)

            output = await service.run_prompt("hello")

            self.assertEqual(output, "done")
            recent = service.list_recent_runs(limit=5)
            self.assertEqual(len(recent), 1)
            self.assertEqual(recent[0].status, "completed")
            self.assertEqual(recent[0].output, "done")
            events = service.list_run_events(recent[0].run_id, limit=10)
            event_types = [e.event_type for e in events]
            self.assertEqual(events[0].event_type, "run.started")
            self.assertIn("run.provider.selected", event_types)
            self.assertIn("run.policy.applied", event_types)
            self.assertIn("run.provider.used", event_types)
            self.assertIn("run.completed", event_types)

    async def test_service_marks_failed_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "state.db"
            store = SqliteRunStore(db_path=db_path)
            bus = EventBus()
            runner = FakeRunner(CommandResult(returncode=2, stdout="", stderr="boom"))
            provider = CodexCliProvider(runner=runner)
            service = AgentService(provider=provider, run_store=store, event_bus=bus)

            output = await service.run_prompt("hello")

            self.assertIn("Error: codex exited with code 2.", output)
            recent = service.list_recent_runs(limit=5)
            self.assertEqual(len(recent), 1)
            self.assertEqual(recent[0].status, "failed")
            self.assertIn("Error:", recent[0].error)

    async def test_service_redacts_secrets_and_emits_audit_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "state.db"
            store = SqliteRunStore(db_path=db_path)
            bus = EventBus()
            runner = FakeRunner(
                CommandResult(returncode=0, stdout="token=supersecret sk-abcdef1234567890", stderr="")
            )
            provider = CodexCliProvider(runner=runner)
            service = AgentService(provider=provider, run_store=store, event_bus=bus)

            output = await service.run_prompt("hello")
            self.assertNotIn("supersecret", output)

            run = service.list_recent_runs(limit=1)[0]
            self.assertNotIn("supersecret", run.output)
            self.assertIn("token=REDACTED", run.output)
            events = service.list_run_events(run.run_id, limit=50)
            event_types = [e.event_type for e in events]
            self.assertIn("security.redaction.applied", event_types)

    async def test_service_applies_agent_policy_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "state.db"
            store = SqliteRunStore(db_path=db_path)
            bus = EventBus()
            runner = FakeRunner(CommandResult(returncode=0, stdout="ok", stderr=""))
            provider = CodexCliProvider(runner=runner)
            service = AgentService(provider=provider, run_store=store, event_bus=bus)
            service.upsert_agent(
                agent_id="secure",
                name="Secure Agent",
                provider="codex_cli",
                policy_profile="strict",
                max_concurrency=1,
                enabled=True,
            )

            await service.run_prompt("hello", agent_id="secure")

            self.assertEqual(runner.last_policy_profile, "strict")

    async def test_agent_registry_upsert_and_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "state.db"
            store = SqliteRunStore(db_path=db_path)
            bus = EventBus()
            runner = FakeRunner(CommandResult(returncode=0, stdout="ok", stderr=""))
            provider = CodexCliProvider(runner=runner)
            service = AgentService(provider=provider, run_store=store, event_bus=bus)

            agents_before = service.list_agents()
            self.assertTrue(any(a.agent_id == "default" for a in agents_before))

            saved = service.upsert_agent(
                agent_id="planner",
                name="Planner Agent",
                provider="codex_cli",
                policy_profile="balanced",
                max_concurrency=1,
                enabled=True,
            )
            self.assertEqual(saved.agent_id, "planner")
            fetched = service.get_agent("planner")
            self.assertIsNotNone(fetched)
            self.assertEqual(fetched.name, "Planner Agent")

            deleted = service.delete_agent("planner")
            self.assertTrue(deleted)

            with self.assertRaises(ValueError):
                service.upsert_agent(
                    agent_id="BAD ID",
                    name="Invalid",
                    provider="codex_cli",
                    policy_profile="balanced",
                    max_concurrency=1,
                    enabled=True,
                )

    async def test_handoff_protocol_events_and_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "state.db"
            store = SqliteRunStore(db_path=db_path)
            bus = EventBus()
            runner = FakeRunner(CommandResult(returncode=0, stdout="ok", stderr=""))
            provider = CodexCliProvider(runner=runner)
            service = AgentService(provider=provider, run_store=store, event_bus=bus)

            service.upsert_agent(
                agent_id="researcher",
                name="Researcher",
                provider="codex_cli",
                policy_profile="balanced",
                max_concurrency=1,
                enabled=True,
            )
            await service.run_prompt("seed")
            parent_run = service.list_recent_runs(limit=1)[0]

            res_ok = await service.handoff_prompt(
                from_agent_id="default",
                to_agent_id="researcher",
                prompt="investigate",
                parent_run_id=parent_run.run_id,
            )
            self.assertEqual(res_ok["status"], "completed")
            events_ok = service.list_run_events(parent_run.run_id, limit=50)
            event_types_ok = [e.event_type for e in events_ok]
            self.assertIn("handoff.requested", event_types_ok)
            self.assertIn("handoff.accepted", event_types_ok)
            self.assertIn("handoff.completed", event_types_ok)

            service.upsert_agent(
                agent_id="researcher",
                name="Researcher",
                provider="codex_cli",
                policy_profile="balanced",
                max_concurrency=1,
                enabled=False,
            )
            res_recover = await service.handoff_prompt(
                from_agent_id="default",
                to_agent_id="researcher",
                prompt="recover",
                parent_run_id=parent_run.run_id,
            )
            self.assertEqual(res_recover["target_agent_id"], "default")
            self.assertEqual(res_recover["status"], "completed")
            events_recover = service.list_run_events(parent_run.run_id, limit=100)
            event_types_recover = [e.event_type for e in events_recover]
            self.assertIn("handoff.recovered", event_types_recover)

    async def test_session_create_reset_and_history_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "state.db"
            store = SqliteRunStore(db_path=db_path)
            bus = EventBus()
            runner = FakeRunner(CommandResult(returncode=0, stdout="ok", stderr=""))
            provider = CodexCliProvider(runner=runner)
            service = AgentService(provider=provider, run_store=store, event_bus=bus)

            session = service.get_or_create_session(chat_id=1001, user_id=2002)
            self.assertEqual(session.status, "active")
            service.append_session_user_message(session.session_id, "hello")
            service.append_session_assistant_message(session.session_id, "world")
            built = service.build_session_prompt(session.session_id, "next")
            self.assertIn("user: hello", built)
            self.assertIn("assistant: world", built)
            self.assertTrue(built.endswith("user: next"))

            reset = service.reset_session(chat_id=1001, user_id=2002)
            self.assertNotEqual(reset.session_id, session.session_id)
            sessions = service.list_recent_sessions(limit=10)
            self.assertTrue(any(s.session_id == reset.session_id for s in sessions))

    async def test_session_branch_activate_and_compaction(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "state.db"
            store = SqliteRunStore(db_path=db_path)
            bus = EventBus()
            runner = FakeRunner(CommandResult(returncode=0, stdout="ok", stderr=""))
            provider = CodexCliProvider(runner=runner)
            service = AgentService(
                provider=provider,
                run_store=store,
                event_bus=bus,
                session_max_messages=10,
                session_compact_keep=5,
            )
            base = service.get_or_create_session(chat_id=10, user_id=20)
            for i in range(12):
                service.append_session_user_message(base.session_id, f"user-{i}")
                service.append_session_assistant_message(base.session_id, f"assistant-{i}")

            compacted_msgs = service.list_session_messages(base.session_id, limit=50)
            self.assertLessEqual(len(compacted_msgs), 12)
            self.assertTrue(any("history.compacted" in m.content for m in compacted_msgs if m.role == "system"))

            branched = service.create_branch_session(
                chat_id=10,
                user_id=20,
                from_session_id=base.session_id,
                copy_messages=4,
            )
            self.assertIsNotNone(branched)
            assert branched is not None
            self.assertNotEqual(branched.session_id, base.session_id)
            restored = service.activate_session(chat_id=10, user_id=20, session_id=base.session_id)
            self.assertIsNotNone(restored)
            assert restored is not None
            self.assertEqual(restored.session_id, base.session_id)

    async def test_tool_loop_exec_and_high_risk_approval(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "state.db"
            store = SqliteRunStore(db_path=db_path)
            bus = EventBus()
            runner = FakeRunner(CommandResult(returncode=0, stdout="tool-ok", stderr=""))
            provider = CodexCliProvider(runner=runner)
            service = AgentService(
                provider=provider,
                run_store=store,
                event_bus=bus,
                execution_runner=runner,
            )
            try:
                service.upsert_agent(
                    agent_id="default",
                    name="Default Agent",
                    provider="codex_cli",
                    policy_profile="trusted",
                    max_concurrency=1,
                    enabled=True,
                )
                session = service.get_or_create_session(chat_id=301, user_id=401)

                out = await service.run_prompt_with_tool_loop(
                    prompt="!exec /bin/echo hi\nSummarize quickly.",
                    chat_id=301,
                    user_id=401,
                    session_id=session.session_id,
                    agent_id="default",
                )
                self.assertIsInstance(out, str)
                self.assertIn("tool-ok", runner.last_stdin)

                pending_msg = await service.run_prompt_with_tool_loop(
                    prompt="!exec codex exec - --dangerously-bypass-approvals-and-sandbox\nDo next step.",
                    chat_id=301,
                    user_id=401,
                    session_id=session.session_id,
                    agent_id="default",
                )
                self.assertIn("Approval required", pending_msg)
                pending = service.list_pending_tool_approvals(chat_id=301, user_id=401, limit=10)
                self.assertTrue(len(pending) >= 1)
                approval_id = pending[0]["approval_id"]
                approval_run_id = pending[0]["run_id"]
                denied = service.deny_tool_action(
                    approval_id=approval_id,
                    chat_id=301,
                    user_id=401,
                )
                self.assertEqual(denied, "Denied.")
                pending_after_deny = service.list_pending_tool_approvals(chat_id=301, user_id=401, limit=10)
                self.assertFalse(any(p["approval_id"] == approval_id for p in pending_after_deny))

                pending_msg = await service.run_prompt_with_tool_loop(
                    prompt="!exec codex exec - --dangerously-bypass-approvals-and-sandbox\nDo next step again.",
                    chat_id=301,
                    user_id=401,
                    session_id=session.session_id,
                    agent_id="default",
                )
                self.assertIn("Approval required", pending_msg)
                pending = service.list_pending_tool_approvals(chat_id=301, user_id=401, limit=10)
                self.assertTrue(len(pending) >= 1)
                approval_id = pending[0]["approval_id"]
                approval_run_id = pending[0]["run_id"]
                approved_output = await service.approve_tool_action(
                    approval_id=approval_id,
                    chat_id=301,
                    user_id=401,
                )
                self.assertIn("[tool:", approved_output)
                approval_events = service.list_run_events(approval_run_id, limit=50)
                approval_types = [e.event_type for e in approval_events]
                self.assertIn("tool.approval.requested", approval_types)
                self.assertIn("tool.approval.approved", approval_types)

                budget = await service.run_prompt_with_tool_loop(
                    prompt="\n".join(
                        [
                            "!exec /bin/echo 1",
                            "!exec /bin/echo 2",
                            "!exec /bin/echo 3",
                            "!exec /bin/echo 4",
                            "summarize",
                        ]
                    ),
                    chat_id=301,
                    user_id=401,
                    session_id=session.session_id,
                    agent_id="default",
                )
                self.assertIn("tool step budget exceeded", budget)
            finally:
                await service.shutdown()

    async def test_tool_loop_json_plan_and_progress_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "state.db"
            store = SqliteRunStore(db_path=db_path)
            bus = EventBus()
            runner = FakeRunner(CommandResult(returncode=0, stdout="ok", stderr=""))
            provider = CodexCliProvider(runner=runner)
            service = AgentService(
                provider=provider,
                run_store=store,
                event_bus=bus,
                execution_runner=runner,
            )
            try:
                service.upsert_agent(
                    agent_id="default",
                    name="Default Agent",
                    provider="codex_cli",
                    policy_profile="trusted",
                    max_concurrency=1,
                    enabled=True,
                )
                session = service.get_or_create_session(chat_id=501, user_id=601)
                events = []

                async def progress(payload):
                    events.append(payload.get("event"))

                out = await service.run_prompt_with_tool_loop(
                    prompt=(
                        "!loop {\"steps\":[{\"kind\":\"exec\",\"command\":\"/bin/echo hi\"}],"
                        "\"final_prompt\":\"Summarize result\"}"
                    ),
                    chat_id=501,
                    user_id=601,
                    session_id=session.session_id,
                    agent_id="default",
                    progress_callback=progress,
                )

                self.assertTrue(isinstance(out, str))
                self.assertIn("loop.started", events)
                self.assertIn("loop.step.started", events)
                self.assertIn("loop.step.completed", events)
                self.assertIn("loop.finished", events)
            finally:
                await service.shutdown()

    async def test_tool_loop_checkpoint_skips_completed_steps(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "state.db"
            store = SqliteRunStore(db_path=db_path)
            bus = EventBus()
            runner = FakeRunner(CommandResult(returncode=0, stdout="ok", stderr=""))
            provider = CodexCliProvider(runner=runner)
            service = AgentService(
                provider=provider,
                run_store=store,
                event_bus=bus,
                execution_runner=runner,
            )
            try:
                service.upsert_agent(
                    agent_id="default",
                    name="Default Agent",
                    provider="codex_cli",
                    policy_profile="trusted",
                    max_concurrency=1,
                    enabled=True,
                )
                session = service.get_or_create_session(chat_id=601, user_id=701)
                prompt = "!exec /bin/echo hello\nSummarize."
                first = await service.run_prompt_with_tool_loop(
                    prompt=prompt,
                    chat_id=601,
                    user_id=701,
                    session_id=session.session_id,
                    agent_id="default",
                )
                self.assertTrue(isinstance(first, str))
                first_calls = len(runner.calls)
                second = await service.run_prompt_with_tool_loop(
                    prompt=prompt,
                    chat_id=601,
                    user_id=701,
                    session_id=session.session_id,
                    agent_id="default",
                )
                self.assertTrue(isinstance(second, str))
                self.assertEqual(len(runner.calls), first_calls + 1)  # no second tool step call; only model call
            finally:
                await service.shutdown()

    async def test_pending_approval_cap_enforced(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "state.db"
            store = SqliteRunStore(db_path=db_path)
            bus = EventBus()
            runner = FakeRunner(CommandResult(returncode=0, stdout="ok", stderr=""))
            provider = CodexCliProvider(runner=runner)
            service = AgentService(
                provider=provider,
                run_store=store,
                event_bus=bus,
                execution_runner=runner,
                max_pending_approvals_per_user=1,
            )
            try:
                service.upsert_agent(
                    agent_id="default",
                    name="Default Agent",
                    provider="codex_cli",
                    policy_profile="trusted",
                    max_concurrency=1,
                    enabled=True,
                )
                session = service.get_or_create_session(chat_id=801, user_id=901)
                first = await service.run_prompt_with_tool_loop(
                    prompt="!exec codex exec - --dangerously-bypass-approvals-and-sandbox\nA",
                    chat_id=801,
                    user_id=901,
                    session_id=session.session_id,
                    agent_id="default",
                )
                self.assertIn("Approval required", first)
                second = await service.run_prompt_with_tool_loop(
                    prompt="!exec codex exec - --dangerously-bypass-approvals-and-sandbox\nB",
                    chat_id=801,
                    user_id=901,
                    session_id=session.session_id,
                    agent_id="default",
                )
                self.assertIn("too many pending approvals", second)
            finally:
                await service.shutdown()

    async def test_tool_actions_use_session_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "state.db"
            workspace_root = Path(tmp) / "session-workspaces"
            store = SqliteRunStore(db_path=db_path)
            bus = EventBus()
            runner = FakeRunner(CommandResult(returncode=0, stdout="ok", stderr=""))
            provider = CodexCliProvider(runner=runner)
            service = AgentService(
                provider=provider,
                run_store=store,
                event_bus=bus,
                execution_runner=runner,
                session_workspaces_root=workspace_root,
            )
            try:
                service.upsert_agent(
                    agent_id="default",
                    name="Default Agent",
                    provider="codex_cli",
                    policy_profile="trusted",
                    max_concurrency=1,
                    enabled=True,
                )
                session = service.get_or_create_session(chat_id=1001, user_id=1101)
                await service.run_prompt_with_tool_loop(
                    prompt="!exec /bin/echo hi\nSummarize quickly.",
                    chat_id=1001,
                    user_id=1101,
                    session_id=session.session_id,
                    agent_id="default",
                )
                tool_calls = [c for c in runner.calls if c["argv"] and c["argv"][0] == "/bin/echo"]
                self.assertTrue(tool_calls)
                self.assertTrue(tool_calls[0]["workspace_root"])
                self.assertTrue(str(tool_calls[0]["workspace_root"]).startswith(str(workspace_root)))
            finally:
                await service.shutdown()

    async def test_build_session_prompt_includes_retrieval_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "state.db"
            repo_root = Path(tmp) / "repo"
            repo_root.mkdir(parents=True, exist_ok=True)
            (repo_root / "src").mkdir(parents=True, exist_ok=True)
            (repo_root / "src" / "scheduler.py").write_text(
                "def schedule_jobs():\n    return 'ok'\n",
                encoding="utf-8",
            )
            store = SqliteRunStore(db_path=db_path)
            bus = EventBus()
            runner = FakeRunner(CommandResult(returncode=0, stdout="ok", stderr=""))
            provider = CodexCliProvider(runner=runner)
            retriever = RepositoryContextRetriever(root=repo_root)
            service = AgentService(
                provider=provider,
                run_store=store,
                event_bus=bus,
                execution_runner=runner,
                repo_retriever=retriever,
            )
            try:
                session = service.get_or_create_session(chat_id=707, user_id=808)
                prompt = service.build_session_prompt(session.session_id, "schedule jobs")
                self.assertIn("Retrieval confidence:", prompt)
                self.assertIn("Relevant repository snippets:", prompt)
                self.assertIn("scheduler.py", prompt)
            finally:
                await service.shutdown()

    async def test_build_session_prompt_adds_planning_guidance_for_edit_tasks(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "state.db"
            store = SqliteRunStore(db_path=db_path)
            bus = EventBus()
            runner = FakeRunner(CommandResult(returncode=0, stdout="ok", stderr=""))
            provider = CodexCliProvider(runner=runner)
            service = AgentService(
                provider=provider,
                run_store=store,
                event_bus=bus,
                execution_runner=runner,
            )
            try:
                session = service.get_or_create_session(chat_id=1201, user_id=1301)
                prompt = service.build_session_prompt(session.session_id, "Refactor multi-file edit for scheduler and tests.")
                self.assertIn("Engineering response contract:", prompt)
                self.assertIn("CHANGES:", prompt)
            finally:
                await service.shutdown()

    async def test_tool_loop_patch_command_requires_approval_even_in_trusted(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "state.db"
            store = SqliteRunStore(db_path=db_path)
            bus = EventBus()
            runner = FakeRunner(CommandResult(returncode=0, stdout="ok", stderr=""))
            provider = CodexCliProvider(runner=runner)
            service = AgentService(
                provider=provider,
                run_store=store,
                event_bus=bus,
                execution_runner=runner,
            )
            try:
                service.upsert_agent(
                    agent_id="default",
                    name="Default Agent",
                    provider="codex_cli",
                    policy_profile="trusted",
                    max_concurrency=1,
                    enabled=True,
                )
                session = service.get_or_create_session(chat_id=1401, user_id=1501)
                out = await service.run_prompt_with_tool_loop(
                    prompt="!exec apply_patch --help\nSummarize.",
                    chat_id=1401,
                    user_id=1501,
                    session_id=session.session_id,
                    agent_id="default",
                )
                self.assertIn("Approval required", out)
            finally:
                await service.shutdown()

    async def test_session_context_diagnostics_exposed(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "state.db"
            store = SqliteRunStore(db_path=db_path)
            bus = EventBus()
            runner = FakeRunner(CommandResult(returncode=0, stdout="ok", stderr=""))
            provider = CodexCliProvider(runner=runner)
            service = AgentService(
                provider=provider,
                run_store=store,
                event_bus=bus,
                execution_runner=runner,
            )
            try:
                session = service.get_or_create_session(chat_id=1601, user_id=1701)
                _ = service.build_session_prompt(session.session_id, "Implement refactor plan for architecture.")
                diag = service.session_context_diagnostics(session.session_id)
                self.assertIn("budget_total_chars", diag)
                self.assertIn("retrieval_confidence", diag)
            finally:
                await service.shutdown()

    async def test_reliability_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "state.db"
            store = SqliteRunStore(db_path=db_path)
            bus = EventBus()
            runner = FakeRunner(CommandResult(returncode=0, stdout="ok", stderr=""))
            provider = CodexCliProvider(runner=runner)
            service = AgentService(
                provider=provider,
                run_store=store,
                event_bus=bus,
                execution_runner=runner,
            )
            try:
                await service.run_prompt("hello")
                snapshot = service.reliability_snapshot(limit=100)
                self.assertIn("failure_rate", snapshot)
                self.assertIn("latency_p95_sec", snapshot)
                self.assertIn("alerts_enabled", snapshot)
            finally:
                await service.shutdown()


class TestProviderRouter(unittest.IsolatedAsyncioTestCase):
    async def test_retry_then_success_on_primary(self):
        runner = FakeRunner(CommandResult(returncode=2, stdout="", stderr="tmp fail"))
        runner.set_results(
            [
                CommandResult(returncode=2, stdout="", stderr="tmp fail"),
                CommandResult(returncode=0, stdout="ok", stderr=""),
            ]
        )
        primary = CodexCliProvider(runner=runner)
        router = ProviderRouter(
            primary=primary,
            fallback=EchoFallbackProvider(),
            config=ProviderRouterConfig(retry_attempts=2, failure_threshold=3, recovery_sec=2),
        )

        out = await router.execute("hello")

        self.assertEqual(out, "ok")
        health = await router.health()
        self.assertEqual(health["active_provider"], "primary")

    async def test_fallback_kicks_in_after_threshold(self):
        runner = FakeRunner(CommandResult(returncode=2, stdout="", stderr="bad"))
        primary = CodexCliProvider(runner=runner)
        fallback = EchoFallbackProvider()
        router = ProviderRouter(
            primary=primary,
            fallback=fallback,
            config=ProviderRouterConfig(retry_attempts=1, failure_threshold=1, recovery_sec=60),
        )

        out1 = await router.execute("hello")
        out2 = await router.execute("hello again")

        self.assertIn("Fallback mode active", out1)
        self.assertIn("Fallback mode active", out2)
        health = await router.health()
        self.assertTrue(health["circuit_open"])
        self.assertEqual(health["active_provider"], "fallback")

    async def test_capability_mismatch_returns_explicit_error(self):
        runner = FakeRunner(CommandResult(returncode=0, stdout="ok", stderr=""))
        primary = CodexCliProvider(runner=runner)
        fallback = EchoFallbackProvider()
        router = ProviderRouter(
            primary=primary,
            fallback=fallback,
            config=ProviderRouterConfig(retry_attempts=1, failure_threshold=1, recovery_sec=10),
        )

        prompt = "!exec echo hi\n" + ("x" * 130000)
        out = await router.execute(prompt, policy_profile="trusted")

        self.assertIn("Error: provider capability mismatch.", out)
        self.assertIn("provider=primary", out)


class TestAlertDispatcher(unittest.TestCase):
    def test_threshold_and_dedup_and_dead_letter(self):
        dispatcher = AlertDispatcher(webhook_url="https://example.com/hook", timeout_sec=1)
        dispatcher._min_severity = "high"
        dispatcher._dedup_window_sec = 300

        with patch("urllib.request.urlopen", side_effect=RuntimeError("down")) as mocked:
            self.assertTrue(dispatcher.send("run.failed", "medium", "skip me", run_id="r1"))
            self.assertEqual(mocked.call_count, 0)

            ok = dispatcher.send("run.failed", "critical", "primary down", run_id="r1")
            self.assertFalse(ok)
            self.assertEqual(len(dispatcher._dead_letters), 1)

            ok2 = dispatcher.send("run.failed", "critical", "primary down", run_id="r1")
            self.assertTrue(ok2)
            self.assertEqual(dispatcher.state()["dropped_by_dedup"], 1)

        with patch("urllib.request.urlopen") as mocked_ok:
            class _Resp:
                def __enter__(self):
                    return self

                def __exit__(self, exc_type, exc, tb):
                    return False

            mocked_ok.return_value = _Resp()
            delivered = dispatcher.flush_dead_letters()
            self.assertEqual(delivered, 1)
            self.assertEqual(len(dispatcher._dead_letters), 0)
