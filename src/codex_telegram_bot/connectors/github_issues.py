"""GitHub issue queue ingestion connector (EPIC 7, issue #80).

Fetches open GitHub issues filtered by labels / milestone / assignee and
converts them into LeadRecords.  Supports incremental sync via a
``since`` ISO-timestamp cursor so re-runs are idempotent and only pull
new/updated issues.

The connector is intentionally decoupled from the GitHub token via an
injectable ``http_client`` so it can be unit-tested without network access.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlencode

from codex_telegram_bot.connectors.base import (
    Connector,
    IngestionCursor,
    LeadRecord,
    RateLimiter,
    make_lead_id,
)

logger = logging.getLogger(__name__)

CONNECTOR_ID = "github_issues"

# Priority mapping from GitHub label names (substring match)
_PRIORITY_LABELS: List[Tuple[str, int]] = [
    ("P0", 0),
    ("P1", 10),
    ("P2", 20),
    ("P3", 30),
    ("critical", 0),
    ("blocker", 0),
    ("high", 10),
    ("medium", 20),
    ("low", 30),
]

_DEFAULT_API_BASE = "https://api.github.com"


def _infer_priority(labels: List[str]) -> int:
    lower = [lb.lower() for lb in labels]
    for fragment, score in _PRIORITY_LABELS:
        if any(fragment.lower() in lb for lb in lower):
            return score
    return 50  # unknown → medium


# ---------------------------------------------------------------------------
# HTTP client type alias
# ---------------------------------------------------------------------------

# Signature: async (url, headers) -> (status_code, body_dict_or_list, headers_dict)
HttpGetFn = Callable[[str, Dict[str, str]], Any]


async def _default_http_get(
    url: str, headers: Dict[str, str]
) -> Tuple[int, Any, Dict[str, str]]:
    """Real aiohttp-based HTTP GET (used in production)."""
    try:
        import aiohttp  # type: ignore[import]
    except ImportError as exc:
        raise RuntimeError(
            "aiohttp is required for GitHubIssueConnector.  "
            "Install it with: pip install aiohttp"
        ) from exc
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers) as resp:
            body = await resp.json(content_type=None)
            return resp.status, body, dict(resp.headers)


# ---------------------------------------------------------------------------
# GitHubIssueConnector
# ---------------------------------------------------------------------------


class GitHubIssueConnector:
    """Ingests open GitHub issues from a single repository.

    Args:
        repo: ``"owner/repo"`` slug.
        token: Optional personal-access-token (adds ``Authorization`` header).
        labels: Comma-separated label filter (e.g. ``"bug,help wanted"``).
        milestone: Milestone number (int) or ``"*"`` for any milestone.
        state: Issue state, ``"open"`` (default), ``"closed"``, or ``"all"``.
        per_page: Page size (max 100).
        rate_per_sec: API calls allowed per second.
        http_get: Injectable HTTP function for testing.
    """

    connector_id: str = CONNECTOR_ID
    display_name: str = "GitHub Issue Queue"

    def __init__(
        self,
        repo: str,
        token: Optional[str] = None,
        labels: Optional[str] = None,
        milestone: Optional[str] = None,
        state: str = "open",
        per_page: int = 100,
        rate_per_sec: float = 1.5,
        api_base: str = _DEFAULT_API_BASE,
        http_get: Optional[HttpGetFn] = None,
    ) -> None:
        self._repo = repo
        self._token = token
        self._labels = labels
        self._milestone = milestone
        self._state = state
        self._per_page = min(per_page, 100)
        self._api_base = api_base.rstrip("/")
        self._http_get: HttpGetFn = http_get or _default_http_get
        self._rate = RateLimiter(rate_per_sec=rate_per_sec, burst=5)

    @property
    def connector_id(self) -> str:  # type: ignore[override]
        return CONNECTOR_ID

    @property
    def display_name(self) -> str:  # type: ignore[override]
        return "GitHub Issue Queue"

    # ------------------------------------------------------------------
    # Connector protocol implementation
    # ------------------------------------------------------------------

    async def fetch(
        self,
        cursor: Optional[IngestionCursor],
        limit: int = 200,
    ) -> Tuple[List[LeadRecord], Optional[IngestionCursor]]:
        """Fetch issues updated since the cursor.

        Returns ``(leads, next_cursor)``.
        """
        since = cursor.value if cursor else None
        leads: List[LeadRecord] = []
        page = 1
        latest_updated_at: Optional[str] = None

        while len(leads) < limit:
            await self._rate.wait_and_consume()
            params: Dict[str, str] = {
                "state": self._state,
                "sort": "updated",
                "direction": "desc",
                "per_page": str(self._per_page),
                "page": str(page),
            }
            if since:
                params["since"] = since
            if self._labels:
                params["labels"] = self._labels
            if self._milestone:
                params["milestone"] = str(self._milestone)

            url = f"{self._api_base}/repos/{self._repo}/issues?{urlencode(params)}"
            status, body, _headers = await self._http_get(url, self._build_headers())

            if status == 304:  # Not Modified (etag / conditional GET)
                break
            if status != 200:
                logger.warning(
                    "github_issues: HTTP %d fetching %s: %s", status, url, str(body)[:200]
                )
                break
            if not isinstance(body, list) or not body:
                break

            for raw in body:
                if not isinstance(raw, dict):
                    continue
                # Skip pull requests (GitHub returns them in the issues endpoint)
                if raw.get("pull_request"):
                    continue
                lead = self._to_lead(raw)
                leads.append(lead)
                # Track the latest updated_at for the next cursor
                ua = raw.get("updated_at") or ""
                if ua and (latest_updated_at is None or ua > latest_updated_at):
                    latest_updated_at = ua

            if len(body) < self._per_page:
                break  # last page
            page += 1

        next_cursor: Optional[IngestionCursor] = None
        if latest_updated_at:
            next_cursor = IngestionCursor(
                connector_id=CONNECTOR_ID,
                value=latest_updated_at,
                updated_at=datetime.now(timezone.utc),
            )

        logger.info(
            "github_issues: fetched %d leads from %s (page=%d)",
            len(leads),
            self._repo,
            page,
        )
        return leads[:limit], next_cursor

    async def health(self) -> Dict[str, Any]:
        try:
            url = f"{self._api_base}/repos/{self._repo}"
            status, body, _ = await self._http_get(url, self._build_headers())
            return {
                "ok": status == 200,
                "status_code": status,
                "repo": self._repo,
            }
        except Exception as exc:
            return {"ok": False, "error": str(exc), "repo": self._repo}

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_headers(self) -> Dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    def _to_lead(self, raw: Dict[str, Any]) -> LeadRecord:
        issue_number = str(raw.get("number", ""))
        labels = [lb["name"] for lb in raw.get("labels", []) if isinstance(lb, dict)]
        created = _parse_gh_dt(raw.get("created_at"))
        updated = _parse_gh_dt(raw.get("updated_at")) or created

        return LeadRecord(
            lead_id=make_lead_id(CONNECTOR_ID, issue_number),
            connector_id=CONNECTOR_ID,
            source_id=issue_number,
            title=str(raw.get("title") or "").strip(),
            body=str(raw.get("body") or "").strip(),
            url=str(raw.get("html_url") or ""),
            priority=_infer_priority(labels),
            labels=labels,
            created_at=created,
            updated_at=updated,
            extra={
                "repo": self._repo,
                "state": raw.get("state", ""),
                "assignees": [a["login"] for a in raw.get("assignees", []) if isinstance(a, dict)],
                "milestone": (raw.get("milestone") or {}).get("title"),
                "comments": raw.get("comments", 0),
            },
        )


def _parse_gh_dt(value: Optional[str]) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    try:
        # GitHub returns ISO 8601 with Z suffix
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(timezone.utc)
