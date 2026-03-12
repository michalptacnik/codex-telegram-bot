#!/usr/bin/env python3
"""Smoke-test AgentHQ browser bridge parity workflows.

Checks:
1) Control Center browser status endpoint is reachable and connected.
2) `browser_open` works on a safe URL.
3) `browser_snapshot` + `browser_screenshot` execute when supported.
4) `browser_action` executes with ref targeting when snapshot is supported.
5) Legacy fallback path works deterministically when snapshot/screenshot are unsupported.

Exit code is non-zero on failure.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from codex_telegram_bot.tools.base import ToolContext, ToolRequest
from codex_telegram_bot.tools.browser import (
    BrowserActionTool,
    BrowserExtractTool,
    BrowserOpenTool,
    BrowserScreenshotTool,
    BrowserSnapshotTool,
)


def _http_json(
    *,
    base_url: str,
    path: str,
    method: str = "GET",
    payload: Dict[str, Any] | None = None,
    timeout_sec: int = 30,
) -> Dict[str, Any]:
    url = f"{base_url.rstrip('/')}{path}"
    data = None
    headers = {"content-type": "application/json"}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=True).encode("utf-8")
    req = urllib.request.Request(url=url, method=method, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=max(3, int(timeout_sec))) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            body = json.loads(raw) if raw.strip() else {}
            if isinstance(body, dict):
                return {"ok": True, "status": int(resp.status), "body": body}
            return {"ok": False, "status": int(resp.status), "error": "non-object response body"}
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace").strip()
        except Exception:
            detail = ""
        return {"ok": False, "status": int(exc.code), "error": detail or str(exc)}
    except Exception as exc:
        return {"ok": False, "status": 0, "error": str(exc)}


class _RemoteBridgeAdapter:
    def __init__(self, *, base_url: str, timeout_sec: int) -> None:
        self._base_url = str(base_url or "").strip().rstrip("/")
        self._timeout_sec = max(5, int(timeout_sec))
        self._ref_map: Dict[str, str] = {}

    def set_snapshot_ref_map(self, ref_map: Dict[str, str]) -> None:
        self._ref_map = {
            str(k): str(v)
            for k, v in dict(ref_map or {}).items()
            if str(k).strip() and str(v).strip()
        }

    def get_snapshot_ref_map(self) -> Dict[str, str]:
        return dict(self._ref_map)

    def status(self) -> Dict[str, Any]:
        result = _http_json(
            base_url=self._base_url,
            path="/api/browser/status",
            timeout_sec=self._timeout_sec,
        )
        if not result.get("ok"):
            return {"connected": False, "error": str(result.get("error") or "")}
        body = result.get("body")
        return dict(body) if isinstance(body, dict) else {"connected": False}

    def extension_supports_command(self, command: str) -> bool:
        payload = self.status()
        if not bool(payload.get("connected")):
            return False
        want = str(command or "").strip().lower()
        if not want:
            return True
        supported = {
            str(item or "").strip().lower()
            for item in list(payload.get("supported_commands") or [])
            if str(item or "").strip()
        }
        if supported:
            return want in supported
        if want in {"open_url", "navigate_url", "run_script"}:
            return True
        clients = list(payload.get("clients") or [])
        for client in clients:
            if not isinstance(client, dict):
                continue
            version = str(client.get("extension_version") or "").strip()
            if not version:
                continue
            try:
                major = int(version.split(".")[0])
            except (ValueError, IndexError):
                major = 0
            if major >= 2 and want in {"snapshot", "screenshot"}:
                return True
        return False

    def enqueue_command(
        self,
        *,
        command_type: str,
        payload: Dict[str, Any],
        client_id: str = "",
        wait: bool = False,
        timeout_sec: int = 20,
    ) -> Dict[str, Any]:
        result = _http_json(
            base_url=self._base_url,
            path="/api/browser/command",
            method="POST",
            payload={
                "command_type": str(command_type or "").strip(),
                "payload": dict(payload or {}),
                "client_id": str(client_id or "").strip(),
                "wait": bool(wait),
                "timeout_sec": max(1, int(timeout_sec or 20)),
            },
            timeout_sec=max(10, int(timeout_sec or self._timeout_sec) + 20),
        )
        if not result.get("ok"):
            return {"ok": False, "error": str(result.get("error") or "remote call failed")}
        body = result.get("body")
        return dict(body) if isinstance(body, dict) else {"ok": False, "error": "invalid response payload"}


def _is_unsupported_error(message: str, command: str) -> bool:
    text = str(message or "").strip().lower()
    marker = f"unsupported command type: {command}".lower()
    return marker in text or f"does not support browser_{command}" in text


def _parse_json(text: str) -> Dict[str, Any]:
    raw = str(text or "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _extract_tab_id(payload: Dict[str, Any]) -> int:
    if not isinstance(payload, dict):
        return 0
    command = payload.get("command")
    if isinstance(command, dict):
        data = command.get("data")
        if isinstance(data, dict):
            try:
                nested = int(data.get("tab_id") or 0)
            except Exception:
                nested = 0
            if nested > 0:
                return nested
    for key in ("tab_id",):
        try:
            parsed = int(payload.get(key) or 0)
        except Exception:
            parsed = 0
        if parsed > 0:
            return parsed
    return 0


def _first_ref(ref_map: Dict[str, str]) -> int:
    for key, selector in dict(ref_map or {}).items():
        if not str(selector or "").strip():
            continue
        try:
            ref = int(str(key).strip())
        except Exception:
            ref = 0
        if ref > 0:
            return ref
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8765")
    parser.add_argument("--open-url", default="https://example.com")
    parser.add_argument("--timeout-sec", type=int, default=60)
    args = parser.parse_args()

    adapter = _RemoteBridgeAdapter(base_url=args.base_url, timeout_sec=args.timeout_sec)
    status = _http_json(base_url=args.base_url, path="/api/browser/status", timeout_sec=args.timeout_sec)
    print("[status]", json.dumps(status, ensure_ascii=True))
    if not status.get("ok"):
        print("FAIL: browser status endpoint is not reachable.")
        return 1
    status_body = status.get("body") if isinstance(status.get("body"), dict) else {}
    if not bool(status_body.get("connected")):
        print("FAIL: no active Chrome extension client is connected.")
        return 2

    clients = status_body.get("clients") if isinstance(status_body.get("clients"), list) else []
    active_tab_url = ""
    if clients and isinstance(clients[0], dict):
        active_tab_url = str(clients[0].get("active_tab_url") or "").strip()
    smoke_url = str(args.open_url)
    if active_tab_url.lower().startswith(("http://", "https://")):
        smoke_url = active_tab_url

    context = ToolContext(workspace_root=Path.cwd())

    open_result = BrowserOpenTool(bridge=adapter).run(
        ToolRequest(
            name="browser_open",
            args={
                "url": smoke_url,
                "new_tab": True,
                "active": False,
                "wait": True,
                "timeout_sec": int(args.timeout_sec),
            },
        ),
        context,
    )
    print("[browser_open]", open_result.output)
    if not open_result.ok:
        print("FAIL: browser_open command failed.")
        return 3
    open_payload = _parse_json(open_result.output)
    tab_id = _extract_tab_id(open_payload)

    snapshot_result = BrowserSnapshotTool(bridge=adapter).run(
        ToolRequest(
            name="browser_snapshot",
            args={
                "tab_id": tab_id,
                "wait": True,
                "timeout_sec": int(args.timeout_sec),
            },
        ),
        context,
    )
    print("[browser_snapshot]", snapshot_result.output)
    snapshot_supported = bool(snapshot_result.ok)
    if (not snapshot_supported) and (
        "browser_snapshot" in snapshot_result.output.lower()
        or "unsupported command type: snapshot" in snapshot_result.output.lower()
    ):
        print("WARN: snapshot unsupported on connected extension, entering legacy fallback flow.")
        snapshot_supported = False
    elif not snapshot_result.ok:
        print("FAIL: browser_snapshot failed unexpectedly.")
        return 4

    screenshot_result = BrowserScreenshotTool(bridge=adapter).run(
        ToolRequest(
            name="browser_screenshot",
            args={
                "tab_id": tab_id,
                "wait": True,
                "timeout_sec": int(args.timeout_sec),
            },
        ),
        context,
    )
    print("[browser_screenshot]", screenshot_result.output)
    screenshot_supported = bool(screenshot_result.ok)
    if not screenshot_result.ok and _is_unsupported_error(screenshot_result.output, "screenshot"):
        screenshot_supported = False
        print("WARN: screenshot unsupported on connected extension.")
    elif not screenshot_result.ok and snapshot_supported:
        print("FAIL: browser_screenshot failed unexpectedly for snapshot-capable extension.")
        return 5

    if snapshot_supported:
        ref = _first_ref(adapter.get_snapshot_ref_map())
        action_args: Dict[str, Any] = {
            "action": "focus",
            "wait": True,
            "timeout_sec": int(args.timeout_sec),
        }
        if tab_id > 0:
            action_args["tab_id"] = tab_id
        if ref > 0:
            action_args["ref"] = ref
        else:
            # Fallback if no refs are available in snapshot payload.
            action_args["selector"] = "body"
        action_result = BrowserActionTool(bridge=adapter).run(
            ToolRequest(name="browser_action", args=action_args),
            context,
        )
        print("[browser_action.ref]", action_result.output)
        if not action_result.ok:
            print("FAIL: browser_action with snapshot/ref workflow failed.")
            return 6
        print(
            "PASS: parity workflow succeeded (open + snapshot + screenshot"
            + (" [unsupported handled]" if not screenshot_supported else "")
            + " + browser_action ref flow)."
        )
        return 0

    # Legacy permissive fallback: snapshot/screenshot unsupported, continue with
    # extract + action over run_script.
    extract_result = BrowserExtractTool(bridge=adapter).run(
        ToolRequest(
            name="browser_extract",
            args={
                "tab_id": tab_id,
                "max_chars": 1500,
                "include_links": False,
                "wait": True,
                "timeout_sec": int(args.timeout_sec),
            },
        ),
        context,
    )
    print("[browser_extract.fallback]", extract_result.output)
    if not extract_result.ok:
        if "unsupported command type: run_script" in extract_result.output.lower():
            print("FAIL: extension cannot execute run_script; action/extract fallback is unavailable.")
            return 7
        print("FAIL: fallback browser_extract failed.")
        return 8

    action_result = BrowserActionTool(bridge=adapter).run(
        ToolRequest(
            name="browser_action",
            args={
                "action": "extract",
                "max_chars": 800,
                "tab_id": tab_id,
                "wait": True,
                "timeout_sec": int(args.timeout_sec),
            },
        ),
        context,
    )
    print("[browser_action.fallback]", action_result.output)
    if not action_result.ok:
        print("FAIL: fallback browser_action failed.")
        return 9

    print(
        "PASS: legacy permissive fallback succeeded "
        "(snapshot unsupported -> extract/action completion without reroute loop)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
