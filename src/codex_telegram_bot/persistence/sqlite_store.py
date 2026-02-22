import sqlite3
import uuid
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from codex_telegram_bot.domain.runs import (
    RUN_STATUS_COMPLETED,
    RUN_STATUS_FAILED,
    RUN_STATUS_PENDING,
    RUN_STATUS_RUNNING,
    RunRecord,
)
from codex_telegram_bot.domain.agents import AgentRecord
from codex_telegram_bot.domain.sessions import (
    SESSION_STATUS_ACTIVE,
    SESSION_STATUS_ARCHIVED,
    TelegramSessionMessageRecord,
    TelegramSessionRecord,
)
from codex_telegram_bot.events.event_bus import RunEvent
from codex_telegram_bot.util import redact_with_audit


class SqliteRunStore:
    def __init__(self, db_path: Path):
        self._db_path = Path(db_path).expanduser().resolve()
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    prompt TEXT NOT NULL,
                    output TEXT NOT NULL,
                    error TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    completed_at TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS run_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS agents (
                    agent_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    policy_profile TEXT NOT NULL,
                    max_concurrency INTEGER NOT NULL DEFAULT 1,
                    enabled INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS telegram_sessions (
                    session_id TEXT PRIMARY KEY,
                    chat_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    current_agent_id TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    last_run_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS telegram_session_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS tool_approvals (
                    approval_id TEXT PRIMARY KEY,
                    chat_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    session_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    run_id TEXT NOT NULL DEFAULT '',
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
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS tool_loop_checkpoints (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    prompt_fingerprint TEXT NOT NULL,
                    step_index INTEGER NOT NULL,
                    command TEXT NOT NULL,
                    status TEXT NOT NULL,
                    run_id TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_tool_loop_checkpoint_key
                ON tool_loop_checkpoints (session_id, prompt_fingerprint, step_index)
                """
            )
            # Lightweight migration for existing DBs created before max_concurrency existed.
            cols = conn.execute("PRAGMA table_info(agents)").fetchall()
            col_names = {c["name"] for c in cols}
            if "max_concurrency" not in col_names:
                conn.execute("ALTER TABLE agents ADD COLUMN max_concurrency INTEGER NOT NULL DEFAULT 1")
            tool_cols = conn.execute("PRAGMA table_info(tool_approvals)").fetchall()
            tool_col_names = {c["name"] for c in tool_cols}
            if tool_cols and "run_id" not in tool_col_names:
                conn.execute("ALTER TABLE tool_approvals ADD COLUMN run_id TEXT NOT NULL DEFAULT ''")
            existing = conn.execute("SELECT COUNT(*) AS c FROM agents").fetchone()["c"]
            if existing == 0:
                now = _utc_now()
                conn.execute(
                    """
                    INSERT INTO agents (agent_id, name, provider, policy_profile, max_concurrency, enabled, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    ("default", "Default Agent", "codex_cli", "balanced", 1, 1, now, now),
                )

    def create_run(self, prompt: str) -> str:
        run_id = str(uuid.uuid4())
        now = _utc_now()
        redacted_prompt = redact_with_audit(prompt)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO runs (run_id, status, prompt, output, error, created_at, started_at, completed_at)
                VALUES (?, ?, ?, ?, ?, ?, NULL, NULL)
                """,
                (run_id, RUN_STATUS_PENDING, redacted_prompt.text, "", "", now),
            )
            if redacted_prompt.redacted:
                _append_redaction_audit(
                    conn=conn,
                    run_id=run_id,
                    context="run.prompt",
                    replacements=redacted_prompt.replacements,
                )
        return run_id

    def mark_running(self, run_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE runs SET status = ?, started_at = ? WHERE run_id = ?",
                (RUN_STATUS_RUNNING, _utc_now(), run_id),
            )

    def mark_completed(self, run_id: str, output: str) -> None:
        redacted_output = redact_with_audit(output)
        with self._connect() as conn:
            conn.execute(
                "UPDATE runs SET status = ?, output = ?, completed_at = ? WHERE run_id = ?",
                (RUN_STATUS_COMPLETED, redacted_output.text, _utc_now(), run_id),
            )
            if redacted_output.redacted:
                _append_redaction_audit(
                    conn=conn,
                    run_id=run_id,
                    context="run.output",
                    replacements=redacted_output.replacements,
                )

    def mark_failed(self, run_id: str, error: str) -> None:
        redacted_error = redact_with_audit(error)
        with self._connect() as conn:
            conn.execute(
                "UPDATE runs SET status = ?, error = ?, completed_at = ? WHERE run_id = ?",
                (RUN_STATUS_FAILED, redacted_error.text, _utc_now(), run_id),
            )
            if redacted_error.redacted:
                _append_redaction_audit(
                    conn=conn,
                    run_id=run_id,
                    context="run.error",
                    replacements=redacted_error.replacements,
                )

    def append_event(self, event: RunEvent) -> None:
        redacted_payload = redact_with_audit(event.payload)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO run_events (run_id, event_type, payload, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    event.run_id,
                    event.event_type,
                    redacted_payload.text,
                    event.created_at.isoformat(),
                ),
            )
            if redacted_payload.redacted and event.event_type != "security.redaction.applied":
                _append_redaction_audit(
                    conn=conn,
                    run_id=event.run_id,
                    context=f"run.event.{event.event_type}",
                    replacements=redacted_payload.replacements,
                )

    def get_run(self, run_id: str) -> Optional[RunRecord]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if not row:
            return None
        return _row_to_record(row)

    def list_recent_runs(self, limit: int = 20) -> List[RunRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM runs ORDER BY created_at DESC LIMIT ?",
                (max(1, limit),),
            ).fetchall()
        return [_row_to_record(r) for r in rows]

    def list_run_events(self, run_id: str, limit: int = 200) -> List[RunEvent]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT run_id, event_type, payload, created_at
                FROM run_events
                WHERE run_id = ?
                ORDER BY id ASC
                LIMIT ?
                """,
                (run_id, max(1, limit)),
            ).fetchall()
        return [
            RunEvent(
                run_id=row["run_id"],
                event_type=row["event_type"],
                payload=row["payload"],
                created_at=datetime.fromisoformat(row["created_at"]),
            )
            for row in rows
        ]

    def list_agents(self) -> List[AgentRecord]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM agents ORDER BY created_at ASC").fetchall()
        return [_row_to_agent(r) for r in rows]

    def get_agent(self, agent_id: str) -> Optional[AgentRecord]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM agents WHERE agent_id = ?", (agent_id,)).fetchone()
        if not row:
            return None
        return _row_to_agent(row)

    def upsert_agent(
        self,
        agent_id: str,
        name: str,
        provider: str,
        policy_profile: str,
        max_concurrency: int,
        enabled: bool,
    ) -> AgentRecord:
        now = _utc_now()
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT agent_id, created_at FROM agents WHERE agent_id = ?",
                (agent_id,),
            ).fetchone()
            if existing:
                conn.execute(
                    """
                    UPDATE agents
                    SET name = ?, provider = ?, policy_profile = ?, max_concurrency = ?, enabled = ?, updated_at = ?
                    WHERE agent_id = ?
                    """,
                    (name, provider, policy_profile, max_concurrency, 1 if enabled else 0, now, agent_id),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO agents (agent_id, name, provider, policy_profile, max_concurrency, enabled, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (agent_id, name, provider, policy_profile, max_concurrency, 1 if enabled else 0, now, now),
                )
        return self.get_agent(agent_id)  # type: ignore[return-value]

    def delete_agent(self, agent_id: str) -> bool:
        if agent_id == "default":
            return False
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM agents WHERE agent_id = ?", (agent_id,))
            return cur.rowcount > 0

    def get_active_session(self, chat_id: int, user_id: int) -> Optional[TelegramSessionRecord]:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM telegram_sessions
                WHERE chat_id = ? AND user_id = ? AND status = ?
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                (chat_id, user_id, SESSION_STATUS_ACTIVE),
            ).fetchone()
        if not row:
            return None
        return _row_to_session(row)

    def create_session(self, chat_id: int, user_id: int, current_agent_id: str = "default") -> TelegramSessionRecord:
        now = _utc_now()
        session_id = str(uuid.uuid4())
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO telegram_sessions
                (session_id, chat_id, user_id, status, current_agent_id, summary, last_run_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (session_id, chat_id, user_id, SESSION_STATUS_ACTIVE, current_agent_id, "", "", now, now),
            )
        return self.get_session(session_id)  # type: ignore[return-value]

    def archive_active_sessions(self, chat_id: int, user_id: int) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                """
                UPDATE telegram_sessions
                SET status = ?, updated_at = ?
                WHERE chat_id = ? AND user_id = ? AND status = ?
                """,
                (SESSION_STATUS_ARCHIVED, _utc_now(), chat_id, user_id, SESSION_STATUS_ACTIVE),
            )
            return int(cur.rowcount or 0)

    def get_or_create_active_session(
        self, chat_id: int, user_id: int, current_agent_id: str = "default"
    ) -> TelegramSessionRecord:
        session = self.get_active_session(chat_id=chat_id, user_id=user_id)
        if session:
            return session
        return self.create_session(chat_id=chat_id, user_id=user_id, current_agent_id=current_agent_id)

    def get_session(self, session_id: str) -> Optional[TelegramSessionRecord]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM telegram_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        if not row:
            return None
        return _row_to_session(row)

    def list_recent_sessions(self, limit: int = 50) -> List[TelegramSessionRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM telegram_sessions
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (max(1, limit),),
            ).fetchall()
        return [_row_to_session(r) for r in rows]

    def list_sessions_for_chat_user(self, chat_id: int, user_id: int, limit: int = 50) -> List[TelegramSessionRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM telegram_sessions
                WHERE chat_id = ? AND user_id = ?
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (chat_id, user_id, max(1, limit)),
            ).fetchall()
        return [_row_to_session(r) for r in rows]

    def append_session_message(self, session_id: str, role: str, content: str, run_id: str = "") -> None:
        redacted = redact_with_audit(content)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO telegram_session_messages (session_id, role, content, run_id, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (session_id, role, redacted.text, run_id, _utc_now()),
            )
            conn.execute(
                "UPDATE telegram_sessions SET updated_at = ? WHERE session_id = ?",
                (_utc_now(), session_id),
            )
            if redacted.redacted:
                conn.execute(
                    """
                    INSERT INTO telegram_session_messages (session_id, role, content, run_id, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        session_id,
                        "system",
                        f"security.redaction.applied replacements={redacted.replacements}",
                        "",
                        _utc_now(),
                    ),
                )

    def list_session_messages(self, session_id: str, limit: int = 20) -> List[TelegramSessionMessageRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, session_id, role, content, run_id, created_at
                FROM telegram_session_messages
                WHERE session_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (session_id, max(1, limit)),
            ).fetchall()
        # Return chronological order.
        ordered = list(reversed(rows))
        return [
            TelegramSessionMessageRecord(
                id=row["id"],
                session_id=row["session_id"],
                role=row["role"],
                content=row["content"],
                run_id=row["run_id"],
                created_at=datetime.fromisoformat(row["created_at"]),
            )
            for row in ordered
        ]

    def set_session_last_run(self, session_id: str, run_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE telegram_sessions
                SET last_run_id = ?, updated_at = ?
                WHERE session_id = ?
                """,
                (run_id, _utc_now(), session_id),
            )

    def activate_session(self, chat_id: int, user_id: int, session_id: str) -> Optional[TelegramSessionRecord]:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM telegram_sessions
                WHERE session_id = ? AND chat_id = ? AND user_id = ?
                """,
                (session_id, chat_id, user_id),
            ).fetchone()
            if not row:
                return None
            conn.execute(
                """
                UPDATE telegram_sessions
                SET status = ?, updated_at = ?
                WHERE chat_id = ? AND user_id = ? AND status = ?
                """,
                (SESSION_STATUS_ARCHIVED, _utc_now(), chat_id, user_id, SESSION_STATUS_ACTIVE),
            )
            conn.execute(
                """
                UPDATE telegram_sessions
                SET status = ?, updated_at = ?
                WHERE session_id = ?
                """,
                (SESSION_STATUS_ACTIVE, _utc_now(), session_id),
            )
        return self.get_session(session_id=session_id)

    def create_branch_session(
        self,
        chat_id: int,
        user_id: int,
        from_session_id: str,
        copy_messages: int = 12,
    ) -> Optional[TelegramSessionRecord]:
        source = self.get_session(from_session_id)
        if not source:
            return None
        if source.chat_id != chat_id or source.user_id != user_id:
            return None
        self.archive_active_sessions(chat_id=chat_id, user_id=user_id)
        branched = self.create_session(chat_id=chat_id, user_id=user_id, current_agent_id=source.current_agent_id)
        inherited = self.list_session_messages(from_session_id, limit=max(1, copy_messages))
        if inherited:
            for msg in inherited:
                self.append_session_message(
                    session_id=branched.session_id,
                    role=msg.role,
                    content=msg.content,
                    run_id=msg.run_id,
                )
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE telegram_sessions
                SET summary = ?, updated_at = ?
                WHERE session_id = ?
                """,
                (f"Branched from {from_session_id[:8]}", _utc_now(), branched.session_id),
            )
        return self.get_session(branched.session_id)

    def compact_session_messages(self, session_id: str, max_messages: int, keep_recent: int) -> int:
        max_messages = max(1, max_messages)
        keep_recent = max(1, min(keep_recent, max_messages))
        with self._connect() as conn:
            total = conn.execute(
                "SELECT COUNT(*) AS c FROM telegram_session_messages WHERE session_id = ?",
                (session_id,),
            ).fetchone()["c"]
            if total <= max_messages:
                return 0
            remove_count = total - max_messages
            rows = conn.execute(
                """
                SELECT id, role, content
                FROM telegram_session_messages
                WHERE session_id = ?
                ORDER BY id ASC
                LIMIT ?
                """,
                (session_id, remove_count),
            ).fetchall()
            ids = [int(r["id"]) for r in rows]
            if not ids:
                return 0
            placeholders = ",".join(["?"] * len(ids))
            conn.execute(
                f"DELETE FROM telegram_session_messages WHERE id IN ({placeholders})",
                ids,
            )
            summary_bits = []
            for r in rows:
                role = r["role"]
                content = (r["content"] or "").replace("\n", " ").strip()
                if role not in {"user", "assistant"} or not content:
                    continue
                summary_bits.append(f"{role}:{content[:120]}")
                if len(summary_bits) >= 3:
                    break
            summary_text = " | ".join(summary_bits) if summary_bits else f"{remove_count} older messages compacted"
            conn.execute(
                """
                UPDATE telegram_sessions
                SET summary = ?, updated_at = ?
                WHERE session_id = ?
                """,
                (f"Compacted {remove_count} messages. {summary_text}", _utc_now(), session_id),
            )
            conn.execute(
                """
                INSERT INTO telegram_session_messages (session_id, role, content, run_id, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (session_id, "system", f"history.compacted removed={remove_count}", "", _utc_now()),
            )
            return remove_count

    def create_tool_approval(
        self,
        chat_id: int,
        user_id: int,
        session_id: str,
        agent_id: str,
        run_id: str,
        argv: list[str],
        stdin_text: str,
        timeout_sec: int,
        risk_tier: str,
    ) -> str:
        approval_id = str(uuid.uuid4())
        now = _utc_now()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO tool_approvals
                (approval_id, chat_id, user_id, session_id, agent_id, run_id, argv_json, stdin_text, timeout_sec, risk_tier, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    approval_id,
                    chat_id,
                    user_id,
                    session_id,
                    agent_id,
                    run_id,
                    json.dumps(argv),
                    stdin_text,
                    int(timeout_sec),
                    risk_tier,
                    "pending",
                    now,
                    now,
                ),
            )
        return approval_id

    def list_pending_tool_approvals(self, chat_id: int, user_id: int, limit: int = 20) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM tool_approvals
                WHERE chat_id = ? AND user_id = ? AND status = 'pending'
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (chat_id, user_id, max(1, limit)),
            ).fetchall()
        return [_row_to_tool_approval(r) for r in rows]

    def list_all_pending_tool_approvals(self, limit: int = 200) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM tool_approvals
                WHERE status = 'pending'
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (max(1, limit),),
            ).fetchall()
        return [_row_to_tool_approval(r) for r in rows]

    def get_tool_approval(self, approval_id: str) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM tool_approvals WHERE approval_id = ?",
                (approval_id,),
            ).fetchone()
        if not row:
            return None
        return _row_to_tool_approval(row)

    def set_tool_approval_status(self, approval_id: str, status: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE tool_approvals SET status = ?, updated_at = ? WHERE approval_id = ?",
                (status, _utc_now(), approval_id),
            )

    def expire_tool_approvals_before(self, cutoff_iso: str) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                """
                UPDATE tool_approvals
                SET status = 'expired', updated_at = ?
                WHERE status = 'pending' AND created_at < ?
                """,
                (_utc_now(), cutoff_iso),
            )
            return int(cur.rowcount or 0)

    def count_pending_tool_approvals(self, chat_id: int, user_id: int) -> int:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS c
                FROM tool_approvals
                WHERE chat_id = ? AND user_id = ? AND status = 'pending'
                """,
                (chat_id, user_id),
            ).fetchone()
        return int(row["c"] or 0)

    def find_pending_tool_approval(
        self,
        chat_id: int,
        user_id: int,
        session_id: str,
        argv: list[str],
    ) -> Optional[dict]:
        argv_json = json.dumps(argv)
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM tool_approvals
                WHERE chat_id = ? AND user_id = ? AND session_id = ? AND status = 'pending' AND argv_json = ?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (chat_id, user_id, session_id, argv_json),
            ).fetchone()
        if not row:
            return None
        return _row_to_tool_approval(row)

    def upsert_tool_loop_checkpoint(
        self,
        session_id: str,
        prompt_fingerprint: str,
        step_index: int,
        command: str,
        status: str,
        run_id: str = "",
    ) -> None:
        now = _utc_now()
        with self._connect() as conn:
            existing = conn.execute(
                """
                SELECT id FROM tool_loop_checkpoints
                WHERE session_id = ? AND prompt_fingerprint = ? AND step_index = ?
                """,
                (session_id, prompt_fingerprint, int(step_index)),
            ).fetchone()
            if existing:
                conn.execute(
                    """
                    UPDATE tool_loop_checkpoints
                    SET command = ?, status = ?, run_id = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (command, status, run_id, now, int(existing["id"])),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO tool_loop_checkpoints
                    (session_id, prompt_fingerprint, step_index, command, status, run_id, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (session_id, prompt_fingerprint, int(step_index), command, status, run_id, now, now),
                )

    def list_tool_loop_checkpoints(self, session_id: str, prompt_fingerprint: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM tool_loop_checkpoints
                WHERE session_id = ? AND prompt_fingerprint = ?
                ORDER BY step_index ASC
                """,
                (session_id, prompt_fingerprint),
            ).fetchall()
        return [
            {
                "step_index": int(r["step_index"]),
                "command": r["command"],
                "status": r["status"],
                "run_id": r["run_id"] or "",
                "updated_at": r["updated_at"],
            }
            for r in rows
        ]


