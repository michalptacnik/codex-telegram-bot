# Runtime Architecture

## Contract Boundary
- Model output is decoded into typed runtime events:
  - `AssistantText(content)`
  - `ToolCall(name, args, call_id)`
  - `ToolResult(call_id, output)`
  - `RuntimeError(kind, detail)`
- Telegram transport only receives assistant text produced from typed events.
- If output is protocol-like but undecodable, runtime applies parse-or-drop:
  - one structured retry in native loop, otherwise safe user-facing error text.

## Single Tool Registry Source
- Every turn builds a `ToolRegistrySnapshot` from one source registry.
- The same snapshot is used for:
  - tool schemas sent to model (`tools`)
  - tool lookup/execution in runtime loop
- Workspace invariants can disable tools (for example git tools when not in a git repo).

## Capabilities Manifest
- At session start/runtime snapshot refresh the bot writes:
  - `CAPABILITIES.md`
  - `AGENTS_INDEX.json`
- Manifest includes tools, disabled tools, repo/cwd invariants, and binary presence.
- The same manifest is injected into the system prompt.

## Orchestration Loop
- Native loop executes:
  1. model step
  2. decode events
  3. execute `0..n` tool calls
  4. append tool results
  5. repeat
- Loop terminates only when stop reason is final and no pending tool calls.
- Streamed tool-call deltas are assembled before decode.

## Workspace Invariants
- Session runtime tracks/logs:
  - `repo_root`
  - `cwd`
  - `is_git_repo`
- Git tools resolve `git rev-parse --show-toplevel` and execute inside repo root only.

## Persistence and Migrations
- SQLite startup creates/updates schema transactionally.
- `schema_version` table tracks applied schema level.
- DB path is deterministic and logged on startup.

## Transport and Lifecycle Hardening
- HTTP providers use bounded retry + exponential backoff + jitter for transient network faults.
- Telegram startup retries transient bootstrap failures instead of hard abort.
- Scheduler shutdown now tracks and awaits/cancels spawned job tasks to avoid pending-task destruction.

## Telegram Message Stability
- `MessageUpdater` serializes edits per `message_id`.
- Duplicate content is hash-deduped.
- Debounced update queue avoids edit races and suppresses harmless “message is not modified”.
