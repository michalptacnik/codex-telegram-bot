#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
CONFIG_DIR="${AGENTHQ_CONFIG_DIR:-$ROOT_DIR/.runtime-config}"
LOG_DIR="${AGENTHQ_LOG_DIR:-$ROOT_DIR/.runtime-workspace}"
LAUNCH_DIR="$HOME/Library/LaunchAgents"
BOT_LABEL="com.agenthq.bot"
CTRL_LABEL="com.agenthq.controlcenter"
BOT_PLIST="$LAUNCH_DIR/$BOT_LABEL.plist"
CTRL_PLIST="$LAUNCH_DIR/$CTRL_LABEL.plist"
EXEC_BIN="$ROOT_DIR/.venv/bin/codex-telegram-bot"
UID_NUM="$(id -u)"

if [[ ! -x "$EXEC_BIN" ]]; then
  echo "Missing executable: $EXEC_BIN"
  echo "Create the local virtualenv first (expected at .venv/bin/codex-telegram-bot)."
  exit 1
fi

mkdir -p "$LAUNCH_DIR" "$CONFIG_DIR" "$LOG_DIR"

cat > "$BOT_PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$BOT_LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$EXEC_BIN</string>
    <string>--config-dir</string>
    <string>$CONFIG_DIR</string>
  </array>
  <key>WorkingDirectory</key>
  <string>$ROOT_DIR</string>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>ProcessType</key>
  <string>Background</string>
  <key>StandardOutPath</key>
  <string>$LOG_DIR/bot.log</string>
  <key>StandardErrorPath</key>
  <string>$LOG_DIR/bot.log</string>
</dict>
</plist>
EOF

cat > "$CTRL_PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$CTRL_LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$EXEC_BIN</string>
    <string>--control-center</string>
    <string>--host</string>
    <string>127.0.0.1</string>
    <string>--port</string>
    <string>8765</string>
    <string>--config-dir</string>
    <string>$CONFIG_DIR</string>
  </array>
  <key>WorkingDirectory</key>
  <string>$ROOT_DIR</string>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>ProcessType</key>
  <string>Background</string>
  <key>StandardOutPath</key>
  <string>$LOG_DIR/control-center.log</string>
  <key>StandardErrorPath</key>
  <string>$LOG_DIR/control-center.log</string>
</dict>
</plist>
EOF

# Stop ad-hoc runtime to prevent duplicate pollers/port conflicts.
"$ROOT_DIR/scripts/stop_macos_app.sh" || true

# Prune stale singleton lock files.
LOCK_DIR="$CONFIG_DIR/.locks"
mkdir -p "$LOCK_DIR"
for lock in "$LOCK_DIR"/*.lock; do
  [[ -f "$lock" ]] || continue
  pid="$(head -n1 "$lock" 2>/dev/null | tr -d '\r' | tr -d ' ')"
  if [[ -z "$pid" ]]; then
    rm -f "$lock"
    continue
  fi
  if ! kill -0 "$pid" 2>/dev/null; then
    rm -f "$lock"
  fi
done

# Reload launch agents.
launchctl bootout "gui/$UID_NUM/$BOT_LABEL" 2>/dev/null || true
launchctl bootout "gui/$UID_NUM/$CTRL_LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$UID_NUM" "$BOT_PLIST"
launchctl bootstrap "gui/$UID_NUM" "$CTRL_PLIST"
launchctl enable "gui/$UID_NUM/$BOT_LABEL" || true
launchctl enable "gui/$UID_NUM/$CTRL_LABEL" || true
launchctl kickstart -k "gui/$UID_NUM/$BOT_LABEL"
launchctl kickstart -k "gui/$UID_NUM/$CTRL_LABEL"

echo "Installed and started launch agents:"
echo "  - $BOT_LABEL"
echo "  - $CTRL_LABEL"
echo "Plists:"
echo "  - $BOT_PLIST"
echo "  - $CTRL_PLIST"
echo "Logs:"
echo "  - $LOG_DIR/bot.log"
echo "  - $LOG_DIR/control-center.log"
