# Local Integration API v1

Stable local API for automation/integration clients.

## Enablement and Auth Model

The v1 API is disabled by default. Enable by setting `LOCAL_API_KEYS`:

```bash
LOCAL_API_KEYS="reader-token:meta:read,runs:read,jobs:read;writer-token:prompts:write,jobs:write"
```

Token format:

- entries separated by `;`
- each entry: `<token>:<scope1>,<scope2>,...`
- special scope: `admin:*` (all scopes)

Accepted headers:

- `Authorization: Bearer <token>`
- `X-Local-Api-Key: <token>`

Response codes:

- `503` API disabled (no configured keys)
- `401` missing/invalid token
- `403` valid token but missing required scope

## Versioning Policy

- base path: `/api/v1`
- policy: backward compatible for additive changes in `v1`
- breaking changes require new major path (`/api/v2`)

## Core Endpoints

- `GET /api/v1/meta` (`meta:read`)
- `GET /api/v1/runs?limit=20` (`runs:read`)
- `GET /api/v1/runs/{run_id}` (`runs:read`)
- `POST /api/v1/prompts` (`prompts:write`)
  - body: `{"prompt":"...", "agent_id":"default"}`
- `GET /api/v1/jobs/{job_id}` (`jobs:read`)
- `POST /api/v1/jobs/{job_id}/cancel` (`jobs:write`)
- `GET /api/v1/plugins` (`plugins:read`)

## Minimal cURL Example

```bash
curl -s \
  -H "Authorization: Bearer reader-token" \
  http://127.0.0.1:8765/api/v1/meta
```
