# Changelog

All notable changes to this project are documented in this file.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [0.3.0] – 2026-02-26

### Added

**Provider backend adapter layer**
- `PROVIDER_BACKEND` env var now supports `codex-cli` (default),
  `responses-api` (OpenAI Responses API), and `codex-exec-fallback` (alias).
- `providers/responses_api.py`: OpenAI Responses API adapter with native
  function-calling support; requires `httpx`; configured via `OPENAI_API_KEY`,
  `OPENAI_MODEL` (default `gpt-4o`), `OPENAI_MAX_TOKENS`, `OPENAI_TIMEOUT_SEC`,
  `OPENAI_API_BASE`.  Includes `generate_with_tools()` for iterative tool loops.

**Probe-first tool selection loop**
- `services/probe_loop.py`: `ProbeLoop` service adds a PROBE step before any
  tool execution.  The model responds with `NO_TOOLS\n<answer>` (direct path,
  no tools run) or `NEED_TOOLS {...}` (declares which tools and a goal).
- Hard runtime tool gate: tool calls not in the probe-declared `allowed_tools`
  set are blocked; one REPAIR attempt is made; loop exits gracefully on failure.
- Tool catalog injected at PROBE time is ≤200 chars.
- Tool schemas for the execution step are capped at 800 chars.
- Activated via `ENABLE_PROBE_LOOP=true`; off by default (zero behaviour change
  for existing deployments).

**Per-session memory MD files**
- `services/session_memory_files.py`: manages `facts.md` (stable project facts,
  overwrite-on-update) and `worklog.md` (append-only timestamped task log) in
  each session workspace.
- Injected into prompts on-demand only (capped at 600 chars total) when the
  files exist and contain non-whitespace content.
- `worklog.md` is updated automatically after probe-loop tool runs.

**Tool-driven capability injection**
- When `allowed_tools` is provided (e.g. from a PROBE result), `AgentService`
  injects capability summaries only for the selected tools rather than using
  keyword matching.  Generic hints are never injected when tools are known.

**Hard prompt char budgets** (tightened from 0.2.0 values):
- History: 4 000 chars (was 6 500)
- Retrieval: 2 500 chars (was 4 000)
- Summary: 1 000 chars (was 1 200)
- Tool schemas: 800 chars (new)
- Memory snippets: 600 chars (new)
- Total budget unchanged: 12 000 chars

**Email tool with safety gating**
- `tools/email.py`: `SendEmailTool` (`send_email`) sends email via SMTP.
- Absent from the tool catalog and registry unless `ENABLE_EMAIL_TOOL=true`.
- `requires_approval = True` — the approval gate in `AgentService` must be
  cleared before the tool executes.
- Supports `dry_run=True` for safe preview.
- Configuration: `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`,
  `EMAIL_FROM`.

### Changed

- `app_container.py`: `PROVIDER_BACKEND` values besides `codex-cli` no longer
  raise immediately; `responses-api` is fully supported.
- `tools/__init__.py`: `SendEmailTool` registered automatically when
  `ENABLE_EMAIL_TOOL=true`.
- `services/agent_service.py`: `ProbeLoop` is an optional dependency; when not
  set (default), behaviour is identical to 0.2.0.

---

## [0.2.0] – 2026-02-25

### Added

**Control Center authentication**
- `CONTROL_CENTER_UI_SECRET` environment variable gates all HTML UI pages behind
  a login page (`GET /login`, `POST /login`, `GET /logout`).  When not set the
  Control Center remains open for local / localhost-only deployments.
- `_opt_api_scope()` helper: when `LOCAL_API_KEYS` is configured every `/api/*`
  route requires a valid scoped token.  Admin-only mutations (plugin management,
  provider switching) require `admin:*` scope.  `/health` is always open.

**RBAC scopes for Control Center API**
- GET endpoints require `api:read`; POST/mutating endpoints require `api:write`;
  destructive/admin endpoints require `admin:*`.

**Onboarding readiness endpoint**
- `GET /api/onboarding/readiness` returns structured pass/fail checks for:
  workspace writability, codex CLI availability, and Telegram token presence.

**Parity evaluation – offline telegram mode**
- `--offline-telegram` flag added to `eval_parity.py`: skips the live
  `TelegramAgentRunner` and uses synthetic expected-token output.  Combine with
  `--offline-baseline` for fully offline CI parity runs.
- CI parity workflow updated: runs all 20 benchmark cases offline, all gates pass.
- Two consecutive passing parity reports generated and committed.

