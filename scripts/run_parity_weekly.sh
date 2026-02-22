#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if ! command -v codex >/dev/null 2>&1; then
  echo "codex CLI not found in PATH; parity run skipped." >&2
  exit 3
fi

export PYTHONPATH="${ROOT_DIR}/src"

set +e
python3 -m codex_telegram_bot.eval_parity \
  --cases docs/benchmarks/parity_cases.json \
  --workspace-root "$ROOT_DIR" \
  --output-dir docs/reports \
  "$@"
run_rc=$?
set -e

latest_json="$(ls -1t docs/reports/parity-report-*.json 2>/dev/null | head -n 1 || true)"
latest_md="$(ls -1t docs/reports/parity-report-*.md 2>/dev/null | head -n 1 || true)"

if [[ -n "$latest_json" ]]; then
  ln -sf "$(basename "$latest_json")" docs/reports/parity-report-latest.json
fi
if [[ -n "$latest_md" ]]; then
  ln -sf "$(basename "$latest_md")" docs/reports/parity-report-latest.md
fi

echo "weekly parity report completed"
exit "$run_rc"
