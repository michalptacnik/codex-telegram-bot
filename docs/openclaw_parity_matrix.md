# OpenClaw Parity Matrix

This is the active parity matrix for the OpenClaw-style runtime goals.

## Status

- `Done`: implemented and verified in repo tests/docs.
- `Partial`: implemented with constraints or opt-in mode.
- `Planned`: not yet implemented.

| Area | Target | Status | Notes |
|---|---|---|---|
| Probe -> expand loop | Small probe before tool expansion | Done | `AgentService.run_prompt_with_tool_loop` + probe gating |
| Protocol enforcement | One repair retry, typed/runtime-safe decode | Done | Runtime contract + correction pass + loop reroute |
| Macro transpilation | Convert `!exec`/`!tool`/step syntaxes to executable actions | Done | `_extract_loop_actions` + `_enforce_need_tools_protocol_output` |
| Output firewall | Never leak protocol syntax to end users | Done | `enforce_transport_text_contract` + Telegram leak reroute |
| Allowed-tools gating | Per-turn filtered toolset | Done | Runtime snapshots + selected tool schemas |
| Responses API backend | Structured function/tool loop | Done | `providers/responses_api.py` |
| Skills lazy loading | Compact skill catalog + on-demand details | Done | `skill_pack.py` + `skill_manager.py` |
| MCP bridge | Lazy discovery + cache + gated invocation | Done | `mcp_bridge.py` (`mcp_search`, `mcp_call`) |
| Markdown memory | Daily markdown memory + retrieval tools | Done | `tools/memory.py`, `services/session_memory_files.py` |
| Gateway/control plane | Admin UI + sessions/runs APIs + health/audit | Done | `control_center/app.py` |
| Docker sandbox execution | Run tool commands in Docker sandbox | Partial | Opt-in runner via `EXECUTION_BACKEND=docker` |
| Signed trust model for skill packs | Signature verification pipeline | Planned | Not yet implemented |

## Gateway and Docker Clarification

- Gateway: implemented as the control center service (`--control-center`) with APIs for runs, sessions, reliability, and runtime capabilities.
- Docker: available as an opt-in execution backend for tool commands (`EXECUTION_BACKEND=docker`). Default remains local shell backend.

## Source Map

- Runtime map: `docs/runtime_map.md`
- Existing historical matrix: `docs/parity_matrix_openclaw.md`
