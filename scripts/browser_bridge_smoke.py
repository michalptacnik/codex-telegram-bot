#!/usr/bin/env python3
"""Smoke-test AgentHQ browser bridge + extension capabilities.

Checks:
1) Control Center browser status endpoint is reachable.
2) A browser client is connected.
3) open_url command works.
4) run_script command works.

Exit code is non-zero on failure.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from typing import Any, Dict


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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8765")
    parser.add_argument("--open-url", default="https://example.com")
    parser.add_argument("--timeout-sec", type=int, default=60)
    args = parser.parse_args()

    status = _http_json(base_url=args.base_url, path="/api/browser/status", timeout_sec=args.timeout_sec)
    print("[status]", json.dumps(status, ensure_ascii=True))
    if not status.get("ok"):
        print("FAIL: browser status endpoint is not reachable.")
        return 1

    status_body = status.get("body") if isinstance(status.get("body"), dict) else {}
    if not bool(status_body.get("connected")):
        print("FAIL: no active Chrome extension client is connected.")
        return 2

    open_resp = _http_json(
        base_url=args.base_url,
        path="/api/browser/command",
        method="POST",
        payload={
            "command_type": "open_url",
            "payload": {"url": str(args.open_url), "new_tab": True, "active": True},
            "wait": True,
            "timeout_sec": int(args.timeout_sec),
        },
        timeout_sec=max(10, int(args.timeout_sec) + 20),
    )
    print("[open_url]", json.dumps(open_resp, ensure_ascii=True))
    if not open_resp.get("ok"):
        print("FAIL: open_url command failed.")
        return 3

    run_script = _http_json(
        base_url=args.base_url,
        path="/api/browser/command",
        method="POST",
        payload={
            "command_type": "run_script",
            "payload": {"script": "return { title: document.title, url: location.href };"},
            "wait": True,
            "timeout_sec": int(args.timeout_sec),
        },
        timeout_sec=max(10, int(args.timeout_sec) + 20),
    )
    print("[run_script]", json.dumps(run_script, ensure_ascii=True))
    if not run_script.get("ok"):
        err = str(run_script.get("error") or "").lower()
        if "unsupported command type: run_script" in err:
            print(
                "FAIL: connected Chrome extension does not support run_script. "
                "Reload/reinstall the latest extension from ./chrome-extension."
            )
            return 4
        print("FAIL: run_script command failed.")
        return 5

    print("PASS: browser bridge open_url + run_script are working.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
