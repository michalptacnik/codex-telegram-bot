import json
import re
from pathlib import Path
from typing import Any, Dict, List

PLUGIN_ID_RE = re.compile(r"^[a-z0-9_-]{3,64}$")
SEMVER_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
SUPPORTED_MANIFEST_VERSION = "1.0"
SUPPORTED_LOCAL_API_VERSION = "v1"

ALLOWED_CAPABILITIES = {
    "runs:read",
    "jobs:read",
    "jobs:cancel",
    "prompts:queue",
    "meta:read",
    "retrieval:read",
    "workspace:read",
    "workspace:write",
    "network:http",
}

PRIVILEGED_CAPABILITIES = {
    "workspace:write",
    "network:http",
}

ALLOWED_PERMISSION_RESOURCES = {
    "runs",
    "jobs",
    "prompts",
    "meta",
    "retrieval",
    "workspace",
    "network",
}
ALLOWED_PERMISSION_ACTIONS = {"read", "write", "queue", "cancel", "request"}


def load_manifest(path: Path) -> Dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Manifest root must be a JSON object.")
    return data


def validate_manifest(manifest: Dict[str, Any]) -> List[str]:
    errors: List[str] = []
    version = str(manifest.get("manifest_version") or "").strip()
    if version != SUPPORTED_MANIFEST_VERSION:
        errors.append(
            f"manifest_version must be '{SUPPORTED_MANIFEST_VERSION}' (got '{version or 'missing'}')."
        )

    plugin_id = str(manifest.get("plugin_id") or "").strip()
    if not PLUGIN_ID_RE.match(plugin_id):
        errors.append("plugin_id must match ^[a-z0-9_-]{3,64}$.")

    name = str(manifest.get("name") or "").strip()
    if not name:
        errors.append("name is required.")

    version_field = str(manifest.get("version") or "").strip()
    if not SEMVER_RE.match(version_field):
        errors.append("version must be semantic version format X.Y.Z.")

    requires_api_version = str(manifest.get("requires_api_version") or SUPPORTED_LOCAL_API_VERSION).strip()
    if requires_api_version != SUPPORTED_LOCAL_API_VERSION:
        errors.append(f"requires_api_version must be '{SUPPORTED_LOCAL_API_VERSION}'.")

    entrypoint = manifest.get("entrypoint")
    if not isinstance(entrypoint, dict):
        errors.append("entrypoint must be an object.")
    else:
        kind = str(entrypoint.get("type") or "").strip()
        argv = entrypoint.get("argv")
        if kind != "command":
            errors.append("entrypoint.type must be 'command'.")
        if not isinstance(argv, list) or not argv or not all(str(x).strip() for x in argv):
            errors.append("entrypoint.argv must be a non-empty array of strings.")

    capabilities = manifest.get("capabilities")
    if not isinstance(capabilities, list) or not capabilities:
        errors.append("capabilities must be a non-empty array.")
        capabilities_set = set()
    else:
        capabilities_set = {str(c).strip() for c in capabilities if str(c).strip()}
        unknown = sorted(c for c in capabilities_set if c not in ALLOWED_CAPABILITIES)
        if unknown:
            errors.append(f"capabilities contains unsupported entries: {', '.join(unknown)}.")

    permissions = manifest.get("permissions", [])
    if permissions is not None and not isinstance(permissions, list):
        errors.append("permissions must be an array when provided.")
    elif isinstance(permissions, list):
        for idx, item in enumerate(permissions):
            if not isinstance(item, dict):
                errors.append(f"permissions[{idx}] must be an object.")
                continue
            resource = str(item.get("resource") or "").strip()
            actions = item.get("actions")
            if resource not in ALLOWED_PERMISSION_RESOURCES:
                errors.append(f"permissions[{idx}].resource is invalid.")
            if not isinstance(actions, list) or not actions:
                errors.append(f"permissions[{idx}].actions must be a non-empty array.")
                continue
            bad_actions = [str(a) for a in actions if str(a) not in ALLOWED_PERMISSION_ACTIONS]
            if bad_actions:
                errors.append(f"permissions[{idx}].actions has invalid values: {', '.join(bad_actions)}.")

    security_ack = bool(manifest.get("security_acknowledged", False))
    used_privileged = sorted(c for c in capabilities_set if c in PRIVILEGED_CAPABILITIES)
    if used_privileged and not security_ack:
        errors.append(
            "security_acknowledged must be true when privileged capabilities are requested: "
            + ", ".join(used_privileged)
            + "."
        )

    return errors

