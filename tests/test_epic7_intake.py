"""Tests for EPIC 7: Work Intake and Connectors.

Covers:
  #80 – GitHub issue queue ingestion
  #81 – Lead source connector framework
  #82 – Dedup + scoring pipeline
  #83 – Outbound action tools with safeguards
"""
from __future__ import annotations

import asyncio
import json
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import AsyncMock, MagicMock, patch

from codex_telegram_bot.connectors.base import (
    ConnectorRegistry,
    IngestionCursor,
    LeadRecord,
    RateLimiter,
    make_lead_id,
)
from codex_telegram_bot.connectors.github_issues import (
    GitHubIssueConnector,
    _infer_priority,
    _parse_gh_dt,
)
from codex_telegram_bot.connectors.pipeline import IngestionPipeline, ScoreFactors, score_lead
from codex_telegram_bot.persistence.sqlite_store import SqliteRunStore
from codex_telegram_bot.tools.base import ToolContext, ToolRequest
from codex_telegram_bot.tools.outbound import (
    GitHubCloseIssueTool,
    GitHubCommentTool,
    GitHubCreateIssueTool,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_store(tmp: str) -> SqliteRunStore:
    return SqliteRunStore(Path(tmp) / "test.db")


def _make_context(policy_profile: str = "balanced") -> ToolContext:
    ctx = ToolContext(workspace_root=Path("/tmp"))
    object.__setattr__(ctx, "policy_profile", policy_profile)
    return ctx


def _make_lead(
    source_id: str = "1",
    title: str = "Test issue",
    priority: int = 20,
    labels: Optional[List[str]] = None,
    comments: int = 0,
) -> LeadRecord:
    return LeadRecord(
        lead_id=make_lead_id("test_connector", source_id),
        connector_id="test_connector",
        source_id=source_id,
        title=title,
        body="body text",
        url=f"https://example.com/issues/{source_id}",
        priority=priority,
        labels=labels or [],
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        extra={"comments": comments},
    )


# ---------------------------------------------------------------------------
# #81 – Lead source connector framework
# ---------------------------------------------------------------------------


class TestLeadRecord(unittest.TestCase):
    def test_make_lead_id_stable(self):
        a = make_lead_id("github_issues", "123")
        b = make_lead_id("github_issues", "123")
        self.assertEqual(a, b)

    def test_make_lead_id_distinct(self):
        a = make_lead_id("github_issues", "123")
        b = make_lead_id("github_issues", "124")
        self.assertNotEqual(a, b)

    def test_lead_record_equality_by_id(self):
        l1 = _make_lead("1")
        l2 = _make_lead("1")
        self.assertEqual(l1, l2)

    def test_lead_record_hashable(self):
        leads = {_make_lead("1"), _make_lead("2"), _make_lead("1")}
        self.assertEqual(len(leads), 2)


class TestRateLimiter(unittest.TestCase):
    def test_consumes_within_burst(self):
        rl = RateLimiter(rate_per_sec=10.0, burst=5)
        for _ in range(5):
            self.assertTrue(rl.consume())

    def test_blocks_when_empty(self):
        rl = RateLimiter(rate_per_sec=1.0, burst=1)
        self.assertTrue(rl.consume())
        self.assertFalse(rl.consume())  # bucket empty

    def test_refills_over_time(self):
        rl = RateLimiter(rate_per_sec=100.0, burst=1)
        rl.consume()
        time.sleep(0.02)
        self.assertTrue(rl.consume())


class TestConnectorRegistry(unittest.TestCase):
    def test_register_and_get(self):
        registry = ConnectorRegistry()

        class FakeConnector:
            connector_id = "fake"
            display_name = "Fake"

        c = FakeConnector()
        registry.register(c)
        self.assertIs(registry.get("fake"), c)

    def test_get_unknown_returns_none(self):
        registry = ConnectorRegistry()
        self.assertIsNone(registry.get("nope"))

    def test_ids_sorted(self):
        registry = ConnectorRegistry()

        for cid in ["b", "a", "c"]:
            class C:
                connector_id = cid
                display_name = cid

            registry.register(C())
        self.assertEqual(registry.ids(), ["a", "b", "c"])

    def test_register_empty_id_raises(self):
        registry = ConnectorRegistry()

        class Bad:
            connector_id = ""
            display_name = "bad"

        with self.assertRaises(ValueError):
            registry.register(Bad())


# ---------------------------------------------------------------------------
# #80 – GitHub issue queue ingestion
# ---------------------------------------------------------------------------


def _fake_issues_response(n: int = 2) -> List[Dict]:
    issues = []
    for i in range(1, n + 1):
        issues.append({
            "number": i,
            "title": f"Issue {i}",
            "body": f"Body of issue {i}",
            "html_url": f"https://github.com/owner/repo/issues/{i}",
            "state": "open",
            "labels": [{"name": "P1"}, {"name": "bug"}],
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-10T00:00:00Z",
            "assignees": [],
            "comments": 3,
        })
    return issues


class TestGitHubIssueConnector(unittest.IsolatedAsyncioTestCase):
    def _make_connector(self, http_responses: List) -> GitHubIssueConnector:
        call_index = [0]

        async def fake_http(url: str, headers: Dict) -> Tuple[int, Any, Dict]:
            idx = call_index[0]
            call_index[0] += 1
            if idx < len(http_responses):
                return http_responses[idx]
            return (200, [], {})

        return GitHubIssueConnector(
            repo="owner/repo",
            token="fake-token",
            http_get=fake_http,
        )

    async def test_fetch_returns_leads(self):
        connector = self._make_connector([(200, _fake_issues_response(3), {})])
        leads, cursor = await connector.fetch(cursor=None, limit=100)
        self.assertEqual(len(leads), 3)
        self.assertIsNotNone(cursor)
        self.assertEqual(leads[0].connector_id, "github_issues")

    async def test_fetch_skips_pull_requests(self):
        issues = _fake_issues_response(2)
        issues[0]["pull_request"] = {"url": "https://..."}
        connector = self._make_connector([(200, issues, {})])
        leads, _ = await connector.fetch(cursor=None)
        self.assertEqual(len(leads), 1)

    async def test_fetch_with_cursor_uses_since(self):
        calls: List[str] = []

        async def fake_http(url: str, headers: Dict) -> Tuple[int, Any, Dict]:
            calls.append(url)
            return (200, _fake_issues_response(1), {})

        connector = GitHubIssueConnector(repo="owner/repo", http_get=fake_http)
        cursor = IngestionCursor("github_issues", "2026-01-05T00:00:00+00:00", datetime.now(timezone.utc))
        await connector.fetch(cursor=cursor)
        self.assertIn("since=", calls[0])

    async def test_fetch_non_200_returns_empty(self):
        connector = self._make_connector([(403, {"message": "Forbidden"}, {})])
        leads, cursor = await connector.fetch(cursor=None)
        self.assertEqual(leads, [])
        self.assertIsNone(cursor)

    async def test_health_ok(self):
        connector = self._make_connector([(200, {"full_name": "owner/repo"}, {})])
        result = await connector.health()
        self.assertTrue(result["ok"])

    async def test_health_fail(self):
        connector = self._make_connector([(404, {}, {})])
        result = await connector.health()
        self.assertFalse(result["ok"])

    def test_infer_priority_p0(self):
        self.assertEqual(_infer_priority(["P0", "bug"]), 0)

    def test_infer_priority_default(self):
        self.assertEqual(_infer_priority(["help wanted"]), 50)


class TestParseDatetime(unittest.TestCase):
    def test_valid_github_date(self):
        dt = _parse_gh_dt("2026-01-15T10:30:00Z")
        self.assertEqual(dt.year, 2026)

    def test_none_returns_now(self):
        dt = _parse_gh_dt(None)
        self.assertIsNotNone(dt)


# ---------------------------------------------------------------------------
# #82 – Dedup + scoring pipeline
# ---------------------------------------------------------------------------


class TestScoreLead(unittest.TestCase):
    def test_high_priority_scores_higher(self):
        high = _make_lead("1", priority=0)   # P0
        low = _make_lead("2", priority=100)  # worst
        self.assertGreater(score_lead(high).total, score_lead(low).total)

    def test_comments_boost_score(self):
        base = _make_lead("1", comments=0)
        active = _make_lead("2", comments=50)
        self.assertGreater(score_lead(active).engagement_score, score_lead(base).engagement_score)

    def test_good_title_boosts_score(self):
        short = _make_lead("1", title="x")
        full = _make_lead("2", title="Fix authentication bug in login endpoint")
        self.assertGreater(score_lead(full).title_length_score, score_lead(short).title_length_score)

    def test_factors_sum_to_total(self):
        f = score_lead(_make_lead("1"))
        expected = round(f.priority_score + f.recency_score + f.engagement_score + f.title_length_score, 2)
        self.assertAlmostEqual(f.total, expected, places=1)

    def test_empty_title_zero_title_score(self):
        lead = _make_lead("1", title="")
        self.assertEqual(score_lead(lead).title_length_score, 0.0)


class TestIngestionPipelinePersistence(unittest.TestCase):
    def test_upsert_and_get_lead(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            lead = _make_lead("42")
            store.upsert_lead(lead, score=75.0)
            row = store.get_lead(lead.lead_id)
            self.assertIsNotNone(row)
            self.assertEqual(row["source_id"], "42")
            self.assertAlmostEqual(row["score"], 75.0)

    def test_lead_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            lead = _make_lead("1")
            self.assertFalse(store.lead_exists(lead.lead_id))
            store.upsert_lead(lead)
            self.assertTrue(store.lead_exists(lead.lead_id))

    def test_list_leads_by_connector(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            for i in range(3):
                store.upsert_lead(_make_lead(str(i)), score=float(i * 10))
            rows = store.list_leads(connector_id="test_connector")
            self.assertEqual(len(rows), 3)
            # Sorted by score descending
            self.assertGreaterEqual(rows[0]["score"], rows[1]["score"])

    def test_upsert_updates_existing(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            lead = _make_lead("1", title="Original")
            store.upsert_lead(lead, score=10.0)
            updated = LeadRecord(
                lead_id=lead.lead_id,
                connector_id=lead.connector_id,
                source_id=lead.source_id,
                title="Updated",
                body=lead.body,
                url=lead.url,
                priority=lead.priority,
                labels=lead.labels,
                created_at=lead.created_at,
                updated_at=lead.updated_at,
                extra=lead.extra,
            )
            store.upsert_lead(updated, score=99.0)
            row = store.get_lead(lead.lead_id)
            self.assertEqual(row["title"], "Updated")
            self.assertAlmostEqual(row["score"], 99.0)

    def test_connector_cursor_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            self.assertIsNone(store.get_connector_cursor("github_issues"))
            cursor = IngestionCursor("github_issues", "2026-01-10T00:00:00+00:00", datetime.now(timezone.utc))
            store.save_connector_cursor(cursor)
            loaded = store.get_connector_cursor("github_issues")
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded.value, "2026-01-10T00:00:00+00:00")

    def test_connector_cursor_upsert(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            for val in ["2026-01-01T00:00:00+00:00", "2026-01-20T00:00:00+00:00"]:
                store.save_connector_cursor(IngestionCursor("c1", val, datetime.now(timezone.utc)))
            loaded = store.get_connector_cursor("c1")
            self.assertEqual(loaded.value, "2026-01-20T00:00:00+00:00")


class TestIngestionPipelineAsync(unittest.IsolatedAsyncioTestCase):
    async def test_run_cycle_deduplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            leads_to_return = [_make_lead("1"), _make_lead("2")]

            class FakeConnector:
                connector_id = "test_connector"
                display_name = "Test"

                async def fetch(self, cursor, limit=200):
                    return leads_to_return, None

                async def health(self):
                    return {"ok": True}

            registry = ConnectorRegistry()
            registry.register(FakeConnector())
            pipeline = IngestionPipeline(store=store, registry=registry)

            first = await pipeline.run_cycle("test_connector")
            self.assertEqual(len(first), 2)
            # Second cycle: same leads → all deduplicated
            second = await pipeline.run_cycle("test_connector")
            self.assertEqual(len(second), 0)

    async def test_run_cycle_saves_cursor(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            cursor_out = IngestionCursor("test_connector", "2026-02-01T00:00:00+00:00", datetime.now(timezone.utc))

            class FakeConnector:
                connector_id = "test_connector"
                display_name = "Test"

                async def fetch(self, cursor, limit=200):
                    return [], cursor_out

                async def health(self):
                    return {"ok": True}

            registry = ConnectorRegistry()
            registry.register(FakeConnector())
            pipeline = IngestionPipeline(store=store, registry=registry)
            await pipeline.run_cycle("test_connector")
            saved = store.get_connector_cursor("test_connector")
            self.assertIsNotNone(saved)
            self.assertEqual(saved.value, cursor_out.value)

    async def test_run_cycle_unknown_connector_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            registry = ConnectorRegistry()
            pipeline = IngestionPipeline(store=store, registry=registry)
            with self.assertRaises(ValueError):
                await pipeline.run_cycle("does_not_exist")

    async def test_run_all_returns_per_connector(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)

            class FakeConnector:
                connector_id = "c1"
                display_name = "C1"

                async def fetch(self, cursor, limit=200):
                    return [_make_lead("99", )], None

                async def health(self):
                    return {"ok": True}

            registry = ConnectorRegistry()
            registry.register(FakeConnector())
            pipeline = IngestionPipeline(store=store, registry=registry)
            results = await pipeline.run_all()
            self.assertIn("c1", results)
            self.assertEqual(len(results["c1"]), 1)


# ---------------------------------------------------------------------------
# #83 – Outbound action tools with safeguards
# ---------------------------------------------------------------------------


class TestGitHubCommentTool(unittest.IsolatedAsyncioTestCase):
    async def test_dry_run_returns_preview(self):
        tool = GitHubCommentTool()
        req = ToolRequest(name="github_comment", args={"repo": "o/r", "issue": 1, "body": "hello", "dry_run": True})
        result = await tool.arun(req, _make_context())
        self.assertTrue(result.ok)
        self.assertIn("[dry_run]", result.output)

    async def test_strict_policy_blocks_write(self):
        tool = GitHubCommentTool()
        req = ToolRequest(name="github_comment", args={"repo": "o/r", "issue": 1, "body": "hello"})
        result = await tool.arun(req, _make_context("strict"))
        self.assertFalse(result.ok)
        self.assertIn("strict", result.output)

    async def test_missing_required_args(self):
        tool = GitHubCommentTool()
        req = ToolRequest(name="github_comment", args={"repo": "o/r"})
        result = await tool.arun(req, _make_context())
        self.assertFalse(result.ok)
        self.assertIn("Error:", result.output)

    async def test_post_comment_success(self):
        async def fake_post(url, headers, body):
            return (201, {"html_url": "https://github.com/o/r/issues/1#comment-1"})

        tool = GitHubCommentTool(http_post_fn=fake_post)
        req = ToolRequest(name="github_comment", args={"repo": "o/r", "issue": 1, "body": "LGTM"})
        result = await tool.arun(req, _make_context())
        self.assertTrue(result.ok)
        self.assertIn("Comment posted", result.output)

    async def test_post_comment_api_error(self):
        async def fake_post(url, headers, body):
            return (422, {"message": "Validation Failed"})

        tool = GitHubCommentTool(http_post_fn=fake_post)
        req = ToolRequest(name="github_comment", args={"repo": "o/r", "issue": 1, "body": "x"})
        result = await tool.arun(req, _make_context())
        self.assertFalse(result.ok)
        self.assertIn("422", result.output)


class TestGitHubCloseIssueTool(unittest.IsolatedAsyncioTestCase):
    async def test_dry_run(self):
        tool = GitHubCloseIssueTool()
        req = ToolRequest(name="github_close_issue", args={"repo": "o/r", "issue": 5, "dry_run": True})
        result = await tool.arun(req, _make_context())
        self.assertTrue(result.ok)
        self.assertIn("[dry_run]", result.output)

    async def test_strict_blocks(self):
        tool = GitHubCloseIssueTool()
        req = ToolRequest(name="github_close_issue", args={"repo": "o/r", "issue": 5})
        result = await tool.arun(req, _make_context("strict"))
        self.assertFalse(result.ok)

    async def test_close_success(self):
        async def fake_patch(url, headers, body):
            return (200, {"html_url": "https://github.com/o/r/issues/5", "state": "closed"})

        tool = GitHubCloseIssueTool(http_patch_fn=fake_patch)
        req = ToolRequest(name="github_close_issue", args={"repo": "o/r", "issue": 5})
        result = await tool.arun(req, _make_context())
        self.assertTrue(result.ok)
        self.assertIn("closed", result.output)

    async def test_missing_args(self):
        tool = GitHubCloseIssueTool()
        req = ToolRequest(name="github_close_issue", args={})
        result = await tool.arun(req, _make_context())
        self.assertFalse(result.ok)


class TestGitHubCreateIssueTool(unittest.IsolatedAsyncioTestCase):
    async def test_dry_run(self):
        tool = GitHubCreateIssueTool()
        req = ToolRequest(
            name="github_create_issue",
            args={"repo": "o/r", "title": "New bug", "body": "desc", "dry_run": True},
        )
        result = await tool.arun(req, _make_context())
        self.assertTrue(result.ok)
        self.assertIn("[dry_run]", result.output)

    async def test_create_success(self):
        async def fake_post(url, headers, body):
            return (201, {"html_url": "https://github.com/o/r/issues/99", "number": 99})

        tool = GitHubCreateIssueTool(http_post_fn=fake_post)
        req = ToolRequest(
            name="github_create_issue",
            args={"repo": "o/r", "title": "Bug report", "labels": ["bug"]},
        )
        result = await tool.arun(req, _make_context())
        self.assertTrue(result.ok)
        self.assertIn("#99", result.output)

    async def test_strict_blocks(self):
        tool = GitHubCreateIssueTool()
        req = ToolRequest(name="github_create_issue", args={"repo": "o/r", "title": "x"})
        result = await tool.arun(req, _make_context("strict"))
        self.assertFalse(result.ok)

    async def test_missing_title_fails(self):
        tool = GitHubCreateIssueTool()
        req = ToolRequest(name="github_create_issue", args={"repo": "o/r"})
        result = await tool.arun(req, _make_context())
        self.assertFalse(result.ok)

    async def test_trusted_policy_allows_write(self):
        async def fake_post(url, headers, body):
            return (201, {"html_url": "https://...", "number": 7})

        tool = GitHubCreateIssueTool(http_post_fn=fake_post)
        req = ToolRequest(name="github_create_issue", args={"repo": "o/r", "title": "Test"})
        result = await tool.arun(req, _make_context("trusted"))
        self.assertTrue(result.ok)
