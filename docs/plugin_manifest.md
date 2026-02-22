# Plugin Manifest and Capability Schema

This document defines plugin manifest `v1` for the local integration platform.

## Files

- Schema: `docs/schemas/plugin_manifest_v1.json`
- Validator: `scripts/validate_plugin_manifest.py`
- Python API: `src/codex_telegram_bot/plugins/manifest.py`

## Security Model

- Capabilities are explicit and deny-by-default.
- No implicit privileged operations are allowed.
- Privileged capabilities (`workspace:write`, `network:http`) require:
  - explicit capability grant in `capabilities`
  - `security_acknowledged: true` in manifest

## Manifest Example

```json
{
  "manifest_version": "1.0",
  "plugin_id": "sample_reader",
  "name": "Sample Reader",
  "version": "1.2.0",
  "requires_api_version": "v1",
  "entrypoint": {
    "type": "command",
    "argv": ["python3", "-m", "sample_plugin"]
  },
  "capabilities": ["runs:read", "jobs:read", "meta:read"],
  "permissions": [
    {"resource": "runs", "actions": ["read"]},
    {"resource": "jobs", "actions": ["read"]}
  ]
}
```

## Versioning Strategy

- `manifest_version` is currently pinned to `1.0`.
- Backward-compatible additions can be made in schema `1.x` while keeping required fields stable.
- Breaking changes require a new manifest major version and a new schema file (for example, `plugin_manifest_v2.json`).

## Validation

```bash
PYTHONPATH=src python3 scripts/validate_plugin_manifest.py /path/to/manifest.json
```

Exit codes:

- `0` valid manifest
- `1` invalid JSON or read failure
- `2` schema/semantic validation failure
