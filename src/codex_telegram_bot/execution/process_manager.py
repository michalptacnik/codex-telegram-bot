from __future__ import annotations

import shlex
import signal
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from codex_telegram_bot.execution.log_index import INDEX_STRIDE_BYTES, SessionLogIndexer, search_log_file
from codex_telegram_bot.execution.policy import ExecutionPolicyEngine
from codex_telegram_bot.util import redact_with_audit

from .pty_spawn import SpawnedProcess, spawn_process

if TYPE_CHECKING:
    from codex_telegram_bot.persistence.sqlite_store import SqliteRunStore


MAX_WALL_SEC = 21_600
IDLE_TIMEOUT_SEC = 1_200
MAX_OUTPUT_BYTES = 5 * 1024 * 1024
RING_BUFFER_BYTES = 65_536
MAX_SESSIONS_PER_USER = 3
POLL_READ_BYTES = 12_000
TERMINATE_GRACE_SEC = 2


@dataclass
class _SessionRuntime:
    process_session_id: str
    chat_id: int
    user_id: int
    argv: List[str]
    workspace_root: Path
    pty_enabled: bool
    status: str
    created_at: str
    started_at: str
    last_activity_at: str
    max_wall_sec: int
    idle_timeout_sec: int
    max_output_bytes: int
    ring_buffer_bytes: int
    log_path: Path
    index_path: Path
    output_bytes: int = 0
    redaction_replacements: int = 0
    last_cursor: int = 0
    exit_code: Optional[int] = None
    error: str = ""
    created_monotonic: float = field(default_factory=time.monotonic)
    last_activity_monotonic: float = field(default_factory=time.monotonic)
    ring_buffer: bytearray = field(default_factory=bytearray)
    process: Optional[SpawnedProcess] = None
    indexer: Optional[SessionLogIndexer] = None


