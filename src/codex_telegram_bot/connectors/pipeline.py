"""Dedup + scoring pipeline (EPIC 7, issue #82).

Responsibilities:
- Hash-based deduplication across ingestion runs (by lead_id).
- Quality scoring with explainable factor breakdown.
- Persisting leads + scores + provenance in SqliteRunStore.
- Driving the full intake cycle: fetch → dedup → score → store → create missions.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Dict, List, Optional, Tuple

from codex_telegram_bot.connectors.base import (
    Connector,
    ConnectorRegistry,
    IngestionCursor,
    LeadRecord,
)

if TYPE_CHECKING:
    from codex_telegram_bot.persistence.sqlite_store import SqliteRunStore

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScoreFactors:
    """Breakdown of the quality score for a lead."""
    priority_score: float       # 0–40 pts, from label-based priority
    recency_score: float        # 0–30 pts, how recently updated
    engagement_score: float     # 0–20 pts, comment count
    title_length_score: float   # 0–10 pts, title quality
    total: float                # sum, higher = better


def score_lead(lead: LeadRecord) -> ScoreFactors:
    """Return an explainable score for a lead (higher = higher priority)."""
    # Priority (0=highest → 40 pts, 100=lowest → 0 pts)
    priority_score = max(0.0, 40.0 - lead.priority * 0.4)

    # Recency: full score for leads updated in the last 24 h, decays over 30 days
    now = datetime.now(timezone.utc)
    age_days = max(0, (now - lead.updated_at.replace(tzinfo=timezone.utc)
                       if lead.updated_at.tzinfo is None
                       else now - lead.updated_at).days)
    recency_score = max(0.0, 30.0 * (1.0 - age_days / 30.0))

    # Engagement: log-scaled comment count (0–20 pts)
    import math
    comments = lead.extra.get("comments", 0) or 0
    engagement_score = min(20.0, 10.0 * math.log1p(comments))

    # Title quality: short/empty titles get 0; ~50-char titles get full marks
    title_len = len(lead.title)
    title_length_score = min(10.0, title_len / 5.0) if title_len > 0 else 0.0

    total = priority_score + recency_score + engagement_score + title_length_score
    return ScoreFactors(
        priority_score=round(priority_score, 2),
        recency_score=round(recency_score, 2),
        engagement_score=round(engagement_score, 2),
        title_length_score=round(title_length_score, 2),
        total=round(total, 2),
    )


# ---------------------------------------------------------------------------
# Ingestion pipeline
# ---------------------------------------------------------------------------


class IngestionPipeline:
    """Orchestrates fetch → dedup → score → persist for all connectors.

    Usage::

        pipeline = IngestionPipeline(store=store, registry=registry)
        new_leads = await pipeline.run_cycle(connector_id="github_issues")
    """

    def __init__(
        self,
        store: SqliteRunStore,
        registry: ConnectorRegistry,
        auto_create_missions: bool = False,
    ) -> None:
        self._store = store
        self._registry = registry
        self._auto_create_missions = auto_create_missions

    async def run_cycle(
        self,
        connector_id: str,
        limit: int = 200,
    ) -> List[LeadRecord]:
        """Run one ingestion cycle for a connector.

        Returns only the *new* leads (after dedup).
        """
        connector = self._registry.get(connector_id)
        if connector is None:
            raise ValueError(f"Unknown connector: {connector_id!r}")

        cursor = self._store.get_connector_cursor(connector_id)
        leads, next_cursor = await connector.fetch(cursor=cursor, limit=limit)
        logger.info(
            "pipeline: connector=%s fetched=%d",
            connector_id,
            len(leads),
        )

        new_leads: List[LeadRecord] = []
        for lead in leads:
            if self._store.lead_exists(lead.lead_id):
                continue  # deduplicated
            factors = score_lead(lead)
            self._store.upsert_lead(lead=lead, score=factors.total, score_factors_json=json.dumps({
                "priority": factors.priority_score,
                "recency": factors.recency_score,
                "engagement": factors.engagement_score,
                "title_length": factors.title_length_score,
                "total": factors.total,
            }))
            new_leads.append(lead)

        if next_cursor:
            self._store.save_connector_cursor(next_cursor)

        logger.info(
            "pipeline: connector=%s new_leads=%d (after dedup)",
            connector_id,
            len(new_leads),
        )

        if self._auto_create_missions and new_leads:
            self._create_missions_for_leads(new_leads)

        return new_leads

    async def run_all(self, limit: int = 200) -> Dict[str, List[LeadRecord]]:
        """Run a cycle for every registered connector."""
        results: Dict[str, List[LeadRecord]] = {}
        for connector in self._registry.all():
            try:
                leads = await self.run_cycle(connector.connector_id, limit=limit)
                results[connector.connector_id] = leads
            except Exception as exc:
                logger.exception(
                    "pipeline: connector=%s cycle failed: %s",
                    connector.connector_id,
                    exc,
                )
                results[connector.connector_id] = []
        return results

    def _create_missions_for_leads(self, leads: List[LeadRecord]) -> None:
        for lead in leads:
            try:
                self._store.create_mission(
                    title=lead.title[:200],
                    goal=f"Process lead from {lead.connector_id}: {lead.title}\n\nURL: {lead.url}\n\n{lead.body[:500]}",
                    context={"lead_id": lead.lead_id, "source_id": lead.source_id, "url": lead.url},
                )
            except Exception as exc:
                logger.warning("pipeline: failed to create mission for lead %s: %s", lead.lead_id, exc)
