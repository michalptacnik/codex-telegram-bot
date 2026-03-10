from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict

from codex_telegram_bot.tools.base import ToolContext, ToolRequest, ToolResult


_ALLOWED_SCHEMES = {"http", "https"}
_BROWSER_WAIT_DEFAULT_SEC = 180
_BROWSER_WAIT_MAX_SEC = 600
_BROWSER_SCRIPT_MAX_CHARS = 20000
_BROWSER_EXTRACT_DEFAULT_CHARS = 12000
_BROWSER_EXTRACT_MAX_CHARS = 50000
_BROWSER_EXTRACT_DEFAULT_LINKS = 20
_BROWSER_EXTRACT_MAX_LINKS = 200
_BROWSER_EXTRACT_DEFAULT_HTML_CHARS = 20000
_BROWSER_EXTRACT_MAX_HTML_CHARS = 200000


class BrowserStatusTool:
    name = "browser_status"
    description = "Show status of connected Chrome extension browser clients."

    def __init__(self, bridge: Any = None) -> None:
        self._bridge = bridge

    def run(self, request: ToolRequest, context: ToolContext) -> ToolResult:
        local_payload: Dict[str, Any] = {}
        if self._bridge is not None:
            local_payload = dict(self._bridge.status())
            if bool(local_payload.get("connected")):
                local_payload["source"] = "local"
                return ToolResult(ok=True, output=json.dumps(local_payload, ensure_ascii=True))

        remote = _remote_bridge_status()
        if remote.get("ok"):
            payload = dict(remote.get("payload") or {})
            payload["source"] = "remote"
            return ToolResult(ok=True, output=json.dumps(payload, ensure_ascii=True))

        if local_payload:
            local_payload["source"] = "local"
            return ToolResult(ok=True, output=json.dumps(local_payload, ensure_ascii=True))
        return ToolResult(ok=False, output=str(remote.get("error") or "Browser bridge is not configured."))


class BrowserOpenTool:
    name = "browser_open"
    description = "Open a URL in the connected Chrome session (only when navigation is explicitly required)."

    def __init__(self, bridge: Any = None) -> None:
        self._bridge = bridge

    def run(self, request: ToolRequest, context: ToolContext) -> ToolResult:
        raw_url = str(request.args.get("url", "") or "").strip()
        query = str(request.args.get("query", "") or "").strip()
        client_id = _normalize_client_id(request.args.get("client_id", ""))
        wait = _to_bool(request.args.get("wait"), default=True)
        timeout_sec = _to_int(
            request.args.get("timeout_sec"),
            default=_BROWSER_WAIT_DEFAULT_SEC,
            min_value=1,
            max_value=_BROWSER_WAIT_MAX_SEC,
        )
        new_tab = _to_bool(request.args.get("new_tab"), default=True)
        active = _to_bool(request.args.get("active"), default=True)

        url = _normalize_url(raw_url=raw_url, query=query)
        if not url:
            return ToolResult(ok=False, output="url or query is required.")

        if not _is_allowed_url(url):
            return ToolResult(ok=False, output="Only public http(s) URLs are allowed.")

        return _run_browser_command(
            bridge=self._bridge,
            command_type="open_url",
            payload={
                "url": url,
                "new_tab": bool(new_tab),
                "active": bool(active),
            },
            client_id=client_id,
            wait=wait,
            timeout_sec=timeout_sec,
        )


class BrowserNavigateTool:
    name = "browser_navigate"
    description = "Navigate current tab in connected Chrome session (only when navigation is explicitly required)."

    def __init__(self, bridge: Any = None) -> None:
        self._bridge = bridge

    def run(self, request: ToolRequest, context: ToolContext) -> ToolResult:
        raw_url = str(request.args.get("url", "") or "").strip()
        query = str(request.args.get("query", "") or "").strip()
        client_id = _normalize_client_id(request.args.get("client_id", ""))
        wait = _to_bool(request.args.get("wait"), default=True)
        timeout_sec = _to_int(
            request.args.get("timeout_sec"),
            default=_BROWSER_WAIT_DEFAULT_SEC,
            min_value=1,
            max_value=_BROWSER_WAIT_MAX_SEC,
        )
        active = _to_bool(request.args.get("active"), default=True)

        url = _normalize_url(raw_url=raw_url, query=query)
        if not url:
            return ToolResult(ok=False, output="url or query is required.")
        if not _is_allowed_url(url):
            return ToolResult(ok=False, output="Only public http(s) URLs are allowed.")

        return _run_browser_command(
            bridge=self._bridge,
            command_type="navigate_url",
            payload={
                "url": url,
                "active": bool(active),
            },
            client_id=client_id,
            wait=wait,
            timeout_sec=timeout_sec,
        )


