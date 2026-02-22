from dataclasses import dataclass
from typing import List


@dataclass(frozen=True)
class RecoveryAction:
    action_id: str
    label: str
    description: str


@dataclass(frozen=True)
class ErrorCatalogEntry:
    code: str
    title: str
    user_message: str
    triggers: List[str]
    actions: List[RecoveryAction]


ERROR_CATALOG: List[ErrorCatalogEntry] = [
    ErrorCatalogEntry(
        code="ERR_POLICY_BLOCKED",
        title="Execution blocked by policy",
        user_message="The command was blocked by the selected policy profile.",
        triggers=["Blocked by execution policy:"],
        actions=[
            RecoveryAction("retry_default_agent", "Retry on default agent", "Run prompt on default profile."),
            RecoveryAction("open_agents", "Review agents", "Check policy profile configuration."),
        ],
    ),
    ErrorCatalogEntry(
        code="ERR_EXEC_TIMEOUT",
        title="Execution timeout",
        user_message="The command exceeded allowed execution time.",
        triggers=["Execution timeout."],
        actions=[
            RecoveryAction("retry_same_agent", "Retry same agent", "Queue the same prompt again."),
            RecoveryAction("retry_default_agent", "Retry on default agent", "Retry with baseline profile."),
        ],
    ),
    ErrorCatalogEntry(
        code="ERR_CLI_NOT_FOUND",
        title="Codex CLI not available",
        user_message="The runtime cannot find the codex executable.",
        triggers=["Error: codex CLI not found."],
        actions=[
            RecoveryAction("open_settings", "Open settings", "Check provider health and runtime setup."),
            RecoveryAction("download_artifact", "Download artifact", "Export full run context for debugging."),
        ],
    ),
    ErrorCatalogEntry(
        code="ERR_CODEX_EXIT_NONZERO",
        title="Codex exited with error",
        user_message="The provider returned a non-zero exit code.",
        triggers=["Error: codex exited with code"],
        actions=[
            RecoveryAction("retry_same_agent", "Retry same agent", "Queue the same prompt again."),
            RecoveryAction("download_artifact", "Download artifact", "Inspect timeline and stderr trail."),
        ],
    ),
    ErrorCatalogEntry(
        code="ERR_PROVIDER_UNHEALTHY",
        title="Primary provider unhealthy",
        user_message="Primary provider is currently unhealthy or circuit-open.",
        triggers=["Error: primary provider is temporarily unhealthy."],
        actions=[
            RecoveryAction("open_settings", "Open settings", "Inspect provider router health."),
            RecoveryAction("retry_default_agent", "Retry on default agent", "Retry via baseline route."),
        ],
    ),
    ErrorCatalogEntry(
        code="ERR_PROVIDER_FAILED",
        title="Provider execution failed",
        user_message="Provider execution failed after retries.",
        triggers=["Error: provider execution failed."],
        actions=[
            RecoveryAction("retry_same_agent", "Retry same agent", "Attempt a clean retry."),
            RecoveryAction("open_settings", "Open settings", "Review provider health and fallback mode."),
        ],
    ),
    ErrorCatalogEntry(
        code="ERR_RUN_CANCELLED",
        title="Run cancelled",
        user_message="Scheduled job was cancelled before completion.",
        triggers=["Error: scheduled job was cancelled."],
        actions=[
            RecoveryAction("retry_same_agent", "Retry same agent", "Queue the same prompt again."),
            RecoveryAction("retry_default_agent", "Retry on default agent", "Retry with baseline agent."),
        ],
    ),
    ErrorCatalogEntry(
        code="ERR_HANDOFF_UNAVAILABLE",
        title="Handoff target unavailable",
        user_message="Requested handoff target agent is unavailable.",
        triggers=["Error: handoff target agent unavailable."],
        actions=[
            RecoveryAction("retry_default_agent", "Retry on default agent", "Fallback to default agent."),
            RecoveryAction("open_agents", "Open agents", "Enable target agent or adjust routing."),
        ],
    ),
    ErrorCatalogEntry(
        code="ERR_INVALID_AGENT_CONFIG",
        title="Invalid agent configuration",
        user_message="Agent configuration failed validation.",
        triggers=["Invalid agent_id", "Invalid policy profile.", "Unsupported provider."],
        actions=[
            RecoveryAction("open_agents", "Open agents", "Fix agent profile/fields."),
            RecoveryAction("retry_default_agent", "Retry on default agent", "Use baseline agent."),
        ],
    ),
    ErrorCatalogEntry(
        code="ERR_UNKNOWN",
        title="Unknown execution error",
        user_message="An unknown error occurred.",
        triggers=[],
        actions=[
            RecoveryAction("retry_same_agent", "Retry same agent", "Retry once to confirm reproducibility."),
            RecoveryAction("download_artifact", "Download artifact", "Export full context for triage."),
        ],
    ),
]


def detect_error_code(text: str) -> str:
    value = text or ""
    for entry in ERROR_CATALOG:
        if any(trigger in value for trigger in entry.triggers):
            return entry.code
    return "ERR_UNKNOWN"


def get_catalog_entry(code: str) -> ErrorCatalogEntry:
    for entry in ERROR_CATALOG:
        if entry.code == code:
            return entry
    return next(entry for entry in ERROR_CATALOG if entry.code == "ERR_UNKNOWN")
