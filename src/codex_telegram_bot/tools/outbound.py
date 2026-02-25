"""Outbound action tools with safeguards (EPIC 7, issue #83).

Provides explicit tools for writing back to external services (GitHub).
All write operations are:
  - Policy-gated: rejected when the execution policy profile is "strict".
  - Dry-run capable: set ``dry_run=True`` in args to preview without
    actually executing.
  - Logged: every invocation records an audit line in the tool output.

Tools implemented:
  - github_comment      – post a comment on an issue/PR
  - github_close_issue  – close an issue
  - github_create_issue – create a new issue

The underlying HTTP calls are injectable for testing (``http_post_fn`` /
``http_patch_fn`` constructor args on GitHubToolBase).
"""
from __future__ import annotations

import json
import logging
from typing import Any, Callable, Dict, Optional, Tuple

from codex_telegram_bot.tools.base import ToolContext, ToolRequest, ToolResult

logger = logging.getLogger(__name__)

_DEFAULT_API_BASE = "https://api.github.com"

# Policy profiles that may NOT execute write operations.
_BLOCKED_PROFILES = {"strict"}

# Type alias for injectable HTTP functions.
HttpWriteFn = Callable[[str, Dict[str, str], Dict[str, Any]], Any]


async def _default_http_post(
    url: str, headers: Dict[str, str], body: Dict[str, Any]
) -> Tuple[int, Any]:
    try:
        import aiohttp  # type: ignore[import]
    except ImportError as exc:
        raise RuntimeError("aiohttp required for outbound tools.") from exc
    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=headers, json=body) as resp:
            data = await resp.json(content_type=None)
            return resp.status, data


async def _default_http_patch(
    url: str, headers: Dict[str, str], body: Dict[str, Any]
) -> Tuple[int, Any]:
    try:
        import aiohttp  # type: ignore[import]
    except ImportError as exc:
        raise RuntimeError("aiohttp required for outbound tools.") from exc
    async with aiohttp.ClientSession() as session:
        async with session.patch(url, headers=headers, json=body) as resp:
            data = await resp.json(content_type=None)
            return resp.status, data


# ---------------------------------------------------------------------------
# Shared base
# ---------------------------------------------------------------------------


class GitHubToolBase:
    """Common helpers shared by all GitHub outbound tools."""

    def __init__(
        self,
        token: Optional[str] = None,
        api_base: str = _DEFAULT_API_BASE,
        http_post_fn: Optional[HttpWriteFn] = None,
        http_patch_fn: Optional[HttpWriteFn] = None,
    ) -> None:
        self._token = token
        self._api_base = api_base.rstrip("/")
        self._http_post = http_post_fn or _default_http_post
        self._http_patch = http_patch_fn or _default_http_patch

    def _headers(self) -> Dict[str, str]:
        h = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
        }
        if self._token:
            h["Authorization"] = f"Bearer {self._token}"
        return h

    @staticmethod
    def _is_blocked(context: ToolContext) -> bool:
        profile = getattr(context, "policy_profile", "") or ""
        return profile in _BLOCKED_PROFILES

    @staticmethod
    def _dry_run(request: ToolRequest) -> bool:
        return bool(request.args.get("dry_run", False))


# ---------------------------------------------------------------------------
# github_comment
# ---------------------------------------------------------------------------


class GitHubCommentTool(GitHubToolBase):
    """Post a comment on a GitHub issue or pull request.

    Required args:
      repo (str)    – "owner/repo"
      issue (int)   – issue or PR number
      body (str)    – comment text (markdown)

    Optional args:
      dry_run (bool) – if True, return what would be posted without calling API
    """

    name = "github_comment"

    def run(self, request: ToolRequest, context: ToolContext) -> ToolResult:
        raise NotImplementedError("Use arun() for async outbound tools.")

    async def arun(self, request: ToolRequest, context: ToolContext) -> ToolResult:
        if self._is_blocked(context):
            return ToolResult(ok=False, output="Error: write operations are blocked by policy profile 'strict'.")
        repo = str(request.args.get("repo") or "")
        issue = request.args.get("issue")
        body = str(request.args.get("body") or "").strip()
        if not repo or issue is None or not body:
            return ToolResult(ok=False, output="Error: 'repo', 'issue', and 'body' are required.")

        if self._dry_run(request):
            return ToolResult(
                ok=True,
                output=f"[dry_run] Would post comment on {repo}#{issue}:\n{body}",
            )

        url = f"{self._api_base}/repos/{repo}/issues/{issue}/comments"
        try:
            status, data = await self._http_post(url, self._headers(), {"body": body})
        except Exception as exc:
            return ToolResult(ok=False, output=f"Error: HTTP call failed: {exc}")

        if status in (200, 201):
            comment_url = data.get("html_url", "") if isinstance(data, dict) else ""
            return ToolResult(ok=True, output=f"Comment posted: {comment_url}")
        return ToolResult(ok=False, output=f"Error: GitHub API returned {status}: {json.dumps(data)[:200]}")


