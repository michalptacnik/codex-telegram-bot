from dataclasses import dataclass
from datetime import datetime
from typing import Optional


RUN_STATUS_PENDING = "pending"
RUN_STATUS_RUNNING = "running"
RUN_STATUS_COMPLETED = "completed"
RUN_STATUS_FAILED = "failed"


@dataclass(frozen=True)
class RunRecord:
    run_id: str
    status: str
    prompt: str
    output: str
    error: str
    created_at: datetime
    started_at: Optional[datetime]
    completed_at: Optional[datetime]