class BrowserScriptTool:
    name = "browser_script"
    description = "Execute JavaScript in the active tab of connected Chrome session."

    def __init__(self, bridge: Any = None) -> None:
        self._bridge = bridge

    def run(self, request: ToolRequest, context: ToolContext) -> ToolResult:
        script = str(
            request.args.get("script")
            or request.args.get("js")
            or request.args.get("code")
            or ""
        ).strip()
        if not script:
            return ToolResult(ok=False, output="script is required.")
        if len(script) > _BROWSER_SCRIPT_MAX_CHARS:
            return ToolResult(
                ok=False,
                output=f"script exceeds max length ({_BROWSER_SCRIPT_MAX_CHARS} chars).",
            )

        client_id = _normalize_client_id(request.args.get("client_id", ""))
        wait = _to_bool(request.args.get("wait"), default=True)
        timeout_sec = _to_int(
            request.args.get("timeout_sec"),
            default=_BROWSER_WAIT_DEFAULT_SEC,
            min_value=1,
            max_value=_BROWSER_WAIT_MAX_SEC,
        )
        tab_id = _to_int(request.args.get("tab_id"), default=0, min_value=0, max_value=2_147_483_647)
        all_frames = _to_bool(request.args.get("all_frames"), default=False)

        payload: Dict[str, Any] = {
            "script": script,
            "all_frames": bool(all_frames),
        }
        if tab_id > 0:
            payload["tab_id"] = int(tab_id)

        return _run_browser_command(
            bridge=self._bridge,
            command_type="run_script",
            payload=payload,
            client_id=client_id,
            wait=wait,
            timeout_sec=timeout_sec,
        )


class BrowserExtractTool:
    name = "browser_extract"
    description = "Extract readable page content from active tab in connected Chrome session."

    def __init__(self, bridge: Any = None) -> None:
        self._bridge = bridge

    def run(self, request: ToolRequest, context: ToolContext) -> ToolResult:
        max_chars = _to_int(
            request.args.get("max_chars"),
            default=_BROWSER_EXTRACT_DEFAULT_CHARS,
            min_value=200,
            max_value=_BROWSER_EXTRACT_MAX_CHARS,
        )
        include_links = _to_bool(request.args.get("include_links"), default=True)
        max_links = _to_int(
            request.args.get("max_links"),
            default=_BROWSER_EXTRACT_DEFAULT_LINKS,
            min_value=0,
            max_value=_BROWSER_EXTRACT_MAX_LINKS,
        )
        include_html = _to_bool(request.args.get("include_html"), default=False)
        html_max_chars = _to_int(
            request.args.get("html_max_chars"),
            default=_BROWSER_EXTRACT_DEFAULT_HTML_CHARS,
            min_value=1000,
            max_value=_BROWSER_EXTRACT_MAX_HTML_CHARS,
        )
        all_frames = _to_bool(request.args.get("all_frames"), default=False)

        script = _build_extract_script(
            max_chars=max_chars,
            include_links=include_links,
            max_links=max_links,
            include_html=include_html,
            html_max_chars=html_max_chars,
        )
        if len(script) > _BROWSER_SCRIPT_MAX_CHARS:
            return ToolResult(
                ok=False,
                output=f"generated extract script exceeds max length ({_BROWSER_SCRIPT_MAX_CHARS} chars).",
            )

        client_id = _normalize_client_id(request.args.get("client_id", ""))
        wait = _to_bool(request.args.get("wait"), default=True)
        timeout_sec = _to_int(
            request.args.get("timeout_sec"),
            default=_BROWSER_WAIT_DEFAULT_SEC,
            min_value=1,
            max_value=_BROWSER_WAIT_MAX_SEC,
        )
        tab_id = _to_int(request.args.get("tab_id"), default=0, min_value=0, max_value=2_147_483_647)

        payload: Dict[str, Any] = {
            "script": script,
            "all_frames": bool(all_frames),
        }
        if tab_id > 0:
            payload["tab_id"] = int(tab_id)

        result = _run_browser_command(
            bridge=self._bridge,
            command_type="run_script",
            payload=payload,
            client_id=client_id,
            wait=wait,
            timeout_sec=timeout_sec,
        )
        if not result.ok:
            return result

        try:
            parsed = json.loads(result.output)
        except Exception:
            return result
        if not isinstance(parsed, dict) or bool(parsed.get("pending")):
            return result
        command = parsed.get("command")
        if not isinstance(command, dict):
            return result
        data = command.get("data")
        if not isinstance(data, dict):
            return result
        extracted = data.get("result")
        if not isinstance(extracted, dict):
            return result

        normalized: Dict[str, Any] = {
            "ok": True,
            "url": str(extracted.get("url") or ""),
            "title": str(extracted.get("title") or ""),
            "description": str(extracted.get("description") or ""),
            "text": str(extracted.get("text") or ""),
            "text_length": _to_int(
                extracted.get("text_length"),
                default=len(str(extracted.get("text") or "")),
                min_value=0,
                max_value=10_000_000,
            ),
            "tab_id": _to_int(data.get("tab_id"), default=0, min_value=0, max_value=2_147_483_647),
        }
        if include_links:
            normalized["links"] = _normalize_string_list(extracted.get("links"), limit=max_links)
        if include_html:
            normalized["html"] = str(extracted.get("html") or "")
        command_id = str(command.get("command_id") or "").strip()
        if command_id:
            normalized["command_id"] = command_id
        source = str(parsed.get("source") or "").strip()
        if source:
            normalized["source"] = source
        return ToolResult(ok=True, output=json.dumps(normalized, ensure_ascii=True))


