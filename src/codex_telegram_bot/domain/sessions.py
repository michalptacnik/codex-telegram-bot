from dataclasses import dataclass
from datetime import datetime
from typing import Optional


SESSION_STATUS_ACTIVE = "active"
SESSION_STATUS_ARCHIVED = "archived"


@dataclass(frozen=True)
class TelegramSessionRecord:
    session_id: str
    chat_id: int
    user_id: int
    status: str
    current_agent_id: str
    summary: str
    last_run_id: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class TelegramSessionMessageRecord:
    id: int
    session_id: str
    role: str
    content: str
    run_id: str
    created_at: datetime
