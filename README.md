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
- `/new`
- `/resume`
- `/branch`
- `/pending`
- `/approve <approval_id>`
- `/deny <approval_id>`
- `/interrupt`
- `/continue`
- `/continue yes` (required when replaying a prior high-risk tool prompt)
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

First-run onboarding wizard:

- Open `http://127.0.0.1:8765/onboarding`
- Configure provider key (optional), workspace root, and safety profile
- Run built-in validation test and complete onboarding

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
- `EXECUTION_WORKSPACE_ROOT` (default: current working directory)
- `REDACTION_EXTRA_PATTERNS` (optional regex list separated by `;;`)
- `SESSION_MAX_MESSAGES` (default: `60`)
- `SESSION_COMPACT_KEEP` (default: `20`)
- `TOOL_LOOP_MAX_STEPS` (default: `3`)
- `APPROVAL_TTL_SEC` (default: `900`)
- `REPO_SCAN_MAX_FILES` (default: `3000`)
- `REPO_SCAN_MAX_FILE_BYTES` (default: `120000`)
- `REPO_INDEX_AUTO_REFRESH_SEC` (default: `30`)

Print active config summary (never prints token):

```bash
codex-telegram-bot --print-config
```

## Service (systemd)

Ubuntu-first bootstrap (recommended):

```bash
./scripts/bootstrap_ubuntu.sh --user --workdir /path/to/repo
```

System-wide bootstrap:

```bash
sudo ./scripts/bootstrap_ubuntu.sh --system --workdir /opt/codex-telegram-bot
```

## Debian Packaging (CI)

Build internal Debian package artifact locally:

```bash
./scripts/build_deb.sh --output-dir dist
```

CI workflow:

- `.github/workflows/build-deb.yml`
- On `v*` tags, the workflow also publishes package files to the corresponding GitHub Release.

Outputs:

- `dist/*.deb`
- `dist/*.sha256`
- `dist/*.provenance.json`

Versioning and provenance details:

- `docs/deb_provenance.md`

Bootstrap behavior:

- installs Ubuntu dependencies (`python3`, `python3-venv`, `systemd`, etc.)
- creates a dedicated virtualenv and installs the project
- generates and reloads systemd unit
- enables and starts the service (unless `--no-enable`)

Useful flags:

- `--skip-apt` (use already-installed dependencies)
- `--no-enable` (install only, do not start service)
- `--dry-run` (print commands without executing)

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
- `/status`: live run card (session, active job id, current step/total steps, pending approvals, elapsed)
- `/reinstall`: clears stored token and restarts for onboarding
- `/purge`: removes `.env` and restarts
- `/restart`: immediate process restart

## Control Center Endpoints

- `GET /` dashboard
- `GET /runs`
- `GET /sessions`
- `GET /approvals`
- `GET /runs/{run_id}`
- `GET /agents`
- `GET /settings`
- `GET /health`
- `GET /api/metrics`
- `GET /api/onboarding/status`
- `GET /api/runs?limit=20`
- `GET /api/sessions?limit=50`
- `GET /api/approvals?limit=200`
- `POST /api/approvals/approve`
- `POST /api/approvals/deny`
- `GET /api/retrieval?query=...`
- `GET /api/retrieval/stats`
- `POST /api/retrieval/refresh`
- `GET /api/runs/{run_id}`
- `GET /api/runs/{run_id}/events`
- `GET /api/runs/{run_id}/artifact.txt`
- `GET /api/error-catalog`
- `GET /api/runs/{run_id}/recovery-options`
- `POST /api/runs/{run_id}/recover`
- `GET /api/agents`
- `POST /api/jobs/{job_id}/cancel`
- `POST /api/handoffs`
- `POST /agents` (create/update)
- `POST /agents/{agent_id}/delete`

## Parity Evaluation Harness

Run measurable parity benchmarks (Telegram agent path vs direct `codex exec`) using a shared case suite:

```bash
PYTHONPATH=src python3 -m codex_telegram_bot.eval_parity \
  --cases docs/benchmarks/parity_cases.json \
  --workspace-root /path/to/repo
```