def _run_browser_command(
    *,
    bridge: Any,
    command_type: str,
    payload: Dict[str, Any],
    client_id: str,
    wait: bool,
    timeout_sec: int,
) -> ToolResult:
    result: Dict[str, Any] = {"ok": False, "error": "Browser bridge is not configured."}
    if bridge is not None:
        result = dict(
            bridge.enqueue_command(
                command_type=command_type,
                payload=dict(payload or {}),
                client_id=client_id,
                wait=wait,
                timeout_sec=timeout_sec,
            )
        )
    result = _unwrap_remote_payload(result)

    if _is_pending_result(result):
        return ToolResult(ok=True, output=json.dumps(_promote_pending_to_success(result), ensure_ascii=True))

    if (not result.get("ok")) and _should_try_remote(result):
        remote_wait = bool(wait) and _remote_wait_enabled()
        result = _unwrap_remote_payload(
            _remote_enqueue_command(
                command_type=command_type,
                payload=dict(payload or {}),
                client_id=client_id,
                wait=remote_wait,
                timeout_sec=timeout_sec,
            )
        )
        if _is_pending_result(result):
            return ToolResult(ok=True, output=json.dumps(_promote_pending_to_success(result), ensure_ascii=True))

    if not result.get("ok"):
        return ToolResult(
            ok=False,
            output=_normalize_browser_error(str(result.get("error") or "Failed to queue browser command.")),
        )

    return ToolResult(ok=True, output=json.dumps(result, ensure_ascii=True))


