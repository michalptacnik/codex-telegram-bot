from __future__ import annotations

import json
import os
import ipaddress
import socket
from html.parser import HTMLParser
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Sequence, Tuple

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


class _ReadableHtmlParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_stack: List[str] = []
        self._title_capture = False
        self.title = ""
        self.text_parts: List[str] = []
        self._skip_tags = {"script", "style", "noscript", "nav", "svg", "footer", "header"}

    def handle_starttag(self, tag: str, attrs: Sequence[Tuple[str, Optional[str]]]) -> None:
        t = (tag or "").lower()
        if t == "title":
            self._title_capture = True
            return
        if t in self._skip_tags:
            self._skip_stack.append(t)
            return
        attrs_map = {str(k or "").lower(): str(v or "").lower() for k, v in attrs}
        role = attrs_map.get("role", "")
        klass = attrs_map.get("class", "")
        if role in {"navigation", "banner", "contentinfo"} or "nav" in klass:
            self._skip_stack.append(t or "div")

    def handle_endtag(self, tag: str) -> None:
        t = (tag or "").lower()
        if t == "title":
            self._title_capture = False
            return
        if self._skip_stack and self._skip_stack[-1] == t:
            self._skip_stack.pop()

    def handle_data(self, data: str) -> None:
        text = str(data or "").strip()
        if not text:
            return
        if self._title_capture:
            if self.title:
                self.title += " "
            self.title += text
            return
        if self._skip_stack:
            return
        self.text_parts.append(text)


class _GuardedRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, validator) -> None:
        super().__init__()
        self._validator = validator
        self._redirects = 0

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self._redirects += 1
        if self._redirects > 5:
            raise ValueError("Too many redirects (max 5).")
        self._validator(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _is_forbidden_ip(value: str) -> bool:
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return True
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def _resolve_ips(hostname: str) -> List[str]:
    infos = socket.getaddrinfo(hostname, None)
    ips = []
    for info in infos:
        sockaddr = info[4]
        if isinstance(sockaddr, tuple) and sockaddr:
            ips.append(str(sockaddr[0]))
    out = sorted(set(ips))
    return out


def _assert_public_url(raw_url: str) -> urllib.parse.ParseResult:
    parsed = urllib.parse.urlparse(str(raw_url or "").strip())
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("Only http(s) URLs are allowed.")
    host = (parsed.hostname or "").strip().lower()
    if not host:
        raise ValueError("URL hostname is required.")
    if host in {"localhost"} or host.endswith(".localhost"):
        raise ValueError("Blocked URL host.")
    ips = _resolve_ips(host)
    if not ips:
        raise ValueError("Could not resolve URL host.")
    for ip in ips:
        if _is_forbidden_ip(ip):
            raise ValueError("Blocked private or local network target.")
    return parsed


def _extract_readable_text(html: str) -> Tuple[str, str]:
    parser = _ReadableHtmlParser()
    parser.feed(html or "")
    parser.close()
    text = " ".join(parser.text_parts)
    text = " ".join(text.split())
    return parser.title.strip(), text.strip()


class WebFetchTool:
    name = "web_fetch"
    description = "Fetch a public URL and return readable text extraction."

    def run(self, request: ToolRequest, context: ToolContext) -> ToolResult:
        url = str(request.args.get("url") or "").strip()
        if not url:
            return ToolResult(ok=False, output="Error: url is required.")
        try:
            max_chars = int(request.args.get("max_chars") or 10000)
        except Exception:
            max_chars = 10000
        max_chars = max(500, min(max_chars, 50000))
        try:
            timeout_s = int(request.args.get("timeout_s") or 15)
        except Exception:
            timeout_s = 15
        timeout_s = max(1, min(timeout_s, 60))
        user_agent = str(request.args.get("user_agent") or "codex-telegram-bot-web-fetch/1.0")

        try:
            _assert_public_url(url)
            opener = urllib.request.build_opener(_GuardedRedirectHandler(_assert_public_url))
            req = urllib.request.Request(
                url=url,
                headers={
                    "User-Agent": user_agent,
                    "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.1",
                },
            )
            with opener.open(req, timeout=timeout_s) as resp:
                final_url = str(resp.geturl() or url)
                _assert_public_url(final_url)
                content_type = (resp.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
                if content_type and content_type not in {
                    "text/html",
                    "application/xhtml+xml",
                    "text/plain",
                }:
                    return ToolResult(ok=False, output=f"Error: unsupported content type '{content_type}'.")
                blob = resp.read(max_chars * 6 + 1)
                charset = (resp.headers.get_content_charset() or "utf-8").strip() if hasattr(resp.headers, "get_content_charset") else "utf-8"
            raw = blob.decode(charset, errors="replace")
        except Exception as exc:
            return ToolResult(ok=False, output=f"Error: web_fetch failed: {exc}")

        if (content_type or "").startswith("text/plain"):
            title = ""
            text = raw
        else:
            title, text = _extract_readable_text(raw)
        truncated = len(text) > max_chars
        if truncated:
            text = text[:max_chars].rstrip()
        payload = {
            "url": final_url,
            "title": title,
            "text": text,
            "truncated": bool(truncated),
        }
        return ToolResult(ok=True, output=json.dumps(payload, ensure_ascii=False))