def _parse_dt(raw: Optional[str]) -> Optional[datetime]:
    if not raw:
        return None
    return datetime.fromisoformat(raw)


def _row_to_record(row: sqlite3.Row) -> RunRecord:
    return RunRecord(
        run_id=row["run_id"],
        status=row["status"],
        prompt=row["prompt"],
        output=row["output"],
        error=row["error"],
        created_at=datetime.fromisoformat(row["created_at"]),
        started_at=_parse_dt(row["started_at"]),
        completed_at=_parse_dt(row["completed_at"]),
    )


def _row_to_agent(row: sqlite3.Row) -> AgentRecord:
    return AgentRecord(
        agent_id=row["agent_id"],
        name=row["name"],
        provider=row["provider"],
        policy_profile=row["policy_profile"],
        max_concurrency=int(row["max_concurrency"] or 1),
        enabled=bool(row["enabled"]),
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


def _row_to_session(row: sqlite3.Row) -> TelegramSessionRecord:
    return TelegramSessionRecord(
        session_id=row["session_id"],
        chat_id=int(row["chat_id"]),
        user_id=int(row["user_id"]),
        status=row["status"],
        current_agent_id=row["current_agent_id"],
        summary=row["summary"],
        last_run_id=row["last_run_id"],
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


def _row_to_tool_approval(row: sqlite3.Row) -> dict:
    return {
        "approval_id": row["approval_id"],
        "chat_id": int(row["chat_id"]),
        "user_id": int(row["user_id"]),
        "session_id": row["session_id"],
        "agent_id": row["agent_id"],
        "run_id": row["run_id"] or "",
        "argv": json.loads(row["argv_json"] or "[]"),
        "stdin_text": row["stdin_text"] or "",
        "timeout_sec": int(row["timeout_sec"] or 60),
        "risk_tier": row["risk_tier"] or "unknown",
        "status": row["status"] or "pending",
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _append_redaction_audit(
    conn: sqlite3.Connection,
    run_id: str,
    context: str,
    replacements: int,
) -> None:
    conn.execute(
        """
        INSERT INTO run_events (run_id, event_type, payload, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (
            run_id,
            "security.redaction.applied",
            f"context={context}, replacements={replacements}",
            _utc_now(),
        ),
    )
