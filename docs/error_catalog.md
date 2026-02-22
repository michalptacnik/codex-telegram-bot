# Error Catalog

Stable backend error codes are defined in `src/codex_telegram_bot/services/error_codes.py`.

Current top failure modes:

1. `ERR_POLICY_BLOCKED`
2. `ERR_EXEC_TIMEOUT`
3. `ERR_CLI_NOT_FOUND`
4. `ERR_CODEX_EXIT_NONZERO`
5. `ERR_PROVIDER_UNHEALTHY`
6. `ERR_PROVIDER_FAILED`
7. `ERR_RUN_CANCELLED`
8. `ERR_HANDOFF_UNAVAILABLE`
9. `ERR_INVALID_AGENT_CONFIG`
10. `ERR_UNKNOWN`

Each catalog entry includes:

- user-facing title and message
- deterministic trigger mapping
- recommended recovery actions

Control Center surfaces this catalog via:

- `GET /api/error-catalog`
- `GET /api/runs/{run_id}/recovery-options`
- `POST /api/runs/{run_id}/recover`
