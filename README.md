# Agent HQ

**The fastest, smallest AI assistant & Telegram bot. Powered by ZeroClaw engine.**

Agent HQ is a production-grade, multi-transport, multi-provider AI agent runtime built 100% in Rust. It combines the ZeroClaw engine (zero overhead, zero compromise) with rich Agent HQ features: soul/personality, missions, plugins, sessions, browser bridge, and a full web dashboard.

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│                         AGENT HQ                             │
│                                                              │
│  ┌────────────┐  ┌────────────┐  ┌────────────────────────┐  │
│  │  Chrome     │  │  VSCode    │  │  Web Dashboard (React) │  │
│  │  Extension  │  │  Extension │  │  + Gateway (axum)      │  │
│  └─────┬──────┘  └─────┬──────┘  └───────────┬────────────┘  │
│        └───────────────┼──────────────────────┘               │
│                        │                                      │
│  ┌─────────────────────▼───────────────────────────────────┐  │
│  │              ★ ZEROCLAW ENGINE (Rust) ★                 │  │
│  │                                                         │  │
│  │  Agent Loop │ 22+ LLM Providers │ Hybrid Memory         │  │
│  │  40+ Tools  │ Security/Sandbox  │ Cron/Scheduler        │  │
│  │  20+ Channels │ Gateway/Webhooks │ Observability        │  │
│  │                                                         │  │
│  │  + Soul/Personality │ Missions │ Plugins │ Sessions     │  │
│  │  + Browser Bridge │ GitHub Outbound │ Cost Tracking     │  │
│  └─────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────┘
```

## Features

### ZeroClaw Engine (Core)
- **22+ LLM Providers**: Anthropic, OpenAI, Gemini, Ollama, OpenRouter, Azure, Bedrock, Copilot, GLM, Telnyx, and more
- **40+ Tools**: Shell, file ops, git, HTTP, browser, cron, memory, email, screenshots, PDF, hardware, MCP, SOP
- **20+ Channels**: Telegram, Discord, Slack, Matrix, WhatsApp (API + Web), Signal, Email, IRC, MQTT, Nostr, DingTalk, Lark, Mattermost, Nextcloud Talk, QQ, iMessage, CLI
- **Hybrid Search Memory**: Vector + BM25 + FTS5 + RRF, backed by SQLite (+ optional Postgres/Qdrant)
- **Security**: Device pairing, sandboxing (bubblewrap/firejail/landlock), encrypted secrets, filesystem scoping, prompt injection detection, e-stop
- **Runtime**: Native + Docker + WASM execution
- **Gateway**: HTTP/WebSocket server with SSE events, webhook ingestion, tunnel support (Cloudflare/Tailscale/ngrok)
- **Cron/Scheduler**: Persistent scheduled tasks with cron expressions, one-shots, intervals
- **Observability**: Structured logging, Prometheus metrics, OpenTelemetry traces
- **Hardware**: Arduino, STM32 Nucleo, Raspberry Pi GPIO, ESP32 peripheral support
- **Skills**: Community skill marketplace, skill auditing, SkillForge
- **Identity**: AIEOS (AI Entity Object Specification) + markdown identity formats
- **Cost Tracking**: Per-provider, per-model cost monitoring with daily/monthly budgets
- **Heartbeat**: Proactive agent with configurable intervals
- **Hooks**: Pre/post command hooks with audit logging

### Agent HQ Extensions
- **Soul/Personality**: Editable agent personality with name, voice, principles, boundaries, and style knobs
- **Autonomous Missions**: Multi-step mission execution with pause/resume/stop, budget enforcement, step-level retry
- **Plugin System**: Install/enable/disable plugins with manifest validation, trust policies, and audit trails
- **Session Management**: Per-(chat, user) isolated workspaces with disk quotas and message history
- **Browser Bridge**: Chrome extension coordination for browser automation
- **GitHub Outbound**: Create issues, comment on PRs, list/close issues
- **Web Dashboard**: React SPA with Soul editor, Mission control, Plugin manager, Session browser, and all ZeroClaw pages

### Extensions
- **Chrome Extension**: AgentHQ Chrome Bridge for browser context passing
- **VSCode Extension**: Agent HQ sidebar with real-time chat

## Quick Start

```bash
# Build
cargo build --release

# Onboard (interactive setup)
./target/release/agent-hq onboard

# Start with channels
./target/release/agent-hq channel start

# Start gateway (web dashboard)
./target/release/agent-hq gateway start

# Or use Make
make build-release
make web        # Build web dashboard
make docker     # Build Docker image
```

## Configuration

Configuration is stored in `~/.zeroclaw/config.toml` (TOML format).

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
```

See `agent-hq onboard --interactive` for guided setup.

## Build from Source

### Prerequisites
- Rust 1.87+ (`rustup install stable`)
- Node.js 18+ (for web dashboard)

```bash
# Full build (engine + web dashboard)
make all

# Development
cargo run -- chat         # Interactive CLI chat
cargo run -- gateway start  # Start web dashboard

# Docker
docker build -t agent-hq .
docker compose up -d
```

## Migration from Python codex-telegram-bot

If you're upgrading from the Python version:

1. Your Telegram bot token and settings can be migrated: `make migrate-from-python`
2. Run `agent-hq onboard --reinit` to set up the new Rust configuration
3. Your SOUL.md and memory files are automatically loaded from the workspace

## License

MIT OR Apache-2.0
