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
  - Secret redaction (AWS keys, GitHub tokens, Stripe keys, bearer tokens, generic API keys)
  - Optional user allowlist
  - Role-based access control (viewer / user / admin)
  - Per-user daily spend ceilings
- Session management:
  - Persistent sessions per chat/user pair
  - Automatic idle-session archival and pruning
  - Per-session isolated workspace directories with disk-byte and file-count quotas
- Multi-provider architecture:
  - Runtime provider registry with hot-switch support
  - Capability-based provider routing
  - Circuit-breaker with configurable echo fallback
- Parity evaluation harness with CI-safe offline baseline mode
- Admin commands:
  - `/ping`
  - `/status`
  - `/help`
  - `/workspace`
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

```
Telegram -> Agent Core -> AccessController -> AgentService -> CapabilityRouter
                                                           -> ProviderRegistry -> codex CLI
                                                           -> WorkspaceManager
                                                           -> Tool Registry -> Execution Runner
```

Current module boundaries:

- `telegram_bot.py`: Telegram transport handlers and command wiring
- `agent_core/agent.py`: agent entrypoint used by transport (`Agent.handle_message`)
- `agent_core/router.py`: agent-to-service routing boundary
- `agent_core/memory.py`: bounded memory defaults (`SESSION_MAX_TURNS=20`)
- `services/agent_service.py`: orchestration, session, approvals, tool loop
- `services/access_control.py`: role-based action authorization, spend ceilings, secret scanning
- `services/capability_router.py`: selects best provider by capability requirements
- `services/workspace_manager.py`: per-session disk workspaces with quota enforcement
- `services/session_retention.py`: idle-session archival and pruning policy
- `providers/registry.py`: runtime provider registry with hot-switch
- `providers/*.py`: provider abstraction + codex-cli implementation
- `tools/*.py`: explicit tool registry (`read_file`, `write_file`, `git_status`)
- `execution/local_shell.py`: local subprocess execution boundary

The runtime is stateful per chat/user session with bounded memory and explicit reset/branch/resume controls.

## Roadmap Tracking

- Roadmap execution is tracked in GitHub Issues and milestones.
- Completed EPICs (merged to main):
  - `#64` Agent Core Foundation
  - `#65` Secure Computer Interaction Layer
  - `#66` Multi-Provider Architecture
  - `#67` Streaming and CLI-like Feedback
  - `#68` Lightweight Web Control Center
  - Parity 1: Session Retention Policy
  - Parity 2: Tool Approval Gate (SQLite-backed)
  - Parity 3: Streaming Updater
  - Parity 4: Repo Context Retrieval
  - Parity 5: Workspace Quota Enforcement
  - Parity 6: Observability & Alerts
  - Parity 7: Runbook Registry
  - Parity 8: Capability-Based Provider Routing
  - Parity 9: Role/Spend Access Control
  - Parity 10: Control Center Session API

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
- `PROVIDER_BACKEND` (default: `codex-cli`; current supported value: `codex-cli`)
- `CODEX_EXEC_TIMEOUT_SEC` (default: `180`, bounded by policy profile max timeout)
- `CODEX_VERSION_TIMEOUT_SEC` (default: `10`)
- `EXECUTION_WORKSPACE_ROOT` (default: current working directory)
- `CAPABILITIES_DIR` (default: `<EXECUTION_WORKSPACE_ROOT>/capabilities`)
- `REDACTION_EXTRA_PATTERNS` (optional regex list separated by `;;`)
- `SESSION_MAX_TURNS` (default: `20`)
- `SESSION_MAX_MESSAGES` (default: `40`, derived from turns)
- `SESSION_COMPACT_KEEP` (default: `20`)
- `TOOL_LOOP_MAX_STEPS` (default: `3`)
- `APPROVAL_TTL_SEC` (default: `900`)
- `MAX_PENDING_APPROVALS_PER_USER` (default: `3`)
- `SESSION_WORKSPACES_ROOT` (default: `<EXECUTION_WORKSPACE_ROOT>/.session_workspaces`)
- `REPO_SCAN_MAX_FILES` (default: `3000`)
- `REPO_SCAN_MAX_FILE_BYTES` (default: `120000`)
- `REPO_INDEX_AUTO_REFRESH_SEC` (default: `30`)
- `ALERT_WEBHOOK_URL` (optional HTTPS endpoint for alerts)
- `ALERT_WEBHOOK_TIMEOUT_SEC` (default: `3`)
- `ALERT_MIN_SEVERITY` (`low|medium|high|critical`, default: `medium`)
- `ALERT_DEDUP_WINDOW_SEC` (default: `90`)
- `ALERT_RETRY_COUNT` (default: `2`)
- `ALERT_DEAD_LETTER_MAX` (default: `200`)
- `LOCAL_API_KEYS` (optional; enables scoped auth for all `/api/*` and `/api/v1/*` endpoints)
- `CONTROL_CENTER_UI_SECRET` (optional; when set, gates all Control Center HTML pages behind a `/login` form)
- `PLUGIN_TRUST_POLICY` (`require_signature` or `allow_local_unsigned`, default: `require_signature`)
- `WORKSPACE_MAX_DISK_BYTES` (default: `104857600` = 100 MB; per-session workspace disk quota)
- `WORKSPACE_MAX_FILE_COUNT` (default: `5000`; per-session workspace file-count quota)
- `SESSION_ARCHIVE_AFTER_IDLE_DAYS` (default: `30`; idle sessions archived after this many days)
- `SESSION_DELETE_AFTER_DAYS` (default: `90`; archived sessions hard-deleted after this many days)

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
- `docs/recovery_playbook.md`
- `docs/reliability_ops.md`
- `docs/upgrade_rollback_runbook.md`
- `docs/local_api_v1.md`
- `docs/plugin_manifest.md`
- `docs/plugin_lifecycle.md`

