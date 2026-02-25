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

---

## Monitoring Integration

### Prometheus / Grafana

The Control Center exposes metrics at `GET /api/metrics` and `GET /api/reliability`.
To scrape these with Prometheus, poll the endpoints with a custom exporter or sidecar.

Example cURL scrape:

```bash
curl -s -H "x-local-api-key: $TOKEN" http://localhost:8080/api/reliability
```

Key fields to monitor:

| Field | Suggested metric name | Alert threshold |
|-------|-----------------------|-----------------|
| `failure_rate` | `codex_bot_run_failure_rate` | > 0.20 |
| `latency_p95_sec` | `codex_bot_run_latency_p95` | > 45 |
| `total_runs` | `codex_bot_runs_total` | — |

### Sample Grafana Dashboard (panels)

1. **Run outcomes** (completed / failed / running) — stacked bar from `/api/metrics`
2. **P95 latency** — line chart from `latency_p95_sec`
3. **Failure rate** — gauge with threshold at 0.20
4. **Active sessions** — stat from `/api/sessions` count
5. **Pending approvals** — stat from `/api/approvals` count

### Alert Rules (Alertmanager-style)

```yaml
groups:
  - name: codex-bot
    rules:
      - alert: HighRunFailureRate
        expr: codex_bot_run_failure_rate > 0.20
        for: 5m
        labels:
          severity: warning
      - alert: CriticalRunFailureRate
        expr: codex_bot_run_failure_rate > 0.50
        for: 2m
        labels:
          severity: critical
      - alert: HighP95Latency
        expr: codex_bot_run_latency_p95 > 45
        for: 5m
        labels:
          severity: warning
```

### Webhook Alert Payload

When `ALERT_WEBHOOK_URL` is configured, payloads look like:

```json
{
  "alert_type": "run.failed",
  "severity": "high",
  "run_id": "...",
  "agent_id": "default",
  "error_code": "ERR_CODEX_EXIT_NONZERO",
  "ts": "2026-02-25T12:00:00Z"
}
```
