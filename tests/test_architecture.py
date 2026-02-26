import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from codex_telegram_bot.agent_core.capabilities import MarkdownCapabilityRegistry
from codex_telegram_bot.domain.contracts import CommandResult
from codex_telegram_bot.providers.fallback import EchoFallbackProvider
from codex_telegram_bot.events.event_bus import EventBus
from codex_telegram_bot.observability.alerts import AlertDispatcher
from codex_telegram_bot.persistence.sqlite_store import SqliteRunStore
from codex_telegram_bot.providers.codex_cli import CodexCliProvider
from codex_telegram_bot.providers.router import ProviderRouter, ProviderRouterConfig
from codex_telegram_bot.services.repo_context import RepositoryContextRetriever
from codex_telegram_bot.services.agent_service import AgentService
from codex_telegram_bot.services.agent_service import _extract_email_address
from codex_telegram_bot.services.agent_service import _extract_email_triplet_from_slash_command
from codex_telegram_bot.services.agent_service import _extract_tool_invocation_from_output
from codex_telegram_bot.services.agent_service import _extract_subject_and_body_from_email_text
from codex_telegram_bot.services.agent_service import _model_job_phase_hint
from codex_telegram_bot.services.agent_service import _is_email_send_intent
from codex_telegram_bot.services.agent_service import _output_claims_email_sent
from codex_telegram_bot.services.agent_service import _parse_planner_output
from codex_telegram_bot.tools.base import ToolRegistry


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
        self.assertIn("--sandbox=workspace-write", runner.last_argv)
        self.assertEqual(runner.last_policy_profile, "balanced")

    async def test_execute_uses_policy_profile(self):
        runner = FakeRunner(CommandResult(returncode=0, stdout="hello", stderr=""))
        provider = CodexCliProvider(runner=runner)

        output = await provider.execute("prompt", policy_profile="strict")

        self.assertEqual(output, "hello")
        self.assertNotIn("--sandbox=workspace-write", runner.last_argv)
        self.assertNotIn("--sandbox=danger-full-access", runner.last_argv)
        self.assertEqual(runner.last_policy_profile, "strict")

    async def test_execute_trusted_uses_full_sandbox(self):
        runner = FakeRunner(CommandResult(returncode=0, stdout="hello", stderr=""))
        provider = CodexCliProvider(runner=runner)

        output = await provider.execute("prompt", policy_profile="trusted")

        self.assertEqual(output, "hello")
        self.assertIn("--sandbox=danger-full-access", runner.last_argv)
        self.assertEqual(runner.last_policy_profile, "trusted")

    async def test_execute_retries_once_on_timeout_124(self):
        runner = FakeRunner(CommandResult(returncode=124, stdout="", stderr="Execution timeout."))
        runner.set_results(
            [
                CommandResult(returncode=124, stdout="partial output", stderr="Execution timeout."),
                CommandResult(returncode=0, stdout="final output", stderr=""),
            ]
        )
        provider = CodexCliProvider(runner=runner, timeout_continue_retries=1)

        output = await provider.execute("long task", policy_profile="trusted")

        self.assertEqual(output, "final output")
        self.assertEqual(len(runner.calls), 2)
        self.assertIn("--sandbox=danger-full-access", runner.calls[0]["argv"])
        self.assertIn("System recovery note", runner.calls[1]["stdin_text"])

    async def test_generate_uses_messages_contract(self):
        runner = FakeRunner(CommandResult(returncode=0, stdout="hello", stderr=""))
        provider = CodexCliProvider(runner=runner)

        output = await provider.generate(
            [
                {"role": "system", "content": "You are terse."},
                {"role": "user", "content": "Do thing"},
            ],
            stream=False,
            policy_profile="balanced",
        )

        self.assertEqual(output, "hello")
        self.assertIn("You are terse.", runner.last_stdin)
        self.assertIn("user: Do thing", runner.last_stdin)

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

    async def test_tool_loop_supports_registered_tool_steps(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "state.db"
            store = SqliteRunStore(db_path=db_path)
            bus = EventBus()
            runner = FakeRunner(CommandResult(returncode=0, stdout="model-ok", stderr=""))
            provider = CodexCliProvider(runner=runner)
            service = AgentService(
                provider=provider,
                run_store=store,
                event_bus=bus,
                execution_runner=runner,
            )
            try:
                session = service.get_or_create_session(chat_id=511, user_id=611)
                ws = service.session_workspace(session.session_id)
                (ws / "notes.txt").write_text("hello tool", encoding="utf-8")

                out = await service.run_prompt_with_tool_loop(
                    prompt='!tool {"name":"read_file","args":{"path":"notes.txt"}}\nSummarize.',
                    chat_id=511,
                    user_id=611,
                    session_id=session.session_id,
                    agent_id="default",
                )

                self.assertEqual(out, "model-ok")
                self.assertIn("hello tool", runner.last_stdin)
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

    async def test_session_reset_reinitializes_workspace_root(self):
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
                first = service.get_or_create_session(chat_id=2101, user_id=2201)
                first_root = service.session_workspace(first.session_id).resolve()
                (first_root / "keep.txt").write_text("x", encoding="utf-8")

                second = service.reset_session(chat_id=2101, user_id=2201)
                second_root = service.session_workspace(second.session_id).resolve()
                reinit = service.initialize_session_workspace(
                    session_id=second.session_id,
                    previous_session_id=first.session_id,
                )

                self.assertNotEqual(first.session_id, second.session_id)
                self.assertNotEqual(first_root, second_root)
                self.assertEqual(reinit["workspace_root"], str(second_root))
                self.assertEqual(reinit["previous_workspace_root"], str(first_root))
                self.assertTrue(second_root.exists())
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

    async def test_build_session_prompt_adds_capability_hints_selectively(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "state.db"
            cap_root = Path(tmp) / "capabilities"
            cap_root.mkdir(parents=True, exist_ok=True)
            (cap_root / "system.md").write_text("# System\n- deterministic\n", encoding="utf-8")
            (cap_root / "git.md").write_text("# Git\n- status and diff\n", encoding="utf-8")
            (cap_root / "files.md").write_text("# Files\n- safe reads/writes\n", encoding="utf-8")
            store = SqliteRunStore(db_path=db_path)
            bus = EventBus()
            runner = FakeRunner(CommandResult(returncode=0, stdout="ok", stderr=""))
            provider = CodexCliProvider(runner=runner)
            service = AgentService(
                provider=provider,
                run_store=store,
                event_bus=bus,
                execution_runner=runner,
                capability_registry=MarkdownCapabilityRegistry(cap_root),
            )
            try:
                session = service.get_or_create_session(chat_id=1202, user_id=1302)
                prompt = service.build_session_prompt(session.session_id, "show git status and branch details")
                self.assertIn("Capability hints (selective summaries):", prompt)
                self.assertIn("git capability", prompt)
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


class TestModelJobPhaseHints(unittest.TestCase):
    def test_phase_hint_progression(self):
        self.assertEqual(_model_job_phase_hint(5, 0), "analyzing request and workspace context")
        self.assertEqual(_model_job_phase_hint(40, 1), "planning next concrete steps")
        self.assertEqual(_model_job_phase_hint(120, 2), "performing repository edits/checks")
        self.assertIn(
            _model_job_phase_hint(240, 3),
            {"performing repository edits/checks", "verifying output and preparing response"},
        )


class TestAutonomousToolPlanner(unittest.IsolatedAsyncioTestCase):
    async def test_probe_no_tools_returns_immediately(self):
        class _Provider:
            def __init__(self):
                self.calls = 0

            async def generate(self, messages, stream=False, correlation_id="", policy_profile="balanced"):
                self.calls += 1
                if self.calls == 1:
                    return "NO_TOOLS\nParis is the capital of France."
                return "unexpected second model call"

            async def version(self):
                return "v1"

            async def health(self):
                return {"status": "ok"}

            def capabilities(self):
                return {"provider": "fake"}

        provider = _Provider()
        service = AgentService(provider=provider)
        out = await service.run_prompt_with_tool_loop(
            prompt="What is the capital of France?",
            chat_id=1,
            user_id=1,
            session_id="sess-probe-1",
            agent_id="default",
        )
        self.assertEqual(out, "Paris is the capital of France.")
        self.assertEqual(provider.calls, 1)
        await service.shutdown()

    async def test_probe_need_tools_injects_selected_tool_schemas_only(self):
        class _Provider:
            def __init__(self):
                self.prompts = []

            async def generate(self, messages, stream=False, correlation_id="", policy_profile="balanced"):
                content = str(messages[0].get("content") or "")
                self.prompts.append(content)
                if len(self.prompts) == 1:
                    return 'NEED_TOOLS {"tools":["read_file","shell_exec"],"goal":"Inspect a file","max_steps":2}'
                return "Done without tool execution."

            async def version(self):
                return "v1"

            async def health(self):
                return {"status": "ok"}

            def capabilities(self):
                return {"provider": "fake"}

        with tempfile.TemporaryDirectory() as tmp:
            cap_root = Path(tmp) / "caps"
            cap_root.mkdir(parents=True, exist_ok=True)
            (cap_root / "system.md").write_text("# System\n- baseline\n", encoding="utf-8")
            provider = _Provider()
            service = AgentService(
                provider=provider,
                capability_registry=MarkdownCapabilityRegistry(cap_root),
            )
            out = await service.run_prompt_with_tool_loop(
                prompt="Inspect README and tell me what's inside.",
                chat_id=1,
                user_id=1,
                session_id="sess-probe-2",
                agent_id="default",
            )
            self.assertEqual(out, "Done without tool execution.")
            self.assertGreaterEqual(len(provider.prompts), 2)
            need_tools_prompt = provider.prompts[1]
            self.assertIn('"name": "read_file"', need_tools_prompt)
            self.assertIn('"name": "shell_exec"', need_tools_prompt)
            self.assertNotIn('"name": "git_commit"', need_tools_prompt)
            self.assertIn("Capability hints (tool-selected):", need_tools_prompt)
            self.assertIn("Files capability", need_tools_prompt)
            self.assertIn("Shell capability", need_tools_prompt)
            self.assertNotIn("Git capability", need_tools_prompt)
            await service.shutdown()

    async def test_probe_no_tools_with_exec_block_is_still_executed(self):
        class _Provider:
            def __init__(self):
                self.calls = 0

            async def generate(self, messages, stream=False, correlation_id="", policy_profile="balanced"):
                self.calls += 1
                if self.calls == 1:
                    return "NO_TOOLS\n!exec\necho probe-step"
                return "Done. probe-step executed."

            async def version(self):
                return "v1"

            async def health(self):
                return {"status": "ok"}

            def capabilities(self):
                return {"provider": "fake"}

        runner = FakeRunner(CommandResult(returncode=0, stdout="probe-step\n", stderr=""))
        service = AgentService(provider=_Provider(), execution_runner=runner)
        out = await service.run_prompt_with_tool_loop(
            prompt="Create the file now.",
            chat_id=1,
            user_id=1,
            session_id="sess-probe-no-tools-exec",
            agent_id="default",
        )
        self.assertEqual(runner.last_argv, ["echo", "probe-step"])
        self.assertIn("probe-step executed", out)
        await service.shutdown()

    async def test_action_prompt_overrides_no_tools_probe_and_executes(self):
        class _Provider:
            def __init__(self):
                self.calls = 0

            async def generate(self, messages, stream=False, correlation_id="", policy_profile="balanced"):
                self.calls += 1
                if self.calls == 1:
                    return "NO_TOOLS\nI cannot do that."
                if self.calls == 2:
                    return "!exec echo forced-action"
                return "Done. Executed forced-action."

            async def version(self):
                return "v1"

            async def health(self):
                return {"status": "ok"}

            def capabilities(self):
                return {"provider": "fake"}

        runner = FakeRunner(CommandResult(returncode=0, stdout="forced-action\n", stderr=""))
        service = AgentService(provider=_Provider(), execution_runner=runner)
        out = await service.run_prompt_with_tool_loop(
            prompt="Install and configure the required tooling.",
            chat_id=1,
            user_id=1,
            session_id="sess-probe-3",
            agent_id="default",
        )
        self.assertEqual(runner.last_argv, ["echo", "forced-action"])
        self.assertIn("Done. Executed forced-action.", out)
        await service.shutdown()

    async def test_model_emitted_protocol_block_is_executed(self):
        class _Provider:
            def __init__(self):
                self.calls = 0

            async def generate(self, messages, stream=False, correlation_id="", policy_profile="balanced"):
                self.calls += 1
                if self.calls == 1:
                    return "UNPARSEABLE_PROBE"
                if self.calls == 2:
                    return "!exec echo protocol-step"
                return "Done. protocol-step executed."

            async def version(self):
                return "v1"

            async def health(self):
                return {"status": "ok"}

            def capabilities(self):
                return {"provider": "fake"}

        runner = FakeRunner(CommandResult(returncode=0, stdout="protocol-step\n", stderr=""))
        service = AgentService(provider=_Provider(), execution_runner=runner)
        out = await service.run_prompt_with_tool_loop(
            prompt="Please proceed.",
            chat_id=2,
            user_id=2,
            session_id="sess-probe-4",
            agent_id="default",
        )
        self.assertEqual(runner.last_argv, ["echo", "protocol-step"])
        self.assertIn("protocol-step executed", out)
        await service.shutdown()

    async def test_model_emitted_multiline_exec_protocol_is_executed(self):
        class _Provider:
            def __init__(self):
                self.calls = 0

            async def generate(self, messages, stream=False, correlation_id="", policy_profile="balanced"):
                self.calls += 1
                if self.calls == 1:
                    return "UNPARSEABLE_PROBE"
                if self.calls == 2:
                    return "!exec python3 -c \"\nprint('protocol-step')\n\""
                return "Done. protocol-step executed."

            async def version(self):
                return "v1"

            async def health(self):
                return {"status": "ok"}

            def capabilities(self):
                return {"provider": "fake"}

        runner = FakeRunner(CommandResult(returncode=0, stdout="protocol-step\n", stderr=""))
        service = AgentService(provider=_Provider(), execution_runner=runner)
        out = await service.run_prompt_with_tool_loop(
            prompt="Please proceed.",
            chat_id=2,
            user_id=2,
            session_id="sess-probe-4b",
            agent_id="default",
        )
        self.assertEqual(runner.last_argv[0:2], ["python3", "-c"])
        self.assertIn("protocol-step", runner.last_argv[2])
        self.assertIn("protocol-step executed", out)
        await service.shutdown()

    async def test_model_emitted_tool_exec_shorthand_is_executed(self):
        class _Provider:
            def __init__(self):
                self.calls = 0

            async def generate(self, messages, stream=False, correlation_id="", policy_profile="balanced"):
                self.calls += 1
                if self.calls == 1:
                    return "UNPARSEABLE_PROBE"
                if self.calls == 2:
                    return '!tool exec cmd="echo shorthand-step"'
                return "Done. shorthand-step executed."

            async def version(self):
                return "v1"

            async def health(self):
                return {"status": "ok"}

            def capabilities(self):
                return {"provider": "fake"}

        runner = FakeRunner(CommandResult(returncode=0, stdout="shorthand-step\n", stderr=""))
        service = AgentService(provider=_Provider(), execution_runner=runner)
        out = await service.run_prompt_with_tool_loop(
            prompt="Please proceed.",
            chat_id=2,
            user_id=2,
            session_id="sess-probe-4d",
            agent_id="default",
        )
        self.assertEqual(runner.last_argv, ["echo", "shorthand-step"])
        self.assertIn("shorthand-step executed", out)
        await service.shutdown()

    async def test_model_emitted_split_exec_block_is_executed(self):
        class _Provider:
            def __init__(self):
                self.calls = 0

            async def generate(self, messages, stream=False, correlation_id="", policy_profile="balanced"):
                self.calls += 1
                if self.calls == 1:
                    return "UNPARSEABLE_PROBE"
                if self.calls == 2:
                    return "!exec\necho split-step"
                return "Done. split-step executed."

            async def version(self):
                return "v1"

            async def health(self):
                return {"status": "ok"}

            def capabilities(self):
                return {"provider": "fake"}

        runner = FakeRunner(CommandResult(returncode=0, stdout="split-step\n", stderr=""))
        service = AgentService(provider=_Provider(), execution_runner=runner)
        out = await service.run_prompt_with_tool_loop(
            prompt="Please proceed.",
            chat_id=2,
            user_id=2,
            session_id="sess-probe-4e",
            agent_id="default",
        )
        self.assertEqual(runner.last_argv, ["echo", "split-step"])
        self.assertIn("split-step executed", out)
        await service.shutdown()

    async def test_model_emitted_bash_fence_command_is_executed(self):
        class _Provider:
            def __init__(self):
                self.calls = 0

            async def generate(self, messages, stream=False, correlation_id="", policy_profile="balanced"):
                self.calls += 1
                if self.calls == 1:
                    return "UNPARSEABLE_PROBE"
                if self.calls == 2:
                    return "I'll check now.\n\n```bash\necho fenced-step\n```"
                return "Done. fenced-step executed."

            async def version(self):
                return "v1"

            async def health(self):
                return {"status": "ok"}

            def capabilities(self):
                return {"provider": "fake"}

        runner = FakeRunner(CommandResult(returncode=0, stdout="fenced-step\n", stderr=""))
        service = AgentService(provider=_Provider(), execution_runner=runner)
        out = await service.run_prompt_with_tool_loop(
            prompt="Create the file now.",
            chat_id=2,
            user_id=2,
            session_id="sess-probe-4f",
            agent_id="default",
        )
        self.assertEqual(runner.last_argv, ["echo", "fenced-step"])
        self.assertIn("fenced-step executed", out)
        await service.shutdown()

    async def test_model_emitted_protocol_can_chain_multiple_follow_up_steps(self):
        class _Provider:
            def __init__(self):
                self.calls = 0

            async def generate(self, messages, stream=False, correlation_id="", policy_profile="balanced"):
                self.calls += 1
                if self.calls == 1:
                    return "UNPARSEABLE_PROBE"
                if self.calls == 2:
                    return "!exec echo first-step"
                if self.calls == 3:
                    return "!exec echo second-step"
                if self.calls == 4:
                    return "!exec echo third-step"
                return "Done. Completed all steps."

            async def version(self):
                return "v1"

            async def health(self):
                return {"status": "ok"}

            def capabilities(self):
                return {"provider": "fake"}

        runner = FakeRunner(CommandResult(returncode=0, stdout="ok\n", stderr=""))
        runner.set_results(
            [
                CommandResult(returncode=0, stdout="first-step\n", stderr=""),
                CommandResult(returncode=0, stdout="second-step\n", stderr=""),
                CommandResult(returncode=0, stdout="third-step\n", stderr=""),
            ]
        )
        service = AgentService(provider=_Provider(), execution_runner=runner)
        out = await service.run_prompt_with_tool_loop(
            prompt="Please proceed and finish the task.",
            chat_id=2,
            user_id=2,
            session_id="sess-probe-4c",
            agent_id="default",
        )
        self.assertEqual(len(runner.calls), 3)
        self.assertEqual(runner.last_argv, ["echo", "third-step"])
        self.assertIn("Completed all steps", out)
        await service.shutdown()

    async def test_action_promise_triggers_tool_call_correction(self):
        class _Provider:
            def __init__(self):
                self.calls = 0

            async def generate(self, messages, stream=False, correlation_id="", policy_profile="balanced"):
                self.calls += 1
                content = str(messages[0].get("content") or "")
                if self.calls == 1:
                    return 'NEED_TOOLS {"tools":["shell_exec"],"goal":"Run command","max_steps":2}'
                if self.calls == 2:
                    return "I can run that now."
                if "previous response described intent but did not execute" in content.lower():
                    return '!exec echo corrected-step'
                return "Done. corrected-step executed."

            async def version(self):
                return "v1"

            async def health(self):
                return {"status": "ok"}

            def capabilities(self):
                return {"provider": "fake"}

        runner = FakeRunner(CommandResult(returncode=0, stdout="corrected-step\n", stderr=""))
        service = AgentService(provider=_Provider(), execution_runner=runner)
        out = await service.run_prompt_with_tool_loop(
            prompt="Create the file now.",
            chat_id=2,
            user_id=2,
            session_id="sess-probe-correction",
            agent_id="default",
        )
        self.assertEqual(runner.last_argv, ["echo", "corrected-step"])
        self.assertIn("corrected-step executed", out)
        await service.shutdown()

    async def test_need_tools_false_done_claim_is_repaired_before_completion(self):
        class _Provider:
            def __init__(self):
                self.calls = 0

            async def generate(self, messages, stream=False, correlation_id="", policy_profile="balanced"):
                self.calls += 1
                if self.calls == 1:
                    return 'NEED_TOOLS {"tools":["write_file"],"goal":"Create a file","max_steps":2}'
                if self.calls == 2:
                    return "Done. I wrote a.txt."
                if self.calls == 3:
                    return '!tool {"name":"write_file","args":{"path":"a.txt","content":"hi\\n"}}'
                return "Done. Created and verified the file."

            async def version(self):
                return "v1"

            async def health(self):
                return {"status": "ok"}

            def capabilities(self):
                return {"provider": "fake"}

        with tempfile.TemporaryDirectory() as tmp:
            workspaces = Path(tmp) / "ws"
            service = AgentService(provider=_Provider(), session_workspaces_root=workspaces)
            out = await service.run_prompt_with_tool_loop(
                prompt="Create file a.txt with hi",
                chat_id=2,
                user_id=2,
                session_id="sess-false-done",
                agent_id="default",
            )
            file_path = service.session_workspace("sess-false-done") / "a.txt"
            self.assertTrue(file_path.exists())
            self.assertEqual(file_path.read_text(encoding="utf-8"), "hi\n")
            self.assertIn("Created and verified", out)
            await service.shutdown()

    async def test_model_emitted_step_with_pipe_executes_via_shell_wrapper(self):
        class _Provider:
            def __init__(self):
                self.calls = 0

            async def generate(self, messages, stream=False, correlation_id="", policy_profile="balanced"):
                self.calls += 1
                if self.calls == 1:
                    return 'NEED_TOOLS {"tools":["shell_exec"],"goal":"Run command","max_steps":2}'
                if self.calls == 2:
                    return "Step 1: echo one | cat"
                return "Done. one"

            async def version(self):
                return "v1"

            async def health(self):
                return {"status": "ok"}

            def capabilities(self):
                return {"provider": "fake"}

        runner = FakeRunner(CommandResult(returncode=0, stdout="one\n", stderr=""))
        service = AgentService(provider=_Provider(), execution_runner=runner)
        out = await service.run_prompt_with_tool_loop(
            prompt="Run the command now.",
            chat_id=2,
            user_id=2,
            session_id="sess-probe-shell-wrap",
            agent_id="default",
        )
        self.assertEqual(runner.last_argv, ["bash", "-lc", "echo one | cat"])
        self.assertIn("Done. one", out)
        await service.shutdown()

    async def test_autonomous_tool_loop_uses_planner_as_fallback_after_probe(self):
        class _PlannerProvider:
            def __init__(self):
                self._calls = 0
                self.prompts = []

            async def generate(self, messages, stream=False, correlation_id="", policy_profile="balanced"):
                self._calls += 1
                content = str(messages[0].get("content") or "")
                self.prompts.append(content)
                if self._calls == 1:
                    return "UNPARSEABLE_PROBE"
                if self._calls == 2:
                    return ""
                if self._calls == 3:
                    return '{"steps":[{"kind":"exec","command":"echo hello"}],"final_prompt":"Summarize result."}'
                return "final answer"

            async def version(self):
                return "planner/1"

            async def health(self):
                return {"status": "ok"}

            def capabilities(self):
                return {"provider": "planner", "supports_tool_calls": False}

        runner = FakeRunner(CommandResult(returncode=0, stdout="hello\n", stderr=""))
        service = AgentService(provider=_PlannerProvider(), execution_runner=runner)
        with patch.dict("os.environ", {"AUTONOMOUS_TOOL_LOOP": "1"}):
            out = await service.run_prompt_with_tool_loop(
                prompt="Please inspect and fix files.",
                chat_id=1,
                user_id=1,
                session_id="sess-1",
                agent_id="default",
            )
        self.assertEqual(runner.last_argv, ["echo", "hello"])
        self.assertIn("final answer", out)
        self.assertIn("Classify whether", service._provider.prompts[0])
        self.assertIn("Behavior rules (strict):", service._provider.prompts[1])
        self.assertIn("You are an execution planner.", service._provider.prompts[2])
        await service.shutdown()

    def test_parse_planner_output_accepts_fenced_json(self):
        raw = "```json\n{\"steps\":[],\"final_prompt\":\"done\"}\n```"
        parsed = _parse_planner_output(raw)
        self.assertEqual(parsed.get("final_prompt"), "done")

    async def test_registered_tool_action_supports_async_arun(self):
        class _AsyncTool:
            name = "async_tool"

            async def arun(self, request, context):
                return type("R", (), {"ok": True, "output": "async-ok"})()

        class _Provider:
            async def generate(self, messages, stream=False, correlation_id="", policy_profile="balanced"):
                return "ok"

            async def version(self):
                return "v1"

            async def health(self):
                return {"status": "ok"}

            def capabilities(self):
                return {"provider": "fake"}

        service = AgentService(provider=_Provider())
        out = await service._execute_registered_tool_action(
            action_id="tool-1",
            tool_name="async_tool",
            tool_args={},
            workspace_root=Path("."),
            policy_profile="trusted",
            extra_tools={"async_tool": _AsyncTool()},
        )
        self.assertTrue(out.ok)
        self.assertIn("async-ok", out.output)
        await service.shutdown()

    async def test_email_send_claim_without_tool_returns_error(self):
        class _Provider:
            async def generate(self, messages, stream=False, correlation_id="", policy_profile="balanced"):
                return "I'll send the email now."

            async def version(self):
                return "v1"

            async def health(self):
                return {"status": "ok"}

            def capabilities(self):
                return {"provider": "fake"}

        with tempfile.TemporaryDirectory() as tmp:
            store = SqliteRunStore(db_path=Path(tmp) / "state.db")
            service = AgentService(provider=_Provider(), run_store=store, event_bus=EventBus())
            session = service.get_or_create_session(chat_id=11, user_id=22)
            out = await service.run_prompt_with_tool_loop(
                prompt="Please send this email to investor@example.com",
                chat_id=11,
                user_id=22,
                session_id=session.session_id,
                agent_id="default",
            )
            self.assertTrue(out.startswith("Error: email send was claimed"))
            await service.shutdown()

    async def test_email_tool_unavailable_error_is_actionable(self):
        class _Provider:
            async def generate(self, messages, stream=False, correlation_id="", policy_profile="balanced"):
                return "ok"

            async def version(self):
                return "v1"

            async def health(self):
                return {"status": "ok"}

            def capabilities(self):
                return {"provider": "fake"}

        with tempfile.TemporaryDirectory() as tmp:
            store = SqliteRunStore(db_path=Path(tmp) / "state.db")
            service = AgentService(provider=_Provider(), run_store=store, event_bus=EventBus())
            result = await service._execute_registered_tool_action(
                action_id="tool-x",
                tool_name="send_email_smtp",
                tool_args={"to": "a@b.com", "subject": "S", "body": "B"},
                workspace_root=Path(tmp),
                policy_profile="balanced",
                extra_tools={},
            )
            self.assertIn("error=tool_unavailable", result.output)
            self.assertIn("Email tool is not available in this runtime", result.output)
            self.assertNotIn("known=[", result.output)
            await service.shutdown()

    async def test_email_send_claim_without_send_intent_still_returns_error(self):
        class _Provider:
            async def generate(self, messages, stream=False, correlation_id="", policy_profile="balanced"):
                return "I'll send this email now."

            async def version(self):
                return "v1"

            async def health(self):
                return {"status": "ok"}

            def capabilities(self):
                return {"provider": "fake"}

        with tempfile.TemporaryDirectory() as tmp:
            store = SqliteRunStore(db_path=Path(tmp) / "state.db")
            service = AgentService(provider=_Provider(), run_store=store, event_bus=EventBus())
            session = service.get_or_create_session(chat_id=12, user_id=23)
            out = await service.run_prompt_with_tool_loop(
                prompt="Please polish this draft so it sounds more personal.",
                chat_id=12,
                user_id=23,
                session_id=session.session_id,
                agent_id="default",
            )
            self.assertTrue(out.startswith("Error: email send was claimed"))
            await service.shutdown()

    async def test_email_send_claim_triggers_autonomous_tool_recovery(self):
        class _Provider:
            async def generate(self, messages, stream=False, correlation_id="", policy_profile="balanced"):
                return "I'll send the email now.\n\n**Subject:** Intro from Michal\nHi Amber Group Team,\nBody text."

            async def version(self):
                return "v1"

            async def health(self):
                return {"status": "ok"}

            def capabilities(self):
                return {"provider": "fake"}

        class _FakeEmailTool:
            name = "send_email_smtp"

            def run(self, request, context):
                return type("R", (), {"ok": True, "output": f"Email sent to {request.args.get('to')}"})()

        class _Skill:
            skill_id = "smtp_email"

        class _SkillManager:
            def auto_activate(self, _prompt):
                return [_Skill()]

            def tools_for_skills(self, _skills):
                return {"send_email_smtp": _FakeEmailTool()}

        with tempfile.TemporaryDirectory() as tmp:
            store = SqliteRunStore(db_path=Path(tmp) / "state.db")
            service = AgentService(
                provider=_Provider(),
                run_store=store,
                event_bus=EventBus(),
                skill_manager=_SkillManager(),
            )
            session = service.get_or_create_session(chat_id=13, user_id=24)
            out = await service.run_prompt_with_tool_loop(
                prompt="Please finalize and send to partnerships@ambergroup.io",
                chat_id=13,
                user_id=24,
                session_id=session.session_id,
                agent_id="default",
            )
            self.assertIn("Email sent to partnerships@ambergroup.io", out)
            await service.shutdown()

    async def test_email_send_claim_in_actions_path_triggers_recovery(self):
        class _Provider:
            async def generate(self, messages, stream=False, correlation_id="", policy_profile="balanced"):
                return "I'll send the email now.\n\n**Subject:** Intro from Michal\nHi Amber Group Team,\nBody text."

            async def version(self):
                return "v1"

            async def health(self):
                return {"status": "ok"}

            def capabilities(self):
                return {"provider": "fake"}

        class _FakeEmailTool:
            name = "send_email_smtp"

            def run(self, request, context):
                return type("R", (), {"ok": True, "output": f"Email sent to {request.args.get('to')}"})()

        class _Skill:
            skill_id = "smtp_email"

        class _SkillManager:
            def auto_activate(self, _prompt):
                return [_Skill()]

            def tools_for_skills(self, _skills):
                return {"send_email_smtp": _FakeEmailTool()}

        with tempfile.TemporaryDirectory() as tmp:
            store = SqliteRunStore(db_path=Path(tmp) / "state.db")
            runner = FakeRunner(CommandResult(returncode=0, stdout="ok", stderr=""))
            service = AgentService(
                provider=_Provider(),
                run_store=store,
                event_bus=EventBus(),
                execution_runner=runner,
                skill_manager=_SkillManager(),
            )
            session = service.get_or_create_session(chat_id=14, user_id=25)
            out = await service.run_prompt_with_tool_loop(
                prompt="!exec /bin/echo hello\nPlease send to partnerships@ambergroup.io",
                chat_id=14,
                user_id=25,
                session_id=session.session_id,
                agent_id="default",
            )
            self.assertIn("Email sent to partnerships@ambergroup.io", out)
            await service.shutdown()

    async def test_email_send_claim_with_slash_command_output_triggers_recovery(self):
        class _Provider:
            async def generate(self, messages, stream=False, correlation_id="", policy_profile="balanced"):
                return (
                    "I'll send the email again using the correct format.\n\n"
                    "/email partnerships@ambergroup.io | Introduction from Michal Ptacnik | "
                    "Hi Amber Group Team"
                )

            async def version(self):
                return "v1"

            async def health(self):
                return {"status": "ok"}

            def capabilities(self):
                return {"provider": "fake"}

        class _FakeEmailTool:
            name = "send_email_smtp"

            def run(self, request, context):
                return type("R", (), {"ok": True, "output": f"Email sent to {request.args.get('to')}"})()

        class _Skill:
            skill_id = "smtp_email"

        class _SkillManager:
            def auto_activate(self, _prompt):
                return [_Skill()]

            def tools_for_skills(self, _skills):
                return {"send_email_smtp": _FakeEmailTool()}

        with tempfile.TemporaryDirectory() as tmp:
            store = SqliteRunStore(db_path=Path(tmp) / "state.db")
            service = AgentService(
                provider=_Provider(),
                run_store=store,
                event_bus=EventBus(),
                skill_manager=_SkillManager(),
            )
            session = service.get_or_create_session(chat_id=15, user_id=26)
            out = await service.run_prompt_with_tool_loop(
                prompt="Try again",
                chat_id=15,
                user_id=26,
                session_id=session.session_id,
                agent_id="default",
            )
            self.assertIn("Email sent to partnerships@ambergroup.io", out)
            await service.shutdown()

    async def test_email_tool_action_requires_approval_when_enabled(self):
        class _Provider:
            async def generate(self, messages, stream=False, correlation_id="", policy_profile="balanced"):
                return "ok"

            async def version(self):
                return "v1"

            async def health(self):
                return {"status": "ok"}

            def capabilities(self):
                return {"provider": "fake"}

        class _FakeEmailTool:
            name = "send_email_smtp"

            def __init__(self):
                self.calls = 0

            def run(self, request, context):
                self.calls += 1
                return type("R", (), {"ok": True, "output": "sent"})()

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "state.db"
            store = SqliteRunStore(db_path=db_path)
            bus = EventBus()
            fake_tool = _FakeEmailTool()
            registry = ToolRegistry()
            registry.register(fake_tool)
            service = AgentService(
                provider=_Provider(),
                run_store=store,
                event_bus=bus,
                tool_registry=registry,
            )
            with patch.dict("os.environ", {"ENABLE_EMAIL_TOOL": "1"}):
                session = service.get_or_create_session(chat_id=21, user_id=22)
                out = await service.run_prompt_with_tool_loop(
                    prompt='!tool {"name":"send_email_smtp","args":{"to":"a@b.com","subject":"S","body":"B"}}',
                    chat_id=21,
                    user_id=22,
                    session_id=session.session_id,
                    agent_id="default",
                )
                self.assertIn("Approval required for high-risk tool action", out)
                self.assertEqual(fake_tool.calls, 0)
                pending = service.list_pending_tool_approvals(chat_id=21, user_id=22, limit=5)
                self.assertEqual(len(pending), 1)
                approved = await service.approve_tool_action(
                    approval_id=pending[0]["approval_id"],
                    chat_id=21,
                    user_id=22,
                )
                self.assertIn("[tool:", approved)
                self.assertEqual(fake_tool.calls, 1)
            await service.shutdown()

    async def test_email_tool_action_requires_approval_when_smtp_env_present(self):
        class _Provider:
            async def generate(self, messages, stream=False, correlation_id="", policy_profile="balanced"):
                return "ok"

            async def version(self):
                return "v1"

            async def health(self):
                return {"status": "ok"}

            def capabilities(self):
                return {"provider": "fake"}

        class _FakeEmailTool:
            name = "send_email_smtp"

            def run(self, request, context):
                return type("R", (), {"ok": True, "output": "sent"})()

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "state.db"
            store = SqliteRunStore(db_path=db_path)
            bus = EventBus()
            registry = ToolRegistry()
            registry.register(_FakeEmailTool())
            service = AgentService(
                provider=_Provider(),
                run_store=store,
                event_bus=bus,
                tool_registry=registry,
            )
            with patch.dict(
                "os.environ",
                {
                    "SMTP_HOST": "smtp.example.com",
                    "SMTP_USER": "bot@example.com",
                    "SMTP_APP_PASSWORD": "app-password",
                },
                clear=True,
            ):
                session = service.get_or_create_session(chat_id=31, user_id=32)
                out = await service.run_prompt_with_tool_loop(
                    prompt='!tool {"name":"send_email_smtp","args":{"to":"a@b.com","subject":"S","body":"B"}}',
                    chat_id=31,
                    user_id=32,
                    session_id=session.session_id,
                    agent_id="default",
                )
                self.assertIn("Approval required for high-risk tool action", out)
            await service.shutdown()

    async def test_generic_slash_tool_invocation_executes(self):
        class _Provider:
            async def generate(self, messages, stream=False, correlation_id="", policy_profile="balanced"):
                return "/contact list"

            async def version(self):
                return "v1"

            async def health(self):
                return {"status": "ok"}

            def capabilities(self):
                return {"provider": "fake"}

        class _ContactListTool:
            name = "contact_list"

            def run(self, request, context):
                return type("R", (), {"ok": True, "output": "contacts: none"})()

        class _Skill:
            skill_id = "email_ops"

        class _SkillManager:
            def auto_activate(self, _prompt):
                return [_Skill()]

            def tools_for_skills(self, _skills):
                return {"contact_list": _ContactListTool()}

        with tempfile.TemporaryDirectory() as tmp:
            store = SqliteRunStore(db_path=Path(tmp) / "state.db")
            service = AgentService(
                provider=_Provider(),
                run_store=store,
                event_bus=EventBus(),
                skill_manager=_SkillManager(),
            )
            session = service.get_or_create_session(chat_id=16, user_id=27)
            out = await service.run_prompt_with_tool_loop(
                prompt="Do what is needed.",
                chat_id=16,
                user_id=27,
                session_id=session.session_id,
                agent_id="default",
            )
            self.assertEqual(out, "contacts: none")
            await service.shutdown()


class TestEmailIntentGuards(unittest.TestCase):
    def test_detects_email_send_intent(self):
        self.assertTrue(_is_email_send_intent("Please send this email to x@y.com"))
        self.assertTrue(_is_email_send_intent("Resend the mail now"))
        self.assertFalse(_is_email_send_intent("Draft an email only, do not send"))

    def test_detects_claimed_send_in_output(self):
        self.assertTrue(_output_claims_email_sent("I'll send the email now."))
        self.assertTrue(_output_claims_email_sent("Email sent to team@example.com"))
        self.assertFalse(_output_claims_email_sent("Here is a draft email you can send"))

    def test_extract_email_address(self):
        self.assertEqual(
            _extract_email_address("Use [Amber Group] [partnerships@ambergroup.io] contact"),
            "partnerships@ambergroup.io",
        )

    def test_extract_subject_and_body(self):
        subject, body = _extract_subject_and_body_from_email_text(
            "I will send it.\n\n**Subject:** Intro from Michal\nHi Team,\nThis is body."
        )
        self.assertEqual(subject, "Intro from Michal")
        self.assertIn("Hi Team", body)

    def test_extract_email_triplet_from_slash_command(self):
        to_addr, subject, body = _extract_email_triplet_from_slash_command(
            "I'll send now.\n/email partnerships@ambergroup.io | Intro | Hello team"
        )
        self.assertEqual(to_addr, "partnerships@ambergroup.io")
        self.assertEqual(subject, "Intro")
        self.assertEqual(body, "Hello team")

    def test_extract_tool_invocation_from_json(self):
        parsed = _extract_tool_invocation_from_output(
            '{"name":"send_email_smtp","args":{"to":"a@b.com","subject":"S","body":"B"}}'
        )
        self.assertEqual(parsed[0], "send_email_smtp")
        self.assertEqual(parsed[1]["to"], "a@b.com")

    def test_extract_tool_invocation_from_slash_contact(self):
        parsed = _extract_tool_invocation_from_output("/contact add user@example.com John Doe")
        self.assertEqual(parsed[0], "contact_upsert")
        self.assertEqual(parsed[1]["email"], "user@example.com")