Bootstrap behavior:

- installs Ubuntu dependencies (`python3`, `python3-venv`, `systemd`, etc.)
- creates a dedicated virtualenv and installs the project
- generates and reloads systemd unit
- enables and starts the service (unless `--no-enable`)

Useful flags:

- `--skip-apt` (use already-installed dependencies)
- `--no-enable` (install only, do not start service)
- `--dry-run` (print commands without executing)
- `--skip-migration-check` (skip state DB integrity+backup preflight; break-glass only)

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
- `/status`: includes context diagnostics (prompt chars + retrieval confidence)
- `/help`: command taxonomy, examples, and active policy profile
- `/workspace`: shows per-session isolated workspace path
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
- `GET /api/onboarding/readiness` (first-run checks: workspace, codex CLI, telegram token)
- `GET /api/runs?limit=20`
- `GET /api/sessions?limit=50`
- `GET /api/sessions/{session_id}/detail`
- `GET /api/approvals?limit=200`
- `POST /api/approvals/approve`
- `POST /api/approvals/deny`
- `GET /api/retrieval?query=...`
- `GET /api/retrieval/stats`
- `POST /api/retrieval/refresh`
- `GET /api/reliability`
- `GET /api/runs/{run_id}`
- `GET /api/runs/{run_id}/events`
- `GET /api/runs/{run_id}/artifact.txt`
- `GET /api/error-catalog`
- `GET /api/runs/{run_id}/recovery-options`
- `POST /api/runs/{run_id}/recover`
- `GET /api/recovery/playbook`
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

CI-safe offline baseline (no `codex` CLI required):

```bash
./scripts/run_parity_offline.sh
```

The offline baseline echoes `expected_contains` tokens as synthetic output and always scores 1.0
`expected_match`. Use it in CI pipelines to verify the harness and benchmark file remain valid.

Weekly automation against a live `codex` deployment (local cron/systemd-friendly):

```bash
./scripts/run_parity_weekly.sh
```

Example cron entry (Sundays at 09:00):

```cron
0 9 * * 0 cd /path/to/codex-telegram-bot && ./scripts/run_parity_weekly.sh >> /tmp/codex-parity.log 2>&1
```

Category filter (run only a subset of cases):

```bash
PYTHONPATH=src python3 -m codex_telegram_bot.eval_parity \
  --cases docs/benchmarks/parity_cases.json \
  --category safety
```

Available categories: `smoke`, `code_editing`, `debugging`, `domain_knowledge`, `multi_step`,
`safety`, `security`, `output_format`, `latency`, `session`.

Current benchmark: **20 cases** across all categories (minimum 15 required by gate).

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

## Access Control

Role-based action authorization is enforced by `AccessController`:

| Role | Actions |
|------|---------|
| `viewer` | `view_status`, `view_help` |
| `user` *(default)* | all viewer actions + `send_prompt`, `approve_tool`, `deny_tool`, `reset_session`, `branch_session`, `interrupt_run`, `continue_run` |
| `admin` | all user actions + `switch_provider`, `manage_agents`, `view_logs`, `prune_sessions` |

Additional enforcement:
- **Per-user daily spend ceiling** (default `$10.00/day`, configurable per `UserProfile`)
- **Secret scanning** on inbound text detects AWS access keys, GitHub tokens, Stripe keys, bearer tokens, and generic API keys before forwarding to the provider

## Workspace Quotas

