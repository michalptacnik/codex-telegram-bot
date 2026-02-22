from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List


@dataclass(frozen=True)
class PluginRecord:
    plugin_id: str
    name: str
    version: str
    manifest_version: str
    requires_api_version: str
    capabilities: List[str]
    enabled: bool
    trust_status: str
    manifest_path: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class PluginAuditEvent:
    ts: datetime
    action: str
    plugin_id: str
    outcome: str
    details: Dict[str, str]

