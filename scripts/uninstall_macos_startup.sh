#!/usr/bin/env bash
set -euo pipefail

BOT_LABEL="com.agenthq.bot"
CTRL_LABEL="com.agenthq.controlcenter"
LAUNCH_DIR="$HOME/Library/LaunchAgents"
BOT_PLIST="$LAUNCH_DIR/$BOT_LABEL.plist"
CTRL_PLIST="$LAUNCH_DIR/$CTRL_LABEL.plist"
UID_NUM="$(id -u)"

launchctl bootout "gui/$UID_NUM/$BOT_LABEL" 2>/dev/null || true
launchctl bootout "gui/$UID_NUM/$CTRL_LABEL" 2>/dev/null || true
rm -f "$BOT_PLIST" "$CTRL_PLIST"

echo "Removed launch agents:"
echo "  - $BOT_LABEL"
echo "  - $CTRL_LABEL"
