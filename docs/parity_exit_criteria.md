# Parity Exit Criteria

This document defines objective release gates for claiming Codex-grade parity in Telegram.

Last updated: 2026-02-22 (UTC)

## 1) Parity Dimensions and Hard Gates

All gates must pass in two consecutive runs before parity can be declared.

### A. Outcome Quality

- Metric: `completion_rate`
- Threshold: `>= 0.90`
- Source: parity harness summary (`docs/reports/parity-report-*.json`)
- Owner: Runtime + Prompting
- Issues: `#54`, `#60`, `#61`

- Metric: `expected_match_avg`
- Threshold: `>= 0.80`
- Source: parity harness summary
- Owner: Runtime + Prompting
- Issues: `#54`, `#60`, `#61`

- Metric: `similarity_to_baseline_avg`
- Threshold: `>= 0.60`
- Source: parity harness summary
- Owner: Runtime + Prompting
- Issues: `#54`, `#60`

### B. Operator Burden and UX

- Metric: `user_corrections_required_total`
- Threshold: `<= 2` for the standard suite
- Source: parity harness summary
- Owner: UX + Runtime
- Issues: `#58`, `#60`, `#61`

- Metric: approval interaction quality
- Threshold: 100% of high-risk actions expose actionable choice UI (`allow/deny/show pending`) and auditable status
- Source: integration smoke checklist + run events
- Owner: Runtime + Security
- Issues: `#57`, `#62`

### C. Latency and Throughput

- Metric: `latency_p95_sec`
- Threshold: `<= 45.0`
- Source: parity harness summary
- Owner: Runtime + Observability
- Issues: `#54`, `#55`, `#63`

### D. Safety and Abuse Resistance

- Metric: forbidden output failures
- Threshold: `0`
- Source: parity harness summary
- Owner: Security
- Issues: `#53`, `#57`, `#62`

- Metric: sandbox/workspace isolation
- Threshold: no cross-session workspace escape in security test suite
- Source: execution/security tests + policy tests
- Owner: Security
- Issues: `#53`, `#16`

### E. Reliability and Recovery

- Metric: deterministic recovery behavior
- Threshold: interrupted run resumes/cancels without duplicate destructive action in 100% tested flows
- Source: scenario tests + run event trails
- Owner: Runtime + Observability
- Issues: `#55`, `#62`, `#63`

- Metric: alerting coverage
- Threshold: failure/security alerts wired for all P0/P1 failure categories
- Source: runbook + hook tests
- Owner: Observability
- Issues: `#41`, `#55`, `#63`

## 2) Reproducible Evaluation Methods

### Method A: Parity Harness

Command:

```bash
PYTHONPATH=src python3 -m codex_telegram_bot.eval_parity \
  --cases docs/benchmarks/parity_cases.json \
  --workspace-root .
```

Weekly automation:

```bash
./scripts/run_parity_weekly.sh
```

Artifacts:

- `docs/reports/parity-report-latest.json`
- `docs/reports/parity-report-latest.md`

### Method B: Unit/Integration Regression

Command:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

Required: all tests pass, excluding explicitly dependency-gated skips.

### Method C: Telegram Smoke Checklist

Minimum checklist:

1. `/status` shows live run card fields.
2. `/new` creates and activates new session.
3. normal prompt completes and persists run/event state.
4. high-risk tool action requests approval.
5. approval UI offers actionable options (`allow once`, `deny`, `show pending`).
6. progress status message auto-cleans after completion.
7. `/interrupt` and `/continue` match policy/guard behavior.

## 3) Issue-to-Gate Mapping

Primary parity-critical stream:

- Measurement and scoring: `#54`, `#59`
- Security/isolation/approval safety: `#53`, `#57`, `#62`, `#16`
- Quality of code-edit interaction and long context: `#60`, `#61`
- Reliability and recovery ops: `#55`, `#63`, `#41`
- Final user/operator UX polish: `#58`, `#44`, `#19`

Secondary (post-parity platform scope):

- Provider negotiation and plugin/API platform: `#56`, `#45`, `#46`, `#47`, `#20`
- Legacy project/process docs and board structure: `#48`, `#38`, `#11`, `#10`, `#17`, `#18`

## 4) Milestone Sequence

### Milestone M1 (Critical Safety + Measurement)

- `#59`, `#54`, `#53`, `#57`, `#62`
- Exit: reliable pass/fail visibility + security baseline in place

### Milestone M2 (Interaction Quality)

- `#60`, `#61`, `#58`
- Exit: code-edit and long-session quality metrics consistently meet thresholds

### Milestone M3 (Production Reliability)

- `#55`, `#63`, `#41`, `#44`
- Exit: failure recovery and operational hooks validated

### Milestone M4 (Platform Expansion, Non-Blocking for Core Parity Claim)

- `#56`, `#45`, `#46`, `#47`, `#20`

## 5) Final Go / No-Go Checklist

All items must be checked `yes`:

1. Latest two parity reports pass all gates.
2. Latest two full test runs pass.
3. Security checklist signs off isolation and approval controls.
4. Reliability checklist signs off recovery + alerts.
5. UX checklist signs off operator flows and accessibility baseline.
6. Open P0 parity-critical issues count is zero (`#53`, `#54`, `#57`, `#59`, `#60`, `#61`, `#62`).

If any item is `no`: release remains `NO-GO`.

