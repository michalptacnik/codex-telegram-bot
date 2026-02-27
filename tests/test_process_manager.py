import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_telegram_bot.execution.process_manager import ProcessManager
from codex_telegram_bot.persistence.sqlite_store import SqliteRunStore
from codex_telegram_bot.tools.base import ToolContext, ToolRequest
from codex_telegram_bot.tools.shell import ShellExecTool


class TestProcessManager(unittest.TestCase):
    def _make_store(self, tmpdir: str) -> SqliteRunStore:
        return SqliteRunStore(Path(tmpdir) / "state.db")

    def _wait_for(self, predicate, timeout_sec: float = 3.0, step_sec: float = 0.05):
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(step_sec)
        return None

    def test_pty_start_write_poll_terminate(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._make_store(tmp)
            mgr = ProcessManager(run_store=store)
            ws = Path(tmp) / "ws"
            ws.mkdir(parents=True, exist_ok=True)

            cmd = (
                "python3 -u -c \"import sys,time;"
                "print('READY', flush=True);"
                "line=sys.stdin.readline().strip();"
                "print('ECHO:'+line, flush=True);"
                "time.sleep(30)\""
            )
            started = mgr.start_session(
                chat_id=10,
                user_id=20,
                cmd=cmd,
                workspace_root=ws,
                policy_profile="trusted",
                pty=True,
            )
            self.assertTrue(started["ok"])
            sid = started["session_id"]

            ready = self._wait_for(lambda: "READY" in mgr.poll_session(sid)["output"], timeout_sec=4.0)
            self.assertTrue(ready)

            wrote = mgr.write_session(process_session_id=sid, stdin_text="hello\n", cursor=None)
            if "ECHO:hello" not in str(wrote.get("output") or ""):
                echoed = self._wait_for(lambda: "ECHO:hello" in mgr.poll_session(sid)["output"], timeout_sec=4.0)
                self.assertTrue(echoed)

            terminated = mgr.terminate_session(process_session_id=sid, mode="interrupt")
            self.assertTrue(terminated["ok"])
            status = mgr.status(sid)
            self.assertTrue(status["ok"])
            self.assertIn(status["status"], {"terminated", "failed", "completed"})

    def test_short_mode_is_backward_compatible(self):
        with tempfile.TemporaryDirectory() as tmp:
            tool = ShellExecTool(process_manager=ProcessManager())
            result = tool.run(
                ToolRequest(name="shell_exec", args={"cmd": "echo hello"}),
                ToolContext(workspace_root=Path(tmp)),
            )
            self.assertTrue(result.ok)
            self.assertIn("hello", result.output)

    def test_idle_cleanup_terminates_session(self):
        with patch.dict(os.environ, {"IDLE_TIMEOUT_SEC": "1"}, clear=False):
            with tempfile.TemporaryDirectory() as tmp:
                store = self._make_store(tmp)
                mgr = ProcessManager(run_store=store)
                ws = Path(tmp) / "idle"
                ws.mkdir(parents=True, exist_ok=True)

                started = mgr.start_session(
                    chat_id=1,
                    user_id=1,
                    cmd="python3 -u -c \"import time; time.sleep(30)\"",
                    workspace_root=ws,
                    policy_profile="trusted",
                    pty=False,
                )
                self.assertTrue(started["ok"])
                sid = started["session_id"]

                time.sleep(1.2)
                cleaned = mgr.cleanup_sessions()
                self.assertGreaterEqual(cleaned, 1)
                status = mgr.status(sid)
                self.assertTrue(status["ok"])
                self.assertEqual(status["status"], "terminated")

    def test_output_cap_forces_termination(self):
        with patch.dict(os.environ, {"MAX_OUTPUT_BYTES": "800"}, clear=False):
            with tempfile.TemporaryDirectory() as tmp:
                store = self._make_store(tmp)
                mgr = ProcessManager(run_store=store)
                ws = Path(tmp) / "cap"
                ws.mkdir(parents=True, exist_ok=True)

                started = mgr.start_session(
                    chat_id=2,
                    user_id=2,
                    cmd=(
                        "python3 -u -c \"import time,sys; "
                        "exec('while True:\\n print(\\\"x\\\"*200, flush=True); time.sleep(0.01)')\""
                    ),
                    workspace_root=ws,
                    policy_profile="trusted",
                    pty=False,
                )
                self.assertTrue(started["ok"])
                sid = started["session_id"]

                capped = self._wait_for(
                    lambda: (
                        state
                        if state.get("status") != "running" and int(state.get("output_bytes") or 0) >= 800
                        else None
                    )
                    if (state := mgr.status(sid))
                    else None,
                    timeout_sec=6.0,
                    step_sec=0.1,
                )
                self.assertIsNotNone(capped)
                self.assertIn(capped["status"], {"terminated", "failed", "completed"})

    def test_workspace_and_allowlist_guards(self):
        with tempfile.TemporaryDirectory() as tmp:
            tool = ShellExecTool(process_manager=ProcessManager())

            blocked_bin = tool.run(
                ToolRequest(name="shell_exec", args={"cmd": "curl https://example.com"}),
                ToolContext(workspace_root=Path(tmp)),
            )
            self.assertFalse(blocked_bin.ok)
            self.assertIn("allowlist", blocked_bin.output)

            blocked_scope = tool.run(
                ToolRequest(name="shell_exec", args={"cmd": "find / -maxdepth 1"}),
                ToolContext(workspace_root=Path(tmp)),
            )
            self.assertFalse(blocked_scope.ok)
            self.assertIn("unsafe search scope", blocked_scope.output)

    def test_redaction_applies_to_poll_and_logs(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._make_store(tmp)
            mgr = ProcessManager(run_store=store)
            ws = Path(tmp) / "redact"
            ws.mkdir(parents=True, exist_ok=True)

            secret = "sk-1234567890ABCDEFGHIJK"
            started = mgr.start_session(
                chat_id=5,
                user_id=5,
                cmd=(
                    "python3 -u -c \"import time;"
                    f"print('token={secret}', flush=True);"
                    "time.sleep(30)\""
                ),
                workspace_root=ws,
                policy_profile="trusted",
                pty=False,
            )
            self.assertTrue(started["ok"])
            sid = started["session_id"]

            polled = self._wait_for(
                lambda: (
                    item
                    if "REDACTED" in str(item.get("output") or "")
                    else None
                )
                if (item := mgr.poll_session(sid))
                else None,
                timeout_sec=3.0,
            )
            self.assertIsNotNone(polled)
            self.assertNotIn(secret, polled["output"])
            self.assertIn("REDACTED", polled["output"])

            row = store.get_process_session(sid)
            self.assertIsNotNone(row)
            log_text = (ws / ".runs" / f"{sid}.log").read_text(encoding="utf-8")
            self.assertNotIn(secret, log_text)
            self.assertIn("REDACTED", log_text)

            mgr.terminate_session(sid, mode="kill")

    def test_running_sessions_are_marked_orphaned_on_startup(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "state.db"
            store = SqliteRunStore(db)
            store.create_process_session(
                process_session_id="proc-test",
                chat_id=1,
                user_id=2,
                argv=["python3", "-V"],
                workspace_root=str(tmp),
                pty_enabled=True,
                status="running",
                exit_code=None,
                created_at="2026-01-01T00:00:00+00:00",
                started_at="2026-01-01T00:00:00+00:00",
                completed_at=None,
                last_activity_at="2026-01-01T00:00:00+00:00",
                max_wall_sec=100,
                idle_timeout_sec=100,
                max_output_bytes=1000,
                ring_buffer_bytes=1000,
                output_bytes=0,
                redaction_replacements=0,
                log_path=str(Path(tmp) / ".runs" / "proc-test.log"),
                index_path=str(Path(tmp) / ".runs" / "proc-test.chunks.jsonl"),
                last_cursor=0,
                error="",
            )

            store2 = SqliteRunStore(db)
            row = store2.get_process_session("proc-test")
            self.assertIsNotNone(row)
            self.assertEqual(row["status"], "orphaned")


if __name__ == "__main__":
    unittest.main()
