from __future__ import annotations

import threading
import time
import uuid
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional


@dataclass
class BrowserClient:
    instance_id: str
    label: str
    version: str
    platform: str
    user_agent: str
    created_at: datetime
    last_seen_at: datetime
    active_tab_url: str = ""
    active_tab_title: str = ""


@dataclass
class BrowserCommand:
    command_id: str
    client_id: str
    command_type: str
    payload: Dict[str, Any]
    created_at: datetime
    status: str = "queued"  # queued | dispatched | completed | failed
    dispatched_at: Optional[datetime] = None
    dispatch_count: int = 0
    completed_at: Optional[datetime] = None
    ok: Optional[bool] = None
    output: str = ""
    data: Dict[str, Any] = field(default_factory=dict)


class BrowserBridge:
    """In-memory bridge between agent tools and a Chrome extension client.

    The extension periodically heartbeats and polls pending commands.
    The agent enqueues commands (browser_open/browser_navigate) and can wait
    for completion.
    """

    def __init__(
        self,
        *,
        heartbeat_ttl_sec: int = 90,
        command_retention_sec: int = 900,
        dispatch_lease_sec: int = 30,
    ) -> None:
        self._heartbeat_ttl_sec = max(10, int(heartbeat_ttl_sec or 90))
        self._command_retention_sec = max(60, int(command_retention_sec or 900))
        self._dispatch_lease_sec = max(1, int(dispatch_lease_sec or 30))
        self._clients: Dict[str, BrowserClient] = {}
        self._queue_by_client: Dict[str, List[str]] = {}
        self._commands: Dict[str, BrowserCommand] = {}
        self._lock = threading.Lock()
        self._last_snapshot_ref_map: Dict[str, str] = {}
        self._extension_versions: Dict[str, str] = {}  # client_id -> extension_version
        self._supported_commands: Dict[str, List[str]] = {}  # client_id -> command types supported by extension
        self._default_client_id: str = ""
        self._session_client_affinity: Dict[str, str] = {}
        self._single_client_mode: bool = str(os.environ.get("BROWSER_BRIDGE_SINGLE_CLIENT", "1") or "").strip().lower() not in {
            "0",
            "false",
            "off",
            "no",
        }

    # ------------------------------------------------------------------
    # Snapshot ref map — caches ref_id -> CSS selector from last snapshot
    # ------------------------------------------------------------------

    def set_snapshot_ref_map(self, ref_map: Dict[str, str]) -> None:
        with self._lock:
            self._last_snapshot_ref_map = dict(ref_map or {})

    def get_snapshot_ref_map(self) -> Dict[str, str]:
        with self._lock:
            return dict(self._last_snapshot_ref_map)

    def extension_supports_command(self, command: str) -> bool:
        """Check if any active extension supports the given command type."""
        want = str(command or "").strip().lower()
        if not want:
            return True
        with self._lock:
            now = _now_utc()
            for client_id, client in self._clients.items():
                if not _client_is_active(client, now, self._heartbeat_ttl_sec):
                    continue
                raw_supported = list(self._supported_commands.get(client_id) or [])
                extension_version = str(self._extension_versions.get(client_id, "") or "").strip()
                effective_supported, _source = _effective_capability_view(
                    supported_commands=raw_supported,
                    extension_version=extension_version,
                    client_version=client.version,
                )
                supported = set(effective_supported)
                if want in supported:
                    return True
            return False

    def active_extension_capabilities(self) -> Dict[str, Any]:
        """Return connected extension capability metadata for planner/guardrails."""
        with self._lock:
            now = _now_utc()
            self._prune_locked(now)
            active_clients: List[Dict[str, Any]] = []
            supported_union: set[str] = set()
            for client_id, client in self._clients.items():
                if not _client_is_active(client, now, self._heartbeat_ttl_sec):
                    continue
                extension_version = str(self._extension_versions.get(client_id, "") or "").strip()
                raw_supported = list(self._supported_commands.get(client_id) or [])
                effective_supported, source = _effective_capability_view(
                    supported_commands=raw_supported,
                    extension_version=extension_version,
                    client_version=client.version,
                )
                active_clients.append(
                    {
                        "instance_id": client_id,
                        "version": str(client.version or "").strip(),
                        "extension_version": extension_version,
                        "supported_commands": list(raw_supported),
                        "effective_supported_commands": list(effective_supported),
                        "capability_source": source,
                    }
                )
                supported_union.update(effective_supported)
            return {
                "connected": bool(active_clients),
                "active_clients": len(active_clients),
                "clients": active_clients,
                "supported_commands": sorted(supported_union),
                "default_client_id": str(self._default_client_id or ""),
            }

    # ------------------------------------------------------------------
    # Client lifecycle
    # ------------------------------------------------------------------

    def register_client(
        self,
        *,
        instance_id: str,
        label: str = "",
        version: str = "",
        platform: str = "",
        user_agent: str = "",
        active_tab_url: str = "",
        active_tab_title: str = "",
        extension_version: str = "",
        supported_commands: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        client_id = (instance_id or "").strip() or str(uuid.uuid4())
        now = _now_utc()
        with self._lock:
            if extension_version:
                self._extension_versions[client_id] = str(extension_version).strip()
            if supported_commands is not None:
                self._supported_commands[client_id] = _normalize_supported_commands(supported_commands)
            existing = self._clients.get(client_id)
            if existing is None:
                existing = BrowserClient(
                    instance_id=client_id,
                    label=(label or "Chrome extension").strip() or "Chrome extension",
                    version=str(version or "").strip(),
                    platform=str(platform or "").strip(),
                    user_agent=str(user_agent or "").strip(),
                    created_at=now,
                    last_seen_at=now,
                    active_tab_url=str(active_tab_url or "").strip(),
                    active_tab_title=str(active_tab_title or "").strip(),
                )
                self._clients[client_id] = existing
            else:
                existing.label = (label or existing.label or "Chrome extension").strip() or "Chrome extension"
                existing.version = str(version or existing.version or "").strip()
                existing.platform = str(platform or existing.platform or "").strip()
                existing.user_agent = str(user_agent or existing.user_agent or "").strip()
                if active_tab_url:
                    existing.active_tab_url = str(active_tab_url).strip()
                if active_tab_title:
                    existing.active_tab_title = str(active_tab_title).strip()
                existing.last_seen_at = now
            self._queue_by_client.setdefault(client_id, [])
            if not self._default_client_id:
                self._default_client_id = client_id
            self._prune_locked(now)
            return self._client_to_dict(existing, now)

    def heartbeat(
        self,
        *,
        instance_id: str,
        active_tab_url: str = "",
        active_tab_title: str = "",
        extension_version: str = "",
        supported_commands: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        client_id = (instance_id or "").strip()
        if not client_id:
            raise ValueError("instance_id is required")
        now = _now_utc()
        with self._lock:
            client = self._clients.get(client_id)
            if client is None:
                client = BrowserClient(
                    instance_id=client_id,
                    label="Chrome extension",
                    version="",
                    platform="",
                    user_agent="",
                    created_at=now,
                    last_seen_at=now,
                )
                self._clients[client_id] = client
                self._queue_by_client.setdefault(client_id, [])
            client.last_seen_at = now
            if extension_version:
                self._extension_versions[client_id] = str(extension_version).strip()
            if supported_commands is not None:
                self._supported_commands[client_id] = _normalize_supported_commands(supported_commands)
            if active_tab_url:
                client.active_tab_url = str(active_tab_url).strip()
            if active_tab_title:
                client.active_tab_title = str(active_tab_title).strip()
            if not self._default_client_id:
                self._default_client_id = client_id
            self._prune_locked(now)
            return self._client_to_dict(client, now)

    def list_clients(self) -> List[Dict[str, Any]]:
        now = _now_utc()
        with self._lock:
            self._prune_locked(now)
            rows = [self._client_to_dict(client, now) for client in self._clients.values()]
        rows.sort(key=lambda item: item.get("last_seen_at") or "", reverse=True)
        return rows

    def status(self) -> Dict[str, Any]:
        clients = self.list_clients()
        active = [row for row in clients if bool(row.get("active"))]
        capabilities = self.active_extension_capabilities()
        return {
            "connected": bool(active),
            "active_clients": len(active),
            "total_clients": len(clients),
            "heartbeat_ttl_sec": self._heartbeat_ttl_sec,
            "clients": clients,
            "supported_commands": list(capabilities.get("supported_commands") or []),
            "default_client_id": str(self._default_client_id or ""),
            "session_client_affinity_count": len(self._session_client_affinity),
        }

    # ------------------------------------------------------------------
    # Command queue
    # ------------------------------------------------------------------

    def enqueue_command(
        self,
        *,
        command_type: str,
        payload: Dict[str, Any],
        client_id: str = "",
        session_key: str = "",
        wait: bool = False,
        timeout_sec: int = 20,
    ) -> Dict[str, Any]:
        ctype = str(command_type or "").strip().lower()
        if not ctype:
            return {"ok": False, "error": "command_type is required"}
        now = _now_utc()
        with self._lock:
            self._prune_locked(now)
            requested_client = (client_id or "").strip()
            normalized_session_key = _normalize_session_key(session_key)
            target_client = ""
            if requested_client:
                requested = self._clients.get(requested_client)
                if requested is not None and _client_is_active(requested, now, self._heartbeat_ttl_sec):
                    target_client = requested_client
                else:
                    # Fallback to any active client when the requested id is stale/invalid.
                    target_client = self._pick_best_client_locked(now)
            elif normalized_session_key:
                bound = str(self._session_client_affinity.get(normalized_session_key) or "").strip()
                if bound:
                    bound_client = self._clients.get(bound)
                    if bound_client is not None and _client_is_active(bound_client, now, self._heartbeat_ttl_sec):
                        target_client = bound
            else:
                target_client = ""
            if not target_client:
                target_client = self._pick_best_client_locked(now)
            if not target_client:
                return {
                    "ok": False,
                    "error": "No active Chrome extension client is connected.",
                }
            self._default_client_id = target_client
            if normalized_session_key:
                self._session_client_affinity[normalized_session_key] = target_client
            cmd = BrowserCommand(
                command_id=str(uuid.uuid4()),
                client_id=target_client,
                command_type=ctype,
                payload=dict(payload or {}),
                created_at=now,
            )
            self._commands[cmd.command_id] = cmd
            self._queue_by_client.setdefault(target_client, []).append(cmd.command_id)
            command_dict = self._command_to_dict(cmd)

        if not wait:
            return {"ok": True, "command": command_dict}

        result = self.wait_for_result(command_id=cmd.command_id, timeout_sec=timeout_sec)
        if not result.get("ok"):
            return result
        return {
            "ok": True,
            "command": result.get("command", {}),
        }

    def poll_commands(self, *, instance_id: str, limit: int = 5) -> List[Dict[str, Any]]:
        client_id = (instance_id or "").strip()
        if not client_id:
            return []
        now = _now_utc()
        with self._lock:
            client = self._clients.get(client_id)
            if client is None:
                return []
            client.last_seen_at = now
            self._prune_locked(now)
            pending = self._queue_by_client.get(client_id) or []
            max_count = max(1, min(int(limit or 5), 20))
            out: List[Dict[str, Any]] = []
            for command_id in pending:
                cmd = self._commands.get(command_id)
                if cmd is None:
                    continue
                if cmd.status in {"completed", "failed"}:
                    continue
                if len(out) >= max_count:
                    continue
                should_dispatch = cmd.status == "queued" or _dispatch_lease_expired(
                    cmd=cmd,
                    now=now,
                    lease_sec=self._dispatch_lease_sec,
                )
                if not should_dispatch:
                    continue
                cmd.status = "dispatched"
                cmd.dispatched_at = now
                cmd.dispatch_count = max(0, int(cmd.dispatch_count or 0)) + 1
                out.append(
                    {
                        "command_id": cmd.command_id,
                        "command_type": cmd.command_type,
                        "payload": dict(cmd.payload),
                        "created_at": cmd.created_at.isoformat(),
                        "dispatch_count": cmd.dispatch_count,
                    }
                )
            return out

    def complete_command(
        self,
        *,
        instance_id: str,
        command_id: str,
        ok: bool,
        output: str = "",
        data: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        client_id = (instance_id or "").strip()
        cid = (command_id or "").strip()
        now = _now_utc()
        with self._lock:
            client = self._clients.get(client_id)
            if client is None:
                return {"ok": False, "error": "Unknown extension instance."}
            client.last_seen_at = now
            cmd = self._commands.get(cid)
            if cmd is None:
                return {"ok": False, "error": "Unknown command."}
            if cmd.client_id != client_id:
                return {"ok": False, "error": "Command does not belong to this extension instance."}
            cmd.completed_at = now
            cmd.ok = bool(ok)
            cmd.status = "completed" if bool(ok) else "failed"
            cmd.dispatched_at = now
            cmd.output = str(output or "")
            cmd.data = dict(data or {})
            self._prune_locked(now)
            return {"ok": True, "command": self._command_to_dict(cmd)}

    def wait_for_result(self, *, command_id: str, timeout_sec: int = 20) -> Dict[str, Any]:
        cid = (command_id or "").strip()
        if not cid:
            return {"ok": False, "error": "command_id is required"}
        timeout = max(1, min(int(timeout_sec or 20), 600))
        deadline = time.monotonic() + timeout
        while time.monotonic() <= deadline:
            now = _now_utc()
            with self._lock:
                self._prune_locked(now)
                cmd = self._commands.get(cid)
                if cmd is None:
                    return {"ok": False, "error": "Command not found."}
                if cmd.status in {"completed", "failed"}:
                    return {
                        "ok": bool(cmd.ok),
                        "command": self._command_to_dict(cmd),
                        "error": "" if bool(cmd.ok) else (cmd.output or "Command failed."),
                    }
            time.sleep(0.2)

        with self._lock:
            cmd = self._commands.get(cid)
            if cmd is None:
                return {"ok": False, "error": "Command not found."}
            status = str(cmd.status or "").strip().lower()
            if status == "queued":
                message = "Command is queued; waiting for extension poll."
            elif status == "dispatched":
                message = "Command was dispatched; waiting for extension result."
            else:
                message = "Timed out waiting for extension command result."
            return {
                "ok": False,
                "command": self._command_to_dict(cmd),
                "pending": status in {"queued", "dispatched"},
                "error": message,
            }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _pick_best_client_locked(self, now: datetime) -> str:
        preferred = str(self._default_client_id or "").strip()
        if preferred:
            preferred_client = self._clients.get(preferred)
            if preferred_client is not None and _client_is_active(preferred_client, now, self._heartbeat_ttl_sec):
                return preferred

        best: Optional[BrowserClient] = None
        for client in self._clients.values():
            if not _client_is_active(client, now, self._heartbeat_ttl_sec):
                continue
            if best is None or client.last_seen_at > best.last_seen_at:
                best = client
        if best is None:
            return ""
        if self._single_client_mode:
            self._default_client_id = best.instance_id
        return best.instance_id

    def _client_to_dict(self, client: BrowserClient, now: Optional[datetime] = None) -> Dict[str, Any]:
        ref = now or _now_utc()
        extension_version = str(self._extension_versions.get(client.instance_id, "") or "").strip()
        supported_commands = list(self._supported_commands.get(client.instance_id) or [])
        effective_supported, source = _effective_capability_view(
            supported_commands=supported_commands,
            extension_version=extension_version,
            client_version=client.version,
        )
        return {
            "instance_id": client.instance_id,
            "label": client.label,
            "version": client.version,
            "extension_version": extension_version,
            "supported_commands": supported_commands,
            "effective_supported_commands": effective_supported,
            "capability_source": source,
            "platform": client.platform,
            "user_agent": client.user_agent,
            "created_at": client.created_at.isoformat(),
            "last_seen_at": client.last_seen_at.isoformat(),
            "active": _client_is_active(client, ref, self._heartbeat_ttl_sec),
            "active_tab_url": client.active_tab_url,
            "active_tab_title": client.active_tab_title,
        }

    def _command_to_dict(self, command: BrowserCommand) -> Dict[str, Any]:
        return {
            "command_id": command.command_id,
            "client_id": command.client_id,
            "command_type": command.command_type,
            "payload": dict(command.payload),
            "created_at": command.created_at.isoformat(),
            "status": command.status,
            "dispatched_at": command.dispatched_at.isoformat() if command.dispatched_at else "",
            "dispatch_count": int(command.dispatch_count or 0),
            "completed_at": command.completed_at.isoformat() if command.completed_at else "",
            "ok": command.ok,
            "output": command.output,
            "data": dict(command.data),
        }

    def _prune_locked(self, now: datetime) -> None:
        stale_commands: List[str] = []
        cutoff = now - timedelta(seconds=self._command_retention_sec)
        for command_id, cmd in self._commands.items():
            if cmd.completed_at is not None and cmd.completed_at < cutoff:
                stale_commands.append(command_id)
        for command_id in stale_commands:
            self._commands.pop(command_id, None)

        for client_id, queue in list(self._queue_by_client.items()):
            kept = [cid for cid in queue if cid in self._commands]
            if kept:
                self._queue_by_client[client_id] = kept
            else:
                self._queue_by_client[client_id] = []

        active_client_ids = set(self._clients.keys())
        for client_id in list(self._extension_versions.keys()):
            if client_id not in active_client_ids:
                self._extension_versions.pop(client_id, None)
        for client_id in list(self._supported_commands.keys()):
            if client_id not in active_client_ids:
                self._supported_commands.pop(client_id, None)
        for key, client_id in list(self._session_client_affinity.items()):
            if client_id not in active_client_ids:
                self._session_client_affinity.pop(key, None)
        if self._default_client_id and self._default_client_id not in active_client_ids:
            self._default_client_id = ""


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _client_is_active(client: BrowserClient, now: datetime, heartbeat_ttl_sec: int) -> bool:
    delta = now - client.last_seen_at
    return delta.total_seconds() <= max(10, int(heartbeat_ttl_sec or 90))


def _dispatch_lease_expired(*, cmd: BrowserCommand, now: datetime, lease_sec: int) -> bool:
    if str(cmd.status or "").strip().lower() != "dispatched":
        return False
    if cmd.dispatched_at is None:
        return True
    delta = now - cmd.dispatched_at
    return delta.total_seconds() >= max(1, int(lease_sec or 30))


def _normalize_supported_commands(raw: List[str]) -> List[str]:
    seen: set[str] = set()
    out: List[str] = []
    for item in list(raw or []):
        name = str(item or "").strip().lower()
        if not name or name in seen:
            continue
        seen.add(name)
        out.append(name)
    return out


def _normalize_session_key(raw: str) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    # Keep keys compact to avoid unbounded memory cardinality.
    return text[:128]


def _parse_major(version: str) -> int:
    raw = str(version or "").strip()
    if not raw:
        return 0
    head = raw.split(".", 1)[0].strip()
    if not head:
        return 0
    try:
        return int(head)
    except ValueError:
        return 0


def _effective_capability_view(
    *,
    supported_commands: List[str],
    extension_version: str,
    client_version: str,
) -> tuple[List[str], str]:
    reported = _normalize_supported_commands(supported_commands)
    if reported:
        return reported, "reported"
    ext = str(extension_version or "").strip()
    cli = str(client_version or "").strip()
    major = _parse_major(ext) if ext else _parse_major(cli)
    baseline = ["open_url", "navigate_url", "run_script"]
    if major >= 2:
        mode = "inferred_v2_from_extension_version" if ext else "inferred_v2_from_client_version"
        return baseline + ["snapshot", "screenshot"], mode
    if ext:
        return baseline, "inferred_baseline_from_extension_version"
    if cli:
        return baseline, "inferred_baseline_from_client_version"
    return baseline, "inferred_baseline_default"
