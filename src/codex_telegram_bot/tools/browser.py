from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Sequence
from urllib.parse import urlparse

from codex_telegram_bot.tools.base import ToolContext, ToolRequest, ToolResult

_BROWSER_BINARIES: Sequence[str] = (
    "chromium",
    "chromium-browser",
    "google-chrome",
    "google-chrome-stable",
)

_DEFAULT_TIMEOUT_SEC = 45
_MAX_TIMEOUT_SEC = 120
_DEFAULT_MAX_CHARS = 15_000
_MAX_MAX_CHARS = 60_000
_DEFAULT_WAIT_MS = 1_500
_MAX_WAIT_MS = 30_000


def _looks_like_url(raw: str) -> bool:
    parsed = urlparse(raw.strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _pick_browser_binary() -> str:
    for name in _BROWSER_BINARIES:
        path = shutil.which(name)
        if path:
            return path
    return ""


def _extract_title(html: str) -> str:
    match = re.search(r"<title[^>]*>(.*?)</title>", html, flags=re.I | re.S)
    if not match:
        return ""
    return re.sub(r"\s+", " ", match.group(1)).strip()


class HeadlessChromiumTool:
    """Fetch a webpage with a real Chromium engine and return captured DOM."""

    name = "headless_chromium"

    def run(self, request: ToolRequest, context: ToolContext) -> ToolResult:
        url = str(request.args.get("url") or "").strip()
        if not url:
            return ToolResult(ok=False, output="Error: missing required arg 'url'.")
        if not _looks_like_url(url):
            return ToolResult(ok=False, output="Error: 'url' must be an http(s) URL.")

        timeout_sec = _DEFAULT_TIMEOUT_SEC
        wait_ms = _DEFAULT_WAIT_MS
        max_chars = _DEFAULT_MAX_CHARS
        try:
            if request.args.get("timeout_sec") is not None:
                timeout_sec = max(5, min(int(request.args.get("timeout_sec") or _DEFAULT_TIMEOUT_SEC), _MAX_TIMEOUT_SEC))
            if request.args.get("wait_ms") is not None:
                wait_ms = max(0, min(int(request.args.get("wait_ms") or _DEFAULT_WAIT_MS), _MAX_WAIT_MS))
            if request.args.get("max_chars") is not None:
                max_chars = max(500, min(int(request.args.get("max_chars") or _DEFAULT_MAX_CHARS), _MAX_MAX_CHARS))
        except (TypeError, ValueError):
            return ToolResult(ok=False, output="Error: invalid numeric args for timeout_sec/wait_ms/max_chars.")

        browser = _pick_browser_binary()
        if not browser:
            return ToolResult(
                ok=False,
                output=(
                    "Error: no Chromium binary found. "
                    "Install one of: chromium, chromium-browser, google-chrome."
                ),
            )

        cmd = [
            browser,
            "--headless=new",
            "--disable-gpu",
            "--no-first-run",
            "--no-default-browser-check",
            f"--virtual-time-budget={wait_ms}",
            "--dump-dom",
            url,
        ]
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(Path(context.workspace_root).resolve()),
                capture_output=True,
                text=True,
                timeout=timeout_sec,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return ToolResult(ok=False, output=f"Error: headless Chromium timed out after {timeout_sec}s.")
        except Exception as exc:
            return ToolResult(ok=False, output=f"Error: failed to run Chromium: {exc}")

        if proc.returncode != 0:
            stderr = (proc.stderr or "").strip()
            return ToolResult(
                ok=False,
                output=(
                    f"Error: Chromium exited with code {proc.returncode}.\n"
                    f"stderr: {stderr[:1200]}"
                ),
            )

        html = (proc.stdout or "").strip()
        if not html:
            return ToolResult(ok=False, output="Error: Chromium returned empty page content.")
        html = html[:max_chars]
        title = _extract_title(html)
        prefix = f"Fetched: {url}\n"
        if title:
            prefix += f"Title: {title}\n"
        prefix += "DOM:\n"
        return ToolResult(ok=True, output=prefix + html)
