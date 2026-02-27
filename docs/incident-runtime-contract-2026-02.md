# Incident Report: Runtime Contract Breach (Tool Call Leak)

## Summary
Tool-like model output (`!tool ...`, `!exec ...`, `Step {cmd:...}|timeout=...`) leaked to Telegram and, in some cases, malformed payloads reached executors. This violated the runtime contract and caused user-visible nonsense plus execution failures.

## Root Cause
- Multiple output paths existed where assistant text could be forwarded without canonical tool-call validation.
- Legacy dialects were not consistently normalized before execution.
- Shell execution lacked persistent process/session primitives and did not provide structured poll/search retrieval.

## Fix Implemented
- Added `ProcessManager` with a single session lifecycle for process actions: `start/poll/write/terminate/status/list/search`.
- Upgraded `shell_exec` to support session mode (PTY default), short mode compatibility, and validated action dispatch.
- Added SQLite persistence for `process_sessions` and `process_session_chunks` (with migration + orphan recovery on startup).
- Enforced output hygiene caps (wall, idle, output bytes, ring buffer, max sessions per user).
- Enforced workspace-constrained log storage under `<workspace_root>/.runs`.
- Applied `redact_with_audit()` before both user-facing return text and persisted session logs.
- Wired Telegram command surface for process sessions: `/sessions`, `/tail`, `/kill`, and updated `/interrupt` + `/continue` behavior.
- Added process cleanup tick wiring in `AgentService`.

## Verification Evidence
- Added regression tests in `tests/test_process_manager.py`:
  - PTY lifecycle (start/write/poll/terminate)
  - short-mode compatibility
  - idle timeout cleanup
  - max output byte cap termination
  - redaction in both poll output and `.runs/*.log`
  - allowlist + workspace scope guards
  - orphan marking of running sessions on startup
- Added command registry test for `/sessions`, `/tail`, `/kill` in `tests/test_telegram_commands.py`.

## User Impact After Fix
- Raw tool-call text is no longer the default interaction path for process execution workflows.
- Long-running commands can be resumed safely via polling/search without full-log prompt inflation.
- Process lifecycle and logs are durable and queryable across service restarts.