# ---------------------------------------------------------------------------
# github_close_issue
# ---------------------------------------------------------------------------


class GitHubCloseIssueTool(GitHubToolBase):
    """Close a GitHub issue.

    Required args:
      repo (str)   – "owner/repo"
      issue (int)  – issue number

    Optional args:
      reason (str)  – "completed" (default) or "not_planned"
      dry_run (bool)
    """

    name = "github_close_issue"

    def run(self, request: ToolRequest, context: ToolContext) -> ToolResult:
        raise NotImplementedError("Use arun() for async outbound tools.")

    async def arun(self, request: ToolRequest, context: ToolContext) -> ToolResult:
        if self._is_blocked(context):
            return ToolResult(ok=False, output="Error: write operations are blocked by policy profile 'strict'.")
        repo = str(request.args.get("repo") or "")
        issue = request.args.get("issue")
        reason = str(request.args.get("reason") or "completed")
        if reason not in ("completed", "not_planned"):
            reason = "completed"
        if not repo or issue is None:
            return ToolResult(ok=False, output="Error: 'repo' and 'issue' are required.")

        if self._dry_run(request):
            return ToolResult(
                ok=True,
                output=f"[dry_run] Would close {repo}#{issue} (reason={reason})",
            )

        url = f"{self._api_base}/repos/{repo}/issues/{issue}"
        payload: Dict[str, Any] = {"state": "closed", "state_reason": reason}
        try:
            status, data = await self._http_patch(url, self._headers(), payload)
        except Exception as exc:
            return ToolResult(ok=False, output=f"Error: HTTP call failed: {exc}")

        if status == 200:
            issue_url = data.get("html_url", "") if isinstance(data, dict) else ""
            return ToolResult(ok=True, output=f"Issue closed: {issue_url}")
        return ToolResult(ok=False, output=f"Error: GitHub API returned {status}: {json.dumps(data)[:200]}")


# ---------------------------------------------------------------------------
# github_create_issue
# ---------------------------------------------------------------------------


class GitHubCreateIssueTool(GitHubToolBase):
    """Create a new GitHub issue.

    Required args:
      repo (str)    – "owner/repo"
      title (str)   – issue title

    Optional args:
      body (str)       – issue body (markdown)
      labels (list)    – list of label names
      assignees (list) – list of GitHub usernames
      milestone (int)  – milestone number
      dry_run (bool)
    """

    name = "github_create_issue"

    def run(self, request: ToolRequest, context: ToolContext) -> ToolResult:
        raise NotImplementedError("Use arun() for async outbound tools.")

    async def arun(self, request: ToolRequest, context: ToolContext) -> ToolResult:
        if self._is_blocked(context):
            return ToolResult(ok=False, output="Error: write operations are blocked by policy profile 'strict'.")
        repo = str(request.args.get("repo") or "")
        title = str(request.args.get("title") or "").strip()
        if not repo or not title:
            return ToolResult(ok=False, output="Error: 'repo' and 'title' are required.")

        payload: Dict[str, Any] = {"title": title}
        body = str(request.args.get("body") or "").strip()
        if body:
            payload["body"] = body
        labels = request.args.get("labels")
        if labels:
            payload["labels"] = list(labels) if not isinstance(labels, str) else [labels]
        assignees = request.args.get("assignees")
        if assignees:
            payload["assignees"] = list(assignees) if not isinstance(assignees, str) else [assignees]
        milestone = request.args.get("milestone")
        if milestone is not None:
            payload["milestone"] = int(milestone)

        if self._dry_run(request):
            return ToolResult(
                ok=True,
                output=f"[dry_run] Would create issue in {repo}:\n{json.dumps(payload, indent=2)}",
            )

        url = f"{self._api_base}/repos/{repo}/issues"
        try:
            status, data = await self._http_post(url, self._headers(), payload)
        except Exception as exc:
            return ToolResult(ok=False, output=f"Error: HTTP call failed: {exc}")

        if status == 201:
            issue_url = data.get("html_url", "") if isinstance(data, dict) else ""
            issue_num = data.get("number", "?") if isinstance(data, dict) else "?"
            return ToolResult(ok=True, output=f"Issue created: #{issue_num} {issue_url}")
        return ToolResult(ok=False, output=f"Error: GitHub API returned {status}: {json.dumps(data)[:200]}")
