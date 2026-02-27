import sqlite3
import uuid
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

from codex_telegram_bot.connectors.base import IngestionCursor, LeadRecord
from codex_telegram_bot.domain.memory import (
    ArtifactRecord,
    MemoryEntry,
    MissionSummary,
)
from codex_telegram_bot.domain.missions import (
    MISSION_STATE_IDLE,
    MissionEventRecord,
    MissionRecord,
    validate_transition,
)
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
        conn.execute("PRAGMA busy_timeout = 2000")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
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
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS missions (
                    mission_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    goal TEXT NOT NULL,
                    state TEXT NOT NULL,
                    schedule_interval_sec INTEGER,
                    retry_limit INTEGER NOT NULL DEFAULT 3,
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    max_concurrency INTEGER NOT NULL DEFAULT 1,
                    context_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    started_at TEXT,
                    completed_at TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS mission_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    mission_id TEXT NOT NULL,
                    from_state TEXT NOT NULL,
                    to_state TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_missions_state
                ON missions (state)
                """
            )
            # EPIC 7: intake leads and connector cursors.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS intake_leads (
                    lead_id TEXT PRIMARY KEY,
                    connector_id TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    body TEXT NOT NULL,
                    url TEXT NOT NULL,
                    priority INTEGER NOT NULL DEFAULT 50,
                    labels_json TEXT NOT NULL DEFAULT '[]',
                    score REAL NOT NULL DEFAULT 0.0,
                    score_factors_json TEXT NOT NULL DEFAULT '{}',
                    extra_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    ingested_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_intake_leads_connector
                ON intake_leads (connector_id, score DESC)
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS connector_cursors (
                    connector_id TEXT PRIMARY KEY,
                    cursor_value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            # EPIC 8: mission memory, artifacts, and summaries.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS mission_memory (
                    entry_id TEXT PRIMARY KEY,
                    mission_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    key TEXT NOT NULL,
                    value TEXT NOT NULL,
                    tags_json TEXT NOT NULL DEFAULT '[]',
                    importance INTEGER NOT NULL DEFAULT 5,
                    created_at TEXT NOT NULL,
                    expires_at TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_mission_memory_lookup
                ON mission_memory (mission_id, kind, key)
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS mission_artifacts (
                    artifact_id TEXT PRIMARY KEY,
                    mission_id TEXT NOT NULL,
                    step_index INTEGER,
                    kind TEXT NOT NULL,
                    name TEXT NOT NULL,
                    uri TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL DEFAULT 0,
                    sha256 TEXT NOT NULL DEFAULT '',
                    tags_json TEXT NOT NULL DEFAULT '[]',
                    meta_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_mission_artifacts_lookup
                ON mission_artifacts (mission_id, kind)
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS mission_summaries (
                    summary_id TEXT PRIMARY KEY,
                    mission_id TEXT NOT NULL,
                    text TEXT NOT NULL,
                    memory_count INTEGER NOT NULL DEFAULT 0,
                    artifact_count INTEGER NOT NULL DEFAULT 0,
                    compacted INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_mission_summaries_mission
                ON mission_summaries (mission_id)
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS process_sessions (
                    process_session_id TEXT PRIMARY KEY,
                    chat_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    argv_json TEXT NOT NULL DEFAULT '[]',
                    workspace_root TEXT NOT NULL DEFAULT '',
                    pty_enabled INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'running',
                    exit_code INTEGER,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    completed_at TEXT,
                    last_activity_at TEXT NOT NULL,
                    max_wall_sec INTEGER NOT NULL DEFAULT 21600,
                    idle_timeout_sec INTEGER NOT NULL DEFAULT 1200,
                    max_output_bytes INTEGER NOT NULL DEFAULT 5242880,
                    ring_buffer_bytes INTEGER NOT NULL DEFAULT 65536,
                    output_bytes INTEGER NOT NULL DEFAULT 0,
                    redaction_replacements INTEGER NOT NULL DEFAULT 0,
                    log_path TEXT NOT NULL DEFAULT '',
                    index_path TEXT NOT NULL DEFAULT '',
                    last_cursor INTEGER NOT NULL DEFAULT 0,
                    error TEXT NOT NULL DEFAULT ''
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_process_sessions_chat_user_status
                ON process_sessions (chat_id, user_id, status, last_activity_at)
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS process_session_chunks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    process_session_id TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    start_offset INTEGER NOT NULL,
                    end_offset INTEGER NOT NULL,
                    preview TEXT NOT NULL DEFAULT ''
                )
                """
            )
            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_process_session_chunks_unique
                ON process_session_chunks (process_session_id, seq)
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
            process_cols = conn.execute("PRAGMA table_info(process_sessions)").fetchall()
            process_col_names = {c["name"] for c in process_cols}
            process_defaults = {
                "argv_json": "TEXT NOT NULL DEFAULT '[]'",
                "workspace_root": "TEXT NOT NULL DEFAULT ''",
                "pty_enabled": "INTEGER NOT NULL DEFAULT 0",
                "status": "TEXT NOT NULL DEFAULT 'running'",
                "exit_code": "INTEGER",
                "created_at": "TEXT NOT NULL DEFAULT ''",
                "started_at": "TEXT",
                "completed_at": "TEXT",
                "last_activity_at": "TEXT NOT NULL DEFAULT ''",
                "max_wall_sec": "INTEGER NOT NULL DEFAULT 21600",
                "idle_timeout_sec": "INTEGER NOT NULL DEFAULT 1200",
                "max_output_bytes": "INTEGER NOT NULL DEFAULT 5242880",
                "ring_buffer_bytes": "INTEGER NOT NULL DEFAULT 65536",
                "output_bytes": "INTEGER NOT NULL DEFAULT 0",
                "redaction_replacements": "INTEGER NOT NULL DEFAULT 0",
                "log_path": "TEXT NOT NULL DEFAULT ''",
                "index_path": "TEXT NOT NULL DEFAULT ''",
                "last_cursor": "INTEGER NOT NULL DEFAULT 0",
                "error": "TEXT NOT NULL DEFAULT ''",
            }
            for key, ddl in process_defaults.items():
                if process_cols and key not in process_col_names:
                    conn.execute(f"ALTER TABLE process_sessions ADD COLUMN {key} {ddl}")
            existing = conn.execute("SELECT COUNT(*) AS c FROM agents").fetchone()["c"]
            if existing == 0:
                now = _utc_now()
                conn.execute(
                    """
                    INSERT INTO agents (agent_id, name, provider, policy_profile, max_concurrency, enabled, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    ("default", "Default Agent", "codex_cli", "trusted", 1, 1, now, now),
                )
            else:
                # Upgrade legacy default agent profile to trusted for full-capability startup.
                conn.execute(
                    """
                    UPDATE agents
                    SET policy_profile = ?, updated_at = ?
                    WHERE agent_id = ? AND policy_profile = ?
                    """,
                    ("trusted", _utc_now(), "default", "balanced"),
                )
            conn.execute(
                """
                UPDATE process_sessions
                SET status = 'orphaned',
                    error = CASE
                        WHEN error = '' THEN 'Recovered after restart: session handle orphaned.'
                        ELSE error
                    END,
                    completed_at = COALESCE(completed_at, ?),
                    last_activity_at = COALESCE(last_activity_at, ?)
                WHERE status = 'running'
                """,
                (_utc_now(), _utc_now()),
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

    def recover_interrupted_runs(self, stale_after_sec: int = 30) -> int:
        """Mark stale 'running' rows as failed after process restart."""
        cutoff = (
            datetime.now(timezone.utc) - timedelta(seconds=max(1, int(stale_after_sec)))
        ).isoformat()
        with self._connect() as conn:
            cur = conn.execute(
                """
                UPDATE runs
                SET status = ?,
                    error = CASE
                        WHEN error = '' THEN ?
                        ELSE error
                    END,
                    completed_at = COALESCE(completed_at, ?)
                WHERE status = ? AND started_at IS NOT NULL AND started_at < ?
                """,
                (
                    RUN_STATUS_FAILED,
                    "Recovered after restart: interrupted execution marked failed.",
                    _utc_now(),
                    RUN_STATUS_RUNNING,
                    cutoff,
                ),
            )
            return int(cur.rowcount or 0)

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
            if remove_count >= 120:
                tier = "archival"
            elif remove_count >= 40:
                tier = "medium"
            else:
                tier = "short"
            prev_summary = conn.execute(
                "SELECT summary FROM telegram_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            carry = ""
            if prev_summary and prev_summary["summary"]:
                carry = f" | prev={str(prev_summary['summary'])[:120]}"
            conn.execute(
                """
                UPDATE telegram_sessions
                SET summary = ?, updated_at = ?
                WHERE session_id = ?
                """,
                (f"[tier={tier}] Compacted {remove_count} messages. {summary_text}{carry}", _utc_now(), session_id),
            )
            conn.execute(
                """
                INSERT INTO telegram_session_messages (session_id, role, content, run_id, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (session_id, "system", f"history.compacted removed={remove_count}", "", _utc_now()),
            )
            return remove_count

    # ------------------------------------------------------------------
    # Session retention helpers (Parity Epic 1)
    # ------------------------------------------------------------------

    def archive_idle_sessions(self, idle_days: int = 30) -> int:
        """Archive ``active`` sessions not updated in ``idle_days`` days.

        Returns the number of sessions transitioned to ``archived``.
        """
        from datetime import timedelta

        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=max(1, int(idle_days)))
        ).isoformat()
        with self._connect() as conn:
            result = conn.execute(
                """
                UPDATE telegram_sessions
                SET status = ?, updated_at = ?
                WHERE status = ? AND updated_at < ?
                """,
                (SESSION_STATUS_ARCHIVED, _utc_now(), SESSION_STATUS_ACTIVE, cutoff),
            )
            return result.rowcount if result.rowcount is not None else 0

    def prune_archived_sessions(self, older_than_days: int = 90) -> int:
        """Hard-delete archived sessions (and their messages) older than
        ``older_than_days`` days.

        Returns the number of sessions deleted.
        """
        from datetime import timedelta

        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=max(1, int(older_than_days)))
        ).isoformat()
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT session_id FROM telegram_sessions WHERE status = ? AND updated_at < ?",
                (SESSION_STATUS_ARCHIVED, cutoff),
            ).fetchall()
            if not rows:
                return 0
            ids = [r["session_id"] for r in rows]
            placeholders = ",".join(["?"] * len(ids))
            conn.execute(
                f"DELETE FROM telegram_session_messages WHERE session_id IN ({placeholders})",
                ids,
            )
            conn.execute(
                f"DELETE FROM telegram_sessions WHERE session_id IN ({placeholders})",
                ids,
            )
            return len(ids)

    # ------------------------------------------------------------------
    # Process session persistence
    # ------------------------------------------------------------------

    def create_process_session(
        self,
        *,
        process_session_id: str,
        chat_id: int,
        user_id: int,
        argv: list[str],
        workspace_root: str,
        pty_enabled: bool,
        status: str,
        exit_code: Optional[int],
        created_at: str,
        started_at: Optional[str],
        completed_at: Optional[str],
        last_activity_at: str,
        max_wall_sec: int,
        idle_timeout_sec: int,
        max_output_bytes: int,
        ring_buffer_bytes: int,
        output_bytes: int,
        redaction_replacements: int,
        log_path: str,
        index_path: str,
        last_cursor: int,
        error: str,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO process_sessions (
                    process_session_id, chat_id, user_id, argv_json, workspace_root,
                    pty_enabled, status, exit_code, created_at, started_at, completed_at,
                    last_activity_at, max_wall_sec, idle_timeout_sec, max_output_bytes,
                    ring_buffer_bytes, output_bytes, redaction_replacements, log_path,
                    index_path, last_cursor, error
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    process_session_id,
                    int(chat_id),
                    int(user_id),
                    json.dumps(argv or []),
                    workspace_root or "",
                    1 if pty_enabled else 0,
                    status or "running",
                    exit_code,
                    created_at,
                    started_at,
                    completed_at,
                    last_activity_at,
                    int(max_wall_sec),
                    int(idle_timeout_sec),
                    int(max_output_bytes),
                    int(ring_buffer_bytes),
                    int(output_bytes),
                    int(redaction_replacements),
                    log_path or "",
                    index_path or "",
                    int(last_cursor),
                    error or "",
                ),
            )

    def update_process_session(
        self,
        process_session_id: str,
        *,
        status: Optional[str] = None,
        exit_code: Optional[int] = None,
        completed_at: Optional[str] = None,
        last_activity_at: Optional[str] = None,
        output_bytes: Optional[int] = None,
        redaction_replacements: Optional[int] = None,
        last_cursor: Optional[int] = None,
        pty_enabled: Optional[bool] = None,
        error: Optional[str] = None,
    ) -> None:
        clauses: list[str] = []
        params: list = []
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        if exit_code is not None:
            clauses.append("exit_code = ?")
            params.append(int(exit_code))
        if completed_at is not None:
            clauses.append("completed_at = ?")
            params.append(completed_at)
        if last_activity_at is not None:
            clauses.append("last_activity_at = ?")
            params.append(last_activity_at)
        if output_bytes is not None:
            clauses.append("output_bytes = ?")
            params.append(int(output_bytes))
        if redaction_replacements is not None:
            clauses.append("redaction_replacements = ?")
            params.append(int(redaction_replacements))
        if last_cursor is not None:
            clauses.append("last_cursor = ?")
            params.append(int(last_cursor))
        if pty_enabled is not None:
            clauses.append("pty_enabled = ?")
            params.append(1 if pty_enabled else 0)
        if error is not None:
            clauses.append("error = ?")
            params.append(error)
        if not clauses:
            return
        params.append(process_session_id)
        with self._connect() as conn:
            conn.execute(
                f"UPDATE process_sessions SET {', '.join(clauses)} WHERE process_session_id = ?",
                params,
            )

    def set_process_session_last_cursor(self, process_session_id: str, cursor: int) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE process_sessions
                SET last_cursor = ?, last_activity_at = ?
                WHERE process_session_id = ?
                """,
                (int(cursor), _utc_now(), process_session_id),
            )

    def get_process_session(self, process_session_id: str) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM process_sessions WHERE process_session_id = ?",
                (process_session_id,),
            ).fetchone()
        if not row:
            return None
        return _row_to_process_session(row)

    def list_process_sessions(self, chat_id: int, user_id: int, limit: int = 20) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM process_sessions
                WHERE chat_id = ? AND user_id = ?
                ORDER BY last_activity_at DESC
                LIMIT ?
                """,
                (int(chat_id), int(user_id), max(1, int(limit))),
            ).fetchall()
        return [_row_to_process_session(r) for r in rows]

    def count_running_process_sessions(self, chat_id: int, user_id: int) -> int:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS c FROM process_sessions
                WHERE chat_id = ? AND user_id = ? AND status = 'running'
                """,
                (int(chat_id), int(user_id)),
            ).fetchone()
        return int(row["c"] or 0)

    def append_process_session_chunk(
        self,
        *,
        process_session_id: str,
        seq: int,
        created_at: str,
        start_offset: int,
        end_offset: int,
        preview: str,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO process_session_chunks
                (process_session_id, seq, created_at, start_offset, end_offset, preview)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    process_session_id,
                    int(seq),
                    created_at,
                    int(start_offset),
                    int(end_offset),
                    preview or "",
                ),
            )

    def list_process_session_chunks(self, process_session_id: str, limit: int = 100) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM process_session_chunks
                WHERE process_session_id = ?
                ORDER BY seq DESC
                LIMIT ?
                """,
                (process_session_id, max(1, int(limit))),
            ).fetchall()
        return [
            {
                "id": int(row["id"]),
                "process_session_id": row["process_session_id"],
                "seq": int(row["seq"]),
                "created_at": row["created_at"],
                "start_offset": int(row["start_offset"]),
                "end_offset": int(row["end_offset"]),
                "preview": row["preview"] or "",
            }
            for row in rows
        ]

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

    # ------------------------------------------------------------------
    # Mission persistence (EPIC 6)
    # ------------------------------------------------------------------

    def create_mission(
        self,
        title: str,
        goal: str,
        schedule_interval_sec: Optional[int] = None,
        retry_limit: int = 3,
        max_concurrency: int = 1,
        context: Optional[Dict] = None,
    ) -> str:
        mission_id = str(uuid.uuid4())
        now = _utc_now()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO missions
                    (mission_id, title, goal, state, schedule_interval_sec,
                     retry_limit, retry_count, max_concurrency, context_json,
                     created_at, updated_at, started_at, completed_at)
                VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, NULL, NULL)
                """,
                (
                    mission_id,
                    title,
                    goal,
                    MISSION_STATE_IDLE,
                    schedule_interval_sec,
                    retry_limit,
                    max_concurrency,
                    json.dumps(context or {}),
                    now,
                    now,
                ),
            )
        return mission_id

    def get_mission(self, mission_id: str) -> Optional[MissionRecord]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM missions WHERE mission_id = ?", (mission_id,)
            ).fetchone()
        if row is None:
            return None
        return _row_to_mission(row)

    def list_missions(self, state: Optional[str] = None) -> List[MissionRecord]:
        with self._connect() as conn:
            if state:
                rows = conn.execute(
                    "SELECT * FROM missions WHERE state = ? ORDER BY created_at ASC",
                    (state,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM missions ORDER BY created_at ASC"
                ).fetchall()
        return [_row_to_mission(r) for r in rows]

    def transition_mission(
        self, mission_id: str, to_state: str, reason: str = ""
    ) -> MissionRecord:
        """Validate and apply a state transition, writing an audit event."""
        mission = self.get_mission(mission_id)
        if mission is None:
            raise ValueError(f"Mission not found: {mission_id}")
        validate_transition(mission.state, to_state)
        now = _utc_now()
        started_at = mission.started_at
        completed_at = mission.completed_at
        if to_state == "running" and started_at is None:
            started_at = datetime.fromisoformat(now)
        if to_state in {"completed", "failed"}:
            completed_at = datetime.fromisoformat(now)
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE missions
                SET state = ?, updated_at = ?,
                    started_at = COALESCE(started_at, ?),
                    completed_at = ?
                WHERE mission_id = ?
                """,
                (
                    to_state,
                    now,
                    started_at.isoformat() if started_at else None,
                    completed_at.isoformat() if completed_at else None,
                    mission_id,
                ),
            )
            conn.execute(
                """
                INSERT INTO mission_events (mission_id, from_state, to_state, reason, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (mission_id, mission.state, to_state, reason or "", now),
            )
        return self.get_mission(mission_id)  # type: ignore[return-value]

    def increment_mission_retry(self, mission_id: str) -> int:
        """Increment retry_count and return the new value."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE missions SET retry_count = retry_count + 1, updated_at = ? WHERE mission_id = ?",
                (_utc_now(), mission_id),
            )
            row = conn.execute(
                "SELECT retry_count FROM missions WHERE mission_id = ?", (mission_id,)
            ).fetchone()
        return int(row["retry_count"]) if row else 0

    def reset_mission_retry(self, mission_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE missions SET retry_count = 0, updated_at = ? WHERE mission_id = ?",
                (_utc_now(), mission_id),
            )

    def list_mission_events(self, mission_id: str) -> List[MissionEventRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM mission_events WHERE mission_id = ?
                ORDER BY id ASC
                """,
                (mission_id,),
            ).fetchall()
        return [_row_to_mission_event(r) for r in rows]

    def count_mission_events_since(self, transition: str, since_iso: str) -> int:
        """Count events matching a 'from→to' transition string since a timestamp.

        Used by the observability layer to compute windowed throughput/error-rate.
        Transition format: 'running→completed', 'pending→running', etc.
        """
        parts = transition.split("→", 1)
        if len(parts) != 2:
            return 0
        from_state, to_state = parts
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS c FROM mission_events
                WHERE from_state = ? AND to_state = ? AND created_at >= ?
                """,
                (from_state, to_state, since_iso),
            ).fetchone()
        return int(row["c"]) if row else 0

    # ------------------------------------------------------------------
    # Intake leads + connector cursors (EPIC 7)
    # ------------------------------------------------------------------

    def upsert_lead(
        self,
        lead: LeadRecord,
        score: float = 0.0,
        score_factors_json: str = "{}",
    ) -> None:
        """Insert or update a lead record."""
        now = _utc_now()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO intake_leads
                    (lead_id, connector_id, source_id, title, body, url,
                     priority, labels_json, score, score_factors_json,
                     extra_json, created_at, updated_at, ingested_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(lead_id) DO UPDATE SET
                    title=excluded.title,
                    body=excluded.body,
                    url=excluded.url,
                    priority=excluded.priority,
                    labels_json=excluded.labels_json,
                    score=excluded.score,
                    score_factors_json=excluded.score_factors_json,
                    extra_json=excluded.extra_json,
                    updated_at=excluded.updated_at
                """,
                (
                    lead.lead_id,
                    lead.connector_id,
                    lead.source_id,
                    lead.title,
                    lead.body,
                    lead.url,
                    lead.priority,
                    json.dumps(lead.labels),
                    score,
                    score_factors_json,
                    json.dumps(lead.extra),
                    lead.created_at.isoformat(),
                    lead.updated_at.isoformat(),
                    now,
                ),
            )

    def lead_exists(self, lead_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM intake_leads WHERE lead_id = ?", (lead_id,)
            ).fetchone()
        return row is not None

    def get_lead(self, lead_id: str) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM intake_leads WHERE lead_id = ?", (lead_id,)
            ).fetchone()
        if row is None:
            return None
        return _row_to_lead(row)

    def list_leads(
        self,
        connector_id: Optional[str] = None,
        limit: int = 100,
        min_score: float = 0.0,
    ) -> List[dict]:
        with self._connect() as conn:
            if connector_id:
                rows = conn.execute(
                    """
                    SELECT * FROM intake_leads
                    WHERE connector_id = ? AND score >= ?
                    ORDER BY score DESC LIMIT ?
                    """,
                    (connector_id, min_score, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM intake_leads
                    WHERE score >= ?
                    ORDER BY score DESC LIMIT ?
                    """,
                    (min_score, limit),
                ).fetchall()
        return [_row_to_lead(r) for r in rows]

    def get_connector_cursor(self, connector_id: str) -> Optional[IngestionCursor]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM connector_cursors WHERE connector_id = ?",
                (connector_id,),
            ).fetchone()
        if row is None:
            return None
        return IngestionCursor(
            connector_id=row["connector_id"],
            value=row["cursor_value"],
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    def save_connector_cursor(self, cursor: IngestionCursor) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO connector_cursors (connector_id, cursor_value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(connector_id) DO UPDATE SET
                    cursor_value=excluded.cursor_value,
                    updated_at=excluded.updated_at
                """,
                (cursor.connector_id, cursor.value, cursor.updated_at.isoformat()),
            )

    # ------------------------------------------------------------------
    # Mission memory (EPIC 8)
    # ------------------------------------------------------------------

    def upsert_memory_entry(
        self,
        mission_id: str,
        kind: str,
        key: str,
        value: str,
        tags: Optional[List[str]] = None,
        importance: int = 5,
        expires_at: Optional["datetime"] = None,
    ) -> MemoryEntry:
        entry_id = str(uuid.uuid4())
        now = _utc_now()
        expires_iso = expires_at.isoformat() if expires_at else None
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO mission_memory
                    (entry_id, mission_id, kind, key, value, tags_json,
                     importance, created_at, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    entry_id, mission_id, kind, key, value,
                    json.dumps(tags or []), importance, now, expires_iso,
                ),
            )
        return MemoryEntry(
            entry_id=entry_id, mission_id=mission_id, kind=kind, key=key,
            value=value, tags=tags or [], importance=importance,
            created_at=datetime.fromisoformat(now),
            expires_at=expires_at,
        )

    def list_memory_entries(
        self,
        mission_id: str,
        kind: Optional[str] = None,
        key: Optional[str] = None,
        tag: Optional[str] = None,
        limit: int = 50,
        include_expired: bool = False,
    ) -> List[MemoryEntry]:
        clauses = ["mission_id = ?"]
        params: list = [mission_id]
        if kind:
            clauses.append("kind = ?"); params.append(kind)
        if key:
            clauses.append("key = ?"); params.append(key)
        if not include_expired:
            clauses.append("(expires_at IS NULL OR expires_at > ?)")
            params.append(_utc_now())
        where = " AND ".join(clauses)
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM mission_memory WHERE {where} "
                f"ORDER BY importance DESC, created_at DESC LIMIT ?",
                params,
            ).fetchall()
        results = [_row_to_memory_entry(r) for r in rows]
        if tag:
            results = [e for e in results if tag in e.tags]
        return results

    def delete_memory_entry(self, entry_id: str) -> bool:
        with self._connect() as conn:
            c = conn.execute(
                "DELETE FROM mission_memory WHERE entry_id = ?", (entry_id,)
            )
        return c.rowcount > 0

    def delete_mission_memory(self, mission_id: str) -> int:
        with self._connect() as conn:
            c = conn.execute(
                "DELETE FROM mission_memory WHERE mission_id = ?", (mission_id,)
            )
        return c.rowcount

    def count_memory_entries(self, mission_id: str) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM mission_memory WHERE mission_id = ?",
                (mission_id,),
            ).fetchone()
        return int(row["c"])

    def trim_memory_entries(self, mission_id: str, drop_count: int) -> int:
        """Drop the ``drop_count`` lowest-importance + oldest entries."""
        with self._connect() as conn:
            ids = conn.execute(
                """
                SELECT entry_id FROM mission_memory
                WHERE mission_id = ?
                ORDER BY importance ASC, created_at ASC
                LIMIT ?
                """,
                (mission_id, drop_count),
            ).fetchall()
            if not ids:
                return 0
            placeholders = ",".join("?" * len(ids))
            c = conn.execute(
                f"DELETE FROM mission_memory WHERE entry_id IN ({placeholders})",
                [r["entry_id"] for r in ids],
            )
        return c.rowcount

    def expire_memory_entries(self, before_iso: str) -> int:
        with self._connect() as conn:
            c = conn.execute(
                "DELETE FROM mission_memory WHERE expires_at IS NOT NULL AND expires_at <= ?",
                (before_iso,),
            )
        return c.rowcount

    # ------------------------------------------------------------------
    # Artifact index (EPIC 8)
    # ------------------------------------------------------------------

    def upsert_artifact(
        self,
        mission_id: str,
        kind: str,
        name: str,
        uri: str,
        size_bytes: int = 0,
        sha256: str = "",
        step_index: Optional[int] = None,
        tags: Optional[List[str]] = None,
        meta: Optional[Dict] = None,
    ) -> str:
        artifact_id = str(uuid.uuid4())
        now = _utc_now()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO mission_artifacts
                    (artifact_id, mission_id, step_index, kind, name, uri,
                     size_bytes, sha256, tags_json, meta_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    artifact_id, mission_id, step_index, kind, name, uri,
                    size_bytes, sha256,
                    json.dumps(tags or []), json.dumps(meta or {}), now,
                ),
            )
        return artifact_id

    def get_artifact(self, artifact_id: str) -> Optional[ArtifactRecord]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM mission_artifacts WHERE artifact_id = ?",
                (artifact_id,),
            ).fetchone()
        return _row_to_artifact(row) if row else None

    def list_artifacts(
        self,
        mission_id: str,
        kind: Optional[str] = None,
        tag: Optional[str] = None,
        limit: int = 100,
    ) -> List[ArtifactRecord]:
        clauses = ["mission_id = ?"]
        params: list = [mission_id]
        if kind:
            clauses.append("kind = ?"); params.append(kind)
        where = " AND ".join(clauses)
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM mission_artifacts WHERE {where} "
                f"ORDER BY created_at DESC LIMIT ?",
                params,
            ).fetchall()
        results = [_row_to_artifact(r) for r in rows]
        if tag:
            results = [a for a in results if tag in a.tags]
        return results

    def count_artifacts(self, mission_id: str) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM mission_artifacts WHERE mission_id = ?",
                (mission_id,),
            ).fetchone()
        return int(row["c"])

    def delete_artifact(self, artifact_id: str) -> bool:
        with self._connect() as conn:
            c = conn.execute(
                "DELETE FROM mission_artifacts WHERE artifact_id = ?", (artifact_id,)
            )
        return c.rowcount > 0

    def delete_mission_artifacts(self, mission_id: str) -> int:
        with self._connect() as conn:
            c = conn.execute(
                "DELETE FROM mission_artifacts WHERE mission_id = ?", (mission_id,)
            )
        return c.rowcount

    # ------------------------------------------------------------------
    # Mission summaries (EPIC 8)
    # ------------------------------------------------------------------

    def save_mission_summary(
        self,
        mission_id: str,
        text: str,
        memory_count: int = 0,
        artifact_count: int = 0,
        compacted: bool = False,
    ) -> MissionSummary:
        summary_id = str(uuid.uuid4())
        now = _utc_now()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO mission_summaries
                    (summary_id, mission_id, text, memory_count, artifact_count,
                     compacted, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (summary_id, mission_id, text, memory_count, artifact_count,
                 1 if compacted else 0, now),
            )
        return MissionSummary(
            summary_id=summary_id, mission_id=mission_id, text=text,
            memory_count=memory_count, artifact_count=artifact_count,
            compacted=compacted, created_at=datetime.fromisoformat(now),
        )

    def list_mission_summaries(self, mission_id: str) -> List[MissionSummary]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM mission_summaries WHERE mission_id = ? ORDER BY created_at DESC",
                (mission_id,),
            ).fetchall()
        return [_row_to_summary(r) for r in rows]

    def get_latest_summary(self, mission_id: str) -> Optional[MissionSummary]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM mission_summaries WHERE mission_id = ? ORDER BY created_at DESC LIMIT 1",
                (mission_id,),
            ).fetchone()
        return _row_to_summary(row) if row else None


def _row_to_memory_entry(row: sqlite3.Row) -> MemoryEntry:
    return MemoryEntry(
        entry_id=row["entry_id"],
        mission_id=row["mission_id"],
        kind=row["kind"],
        key=row["key"],
        value=row["value"],
        tags=json.loads(row["tags_json"] or "[]"),
        importance=int(row["importance"] or 5),
        created_at=datetime.fromisoformat(row["created_at"]),
        expires_at=datetime.fromisoformat(row["expires_at"]) if row["expires_at"] else None,
    )


def _row_to_artifact(row: sqlite3.Row) -> ArtifactRecord:
    return ArtifactRecord(
        artifact_id=row["artifact_id"],
        mission_id=row["mission_id"],
        step_index=row["step_index"],
        kind=row["kind"],
        name=row["name"],
        uri=row["uri"],
        size_bytes=int(row["size_bytes"] or 0),
        sha256=row["sha256"] or "",
        tags=json.loads(row["tags_json"] or "[]"),
        created_at=datetime.fromisoformat(row["created_at"]),
        meta_json=row["meta_json"] or "{}",
    )


def _row_to_summary(row: sqlite3.Row) -> MissionSummary:
    return MissionSummary(
        summary_id=row["summary_id"],
        mission_id=row["mission_id"],
        text=row["text"],
        memory_count=int(row["memory_count"] or 0),
        artifact_count=int(row["artifact_count"] or 0),
        compacted=bool(row["compacted"]),
        created_at=datetime.fromisoformat(row["created_at"]),
    )


def _row_to_lead(row: sqlite3.Row) -> dict:
    return {
        "lead_id": row["lead_id"],
        "connector_id": row["connector_id"],
        "source_id": row["source_id"],
        "title": row["title"],
        "body": row["body"],
        "url": row["url"],
        "priority": int(row["priority"]),
        "labels": json.loads(row["labels_json"] or "[]"),
        "score": float(row["score"] or 0),
        "score_factors": json.loads(row["score_factors_json"] or "{}"),
        "extra": json.loads(row["extra_json"] or "{}"),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "ingested_at": row["ingested_at"],
    }


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


def _row_to_mission(row: sqlite3.Row) -> MissionRecord:
    return MissionRecord(
        mission_id=row["mission_id"],
        title=row["title"],
        goal=row["goal"],
        state=row["state"],
        schedule_interval_sec=row["schedule_interval_sec"],
        retry_limit=int(row["retry_limit"] or 3),
        retry_count=int(row["retry_count"] or 0),
        max_concurrency=int(row["max_concurrency"] or 1),
        context_json=row["context_json"] or "{}",
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
        started_at=datetime.fromisoformat(row["started_at"]) if row["started_at"] else None,
        completed_at=datetime.fromisoformat(row["completed_at"]) if row["completed_at"] else None,
    )


def _row_to_mission_event(row: sqlite3.Row) -> MissionEventRecord:
    return MissionEventRecord(
        id=int(row["id"]),
        mission_id=row["mission_id"],
        from_state=row["from_state"],
        to_state=row["to_state"],
        reason=row["reason"],
        created_at=datetime.fromisoformat(row["created_at"]),
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


def _row_to_process_session(row: sqlite3.Row) -> dict:
    return {
        "process_session_id": row["process_session_id"],
        "chat_id": int(row["chat_id"] or 0),
        "user_id": int(row["user_id"] or 0),
        "argv": json.loads(row["argv_json"] or "[]"),
        "workspace_root": row["workspace_root"] or "",
        "pty_enabled": bool(row["pty_enabled"]),
        "status": row["status"] or "unknown",
        "exit_code": int(row["exit_code"]) if row["exit_code"] is not None else None,
        "created_at": row["created_at"] or "",
        "started_at": row["started_at"] or "",
        "completed_at": row["completed_at"] or "",
        "last_activity_at": row["last_activity_at"] or "",
        "max_wall_sec": int(row["max_wall_sec"] or 0),
        "idle_timeout_sec": int(row["idle_timeout_sec"] or 0),
        "max_output_bytes": int(row["max_output_bytes"] or 0),
        "ring_buffer_bytes": int(row["ring_buffer_bytes"] or 0),
        "output_bytes": int(row["output_bytes"] or 0),
        "redaction_replacements": int(row["redaction_replacements"] or 0),
        "log_path": row["log_path"] or "",
        "index_path": row["index_path"] or "",
        "last_cursor": int(row["last_cursor"] or 0),
        "error": row["error"] or "",
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
