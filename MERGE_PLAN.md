# MERGE PLAN: Zero Claw → Codex Telegram Bot (Agent HQ)

**Principle**: Zero Claw becomes the ENGINE. Where functionality overlaps, **Zero Claw wins**.
**Invariant**: Everything in Zero Claw remains fully existing and functional.

---

## Architecture Overview (Post-Merge)

```
┌──────────────────────────────────────────────────────────────┐
│                    AGENT HQ (Codex Telegram Bot)             │
│                                                              │
│  ┌────────────┐  ┌────────────┐  ┌────────────────────────┐  │
│  │  Chrome     │  │  VSCode    │  │  Control Center (GUI)  │  │
│  │  Extension  │  │  Extension │  │  FastAPI + WebSocket   │  │
│  └─────┬──────┘  └─────┬──────┘  └───────────┬────────────┘  │
│        │               │                      │               │
│  ┌─────▼───────────────▼──────────────────────▼────────────┐  │
│  │              Python Integration Layer                    │  │
│  │  (Skills Marketplace, Plugin Lifecycle, Cost Tracking,   │  │
│  │   Soul/Personality, Onboarding, Browser Bridge,          │  │
│  │   WhatsApp Bridge, Mission Runner)                       │  │
│  └─────────────────────┬───────────────────────────────────┘  │
│                        │  Python bindings / FFI / subprocess   │
│  ┌─────────────────────▼───────────────────────────────────┐  │
│  │             ★ ZERO CLAW ENGINE (Rust) ★                 │  │
│  │                                                         │  │
│  │  ┌─────────┐ ┌──────────┐ ┌────────┐ ┌──────────────┐  │  │
│  │  │ Agent   │ │ Provider │ │ Memory │ │  Security    │  │  │
│  │  │ Loop    │ │ (22+LLMs)│ │ Hybrid │ │  (Pairing,   │  │  │
│  │  │         │ │          │ │ Search │ │   Sandbox,   │  │  │
│  │  │         │ │          │ │        │ │   Encrypted) │  │  │
│  │  └─────────┘ └──────────┘ └────────┘ └──────────────┘  │  │
│  │  ┌─────────┐ ┌──────────┐ ┌────────┐ ┌──────────────┐  │  │
│  │  │ Channel │ │ Tool     │ │Runtime │ │  Gateway     │  │  │
│  │  │ Transp. │ │ System   │ │Native/ │ │  Webhooks    │  │  │
│  │  │         │ │ (+MCP)   │ │Docker  │ │              │  │  │
│  │  └─────────┘ └──────────┘ └────────┘ └──────────────┘  │  │
│  │  ┌───────────────────────────────────────────────────┐  │  │
│  │  │                 Daemon Service                    │  │  │
│  │  └───────────────────────────────────────────────────┘  │  │
│  └─────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────┘
```

---

## Phase 0: Foundation Setup
**Goal**: Bring Zero Claw into the repository and establish the build pipeline.

### Step 0.1 — Import Zero Claw as a Subtree
- Clone `michalptacnik/zeroclaw` into `engine/` directory at repo root
- Preserve full git history via `git subtree add`
- Directory layout: `engine/` = complete Zero Claw codebase (untouched)

### Step 0.2 — Unified Build System
- Add a top-level `Makefile` (or `justfile`) with targets:
  - `make engine` — builds Zero Claw Rust binary (`cargo build --release` inside `engine/`)
  - `make bot` — installs Python deps (`pip install -e .`)
  - `make all` — builds both
  - `make test` — runs both Rust and Python tests
  - `make docker` — multi-stage Docker image (Rust build → Python runtime)
- Update `docker-compose.yml` to use multi-stage build:
  - Stage 1: Rust builder (compile Zero Claw)
  - Stage 2: Python runtime (copy binary + install Python package)
- Update `pyproject.toml` to declare Zero Claw binary as a build dependency
- Add CI workflow: `.github/workflows/build.yml`

