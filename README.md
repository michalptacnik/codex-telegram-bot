# codex-telegram-bot

A Telegram bot that forwards user messages to the local `codex` CLI and returns the response back in chat.

Designed for private/self-hosted use, with an optional allowlist to prevent unauthorized usage.

## Features

- Runs as a local polling Telegram bot
- Forwards text prompts to `codex exec` (non-interactive)
- Streams back long output in multiple Telegram messages
- Built-in onboarding flow for token + allowlist
- Safety guardrails:
  - Input length cap
  - Output chunking
  - Secret redaction pattern for `sk-*` tokens
  - Optional user allowlist
- Admin commands:
  - `/ping`
  - `/status`
  - `/reset`
  - `/reinstall`
  - `/purge`
  - `/restart`

## Architecture

`Telegram -> handlers -> AgentService -> ProviderAdapter -> ExecutionRunner -> codex CLI`

Current module boundaries:

- `telegram_bot.py`: Telegram handlers and command wiring
- `services/agent_service.py`: app service boundary for agent actions
- `providers/codex_cli.py`: OpenAI/Codex provider adapter (CLI-backed)
- `execution/local_shell.py`: local subprocess execution boundary
- `domain/contracts.py`: shared contracts for providers/execution

The bot remains stateless by default. Each incoming message is executed as an independent `codex exec` call through these layers.

## Requirements

- Python 3.10+
- `codex` CLI installed and accessible in `PATH`
- Telegram bot token from `@BotFather`

## Install

Dev install:

```bash
pip install -e .
```

Local install:

```bash
pip install .
```

## Run

```bash
codex-telegram-bot
```

Run Control Center web UI (local dashboard):

```bash
codex-telegram-bot --control-center --host 127.0.0.1 --port 8765
```

## Config

By default, config is stored in:

```
~/.config/codex-telegram-bot/.env
```

Run lifecycle state is persisted in:

```
~/.config/codex-telegram-bot/state.db
```

You can override it with:

```bash
codex-telegram-bot --config-dir /path/to/config
```

On first run, the bot prompts you for:

- `TELEGRAM_BOT_TOKEN`
- `ALLOWLIST` (comma-separated Telegram user IDs)

If `ALLOWLIST` is blank, the bot will warn and require you to type `YES` to continue.

Environment variables override `.env`:

- `TELEGRAM_BOT_TOKEN` (required)
- `ALLOWLIST` (optional)
- `LOG_LEVEL` (default: INFO)
- `PROVIDER_RETRY_ATTEMPTS` (default: `1`)
- `PROVIDER_FAILURE_THRESHOLD` (default: `2`)
- `PROVIDER_RECOVERY_SEC` (default: `30`)
- `PROVIDER_FALLBACK_MODE` (`none` or `echo`, default: `none`)

Print active config summary (never prints token):

```bash
codex-telegram-bot --print-config
```

## Service (systemd)

Recommended (user service):

```bash
./scripts/install_service.sh --user --workdir /path/to/repo
systemctl --user enable --now codex-telegram-bot
```

System-wide:

```bash
sudo ./scripts/install_service.sh --system --workdir /opt/codex-telegram-bot
sudo systemctl enable --now codex-telegram-bot
```

## Docker (optional)

The container expects `codex` to be available. The simplest approach is to mount the host binary into the container.

```bash
docker compose up --build
```

If your `codex` binary is in a different location, update the volume in `docker-compose.yml`.

## Admin Commands

- `/status`: shows Codex version, working directory, allowlist mode
- `/reinstall`: clears stored token and restarts for onboarding
- `/purge`: removes `.env` and restarts
- `/restart`: immediate process restart

## Control Center Endpoints

- `GET /` dashboard
- `GET /runs`
- `GET /runs/{run_id}`
- `GET /agents`
- `GET /settings`
- `GET /health`
- `GET /api/metrics`
- `GET /api/runs?limit=20`
- `GET /api/runs/{run_id}`
- `GET /api/runs/{run_id}/events`
- `GET /api/runs/{run_id}/artifact.txt`
- `GET /api/agents`
- `POST /api/jobs/{job_id}/cancel`
- `POST /api/handoffs`
- `POST /agents` (create/update)
- `POST /agents/{agent_id}/delete`

## Logging

Runtime lifecycle logs now include structured JSON lines with run correlation IDs for:

- run start/failure/completion events
- provider execution start/finish/error events

Provider router behavior:

- Retries failed primary executions up to configured attempts
- Opens circuit after configured consecutive failures
- Uses fallback provider when circuit is open (if enabled)

## Agent Registry

Agent profiles are persisted in SQLite and seeded with:

- `default` agent (`provider=codex_cli`, `policy_profile=balanced`)

Supported policy profiles:

- `strict`
- `balanced`
- `trusted`

Agent concurrency:

- Each agent has `max_concurrency` (1-10)
- Scheduler enforces per-agent concurrency limits
- Queued jobs support cancellation by `job_id`

## Handoff Protocol

Cross-agent handoff uses an explicit envelope payload:

- `version`
- `from_agent_id`
- `to_agent_id`
- `parent_run_id`
- `created_at`
- `prompt_preview`

Handoff lifecycle events (attached to `parent_run_id` timeline when provided):

- `handoff.requested`
- `handoff.accepted`
- `handoff.recovered` (fallback to `default` agent if target unavailable)
- `handoff.completed`
- `handoff.failed`

## Troubleshooting

- `Error: codex CLI not found.`
  - Ensure `codex` is installed and available in `PATH` for the runtime user.
- Bot does not respond in Telegram
  - Verify token correctness
  - Verify allowlist includes your Telegram user ID
  - Check stderr logs for handler exceptions

## License

MIT
