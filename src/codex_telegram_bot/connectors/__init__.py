"""Work intake connectors package (EPIC 7)."""
from codex_telegram_bot.connectors.base import (
    Connector,
    ConnectorRegistry,
    IngestionCursor,
    LeadRecord,
    RateLimiter,
    make_lead_id,
)
from codex_telegram_bot.connectors.github_issues import GitHubIssueConnector
from codex_telegram_bot.connectors.pipeline import IngestionPipeline, ScoreFactors, score_lead

__all__ = [
    "Connector",
    "ConnectorRegistry",
    "IngestionCursor",
    "IngestionPipeline",
    "LeadRecord",
    "RateLimiter",
    "ScoreFactors",
    "GitHubIssueConnector",
    "make_lead_id",
    "score_lead",
]
