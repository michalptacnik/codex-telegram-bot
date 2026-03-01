from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from typing import Any, Dict, List

from codex_telegram_bot.tools.base import ToolContext, ToolRequest, ToolResult


def web_search_tool_enabled(env: Dict[str, str] | None = None) -> bool:
    source = env if env is not None else os.environ
    raw = str(source.get("ENABLE_WEB_SEARCH_TOOL", "1") or "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _duckduckgo_search(query: str, timeout_sec: int) -> Dict[str, Any]:
    params = {
        "q": query,
        "format": "json",
        "no_html": "1",
        "skip_disambig": "1",
        "no_redirect": "1",
    }
    url = "https://api.duckduckgo.com/?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "codex-telegram-bot-web-search/1.0",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=max(1, timeout_sec)) as resp:
        blob = resp.read()
    parsed = json.loads(blob.decode("utf-8", errors="replace"))
    return parsed if isinstance(parsed, dict) else {}


def _flatten_related_topics(raw: Any) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    if not isinstance(raw, list):
        return out
    for item in raw:
        if isinstance(item, dict) and isinstance(item.get("Topics"), list):
            out.extend(_flatten_related_topics(item.get("Topics")))
            continue
        if not isinstance(item, dict):
            continue
        text = str(item.get("Text") or "").strip()
        url = str(item.get("FirstURL") or "").strip()
        if text or url:
            out.append({"title": text.split(" - ", 1)[0].strip() or text, "url": url, "snippet": text})
    return out


def _normalize_results(payload: Dict[str, Any], k: int) -> List[Dict[str, str]]:
    rows: List[Dict[str, str]] = []
    abstract = str(payload.get("AbstractText") or "").strip()
    abstract_url = str(payload.get("AbstractURL") or "").strip()
    heading = str(payload.get("Heading") or "").strip()
    if abstract and abstract_url:
        rows.append(
            {
                "title": heading or "DuckDuckGo Instant Answer",
                "url": abstract_url,
                "snippet": abstract,
            }
        )
    for item in _flatten_related_topics(payload.get("RelatedTopics")):
        if len(rows) >= k:
            break
        if not item.get("url"):
            continue
        rows.append(item)
    dedup: List[Dict[str, str]] = []
    seen = set()
    for row in rows:
        key = (row.get("url") or "").strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        dedup.append(row)
        if len(dedup) >= k:
            break
    return dedup


class WebSearchTool:
    name = "web_search"
    description = "Search the public web and return source URLs with snippets."

    def __init__(self, fetch_fn=_duckduckgo_search) -> None:
        self._fetch_fn = fetch_fn

    def run(self, request: ToolRequest, context: ToolContext) -> ToolResult:
        if not web_search_tool_enabled():
            return ToolResult(
                ok=False,
                output=(
                    "Error: web_search is disabled (ENABLE_WEB_SEARCH_TOOL=0). "
                    "Enable it or provide a local dataset."
                ),
            )
        query = str(request.args.get("query") or "").strip()
        if not query:
            return ToolResult(ok=False, output="Error: query is required.")
        try:
            k = int(request.args.get("k") or 5)
        except Exception:
            k = 5
        k = max(1, min(k, 10))
        try:
            timeout_sec = int(request.args.get("timeout_sec") or request.args.get("timeoutSec") or 15)
        except Exception:
            timeout_sec = 15
        timeout_sec = max(1, min(timeout_sec, 60))

        try:
            payload = self._fetch_fn(query, timeout_sec)
        except Exception as exc:
            return ToolResult(
                ok=False,
                output=(
                    "Error: web search failed. "
                    f"{exc}"
                ),
            )

        rows = _normalize_results(payload, k=k)
        if not rows:
            return ToolResult(ok=True, output=f"No web results found for: {query}")

        lines = [f'Web results for "{query}" (source: DuckDuckGo):']
        for idx, row in enumerate(rows, start=1):
            title = (row.get("title") or "").strip() or row.get("url") or "(untitled)"
            url = (row.get("url") or "").strip()
            snippet = (row.get("snippet") or "").strip()
            if len(snippet) > 240:
                snippet = snippet[:240].rstrip() + "..."
            lines.append(f"{idx}. {title}")
            if url:
                lines.append(f"   {url}")
            if snippet:
                lines.append(f"   {snippet}")
        return ToolResult(ok=True, output="\n".join(lines))
