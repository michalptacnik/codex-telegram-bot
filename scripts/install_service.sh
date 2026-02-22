#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: install_service.sh [--user|--system] [--workdir DIR] [--config-dir DIR] [--entrypoint CMD]

Defaults:
  --user
  --workdir current directory
  --config-dir ~/.config/codex-telegram-bot
  --entrypoint codex-telegram-bot
  --skip-reload false
EOF
}

MODE="user"
WORKDIR="$(pwd)"
CONFIG_DIR="$HOME/.config/codex-telegram-bot"
ENTRYPOINT="codex-telegram-bot"
SKIP_RELOAD="false"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --user) MODE="user"; shift ;;
    --system) MODE="system"; shift ;;
    --workdir) WORKDIR="$2"; shift 2 ;;
    --config-dir) CONFIG_DIR="$2"; shift 2 ;;
    --entrypoint) ENTRYPOINT="$2"; shift 2 ;;
    --skip-reload) SKIP_RELOAD="true"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1"; usage; exit 1 ;;
  esac
 done

TEMPLATE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/../systemd"
TEMPLATE="$TEMPLATE_DIR/codex-telegram-bot.service.template"

if [[ ! -f "$TEMPLATE" ]]; then
  echo "Template not found: $TEMPLATE" >&2
  exit 1
fi

if [[ "$MODE" == "system" ]]; then
  SERVICE_PATH="/etc/systemd/system/codex-telegram-bot.service"
  WANTED_BY="multi-user.target"
else
  SERVICE_PATH="$HOME/.config/systemd/user/codex-telegram-bot.service"
  WANTED_BY="default.target"
  mkdir -p "$HOME/.config/systemd/user"
fi

sed \
  -e "s|{{WORKDIR}}|$WORKDIR|g" \
  -e "s|{{CONFIG_DIR}}|$CONFIG_DIR|g" \
  -e "s|{{ENTRYPOINT}}|$ENTRYPOINT|g" \
  -e "s|{{WANTED_BY}}|$WANTED_BY|g" \
  "$TEMPLATE" > "$SERVICE_PATH"

echo "Wrote service file: $SERVICE_PATH"

if [[ "$SKIP_RELOAD" == "true" ]]; then
  echo "Skipped systemd daemon-reload (--skip-reload)."
else
  echo "Reloading systemd..."
  if [[ "$MODE" == "system" ]]; then
    systemctl daemon-reload
    echo "Run: systemctl enable --now codex-telegram-bot"
  else
    if ! systemctl --user daemon-reload; then
      echo "Warning: could not reload user systemd manager in this session." >&2
      echo "Run later: systemctl --user daemon-reload && systemctl --user enable --now codex-telegram-bot" >&2
      exit 0
    fi
    echo "Run: systemctl --user enable --now codex-telegram-bot"
  fi
fi
