# Runtime Map

This file maps the current runtime wiring used by `codex-telegram-bot`.

## Entry Points

- CLI bootstrap: `src/codex_telegram_bot/cli.py`
- Telegram transport: `src/codex_telegram_bot/telegram_bot.py`
- Control center (Gateway-style UI/API): `src/codex_telegram_bot/control_center/app.py`

## Composition Root

- Container wiring: `src/codex_telegram_bot/app_container.py`
  - provider registry + router
  - execution runner backend (`EXECUTION_BACKEND=local|docker`)
  - tool registry
  - optional MCP bridge
  - optional skill-pack loader
  - probe loop wiring

## Core Orchestrator

- Main orchestrator: `src/codex_telegram_bot/services/agent_service.py`
  - probe decision (`NO_TOOLS` / `NEED_TOOLS`)
  - allowed-tool filtering and runtime snapshot
  - protocol repair (single correction turn)
  - macro/action transpilation (`!exec` / `!tool` / `!loop` + step syntaxes)
  - output firewall at transport boundary (`enforce_transport_text_contract`)
  - approvals, checkpoints, and progress events

## Provider Layer

- Registry/router:
  - `src/codex_telegram_bot/providers/registry.py`
  - `src/codex_telegram_bot/providers/router.py`
- OpenAI Responses support:
  - `src/codex_telegram_bot/providers/responses_api.py`
- Codex CLI provider:
  - `src/codex_telegram_bot/providers/codex_cli.py`

## Execution Layer

- Local shell backend: `src/codex_telegram_bot/execution/local_shell.py`
- Docker sandbox backend: `src/codex_telegram_bot/execution/docker_sandbox.py`
- Risk policy: `src/codex_telegram_bot/execution/policy.py`
- Process sessions: `src/codex_telegram_bot/execution/process_manager.py`

## Capability/Tool Expansion

- Skill packs:
  - `src/codex_telegram_bot/services/skill_pack.py`
  - `src/codex_telegram_bot/services/skill_manager.py`
- MCP bridge:
  - `src/codex_telegram_bot/services/mcp_bridge.py`
- Tool policy/group gating:
  - `src/codex_telegram_bot/services/tool_policy.py`

## Persistence and Audit

- SQLite store: `src/codex_telegram_bot/persistence/sqlite_store.py`
- Event bus: `src/codex_telegram_bot/events/event_bus.py`
- Control-center API surfaces:
  - `/health`
  - `/api/metrics`
  - `/api/reliability`
  - `/api/runtime/capabilities`
  - `/api/sessions`
  - `/api/runs`