### Step 0.3 — Python ↔ Rust Bridge
- Implement bridge layer in `src/codex_telegram_bot/engine/` (new package):
  - `bridge.py` — subprocess wrapper to communicate with Zero Claw binary via stdin/stdout JSON-RPC or Unix socket
  - `types.py` — shared data models (Pydantic) matching Zero Claw's Rust structs
  - `config.py` — translates existing `.env` / `config.py` settings into Zero Claw's `config.toml`
- Alternative: If Zero Claw's Python bindings (`python/`) are mature enough, use PyO3/maturin FFI directly instead of subprocess

---

## Phase 1: Replace Core Engine Components
**Goal**: Swap codex-telegram-bot internals for Zero Claw equivalents. Zero Claw wins on all overlapping functionality.

### Step 1.1 — Agent Loop (REPLACE)
| Component | Current (Python) | New (Zero Claw) |
|-----------|-----------------|-----------------|
| Agent loop | `agent_core/agent.py`, `agent_core/router.py` | Zero Claw `src/agent/` |
| Probe pattern | `services/probe_loop.py` | Zero Claw agent planning |
| Tool execution loop | `services/agent_service.py` (run_prompt_with_tool_loop) | Zero Claw agent loop |

**Actions:**
- Wire `Agent.handle_message()` to forward to Zero Claw engine via bridge
- The Python `AgentRouter` becomes a thin proxy that:
  1. Receives message from transport
  2. Serializes to Zero Claw `ChannelMessage` format
  3. Sends to Zero Claw engine
  4. Receives response and forwards back to transport
- Remove/deprecate: `agent_service.py`'s tool loop, probe loop, protocol repair logic
- Keep `agent_service.py` as a session/state coordinator only (session lifecycle, not AI logic)

### Step 1.2 — Provider Layer (REPLACE)
| Component | Current (Python) | New (Zero Claw) |
|-----------|-----------------|-----------------|
| Provider registry | `providers/registry.py` | Zero Claw trait-based providers |
| Anthropic | `providers/anthropic_provider.py` | Zero Claw Anthropic provider |
| OpenAI | `providers/openai_compatible.py` | Zero Claw OpenAI provider |
| Gemini | `providers/gemini_provider.py` | Zero Claw Gemini provider |
| Codex CLI | `providers/codex_cli.py` | Zero Claw custom provider |
| Circuit breaker | `providers/router.py` | Zero Claw provider failover |