Outputs:

- `docs/reports/parity-report-<timestamp>.json`
- `docs/reports/parity-report-<timestamp>.md`
- `docs/reports/parity-report-latest.json` (via weekly helper script)
- `docs/reports/parity-report-latest.md` (via weekly helper script)

Exit code:

- `0` parity gates passed
- `2` one or more parity gates failed

Weekly automation (local cron/systemd-friendly):

```bash
./scripts/run_parity_weekly.sh
```

Example cron entry (Sundays at 09:00):

```cron
0 9 * * 0 cd /path/to/codex-telegram-bot && ./scripts/run_parity_weekly.sh >> /tmp/codex-parity.log 2>&1
```

Parity gates and milestone plan:

- `docs/parity_exit_criteria.md`

Onboarding:

- `GET /onboarding`
- `POST /onboarding`

## Logging

Runtime lifecycle logs now include structured JSON lines with run correlation IDs for:

- run start/failure/completion events
- provider execution start/finish/error events
- secret redaction audit events (`security.redaction.applied`)
- recovery telemetry (`recovery.attempted`, `recovery.queued`, `recovery.completed`, `recovery.failed`)

## Error UX

- Failed runs expose stable backend error codes (`error_code`) in the run API.
- Run detail page shows catalog-based recovery guidance and one-click actions.
- Error catalog is available at `/api/error-catalog`.

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

Execution profile behavior:

- `strict`: command allowlist + workspace-root path enforcement + timeout cap `45s`
- `balanced`: command allowlist + workspace-root path enforcement + timeout cap `120s`
- `trusted`: relaxed command scope + timeout cap `300s`

Each run emits `run.policy.applied` audit events with agent/profile metadata.

Agent concurrency:

- Each agent has `max_concurrency` (1-10)
- Scheduler enforces per-agent concurrency limits
- Queued jobs support cancellation by `job_id`

## Telegram Session Runtime

- Each Telegram chat/user pair gets a persisted active session.
- Session history is stored in SQLite and reused for subsequent prompts.
- `/new` (or `/reset`) archives current session and starts a fresh one.
- `/resume [session_prefix]` resumes active session or switches to a matching past session.
- `/branch` creates a new session branched from current recent history.
- Session metadata is visible in Control Center (`/sessions` and `/api/sessions`).
- Retention policy compacts old session history when message count exceeds configured limits.

## Tool Loop (MVP)

- Messages can include deterministic shell actions using `!exec ...` lines.
- Example:
  - `!exec /bin/ls -la`
  - `!exec /usr/bin/git status`
- Structured loop objects are also supported:
  - `!loop {"steps":[{"kind":"exec","command":"/bin/ls -la"}],"final_prompt":"Summarize findings"}`
- The agent executes listed actions, captures observations, and injects them into the provider prompt.
- High-risk actions require explicit approval:
  - `/pending` to list pending approvals
  - `/approve <approval_id>` to execute approved action
  - `/deny <approval_id>` to reject pending action
- Tool loop enforces per-message max step budget (`TOOL_LOOP_MAX_STEPS`).
- Pending approvals expire automatically after `APPROVAL_TTL_SEC`.
- Telegram now emits step-by-step loop progress messages (start, step start, approval wait, finish).
- Progress updates are delivered via in-place status message edits to reduce chat noise.
- Progress updates are impermanent: in-place status messages auto-delete shortly after completion.
- High-risk tool approvals include Telegram 1/2/3 action buttons:
  - `1) Allow once`
  - `2) Deny`
  - `3) Show pending`
- `/interrupt` cancels active run for current chat (queued job + in-flight task).
- `/continue` reuses latest user prompt and asks the agent to continue the task.

## Repository Retrieval (MVP)

- Prompt construction now includes relevant local repository snippets.
- Retrieval uses fast path+content scoring over text files under `EXECUTION_WORKSPACE_ROOT`.
- Retrieval index is cached in memory and auto-refreshed.
- Symbol-aware weighting boosts files with matching `def/class/function` names.
- Debug retrieval quality via `GET /api/retrieval?query=...`.

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
