# Recovery Playbook

This playbook defines deterministic response steps for failed runs.

## 1) Identify Failure Class

Use run detail and error code:

- `ERR_POLICY_BLOCKED`: policy rejected command.
- `ERR_EXEC_TIMEOUT`: command exceeded timeout.
- `ERR_CLI_NOT_FOUND`: codex runtime missing.
- `ERR_CODEX_EXIT_NONZERO`: provider returned non-zero.
- `ERR_PROVIDER_UNHEALTHY` / `ERR_PROVIDER_FAILED`: provider/router instability.

## 2) Execute Recovery Action

Use one of:

- `retry_same_agent`
- `retry_default_agent`
- `open_settings`
- `open_agents`
- `download_artifact`

API:

```http
POST /api/runs/{run_id}/recover
Content-Type: application/json

{"action_id":"retry_same_agent"}
```

## 3) Validate Outcome

- Check run lifecycle events:
  - `recovery.attempted`
  - `recovery.queued` or `recovery.completed`
  - `recovery.failed` (if recovery did not queue/complete)
- Confirm new run status in `/api/runs`.

## 4) Escalation Rules

- Repeated timeout/failure (>3 in 10 minutes): switch to `retry_default_agent`, then inspect provider health.
- Policy blocks for mutating command: require explicit user approval path, do not auto-bypass.
- Provider unavailable: gather artifact and defer action until provider health is `healthy`.

## 5) Compliance Guard

- Do not auto-approve high-risk or mutating tool actions during recovery.
- Keep audit trail intact (events + artifacts).
- Prefer least-privileged profile for retries.
