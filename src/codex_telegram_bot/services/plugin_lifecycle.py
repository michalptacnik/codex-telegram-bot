import json
import os
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from codex_telegram_bot.domain.plugins import PluginAuditEvent, PluginRecord
from codex_telegram_bot.plugins.manifest import load_manifest, validate_manifest


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class PluginLifecycleManager:
    def __init__(self, config_dir: Path):
        self._config_dir = Path(config_dir).expanduser().resolve()
        self._root = self._config_dir / "plugins"
        self._manifests_dir = self._root / "manifests"
        self._registry_path = self._root / "registry.json"
        self._audit_path = self._root / "audit.jsonl"
        self._trust_policy = (os.environ.get("PLUGIN_TRUST_POLICY", "require_signature") or "").strip().lower()
        if self._trust_policy not in {"require_signature", "allow_local_unsigned"}:
            self._trust_policy = "require_signature"
        self._manifests_dir.mkdir(parents=True, exist_ok=True)
        if not self._registry_path.exists():
            self._write_registry({"plugins": {}})

    def list_plugins(self) -> List[PluginRecord]:
        rows = self._load_registry().get("plugins", {})
        out: List[PluginRecord] = []
        for _, value in rows.items():
            out.append(_dict_to_plugin(value))
        out.sort(key=lambda x: x.plugin_id)
        return out

    def get_plugin(self, plugin_id: str) -> Optional[PluginRecord]:
        row = self._load_registry().get("plugins", {}).get(plugin_id)
        if not row:
            return None
        return _dict_to_plugin(row)

    def install_plugin(self, manifest_path: Path, enable: bool = False) -> PluginRecord:
        manifest = load_manifest(manifest_path)
        errors = validate_manifest(manifest)
        if errors:
            self._append_audit(
                action="install",
                plugin_id=str(manifest.get("plugin_id") or "unknown"),
                outcome="failed",
                details={"reason": "; ".join(errors[:3])},
            )
            raise ValueError("Invalid plugin manifest: " + "; ".join(errors))

        plugin_id = str(manifest["plugin_id"])
        trust = self._trust_status(manifest)
        enabled = bool(enable)
        if enabled and trust != "trusted":
            self._append_audit(
                action="install",
                plugin_id=plugin_id,
                outcome="failed",
                details={"reason": "trust_policy_block", "trust_status": trust},
            )
            raise ValueError("Plugin cannot be enabled due to trust policy.")

        dst_manifest = self._manifests_dir / f"{plugin_id}.json"
        dst_manifest.write_text(json.dumps(manifest, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
        now = _utc_now().isoformat()
        record = {
            "plugin_id": plugin_id,
            "name": str(manifest.get("name") or plugin_id),
            "version": str(manifest.get("version") or "0.0.0"),
            "manifest_version": str(manifest.get("manifest_version") or ""),
            "requires_api_version": str(manifest.get("requires_api_version") or "v1"),
            "capabilities": list(manifest.get("capabilities") or []),
            "enabled": enabled,
            "trust_status": trust,
            "manifest_path": str(dst_manifest),
            "created_at": now,
            "updated_at": now,
        }
        registry = self._load_registry()
        registry.setdefault("plugins", {})[plugin_id] = record
        self._write_registry(registry)
        self._append_audit(
            action="install",
            plugin_id=plugin_id,
            outcome="success",
            details={"enabled": str(enabled).lower(), "trust_status": trust},
        )
        return _dict_to_plugin(record)

    def update_plugin(self, plugin_id: str, manifest_path: Path) -> PluginRecord:
        existing = self.get_plugin(plugin_id)
        if not existing:
            raise ValueError("Plugin not found.")
        updated = self.install_plugin(manifest_path=manifest_path, enable=existing.enabled)
        self._append_audit(action="update", plugin_id=plugin_id, outcome="success", details={})
        return updated

    def enable_plugin(self, plugin_id: str) -> PluginRecord:
        registry = self._load_registry()
        row = registry.get("plugins", {}).get(plugin_id)
        if not row:
            raise ValueError("Plugin not found.")
        manifest = load_manifest(Path(row["manifest_path"]))
        trust = self._trust_status(manifest)
        if trust != "trusted":
            self._append_audit(
                action="enable",
                plugin_id=plugin_id,
                outcome="failed",
                details={"reason": "trust_policy_block", "trust_status": trust},
            )
            raise ValueError("Plugin cannot be enabled due to trust policy.")
        row["enabled"] = True
        row["trust_status"] = trust
        row["updated_at"] = _utc_now().isoformat()
        self._write_registry(registry)
        self._append_audit(action="enable", plugin_id=plugin_id, outcome="success", details={})
        return _dict_to_plugin(row)

    def disable_plugin(self, plugin_id: str) -> PluginRecord:
        registry = self._load_registry()
        row = registry.get("plugins", {}).get(plugin_id)
        if not row:
            raise ValueError("Plugin not found.")
        row["enabled"] = False
        row["updated_at"] = _utc_now().isoformat()
        self._write_registry(registry)
        self._append_audit(action="disable", plugin_id=plugin_id, outcome="success", details={})
        return _dict_to_plugin(row)

    def uninstall_plugin(self, plugin_id: str) -> bool:
        registry = self._load_registry()
        row = registry.get("plugins", {}).pop(plugin_id, None)
        if not row:
            return False
        self._write_registry(registry)
        manifest_path = Path(str(row.get("manifest_path") or ""))
        try:
            if manifest_path.exists():
                manifest_path.unlink()
        except Exception:
            pass
        self._append_audit(action="uninstall", plugin_id=plugin_id, outcome="success", details={})
        return True

    def list_audit_events(self, limit: int = 200) -> List[PluginAuditEvent]:
        if not self._audit_path.exists():
            return []
        rows = self._audit_path.read_text(encoding="utf-8").splitlines()
        items: List[PluginAuditEvent] = []
        for raw in rows[-max(1, limit) :]:
            try:
                data = json.loads(raw)
                items.append(
                    PluginAuditEvent(
                        ts=datetime.fromisoformat(data["ts"]),
                        action=str(data.get("action") or ""),
                        plugin_id=str(data.get("plugin_id") or ""),
                        outcome=str(data.get("outcome") or ""),
                        details={k: str(v) for k, v in dict(data.get("details") or {}).items()},
                    )
                )
            except Exception:
                continue
        return items

    def _trust_status(self, manifest: Dict[str, Any]) -> str:
        signature = str(manifest.get("signature") or "").strip()
        if signature:
            return "trusted"
        if self._trust_policy == "allow_local_unsigned":
            return "trusted"
        return "untrusted_unsigned"

    def _append_audit(self, action: str, plugin_id: str, outcome: str, details: Dict[str, str]) -> None:
        row = {
            "ts": _utc_now().isoformat(),
            "action": action,
            "plugin_id": plugin_id,
            "outcome": outcome,
            "details": details,
        }
        with self._audit_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=True) + "\n")

    def _load_registry(self) -> Dict[str, Any]:
        try:
            data = json.loads(self._registry_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except Exception:
            pass
        return {"plugins": {}}

    def _write_registry(self, registry: Dict[str, Any]) -> None:
        self._registry_path.write_text(json.dumps(registry, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")


def _dict_to_plugin(data: Dict[str, Any]) -> PluginRecord:
    return PluginRecord(
        plugin_id=str(data.get("plugin_id") or ""),
        name=str(data.get("name") or ""),
        version=str(data.get("version") or ""),
        manifest_version=str(data.get("manifest_version") or ""),
        requires_api_version=str(data.get("requires_api_version") or ""),
        capabilities=[str(v) for v in list(data.get("capabilities") or [])],
        enabled=bool(data.get("enabled", False)),
        trust_status=str(data.get("trust_status") or ""),
        manifest_path=str(data.get("manifest_path") or ""),
        created_at=datetime.fromisoformat(str(data.get("created_at"))),
        updated_at=datetime.fromisoformat(str(data.get("updated_at"))),
    )

