# OpenClaw Parity Matrix

Feature-by-feature comparison between codex-telegram-bot and OpenClaw capabilities.

## Status Legend

- **Done** — Implemented and tested
- **Partial** — Implemented, needs refinement
- **N/A** — Not applicable

## Feature Matrix

| # | Feature Area | OpenClaw Capability | codex-telegram-bot Status | Issue | Notes |
|---|-------------|-------------------|--------------------------|-------|-------|
| 1 | **Responses API** | Structured tool-calling via `/v1/responses` | Done | #102 | `ResponsesApiProvider.run_tool_loop()` with iterative execution, `tool_schemas_from_registry()` helper |
| 2 | **MCP Bridge** | External tool ecosystem via MCP servers | Done | #103 | `McpBridge` with lazy discovery, TTL cache, SSRF protection, `mcp_search`/`mcp_call` tools |
| 3 | **Skill Packs** | SKILL.md-based skill lifecycle | Done | #104 | `SkillPackLoader` with YAML frontmatter, workspace > global > bundled precedence, gating |
| 4 | **Session Tools** | Agent-facing session operations | Done | #105 | `sessions_list`, `sessions_history`, `sessions_send`, `sessions_spawn`, `session_status` with visibility controls |
| 5 | **Memory Model** | Markdown-first memory with daily logs | Done | #106 | `memory/YYYY-MM-DD.md` daily logs, `MEMORY.md` curated, `memory_get`/`memory_search` tools, preload budget |
| 6 | **Tool Policy** | Group-based allow/deny with wildcards | Done | #107 | `ToolPolicyEngine` with group aliases, wildcard patterns, per-provider restrictions, `/elevated` modes |
| 7 | **Golden Scenarios** | CI-safe verification suite | Done | #108 | 5 golden scenarios in `tests/test_openclaw_parity.py`, CI-safe mocks |

## Architecture Invariants Preserved

| Invariant | Status |
|-----------|--------|
| Probe → Expand flow | Preserved — all new tools integrate with existing probe loop |
| Markdown-first memory | Native — daily logs and curated memory use `.md` files |
| Per-session workspace isolation | Preserved — memory and MCP cache scoped to workspace |
| Tool approval gates | Preserved — new tools can be added to `APPROVAL_REQUIRED_TOOLS` |
| Provider fallback/circuit breaker | Preserved — Responses API sits behind `ProviderRouter` |

## Golden Scenarios

1. **Write + verify + absolute path** — Write file via `write_file`, verify with `read_file`, confirm absolute path in output
2. **Memory write/search/get** — Write daily log, search for content, get specific lines
3. **Sessions spawn + background result** — Spawn session, check status, verify isolation
4. **MCP discover + call** — Register server, discover tools, search, call tool
5. **Approval flow for high-risk actions** — Trigger tool needing approval, verify gate

## Configuration Flags

| Flag | Default | Purpose |
|------|---------|---------|
| `ENABLE_RESPONSES_API` | `false` | Enable Responses API provider |
| `PROVIDER_BACKEND` | `codex-cli` | Select active provider backend |
| `ENABLE_MCP` | `false` | Enable MCP bridge |
| `MCP_ALLOWED_URL_PREFIXES` | (empty) | Allowlisted MCP server URL prefixes |
| `MCP_DISABLE_HTTP` | `true` | Block plain HTTP MCP servers |
| `ENABLE_PROBE_LOOP` | `false` | Enable probe-first tool selection |
