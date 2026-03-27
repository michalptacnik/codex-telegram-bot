# Agent HQ

**Autonomous AI agent runtime built in Rust. Powered by the ZeroClaw engine.**

Agent HQ is a production-grade, multi-channel, multi-provider AI agent runtime. It connects 22+ LLM providers to 26+ messaging channels through a modular, trait-driven architecture -- with 40+ built-in tools, hybrid memory, hardware peripheral support, and a full security stack.

Binary: `agent-hq` | Library: `zeroclaw` | ~166K lines of Rust across 229 source files

---

## Table of Contents

- [Architecture](#architecture)
- [Features](#features)
- [Quick Start](#quick-start)
- [Configuration](#configuration)
- [CLI Reference](#cli-reference)
- [Extending Agent HQ](#extending-agent-hq)
- [Development](#development)
- [Project Structure](#project-structure)
- [License](#license)

---

## Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│                           AGENT HQ                               │
│                                                                  │
│  Channels (26+)          Agent Core            LLM Providers     │
│  ┌─────────────┐     ┌───────────────┐     ┌──────────────┐     │
│  │ Telegram     │     │ Agent Loop    │     │ Anthropic    │     │
│  │ Discord      │     │ Planner       │     │ OpenAI       │     │
│  │ Slack        │◄───►│ Dispatcher    │◄───►│ Gemini       │     │
│  │ WhatsApp     │     │ Classifier    │     │ Ollama       │     │
│  │ Matrix       │     │ Memory Loader │     │ OpenRouter   │     │
│  │ Nostr        │     └───────┬───────┘     │ Azure        │     │
│  │ IRC / MQTT   │             │             │ Bedrock      │     │
│  │ Email / CLI  │             │             │ + 14 more    │     │
│  │ + 17 more    │     ┌───────▼───────┐     └──────────────┘     │
│  └─────────────┘     │   40+ Tools   │                           │
│                       │ Shell, File,  │     ┌──────────────┐     │
│  Gateway (axum)       │ Browser, Git, │     │ Memory       │     │
│  ┌─────────────┐     │ HTTP, Cron,   │     │ SQLite/PG    │     │
│  │ REST API    │     │ SOP, Hardware,│     │ Vector/RAG   │     │
│  │ WebSocket   │     │ MCP, Email,   │     │ Embeddings   │     │
│  │ SSE Events  │     │ Screenshot    │     │ Markdown     │     │
│  │ Webhooks    │     └───────────────┘     └──────────────┘     │
│  └─────────────┘                                                 │
│                                                                  │
│  Security ─ Observability ─ Sessions ─ Missions ─ Plugins ─ SOP │
└──────────────────────────────────────────────────────────────────┘
```

---

## Features

### Channels

Connect your agent to 26+ messaging platforms:

Telegram, Discord, Slack, WhatsApp (API + Web), Matrix, Signal, Nostr, IRC, MQTT, Email, iMessage, DingTalk, Lark, Mattermost, Nextcloud Talk, QQ, CLI, and more. Voice transcription and TTS built in.

### LLM Providers

22+ providers with automatic failover and cost tracking:

Anthropic, OpenAI, Google Gemini, Ollama, OpenRouter, Azure OpenAI, AWS Bedrock, GitHub Copilot, GLM, Telnyx, and any OpenAI-compatible endpoint. Provider routing lets you map different models to different tasks.

### Tools

40+ built-in tools the agent can invoke:

| Category | Tools |
|----------|-------|
| **System** | Shell execution, file read/write/edit, glob search, content search |
| **Web** | HTTP requests, web fetch, web search, browser automation |
| **Dev** | Git operations, GitHub issues/PRs, code discovery |
| **Memory** | Store, recall, forget -- backed by hybrid search |
| **Scheduling** | Cron add/list/remove/update, one-shot timers |
| **Hardware** | Board info, memory map, memory read (STM32, RPi GPIO) |
| **SOP** | Execute, advance, approve, list, status |
| **Other** | Screenshot, PDF read, image info, email, push notifications |

### Memory

Hybrid search combining Vector + BM25 + FTS5 with Reciprocal Rank Fusion. Backends: SQLite (default), PostgreSQL, Qdrant. Supports embeddings, chunking, snapshots, and response caching.

### Security

- Prompt injection detection guard
- Approval workflows for sensitive operations
- Device pairing and encrypted secret store
- Filesystem scoping and sandboxing (bubblewrap/firejail/landlock)
- Emergency stop

### SOP Engine

Standard Operating Procedures automation. Define multi-step workflows with approval gates, execute them through the agent, and track status in real time.

### Missions

Autonomous multi-step mission execution with pause/resume/stop, budget enforcement, and step-level retry.

### Observability

Structured logging, Prometheus metrics, OpenTelemetry traces, runtime tracing, and verbose debug mode.

### More

- **Sessions** -- per-chat, per-user isolated workspaces with disk quotas
- **Plugins** -- install/enable/disable with manifest validation and trust policies
- **Skills & SkillForge** -- community skill marketplace with auditing
- **RAG** -- retrieval-augmented generation pipeline
- **Cost tracking** -- per-provider, per-model monitoring with daily/monthly budgets
- **Heartbeat** -- proactive agent with configurable intervals
- **Hooks** -- pre/post command hooks with audit logging
- **Gateway** -- HTTP/WebSocket/SSE server with webhook ingestion
- **Tunnel** -- Cloudflare, Tailscale, ngrok support
- **Hardware peripherals** -- STM32 Nucleo, Raspberry Pi GPIO, ESP32, Arduino
- **Daemon** -- macOS launchd support with PID management and preflight checks
- **Browser bridge** -- Chrome extension coordination for browser context
- **Identity** -- AIEOS + markdown identity with soul/personality editing

---

## Quick Start

### Prerequisites

- Rust 1.87+ (`rustup install stable`)

### Build

```bash
cargo build --release
```

### Onboard

Interactive guided setup -- configures your provider keys, default model, and channels:

```bash
./target/release/agent-hq onboard
```

### OpenAI Codex Setup

For `openai-codex`, Agent HQ now uses the official OpenAI `codex` client as its compliant bridge.

```bash
codex login
codex login status
```

Then set `default_provider = "openai-codex"` in your Agent HQ config. Agent HQ does not perform direct ChatGPT OAuth for this provider.

### Run

```bash
# Start with configured channels (Telegram, Discord, etc.)
./target/release/agent-hq run

# Or run as a background daemon
./target/release/agent-hq daemon start

# Interactive CLI chat
./target/release/agent-hq chat
```

---

## Configuration

Agent HQ loads configuration from TOML, checking these paths in order:

1. `./config.toml` (project root)
2. `~/.config/zeroclaw/config.toml`

```toml
api_key = "sk-..."
default_provider = "anthropic"
default_model = "claude-sonnet-4-6"

[channels_config.telegram]
enabled = true
bot_token = "your-telegram-bot-token"
allowlist = ["your_username"]

[autonomy]
level = "full"

[gateway]
port = 8765

[memory]
backend = "sqlite"

[cost]
daily_budget_usd = 10.0
```

Run `agent-hq onboard --interactive` for guided setup, or `agent-hq doctor` to validate your configuration.

---

## CLI Reference

| Command | Description |
|---------|-------------|
| `agent-hq run` | Start the agent with all configured channels |
| `agent-hq daemon start\|stop\|status` | Manage the background daemon (macOS launchd) |
| `agent-hq chat` | Interactive CLI conversation |
| `agent-hq onboard` | Guided first-time setup |
| `agent-hq doctor` | Diagnose configuration and connectivity |
| `agent-hq gateway start` | Start the HTTP/WebSocket gateway |
| `agent-hq channel start` | Start configured messaging channels |
| `agent-hq sop` | Manage and execute Standard Operating Procedures |
| `agent-hq memory` | Query and manage agent memory |
| `agent-hq config` | View or export configuration |

---

## Extending Agent HQ

The architecture is trait-driven. Add new capabilities by implementing a trait and registering it in the corresponding factory module:

| Extension Point | Trait | Location |
|----------------|-------|----------|
| LLM Provider | `Provider` | `src/providers/traits.rs` |
| Channel | `Channel` | `src/channels/traits.rs` |
| Tool | `Tool` | `src/tools/traits.rs` |
| Memory Backend | `Memory` | `src/memory/traits.rs` |
| Observer | `Observer` | `src/observability/traits.rs` |
| Runtime Adapter | `RuntimeAdapter` | `src/runtime/traits.rs` |
| Peripheral | `Peripheral` | `src/peripherals/traits.rs` |
| Hook | `Hook` | `src/hooks/traits.rs` |

---

## Development

### Build and Test

```bash
# Build
cargo build --release

# Run tests
cargo test

# Lint
cargo clippy --all-targets -- -D warnings

# Format check
cargo fmt --all -- --check
```

### Full CI Validation

```bash
./dev/ci.sh all
```

### Project Structure

```
src/
  main.rs              CLI entrypoint and command routing
  lib.rs               Module exports (42 modules)
  agent/               Orchestration loop, planner, dispatcher, classifier
  channels/            26+ channel implementations
  providers/           22+ LLM provider implementations
  tools/               40+ tool implementations
  memory/              Markdown, SQLite, PostgreSQL, Qdrant, vector, embeddings
  security/            Policy engine, pairing, secret store, sandbox
  gateway/             HTTP/WS/SSE server, static files
  sop/                 Standard Operating Procedures engine
  config/              Schema, loading, merging
  observability/       Logging, Prometheus, OpenTelemetry, tracing
  peripherals/         Hardware peripherals (STM32, RPi GPIO)
  runtime/             Runtime adapters
  sessions/            Per-chat/user session management
  missions/            Autonomous multi-step missions
  plugins/             Plugin system with trust policies
  skills/              Skill marketplace
  skillforge/          Skill authoring tools
  rag/                 Retrieval-augmented generation
  cost/                Cost tracking and budgets
  cron/                Scheduled task engine
  daemon/              Background daemon with PID management
  approval/            Approval workflows
  auth/                OAuth, API key management, profiles
  browser_bridge/      Chrome extension bridge
  hooks/               Pre/post command hooks
  health/              Health checks
  heartbeat/           Proactive heartbeat engine
  hardware/            Hardware discovery and introspection
  tunnel/              Cloudflare/Tailscale/ngrok tunnels
  integrations/        Third-party integration registry
  soul/                Agent personality and identity
  doctor/              Diagnostic checks
  onboard/             First-time setup wizard
  service/             Service layer
docs/                  Topic-based documentation
.github/               CI, templates, automation workflows
```

---

## License

Dual-licensed under [MIT](LICENSE-MIT) or [Apache-2.0](LICENSE-APACHE), at your option.
