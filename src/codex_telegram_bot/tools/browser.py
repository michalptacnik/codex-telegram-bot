from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

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
_BROWSER_ACTION_MAX_STEPS = 20
_BROWSER_ACTION_DEFAULT_STEP_TIMEOUT_MS = 15000
_BROWSER_ACTION_MAX_STEP_TIMEOUT_MS = 120000
_BROWSER_ACTION_MAX_SELECTOR_CHARS = 1000
_BROWSER_ACTION_MAX_TEXT_CHARS = 4000


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


# ---------------------------------------------------------------------------
# Snapshot — accessibility tree with numeric element refs (OpenClaw parity)
# ---------------------------------------------------------------------------

_BROWSER_SNAPSHOT_DEFAULT_MAX_ELEMENTS = 200
_BROWSER_SNAPSHOT_MAX_ELEMENTS = 500


def _format_snapshot_for_agent(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    """Convert raw snapshot result into compact LLM-readable format."""
    elements = snapshot.get("elements") or []
    lines: List[str] = []
    for el in elements:
        ref = el.get("ref", 0)
        tag = str(el.get("tag") or "")
        role = str(el.get("role") or "")
        name = str(el.get("name") or "")
        text = str(el.get("text") or "")[:80]
        el_type = str(el.get("type") or "")
        href = str(el.get("href") or "")
        placeholder = str(el.get("placeholder") or "")
        parts: List[str] = [f"ref:{ref}"]
        if role:
            parts.append(f"[{role}]")
        parts.append(f"<{tag}>")
        if name:
            parts.append(f'"{name}"')
        elif text:
            parts.append(f'"{text}"')
        if el_type:
            parts.append(f"type={el_type}")
        if placeholder:
            parts.append(f"placeholder={placeholder}")
        if href:
            parts.append(f"href={href[:80]}")
        lines.append(" ".join(parts))
    return {
        "ok": True,
        "url": str(snapshot.get("url") or ""),
        "title": str(snapshot.get("title") or ""),
        "element_count": len(elements),
        "total_on_page": int(snapshot.get("total_elements_on_page") or 0),
        "truncated": bool(snapshot.get("truncated")),
        "elements": "\n".join(lines),
    }


class BrowserSnapshotTool:
    name = "browser_snapshot"
    description = (
        "Get accessibility tree snapshot of active tab with numeric element refs. "
        "ALWAYS call before browser_action to see page elements."
    )

    def __init__(self, bridge: Any = None) -> None:
        self._bridge = bridge

    def run(self, request: ToolRequest, context: ToolContext) -> ToolResult:
        max_elements = _to_int(
            request.args.get("max_elements"),
            default=_BROWSER_SNAPSHOT_DEFAULT_MAX_ELEMENTS,
            min_value=10,
            max_value=_BROWSER_SNAPSHOT_MAX_ELEMENTS,
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

        payload: Dict[str, Any] = {"max_elements": max_elements}
        if tab_id > 0:
            payload["tab_id"] = int(tab_id)

        result = _run_browser_command(
            bridge=self._bridge,
            command_type="snapshot",
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
        snapshot = data.get("result")
        if not isinstance(snapshot, dict):
            return result

        # Cache the ref map on the bridge for ref-based browser_action
        ref_map = snapshot.get("ref_map")
        if isinstance(ref_map, dict) and self._bridge is not None:
            try:
                self._bridge.set_snapshot_ref_map(ref_map)
            except Exception:
                pass

        normalized = _format_snapshot_for_agent(snapshot)
        return ToolResult(ok=True, output=json.dumps(normalized, ensure_ascii=True))


class BrowserScreenshotTool:
    name = "browser_screenshot"
    description = "Capture a screenshot of the visible browser tab. Returns base64 image data."

    def __init__(self, bridge: Any = None) -> None:
        self._bridge = bridge

    def run(self, request: ToolRequest, context: ToolContext) -> ToolResult:
        client_id = _normalize_client_id(request.args.get("client_id", ""))
        wait = _to_bool(request.args.get("wait"), default=True)
        timeout_sec = _to_int(
            request.args.get("timeout_sec"),
            default=_BROWSER_WAIT_DEFAULT_SEC,
            min_value=1,
            max_value=_BROWSER_WAIT_MAX_SEC,
        )
        tab_id = _to_int(request.args.get("tab_id"), default=0, min_value=0, max_value=2_147_483_647)
        fmt = str(request.args.get("format", "png") or "png").strip().lower()
        if fmt not in {"png", "jpeg"}:
            fmt = "png"

        payload: Dict[str, Any] = {"format": fmt}
        if tab_id > 0:
            payload["tab_id"] = int(tab_id)

        return _run_browser_command(
            bridge=self._bridge,
            command_type="screenshot",
            payload=payload,
            client_id=client_id,
            wait=wait,
            timeout_sec=timeout_sec,
        )


class BrowserActionTool:
    name = "browser_action"
    description = (
        "Execute high-level browser actions in active tab "
        "(click/type/press/wait_for/scroll/focus/submit/select/extract)."
    )

    def __init__(self, bridge: Any = None) -> None:
        self._bridge = bridge

    def run(self, request: ToolRequest, context: ToolContext) -> ToolResult:
        plan, plan_error = _build_browser_action_plan(dict(request.args or {}))
        if plan_error:
            return ToolResult(ok=False, output=plan_error)
        if not plan:
            return ToolResult(ok=False, output="action or steps is required.")

        # Fetch ref map from bridge if any step uses ref-based targeting
        ref_map: Optional[Dict[str, str]] = None
        has_refs = any(isinstance(step.get("ref"), int) and step["ref"] > 0 for step in plan)
        if has_refs and self._bridge is not None:
            try:
                ref_map = self._bridge.get_snapshot_ref_map()
            except Exception:
                ref_map = None

        script = _build_browser_action_script(plan, ref_map=ref_map)
        if len(script) > _BROWSER_SCRIPT_MAX_CHARS:
            return ToolResult(
                ok=False,
                output=f"generated action script exceeds max length ({_BROWSER_SCRIPT_MAX_CHARS} chars).",
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
        action_result = data.get("result")
        if not isinstance(action_result, dict):
            return result

        normalized: Dict[str, Any] = {
            "ok": bool(action_result.get("ok", True)),
            "url": str(action_result.get("url") or ""),
            "title": str(action_result.get("title") or ""),
            "tab_id": _to_int(data.get("tab_id"), default=0, min_value=0, max_value=2_147_483_647),
            "steps": action_result.get("steps") if isinstance(action_result.get("steps"), list) else [],
        }
        if "error" in action_result:
            normalized["error"] = str(action_result.get("error") or "")
        if "step_index" in action_result:
            normalized["step_index"] = _to_int(
                action_result.get("step_index"),
                default=0,
                min_value=0,
                max_value=_BROWSER_ACTION_MAX_STEPS,
            )
        if "step_action" in action_result:
            normalized["step_action"] = str(action_result.get("step_action") or "")
        command_id = str(command.get("command_id") or "").strip()
        if command_id:
            normalized["command_id"] = command_id
        source = str(parsed.get("source") or "").strip()
        if source:
            normalized["source"] = source
        ok = bool(normalized.get("ok"))
        return ToolResult(ok=ok, output=json.dumps(normalized, ensure_ascii=True))


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


def _build_browser_action_plan(args: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], str]:
    payload = dict(args or {})
    default_timeout_ms = _to_int(
        payload.get("step_timeout_ms") or payload.get("timeout_ms"),
        default=_BROWSER_ACTION_DEFAULT_STEP_TIMEOUT_MS,
        min_value=100,
        max_value=_BROWSER_ACTION_MAX_STEP_TIMEOUT_MS,
    )

    raw_steps = payload.get("steps")
    if isinstance(raw_steps, dict):
        raw_steps = [raw_steps]
    if isinstance(raw_steps, list) and raw_steps:
        if len(raw_steps) > _BROWSER_ACTION_MAX_STEPS:
            return [], f"steps exceeds max length ({_BROWSER_ACTION_MAX_STEPS})."
        plan: List[Dict[str, Any]] = []
        for idx, item in enumerate(raw_steps):
            if not isinstance(item, dict):
                return [], f"steps[{idx}] must be an object."
            normalized, err = _normalize_browser_action_step(item, default_timeout_ms=default_timeout_ms)
            if err:
                return [], f"steps[{idx}] {err}"
            plan.append(normalized)
        return plan, ""

    # Single-action mode: top-level args define one step.
    single = dict(payload)
    for key in (
        "steps",
        "client_id",
        "wait",
        "timeout_sec",
        "tab_id",
        "all_frames",
        "step_timeout_ms",
        "timeout_ms",
    ):
        single.pop(key, None)
    normalized, err = _normalize_browser_action_step(single, default_timeout_ms=default_timeout_ms)
    if err:
        return [], err
    return [normalized], ""


def _normalize_browser_action_step(step: Dict[str, Any], *, default_timeout_ms: int) -> Tuple[Dict[str, Any], str]:
    alias = {
        "input": "type",
        "fill": "type",
        "key": "press",
        "keypress": "press",
        "wait": "wait_for",
        "waitfor": "wait_for",
    }
    action_raw = str(step.get("action") or step.get("op") or "").strip().lower()
    if not action_raw:
        return {}, "action is required."
    action = alias.get(action_raw, action_raw)
    allowed = {"click", "type", "press", "wait_for", "scroll", "focus", "submit", "select", "extract"}
    if action not in allowed:
        return {}, f"unsupported action '{action_raw}'."

    timeout_ms = _to_int(
        step.get("timeout_ms") or step.get("timeoutMs"),
        default=default_timeout_ms,
        min_value=100,
        max_value=_BROWSER_ACTION_MAX_STEP_TIMEOUT_MS,
    )
    selector = _clip_string(step.get("selector"), _BROWSER_ACTION_MAX_SELECTOR_CHARS)
    text_contains = _clip_string(
        step.get("text_contains") or step.get("contains_text") or step.get("contains"),
        500,
    )
    text_not_contains = _clip_string(step.get("text_not_contains") or step.get("not_contains"), 500)
    index = _to_int(step.get("index"), default=0, min_value=0, max_value=50)
    scroll = _to_bool(step.get("scroll"), default=True)

    ref = _to_int(step.get("ref"), default=0, min_value=0, max_value=10000)

    out: Dict[str, Any] = {"action": action, "timeout_ms": timeout_ms}
    if ref > 0:
        out["ref"] = ref
    if selector:
        out["selector"] = selector
    if text_contains:
        out["text_contains"] = text_contains
    if text_not_contains:
        out["text_not_contains"] = text_not_contains
    if index > 0:
        out["index"] = index
    if not scroll:
        out["scroll"] = False

    if action in {"click", "type", "focus", "submit", "select"} and (not selector and not text_contains and ref <= 0):
        return {}, f"action '{action}' requires ref, selector, or text_contains."

    if action == "type":
        text = _clip_string(step.get("text") if "text" in step else step.get("value"), _BROWSER_ACTION_MAX_TEXT_CHARS)
        if text is None:
            text = ""
        out["text"] = text
        out["clear"] = _to_bool(step.get("clear"), default=True)
        if _to_bool(step.get("submit"), default=False):
            out["submit"] = True

    if action == "press":
        key = _clip_string(step.get("key"), 32) or "Enter"
        out["key"] = key

    if action == "wait_for":
        sleep_ms = _to_int(
            step.get("sleep_ms") or step.get("sleepMs"),
            default=0,
            min_value=0,
            max_value=_BROWSER_ACTION_MAX_STEP_TIMEOUT_MS,
        )
        if sleep_ms <= 0:
            seconds = _to_int(
                step.get("seconds") or step.get("sleep_sec"),
                default=0,
                min_value=0,
                max_value=600,
            )
            sleep_ms = int(seconds * 1000)
        if sleep_ms > 0:
            out["sleep_ms"] = sleep_ms
        present = _to_bool(step.get("present"), default=True)
        if not present:
            out["present"] = False
        if (not selector) and (not text_contains) and (not text_not_contains) and sleep_ms <= 0:
            return {}, "wait_for requires selector/text_contains/text_not_contains or sleep duration."

    if action == "scroll":
        y = _to_int(step.get("y") or step.get("by"), default=700, min_value=-10000, max_value=10000)
        out["y"] = y
        if _to_bool(step.get("smooth"), default=False):
            out["smooth"] = True

    if action == "select":
        option_value = _clip_string(step.get("option_value"), 300)
        option_text = _clip_string(step.get("option_text") or step.get("text"), 300)
        if not option_value and not option_text:
            return {}, "select requires option_value or option_text."
        if option_value:
            out["option_value"] = option_value
        if option_text:
            out["option_text"] = option_text

    if action == "extract":
        max_chars = _to_int(
            step.get("max_chars"),
            default=4000,
            min_value=200,
            max_value=_BROWSER_EXTRACT_MAX_CHARS,
        )
        include_links = _to_bool(step.get("include_links"), default=False)
        max_links = _to_int(
            step.get("max_links"),
            default=10,
            min_value=0,
            max_value=_BROWSER_EXTRACT_MAX_LINKS,
        )
        out["max_chars"] = max_chars
        if include_links:
            out["include_links"] = True
            out["max_links"] = max_links

    return out, ""


def _clip_string(value: Any, max_len: int) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    if len(text) <= max(1, int(max_len)):
        return text
    return text[: max(1, int(max_len))]


def _build_browser_action_script(plan: List[Dict[str, Any]], ref_map: Optional[Dict[str, str]] = None) -> str:
    steps_json = json.dumps(plan, ensure_ascii=True, separators=(",", ":"))
    ref_map_json = json.dumps(ref_map or {}, ensure_ascii=True, separators=(",", ":"))
    return (
        f"const plan={steps_json};\n"
        f"const __refMap={ref_map_json};\n"
        "const now=()=>Date.now();\n"
        "const sleep=(ms)=>new Promise((r)=>setTimeout(r,Math.max(0,Number(ms)||0)));\n"
        "const txt=(v,m)=>String(v==null?\"\":v).slice(0,m||4000);\n"
        "const bodyText=()=>txt((document.body&&document.body.innerText)||\"\",120000);\n"
        "const clamp=(v,lo,hi)=>Math.max(lo,Math.min(hi,Number(v)||0));\n"
        "const elText=(el)=>txt((el&&(el.innerText||el.textContent||el.value||\"\"))||\"\",2000);\n"
        "const query=(step)=>{"
        "if(step.ref){const sel=__refMap[String(step.ref)];if(sel){try{const el=document.querySelector(sel);if(el)return el;}catch(_){}}}"
        "let nodes=[];const sels=[];if(step.selector)sels.push(String(step.selector));"
        "if(!sels.length&&step.text_contains)sels.push(\"button,a,[role='button'],input,textarea,article,div,span\");"
        "for(const sel of sels){try{nodes=nodes.concat(Array.from(document.querySelectorAll(sel)));}catch(_e){}}"
        "if(step.text_contains){const needle=String(step.text_contains).toLowerCase();"
        "nodes=nodes.filter((n)=>elText(n).toLowerCase().includes(needle));}"
        "if(step.text_not_contains){const noNeedle=String(step.text_not_contains).toLowerCase();"
        "nodes=nodes.filter((n)=>!elText(n).toLowerCase().includes(noNeedle));}"
        "if(!nodes.length)return null;const idx=clamp(step.index,0,Math.max(0,nodes.length-1));"
        "return nodes[idx]||nodes[0]||null;};\n"
        "const waitEl=async(step,timeoutMs)=>{const start=now();for(;;){const el=query(step);if(el)return el;"
        "if(now()-start>=timeoutMs)throw new Error(`Target not found for action ${String(step.action||\"\")}`);"
        "await sleep(120);}};\n"
        "const setValue=(el,value)=>{const p=Object.getPrototypeOf(el);"
        "const d=p?Object.getOwnPropertyDescriptor(p,'value'):null;"
        "if(d&&typeof d.set==='function'){d.set.call(el,value);}else{el.value=value;}"
        "el.dispatchEvent(new Event('input',{bubbles:true}));"
        "el.dispatchEvent(new Event('change',{bubbles:true}));};\n"
        "const keyDispatch=(el,key)=>{const target=el||document.activeElement||document.body;"
        "if(target&&target.focus)target.focus();const k=String(key||'Enter');"
        "for(const t of ['keydown','keypress','keyup']){target.dispatchEvent(new KeyboardEvent(t,{key:k,code:k,bubbles:true,cancelable:true}));}};\n"
        "const waitCond=async(step,timeoutMs)=>{if(step.sleep_ms&&Number(step.sleep_ms)>0){await sleep(Number(step.sleep_ms));return true;}"
        "const start=now();const wantPresent=step.present!==false;for(;;){let ok=true;"
        "if(step.selector||step.text_contains){ok=!!query(step);}if(step.text_not_contains){ok=ok&&!bodyText().toLowerCase().includes(String(step.text_not_contains).toLowerCase());}"
        "if((ok&&wantPresent)||(!ok&&!wantPresent))return true;"
        "if(now()-start>=timeoutMs)throw new Error('wait_for timeout');await sleep(150);}};\n"
        "const extract=(step)=>{const maxChars=clamp(step.max_chars||4000,200,50000);"
        "const text=bodyText();const out={url:String(location.href||''),title:String(document.title||''),text:text.slice(0,maxChars),text_length:text.length};"
        "if(step.include_links){const maxLinks=clamp(step.max_links||10,0,200);const links=[];const seen=new Set();"
        "for(const a of Array.from(document.querySelectorAll('a[href]'))){if(links.length>=maxLinks)break;"
        "const href=String(a.href||'').trim();if(!href||!/^https?:/i.test(href)||seen.has(href))continue;seen.add(href);links.push(href);}out.links=links;}return out;};\n"
        "const steps=[];for(let i=0;i<plan.length;i++){const step=plan[i]||{};const action=String(step.action||'').toLowerCase();"
        "const timeoutMs=clamp(step.timeout_ms||15000,100,120000);try{"
        "if(action==='click'){const el=await waitEl(step,timeoutMs);if(step.scroll!==false&&el.scrollIntoView)el.scrollIntoView({block:'center',inline:'center'});"
        "if(typeof el.click==='function'){el.click();}else{throw new Error('Target not clickable');}"
        "steps.push({index:i,action,ok:true,target:elText(el)});continue;}"
        "if(action==='type'){const el=await waitEl(step,timeoutMs);if(step.scroll!==false&&el.scrollIntoView)el.scrollIntoView({block:'center',inline:'center'});"
        "if(!('value' in el))throw new Error('Target does not support typing');if(el.focus)el.focus();"
        "const base=step.clear===false?String(el.value||''):'';const next=base+String(step.text||'');setValue(el,next);if(step.submit)keyDispatch(el,'Enter');"
        "steps.push({index:i,action,ok:true,chars:next.length});continue;}"
        "if(action==='press'){let el=null;if(step.selector||step.text_contains)el=await waitEl(step,timeoutMs);keyDispatch(el,String(step.key||'Enter'));"
        "steps.push({index:i,action,ok:true,key:String(step.key||'Enter')});continue;}"
        "if(action==='wait_for'){await waitCond(step,timeoutMs);steps.push({index:i,action,ok:true});continue;}"
        "if(action==='scroll'){if(step.selector||step.text_contains){const el=await waitEl(step,timeoutMs);if(el.scrollIntoView)el.scrollIntoView({block:'center',inline:'center'});}"
        "else{window.scrollBy({top:Number(step.y||700),left:0,behavior:step.smooth?'smooth':'auto'});if(step.smooth)await sleep(300);}steps.push({index:i,action,ok:true});continue;}"
        "if(action==='focus'){const el=await waitEl(step,timeoutMs);if(el.focus)el.focus();else throw new Error('Target not focusable');steps.push({index:i,action,ok:true,target:elText(el)});continue;}"
        "if(action==='submit'){const el=await waitEl(step,timeoutMs);if(el.form&&typeof el.form.requestSubmit==='function'){el.form.requestSubmit();}"
        "else if(el.form&&typeof el.form.submit==='function'){el.form.submit();}else{keyDispatch(el,'Enter');}"
        "steps.push({index:i,action,ok:true});continue;}"
        "if(action==='select'){const el=await waitEl(step,timeoutMs);if(String(el.tagName||'').toLowerCase()!=='select')throw new Error('Target is not <select>');"
        "const options=Array.from(el.options||[]);let chosen=null;if(step.option_value)chosen=options.find((o)=>String(o.value)===String(step.option_value));"
        "if(!chosen&&step.option_text){const needle=String(step.option_text).toLowerCase();chosen=options.find((o)=>String(o.text||'').toLowerCase().includes(needle));}"
        "if(!chosen)throw new Error('No matching option');setValue(el,String(chosen.value));steps.push({index:i,action,ok:true,value:String(chosen.value)});continue;}"
        "if(action==='extract'){steps.push({index:i,action,ok:true,...extract(step)});continue;}"
        "throw new Error(`Unsupported action ${action}`);}catch(err){"
        "return {ok:false,error:String(err&&err.message?err.message:err),step_index:i,step_action:action,steps,url:String(location.href||''),title:String(document.title||'')};}}\n"
        "return {ok:true,steps,url:String(location.href||''),title:String(document.title||'')};"
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