**Actions:**
- Zero Claw already supports 22+ providers → delete all Python provider files
- Provider switching UI in Control Center sends config update to Zero Claw via bridge
- Codex CLI provider: port as a custom Zero Claw provider if not already supported, OR keep as a Python-side fallback that delegates to Zero Claw for other providers
- `prices.json` stays in Python layer for cost tracking (Zero Claw doesn't do billing)

### Step 1.3 — Tool System (REPLACE)
| Component | Current (Python) | New (Zero Claw) |
|-----------|-----------------|-----------------|
| Shell exec | `tools/shell.py` | Zero Claw shell tool |
| Git ops | `tools/git.py` | Zero Claw git tool |
| File I/O | `tools/files.py` | Zero Claw file tool |
| Web/HTTP | `tools/web.py` | Zero Claw HTTP tool |
| MCP tools | `services/mcp_bridge.py` | Zero Claw MCP tool loading |
| Cron/schedule | `tools/schedule.py` | Zero Claw cron tool |
| Tool registry | `tools/base.py`, `tools/runtime_registry.py` | Zero Claw tool registry |

**Actions:**
- Replace ALL overlapping tool implementations with Zero Claw equivalents
- Keep Python-only tools that Zero Claw doesn't have (as bridge-called Python tools):
  - `tools/email.py`, `tools/email_assets.py` (SMTP)
  - `tools/browser.py` (Chrome extension bridge)
  - `tools/skill_market.py` (marketplace)
  - `tools/outbound.py` (GitHub issues)
  - `tools/sessions.py` (session management — may partially overlap)
  - `tools/memory.py` (soul/personality — unique to Agent HQ)
  - `tools/heartbeat.py` (proactive messaging)
  - `tools/message.py` (Telegram message sending)
  - `tools/provider.py` (runtime provider switching UI)
  - `tools/tasks.py` (task tracking)
  - `tools/ssh.py` (SSH key detection)
- Register Python-only tools with Zero Claw's tool registry via bridge so the agent can call them

### Step 1.4 — Execution Runtime (REPLACE)
| Component | Current (Python) | New (Zero Claw) |
|-----------|-----------------|-----------------|
| Local shell | `execution/local_shell.py` | Zero Claw native runtime |
| Docker sandbox | `execution/docker_sandbox.py` | Zero Claw Docker runtime |
| Process manager | `execution/process_manager.py` | Zero Claw daemon |
| PTY spawn | `execution/pty_spawn.py` | Zero Claw native runtime |
| Policy engine | `execution/policy.py` | Zero Claw security module |
| Profiles | `execution/profiles.py` | Zero Claw autonomy levels |

**Actions:**
- Zero Claw's runtime module replaces all Python execution backends
- Map existing execution profiles to Zero Claw autonomy levels:
  - `safe` → Zero Claw restricted mode
  - `balanced` → Zero Claw standard mode
  - `power_user` → Zero Claw elevated mode
  - `unsafe` → Zero Claw unrestricted mode (with pairing/unlock)
- Zero Claw's security module (pairing, sandboxing, allowlists, encrypted secrets) replaces `execution/policy.py`

### Step 1.5 — Memory & Persistence (REPLACE)
| Component | Current (Python) | New (Zero Claw) |
|-----------|-----------------|-----------------|
| SQLite store | `persistence/sqlite_store.py` | Zero Claw SQLite + sqlite-vec |
| Thin memory | `services/thin_memory.py` | Zero Claw hybrid search memory |
| Session retention | `services/session_retention.py` | Zero Claw memory management |

**Actions:**
- Zero Claw's hybrid search engine (vector + BM25 + FTS5 + RRF) replaces thin memory
- Write a one-time migration script: `scripts/migrate_to_zeroclaw_memory.py`
  - Exports existing `state.db` data → imports into Zero Claw's memory format
  - Migrates session history, runs, agents, missions
  - Generates embeddings for existing memory pages
- Keep `SOUL.md` and `MEMORY_INDEX.md` as inputs to Zero Claw's memory (load at startup)
- Keep daily logs format — configure Zero Claw to write to same structure

### Step 1.6 — Channel Transports (REPLACE)
| Component | Current (Python) | New (Zero Claw) |
|-----------|-----------------|-----------------|
| Telegram | `transports/telegram_transport.py` + `telegram_bot.py` | Zero Claw Telegram channel |
| Discord | `transports/discord_transport.py` | Zero Claw Discord channel |

**Actions:**
- Zero Claw handles Telegram polling/webhook natively → remove Python Telegram transport
- Zero Claw handles Discord natively → remove Python Discord transport
- Keep Python-only transports as bridge channels:
  - WhatsApp (Twilio) — `services/whatsapp_bridge.py` (register as custom Zero Claw channel)
  - Control Center WebSocket — `control_center/app.py` (bridge channel)

### Step 1.7 — Security (REPLACE)
| Component | Current (Python) | New (Zero Claw) |
|-----------|-----------------|-----------------|
| Access control | `services/access_control.py` | Zero Claw security module |
| Secret redaction | `services/access_control.py` | Zero Claw encrypted secrets |
| Execution policy | `execution/policy.py` | Zero Claw sandboxing + allowlists |

**Actions:**
- Zero Claw's pairing, sandboxing, filesystem scoping, and encrypted secrets replace Python access control
- Map existing roles (viewer/user/admin) to Zero Claw pairing levels
- Keep Python-side cost-based spend ceilings (Zero Claw doesn't do billing)

---

## Phase 2: Integrate Agent HQ-Unique Features with Zero Claw
**Goal**: Wire codex-telegram-bot features that don't exist in Zero Claw into the new engine.

### Step 2.1 — Soul/Personality System
- Zero Claw doesn't have an equivalent personality/identity system
- Keep `services/soul.py` in Python layer
- Inject `SOUL.md` content into Zero Claw's system prompt via bridge config
- Control Center soul editing → writes `SOUL.md` → signals Zero Claw to reload

### Step 2.2 — Skill Marketplace & Plugin System
- Keep `services/skill_manager.py`, `services/skill_marketplace.py`, `services/plugin_lifecycle.py` in Python
- Register marketplace-installed skills as Zero Claw tools via bridge
- Plugin lifecycle (install/enable/disable/audit) stays Python-side
- Plugin-provided tools get registered with Zero Claw's tool registry dynamically

### Step 2.3 — Cost Tracking & Billing
- Keep `services/cost_tracking.py` and `prices.json` in Python
- Hook into Zero Claw's provider responses to extract token counts
- Bridge emits usage events → Python cost tracker aggregates
- Spend ceilings enforced at bridge level (cut off requests when ceiling hit)

### Step 2.4 — Mission Runner
- Keep `services/mission_runner.py` in Python
- Missions decompose into steps → each step dispatched to Zero Claw engine
- Mission state machine (idle/running/paused/completed) stays Python-side
- Budget enforcement at mission level wraps Zero Claw calls

### Step 2.5 — Cron/Heartbeat Agent
- Zero Claw has cron tool → use it for scheduled command execution
- Keep `services/cron_agent.py` for Agent HQ-specific heartbeat logic:
  - Git repo monitoring
  - Proactive messaging with quiet hours
  - System health checks
- Heartbeat triggers → dispatched to Zero Claw engine for execution

### Step 2.6 — Browser Bridge (Chrome Extension)
- Keep `services/browser_bridge.py` and `chrome-extension/` entirely
- Register browser tools with Zero Claw via bridge
- Chrome extension connects to Control Center → forwards to Zero Claw

### Step 2.7 — Email System
- Keep `tools/email.py`, `tools/email_assets.py` in Python
- Register as Zero Claw tools via bridge
- SMTP configuration stays in Python `.env`

### Step 2.8 — Observability
- Keep `observability/structured_log.py` and `observability/alerts.py`
- Add Zero Claw log forwarding: Zero Claw logs → Python structured logger
- Prometheus metrics: expose Zero Claw's internal metrics alongside Python metrics

---

## Phase 3: Control Center GUI Integration
**Goal**: The web dashboard talks to Zero Claw engine through the Python bridge.

### Step 3.1 — API Endpoints Update
Update `control_center/app.py` endpoints:

| Endpoint | Change |
|----------|--------|
| `/api/sessions` | Query Zero Claw memory for sessions |
| `/api/runs` | Query Zero Claw for execution history |
| `/api/cost` | Keep as-is (Python cost tracker) |
| `/api/runtime/capabilities` | Query Zero Claw for available tools/providers |
| `/api/plugins` | Keep as-is (Python plugin lifecycle) |
| `/api/skills` | Keep as-is + include Zero Claw native tools |
| `/api/agents` | Query/configure Zero Claw agent settings |
| `/api/providers` | Proxy to Zero Claw provider switching |
| `/ws/chat` | Messages routed through Zero Claw engine |

### Step 3.2 — New GUI Panels
Add new Control Center pages for Zero Claw features:
- **Engine Status** — Zero Claw health, uptime, memory usage, provider status
- **Memory Search** — Interactive hybrid search (vector + keyword) via Zero Claw
- **Security Dashboard** — Pairing status, sandbox config, allowlists, encrypted secrets
- **Channel Manager** — Enable/disable/configure channels (Telegram, Discord, Slack, Matrix, Email)
- **Gateway Config** — Webhook endpoints, tunnel status (Cloudflare/Tailscale/ngrok)

### Step 3.3 — Settings Unification
Create unified settings page combining:
- Zero Claw `config.toml` settings (provider, channels, memory, security, runtime)
- Agent HQ Python settings (soul, cost ceilings, skills, plugins, email, heartbeat)
- Single settings UI that writes to both config sources

---

## Phase 4: VSCode & Chrome Extension Updates
**Goal**: Extensions work seamlessly with Zero Claw engine.

### Step 4.1 — VSCode Extension
- Gateway URL still points to Control Center (FastAPI)
- No changes needed to extension code — it talks to Control Center, which now routes through Zero Claw
- Update status bar to show Zero Claw engine status (connected/provider/memory stats)

### Step 4.2 — Chrome Extension
- Still connects to Control Center WebSocket
- No changes needed to extension code
- Browser context forwarded through bridge to Zero Claw

---

## Phase 5: Migration & Backwards Compatibility
**Goal**: Existing users can upgrade smoothly.

### Step 5.1 — Data Migration Script
Create `scripts/migrate_v1_to_v2.py`:
1. Export existing `state.db` (schema v9) data
2. Import sessions, messages, runs, agents, missions into Zero Claw memory
3. Convert memory pages to Zero Claw searchable documents (generate embeddings)
4. Preserve `SOUL.md` and `MEMORY_INDEX.md`
5. Map existing provider config to `config.toml`
6. Preserve all `.env` settings

### Step 5.2 — Config Migration
- Auto-generate `config.toml` from existing `.env` on first startup
- Keep `.env` support for Agent HQ-specific settings (cost, email, plugins)
- Warn on deprecated settings

### Step 5.3 — Fallback Mode
- If Zero Claw binary not found/not built → fall back to pure-Python mode (current behavior)
- Log warning: "Running without Zero Claw engine — reduced functionality"
- Allows gradual adoption

---

## Phase 6: Testing & Validation

### Step 6.1 — Integration Tests
- Bridge communication tests (Python ↔ Zero Claw)
- Provider delegation tests (all 22+ providers work through bridge)
- Tool execution tests (native + Python-bridged tools)
- Memory migration verification
- Channel transport tests (Telegram, Discord through Zero Claw)

### Step 6.2 — Regression Tests
- All existing Control Center API endpoints return expected data
- Chrome/VSCode extensions connect and function
- Cost tracking accurate with Zero Claw provider
- Soul/personality injection works
- Plugin/skill marketplace functional

### Step 6.3 — Performance Benchmarks
- Compare response latency: Python-only vs Zero Claw engine
- Memory usage comparison
- Startup time comparison
- Concurrent session handling

---

## File Deletion Plan (Post-Migration)

Files to DELETE (replaced by Zero Claw):
```
src/codex_telegram_bot/providers/anthropic_provider.py
src/codex_telegram_bot/providers/openai_compatible.py
src/codex_telegram_bot/providers/gemini_provider.py
src/codex_telegram_bot/providers/router.py
src/codex_telegram_bot/providers/registry.py
src/codex_telegram_bot/tools/shell.py
src/codex_telegram_bot/tools/git.py
src/codex_telegram_bot/tools/files.py
src/codex_telegram_bot/tools/web.py
src/codex_telegram_bot/tools/base.py
src/codex_telegram_bot/tools/runtime_registry.py
src/codex_telegram_bot/execution/local_shell.py
src/codex_telegram_bot/execution/docker_sandbox.py
src/codex_telegram_bot/execution/process_manager.py
src/codex_telegram_bot/execution/pty_spawn.py
src/codex_telegram_bot/execution/policy.py
src/codex_telegram_bot/execution/profiles.py
src/codex_telegram_bot/persistence/sqlite_store.py
src/codex_telegram_bot/services/thin_memory.py
src/codex_telegram_bot/services/probe_loop.py
src/codex_telegram_bot/services/mcp_bridge.py
src/codex_telegram_bot/services/access_control.py
src/codex_telegram_bot/transports/telegram_transport.py
src/codex_telegram_bot/transports/discord_transport.py
```

Files to KEEP (unique to Agent HQ):
```
src/codex_telegram_bot/services/soul.py
src/codex_telegram_bot/services/cost_tracking.py
src/codex_telegram_bot/services/skill_manager.py
src/codex_telegram_bot/services/skill_marketplace.py
src/codex_telegram_bot/services/plugin_lifecycle.py
src/codex_telegram_bot/services/mission_runner.py
src/codex_telegram_bot/services/cron_agent.py
src/codex_telegram_bot/services/browser_bridge.py
src/codex_telegram_bot/services/whatsapp_bridge.py
src/codex_telegram_bot/services/observability.py
src/codex_telegram_bot/services/workspace_manager.py
src/codex_telegram_bot/tools/email.py
src/codex_telegram_bot/tools/email_assets.py
src/codex_telegram_bot/tools/browser.py
src/codex_telegram_bot/tools/skill_market.py
src/codex_telegram_bot/tools/outbound.py
src/codex_telegram_bot/tools/sessions.py
src/codex_telegram_bot/tools/memory.py
src/codex_telegram_bot/tools/heartbeat.py
src/codex_telegram_bot/tools/message.py
src/codex_telegram_bot/tools/provider.py
src/codex_telegram_bot/tools/tasks.py
src/codex_telegram_bot/tools/ssh.py
src/codex_telegram_bot/control_center/app.py
src/codex_telegram_bot/observability/
src/codex_telegram_bot/config.py
src/codex_telegram_bot/cli.py
chrome-extension/
vscode-extension/
```

New files to CREATE:
```
engine/                              # Zero Claw subtree (entire repo)
src/codex_telegram_bot/engine/       # Python ↔ Rust bridge package
  __init__.py
  bridge.py                          # Communication with Zero Claw binary
  types.py                           # Shared data models
  config.py                          # Config translation (.env → config.toml)
  tool_bridge.py                     # Register Python tools with Zero Claw
  channel_bridge.py                  # Register Python channels with Zero Claw
scripts/migrate_v1_to_v2.py          # Data migration script
Makefile                             # Unified build system
```

---

## Implementation Order (Recommended)

1. **Phase 0** — Foundation (1-2 days): Import, build system, bridge skeleton
2. **Phase 1.1-1.2** — Agent loop + providers (2-3 days): Core engine swap
3. **Phase 1.3-1.4** — Tools + runtime (2-3 days): Execution layer swap
4. **Phase 1.5** — Memory (1-2 days): Persistence migration
5. **Phase 1.6-1.7** — Channels + security (1-2 days): Transport + auth swap
6. **Phase 2** — Integration features (2-3 days): Soul, skills, cost, missions
7. **Phase 3** — GUI updates (1-2 days): Control Center rewiring
8. **Phase 4** — Extensions (0.5 days): Mostly verification
9. **Phase 5** — Migration (1 day): Scripts + backwards compat
10. **Phase 6** — Testing (2-3 days): Integration + regression + perf

---

## Key Decisions & Trade-offs

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Bridge mechanism | JSON-RPC over Unix socket | Fast IPC, language-agnostic, debuggable |
| Alt: PyO3 FFI | Use if Zero Claw Python bindings mature | Zero-copy, fastest, but tighter coupling |
| Config format | Keep `.env` for Python, `config.toml` for Zero Claw | Bridge auto-translates |
| Fallback mode | Pure Python if no engine binary | Graceful degradation for dev/CI |
| Memory migration | One-time script, no dual-write | Clean cut, simpler maintenance |
| Provider overlap | Delete all Python providers | Zero Claw has 22+ providers, superset |
| Tool overlap | Delete Python tools, bridge unique ones | Zero Claw tools are faster (Rust) |

---

## Risk Mitigation

| Risk | Mitigation |
|------|-----------|
| Bridge latency | Unix socket + msgpack serialization; benchmark early |
| Zero Claw API changes | Pin to specific Zero Claw release tag |
| Memory format incompatibility | Migration script with validation + rollback |
| Python tool registration complexity | Start with subprocess tool calls, optimize later |
| Build complexity (Rust + Python) | Multi-stage Docker, pre-built binaries for CI |
| User disruption | Fallback mode + migration script + clear upgrade docs |