def _unwrap_remote_payload(result: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(result, dict):
        return {"ok": False, "error": "Invalid bridge response."}
    payload = result.get("payload")
    if bool(result.get("ok")) and isinstance(payload, dict):
        merged = dict(payload)
        merged.setdefault("ok", True)
        merged.setdefault("source", "remote")
        return merged
    return dict(result)


def _is_pending_result(result: Dict[str, Any]) -> bool:
    if not isinstance(result, dict):
        return False
    if bool(result.get("ok")):
        return False
    if bool(result.get("pending")) and isinstance(result.get("command"), dict):
        return True
    command = result.get("command")
    if not isinstance(command, dict):
        return False
    status = str(command.get("status") or "").strip().lower()
    return status in {"queued", "dispatched"}


def _promote_pending_to_success(result: Dict[str, Any]) -> Dict[str, Any]:
    command = result.get("command") if isinstance(result, dict) else None
    status = ""
    if isinstance(command, dict):
        status = str(command.get("status") or "").strip().lower()
    warning = str(result.get("error") or "").strip()
    if not warning:
        if status == "queued":
            warning = "Command queued; waiting for extension poll."
        else:
            warning = "Command dispatched; waiting for extension result."
    promoted = dict(result)
    promoted["ok"] = True
    promoted["pending"] = True
    promoted["warning"] = warning
    return promoted


def _to_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def _to_int(value: Any, default: int, min_value: int, max_value: int) -> int:
    try:
        parsed = int(value)
    except Exception:
        parsed = default
    return max(min_value, min(parsed, max_value))


def _normalize_string_list(value: Any, *, limit: int) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if not text:
            continue
        if text in out:
            continue
        out.append(text)
        if len(out) >= max(0, int(limit)):
            break
    return out


def _build_extract_script(
    *,
    max_chars: int,
    include_links: bool,
    max_links: int,
    include_html: bool,
    html_max_chars: int,
) -> str:
    opts = json.dumps(
        {
            "max_chars": int(max_chars),
            "include_links": bool(include_links),
            "max_links": int(max_links),
            "include_html": bool(include_html),
            "html_max_chars": int(html_max_chars),
        },
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return (
        f"const opts = {opts};\n"
        "const normalize = (value) => String(value || \"\")\n"
        "  .replace(/\\r/g, \"\\n\")\n"
        "  .replace(/[ \\t]+\\n/g, \"\\n\")\n"
        "  .replace(/\\n{3,}/g, \"\\n\\n\")\n"
        "  .replace(/[ \\t]{2,}/g, \" \")\n"
        "  .trim();\n"
        "const meta = (name, attr) => {\n"
        "  const key = String(attr || \"name\").toLowerCase() === \"property\" ? \"property\" : \"name\";\n"
        "  const node = document.querySelector(`meta[${key}=\"${name}\"]`);\n"
        "  return node && node.content ? String(node.content).trim() : \"\";\n"
        "};\n"
        "const root = document.querySelector(\"article,main,[role='main'],#content,.content,.post,.article\") || document.body;\n"
        "const clone = root ? root.cloneNode(true) : document.body.cloneNode(true);\n"
        "for (const selector of [\"script\",\"style\",\"noscript\",\"template\",\"svg\",\"canvas\",\"iframe\",\"form\",\"button\"]) {\n"
        "  clone.querySelectorAll(selector).forEach((node) => node.remove());\n"
        "}\n"
        "const rawText = normalize(clone.innerText || clone.textContent || \"\");\n"
        "const text = rawText.slice(0, Math.max(200, Number(opts.max_chars || 0)));\n"
        "let links = [];\n"
        "if (opts.include_links) {\n"
        "  const seen = new Set();\n"
        "  for (const node of clone.querySelectorAll(\"a[href]\")) {\n"
        "    if (links.length >= Math.max(0, Number(opts.max_links || 0))) break;\n"
        "    const hrefRaw = node.getAttribute(\"href\");\n"
        "    if (!hrefRaw) continue;\n"
        "    try {\n"
        "      const href = new URL(hrefRaw, location.href).href;\n"
        "      if (!/^https?:/i.test(href) || seen.has(href)) continue;\n"
        "      seen.add(href);\n"
        "      links.push(href);\n"
        "    } catch (_err) {}\n"
        "  }\n"
        "}\n"
        "let html = \"\";\n"
        "if (opts.include_html) {\n"
        "  html = String(document.documentElement ? document.documentElement.outerHTML : \"\")\n"
        "    .slice(0, Math.max(1000, Number(opts.html_max_chars || 0)));\n"
        "}\n"
        "return {\n"
        "  url: String(location.href || \"\"),\n"
        "  title: String(document.title || \"\"),\n"
        "  description: meta(\"description\") || meta(\"og:description\", \"property\"),\n"
        "  text,\n"
        "  text_length: rawText.length,\n"
        "  links,\n"
        "  html,\n"
        "};"
    )


def _normalize_url(*, raw_url: str, query: str) -> str:
    candidate = str(raw_url or "").strip()
    if not candidate and query:
        encoded = urllib.parse.quote_plus(query)
        candidate = f"https://duckduckgo.com/?q={encoded}"
    if not candidate:
        return ""
    parsed = urllib.parse.urlparse(candidate)
    if not parsed.scheme:
        # Allow bare hostnames, defaulting to https.
        candidate = f"https://{candidate}"
    return candidate


def _normalize_client_id(value: Any) -> str:
    candidate = str(value or "").strip()
    if not candidate:
        return ""
    if candidate.lower() in {"0", "none", "null", "auto", "default", "any"}:
        return ""
    return candidate


def _is_allowed_url(url: str) -> bool:
    try:
        parsed = urllib.parse.urlparse(url)
    except Exception:
        return False
    if parsed.scheme.lower() not in _ALLOWED_SCHEMES:
        return False
    host = (parsed.hostname or "").strip().lower()
    if not host:
        return False
    if host in {"localhost", "127.0.0.1", "::1"}:
        return False
    return True


def _should_try_remote(result: Dict[str, Any]) -> bool:
    if _is_pending_result(result):
        return False
    if not _remote_fallback_enabled():
        return False
    err = str(result.get("error") or "").lower()
    if not err:
        return False
    return (
        "not configured" in err
        or "no active chrome extension client" in err
        or "timed out waiting for extension command result" in err
    )


def _normalize_browser_error(message: str) -> str:
    text = str(message or "").strip()
    low = text.lower()
    if "unsupported command type: run_script" in low:
        return (
            "Connected Chrome extension is outdated and cannot execute scripts "
            "(missing command type 'run_script'). Reload/reinstall the extension "
            "from ./chrome-extension and retry."
        )
    return text or "Failed to queue browser command."


def _remote_fallback_enabled() -> bool:
    raw = str(os.environ.get("BROWSER_BRIDGE_REMOTE_FALLBACK", "1") or "").strip().lower()
    return raw not in {"0", "false", "off", "no"}


def _remote_wait_enabled() -> bool:
    raw = str(os.environ.get("BROWSER_BRIDGE_REMOTE_WAIT", "0") or "").strip().lower()
    return raw in {"1", "true", "on", "yes"}


def _remote_base_url() -> str:
    raw = str(os.environ.get("BROWSER_BRIDGE_URL", "http://127.0.0.1:8765") or "").strip()
    return raw.rstrip("/")


def _remote_api_key() -> str:
    return str(os.environ.get("BROWSER_BRIDGE_API_KEY", "") or "").strip()


def _remote_bridge_status() -> Dict[str, Any]:
    return _remote_json_call(path="/api/browser/status", method="GET", payload=None)


def _remote_enqueue_command(
    *,
    command_type: str,
    payload: Dict[str, Any],
    client_id: str,
    wait: bool,
    timeout_sec: int,
) -> Dict[str, Any]:
    body = {
        "command_type": str(command_type or "").strip(),
        "payload": dict(payload or {}),
        "client_id": str(client_id or "").strip(),
        "wait": bool(wait),
        "timeout_sec": int(timeout_sec),
    }
    req_timeout = 10
    if bool(wait):
        req_timeout = max(10, min(int(timeout_sec) + 30, 900))
    return _remote_json_call(
        path="/api/browser/command",
        method="POST",
        payload=body,
        request_timeout_sec=req_timeout,
    )


def _remote_json_call(
    path: str,
    method: str,
    payload: Dict[str, Any] | None,
    *,
    request_timeout_sec: int = 8,
) -> Dict[str, Any]:
    base = _remote_base_url()
    if not base:
        return {"ok": False, "error": "Remote browser bridge URL is not configured."}
    url = f"{base}{path}"
    headers = {"content-type": "application/json"}
    api_key = _remote_api_key()
    if api_key:
        headers["x-local-api-key"] = api_key
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=True).encode("utf-8")
    req = urllib.request.Request(url=url, method=method, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=max(3, int(request_timeout_sec))) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            try:
                body = json.loads(raw) if raw.strip() else {}
            except Exception:
                body = {"raw": raw}
            return {"ok": True, "payload": body}
    except urllib.error.HTTPError as exc:
        detail = str(exc)
        try:
            raw = exc.read().decode("utf-8", errors="replace")
        except Exception:
            raw = ""
        if raw.strip():
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    msg = str(parsed.get("detail") or parsed.get("error") or "").strip()
                    if msg:
                        detail = msg
                    else:
                        detail = raw.strip()
                else:
                    detail = raw.strip()
            except Exception:
                detail = raw.strip()
        return {"ok": False, "error": f"Remote browser bridge call failed: {detail}"}
    except Exception as exc:
        text = str(exc)
        return {"ok": False, "error": f"Remote browser bridge call failed: {text}"}