Each session gets an isolated workspace directory under `SESSION_WORKSPACES_ROOT`.
Quotas are enforced by `WorkspaceManager`:

- `WORKSPACE_MAX_DISK_BYTES` (default 100 MB) — total bytes across all files in the workspace
- `WORKSPACE_MAX_FILE_COUNT` (default 5000) — total number of files

Attempts to write beyond quota raise `WorkspaceQuotaExceeded`.

## Session Retention

`SessionRetentionPolicy` runs on demand (e.g. via a cron job calling `AgentService.run_retention_sweep()`):

- Sessions idle for `SESSION_ARCHIVE_AFTER_IDLE_DAYS` (default 30) are moved to `archived` status
- Archived sessions older than `SESSION_DELETE_AFTER_DAYS` (default 90) are hard-deleted along with their messages

## Capability-Based Provider Routing

`CapabilityRouter` wraps the `ProviderRegistry` and selects the best provider per request:

1. Filter registered providers by `required_caps` dict (e.g. `{"supports_streaming": True}`)
2. Among matching providers, prefer one with `supports_streaming` if `prefer_streaming=True`
3. Among matching providers, prefer the currently active provider
4. Falls back to the active provider when no match is found

At startup, `CodexCliProvider` is registered as `codex_cli` and is active by default.

## Telegram Session Runtime

- Each Telegram chat/user pair gets a persisted active session.
- Session history is stored in SQLite and reused for subsequent prompts.
- `/new` (or `/reset`) archives current session and starts a fresh one.
- `/resume [session_prefix]` resumes active session or switches to a matching past session.
- `/branch` creates a new session branched from current recent history.
- Session metadata is visible in Control Center (`/sessions` and `/api/sessions`).
- Retention policy compacts old session history when message count exceeds configured limits.
- Tool execution uses isolated per-session workspace directories managed by `WorkspaceManager`.

## Tool Loop (MVP)

- Messages can include deterministic shell actions using `!exec ...` lines.
- Example:
  - `!exec /bin/ls -la`
  - `!exec /usr/bin/git status`
- Registered tools are available via explicit `!tool` JSON:
  - `!tool {"name":"read_file","args":{"path":"README.md"}}`
  - `!tool {"name":"write_file","args":{"path":"notes.txt","content":"hello"}}`
  - `!tool {"name":"git_status","args":{"short":true}}`
- Structured loop objects are also supported:
  - `!loop {"steps":[{"kind":"exec","command":"/bin/ls -la"},{"kind":"tool","tool":"git_status","args":{"short":true}}],"final_prompt":"Summarize findings"}`
- The agent executes listed actions, captures observations, and injects them into the provider prompt.
- High-risk actions require explicit approval:
  - `/pending` to list pending approvals
  - `/approve <approval_id>` to execute approved action
  - `/deny <approval_id>` to reject pending action
- Tool loop enforces per-message max step budget (`TOOL_LOOP_MAX_STEPS`).
- Pending approvals expire automatically after `APPROVAL_TTL_SEC`.
- Pending approvals are capped per user (`MAX_PENDING_APPROVALS_PER_USER`) to reduce abuse risk.
- Telegram now emits step-by-step loop progress messages (start, step start, approval wait, finish).
- Progress updates are delivered via in-place status message edits to reduce chat noise.
- Progress updates are impermanent: in-place status messages auto-delete shortly after completion.
- High-risk tool approvals include Telegram 1/2/3 action buttons:
  - `1) Allow once`
  - `2) Deny`
  - `3) Show pending`
- `/interrupt` cancels active run for current chat (queued job + in-flight task).
- `/continue` reuses latest user prompt and asks the agent to continue the task.
- Safe replay guardrails:
  - high-risk replays require explicit confirmation (`/continue yes`)
  - completed tool steps can be checkpoint-skipped on identical replay to avoid duplicate execution

## Capability Registry

- Capability markdowns live in `capabilities/`:
  - `capabilities/system.md`
  - `capabilities/git.md`
  - `capabilities/files.md`
- Registry behavior is lean:
  - load markdowns dynamically from `CAPABILITIES_DIR`
  - select only relevant capabilities by prompt keywords
  - inject only short summaries, never full markdown contents

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

See [docs/support.md](docs/support.md) for the full troubleshooting playbook, FAQ,
and bug reporting instructions.

Quick checks:

- `Error: codex CLI not found.`
  - Ensure `codex` is installed and available in `PATH` for the runtime user.
- Bot does not respond in Telegram
  - Verify token correctness
  - Verify allowlist includes your Telegram user ID
  - Check stderr logs for handler exceptions

## License

MIT
