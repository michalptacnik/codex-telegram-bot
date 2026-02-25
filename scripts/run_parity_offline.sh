#!/usr/bin/env bash
# run_parity_offline.sh — CI-safe parity harness run (no codex CLI required).
#
# Runs eval_parity.py with --offline-baseline so it uses synthetic expected-
# token output as the codex baseline instead of calling the real CLI.
# Useful in CI environments, fast smoke checks, and local development.
#
# Usage:
#   ./scripts/run_parity_offline.sh [extra eval_parity flags...]
#
# Exit codes:
#   0  all parity gates pass
#   2  one or more gates failed
#   1  unexpected error

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

export PYTHONPATH="${ROOT_DIR}/src"

mkdir -p docs/reports

set +e
python3 -m codex_telegram_bot.eval_parity \
  --cases docs/benchmarks/parity_cases.json \
  --workspace-root "$ROOT_DIR" \
  --output-dir docs/reports \
  --offline-baseline \
  "$@"
run_rc=$?
set -e

# Update latest symlinks
latest_json="$(ls -1t docs/reports/parity-report-*.json 2>/dev/null | head -n 1 || true)"
latest_md="$(ls -1t docs/reports/parity-report-*.md 2>/dev/null | head -n 1 || true)"
if [[ -n "$latest_json" ]]; then
  ln -sf "$(basename "$latest_json")" docs/reports/parity-report-latest.json
fi
if [[ -n "$latest_md" ]]; then
  ln -sf "$(basename "$latest_md")" docs/reports/parity-report-latest.md
fi

echo "offline parity run completed (rc=${run_rc})"
exit "$run_rc"
