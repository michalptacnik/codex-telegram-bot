# Session Handoff (Autonomy Roadmap)

Last updated: 2026-02-22

## What Is Already Done

- EPIC 1 is fully implemented and closed:
  - `#64`, `#69`, `#70`, `#71`, `#72`, `#73` (all closed)
- Implementation commit is on `main`:
  - `0e7faa4` (`feat: complete epic1 agent core foundation`)
- Core additions now in repo:
  - `src/codex_telegram_bot/agent_core/*`
  - `src/codex_telegram_bot/tools/*`
  - `capabilities/system.md`, `capabilities/git.md`, `capabilities/files.md`
  - provider message contract `generate(messages, stream=False)`
  - bounded memory default `SESSION_MAX_TURNS=20`

## New Autonomy Backlog Created

These are open and intended to make true 24/7 autonomous operation possible:

- EPIC 6 `#74` Autonomous Mission Runtime
  - `#75` `6.1 Mission model + state machine`
  - `#76` `6.2 Recurring scheduler + retry/backoff`
  - `#77` `6.3 Planner + task decomposition`
  - `#78` `6.4 Autonomous execution loop`
- EPIC 7 `#79` Work Intake and Connectors
  - `#80` `7.1 GitHub issue queue ingestion`
  - `#81` `7.2 Lead source connector framework`
  - `#82` `7.3 Dedup + scoring pipeline`
  - `#83` `7.4 Outbound action tools with safeguards`
- EPIC 8 `#84` Long-Horizon Mission Memory
  - `#85` `8.1 Durable mission memory store`
  - `#86` `8.2 Artifact and evidence index`
  - `#87` `8.3 Periodic summarization and compaction`
- EPIC 9 `#88` Unattended Safety, Budgets, and Watchdogs
  - `#89` `9.1 Autonomy policy modes`
  - `#90` `9.2 Mission budgets and kill switches`
  - `#91` `9.3 Watchdog + auto-recovery`
  - `#92` `9.4 Escalation and notifications`
- EPIC 10 `#93` 24/7 Operations and Reliability
  - `#94` `10.1 Daemon mode and supervisor integration`
  - `#95` `10.2 Mission observability dashboard`
  - `#96` `10.3 State backup and restore`
  - `#97` `10.4 Autonomous mission runbooks and chaos tests`

## Recommended Next Execution Order

1. EPIC 2 (`#65`) first:
   - safe subprocess, workspace isolation, git layer, ssh detection.
2. Then EPIC 6:
   - build mission state machine and scheduler before connectors.
3. Then EPIC 9:
   - enforce unattended safety before enabling continuous external actions.
4. Then EPIC 7 + EPIC 8 in parallel.
5. Then EPIC 10 for production-grade 24/7 ops.

## How A New Codex Instance Should Continue

From repo root:

```bash
git pull
PYTHONPATH=src python3 -m unittest discover -s tests
```

Then instruct Codex:

- "Open `docs/SESSION_HANDOFF.md`, then implement issue `#65` / `2.1` first."
- "After each issue, run tests, push to `main`, and comment/close the issue."
- "Keep `README.md` updated with any behavior/config changes."

## Important Notes

- New Codex sessions do **not** retain prior chat memory.
- Continuity should come from this file + GitHub issues + commit history.
- For GitHub project board actions, token needs `read:project` scope.