class ProcessManager:
    """Persistent process/session lifecycle manager for shell execution."""

    def __init__(
        self,
        run_store: Optional["SqliteRunStore"] = None,
        policy_engine: Optional[ExecutionPolicyEngine] = None,
    ) -> None:
        self._run_store = run_store
        self._policy_engine = policy_engine or ExecutionPolicyEngine()
        self._lock = threading.RLock()
        self._sessions: Dict[str, _SessionRuntime] = {}
        self._max_wall_sec = _env_int("MAX_WALL_SEC", MAX_WALL_SEC)
        self._idle_timeout_sec = _env_int("IDLE_TIMEOUT_SEC", IDLE_TIMEOUT_SEC)
        self._max_output_bytes = _env_int("MAX_OUTPUT_BYTES", MAX_OUTPUT_BYTES)
        self._ring_buffer_bytes = _env_int("RING_BUFFER_BYTES", RING_BUFFER_BYTES)
        self._max_sessions_per_user = _env_int("MAX_SESSIONS_PER_USER", MAX_SESSIONS_PER_USER)
        self._terminate_grace_sec = max(1, _env_int("PROCESS_TERMINATE_GRACE_SEC", TERMINATE_GRACE_SEC))

    def run_short_command(
        self,
        cmd: str,
        workspace_root: Path,
        timeout_sec: int,
    ) -> Dict[str, Any]:
        """Backward-compatible short command execution (non-session)."""
        try:
            argv = shlex.split(str(cmd or ""))
        except ValueError as exc:
            return {"ok": False, "returncode": 2, "output": f"Error: failed to parse command: {exc}"}

        if not argv:
            return {"ok": False, "returncode": 2, "output": "Error: empty command after parsing."}

        root = self._resolve_workspace_root(workspace_root)
        timeout_sec = max(1, int(timeout_sec or 1))
        try:
            proc = subprocess.run(
                argv,
                cwd=str(root),
                capture_output=True,
                text=True,
                shell=False,
                check=False,
                timeout=timeout_sec,
                start_new_session=True,
            )
        except subprocess.TimeoutExpired:
            return {"ok": False, "returncode": 124, "output": f"Error: command timed out after {timeout_sec}s."}
        except FileNotFoundError:
            return {"ok": False, "returncode": 127, "output": f"Error: command not found: {argv[0]}"}
        except Exception as exc:
            return {"ok": False, "returncode": 1, "output": f"Error: execution failed: {exc}"}

        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
        merged = stdout
        if stderr:
            merged = f"{stdout}\nstderr:\n{stderr}".strip()
        redacted = redact_with_audit(merged)
        return {
            "ok": proc.returncode == 0,
            "returncode": int(proc.returncode or 0),
            "output": redacted.text,
            "redaction_replacements": int(redacted.replacements),
        }

    def start_session(
        self,
        *,
        chat_id: int,
        user_id: int,
        cmd: str,
        workspace_root: Path,
        policy_profile: str,
        pty: bool = True,
    ) -> Dict[str, Any]:
        if not str(cmd or "").strip():
            return {"ok": False, "error": "Error: 'cmd' is required for action=start."}
        try:
            argv = shlex.split(str(cmd or ""))
        except ValueError as exc:
            return {"ok": False, "error": f"Error: failed to parse command: {exc}"}
        if not argv:
            return {"ok": False, "error": "Error: empty command after parsing."}

        root = self._resolve_workspace_root(workspace_root)
        policy = self._policy_engine.evaluate(argv=argv, policy_profile=policy_profile)
        if not policy.allowed:
            return {"ok": False, "error": f"Blocked by execution policy: {policy.reason}"}

        active_count = self._count_active_sessions(chat_id=chat_id, user_id=user_id)
        if active_count >= self._max_sessions_per_user:
            return {
                "ok": False,
                "error": (
                    f"Error: max concurrent sessions reached ({self._max_sessions_per_user}). "
                    "Terminate one session first."
                ),
            }

        process_session_id = "proc-" + uuid.uuid4().hex[:16]
        runs_dir = self._runs_dir(root)
        log_path = (runs_dir / f"{process_session_id}.log").resolve()
        index_path = (runs_dir / f"{process_session_id}.chunks.jsonl").resolve()
        if not log_path.is_relative_to(root) or not index_path.is_relative_to(root):
            return {"ok": False, "error": "Error: invalid log path outside workspace root."}

        now_iso = _utc_now()
        runtime = _SessionRuntime(
            process_session_id=process_session_id,
            chat_id=int(chat_id or 0),
            user_id=int(user_id or 0),
            argv=list(argv),
            workspace_root=root,
            pty_enabled=bool(pty),
            status="running",
            created_at=now_iso,
            started_at=now_iso,
            last_activity_at=now_iso,
            max_wall_sec=self._max_wall_sec,
            idle_timeout_sec=self._idle_timeout_sec,
            max_output_bytes=self._max_output_bytes,
            ring_buffer_bytes=self._ring_buffer_bytes,
            log_path=log_path,
            index_path=index_path,
        )
        runtime.indexer = SessionLogIndexer(
            process_session_id=process_session_id,
            log_path=log_path,
            chunks_path=index_path,
            stride_bytes=INDEX_STRIDE_BYTES,
        )
        runtime.indexer.initialize()

        with self._lock:
            self._sessions[process_session_id] = runtime
            self._persist_start(runtime)

        def _on_output(data: bytes) -> None:
            self._handle_output(process_session_id, data)

        try:
            spawned = spawn_process(
                argv=argv,
                cwd=root,
                pty_enabled=bool(pty),
                output_cb=_on_output,
            )
        except FileNotFoundError:
            with self._lock:
                self._sessions.pop(process_session_id, None)
                if self._run_store is not None:
                    self._run_store.update_process_session(
                        process_session_id=process_session_id,
                        status="failed",
                        error=f"command not found: {argv[0]}",
                        completed_at=_utc_now(),
                    )
            return {"ok": False, "error": f"Error: command not found: {argv[0]}"}
        except Exception as exc:
            with self._lock:
                self._sessions.pop(process_session_id, None)
                if self._run_store is not None:
                    self._run_store.update_process_session(
                        process_session_id=process_session_id,
                        status="failed",
                        error=f"failed to start session: {exc}",
                        completed_at=_utc_now(),
                    )
            return {"ok": False, "error": f"Error: failed to start session: {exc}"}

        with self._lock:
            current = self._sessions.get(process_session_id)
            if current is None:
                return {"ok": False, "error": "Error: session was removed during startup."}
            current.process = spawned
            current.pty_enabled = bool(spawned.pty_enabled)
            self._persist_runtime(current)

        return {
            "ok": True,
            "process_session_id": process_session_id,
            "session_id": process_session_id,
            "status": "running",
            "cursor": 0,
            "pty": runtime.pty_enabled,
        }

    def poll_session(
        self,
        process_session_id: str,
        cursor: Optional[int] = None,
        max_bytes: int = POLL_READ_BYTES,
    ) -> Dict[str, Any]:
        with self._lock:
            runtime = self._sessions.get(process_session_id)

        self._finalize_if_exited(process_session_id)
        row = self._get_session_row(process_session_id)
        if not row and not runtime:
            return {"ok": False, "error": "Error: session not found."}

        row = row or self._runtime_to_row(runtime)  # type: ignore[arg-type]
        log_path = Path(str(row.get("log_path") or "")).expanduser().resolve()
        if not log_path.exists():
            text = ""
            cursor_start = int(cursor if cursor is not None else row.get("last_cursor") or 0)
            cursor_next = cursor_start
        else:
            cursor_start = int(cursor if cursor is not None else row.get("last_cursor") or 0)
            cursor_start = max(0, cursor_start)
            blob = b""
            with log_path.open("rb") as fh:
                fh.seek(cursor_start)
                blob = fh.read(max(1, int(max_bytes)))
            text = blob.decode("utf-8", errors="replace")
            cursor_next = cursor_start + len(blob)

        if self._run_store is not None:
            self._run_store.set_process_session_last_cursor(process_session_id, cursor_next)

        return {
            "ok": True,
            "session_id": process_session_id,
            "status": row.get("status", "unknown"),
            "exit_code": row.get("exit_code"),
            "cursor": cursor_start,
            "cursor_next": cursor_next,
            "output": text,
            "log_path": str(log_path),
        }

    def write_session(
        self,
        process_session_id: str,
        stdin_text: str,
        cursor: Optional[int] = None,
    ) -> Dict[str, Any]:
        with self._lock:
            runtime = self._sessions.get(process_session_id)
        if runtime is None or runtime.process is None:
            return {"ok": False, "error": "Error: session is not running."}

        if runtime.status != "running":
            return {"ok": False, "error": f"Error: session status is {runtime.status}."}

        try:
            runtime.process.write_stdin(stdin_text)
        except Exception as exc:
            return {"ok": False, "error": f"Error: stdin write failed: {exc}"}

        now = _utc_now()
        with self._lock:
            runtime.last_activity_at = now
            runtime.last_activity_monotonic = time.monotonic()
            self._persist_runtime(runtime)

        time.sleep(0.05)
        return self.poll_session(process_session_id=process_session_id, cursor=cursor)

    def terminate_session(self, process_session_id: str, mode: str = "interrupt") -> Dict[str, Any]:
        normalized = (mode or "interrupt").strip().lower()
        if normalized not in {"interrupt", "kill"}:
            normalized = "interrupt"

        with self._lock:
            runtime = self._sessions.get(process_session_id)
        if runtime is None or runtime.process is None:
            row = self._get_session_row(process_session_id)
            if row:
                return {
                    "ok": True,
                    "session_id": process_session_id,
                    "status": row.get("status", "unknown"),
                    "exit_code": row.get("exit_code"),
                }
            return {"ok": False, "error": "Error: session not found."}

        if normalized == "interrupt":
            runtime.process.interrupt()
        else:
            runtime.process.terminate()

        deadline = time.monotonic() + self._terminate_grace_sec
        while time.monotonic() < deadline:
            if runtime.process.poll() is not None:
                break
            time.sleep(0.05)

        if runtime.process.poll() is None:
            runtime.process.kill()

        self._finalize_runtime(
            process_session_id=process_session_id,
            forced_status="terminated",
            forced_error=f"terminated via {normalized}",
        )
        row = self._get_session_row(process_session_id) or {}
        return {
            "ok": True,
            "session_id": process_session_id,
            "status": row.get("status", "terminated"),
            "exit_code": row.get("exit_code"),
        }

    def status(self, process_session_id: str) -> Dict[str, Any]:
        self._finalize_if_exited(process_session_id)
        row = self._get_session_row(process_session_id)
        if not row:
            return {"ok": False, "error": "Error: session not found."}
        now = datetime.now(timezone.utc)
        created = _parse_iso(row.get("created_at"))
        last = _parse_iso(row.get("last_activity_at"))
        age_sec = int((now - created).total_seconds()) if created else 0
        idle_sec = int((now - last).total_seconds()) if last else 0
        return {
            "ok": True,
            "session_id": process_session_id,
            "status": row.get("status"),
            "exit_code": row.get("exit_code"),
            "age_sec": age_sec,
            "idle_sec": idle_sec,
            "output_bytes": int(row.get("output_bytes") or 0),
            "pty": bool(row.get("pty_enabled")),
            "cmd": " ".join(row.get("argv") or []),
        }

    def list_sessions(self, chat_id: int, user_id: int, limit: int = 20) -> List[Dict[str, Any]]:
        if self._run_store is not None:
            return self._run_store.list_process_sessions(chat_id=chat_id, user_id=user_id, limit=limit)

        with self._lock:
            rows = [
                self._runtime_to_row(s)
                for s in self._sessions.values()
                if s.chat_id == int(chat_id or 0) and s.user_id == int(user_id or 0)
            ]
        rows.sort(key=lambda r: str(r.get("last_activity_at") or ""), reverse=True)
        return rows[: max(1, int(limit))]

    def get_active_session(self, chat_id: int, user_id: int) -> Optional[Dict[str, Any]]:
        rows = self.list_sessions(chat_id=chat_id, user_id=user_id, limit=50)
        for row in rows:
            if row.get("status") == "running":
                return row
        return None

    def search_log(
        self,
        process_session_id: str,
        query: str,
        max_results: int = 5,
        context_lines: int = 2,
        cursor: int = 0,
    ) -> Dict[str, Any]:
        row = self._get_session_row(process_session_id)
        if not row:
            return {"ok": False, "error": "Error: session not found."}
        log_path = Path(str(row.get("log_path") or "")).expanduser().resolve()
        matches = search_log_file(
            log_path=log_path,
            query=query,
            max_results=max_results,
            context_lines=context_lines,
            min_offset=max(0, int(cursor or 0)),
        )
        cursor_next = int(cursor or 0)
        if matches:
            cursor_next = max(int(m.get("offset") or 0) for m in matches)
        return {
            "ok": True,
            "session_id": process_session_id,
            "query": query,
            "matches": matches,
            "cursor": int(cursor or 0),
            "cursor_next": cursor_next,
            "log_path": str(log_path),
        }

    def cleanup_sessions(self) -> int:
        now_mono = time.monotonic()
        candidates: List[str] = []
        with self._lock:
            for sid, runtime in self._sessions.items():
                if runtime.status != "running":
                    candidates.append(sid)
                    continue
                if runtime.process and runtime.process.poll() is not None:
                    candidates.append(sid)
                    continue
                if now_mono - runtime.created_monotonic > runtime.max_wall_sec:
                    candidates.append(sid)
                    continue
                if now_mono - runtime.last_activity_monotonic > runtime.idle_timeout_sec:
                    candidates.append(sid)
                    continue
                if runtime.output_bytes >= runtime.max_output_bytes:
                    candidates.append(sid)

        cleaned = 0
        for sid in candidates:
            with self._lock:
                runtime = self._sessions.get(sid)
            if runtime is None:
                continue
            if runtime.process and runtime.process.poll() is None:
                if now_mono - runtime.created_monotonic > runtime.max_wall_sec:
                    runtime.process.terminate()
                    time.sleep(0.1)
                    if runtime.process.poll() is None:
                        runtime.process.kill()
                    self._finalize_runtime(sid, forced_status="terminated", forced_error="max wall time exceeded")
                    cleaned += 1
                    continue
                if now_mono - runtime.last_activity_monotonic > runtime.idle_timeout_sec:
                    runtime.process.interrupt()
                    time.sleep(0.1)
                    if runtime.process.poll() is None:
                        runtime.process.kill()
                    self._finalize_runtime(sid, forced_status="terminated", forced_error="idle timeout exceeded")
                    cleaned += 1
                    continue
                if runtime.output_bytes >= runtime.max_output_bytes:
                    runtime.process.kill()
                    self._finalize_runtime(sid, forced_status="terminated", forced_error="max output bytes exceeded")
                    cleaned += 1
                    continue
            self._finalize_if_exited(sid)
            cleaned += 1
        return cleaned

    def _count_active_sessions(self, chat_id: int, user_id: int) -> int:
        chat_id = int(chat_id or 0)
        user_id = int(user_id or 0)
        with self._lock:
            in_memory = sum(
                1
                for s in self._sessions.values()
                if s.chat_id == chat_id and s.user_id == user_id and s.status == "running"
            )
        if self._run_store is None:
            return in_memory
        persisted = int(self._run_store.count_running_process_sessions(chat_id=chat_id, user_id=user_id) or 0)
        return max(in_memory, persisted)

    def _handle_output(self, process_session_id: str, data: bytes) -> None:
        text = (data or b"").decode("utf-8", errors="replace")
        if not text:
            return

        should_terminate = False
        with self._lock:
            runtime = self._sessions.get(process_session_id)
            if runtime is None or runtime.indexer is None:
                return
            redacted = redact_with_audit(text)
            written, chunks = runtime.indexer.append_text(redacted.text)
            runtime.output_bytes += int(written)
            runtime.redaction_replacements += int(redacted.replacements)
            runtime.last_activity_at = _utc_now()
            runtime.last_activity_monotonic = time.monotonic()

            payload = redacted.text.encode("utf-8", errors="replace")
            runtime.ring_buffer.extend(payload)
            if len(runtime.ring_buffer) > runtime.ring_buffer_bytes:
                del runtime.ring_buffer[: len(runtime.ring_buffer) - runtime.ring_buffer_bytes]

            self._persist_runtime(runtime)
            if self._run_store is not None:
                for item in chunks:
                    self._run_store.append_process_session_chunk(
                        process_session_id=process_session_id,
                        seq=item.seq,
                        created_at=item.created_at,
                        start_offset=item.start_offset,
                        end_offset=item.end_offset,
                        preview=item.preview,
                    )
            if runtime.output_bytes >= runtime.max_output_bytes:
                should_terminate = True

        if should_terminate:
            self.terminate_session(process_session_id=process_session_id, mode="kill")

    def _finalize_if_exited(self, process_session_id: str) -> None:
        with self._lock:
            runtime = self._sessions.get(process_session_id)
            if runtime is None or runtime.process is None:
                return
            if runtime.process.poll() is None:
                return
        self._finalize_runtime(process_session_id)

    def _finalize_runtime(
        self,
        process_session_id: str,
        forced_status: Optional[str] = None,
        forced_error: str = "",
    ) -> None:
        with self._lock:
            runtime = self._sessions.get(process_session_id)
            if runtime is None:
                return
            exit_code = runtime.process.poll() if runtime.process else runtime.exit_code
            runtime.exit_code = exit_code

            if forced_status:
                runtime.status = forced_status
            elif exit_code is None:
                runtime.status = runtime.status or "terminated"
            elif int(exit_code) == 0:
                runtime.status = "completed"
            else:
                runtime.status = "failed"

            runtime.error = forced_error or runtime.error
            runtime.last_activity_at = _utc_now()
            self._persist_runtime(runtime, completed=True)

            if runtime.process is not None:
                runtime.process.close()
            self._sessions.pop(process_session_id, None)

    def _persist_start(self, runtime: _SessionRuntime) -> None:
        if self._run_store is None:
            return
        self._run_store.create_process_session(
            process_session_id=runtime.process_session_id,
            chat_id=runtime.chat_id,
            user_id=runtime.user_id,
            argv=runtime.argv,
            workspace_root=str(runtime.workspace_root),
            pty_enabled=runtime.pty_enabled,
            status=runtime.status,
            exit_code=runtime.exit_code,
            created_at=runtime.created_at,
            started_at=runtime.started_at,
            completed_at=None,
            last_activity_at=runtime.last_activity_at,
            max_wall_sec=runtime.max_wall_sec,
            idle_timeout_sec=runtime.idle_timeout_sec,
            max_output_bytes=runtime.max_output_bytes,
            ring_buffer_bytes=runtime.ring_buffer_bytes,
            output_bytes=runtime.output_bytes,
            redaction_replacements=runtime.redaction_replacements,
            log_path=str(runtime.log_path),
            index_path=str(runtime.index_path),
            last_cursor=runtime.last_cursor,
            error=runtime.error,
        )

    def _persist_runtime(self, runtime: _SessionRuntime, completed: bool = False) -> None:
        if self._run_store is None:
            return
        self._run_store.update_process_session(
            process_session_id=runtime.process_session_id,
            status=runtime.status,
            exit_code=runtime.exit_code,
            completed_at=(_utc_now() if completed else None),
            last_activity_at=runtime.last_activity_at,
            output_bytes=runtime.output_bytes,
            redaction_replacements=runtime.redaction_replacements,
            last_cursor=runtime.last_cursor,
            pty_enabled=runtime.pty_enabled,
            error=runtime.error,
        )

    def _get_session_row(self, process_session_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            runtime = self._sessions.get(process_session_id)
            if runtime is not None:
                return self._runtime_to_row(runtime)
        if self._run_store is None:
            return None
        return self._run_store.get_process_session(process_session_id)

    def _runtime_to_row(self, runtime: _SessionRuntime) -> Dict[str, Any]:
        return {
            "process_session_id": runtime.process_session_id,
            "chat_id": runtime.chat_id,
            "user_id": runtime.user_id,
            "argv": list(runtime.argv),
            "workspace_root": str(runtime.workspace_root),
            "pty_enabled": 1 if runtime.pty_enabled else 0,
            "status": runtime.status,
            "exit_code": runtime.exit_code,
            "created_at": runtime.created_at,
            "started_at": runtime.started_at,
            "completed_at": None,
            "last_activity_at": runtime.last_activity_at,
            "max_wall_sec": runtime.max_wall_sec,
            "idle_timeout_sec": runtime.idle_timeout_sec,
            "max_output_bytes": runtime.max_output_bytes,
            "ring_buffer_bytes": runtime.ring_buffer_bytes,
            "output_bytes": runtime.output_bytes,
            "redaction_replacements": runtime.redaction_replacements,
            "log_path": str(runtime.log_path),
            "index_path": str(runtime.index_path),
            "last_cursor": runtime.last_cursor,
            "error": runtime.error,
        }

    def _resolve_workspace_root(self, workspace_root: Path) -> Path:
        root = Path(workspace_root).expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        if not root.is_dir():
            raise ValueError(f"Workspace root is not a directory: {root}")
        return root

    def _runs_dir(self, workspace_root: Path) -> Path:
        runs = (workspace_root / ".runs").resolve()
        if not runs.is_relative_to(workspace_root):
            raise ValueError("Runs directory escapes workspace root.")
        runs.mkdir(parents=True, exist_ok=True)
        return runs


def _env_int(name: str, default: int) -> int:
    import os

    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return int(default)
    try:
        value = int(raw)
    except ValueError:
        return int(default)
    return max(1, value)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(raw: Any) -> Optional[datetime]:
    value = str(raw or "").strip()
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None
