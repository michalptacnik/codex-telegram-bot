import sqlite3
import uuid
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
from codex_telegram_bot.events.event_bus import RunEvent
from codex_telegram_bot.util import redact


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
            # Lightweight migration for existing DBs created before max_concurrency existed.
            cols = conn.execute("PRAGMA table_info(agents)").fetchall()
            col_names = {c["name"] for c in cols}
            if "max_concurrency" not in col_names:
                conn.execute("ALTER TABLE agents ADD COLUMN max_concurrency INTEGER NOT NULL DEFAULT 1")
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
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO runs (run_id, status, prompt, output, error, created_at, started_at, completed_at)
                VALUES (?, ?, ?, ?, ?, ?, NULL, NULL)
                """,
                (run_id, RUN_STATUS_PENDING, redact(prompt), "", "", now),
            )
        return run_id

    def mark_running(self, run_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE runs SET status = ?, started_at = ? WHERE run_id = ?",
                (RUN_STATUS_RUNNING, _utc_now(), run_id),
            )

    def mark_completed(self, run_id: str, output: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE runs SET status = ?, output = ?, completed_at = ? WHERE run_id = ?",
                (RUN_STATUS_COMPLETED, redact(output), _utc_now(), run_id),
            )

    def mark_failed(self, run_id: str, error: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE runs SET status = ?, error = ?, completed_at = ? WHERE run_id = ?",
                (RUN_STATUS_FAILED, redact(error), _utc_now(), run_id),
            )

    def append_event(self, event: RunEvent) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO run_events (run_id, event_type, payload, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    event.run_id,
                    event.event_type,
                    redact(event.payload),
                    event.created_at.isoformat(),
                ),
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


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
