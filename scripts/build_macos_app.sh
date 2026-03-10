#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
APP_NAME="${APP_NAME:-AgentHQ}"
BUILD_DIR="${BUILD_DIR:-$ROOT_DIR/dist}"
INSTALL_DIR="${INSTALL_DIR:-$HOME/Applications}"
APP_DIR="$BUILD_DIR/${APP_NAME}.app"
CONTENTS_DIR="$APP_DIR/Contents"
MACOS_DIR="$CONTENTS_DIR/MacOS"

mkdir -p "$MACOS_DIR"

cat > "$CONTENTS_DIR/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key>
  <string>AgentHQ</string>
  <key>CFBundleDisplayName</key>
  <string>AgentHQ</string>
  <key>CFBundleIdentifier</key>
  <string>com.agenthq.desktop</string>
  <key>CFBundleVersion</key>
  <string>1.0.0</string>
  <key>CFBundleShortVersionString</key>
  <string>1.0.0</string>
  <key>CFBundleExecutable</key>
  <string>AgentHQLauncher</string>
  <key>CFBundlePackageType</key>
  <string>APPL</string>
  <key>LSMinimumSystemVersion</key>
  <string>12.0</string>
  <key>NSHighResolutionCapable</key>
  <true/>
</dict>
</plist>
PLIST

cat > "$MACOS_DIR/AgentHQLauncher" <<EOF
#!/usr/bin/env bash
set -euo pipefail

APP_ROOT="$ROOT_DIR"
CONFIG_DIR="\${AGENTHQ_CONFIG_DIR:-\$APP_ROOT/.runtime-config}"
BOT_LOG="\$CONFIG_DIR/bot.log"
CTRL_LOG="\$CONFIG_DIR/control.log"

mkdir -p "\$CONFIG_DIR"

if [[ -f "\$CONFIG_DIR/.env" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "\$CONFIG_DIR/.env"
  set +a
fi
if [[ -f "\$CONFIG_DIR/runtime.env" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "\$CONFIG_DIR/runtime.env"
  set +a
fi

if [[ ! -x "\$APP_ROOT/.venv/bin/codex-telegram-bot" ]]; then
  osascript -e 'display alert "AgentHQ launch failed" message "Missing .venv/bin/codex-telegram-bot in repo root."' >/dev/null 2>&1 || true
  exit 1
fi

# Always restart app-owned runtime cleanly to avoid duplicate polling conflicts.
pkill -f "codex-telegram-bot --control-center --host 127.0.0.1 --port 8765 --config-dir \$CONFIG_DIR" || true
bot_pids="\$(pgrep -af "codex-telegram-bot --config-dir \$CONFIG_DIR" | grep -v -- "--control-center" | awk '{print \$1}' || true)"
if [[ -n "\$bot_pids" ]]; then
  echo "\$bot_pids" | xargs kill || true
fi

if command -v screen >/dev/null 2>&1; then
  screen -S agenthq-app-bot -X quit || true
  screen -S agenthq-app-control -X quit || true
  screen -dmS agenthq-app-bot bash -lc "cd '\$APP_ROOT' && ./.venv/bin/codex-telegram-bot --config-dir '\$CONFIG_DIR' >> '\$BOT_LOG' 2>&1"
  screen -dmS agenthq-app-control bash -lc "cd '\$APP_ROOT' && ./.venv/bin/codex-telegram-bot --control-center --host 127.0.0.1 --port 8765 --config-dir '\$CONFIG_DIR' >> '\$CTRL_LOG' 2>&1"
else
  nohup bash -lc "cd '\$APP_ROOT' && ./.venv/bin/codex-telegram-bot --config-dir '\$CONFIG_DIR' >> '\$BOT_LOG' 2>&1" >/dev/null 2>&1 &
  nohup bash -lc "cd '\$APP_ROOT' && ./.venv/bin/codex-telegram-bot --control-center --host 127.0.0.1 --port 8765 --config-dir '\$CONFIG_DIR' >> '\$CTRL_LOG' 2>&1" >/dev/null 2>&1 &
fi

for _ in {1..40}; do
  if curl -fsS "http://127.0.0.1:8765/health" >/dev/null 2>&1; then
    break
  fi
  sleep 0.25
done

open "http://127.0.0.1:8765/chat" >/dev/null 2>&1 || true
EOF

chmod +x "$MACOS_DIR/AgentHQLauncher"

mkdir -p "$INSTALL_DIR"
rm -rf "$INSTALL_DIR/${APP_NAME}.app"
cp -R "$APP_DIR" "$INSTALL_DIR/${APP_NAME}.app"

echo "Built app bundle: $APP_DIR"
echo "Installed app bundle: $INSTALL_DIR/${APP_NAME}.app"