**CI test workflow**
- `.github/workflows/ci.yml`: runs `pytest tests/ -v` on Python 3.11 and 3.12
  for every push and pull request.

**Startup codex CLI preflight**
- `cli.py` now checks for `codex` in PATH before entering serving mode and
  prints a clear, actionable warning (with install instructions and a link to
  the readiness endpoint) when the CLI is missing.

**Documentation**
- `docs/deployment.md`: system requirements, env vars, systemd/Docker, reverse
  proxy, backup/restore, upgrade procedure, multi-tenant isolation guide.
- `docs/security.md`: threat model (5 threat categories), hardening checklist,
  known limitations.
- `docs/reliability_ops.md`: extended with Prometheus/Grafana scrape examples,
  sample alert rules, and webhook payload format.
- `docs/support.md`: user-facing troubleshooting playbook, FAQ, log collection,
  bug reporting process.

### Changed

- **Version** synced across all locations: `pyproject.toml`, `__init__.py`, and
  the FastAPI app all report `0.2.0` (was `0.1.0` / `0.2.0-alpha`).
- `[dev]` optional dependency group added to `pyproject.toml` so
  `pip install -e ".[dev]"` installs pytest and httpx for local testing.

### Fixed

- Version mismatch between `pyproject.toml` (`0.1.0`) and Control Center app
  (`0.2.0-alpha`) resolved.

---

## [0.1.0] – 2026-02-01

### Added

**EPIC #64 – Agent Core Foundation**
- `AgentService`: orchestration layer with session management, run lifecycle,
  and event bus.
- `TelegramBot`: PTB-based polling transport with `/ping`, `/status`, `/help`,
  `/new`, `/reset`, `/branch`, `/resume`.

**EPIC #65 – Secure Computer Interaction Layer**
- `AccessController`: viewer / user / admin RBAC with per-user daily spend
  ceilings and secret scanning (AWS, GitHub, Stripe, bearer tokens, API keys).
- `ExecutionPolicy`: policy profiles (`strict`, `balanced`, `trusted`) with
  command allowlists, workspace enforcement, and timeout caps.
- `WorkspaceManager`: per-session isolated workspace directories with
  configurable disk-byte and file-count quotas.

**EPIC #66 – Multi-Provider Architecture**
- `ProviderRegistry`: runtime provider registry with hot-switch support.
- `CodexCliProvider`: primary provider wrapping `codex exec`.
- `AnthropicProvider`: Claude API provider (SDK + httpx fallback, streaming).
- `EchoFallbackProvider`: degraded-mode fallback activated by circuit-breaker.
- `CapabilityRouter`: capability-based provider selection.

**EPIC #67 – Streaming and CLI-like Feedback**
- `StreamingUpdater`: in-place Telegram message edits for step-by-step progress.
- `/interrupt`, `/continue`, `/continue yes` commands.

**EPIC #68 – Lightweight Web Control Center**
- FastAPI-based Control Center with dashboard, runs, sessions, approvals,
  agents, plugins, settings, and onboarding wizard.
- `GET /api/v1/*` scoped integration API with `LOCAL_API_KEYS` auth.

**Parity 1–10: incremental production hardening**
- Session retention policy (idle archival + hard delete).
- Tool approval gate (SQLite-backed, TTL, per-user cap).
- Repo context retrieval with symbol-aware scoring.
- Workspace quota enforcement.
- Observability and structured JSON logging.
- Runbook registry with recovery playbook.
- Error catalog with stable error codes and one-click recovery actions.
- Parity evaluation harness with 20 benchmark cases.

---

## Upgrade Notes

### 0.1.0 → 0.2.0

- **No breaking changes** to the Telegram bot command interface.
- **Control Center API**: If you set `LOCAL_API_KEYS`, all `/api/*` routes now
  require auth (not just `/api/v1/*`).  Update any scripts or integrations that
  called unprotected endpoints.
- **UI auth**: Set `CONTROL_CENTER_UI_SECRET` to protect the web dashboard.  If
  left unset, behaviour is identical to 0.1.0 (open localhost access).
- Run `pip install -e ".[dev]"` to install the new dev dependencies group.

---

## Versioning Policy

- **Patch** (`x.y.Z`): bug fixes, doc updates, no API changes.
- **Minor** (`x.Y.0`): new features, backwards-compatible API additions.
- **Major** (`X.0.0`): breaking changes to API, config schema, or DB format;
  accompanied by a migration guide.

Pre-1.0 minor bumps may include breaking changes with appropriate changelog
entries and upgrade notes.
