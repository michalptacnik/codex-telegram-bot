# Plugin Lifecycle Manager

Plugin lifecycle is implemented with persistent registry and audit logs.

## Storage

- Registry: `<config-dir>/plugins/registry.json`
- Installed manifests: `<config-dir>/plugins/manifests/<plugin_id>.json`
- Audit events: `<config-dir>/plugins/audit.jsonl`

## Trust Policy

Environment variable:

- `PLUGIN_TRUST_POLICY=require_signature` (default)
- `PLUGIN_TRUST_POLICY=allow_local_unsigned`

With `require_signature`, activation requires a non-empty `signature` field in the manifest.

## Lifecycle Operations

Control Center API:

- `POST /api/plugins/install` body `{"manifest_path":"...", "enable":false}`
- `POST /api/plugins/{plugin_id}/enable`
- `POST /api/plugins/{plugin_id}/disable`
- `POST /api/plugins/{plugin_id}/update` body `{"manifest_path":"..."}`
- `POST /api/plugins/{plugin_id}/uninstall`
- `GET /api/plugins`
- `GET /api/plugins/audit`

Control Center UI:

- `/plugins` page supports install/enable/disable/uninstall and shows recent lifecycle audits.

## Audit Semantics

Each lifecycle operation appends an audit record with:

- timestamp (`ts`)
- `action` (`install|enable|disable|update|uninstall`)
- `plugin_id`
- `outcome` (`success|failed`)
- operation details (validation/trust-policy failures, etc.)
