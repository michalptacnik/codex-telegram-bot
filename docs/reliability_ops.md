# Reliability Ops Guide

Operational guide for always-on Telegram agent reliability.

## Reliability Signals

Use:

- `GET /health` (includes `reliability` summary)
- `GET /api/reliability`
- `GET /api/metrics`

Key fields:

- `failure_rate`
- `latency_p95_sec`
- `recovery_events`
- `alerts_enabled`
- `alerts` (threshold/dedup/dead-letter counters)

## Alert Hook Configuration

Environment variables:

- `ALERT_WEBHOOK_URL` (optional HTTPS endpoint)
- `ALERT_WEBHOOK_TIMEOUT_SEC` (default `3`)
- `ALERT_MIN_SEVERITY` (`low|medium|high|critical`, default `medium`)
- `ALERT_DEDUP_WINDOW_SEC` (default `90`)
- `ALERT_RETRY_COUNT` (default `2`)
- `ALERT_DEAD_LETTER_MAX` (default `200`)

Alert categories currently emitted:

- `run.failed`
- `tool.step.failed`
- `tool.approval.requested`

## SLO Baseline (Initial)

- Failure rate target: `<= 0.20` over rolling 300 runs.
- P95 run latency target: `<= 45s`.
- Recovery action traceability: 100% failed runs have recovery event trail.

## Incident Handling

1. Confirm health and reliability snapshots.
2. Pull failed run artifact and timeline.
3. Apply recovery action from playbook.
4. If failures persist:
   - fallback to default agent/profile
   - halt high-risk approvals
   - notify operators via alert sink.
