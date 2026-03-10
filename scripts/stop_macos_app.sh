#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
CONFIG_DIR="${AGENTHQ_CONFIG_DIR:-$ROOT_DIR/.runtime-config}"

screen -S agenthq-app-bot -X quit || true
screen -S agenthq-app-control -X quit || true
pkill -f "codex-telegram-bot --control-center --host 127.0.0.1 --port 8765 --config-dir $CONFIG_DIR" || true
bot_pids="$(pgrep -af "codex-telegram-bot --config-dir $CONFIG_DIR" | grep -v -- "--control-center" | awk '{print $1}' || true)"
if [[ -n "$bot_pids" ]]; then
  echo "$bot_pids" | xargs kill || true
fi

echo "Stopped AgentHQ processes for config dir: $CONFIG_DIR"
