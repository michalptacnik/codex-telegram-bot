from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class AgentRecord:
    agent_id: str
    name: str
    provider: str
    policy_profile: str
    max_concurrency: int
    enabled: bool
    created_at: datetime
    updated_at: datetime
